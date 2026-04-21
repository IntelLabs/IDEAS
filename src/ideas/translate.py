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
from .tools import Crate, HYBRID_BUILD

logger = logging.getLogger("ideas.translate")


@dataclass
class TranslateConfig:
    filename: Path = MISSING
    model: ModelConfig = field(default_factory=ModelConfig)
    generate: GenerateConfig = field(default_factory=GenerateConfig)

    cargo_toml: Path = MISSING

    translator: str = "ChainOfThought"
    translator_max_iters: int = 5
    wrapper_max_iters: int = 5
    max_iters: int = 5
    readonly_cache: Path | None = None

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

    # Get global symbol table
    tu = create_translation_unit(cfg.filename)
    asts = [extract_info_c(tu)]
    symbols, dependencies = get_symbols_and_dependencies(
        asts, source_priority=[], external_symbol_names=["c:@F@main"] if crate.is_bin else None
    )
    global_functions = [
        s for s in symbols.values() if s.is_global and (s.is_function and s.is_definition)
    ]
    if not global_functions:
        logger.info("No global functions to translate!")
        return

    # Create translation agent
    model.configure(cfg.model, cfg.generate)
    dspy.configure(adapter=adapters.ChatAdapter())
    translator = getattr(dspy, cfg.translator)
    snippet_translator = SnippetTranslator(
        translator, crate, cfg.translator_max_iters, readonly_cache=cfg.readonly_cache
    )
    symbol_wrapper = WrapperGenerator(
        crate, cfg.wrapper_max_iters, readonly_cache=cfg.readonly_cache
    )
    symbol_tester = None
    if HYBRID_BUILD:
        symbol_tester = SymbolTester(crate, symbols=global_functions)
    agent = RecurrentTranslator(
        crate, snippet_translator, symbol_wrapper, symbol_tester, cfg.max_iters
    )

    # Run translation agent and write it to disk
    pred = agent(symbols, dependencies)
    crate.rust_src_path.write_text(pred.translation)
    if pred.success:
        # FIXME: Only keep wrappers for symbols we need to export

        msg = f"Translated `{crate.root_package['name']}` to Rust!"
        logger.info(msg)
    else:
        # Restore original C code so next agent can use it
        crate.c_src_path.write_bytes(orig_c_src)

        msg = f"Failed to translate `{crate.root_package['name']}` to Rust!"
        logger.error(msg)

    # Commit translation
    if (output_subdir := HydraConfig.get().output_subdir) is not None:
        crate.vcs.add(output_dir / output_subdir)
    crate.vcs.add(crate.rust_src_path, crate.c_src_path)
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
