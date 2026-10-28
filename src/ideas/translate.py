#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import sys
import logging
import traceback
from typing import Any, cast
from pathlib import Path
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass, field

import hydra
from omegaconf import MISSING, OmegaConf
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig

from ideas import model, ModelConfig
from ideas import SnippetTranslator, RecurrentTranslator, WrapperGenerator
from ideas import create_translation_unit, extract_info_c
from ideas.consolidate import get_symbols_and_dependencies
from .codex import CodexConfig
from .oracle import NullOracle, SymbolOracle, TestOracle
from .hybrid import SAFE_CRATE, Hybrid
from .tools import Crate, Workspace
from .translate_recurrent import SymbolTranslator, SymbolWrapper, TranslationReport

logger = logging.getLogger("ideas.translate")


@dataclass
class TranslateConfig:
    # _self_ first so the selected provider preset wins over this schema's own defaults
    defaults: list[Any] = field(default_factory=lambda: ["_self_", {"model": "openrouter"}])

    model: ModelConfig = MISSING
    codex: CodexConfig = field(default_factory=CodexConfig)

    cargo_toml: Path = MISSING
    bindings_cargo_toml: Path = MISSING
    tests: Path | None = MISSING
    template: str = "bin"
    deps: list[str] = field(default_factory=list)

    translator_max_iters: int = 5
    wrapper_max_iters: int = 5
    max_iters: int = 3
    share_test_evidence: bool = True
    keep_hybrid: bool = False

    vcs: str = "none"


cs = ConfigStore.instance()
cs.store(name="translate", node=TranslateConfig)


