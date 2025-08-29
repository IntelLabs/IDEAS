#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#


import pytest
from pathlib import Path
from clang.cindex import CursorKind

from ideas import ast, TreeResult


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "ast"


@pytest.fixture
def c_code(fixtures_dir: Path) -> str:
    return (fixtures_dir / "signatures.c").read_text()


@pytest.fixture
def c_fn_calling_code(fixtures_dir: Path) -> str:
    return (fixtures_dir / "fn_calling" / "src" / "main.c.i").read_text()


@pytest.fixture
def c_data_structures_code(fixtures_dir: Path) -> str:
    return (fixtures_dir / "data_structures" / "src" / "main.c.i").read_text()


def get_all_decl_by_kind(tr: TreeResult, kind: CursorKind) -> dict[str, str]:
    return {name: symbol.decl for name, symbol in tr.symbols.items() if symbol.kind == kind}


def get_all_symbols_by_kind(tr: TreeResult, kind: CursorKind) -> set[str]:
    return {symbol.name for _, symbol in tr.symbols.items() if symbol.kind == kind}


def get_all_symbols_for_name_by_kind(tr: TreeResult, name: str, kind: CursorKind) -> set[str]:
    return {symbol.name for symbol in tr.complete_graph[name] if symbol.kind == kind}


def test_declarations_and_definitions(c_code: str):
    expected_names = [
        "add_without_definition",
        "print_message",
        "add",
        "helper",
    ]

    expected_declarations = [
        "int add_without_definition(int a, int b)",
        "void print_message(const char* msg)",
        "int add(int a, int b)",
        "static int helper(int x)",
    ]

    expected_definitions = {
        "add_without_definition": None,
        "add": r"""
int add(int a, int b) {
    // This is a helpful comment that should not be stripped!
    return a + b;
}""".strip(),
        "print_message": r"""
void print_message(const char* msg) {
    printf("%s\\n", msg);
}""".strip(),
        "helper": r"""
static int helper(int x) {
    return x * 2;
}""".strip(),
    }

    tu = ast.create_translation_unit(c_code)
    result = ast.extract_info_c(tu)
    functions = get_all_decl_by_kind(result, CursorKind.FUNCTION_DECL)  # type: ignore[reportAttributeAccessIssue]
    names, declarations, definitions = (
        functions.keys(),
        functions.values(),
        result.fn_definitions,
    )

    # NOTE: The expected order is the order in which the declarations first appear
    for name, exp in zip(names, expected_names):
        assert name == exp

    for decl, exp in zip(declarations, expected_declarations):
        assert decl == exp

    for exp in expected_definitions.items():
        assert definitions[exp[0]] == exp[1]


def test_function_calls(c_fn_calling_code: str):
    tu = ast.create_translation_unit(c_fn_calling_code)
    result = ast.extract_info_c(tu)

    functions = get_all_decl_by_kind(result, CursorKind.FUNCTION_DECL)  # type: ignore[reportAttributeAccessIssue]
    fn_names = functions.keys()
    # Declared or imported function names should be extracted correctly
    assert {
        "printf",
        "fabs",
        "pow",
        "log10",
        "add",
        "subtract",
        "double_value",
        "triple_value",
        "quadruple_value",
        "double_absolute_value",
    } < fn_names

    declarations = set(functions.values())
    # Functions in libraries should be declared
    assert {
        "int add(int a, int b)",
        "int subtract(int a, int b)",
        "int double_value(int x)",
        "int triple_value(int x)",
        "int quadruple_value(int x)",
        "double double_absolute_value(double x)",
    } < declarations

    # Check that definitions of library functions are correctly pre-processed
    fn_definitions = result.fn_definitions
    assert (
        fn_definitions["add"]
        == r"""
int add(int a, int b) {
    return a + b;
}""".strip()
    )

    assert (
        fn_definitions["subtract"]
        == r"""
int subtract(int a, int b) {
    return add(a, -b);
}""".strip()
    )

    # All function calls should be detected
    assert get_all_symbols_for_name_by_kind(
        result,
        "double_value",
        CursorKind.CALL_EXPR,  # type: ignore[reportAttributeAccessIssue]
    ) == {"add", "printf"}

    assert get_all_symbols_for_name_by_kind(
        result,
        "triple_value",
        CursorKind.CALL_EXPR,  # type: ignore[reportAttributeAccessIssue]
    ) == {"add", "subtract"}

    assert (
        get_all_symbols_for_name_by_kind(result, "quadruple_value", CursorKind.CALL_EXPR)  # type: ignore[reportAttributeAccessIssue]
        == set()
    )

    assert get_all_symbols_for_name_by_kind(
        result,
        "double_absolute_value",
        CursorKind.CALL_EXPR,  # type: ignore[reportAttributeAccessIssue]
    ) == {"add", "fabs", "pow", "log10", "printf"}


