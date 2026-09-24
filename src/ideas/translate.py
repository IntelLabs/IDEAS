#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import sys
import logging
import traceback
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
from .codex import CodexConfig, CodexSnippetTranslator, CodexWrapperGenerator
from .oracle import TestOracle, UntrustedBaselineError
from .tools import Crate

logger = logging.getLogger("ideas.translate")


@dataclass
class TranslateConfig:
    model: ModelConfig = field(default_factory=ModelConfig)
    generate: GenerateConfig = field(default_factory=GenerateConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)

    cargo_toml: Path = MISSING
    bindings_cargo_toml: Path = MISSING
    tests: Path | None = MISSING
    template: str = "bin"
    deps: list[str] = field(default_factory=list)

    translator: str = "codex"
    translator_max_iters: int = 5
    wrapper: str = "codex"
    wrapper_max_iters: int = 5
    max_iters: int = 3
    share_test_evidence: bool = True

    vcs: str = "none"


cs = ConfigStore.instance()
cs.store(name="translate", node=TranslateConfig)


def copy_and_port_tests(src: Path, dst: Path, sys_crate_name: str, lib_name: str) -> None:
    sys_import = f"use {sys_crate_name}::*;"
    replacement = f"{sys_import}\nuse ::{lib_name} as _;"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text().replace(sys_import, replacement))


def _init_crates(cfg: TranslateConfig) -> tuple[Crate, Crate, Crate]:
    sys_crate = Crate(cfg.bindings_cargo_toml, vcs=cfg.vcs)  # type: ignore[reportArgumentType]

    # Create fresh -rs (pure Rust translation) crate Cargo.toml so cargo init always runs and registers the crate in workspace.members
    rs_cargo_toml = Path(str(cfg.cargo_toml.parent) + "-rs") / "Cargo.toml"
    rs_crate = Crate(
        rs_cargo_toml,
        vcs=cfg.vcs,  # type: ignore[reportArgumentType]
        template=cfg.template,  # type: ignore[reportArgumentType]
        reinit=True,
        jobs=4,
        codegen_units=4096,
    )
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
    workspace_cargo_lock = rs_crate.workspace_root / "Cargo.lock"
    if workspace_cargo_lock.exists():
        rs_crate.vcs.add(workspace_cargo_lock)
    rs_crate.vcs.commit(f"Created Rust translation crate '{rs_crate.name}'")

    # Create fresh hybrid crate (links -rs + -sys together) Cargo.toml so cargo init always runs and registers the crate in workspace.members
    crate = Crate(
        cfg.cargo_toml,
        vcs=cfg.vcs,  # type: ignore[reportArgumentType]
        template=cfg.template,  # type: ignore[reportArgumentType]
        reinit=True,
        jobs=4,
        codegen_units=4096,
    )
    crate.add_workspace_dependencies([rs_crate.name, sys_crate.name])
    crate.cargo_add("libc")
    for dep in cfg.deps:
        crate.cargo_add(dep)

    for dep in sys_crate.root_package["dependencies"]:
        # Do not inherit path dependencies from -sys crate
        if dep["kind"] == "dev" and dep.get("path") is None:
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

    # Port translation-time tests
    if cfg.tests is not None:
        test_path = crate.cargo_toml.parent / "tests" / f"{cfg.tests.stem}.rs"
        assert sys_crate.lib_name is not None, "Expected a library target in the -sys crate!"
        assert crate.lib_name is not None, "Expected a library target in the hybrid crate!"
        copy_and_port_tests(cfg.tests, test_path, sys_crate.lib_name, crate.lib_name)
        crate.vcs.add(test_path)

    workspace_cargo_toml = crate.workspace_root / "Cargo.toml"
    if workspace_cargo_toml != crate.cargo_toml and workspace_cargo_toml.exists():
        crate.vcs.add(workspace_cargo_toml)
    workspace_cargo_lock = crate.workspace_root / "Cargo.lock"
    if workspace_cargo_lock.exists():
        crate.vcs.add(workspace_cargo_lock)
    crate.vcs.commit(f"Created hybrid crate '{crate.name}'")

    return crate, rs_crate, sys_crate


def _log_visible(cfg: TranslateConfig) -> None:
    if cfg.codex.sandbox != "danger-full-access":
        return
    logger.info("Visible directories:")
    # cargo_toml is <workspace>/<crate>/Cargo.toml
    root = cfg.cargo_toml.parent.parent.resolve()
    for d in [root, *root.parents]:
        try:
            names = sorted(p.name + ("/" if p.is_dir() else "") for p in d.iterdir())
        except OSError as e:
            names = [f"<{e.strerror}>"]
        logger.info(f"  {d}: {' '.join(names)}")


