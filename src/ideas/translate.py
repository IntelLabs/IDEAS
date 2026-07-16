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
    crate = Crate(cfg.cargo_toml, vcs=cfg.vcs)  # type: ignore[reportArgumentType]

    # Save C source since it will be modified by the agent
    orig_c_src = crate.c_src_path.read_bytes()

    # Make sure Rust source is in known state (i.e., empty)
    crate.rust_src_path.write_text("")

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
    snippet_translator = SnippetTranslator(crate, translator, cfg.translator_max_iters)
    symbol_wrapper = WrapperGenerator(crate, cfg.wrapper_max_iters)
    symbol_tester = None
    if not LARGE_PROJECT:
        symbol_tester = SymbolTester(crate, symbols=list(symbols.values()), tests=cfg.tests)
    agent = RecurrentTranslator(
        crate, snippet_translator, symbol_wrapper, symbol_tester, cfg.max_iters
    )

    # Run translation agent and write it to disk
    try:
        pred = agent(symbols, dependencies)
        crate.rust_src_path.write_text(str(pred.translation))
    except Exception as e:
        logger.exception(e)
        pred = dspy.Prediction(success=False)
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
    _cleanup(crate)

    # Commit translation
    if (output_subdir := HydraConfig.get().output_subdir) is not None:
        crate.vcs.add(output_dir / output_subdir)
    crate.vcs.add(crate.rust_src_path, crate.c_src_path)
    crate.vcs.commit(msg)


def _cleanup(crate: Crate) -> None:
    # Remove bindgen artifacts
    crate.vcs.rm(
        crate.rust_src_path.parent / "binding",
        crate.rust_src_path.parent / "binding.rs",
        force=True,
    )
    logger.info("Removed bindgen artifacts")

    # For binaries, delete wrappers
    if crate.is_bin:
        wrapper_dir = crate.rust_src_path.parent / "wrapper"
        wrapper_module = crate.rust_src_path.parent / "wrapper.rs"
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