def test_typedefs(c_data_structures_code: str):
    tu = ast.create_translation_unit(c_data_structures_code)
    result = ast.extract_info_c(tu)

    typedefs = get_all_decl_by_kind(result, CursorKind.TYPEDEF_DECL)  # type: ignore[reportAttributeAccessIssue]
    assert {"radius_t"} < typedefs.keys()
    assert typedefs["radius_t"] == "typedef double radius_t"


def test_variables(c_data_structures_code: str):
    tu = ast.create_translation_unit(c_data_structures_code)
    result = ast.extract_info_c(tu)

    variables = get_all_decl_by_kind(result, CursorKind.VAR_DECL)  # type: ignore[reportAttributeAccessIssue]
    assert {
        "p1",
        "num_dimensions",
        "anonymous_struct",
        "half_pi",
        "one_third_pi",
        "quarter_pi",
        "PI",
        "e_powers",
        "circle_color",
    } < variables.keys()

    assert variables["p1"] == "struct Point p1"
    assert variables["num_dimensions"] == "int num_dimensions"
    assert variables["anonymous_struct"] == "struct { int a; int b; } anonymous_struct"
    assert variables["half_pi"] == "const double half_pi"
    assert variables["one_third_pi"] == "static double one_third_pi"
    assert variables["quarter_pi"] == "static const double quarter_pi"
    assert variables["PI"] == "extern const double PI"
    assert variables["e_powers"] == "extern float e_powers[4]"
    assert variables["circle_color"] == "enum Color circle_color"


def test_structs(c_data_structures_code: str):
    tu = ast.create_translation_unit(c_data_structures_code)
    result = ast.extract_info_c(tu)

    structs = get_all_decl_by_kind(result, CursorKind.STRUCT_DECL)  # type: ignore[reportAttributeAccessIssue]
    assert {"struct Point"} < structs.keys()
    assert (
        structs["struct Point"]
        == r"""
struct Point {
    int x;
    int y;
}""".strip()
    )


def test_enums(c_data_structures_code: str):
    tu = ast.create_translation_unit(c_data_structures_code)
    result = ast.extract_info_c(tu)

    enums = get_all_decl_by_kind(result, CursorKind.ENUM_DECL)  # type: ignore[reportAttributeAccessIssue]
    assert enums.keys() == {"enum Color"}
    assert (
        enums["enum Color"]
        == r"""
enum Color {
    red = 1,
    green = 10,
    undefined = -1,
}""".strip()
    )


def test_get_code_from_tu_range():
    # NOTE: Add unicode character “
    code = '//“\n\n#include <stdio.h>\n\nint main() {\n    printf("Hello World!\\n");\n    return 0;\n}\n'
    tu = ast.create_translation_unit(code)
    tr = ast.extract_info_c(tu)
    code2 = list(tr.fn_definitions.values())[0]
    assert code2 == 'int main() {\n    printf("Hello World!\\n");\n    return 0;\n}'
