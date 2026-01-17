from pathlib import Path

from ideas import ast, tools

C_CODE = """
#include <stdlib.h>

int var0 = 1;
static int {prefix}var1;
static int {prefix}var2 = 10;

void func0();
static void {prefix}func1();
static void {prefix}func1() {{ }}

static void {prefix}func2() {{
    var0 += 1;
    {prefix}var1 += 1;
    {prefix}var2 += 1;
    func0();
    {prefix}func1();
    {prefix}func2();
}}

void func0() {{
    static int var1 = 0;
    static int var2;
    static int var3;
    var0 += 1;
    var1 += 1;
    var2 += 2;
    var3 += 1;
    func0();
    {prefix}func1();
    {prefix}func2();
}}
"""


def test_get_internally_linked_cursors():
    c_code = C_CODE.format(prefix="")
    tu = ast.create_translation_unit(c_code)
    assert tu.cursor is not None
    statics = [
        node.spelling
        for node in ast.get_internally_linked_cursors(tu.cursor, filter_system=True)
    ]
    assert "var1" in statics
    assert "var2" in statics
    assert "func1" in statics
    assert "func2" in statics
    # stdlib.h internally linked declarations should be filtered
    assert len(statics) == 4


def test_get_internally_linked_cursors_no_filter():
    c_code = C_CODE.format(prefix="")
    tu = ast.create_translation_unit(c_code)
    assert tu.cursor is not None
    statics = [
        node.spelling
        for node in ast.get_internally_linked_cursors(tu.cursor, filter_system=False)
    ]
    # stdlib.h should include internally linked declarations
    assert len(statics) > 4


def test_clang_rename(tmp_path: Path):
    prefix = "main_"
    expected_c_code = C_CODE.format(prefix=prefix)

    c_code_path = tmp_path / "c_code.c"
    c_code_path.write_text(C_CODE.format(prefix=""))
    tools.clang_rename_(
        c_code_path,
        {
            "var1": f"{prefix}var1",
            "var2": f"{prefix}var2",
            "func1": f"{prefix}func1",
            "func2": f"{prefix}func2",
        },
    )
    actual_c_code = c_code_path.read_text()
    assert actual_c_code == expected_c_code
