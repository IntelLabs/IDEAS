#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import sys
import logging
from pathlib import Path
from dataclasses import dataclass, field

import dspy
import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig

from ideas import adapters, model, ModelConfig, GenerateConfig
from ideas import SnippetTranslator, RecurrentTranslator, WrapperGenerator
from ideas import create_translation_unit, extract_info_c
from ideas.consolidate import get_symbols_and_dependencies
from .tools import Crate

logger = logging.getLogger("ideas.translate")


@dataclass
class TranslateConfig:
    model: ModelConfig = field(default_factory=ModelConfig)
    generate: GenerateConfig = field(default_factory=GenerateConfig)

    cargo_toml: Path = MISSING
    bindings_cargo_toml: Path = MISSING
    tests: str | None = MISSING
    template: str = "bin"
    deps: list[str] = field(default_factory=list)

    translator: str = "ChainOfThought"
    translator_max_iters: int = 5
    wrapper: str = "ChainOfThought"
    wrapper_max_iters: int = 5
    max_iters: int = 3

    vcs: str = "none"


cs = ConfigStore.instance()
cs.store(name="translate", node=TranslateConfig)


def _init_crates(cfg: TranslateConfig) -> tuple[Crate, Crate, Crate]:
    sys_crate = Crate(cfg.bindings_cargo_toml, vcs=cfg.vcs)  # type: ignore[reportArgumentType]

    # Create fresh -rs (pure Rust translation) crate Cargo.toml so cargo init always runs and registers the crate in workspace.members
    rs_cargo_toml = Path(str(cfg.cargo_toml.parent) + "-rs") / "Cargo.toml"
    rs_crate = Crate(rs_cargo_toml, vcs=cfg.vcs, template=cfg.template, reinit=True)  # type: ignore[reportArgumentType]
    # Binary -rs crates always have a lib.rs file with all the functions (including main) translated to safe Rust
    if cfg.template == "bin":
        assert rs_crate.main_src_path is not None, "Expected main.rs to exist in -rs crate!"
        (rs_crate.main_src_path.parent / "lib.rs").touch()
        rs_crate.invalidate_metadata()

    for dep in cfg.deps:
        rs_crate.cargo_add(dep)
    rs_crate.vcs.add(rs_crate.cargo_toml.parent)
    workspace_cargo_toml = rs_crate.workspace_root / "Cargo.toml"
    if workspace_cargo_toml != rs_crate.cargo_toml and workspace_cargo_toml.exists():
        rs_crate.vcs.add(workspace_cargo_toml)
    rs_crate.vcs.commit(f"Created Rust translation crate '{rs_crate.name}'")

    # Create fresh hybrid crate (links -rs + -sys together) Cargo.toml so cargo init always runs and registers the crate in workspace.members
    crate = Crate(cfg.cargo_toml, vcs=cfg.vcs, template=cfg.template, reinit=True)  # type: ignore[reportArgumentType]
    crate.add_workspace_dependencies([rs_crate.name, sys_crate.name])
    for dep in cfg.deps:
        crate.cargo_add(dep)

    for dep in sys_crate.root_package["dependencies"]:
        if dep["kind"] == "dev":
            name_req = dep["name"] + "@" + dep["req"]
            crate.cargo_add(name_req, section="dev", features=dep["features"])
    if cfg.template == "bin":
        crate.configure_target("bin", name=crate.name, test=False, doctest=False)
        # Ensure binary hybrid crates also expose a lib target so wrapper module unit
        # tests are discoverable by cargo test/nextest.
        assert crate.main_src_path is not None, "Expected main.rs to exist in hybrid crate!"
        (crate.main_src_path.parent / "lib.rs").touch()
        crate.invalidate_metadata()
    elif cfg.template == "lib":
        crate.configure_target(
            "lib",
            name=crate.name.removeprefix("lib"),
            test=False,
            doctest=False,
            crate_type=["lib", "cdylib"],
        )
    else:
        raise NotImplementedError(f"Unsupported template: {cfg.template!r}")
    crate.vcs.add(crate.cargo_toml.parent)

    # Copy the translation test from the -sys crate; skipped entirely when tests=null
    if cfg.tests is not None:
        sys_test_src = (
            cfg.bindings_cargo_toml.parent / "tests" / f"{cfg.tests}.rs"
        ).read_text()
        test_path = crate.cargo_toml.parent / "tests" / f"{cfg.tests}.rs"
        test_path.parent.mkdir(parents=True, exist_ok=True)
        # Prepend `use <lib> as _;` so the hybrid crate's `#[export_name]` functions are
        # retained by the linker. The translation loop makes each C function extern-only
        # (via clang_make_extern_), so if these Rust implementations are dead-code-eliminated
        # the test binary will fail to link with unresolved symbol errors.
        # FIXME: This could be removed if library tests were portable like binary tests
        if cfg.template == "lib":
            sys_test_src = f"use {crate.lib_name} as _;\n" + sys_test_src
        test_path.write_text(sys_test_src)
        crate.vcs.add(test_path)

    workspace_cargo_toml = crate.workspace_root / "Cargo.toml"
    if workspace_cargo_toml != crate.cargo_toml and workspace_cargo_toml.exists():
        crate.vcs.add(workspace_cargo_toml)
    crate.vcs.commit(f"Created hybrid crate '{crate.name}'")

    return crate, rs_crate, sys_crate


