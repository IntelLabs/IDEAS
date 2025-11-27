#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import pytest
from pathlib import Path

from ideas import ast, ltu, tools


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "ast"


@pytest.fixture
def c_data_structures_code(fixtures_dir: Path) -> str:
    return (fixtures_dir / "data_structures" / "src" / "main.c.i").read_text()


@pytest.fixture
def c_data_structures_lib(fixtures_dir: Path) -> tuple[str, str]:
    return (
        (fixtures_dir / "data_structures" / "include" / "lib.h").read_text(),
        (fixtures_dir / "data_structures" / "include" / "lib.c").read_text(),
    )


def test_mtu_builder(c_data_structures_code: str, c_data_structures_lib: tuple[str, str]):
    # Build the maximal translation units for each function
    macro_tu = ast.create_translation_unit(c_data_structures_code)
    ast_info = ast.extract_info_c(macro_tu)
    with pytest.raises(ValueError) as error:
        _ = ltu.build_unit(ast_info, type="functional_maximal")
        assert str(error.value) == "ltu-max not implemented!"


def test_mtu_on_typedef():
    nested_code = r"""
typedef struct Point {
    int x;
    int y;
} Point;

typedef union Circleish {
    float r;
    Point center;
} Circle;

struct Unrelated {
    int a;
};
typedef int totally_unrelated;

struct Later;
typedef struct Later {
    int a;
} Later;

int main() {
    struct Point p1;
    Point p2;

    union Circleish c;
    Circle c_prime;

    struct Unrelated u;

    totally_unrelated tu = 42;

    Later L;

    return 0;
}
""".strip()

    # Check that the full code compiles
    full_success, full_out = tools.check_c(
        nested_code,
        flags=["-c"],
    )
    assert full_success
    assert full_out == ""

    # Build the maximal translation units for each function
    macro_tu = ast.create_translation_unit(nested_code)
    ast_info = ast.extract_info_c(macro_tu)

    with pytest.raises(ValueError) as error:
        _ = ltu.build_unit(ast_info, type="functional_maximal")
        assert str(error.value) == "ltu-max not implemented!"


def test_mtu_delayed_dependency():
    delayed_code = r"""
typedef int custom_int;

struct Delayed;
typedef struct Delayed Alias;

typedef double custom_double;
struct DoubleNested {
    custom_double value;
};

struct Nested {
    custom_int value;
    struct DoubleNested nested;
};

struct Delayed {
    struct Nested stuff;
};

int main() {
    Alias alias;

    return 0;
}
""".strip()

    # Check that the full code compiles
    full_success, full_out = tools.check_c(
        delayed_code,
        flags=["-c"],
    )
    assert full_success
    assert full_out == ""

    # Build the maximal translation units for each function
    macro_tu = ast.create_translation_unit(delayed_code)
    ast_info = ast.extract_info_c(macro_tu)

    with pytest.raises(ValueError) as error:
        _ = ltu.build_unit(ast_info, type="functional_maximal")
        assert str(error.value) == "ltu-max not implemented!"


def test_mtu_deep_nested_enum():
    deep_enum_code = r"""
typedef union {
    enum {
        A, B, C
    } a;
    int b;
} c;

int main(int argc, char **argv) {
    // Don't leak name through local
    int another = C;

    return 0;
}
""".strip()

    # Check that the full code compiles
    full_success, full_out = tools.check_c(
        deep_enum_code,
        flags=["-c"],
    )
    assert full_success
    assert full_out == ""

    # Build the maximal translation units for each function
    macro_tu = ast.create_translation_unit(deep_enum_code)
    ast_info = ast.extract_info_c(macro_tu)

    with pytest.raises(ValueError) as error:
        _ = ltu.build_unit(ast_info, type="functional_maximal")
        assert str(error.value) == "ltu-max not implemented!"
