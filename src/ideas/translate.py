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
from ideas import SnippetTranslator, RecurrentTranslator, WrapperGenerator, SymbolTester
from ideas import create_translation_unit, extract_info_c
from ideas.ast_rust import mangle
from ideas.init.consolidate import get_symbols_and_dependencies
from .tools import Crate, LARGE_PROJECT

logger = logging.getLogger("ideas.translate")


@dataclass
class TranslateConfig:
    filename: Path = MISSING
    model: ModelConfig = field(default_factory=ModelConfig)
    generate: GenerateConfig = field(default_factory=GenerateConfig)

    cargo_toml: Path = MISSING
    tests: str = MISSING

    translator: str = "ChainOfThought"
    translator_max_iters: int = 5
    wrapper_max_iters: int = 5
    max_iters: int = 3

    vcs: str = "none"


cs = ConfigStore.instance()
cs.store(name="translate", node=TranslateConfig)


def _main(cfg: TranslateConfig) -> None:
    output_dir = Path(HydraConfig.get().runtime.output_dir)
    logger.info(f"Saving results to {output_dir}")
    crate = Crate(cargo_toml=cfg.cargo_toml.resolve(), vcs=cfg.vcs)  # type: ignore[reportArgumentType]

    # Save C source since it will be modified by the agent
    orig_c_src = crate.c_src_path.read_bytes()

    # Make sure Rust source is in known state (i.e., empty)
    crate.rust_src_path.write_text("")
    if LARGE_PROJECT and (crate.cargo_toml.parent / "build.rs").exists():
        (crate.cargo_toml.parent / "build.rs").unlink()
        crate.vcs.rm(crate.cargo_toml.parent / "build.rs", force=True)

    # Get global symbol table
    tu = create_translation_unit(cfg.filename)
    asts = [extract_info_c(tu)]
    symbols, dependencies = get_symbols_and_dependencies(
        asts, external_symbol_names=["c:@F@main"] if crate.is_bin else None
    )

    # Create translation agent
    model.configure(cfg.model, cfg.generate)
    dspy.configure(adapter=adapters.ChatAdapter())
    translator = getattr(dspy, cfg.translator)
    snippet_translator = SnippetTranslator(translator, crate, cfg.translator_max_iters)
    symbol_wrapper, symbol_tester = None, None
    if not LARGE_PROJECT:
        symbol_wrapper = WrapperGenerator(crate, cfg.wrapper_max_iters)
        symbol_tester = SymbolTester(crate, symbols=list(symbols.values()), tests=cfg.tests)
    agent = RecurrentTranslator(
        crate, snippet_translator, symbol_wrapper, symbol_tester, cfg.max_iters
    )

    # Run translation agent and write it to disk
    pred = agent(symbols, dependencies)
    crate.rust_src_path.write_text(pred.translation.text)
    usage = model.format_usage(pred)
    if pred.success:
        msg = f"Translated `{crate.root_package['name']}` to Rust: {usage}"
        logger.info(msg)
    else:
        # Restore original C code so next agent can use it
        crate.c_src_path.write_bytes(orig_c_src)

        msg = f"Failed to translate `{crate.root_package['name']}`: {usage}"
        logger.error(msg)

    # Clean up intermediate artifacts produced during translation
    _cleanup(crate, symbols)

    # Commit translation
    if (output_subdir := HydraConfig.get().output_subdir) is not None:
        crate.vcs.add(output_dir / output_subdir)
    crate.vcs.add(crate.rust_src_path, crate.c_src_path)
    crate.vcs.commit(msg)


def _cleanup(crate: Crate, symbols: dict) -> None:
    # Remove bindgen artifacts
    crate.vcs.rm(
        crate.rust_src_path.parent / "binding",
        crate.rust_src_path.parent / "binding.rs",
        force=True,
    )
    logger.info("Removed bindgen artifacts")

    # Remove wrappers for symbols that are not globally linked
    keepers = {
        mangle(s.spelling)
        for s in symbols.values()
        if s.is_global
        and not crate.is_bin
        and (s.is_variable or (s.is_function and s.is_definition))
    }
    wrapper_dir = crate.rust_src_path.parent / "wrapper"
    wrapper_module = crate.rust_src_path.parent / "wrapper.rs"

    lines = wrapper_module.read_text().splitlines() if wrapper_module.exists() else []
    if wrapper_dir.exists():
        for wrapper_file in wrapper_dir.glob("*.rs"):
            if wrapper_file.stem not in keepers:
                crate.vcs.rm(wrapper_file, force=True)
                logger.info(f"Removed non-global wrapper: {wrapper_file.name}")
                mod_line = f"pub mod {wrapper_file.stem};"
                if mod_line in lines:
                    lines.remove(mod_line)
    if lines:
        wrapper_module.write_text("\n".join(lines) + "\n")
        crate.vcs.add(wrapper_module)
    else:
        crate.vcs.rm(wrapper_module, wrapper_dir, force=True)


@hydra.main(version_base=None, config_name="translate")
def main(cfg: TranslateConfig) -> None:
    try:
        _main(cfg)
    except Exception as e:
        logger.exception(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
