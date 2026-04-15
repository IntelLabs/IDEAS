#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import re
import sys
import sqlite3
import logging
from pathlib import Path
from collections import OrderedDict
from dataclasses import dataclass, field

import dspy
import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig

from ideas import adapters, model, ModelConfig, GenerateConfig
from ideas.tools import Crate, check_rust, run_subprocess
from ideas import create_translation_unit, extract_info_c
from ideas.adapters import Code
from ideas.init.consolidate import get_symbols_and_dependencies
from ideas.ast_rust import get_nodes, get_root, validate_changes
from ideas.ast import Symbol

logger = logging.getLogger("ideas.wrapper")
CodeRust = Code["rust"]


@dataclass
class WrapperConfig:
    filename: Path = MISSING
    model: ModelConfig = field(default_factory=ModelConfig)
    generate: GenerateConfig = field(default_factory=GenerateConfig)

    cargo_toml: Path = MISSING

    max_iters: int = 5
    readonly_cache: Path | None = None

    vcs: str = "none"


cs = ConfigStore.instance()
cs.store(name="wrapper", node=WrapperConfig)


class Signature(dspy.Signature):
    """
    Output a C-compatible FFI wrapper for `crate::{symbol_name}`.
    Use `example_wrapper` as a template for the `wrapper` and replace the `unimplemented!()` part with an implementation.
    The implementation for `crate::{symbol_name}` is in a crate that was read from "{crate_path}".
    Assume the types in `crate::wrapper::` do not have the same memory layout as those in `crate::`.
    The wrapper should properly convert between `crate::wrapper::` and `crate::` types by copying the values from the wrapper type to the crate type before calling `crate::{symbol_name}`.
    After this conversion, the wrapper should call the Rust function `crate::{symbol_name}`.
    After the call to `crate::{symbol_name}`, the wrapper should convert back the `crate::` types to `crate::wrapper::` types.
    The wrapper will be written to "{wrapper_path}".
    You will receive feedback about a `prior_wrapper` attempt that should be fixed, if any.
    Use the `build_feedback` from `cargo build` about possible build errors.
    Use the `scope_feedback` about possible deviations from the templated `example_wrapper`.
    """

    # FIXME: Move crate and example_wrapper into instructions?
    crate: CodeRust = dspy.InputField()
    example_wrapper: CodeRust = dspy.InputField()
    prior_wrapper: CodeRust = dspy.InputField()
    build_feedback: str = dspy.InputField()
    scope_feedback: str = dspy.InputField()

    wrapper: CodeRust = dspy.OutputField()


def generate_unimplemented_wrapper(crate: Crate, symbol_name: str) -> str:
    # unsafe extern "C" {
    #     pub fn helloworld() -> ::std::os::raw::c_int;
    # }
    ok, bindgen_wrapper, error, _ = run_subprocess(
        [
            "bindgen",
            "--disable-header-comment",
            "--no-doc-comments",
            "--no-layout-tests",
            "--sort-semantically",
            str(crate.c_src_path),
            "--allowlist-function",
            symbol_name,
        ]
    )
    if not ok:
        raise ValueError(
            f"Bindgen failed to generate wrapper for `{symbol_name}`!\nError:\n{error}"
        )

    if bindgen_wrapper.strip() == "":
        raise ValueError(f"Bindgen generated an empty wrapper for `{symbol_name}`!")

    # #[unsafe(export_name="helloworld")]
    # pub extern "C" fn helloworld() -> ::std::os::raw::c_int {
    #     unimplemented!();
    # }
    unimplemented_wrapper = re.sub(
        r'unsafe extern "C" {\s+pub fn (.*);\s+}',
        rf'#[unsafe(export_name="{symbol_name}")]\npub extern "C" fn \1 {{\n    unimplemented!();\n}}',
        bindgen_wrapper,
        flags=re.DOTALL,
    )
    if unimplemented_wrapper == bindgen_wrapper:
        raise ValueError(
            f"Failed to convert bindgen output to function for `{symbol_name}`!\nWrapper:\n{unimplemented_wrapper}"
        )
    unimplemented_wrapper = unimplemented_wrapper.rstrip()

    # Validate the template
    success, output = check_rust(
        unimplemented_wrapper, flags=["--crate-type", "lib", "--emit", "metadata"]
    )
    if not success:
        raise ValueError(
            f"Failed to validate wrapper template for `{symbol_name}`!\nWrapper:\n{unimplemented_wrapper}\nError:\n{output}"
        )
    return unimplemented_wrapper.strip() + "\n"


