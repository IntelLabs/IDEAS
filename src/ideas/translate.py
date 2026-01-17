#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
from pathlib import Path
from dataclasses import dataclass, field

import dspy
import hydra
from omegaconf import MISSING
from clang.cindex import TranslationUnit
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig

from ideas import model, ModelConfig, GenerateConfig
from ideas import SymbolTranslator, RecurrentTranslator
from ideas import extract_info_c
from .init import get_symbols_and_dependencies
from .tools import Crate

logger = logging.getLogger("ideas.translate")


@dataclass
class TranslateConfig:
    filename: Path = MISSING
    model: ModelConfig = field(default_factory=ModelConfig)
    generate: GenerateConfig = field(default_factory=GenerateConfig)

    translator: str = "ChainOfThought"
    max_iters: int = 5

    vcs: str = "none"


cs = ConfigStore.instance()
cs.store(name="translate", node=TranslateConfig)


@hydra.main(version_base=None, config_name="translate")
def main(cfg: TranslateConfig) -> None:
    output_dir = Path(HydraConfig.get().runtime.output_dir)
    logger.info(f"Saving results to {output_dir}")
    crate = Crate(cargo_toml=output_dir / "Cargo.toml", vcs=cfg.vcs)  # type: ignore[reportArgumentType]

    model.configure(cfg.model, cfg.generate)
    translator = getattr(dspy, cfg.translator)
    symbol_translator = SymbolTranslator(translator, crate, cfg.max_iters)
    agent = RecurrentTranslator(symbol_translator)

    # Get global symbol table
    tu = TranslationUnit.from_source(cfg.filename)
    asts = [extract_info_c(tu)]
    symbols, dependencies = get_symbols_and_dependencies(asts)
    pred = agent(symbols, dependencies)
    translation: str = pred.translation
    translated: bool = pred.success

    # Write translation to disk
    crate.rust_src_path.parent.mkdir(exist_ok=True, parents=True)
    crate.rust_src_path.write_text(translation)

    # Commit translation
    crate.add(crate.rust_src_path)
    if (output_subdir := HydraConfig.get().output_subdir) is not None:
        crate.add(output_dir / output_subdir)
    name = f"`{crate.root_package['name']}`"
    crate.commit(
        f"Translated {name} to Rust" if translated else f"Failed to translate {name} to Rust"
    )


if __name__ == "__main__":
    main()
