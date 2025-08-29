#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import dspy
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from hydra.core.config_store import ConfigStore
from clang.cindex import CompilationDatabase, TranslationUnit

from ideas import agents
from .agents import AlgorithmConfig


@dataclass
class ModelConfig:
    name: str = "Qwen/Qwen2.5-Coder-7B-Instruct"
    cache: bool = False
    revision: str | None = None
    base_url: str | None = None
    api_key: str | None = None


@dataclass
class GenerateConfig:
    max_new_tokens: int = 10000
    do_sample: bool = False
    temperature: float = 1.0
    top_p: float = 1.0
    top_k: int | None = None


cs = ConfigStore.instance()
cs.store(name="model", node=ModelConfig)
cs.store(name="generate", node=GenerateConfig)


def translate_file(
    path: Path,
    model_cfg: ModelConfig,
    generate_cfg: GenerateConfig,
    algorithm_cfg: AlgorithmConfig,
) -> None:
    logger = logging.getLogger("ideas.translate")
    logger.info(f"Translating: {path} ...")

    lm = dspy.LM(
        model=model_cfg.name,
        cache=model_cfg.cache,
        api_key=model_cfg.api_key,
        api_base=model_cfg.base_url,
        temperature=generate_cfg.temperature,
        max_tokens=generate_cfg.max_new_tokens,
    )

    # Add OpenRouter-specific provider routing: https://openrouter.ai/docs/features/provider-routing
    if model_cfg.name.startswith("openrouter/"):
        provider: dict[str, Any] = {}

        # Deny data collection
        provider["data_collection"] = "deny"

        # Require fp8 and limit prices for qwen3-coder
        if model_cfg.name.lower().endswith("qwen/qwen3-coder"):
            provider["quantizations"] = ["fp8"]
            provider["max_price"] = {"prompt": 0.5, "completion": 2}

        lm.kwargs["provider"] = provider  # type: ignore[reportArgumentType]

    dspy.configure(lm=lm)

    if path.is_dir():
        # Create TranslationUnit from original .c filename in compile commands database
        db = CompilationDatabase.fromDirectory(path)
        cmds = db.getAllCompileCommands()
        if len(cmds) != 1:
            raise ValueError(
                f"Only 1 compile command is currently supported. Found {len(cmds)}!"
            )
        tu = TranslationUnit.from_source(None, args=list(cmds[0].arguments))

        # Construct intermediate .c.i filename
        orig_filename = Path(cmds[0].filename)
        filename = (path / "src" / orig_filename.name).with_suffix(".c.i")
    else:
        # Create TranslationUnit from intermediate .c.i filename
        tu = TranslationUnit.from_source(path)

        # Construct original .c filename
        orig_filename = path.parent / path.stem
        filename = path

    if not filename.exists():
        raise ValueError(f"Intermediate .c.i file {filename} does not exist.")
    if not orig_filename.exists():
        raise ValueError(f"Original .c file {orig_filename} does not exist.")

    # Check for symlinks
    if filename.is_symlink():
        raise ValueError(f"Input file {filename} is a symlink.")
    if orig_filename.is_symlink():
        raise ValueError(f"Original file {orig_filename} is a symlink.")

    agent: dspy.Module = agents.from_config(algorithm_cfg)
    translation: dict[str, str] = agent(orig_filename.read_text(), filename.read_text(), tu)

    logger.info(
        f"Translated {filename}",
        extra={"c": translation["c_code"], "rust": translation["rust_code"]},
    )

    src_dir = filename.parent
    prompt_path = (src_dir / orig_filename.name).with_suffix(".prompt")
    with prompt_path.open("w") as f:
        f.write(translation["c_code"])

    rs_translation_path = (src_dir / orig_filename.name).with_suffix(".rs")
    with rs_translation_path.open("w") as f:
        f.write(translation["rust_code"])