class WrapperGenerator(dspy.Module):
    def __init__(
        self,
        crate: Crate,
        max_iters: int,
        readonly_cache: Path | None = None,
    ) -> None:
        super().__init__()
        self.crate = crate
        self.max_iters = max_iters
        self.readonly_cache = readonly_cache
        self.cache = _init_cache(crate.workspace_root / "cache.db")

        # Add sync module to crate
        sync_path = crate.rust_src_path.parent / "sync.rs"
        sync_path.write_text((Path(__file__).parent / "sync.rs").read_text())
        self.crate.vcs.add(sync_path)

        # Make sure wrapper module exists
        self.wrapper_path = crate.rust_src_path.parent / "wrapper.rs"
        self.wrapper_path.touch()

    def forward(self, symbol: Symbol, reference_code: str, translation: str) -> dspy.Prediction:
        if symbol.is_function and symbol.is_definition:
            return self.wrap_function(symbol, reference_code, translation)
        elif symbol.is_variable:
            return self.annotate_variable(symbol, reference_code, translation)
        else:
            logger.info(f"Skipping wrap of symbol `{symbol.name}`")
            return dspy.Prediction()

    def annotate_variable(
        self, symbol: Symbol, reference_code: str, translation: str
    ) -> dspy.Prediction:
        logger.info(f"Adding export_name attribute to variable `{symbol.name}` ...")
        orig_rust_src = self.crate.rust_src_path.read_text()
        assert translation in orig_rust_src, "translation must be on disk!"
        rust_src = orig_rust_src

        # Add export_name attribute to symbol translation
        new_translation = export_first_unannotated_variable(translation, symbol.spelling)
        if new_translation is None:
            logger.error(f"Failed to add export_name attribute to variable `{symbol.name}`")
            return dspy.Prediction(success=False, translation=translation)

        # Update Rust source with export_name attribute
        rust_src = rust_src.replace(translation, new_translation)
        self.crate.rust_src_path.write_text(rust_src)
        self.crate.vcs.add(self.crate.rust_src_path)

        # Replace Rust Mutex with C ABI-compatible Mutex
        RUST_MUTEX = "use std::sync::{Mutex, MutexGuard};"
        C_ABI_MUTEX = "mod sync;\nuse crate::sync::{Mutex, MutexGuard};"
        if RUST_MUTEX in rust_src:
            rust_src = rust_src.replace(RUST_MUTEX, C_ABI_MUTEX)
            self.crate.rust_src_path.write_text(rust_src)
            self.crate.vcs.add(self.crate.rust_src_path)

        self.crate.vcs.commit(f"Added export_name attribute to variable `{symbol.name}` ...")

        return dspy.Prediction(success=True, translation=new_translation)

    def wrap_function(
        self,
        symbol: Symbol,
        reference_code: str,
        translation: str,
        wrapper: str = "",
        prior_wrapper: str = "",
    ) -> dspy.Prediction:
        # Don't bother wrapping main in binary crates
        if symbol.spelling == "main" and self.crate.is_bin:
            return dspy.Prediction(success=True, translation=translation)

        logger.info(f"Generating wrapper for function `{symbol.name}` ...")

        # Write blank wrapper and ensure only that blank wrapper is referenced since we're going to build
        symbol_wrapper_path = self.wrapper_path.parent / "wrapper" / f"{symbol.spelling}.rs"
        symbol_wrapper_path.parent.mkdir(exist_ok=True, parents=True)
        symbol_wrapper_path.write_text("")

        # Try building the crate with an empty wrapper and if it fails then just return the unimplemented wrapper
        max_iters = max(1, self.max_iters) if self._build(symbol.spelling) == (True, "") else 0

        # Use bindgen to generate unimplemented wrapper and write to disk. Note the unimplemented
        # wrapper contains unsafe code!
        unimplemented_wrapper = generate_unimplemented_wrapper(self.crate, symbol.spelling)
        symbol_wrapper_path.write_text(unimplemented_wrapper)

        # Prefer supplied wrapper, crate cache, then read-only cache.
        wrapper = (
            wrapper
            or _read_cache(self.cache, symbol.spelling, unimplemented_wrapper)
            or _read_cache(self.readonly_cache, symbol.spelling, unimplemented_wrapper)
        )

        # Generate dynamic signature and module for symbol
        signature = Signature.with_instructions(
            Signature.instructions.format(
                symbol_name=symbol.spelling,
                crate_path=self.crate.rust_src_path.relative_to(self.crate.cargo_toml.parent),
                wrapper_path=symbol_wrapper_path.relative_to(self.crate.cargo_toml.parent),
            )
        )
        generate_wrapper = dspy.ChainOfThought(signature)

        # Try generating wrapper up to max_iter times
        msg = ""
        success, build_feedback = False, ""
        dspy_exception = None
        scope_feedback: OrderedDict[str, str] = OrderedDict()
        pred = dspy.Prediction()
        for i in range(max_iters):
            # Use the wrapper from the prior iteration as feedback for the next iteration
            if i > 0:
                prior_wrapper = wrapper

            try:
                if i == 0 and wrapper:
                    pred = dspy.Prediction(wrapper=CodeRust(code=wrapper))
                else:
                    pred = generate_wrapper(
                        crate=CodeRust(code=reference_code + "\n" + translation),
                        example_wrapper=CodeRust(code=unimplemented_wrapper),
                        prior_wrapper=CodeRust(code=prior_wrapper),
                        build_feedback=build_feedback,
                        scope_feedback="\n\n".join(scope_feedback.values()),
                    )
                dspy_exception = None
            except Exception as e:
                logger.exception(
                    f"DSPy exception while generating wrapper for `{symbol.name}` on iteration {i + 1}/{self.max_iters}!"
                )
                dspy_exception = e
                # Attempt again before any build logic
                continue

            # Reset scope feedback
            scope_feedback.clear()

            if pred.wrapper is None:
                scope_feedback["no_wrapper"] = (
                    "No wrapper was generated. You must respect the template and instructions **exactly**!"
                )
                wrapper = unimplemented_wrapper
            else:
                wrapper = pred.wrapper.code.strip() + "\n"
                # Validate that changes are in scope
                scope_feedback.update(validate_changes(wrapper, unimplemented_wrapper))

                # TODO: Check for a single crate function call in scope

            # Write wrapper to disk and check if we build with unsafe code since wrappers can use unsafe code
            symbol_wrapper_path.write_text(wrapper)
            self.crate.vcs.add(symbol_wrapper_path)
            success, build_feedback = self._build(symbol.spelling)
            success = success and not build_feedback and not scope_feedback

            if success:
                msg = f"Wrapped function `{symbol.name}`"
                logger.info(msg)
                if "reasoning" in pred:
                    msg += f"\n\n# Reasoning\n{pred.reasoning}"
                break

            msg = f"Failed to wrap function `{symbol.name}` ({i + 1}/{max_iters})"
            logger.error(msg)
            msg += f"\n\n# Reasoning\n{pred.reasoning}" if "reasoning" in pred else ""
            msg += f"\n\n# Build feedback\n{build_feedback}"
            msg += f"\n\n# Scope Feedback\n{scope_feedback}"
            self.crate.vcs.commit(msg)

        # Reference symbol wrapper in wrapper module
        with self.wrapper_path.open("a") as f:
            f.write(f"pub mod {symbol.spelling};\n")
        self.crate.vcs.add(self.wrapper_path)

        # Write unimplemented wrapper to disk if generation failed
        if not success:
            symbol_wrapper_path.write_text(unimplemented_wrapper)
            self.crate.vcs.add(symbol_wrapper_path)
            msg = f"Wrote unimplemented wrapper for `{symbol.name}`"
            logger.warning(msg)
        self.crate.vcs.commit(msg)

        # All iterations failed because of DSPy exceptions
        if dspy_exception:
            raise dspy_exception

        pred.success = success
        pred.name = symbol.spelling
        pred.translation = translation
        pred.wrapper = wrapper
        pred.bindgen_template = unimplemented_wrapper
        pred.prior_wrapper = prior_wrapper
        pred.build_feedback = build_feedback
        pred.scope_feedback = "\n\n".join(scope_feedback.values())
        return pred

    def _build(self, symbol_spelling: str) -> tuple[bool, str]:
        orig_rust_src = self.crate.rust_src_path.read_text()
        orig_wrapper_src = self.wrapper_path.read_text()

        # Reference wrapper module in Rust source
        with self.crate.rust_src_path.open("a") as f:
            f.write("pub mod wrapper;\n")
        self.crate.vcs.add(self.crate.rust_src_path)

        # Reference symbol wrapper module to wrapper module
        with self.wrapper_path.open("a") as f:
            f.write(f"pub mod {symbol_spelling};\n")
        self.crate.vcs.add(self.wrapper_path)

        # Check whether all of the changes compile and commit them
        success, feedback = self.crate.cargo_build(allow_unsafe=True)

        # Restore original source
        self.crate.rust_src_path.write_text(orig_rust_src)
        self.wrapper_path.write_text(orig_wrapper_src)

        return success, feedback

    def write_cache(self, pred: dspy.Prediction) -> None:
        required_fields = (
            "name",
            "bindgen_template",
            "prior_wrapper",
            "build_feedback",
            "scope_feedback",
            "wrapper",
            "success",
        )
        if not all(hasattr(pred, field) for field in required_fields):
            return

        _write_cache(
            self.cache,
            pred.name,
            pred.bindgen_template,
            pred.prior_wrapper,
            pred.build_feedback,
            pred.scope_feedback,
            pred.wrapper,
            pred.success,
        )


