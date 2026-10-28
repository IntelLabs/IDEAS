#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import re
from typing import ClassVar, Self
from dataclasses import dataclass
from collections.abc import Iterable

# A model asked for code answers with it fenced as often as not, with or without a language
_FENCED = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_FENCED_INLINE = re.compile(r"```(.*?)```", re.DOTALL)


def _unfence(code: str) -> str:
    for pattern in (_FENCED, _FENCED_INLINE):
        if match := pattern.search(code):
            return match.group(1).strip()
    return code


# `eq=False` keeps the comparisons below, which hold between any two Code of one language
@dataclass(frozen=True, eq=False)
class Code:
    code: str = ""

    language: ClassVar[str] = ""

    def __post_init__(self):
        stripped = _unfence(self.code).rstrip()
        object.__setattr__(self, "code", stripped + "\n" if stripped else "")

    def __str__(self):
        return self.code

    def __bool__(self):
        return bool(self.code)

    def __add__(self, other: Self) -> Self:
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
    def join(cls, parts: Iterable[Self]) -> Self:
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
