#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from typing import Any
from collections.abc import Iterable

import dspy
import json_repair
import dspy.adapters.chat_adapter

from unittest.mock import patch
from pydantic import ConfigDict, field_validator
from pydantic.fields import FieldInfo
from json_repair import loads as _json_repair_loads
from dspy.adapters.chat_adapter import ChatAdapter as _ChatAdapter
from dspy.adapters.utils import translate_field_type as _translate_field_type
from dspy.signatures.utils import get_dspy_field_type
from dspy.signatures.signature import Signature


class Code(dspy.Code):
    model_config = ConfigDict(frozen=True)

    @field_validator("code", mode="before")
    @classmethod
    def _normalize_code(cls, v: str) -> str:
        stripped = v.rstrip()
        return stripped + "\n" if stripped else ""

    def __init__(self, code: str = "", **kwargs):
        kwargs["code"] = code
        super().__init__(**kwargs)

    @property
    def text(self) -> str:
        return self.code

    def __add__(self, other):
        if not isinstance(other, Code):
            return NotImplemented
        if self.language != other.language:
            raise TypeError(f"Cannot add {other.language} code to {self.language} code")
        if not self.code:
            return other
        if not other.code:
            return self
        return type(self)(self.code + "\n" + other.code)

    @classmethod
    def join(cls, parts: Iterable["Code"]) -> "Code":
        result = cls()
        seen = set()
        for part in parts:
            if part not in seen:
                seen.add(part)
                result = result + part
        return result

    def __contains__(self, other):
        if not isinstance(other, Code):
            raise TypeError(
                f"Cannot check membership of {type(other).__name__} in {type(self).__name__}"
            )
        if self.language != other.language:
            raise TypeError(f"Cannot check {other.language} code in {self.language} code")
        return other.code in self.code

    def __eq__(self, other):
        if not isinstance(other, Code):
            return NotImplemented
        return self.language == other.language and self.code == other.code

    def __hash__(self):
        return hash((self.language, self.code))

    def format(self):
        return f"```{self.language.lower()}\n{self.code}```"

    @classmethod
    def short_description(cls):
        return f"must be {cls.__name__}"


class ChatAdapter(_ChatAdapter):
    def format_field_structure(self, signature: type[dspy.Signature]) -> str:
        with patch.object(
            dspy.adapters.chat_adapter, "translate_field_type", translate_field_type
        ):
            return super().format_field_structure(signature)

    # Disable json_repair.loads for dspy.Code-like outputs when parsing completions.
    # The following snippet is treated as "repaired json", which is clearly wrong:
    #   ```rust
    #   pub struct Program<'a> {
    #       pub code: &'a [i32],
    #       pub n: usize,
    #       pub ip: usize,
    #   }
    #   ```
    # That snippet is repaired as:
    #   a [i32]
    # Unfortunately, dspy always runs json_repair.loads on every field and there is no
    # option in dspy to disable it.
    def parse(self, signature: type[Signature], completion: str) -> dict[str, Any]:
        with patch.object(json_repair, "loads", json_repair_loads):
            return super().parse(signature, completion)


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


def json_repair_loads(json_str: str, *args, **kwargs):
    # If json_str starts with a fence (```), then immediately fail repair.
    if isinstance(json_str, str) and json_str.startswith("```"):
        return ""
    return _json_repair_loads(json_str, *args, **kwargs)
