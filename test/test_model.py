#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
import importlib
from unittest.mock import patch

import pytest
import dspy
import dspy.clients.lm as dspy_lm
from dspy.utils.logging_utils import configure_dspy_loggers
from litellm import ModelResponse
from litellm.types.utils import Choices, Message, Usage

import ideas.model


def _truncated_completion(request, num_retries, cache):
    return ModelResponse(
        model="openai/gpt-4o-mini",
        choices=[
            Choices(
                index=0,
                finish_reason="length",
                message=Message(role="assistant", content="partial output"),
            )
        ],
        usage=Usage(prompt_tokens=5, completion_tokens=10, total_tokens=15),
    )


@pytest.fixture
def translate_log(tmp_path):
    log_path = tmp_path / "translate.log"
    handler = logging.FileHandler(log_path)
    handler.setFormatter(logging.Formatter("[%(name)s][%(levelname)s] %(message)s"))
    root = logging.getLogger()
    prior_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield log_path
    finally:
        handler.close()
        root.removeHandler(handler)
        root.setLevel(prior_level)


def test_truncation_warning_written_to_translate_log(translate_log):
    configure_dspy_loggers("dspy")
    importlib.reload(ideas.model)

    # Mimic the app's own logging around a DSPy module call
    app_logger = logging.getLogger("ideas.translate_snippet")
    app_logger.info("Translating snippet `demo` ...")

    lm = dspy.LM(model="openai/gpt-4o-mini", max_tokens=10, temperature=0.0, cache=False)
    with patch.object(dspy_lm, "litellm_completion", _truncated_completion):
        lm("hello")

    app_logger.info("Translated snippet `demo`")

    warning = (
        "LM response was truncated due to exceeding max_tokens=10. "
        "You can inspect the latest LM interactions with `dspy.inspect_history()`. "
        "To avoid truncation, consider passing a larger max_tokens when setting up dspy.LM. "
        "You may also consider increasing the temperature (currently 0.0) "
        " if the reason for truncation is repetition."
    )
    expected = (
        "[ideas.translate_snippet][INFO] Translating snippet `demo` ...\n"
        f"[dspy.clients.lm][WARNING] {warning}\n"
        "[ideas.translate_snippet][INFO] Translated snippet `demo`\n"
    )
    assert translate_log.read_text() == expected
