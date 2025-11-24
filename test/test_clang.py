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
        "c:@F@add_without_definition",
        "c:@F@print_message",
        "c:@F@add",
        "c:file.c@F@helper",
    ]

    expected_declarations = [
        "int add_without_definition(int a, int b)",
        "void print_message(const char* msg)",
        "int add(int a, int b)",
        "static int helper(int x)",
    ]

    expected_definitions = [
        None,
        r"""
void print_message(const char* msg) {
    printf("%s\\n", msg);
}""".strip(),
        r"""
int add(int a, int b) {
    // This is a helpful comment that should not be stripped!
    return a + b;
}""".strip(),
        r"""
static int helper(int x) {
    return x * 2;
}""".strip(),
    ]

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

    for name, exp in zip(names, expected_definitions):
        assert definitions[name] == exp


def test_function_calls(c_fn_calling_code: str):
    tu = ast.create_translation_unit(c_fn_calling_code)
    result = ast.extract_info_c(tu)

    functions = get_all_decl_by_kind(result, CursorKind.FUNCTION_DECL)  # type: ignore[reportAttributeAccessIssue]
    fn_names = functions.keys()
    # Declared or imported function names should be extracted correctly
    assert {
        "c:@F@printf",
        "c:@F@fabs",
        "c:@F@pow",
        "c:@F@log10",
        "c:@F@add",
        "c:@F@subtract",
        "c:@F@double_value",
        "c:@F@triple_value",
        "c:@F@quadruple_value",
        "c:@F@double_absolute_value",
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
        fn_definitions["c:@F@add"]
        == r"""
int add(int a, int b) {
    return a + b;
}""".strip()
    )

    assert (
        fn_definitions["c:@F@subtract"]
        == r"""
int subtract(int a, int b) {
    return add(a, -b);
}""".strip()
    )

    # All function calls should be detected
    assert get_all_symbols_for_name_by_kind(
        result,
        "c:@F@double_value",
        CursorKind.CALL_EXPR,  # type: ignore[reportAttributeAccessIssue]
    ) == {"c:@F@add", "c:@F@printf"}

    assert get_all_symbols_for_name_by_kind(
        result,
        "c:@F@triple_value",
        CursorKind.CALL_EXPR,  # type: ignore[reportAttributeAccessIssue]
    ) == {"c:@F@add", "c:@F@subtract"}

    assert (
        get_all_symbols_for_name_by_kind(result, "c:@F@quadruple_value", CursorKind.CALL_EXPR)  # type: ignore[reportAttributeAccessIssue]
        == set()
    )

    assert get_all_symbols_for_name_by_kind(
        result,
        "c:@F@double_absolute_value",
        CursorKind.CALL_EXPR,  # type: ignore[reportAttributeAccessIssue]
    ) == {"c:@F@add", "c:@F@fabs", "c:@F@pow", "c:@F@log10", "c:@F@printf"}


def test_typedefs(c_data_structures_code: str):
    tu = ast.create_translation_unit(c_data_structures_code)
    result = ast.extract_info_c(tu)

    typedefs = get_all_decl_by_kind(result, CursorKind.TYPEDEF_DECL)  # type: ignore[reportAttributeAccessIssue]
    assert {"c:file.c@T@radius_t"} < typedefs.keys()
    assert typedefs["c:file.c@T@radius_t"] == "typedef double radius_t"


def test_variables(c_data_structures_code: str):
    tu = ast.create_translation_unit(c_data_structures_code)
    result = ast.extract_info_c(tu)

    variables = get_all_decl_by_kind(result, CursorKind.VAR_DECL)  # type: ignore[reportAttributeAccessIssue]
    assert {
        "c:@p1",
        "c:@num_dimensions",
        "c:@anonymous_struct",
        "c:@half_pi",
        "c:file.c@one_third_pi",
        "c:file.c@quarter_pi",
        "c:@PI",
        "c:@e_powers",
        "c:@circle_color",
    } < variables.keys()

    assert variables["c:@p1"] == "struct Point p1"
    assert variables["c:@num_dimensions"] == "int num_dimensions"
    assert variables["c:@anonymous_struct"] == "struct { int a; int b; } anonymous_struct"
    assert variables["c:@half_pi"] == "const double half_pi"
    assert (
        ast.get_cursor_code(result.symbols["c:@half_pi"].cursor)
        == "const double half_pi = PI / 2.0;"
    )
    assert variables["c:file.c@one_third_pi"] == "static double one_third_pi"
    assert variables["c:file.c@quarter_pi"] == "static const double quarter_pi"
    assert variables["c:@PI"] == "extern const double PI"
    assert variables["c:@e_powers"] == "extern float e_powers[4]"
    assert variables["c:@circle_color"] == "enum Color circle_color"


def test_structs(c_data_structures_code: str):
    tu = ast.create_translation_unit(c_data_structures_code)
    result = ast.extract_info_c(tu)

    structs = get_all_decl_by_kind(result, CursorKind.STRUCT_DECL)  # type: ignore[reportAttributeAccessIssue]
    assert {"c:@S@Point"} < structs.keys()
    assert (
        structs["c:@S@Point"]
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
    assert enums.keys() == {"c:@E@Color"}
    assert (
        enums["c:@E@Color"]
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
