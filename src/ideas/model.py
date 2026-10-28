#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import json
import logging
from typing import Any
from pathlib import Path
from functools import cache
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from collections.abc import Iterator

from omegaconf import MISSING

from hydra.core.config_store import ConfigStore

from .ast_rust import CodeRust

logger = logging.getLogger(__name__)

# What an LM reports per call, keyed by model name, summed across calls by UsageTracker
type LMUsage = dict[str, dict[str, Any]]

PRICES_PATH = Path(__file__).with_name("model_prices.json")


@cache
def _model_prices() -> tuple[dict[str, dict[str, Any]], dict[str, str], frozenset[str]]:
    models = json.loads(PRICES_PATH.read_bytes())
    lowercase = {key.lower(): key for key in models}
    providers = frozenset(info.get("litellm_provider") for info in models.values())
    return models, lowercase, providers


def model_info(model: str) -> dict[str, Any] | None:
    models, lowercase, providers = _model_prices()

    vendor, _, bare = model.partition("/")
    provider, split = (vendor, bare) if bare and vendor in providers else (None, model)

    for candidate in (model, split):
        key = candidate if candidate in models else lowercase.get(candidate.lower())
        if key is None:
            continue
        info = models[key]
        entry_provider = info.get("litellm_provider")
        # litellm reroutes AI21 and Cohere to `_chat` providers; leave them unpriced
        if entry_provider in ("ai21", "cohere", "cohere_chat"):
            return None
        if provider is None or entry_provider in (None, provider):
            return info
    return None


@dataclass
class ModelConfig:
    name: str = MISSING
    reasoning_effort: str = MISSING
    provider: str = MISSING
    base_url: str = MISSING
    env_key: str = MISSING
    output_schema: bool | None = None

    # `name` stays OpenRouter-shaped so `model_info` can price it, but a vendor's own API
    # wants the bare id
    @property
    def api_name(self) -> str:
        if self.provider == "openai":
            return self.name.removeprefix("openai/")

        if self.provider == "azure":
            vendor, _ = self.name.split("/", 1)
            if vendor != "openai":
                raise ValueError(f"Unsupported vendor '{vendor}' for Azure deployment!")

            # Strip vendor prefix from Azure model name
            return self.name.removeprefix(f"{vendor}/")

        return self.name

    # Other models are likely to apply the schema on every step, which breaks Codex tool calls
    @property
    def enable_output_schema(self) -> bool:
        if self.output_schema is not None:
            return self.output_schema
        return self.provider == "openai" or self.name.startswith("openai/")


_PROVIDERS = (
    ModelConfig(
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        env_key="OPENROUTER_API_KEY",
    ),
    ModelConfig(
        provider="openai",
        base_url="https://api.openai.com/v1",
        env_key="OPENAI_API_KEY",
    ),
    ModelConfig(
        provider="vllm",
        base_url=MISSING,
        env_key="VLLM_API_KEY",
    ),
    ModelConfig(
        provider="azure",
        base_url=MISSING,
        env_key="AZURE_OPENAI_API_KEY",
    ),
)

cs = ConfigStore.instance()
cs.store(name="model", node=ModelConfig)
for _preset in _PROVIDERS:
    cs.store(group="model", name=_preset.provider, node=_preset)


# One turn of code generation, whether the model produced it or a cache hit replayed it
@dataclass(kw_only=True)
class Generation:
    # None when the turn came back without the code it was asked for
    code: CodeRust | None = None
    reasoning: str = ""
    usage: LMUsage = field(default_factory=dict)
    was_generated: bool = False
    prompt_files: dict[str, str] = field(default_factory=dict)


# A response a fresh attempt might still survive, as opposed to a failure of the run
class GenerationParseError(Exception):
    pass


# Every counter a turn reports is a flat token count or cost, so summing keys is enough
class UsageTracker:
    def __init__(self) -> None:
        self.totals: LMUsage = {}

    def add(self, model_name: str, usage: dict[str, Any]) -> None:
        per_lm = self.totals.setdefault(model_name, {})
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                per_lm[key] = per_lm.get(key, 0) + value


_tracker: ContextVar[UsageTracker | None] = ContextVar("ideas_usage_tracker", default=None)


# The tracker every generator in this context reports into, if anyone installed one
def current_tracker() -> UsageTracker | None:
    return _tracker.get()


@contextmanager
def track_usage() -> Iterator[UsageTracker]:
    tracker = UsageTracker()
    token = _tracker.set(tracker)
    try:
        yield tracker
    finally:
        _tracker.reset(token)


@contextmanager
def track_lm_usage() -> Iterator[LMUsage]:
    # A nested tracker shadows the enclosing one, so this block's usage has to be folded
    # back into the parent by hand. The yielded dict is filled on exit.
    parent = current_tracker()

    total: LMUsage = {}
    with track_usage() as tracker:
        try:
            yield total
        finally:
            total.update(tracker.totals)
            if parent is not None:
                for lm_name, usage_entry in total.items():
                    parent.add(lm_name, usage_entry)


def format_usage(lm_usage: LMUsage | None) -> str:
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

    # Costs summed per Codex turn in codex.py; a genuine $0.00 must not be dropped
    cost_usd = None
    for key in ("cost", "cost_usd", "total_cost"):
        if usage.get(key) is not None:
            cost_usd = usage[key]
            break

    cost = f"${cost_usd:.6f}, " if cost_usd is not None else ""

    # A summed cost silently undercounts when only some calls were priced
    unpriced = usage.get("cost_missing") or 0
    missing = f" ({unpriced:,} calls unpriced)" if unpriced else ""

    return f"{cost}{total_tokens:,} tok ({prompt_tokens:,} in / {completion_tokens:,} out){missing}"