class TranslationWorkspace(Workspace):
    def __init__(self, cfg: TranslateConfig):
        self.sys_crate = Crate(cfg.bindings_cargo_toml, vcs=cfg.vcs)  # type: ignore[reportArgumentType]
        self.rs_crate = self._init_rs_crate(cfg)
        super().__init__(self.rs_crate.workspace.root, vcs=cfg.vcs)  # type: ignore[reportArgumentType]
        self.ffi_crate = self._init_ffi_crate(cfg) if cfg.template == "lib" else None
        self.hybrid_crate = self._init_hybrid_crate(cfg)
        self._tests = cfg.tests

        # A bare `cargo build` must skip the -sys bin, which cannot link once its C is stranded
        self.add_default_member(self.shipped_crate)

    @property
    def shipped_crate(self) -> Crate:
        return self.ffi_crate or self.rs_crate

    def audit(self, oracle: SymbolOracle) -> str:
        # The view keeps the baseline and blame `oracle` gathered against the hybrid
        with self.shipped_tests():
            return oracle.on(self.shipped_crate).audit()

    @contextmanager
    def shipped_tests(self) -> Iterator[None]:
        # -ffi keeps the tests it was given at init, as the hybrid does
        if self._tests is None or self.ffi_crate is not None:
            yield
            return
        crate = self.shipped_crate
        manifests = {
            path: path.read_bytes()
            for path in (crate.cargo_toml, self.cargo_toml, self.cargo_lock)
            if path.exists()
        }
        test_path = None
        try:
            # The tests name -sys, which -rs ships without
            crate.add_workspace_dependencies([self.sys_crate.name], dev=True)
            test_path = self._port_tests(crate, self._tests)
            yield
        finally:
            if test_path is not None:
                test_path.unlink()
            for path, content in manifests.items():
                path.write_bytes(content)
            crate.invalidate_metadata()

    @staticmethod
    def _target(cfg: TranslateConfig) -> str:
        return cfg.cargo_toml.parent.name.removesuffix(
            "-ffi" if cfg.template == "lib" else "-rs"
        )

    def _init_rs_crate(self, cfg: TranslateConfig) -> Crate:
        target = self._target(cfg)
        # Reinit so cargo init always registers it in workspace.members
        rs_crate = Crate(
            cfg.cargo_toml.parent.with_name(f"{target}-rs") / "Cargo.toml",
            vcs=cfg.vcs,  # type: ignore[reportArgumentType]
            template=cfg.template,  # type: ignore[reportArgumentType]
            reinit=True,
        )
        # Binary -rs crates always have a lib.rs file with all the functions (including main) translated to safe Rust
        if cfg.template == "bin":
            # -rs ships the binary, so it keeps the C target's name
            rs_crate.configure_target(
                "bin", name=target, test=False, doctest=False, path="src/main.rs"
            )
            assert rs_crate.main_src_path is not None, "Expected main.rs to exist in -rs crate!"
            (rs_crate.main_src_path.parent / "lib.rs").touch()
            rs_crate.invalidate_metadata()
            # The C target's name may not be snake case, and an untranslated `main` must fail
            # rather than exit 0
            rs_crate.main_src_path.write_text(
                "#![forbid(unsafe_code)]\n"
                "#![allow(non_snake_case)]\n\n"
                'fn main() {\n    unimplemented!("`main` was never translated to Rust");\n}\n'
            )
        # Generated crate names preserve C casing, and later translations may follow test modules.
        assert rs_crate.lib_src_path is not None, "Expected lib.rs to exist in -rs crate!"
        rs_crate.lib_src_path.write_text(
            "#![forbid(unsafe_code)]\n"
            "#![allow(clippy::items_after_test_module, non_snake_case)]\n\n"
        )

        for dep in cfg.deps:
            rs_crate.cargo_add(dep)
        rs_crate.vcs.add(rs_crate.cargo_toml.parent)
        rs_crate.workspace.stage()
        return rs_crate

    def _init_ffi_crate(self, cfg: TranslateConfig) -> Crate:
        # Ships lib<name>.so: the hybrid's wrappers for the C interface, linked against -sys only
        # for the storage of C globals, since `strand_c` leaves it no C functions
        ffi_crate = Crate(
            cfg.cargo_toml,
            vcs=cfg.vcs,  # type: ignore[reportArgumentType]
            template="lib",
            reinit=True,
        )
        self._link_crate(ffi_crate, cfg, lib_name=self._target(cfg).removeprefix("lib"))
        assert ffi_crate.lib_src_path is not None, "Expected lib.rs to exist in -ffi crate!"
        ffi_crate.lib_src_path.write_text("")
        ffi_crate.vcs.add(ffi_crate.lib_src_path)
        # The same tests as the hybrid, so what ships is tested as the hybrid was
        if cfg.tests is not None:
            test_path = self._port_tests(ffi_crate, cfg.tests)
            ffi_crate.vcs.add(ffi_crate.cargo_toml, test_path)
            self.stage()
        return ffi_crate

    def _init_hybrid_crate(self, cfg: TranslateConfig) -> Crate:
        # Reinit so cargo init always registers it in workspace.members
        hybrid_crate = Crate(
            cfg.cargo_toml.parent.with_name(f"{self._target(cfg)}-hybrid") / "Cargo.toml",
            vcs=cfg.vcs,  # type: ignore[reportArgumentType]
            template=cfg.template,  # type: ignore[reportArgumentType]
            reinit=True,
        )
        self._link_crate(hybrid_crate, cfg)
        if cfg.tests is not None:
            test_path = self._port_tests(hybrid_crate, cfg.tests)
            hybrid_crate.vcs.add(hybrid_crate.cargo_toml, test_path)
            self.stage()
        return hybrid_crate

    def _link_crate(
        self, crate: Crate, cfg: TranslateConfig, *, lib_name: str | None = None
    ) -> None:
        # Makes a freshly initialized crate depend on both -rs and -sys, the former only by
        # the crate-agnostic name its wrappers use
        crate.add_workspace_dependencies(
            [{SAFE_CRATE: self.rs_crate.name}, self.sys_crate.name]
        )
        crate.cargo_add("libc")
        for dep in cfg.deps:
            crate.cargo_add(dep)

        if crate.main_src_path is not None:
            crate.configure_target("bin", name=crate.name, test=False, doctest=False)
            # So cargo test/nextest discover the wrapper modules' unit tests
            (crate.main_src_path.parent / "lib.rs").touch()
            crate.invalidate_metadata()
        else:
            crate.configure_target(
                "lib", name=lib_name, test=False, doctest=False, crate_type=["lib", "cdylib"]
            )
        crate.vcs.add(crate.cargo_toml.parent)
        self.stage()

    # Copies the translation-time tests into `crate`, with the dev-dependencies they take from -sys
    def _port_tests(self, crate: Crate, tests: Path) -> Path:
        # Path dependencies are not inherited
        for dep in self.sys_crate.root_package["dependencies"]:
            if dep["kind"] == "dev" and dep.get("path") is None:
                crate.cargo_add(dep["name"], section="dev", features=dep["features"])

        assert self.sys_crate.lib_name is not None, (
            "Expected a library target in the -sys crate!"
        )
        assert crate.lib_name is not None, f"Expected a library target in {crate.name}!"
        # Link the crate under test into the tests, which otherwise only name -sys
        sys_import = f"use {self.sys_crate.lib_name}::*;"
        source = tests.read_text().replace(
            sys_import, f"{sys_import}\nuse ::{crate.lib_name} as _;"
        )
        # The tests find the binary by package name, but -rs ships it under the C target's
        if bins := crate.bin_targets:
            source = source.replace("assert_cmd::pkg_name!()", f'"{bins[0]["name"]}"')

        test_path = crate.cargo_toml.parent / "tests" / f"{tests.stem}.rs"
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(source)
        return test_path


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

    workspace = TranslationWorkspace(cfg)
    rs_crate = workspace.rs_crate
    hybrid = Hybrid(workspace.hybrid_crate, workspace.sys_crate, workspace.ffi_crate)

    # Get global symbol table
    tu = create_translation_unit(hybrid.c_src_path)
    asts = [extract_info_c(tu)]
    symbols, dependencies = get_symbols_and_dependencies(
        asts, external_symbol_names=["c:@F@main"] if cfg.template == "bin" else None
    )

    # Create translation agent
    cache = workspace.root / "cache.db"
    assert rs_crate.lib_src_path is not None, "Expected lib.rs to exist in -rs crate!"
    snippet_translator = SnippetTranslator(
        cache=cache,
        codex=cfg.codex,
        model=cfg.model,
        workdir=workspace.root,
        rust_src=rs_crate.lib_src_path,
        c_src=hybrid.c_src_path,
        max_iters=cfg.translator_max_iters,
        deps={
            name: pkg["description"] or ""
            for name, pkg in rs_crate.dependency_packages(cfg.deps).items()
        },
    )
    wrapper_generator = None
    if cfg.wrapper_max_iters > 0:
        wrapper_generator = WrapperGenerator(
            cache=cache,
            codex=cfg.codex,
            model=cfg.model,
            workdir=workspace.root,
            rust_src=rs_crate.lib_src_path,
            c_src=hybrid.c_src_path,
            max_iters=cfg.wrapper_max_iters,
        )
    oracle: SymbolOracle = NullOracle()
    if cfg.tests is not None and wrapper_generator is not None:
        oracle = TestOracle(
            hybrid.crate, cfg.tests.stem, share_evidence=cfg.share_test_evidence
        )
        if cfg.share_test_evidence:
            logger.warning("Sharing test failure output with the translator!")

    translator = SymbolTranslator(rs_crate, snippet_translator)
    wrapper = SymbolWrapper(hybrid, wrapper_generator, oracle)
    agent = RecurrentTranslator(translator, wrapper, oracle, max_iters=cfg.max_iters)

    agent.prepare(symbols.values())
    if cfg.translator_max_iters == 0:
        logger.warning("Init-only mode enabled, skipping translation!")
        return

    # Run translation agent and write it to disk
    # Nothing else installs the tracker every Codex turn reports its usage into
    with model.track_usage() as tracker:
        try:
            report = agent(symbols, dependencies)
        except Exception as e:
            logger.exception(e)
            workspace.vcs.commit(f"{type(e).__name__}\n\n{e}\n{traceback.format_exc()}")
            report = TranslationReport(complete=False, regressions=oracle.regressions)
    report.usage = tracker.totals

    # Wrapping is never exhaustive, so on its own a passing test suite only proves the hybrid
    # works, not that Rust is what made it work. Dropping every C body the wrappers did not
    # replace is the guarantee: whatever still links is Rust, and whatever needed C now fails.
    hybrid.strand_c()
    if not cfg.keep_hybrid and cfg.vcs == "git":
        workspace.remove_member(hybrid.crate)

    # Every per-symbol verdict was reached while the surviving C could still carry the suite,
    # so this is the only run that measures the Rust alone, in exactly what ships
    findings = workspace.audit(oracle)

    # Same tail a symbol group's closing log carries, since a run is just their sum
    summary = " ".join(filter(None, (model.format_usage(report.usage), oracle.counts())))
    shipped = f"{cfg.template} `{workspace.shipped_crate.name}`"
    if report.complete and not findings:
        msg = f"Translated {shipped} to Rust: {summary}"
        logger.info(msg)
    else:
        msg = f"Failed to translate {shipped} to Rust: {summary}"
        logger.error(msg)

    if findings:
        msg += f"\n\n{findings}"

    if report.regressions:
        regressed = [f"`{t}` in `{' '.join(g)}`" for t, g in sorted(report.regressions.items())]
        logger.warning("Test(s) regressed during this translation: " + ", ".join(regressed))
        msg += "\n\n# Regressed Tests\n" + "\n".join(f"- {r}" for r in regressed)

    # Commit translation
    if (output_subdir := HydraConfig.get().output_subdir) is not None:
        workspace.vcs.add(output_dir / output_subdir)
    workspace.vcs.commit(msg)
    workspace.clean()


@hydra.main(version_base=None, config_name="translate")
def main(cfg: TranslateConfig) -> None:
    try:
        _main(cast(TranslateConfig, OmegaConf.to_object(cfg)))
    except Exception as e:
        logger.exception(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
