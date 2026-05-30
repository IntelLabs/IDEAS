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
    text_output: bool = True
    revision: str | None = None
    base_url: str | None = None
    api_key: str | None = None


@dataclass
class GenerateConfig:
    max_new_tokens: int = 128000
    temperature: float = 0.0
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
        if "openai" in model.name:
            provider["order"] = ["openai", "azure"]
        if "anthropic" in model.name:
            provider["order"] = ["anthropic", "anthropic/2", "google-vertex/us-east5", "azure"]

        lm.kwargs["provider"] = provider  # type: ignore[reportArgumentType]

        # Mask and/or disable reasoning if desired and possible
        if model.text_output:
            lm.kwargs["reasoning"] = {"exclude": True}  # type: ignore[reportArgumentType]

    return lm


def configure(model: ModelConfig, generate: GenerateConfig):
    lm = get_lm(model, generate)
    dspy.configure(lm=lm, track_usage=True)


def format_usage(pred: dspy.Prediction) -> str:
    # get_lm_usage() returns dict[lm_name, dict[str, Any]] — aggregate across all LMs
    lm_usage = pred.get_lm_usage()
    if lm_usage is None:
        return "unknown usage"

    usage: dict[str, Any] = {}
    for per_lm in lm_usage.values():
        for key, value in per_lm.items():
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value

    prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    total_tokens = usage.get("total_tokens") or (prompt_tokens + completion_tokens)
    cost_usd = usage.get("cost") or usage.get("cost_usd") or usage.get("total_cost")

    cost = f"${cost_usd:.4f}, " if cost_usd is not None else ""

    return f"{cost}{total_tokens:,} tok ({prompt_tokens:,} in / {completion_tokens:,} out)"
