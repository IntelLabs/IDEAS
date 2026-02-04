#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from unittest.mock import patch

from pydantic.fields import FieldInfo

import dspy
import dspy.adapters.chat_adapter
from dspy.adapters.chat_adapter import ChatAdapter as _ChatAdapter
from dspy.adapters.utils import translate_field_type as _translate_field_type
from dspy.signatures.utils import get_dspy_field_type


class Code(dspy.Code):
    def format(self):
        return f"```{self.language.lower()}\n{self.code.rstrip()}\n```"

    @classmethod
    def short_description(cls):
        return f"must be {cls.__name__}"


class ChatAdapter(_ChatAdapter):
    def format_field_structure(self, signature: type[dspy.Signature]) -> str:
        with patch.object(
            dspy.adapters.chat_adapter, "translate_field_type", translate_field_type
        ):
            return super().format_field_structure(signature)


def translate_field_type(field_name: str, field_info: FieldInfo) -> str:
    # If a non-input field has a short_description, then use that.
    field_type = field_info.annotation
    if not field_type:
        raise RuntimeError(f"Field '{field_name}' is missing a type annotation")

    if hasattr(field_type, "short_description") and get_dspy_field_type(field_info) != "input":
        desc = field_type.short_description()
        desc = (" " * 8) + f"# note: the value you produce {desc}" if desc else ""
        return f"{{{field_name}}}{desc}"
    return _translate_field_type(field_name, field_info)
