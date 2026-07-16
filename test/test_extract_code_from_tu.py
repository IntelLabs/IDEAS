#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#


import pickle
import pytest
from pathlib import Path
from textwrap import dedent as d

from ideas import ast


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "ast"


@pytest.fixture
def i_code(fixtures_dir: Path) -> str:
    return (fixtures_dir / "formatting.c.i").read_text()


def parse_c(code: str):
    return ast.create_translation_unit(ast.CodeC(code=code))


def test_all_code_from_tu(i_code: str):
    # Parse the code using clang
    tu = parse_c(i_code)
    result = ast.extract_info_c(tu)

    assert isinstance(result.symbols["c:@F@foo"].code, ast.CodeC)

    # Check for exact formatting
    assert (
        result.symbols["c:@F@foo"].code.code
        == d(
            """
            void foo() {
                int x = 10;
                int y = 20;
                int z = 20;
                if (z > 15) {
                    z += 5;
                } else {
                    z -= 5;
                }
            }
            """
        ).strip()
        + "\n"
    )


def test_newline():
    code = "int main(int argc, char **argv) { return 0;\r\n}"
    tu = parse_c(code)
    result = ast.extract_info_c(tu)

    assert (
        result.symbols["c:@F@main"].code.code
        == d(
            """
            int main(int argc, char **argv) {
                return 0;
            }
            """
        ).strip()
        + "\n"
    )


def test_symbol_is_picklable():
    tu = parse_c("int main(int argc, char **argv) { return 0; }")
    symbol = ast.extract_info_c(tu).symbols["c:@F@main"]

    blob = pickle.dumps(symbol)
    restored = pickle.loads(blob)

    assert restored.name == symbol.name
    assert restored.spelling == symbol.spelling
    assert restored.kind == symbol.kind
    assert restored.tu_preorder_index == symbol.tu_preorder_index
    assert restored.code == symbol.code


def test_tree_result_is_picklable():
    tu = parse_c("int main(int argc, char **argv) { return 0; }")
    result = ast.extract_info_c(tu)

    blob = pickle.dumps(result)
    restored = pickle.loads(blob)

    assert isinstance(restored, ast.TreeResult)
    assert restored == result