def export_first_unannotated_variable(rust_src: str, export_name: str) -> str | None:
    # Loop through nodes trying to find a static item
    attrs = []
    rust_bytes = rust_src.encode()
    for node in get_nodes(get_root(rust_bytes)):
        # Keep track of attributes
        if node.type == "attribute_item":
            attrs.append(node)
            continue

        # Reset list of attributes when we encounter non-static/non-attribute item
        elif node.type != "static_item":
            attrs = []
            continue

        # If export name already in attrs, skip this static item
        if any(b"export_name" in attr.text for attr in attrs if attr.text is not None):
            continue

        # FIXME: Warn if name of variable does not correspond to export_name

        # Insert attribute at location
        return (
            rust_bytes[: node.start_byte].decode()
            + f'#[unsafe(export_name="{export_name}")]\n'
            + rust_bytes[node.start_byte :].decode()
        )
    return None


def _init_cache(cache: Path | None) -> Path | None:
    if cache is None:
        return None
    with sqlite3.connect(cache) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wrapper_translations (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT    NOT NULL,
                bindgen_template TEXT    NOT NULL,
                prior_wrapper    TEXT    NOT NULL,
                build_feedback   TEXT    NOT NULL,
                scope_feedback   TEXT    NOT NULL,
                wrapper          TEXT    NOT NULL,
                success          INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wrapper_lookup"
            " ON wrapper_translations (name, bindgen_template)"
        )
    return cache


