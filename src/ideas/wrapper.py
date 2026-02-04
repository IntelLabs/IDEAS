#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import re
import logging
from pathlib import Path
from collections import defaultdict, OrderedDict
from dataclasses import dataclass, field

import dspy
import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig

from ideas import adapters, model, ModelConfig, GenerateConfig
from ideas.tools import Crate, check_rust, run_subprocess
from ideas.adapters import Code
from ideas.ast_rust import validate_changes

logger = logging.getLogger("ideas.wrapper")
CodeRust = Code["rust"]


@dataclass
class WrapperConfig:
    model: ModelConfig = field(default_factory=ModelConfig)
    generate: GenerateConfig = field(default_factory=GenerateConfig)

    symbols: Path = MISSING
    cargo_toml: Path = MISSING

    max_iters: int = 5

    vcs: str = "none"


cs = ConfigStore.instance()
cs.store(name="wrapper", node=WrapperConfig)


class Signature(dspy.Signature):
    """
    Implement a C-compatible FFI wrapper for `crate::{symbol_name}` by replacing the `unimplemented!()` macro in `example_wrapper`.
    **Only** modify the body of the function, and **nothing** else.
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


class WrapperGenerator(dspy.Module):
    def __init__(
        self,
        crate: Crate,
        max_iters: int,
    ) -> None:
        super().__init__()
        self.crate = crate
        self.max_iters = max_iters

        # Setup crate for wrappers by writing empty wrapper.rs and adding `pub mod wrapper;` to lib.rs
        self.wrapper_path = crate.rust_src_path.parent / "wrapper.rs"
        self.wrapper_path.write_text("")
        if not re.search(
            r"^pub mod wrapper;$", crate.rust_src_path.read_text(), flags=re.MULTILINE
        ):
            with crate.rust_src_path.open("a+") as f:
                f.write("\npub mod wrapper;\n")

    def forward(
        self,
        symbol_name: str,
    ) -> dspy.Prediction:
        logger.info(f"Generating wrapper for symbol `{symbol_name}` ...")

        # Write blank wrapper and ensure only that blank wrapper is referenced since we're going to build
        symbol_wrapper_path = self.wrapper_path.parent / "wrapper" / f"{symbol_name}.rs"
        symbol_wrapper_path.parent.mkdir(exist_ok=True, parents=True)
        symbol_wrapper_path.write_text("")
        self.wrapper_path.write_text(f"pub mod {symbol_name};\n")

        # Try building the crate with an empty wrapper and if it fails then just return the unimplemented wrapper
        max_iters = max(1, self.max_iters) if self.crate.cargo_build() == (True, "") else 0

        # Use bindgen to generate unimplemented wrapper and write to disk. Note the unimplemented
        # wrapper contains unsafe code!
        unimplemented_wrapper = self.generate_unimplemented_wrapper(symbol_name)
        symbol_wrapper_path.write_text(unimplemented_wrapper)

        # Generate dynamic signature and module for symbol
        signature = Signature.with_instructions(
            Signature.instructions.format(
                symbol_name=symbol_name,
                crate_path=self.crate.rust_src_path.relative_to(self.crate.cargo_toml.parent),
                wrapper_path=symbol_wrapper_path.relative_to(self.crate.cargo_toml.parent),
            )
        )
        generate_wrapper = dspy.ChainOfThought(signature)

        # Try generating wrapper up to max_iter times
        code = self.crate.rust_src_path.read_text()
        wrapper, success, build_feedback = "", False, ""
        scope_feedback: OrderedDict[str, str] = OrderedDict()
        for i in range(max_iters):
            pred = generate_wrapper(
                crate=CodeRust(code=code),
                example_wrapper=CodeRust(code=unimplemented_wrapper),
                prior_wrapper=CodeRust(code=wrapper),
                build_feedback=build_feedback,
                scope_feedback="\n\n".join(scope_feedback.values()),
            )
            # Reset scope feedback
            scope_feedback.clear()

            if pred.wrapper is None:
                scope_feedback["no_wrapper"] = (
                    "No wrapper was generated. You must respect the template and instructions **exactly**!"
                )
                wrapper = unimplemented_wrapper
            else:
                wrapper = pred.wrapper.code
                # Validate that changes are in scope
                scope_feedback.update(validate_changes(wrapper, unimplemented_wrapper))

                # TODO: Check for a single crate function call in scope

            # Write wrapper to disk and check if we build with unsafe code since wrappers can use unsafe code
            symbol_wrapper_path.write_text(wrapper)
            self.crate.add(self.crate.rust_src_path, self.wrapper_path, symbol_wrapper_path)
            success, build_feedback = self.crate.cargo_build(allow_unsafe=True)
            if success and not build_feedback and not scope_feedback:
                self.crate.commit(
                    f"Wrapped symbol `{symbol_name}`\n\n# Reasoning\n{pred.reasoning}"
                )
                break

            self.crate.commit(
                f"Failed to wrap symbol `{symbol_name}` ({i + 1}/{max_iters})!\n\n"
                f"# Reasoning\n{pred.reasoning}\n\n"
                f"# Build feedback\n{build_feedback}\n\n"
                f"# Scope Feedback\n{scope_feedback}"
            )
        else:
            logger.warning(f"Wrapper generation failed after {max_iters} feedback iterations!")
        return dspy.Prediction(
            wrapper_path=self.wrapper_path.relative_to(self.crate.cargo_toml.parent),
            wrapper=self.wrapper_path.read_text(),
            symbol_wrapper_path=symbol_wrapper_path.relative_to(self.crate.cargo_toml.parent),
            symbol_wrapper=wrapper if success else unimplemented_wrapper,
            success=success,
        )

    def generate_unimplemented_wrapper(self, symbol_name) -> str:
        # unsafe extern "C" {
        #     pub fn helloworld() -> ::std::os::raw::c_int;
        # }
        ok, bindgen_wrapper = run_subprocess(
            [
                "bindgen",
                "--disable-header-comment",
                "--no-doc-comments",
                "--no-layout-tests",
                str(self.crate.rust_src_path.with_suffix(".c")),
                "--allowlist-function",
                symbol_name,
            ]
        )
        if not ok:
            raise ValueError(f"bindgen failed!\n{bindgen_wrapper}")

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
            raise ValueError("Failed to convert bindgen to valid wrapper!")
        unimplemented_wrapper = unimplemented_wrapper.rstrip()

        # Validate the template
        success, output = check_rust(
            unimplemented_wrapper, flags=["--crate-type", "lib", "--emit", "metadata"]
        )
        if not success:
            raise ValueError(
                f"Invalid template for the wrapper: {unimplemented_wrapper}\n\nBuild error:\n{output}"
            )
        return unimplemented_wrapper


@hydra.main(version_base=None, config_name="wrapper")
def main(cfg: WrapperConfig) -> None:
    output_dir = Path(HydraConfig.get().runtime.output_dir)
    logger.info(f"Saving results to {output_dir}")
    # FIXME: Why not use output_dir / Cargo.toml?
    crate = Crate(cargo_toml=cfg.cargo_toml.resolve(), vcs=cfg.vcs)  # type: ignore[reportArgumentType]

    model.configure(cfg.model, cfg.generate)
    dspy.configure(adapter=adapters.ChatAdapter())
    agent = WrapperGenerator(crate, max_iters=cfg.max_iters)
    wrappers: dict[Path, list[str]] = defaultdict(list)

    # Generate wrappers for each symbol
    wrapped = True
    for symbol_name in cfg.symbols.read_text().splitlines():
        pred = agent(symbol_name)
        symbol_wrapped: bool = pred.success
        symbol_wrapper: str = pred.symbol_wrapper
        symbol_wrapper_path: Path = pred.symbol_wrapper_path
        wrapper: str = pred.wrapper
        wrapper_path: Path = pred.wrapper_path

        # Write unimplemented wrapper to disk if generation failed
        if not symbol_wrapped:
            crate.write(symbol_wrapper_path, symbol_wrapper)
            crate.add(symbol_wrapper_path)
            crate.commit(f"Failed to wrap symbol `{symbol_name}`")
            wrapped = False

        # Save symbol wrapper declaration in wrapper_path. There should only ever be one
        # wrapper_path, but we handle the case where there are many.
        wrappers[wrapper_path].append(wrapper)

    # Update wrapper module with symbol wrapper declarations `pub mod {symbol_name};`;
    for wrapper_path, symbol_wrapper_declarations in wrappers.items():
        crate.write(wrapper_path, "\n".join(symbol_wrapper_declarations))
        crate.add(wrapper_path)

    # Commit wrappers
    if (output_subdir := HydraConfig.get().output_subdir) is not None:
        crate.add(output_dir / output_subdir)
    name = f"`{crate.root_package['name']}`"
    crate.commit(
        f"Wrapped all symbols in {name}" if wrapped else f"Failed to wrap all symbols in {name}"
    )


if __name__ == "__main__":
    main()