def _main(cfg: TranslateConfig) -> None:
    output_dir = Path(HydraConfig.get().runtime.output_dir)
    logger.info(f"Saving results to {output_dir}")
    _log_visible(cfg)

    crate, rs_crate, sys_crate = _init_crates(cfg)
    if cfg.translator_max_iters == 0:
        logger.warning("Init-only mode enabled, skipping translation!")
        return

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
    cache = crate.workspace_root / "cache.db"
    assert rs_crate.lib_src_path is not None, "Expected lib.rs to exist in -rs crate!"
    paths = (crate.workspace_root, rs_crate.lib_src_path, c_src_path)
    symbol_wrapper = None
    if cfg.wrapper_max_iters > 0:
        hybrid_src = crate.lib_src_path or crate.main_src_path
        assert hybrid_src is not None, "Expected lib.rs or main.rs to exist in hybrid crate!"
        symbol_wrapper = _make_wrapper(cfg, cache, *paths, hybrid_src=hybrid_src)
    symbol_oracle = None
    if cfg.tests is not None and symbol_wrapper is not None:
        symbol_oracle = TestOracle(
            crate, cfg.tests.stem, share_evidence=cfg.share_test_evidence
        )
        if cfg.share_test_evidence:
            logger.warning("Sharing test failure output with the translator!")
    agent = RecurrentTranslator(
        sys_crate=sys_crate,
        crate=crate,
        rs_crate=rs_crate,
        symbol_translator=_make_translator(cfg, cache, *paths),
        symbol_wrapper=symbol_wrapper,
        symbol_oracle=symbol_oracle,
        max_iters=cfg.max_iters,
    )

    # Run translation agent and write it to disk
    try:
        pred = agent(symbols, dependencies)
    except UntrustedBaselineError:
        # Broken input tests are not a translation failure, so don't record one
        raise
    except Exception as e:
        logger.exception(e)
        crate.vcs.commit(f"{type(e).__name__}\n\n{e}\n{traceback.format_exc()}")
        pred = dspy.Prediction(complete=False, regressions={})

    # Wrapping is never exhaustive, so on its own a passing test suite only proves the hybrid
    # works, not that Rust is what made it work. Dropping every C body the wrappers did not
    # replace is the guarantee: whatever still links is Rust, and whatever needed C now fails.
    agent.strand_c()

    # Every per-symbol verdict was reached while the surviving C could still carry the suite,
    # so this is the only run that measures the Rust alone
    findings = symbol_oracle.audit() if symbol_oracle is not None else ""

    if pred.regressions:
        logger.warning(
            "Test(s) regressed during this translation: "
            + ", ".join(
                f"`{t}` in `{' '.join(g)}`" for t, g in sorted(pred.regressions.items())
            )
        )

    # Same tail a symbol group's closing log carries, since a run is just their sum
    counts = (
        f" regressed={len(pred.regressions)} "
        f"total={len(symbol_oracle.expected_tests)} "
        f"baseline_failures={len(symbol_oracle.baseline_failures)}"
        if symbol_oracle is not None
        else ""
    )

    usage = model.format_usage(pred)
    if pred.complete and not findings:
        msg = f"Translated {cfg.template} `{crate.name}` to Rust: {usage}{counts}"
        logger.info(msg)
    else:
        msg = f"Failed to translate {cfg.template} `{crate.name}` to Rust: {usage}{counts}"
        logger.error(msg)

    if findings:
        msg += f"\n\n{findings}"

    if pred.regressions:
        regressed = "\n".join(
            f"- `{t}` in `{' '.join(g)}`" for t, g in sorted(pred.regressions.items())
        )
        msg += f"\n\n# Regressed Tests\n{regressed}"

    # Commit translation
    if (output_subdir := HydraConfig.get().output_subdir) is not None:
        crate.vcs.add(output_dir / output_subdir)
    crate.vcs.commit(msg)

    crate.cargo_clean(workspace=True)


def _make_translator(
    cfg: TranslateConfig, cache: Path, workdir: Path, rust_src: Path, c_src: Path
) -> SnippetTranslator:
    if cfg.translator != "codex":
        return SnippetTranslator(
            getattr(dspy, cfg.translator), cfg.translator_max_iters, cache=cache
        )
    return CodexSnippetTranslator(
        cache=cache,
        codex=cfg.codex,
        model=cfg.model,
        workdir=workdir,
        rust_src=rust_src,
        c_src=c_src,
        max_iters=cfg.translator_max_iters,
    )


def _make_wrapper(
    cfg: TranslateConfig,
    cache: Path,
    workdir: Path,
    rust_src: Path,
    c_src: Path,
    *,
    hybrid_src: Path,
) -> WrapperGenerator:
    if cfg.wrapper != "codex":
        return WrapperGenerator(getattr(dspy, cfg.wrapper), cfg.wrapper_max_iters, cache=cache)
    return CodexWrapperGenerator(
        cache=cache,
        codex=cfg.codex,
        model=cfg.model,
        workdir=workdir,
        rust_src=rust_src,
        c_src=c_src,
        hybrid_src=hybrid_src,
        max_iters=cfg.wrapper_max_iters,
    )


@hydra.main(version_base=None, config_name="translate")
def main(cfg: TranslateConfig) -> None:
    try:
        _main(cfg)
    except Exception as e:
        logger.exception(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