def _read_cache(cache: Path | None, name: str, bindgen_template: str) -> str:
    wrapper = ""
    if cache is None:
        return wrapper
    with sqlite3.connect(cache) as conn:
        try:
            row = conn.execute(
                "SELECT wrapper FROM wrapper_translations WHERE name=? AND bindgen_template=? AND success=1 ORDER BY id DESC LIMIT 1",
                (name, bindgen_template),
            ).fetchone()
        except Exception:
            row = None
    if row:
        logger.info(f"Cache hit for wrapper `{name}`")
        wrapper = row[0]
    return wrapper


def _write_cache(
    cache: Path | None,
    name: str,
    bindgen_template: str,
    prior_wrapper: str,
    build_feedback: str,
    scope_feedback: str,
    wrapper: str,
    success: bool,
) -> None:
    if cache is None:
        return
    with sqlite3.connect(cache) as conn:
        conn.execute(
            """
            INSERT INTO wrapper_translations
                (name, bindgen_template, prior_wrapper, build_feedback, scope_feedback, wrapper, success)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                bindgen_template,
                prior_wrapper,
                build_feedback,
                scope_feedback,
                wrapper,
                int(success),
            ),
        )


def _main(cfg: WrapperConfig) -> None:
    output_dir = Path(HydraConfig.get().runtime.output_dir)
    logger.info(f"Saving results to {output_dir}")
    crate = Crate(cargo_toml=cfg.cargo_toml.resolve(), vcs=cfg.vcs)  # type: ignore[reportArgumentType]

    model.configure(cfg.model, cfg.generate)
    dspy.configure(adapter=adapters.ChatAdapter())
    agent = WrapperGenerator(crate, max_iters=cfg.max_iters, readonly_cache=cfg.readonly_cache)

    # Remove forbid unsafe from Rust source
    rust_src = re.sub(re.escape("#![forbid(unsafe_code)]"), "", crate.rust_src_path.read_text())
    crate.rust_src_path.write_text(rust_src)

    # Get global symbol table
    tu = create_translation_unit(cfg.filename)
    asts = [extract_info_c(tu)]
    symbols, _ = get_symbols_and_dependencies(asts, source_priority=[])

    # Generate wrappers for each global function definition
    for symbol in symbols.values():
        if symbol.is_global and symbol.is_function and symbol.is_definition:
            agent(symbol, "", rust_src)

    # Reference wrapper in Rust source
    with crate.rust_src_path.open("a") as f:
        f.write("pub mod wrapper;\n")
    crate.vcs.add(crate.rust_src_path)

    success, feedback = crate.cargo_build(allow_unsafe=True)

    # Commit unsafe Rust code and wrappers
    if (output_subdir := HydraConfig.get().output_subdir) is not None:
        crate.vcs.add(output_dir / output_subdir)
    name = f"`{crate.root_package['name']}`"
    msg = f"Successfully wrapped all symbols in {name}!"
    if not success:
        msg = f"Failed to wrap all symbols in {name}!"
        logger.error(msg)
        msg += f"\n\n{feedback}"
    else:
        logger.info(msg)
    crate.vcs.commit(msg)


@hydra.main(version_base=None, config_name="wrapper")
def main(cfg: WrapperConfig) -> None:
    try:
        _main(cfg)
    except Exception as e:
        logger.exception(e)
        sys.exit(-1)


if __name__ == "__main__":
    main()
