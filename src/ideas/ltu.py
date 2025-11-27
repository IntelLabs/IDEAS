#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from dataclasses import dataclass, field
from ideas.ast import TreeResult


@dataclass
class LLMTranslationUnit:
    symbol_name: str
    symbol_definition: str

    ref_symbols: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        if not self.symbol_name:
            raise ValueError("Symbol name cannot be empty!")

        code = f"// {self.symbol_name}\n\n"
        for ref_symbol in self.ref_symbols:
            code += f"{ref_symbol};\n\n"

        code += f"{self.symbol_definition};\n"
        return code.strip()


def build_unit(
    ast_info: TreeResult, type: str = "functional_maximal"
) -> list[LLMTranslationUnit]:
    if type == "functional_maximal":
        raise ValueError("ltu-max not implemented!")
    elif type == "functional_minimal":
        return build_functional_minimal_unit(ast_info)
    else:
        raise ValueError(f"Unknown unit type: {type}")


def build_functional_minimal_unit(ast_info: TreeResult) -> list[LLMTranslationUnit]:
    definitions = [
        (name, definition) for name, definition in ast_info.fn_definitions.items() if definition
    ]
    units = []
    for name, definition in definitions:
        unit = LLMTranslationUnit(
            symbol_name=name,
            symbol_definition=definition,
        )
        units.append(unit)
    return units
