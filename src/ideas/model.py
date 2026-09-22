#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
from typing import Any
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass

import dspy
from omegaconf import MISSING
from litellm import cost_per_token
from dspy.dsp.utils.settings import settings
from dspy.utils.usage_tracker import track_usage

from hydra.core.config_store import ConfigStore

logger = logging.getLogger(__name__)

# Surface DSPy logging to Hydra and disable verbose to sys.stderr to avoid duplicates
dspy_logger = logging.getLogger("dspy")
dspy_logger.propagate = True
dspy.disable_logging()


@dataclass
class ModelConfig:
    name: str = MISSING
    reasoning_effort: str = MISSING
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


class CostTrackingLM(dspy.LM):
    _warned_missing_cost = False

    # DSPy records only token counts in the usage tracker and discards litellm's per-call
    # cost into `lm.history`. Re-injecting it lets costs be summed instead of recomputed
    # from aggregated tokens, which would misapply long-context (>200k) pricing tiers.
    def forward(self, prompt=None, messages=None, **kwargs):
        results = super().forward(prompt=prompt, messages=messages, **kwargs)

        tracker = dspy.settings.usage_tracker
        if tracker is None or getattr(results, "cache_hit", False):
            return results

        cost = getattr(results, "_hidden_params", {}).get("response_cost")
        if cost is not None:
            # Merges into the token entry recorded by super().forward()
            tracker.add_usage(self.model, {"cost": cost})
        else:
            # Counted so mixed priced/unpriced totals can be flagged as underestimates
            tracker.add_usage(self.model, {"cost_missing": 1})
            if not CostTrackingLM._warned_missing_cost:
                CostTrackingLM._warned_missing_cost = True
                logger.warning(
                    f"litellm reported no cost for `{self.model}`; reported spend is an underestimate"
                )

        return results


def get_lm(model: ModelConfig, generate: GenerateConfig) -> dspy.LM:
    lm = CostTrackingLM(
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

    if model.name.startswith("openai/"):
        lm.model_type = "responses"

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


@contextmanager
def track_lm_usage() -> Iterator[dict[str, dict[str, Any]]]:
    # A nested tracker hides the enclosing one from dspy, so this block's usage has to be
    # folded back into the parent by hand. The yielded dict is filled on exit.
    parent = settings.usage_tracker
    if parent is None:
        yield {}
        return

    total: dict[str, dict[str, Any]] = {}
    with track_usage() as tracker:
        try:
            yield total
        finally:
            total.update(tracker.get_total_tokens())
            for lm_name, usage_entry in total.items():
                parent.add_usage(lm_name, usage_entry)


def format_usage(pred: dspy.Prediction) -> str:
    return format_lm_usage(pred.get_lm_usage())


def format_lm_usage(lm_usage: dict[str, dict[str, Any]] | None) -> str:
    # lm_usage maps lm_name to that LM's totals — aggregate across all of them
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

    # Costs summed per LM call by CostTrackingLM; a genuine $0.00 must not fall back
    cost_usd = None
    for key in ("cost", "cost_usd", "total_cost"):
        if usage.get(key) is not None:
            cost_usd = usage[key]
            break
    summed = cost_usd is not None

    # Fallback for models litellm cannot price per call; approximate, tiers may be wrong
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

    cost = f"${cost_usd:.6f}, " if cost_usd is not None else ""

    # A summed cost silently undercounts when only some calls were priced
    unpriced = usage.get("cost_missing") or 0
    missing = f" ({unpriced:,} calls unpriced)" if summed and unpriced else ""

    return f"{cost}{total_tokens:,} tok ({prompt_tokens:,} in / {completion_tokens:,} out){missing}"


def render_prompt(
    module: dspy.Module, inputs: dict[str, Any], was_generated: bool = True
) -> dict[str, str]:
    # Re-rendering assumes `inputs` is verbatim what the predictor received. Other modules,
    # like ReAct and ProgramOfThought, reshape them or send several prompts per forward.
    if not isinstance(module, (dspy.Predict, dspy.ChainOfThought)):
        logger.warning(f"Unable to record prompt for {type(module).__name__} module")
        return {}

    # ChainOfThought extends the signature, so its inner Predict holds the original signature
    signature = module.predictors()[0].signature
    assert isinstance(signature, type)

    adapter = settings.adapter or dspy.ChatAdapter()
    files = {"_system.md": adapter.format_system_message(signature)}
    if not was_generated:
        files["_cached.md"] = (
            "THIS PROMPT WAS NOT SENT: A CACHED OR STATIC RESULT WAS USED INSTEAD.\n"
        )
    for name, input_field in signature.input_fields.items():
        # Only dspy.Code subclasses carry a `language`; anything else renders as .md
        match getattr(input_field.annotation, "language", None):
            case "c":
                ext = ".c"
            case "rust":
                ext = ".rs"
            case _:
                ext = ".md"
        files[f"{name}{ext}"] = str(inputs[name])

    # A NUL would make git treat the file as binary and suppress the diff
    return {name: text.replace("\x00", "\\0") for name, text in files.items()}


def predict(
    module: dspy.Module,
    inputs: dict[str, Any],
    output_field: str,
    replay: Any | None = None,
) -> dspy.Prediction:
    parent_usage_tracker = settings.usage_tracker
    if replay is not None:
        pred = dspy.Prediction(**{output_field: replay})
        if parent_usage_tracker is not None:
            pred.set_lm_usage({})
    elif parent_usage_tracker is None:
        pred = module(**inputs)
    else:
        # dspy installs a tracker only at the outermost module, so this call's own usage
        # has to be measured locally before being folded back into the enclosing total
        with track_usage() as local_usage_tracker:
            pred = module(**inputs)
        lm_usage = local_usage_tracker.get_total_tokens()
        pred.set_lm_usage(lm_usage)
        for lm_name, usage_entry in lm_usage.items():
            parent_usage_tracker.add_usage(lm_name, usage_entry)
    pred.was_generated = replay is None
    pred.prompt_files = render_prompt(module, inputs, pred.was_generated)
    return pred
