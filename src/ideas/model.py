#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from typing import Any
from dataclasses import dataclass

import dspy
from hydra.core.config_store import ConfigStore


@dataclass
class ModelConfig:
    name: str = "Qwen/Qwen2.5-Coder-7B-Instruct"
    cache: bool = False
    revision: str | None = None
    base_url: str | None = None
    api_key: str | None = None


@dataclass
class GenerateConfig:
    max_new_tokens: int = 32000
    do_sample: bool = False
    temperature: float = 1.0
    top_p: float = 1.0
    top_k: int | None = None


cs = ConfigStore.instance()
cs.store(name="model", node=ModelConfig)
cs.store(name="generate", node=GenerateConfig)


def get_lm(model: ModelConfig, generate: GenerateConfig) -> dspy.LM:
    lm = dspy.LM(
        model=model.name,
        cache=model.cache,
        api_key=model.api_key,
        api_base=model.base_url,
        temperature=generate.temperature,
        max_tokens=generate.max_new_tokens,
    )

    # Add OpenRouter-specific provider routing: https://openrouter.ai/docs/features/provider-routing
    if model.name.startswith("openrouter/"):
        provider: dict[str, Any] = {}

        # Deny data collection
        provider["data_collection"] = "deny"

        # Require fp8 and limit prices for qwen3-coder
        if model.name.lower().endswith("qwen/qwen3-coder"):
            provider["quantizations"] = ["fp8"]
            provider["max_price"] = {"prompt": 0.5, "completion": 2}

        lm.kwargs["provider"] = provider  # type: ignore[reportArgumentType]

    return lm


def configure(model: ModelConfig, generate: GenerateConfig):
    lm = get_lm(model, generate)
    dspy.configure(lm=lm)
