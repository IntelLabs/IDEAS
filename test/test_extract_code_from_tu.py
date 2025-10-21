#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#


import pytest
from pathlib import Path

from ideas import ast


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "ast"


@pytest.fixture
def i_code(fixtures_dir: Path) -> str:
    return (fixtures_dir / "formatting.c.i").read_text()


def test_all_code_from_tu(i_code: str):
    # Parse the code using clang
    tu = ast.create_translation_unit(i_code)
    result = ast.extract_info_c(tu)

    # Check for exact formatting
    assert (
        result.fn_definitions["c:@F@foo"]
        == r"""void foo() {
      int x = 10; int y = 20;
    // A comment
    int z = 20;


    /* A comment
    block */
    if (z > 15) { z += 5;    } else {
        z -= 5;
    }
}""".strip()
    )


def test_newline():
    code = "int main(int argc, char **argv) { return 0;\r\n}"
    tu = ast.create_translation_unit(code)
    result = ast.extract_info_c(tu)

    assert (
        result.fn_definitions["c:@F@main"] == "int main(int argc, char **argv) { return 0;\r\n}"
    )
