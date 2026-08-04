#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
from typing import Any
from dataclasses import dataclass

import dspy
from omegaconf import MISSING
from litellm import cost_per_token

from hydra.core.config_store import ConfigStore

# Surface DSPy logging to Hydra and disable verbose to sys.stderr to avoid duplicates
dspy_logger = logging.getLogger("dspy")
dspy_logger.propagate = True
dspy.disable_logging()


@dataclass
class ModelConfig:
    name: str = MISSING
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
    timeout: int | None = 600


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
        timeout=generate.timeout,
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

    # NOTE: Covers exotic variants like gpt-5.5-pro
    if model.name.startswith(("openai/gpt-5.5", "openai/gpt-5.6")):
        lm.kwargs["temperature"] = 1.0
        # Choices: "none", "low", "medium" (default), "high", "xhigh", "max"
        lm.kwargs["reasoning_effort"] = "high"  # type: ignore[reportArgumentType]

    if model.name.startswith("hosted_vllm/zai-org/GLM"):
        # Choices: enable_thinking: False, "high", "max" (default)
        lm.kwargs["extra_body"] = {  # type: ignore[reportArgumentType]
            "chat_template_kwargs": {"reasoning_effort": "high"},
        }

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

    # Compute costs using litellm
    if cost_usd is None and lm_usage:
        cost_usd = 0.0
        for model_name, per_lm in lm_usage.items():
            pt = per_lm.get("prompt_tokens") or per_lm.get("input_tokens") or 0
            ct = per_lm.get("completion_tokens") or per_lm.get("output_tokens") or 0
            try:
                pc, cc = cost_per_token(model_name, prompt_tokens=pt, completion_tokens=ct)
            except Exception:
                pc, cc = 0.0, 0.0
            cost_usd += pc + cc

    cost = f"${cost_usd:.4f}, " if cost_usd is not None else ""

    return f"{cost}{total_tokens:,} tok ({prompt_tokens:,} in / {completion_tokens:,} out)"
