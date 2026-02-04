#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import dspy
from src.ideas.adapters import ChatAdapter, Code

CodeLanguage = Code["language"]


def test_chat_adapter_uses_custom_translate_field_type():
    class TestSignature(dspy.Signature):
        input_text: str = dspy.InputField()
        code_output: CodeLanguage = dspy.OutputField()

    adapter = ChatAdapter()
    result = adapter.format_field_structure(TestSignature)

    # The result should contain the custom format with "note: the value you produce must be Code"
    assert f"# note: the value you produce {CodeLanguage.short_description()}" in result
    assert "{code_output}" in result