def _main(cfg: TranslateConfig) -> None:
    output_dir = Path(HydraConfig.get().runtime.output_dir)
    logger.info(f"Saving results to {output_dir}")

    crate, rs_crate, sys_crate = _init_crates(cfg)

    # Get global symbol table
    assert sys_crate.lib_src_path is not None, "Expected lib.rs to exist in -sys crate!"
    c_src_path = sys_crate.lib_src_path.with_suffix(".c")
    tu = create_translation_unit(c_src_path)
    asts = [extract_info_c(tu)]
    symbols, dependencies = get_symbols_and_dependencies(
        asts, external_symbol_names=["c:@F@main"] if cfg.template == "bin" else None
    )

    # Create translation agent
    model.configure(cfg.model, cfg.generate)
    dspy.configure(adapter=adapters.ChatAdapter())
    translator = getattr(dspy, cfg.translator)
    wrapper = getattr(dspy, cfg.wrapper)
    cache = crate.workspace_root / "cache.db"
    symbol_wrapper = None
    tests = None
    if cfg.wrapper_max_iters > 0:
        symbol_wrapper = WrapperGenerator(wrapper, cfg.wrapper_max_iters, cache=cache)
        tests = cfg.tests
    agent = RecurrentTranslator(
        sys_crate=sys_crate,
        crate=crate,
        rs_crate=rs_crate,
        symbol_translator=SnippetTranslator(translator, cfg.translator_max_iters, cache=cache),
        symbol_wrapper=symbol_wrapper,
        tests=tests,
        max_iters=cfg.max_iters,
    )

    msg = f"Initialized translation of {cfg.template} `{crate.name}` ({len(symbols)} symbols)"
    logger.info(msg)
    crate.vcs.commit(msg)

    # Run translation agent and write it to disk
    try:
        pred = agent(symbols, dependencies)
    except Exception as e:
        logger.exception(e)
        pred = dspy.Prediction(success=False)

    usage = model.format_usage(pred)
    if not pred.success:
        msg = f"Failed to translate `{crate.name}`: {usage}"
        logger.error(msg)

        # Force test failures by stubbing out the hybrid crate
        if crate.lib_src_path is not None:
            crate.lib_src_path.write_text("\n")
            crate.vcs.add(crate.lib_src_path)
        if crate.main_src_path is not None:
            crate.main_src_path.write_text("\n")
            crate.vcs.add(crate.main_src_path)
    else:
        msg = f"Translated {cfg.template} `{crate.name}` to Rust: {usage}"
        logger.info(msg)

    # Commit translation
    if (output_subdir := HydraConfig.get().output_subdir) is not None:
        crate.vcs.add(output_dir / output_subdir)
    crate.vcs.commit(msg)


@hydra.main(version_base=None, config_name="translate")
def main(cfg: TranslateConfig) -> None:
    try:
        _main(cfg)
    except Exception as e:
        logger.exception(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
