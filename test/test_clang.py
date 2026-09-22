#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#


from textwrap import dedent

import pytest

from ideas import ast
from clang.cindex import TranslationUnit, CursorKind, Diagnostic


def parse_c(code: str) -> TranslationUnit:
    return ast.create_translation_unit(ast.CodeC(code))


def parse_errors(code: str) -> list[str]:
    # Bypasses create_translation_unit, which raises instead of reporting diagnostics
    tu = TranslationUnit.from_source("file.c", unsaved_files=[("file.c", code)])
    return [d.spelling for d in tu.diagnostics if d.severity >= Diagnostic.Error]


def test_basic_fns():
    code = dedent(
        """
        int add_without_definition(int a, int b);

        int add(int a, int b) {
            return a + b;
        }

        static int helper(int x) {
            return x * 2;
        }

        extern void other(int y);
        """
    )

    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    assert len(tr.symbols) == 4
    assert "c:@F@add_without_definition" in tr.symbols
    assert "c:@F@add" in tr.symbols
    # Statics have a different name convention
    assert "c:file.c@F@helper" in tr.symbols
    assert "c:@F@other" in tr.symbols


def test_detailed_complete_graph():
    code = dedent(
        """
        int add(int a, int b) {
            return a + b;
        }

        int subtract(int a, int b) {
            return add(a, -b);
        }

        int main() {
            int result = subtract(3, 4);
            return 0;
        }
        """
    )

    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    assert len(tr.symbols) == 3
    assert "c:@F@add" in tr.symbols
    assert "c:@F@subtract" in tr.symbols
    assert "c:@F@main" in tr.symbols

    assert len(tr.complete_graph["c:@F@add"]) == 0
    assert len(tr.complete_graph["c:@F@subtract"]) == 2
    dep_names = tr.complete_graph["c:@F@subtract"]
    # Reference to called function appears twice - actual call and declaration reference
    assert "c:@F@add" in dep_names
    assert sum([name == "c:@F@add" for name in dep_names]) == 2

    assert len(tr.complete_graph["c:@F@main"]) == 2
    dep_names = tr.complete_graph["c:@F@main"]
    # Reference to called function appears twice - actual call and declaration reference
    assert "c:@F@subtract" in dep_names
    assert sum([name == "c:@F@subtract" for name in dep_names]) == 2


def test_basic_types():
    code = dedent(
        """
        typedef double radius_t;

        struct Point {
            int x;
            int y;
        };

        enum Color {
            red = 1,
            green = 10,
            undefined = -1,
        };

        int main() {
            radius_t r = 5.0;
            struct Point p = {10, 20};
            enum Color c = green;

            return 0;
        }
        """
    )

    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    # 3 data structures + 3 enumerators + main
    assert len(tr.symbols) == 7
    assert "c:file.c@T@radius_t" in tr.symbols
    assert "c:@S@Point" in tr.symbols
    assert "c:@E@Color" in tr.symbols
    assert "c:@E@Color@red" in tr.symbols
    assert "c:@E@Color@green" in tr.symbols
    assert "c:@E@Color@undefined" in tr.symbols
    assert "c:@F@main" in tr.symbols

    dep_names = tr.complete_graph["c:@F@main"]
    assert "c:file.c@T@radius_t" in dep_names
    assert "c:@S@Point" in dep_names
    assert "c:@E@Color" in dep_names
    assert "c:@E@Color@green" in dep_names


def test_forward_declaration():
    code = dedent(
        """
        int return_stuff(int input);
        int return_stuff(int input) {
            return input + 1;
        }
        """
    )
    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    assert len(tr.symbols) == 1
    assert "c:@F@return_stuff" in tr.symbols


def test_forward_declaration_of_anonymous_inline_tag_variable():
    code = dedent(
        """
        struct { int x; } anon_var;
        struct { int x; } anon_arr[3];
        union { int a; float b; } anon_union_var;
        enum { A, B } anon_enum_var;
        struct Tag { int y; } tagged_var;
        static struct StaticTag { int z; } static_tagged_var[] = {{0}};
        """
    )
    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    declarations = {
        symbol.spelling: symbol.forward_declaration
        for symbol in tr.symbols.values()
        if symbol.is_variable
    }
    assert declarations["anon_var"] is None
    assert declarations["anon_arr"] is None
    assert declarations["anon_union_var"] is None
    assert declarations["anon_enum_var"] is None
    assert str(declarations["tagged_var"]).strip() == "extern struct Tag tagged_var;"
    # `static struct StaticTag static_tagged_var[];` is a tentative definition, and the tag
    # it names is only completed by the definition this would stand in for
    assert declarations["static_tagged_var"] is None


def test_forward_declaration_of_anonymous_inline_tag_return_type():
    # Each `struct { ... }` mints a distinct type, so repeating one in a prototype conflicts
    # with the definition. No valid forward declaration exists for these.
    code = dedent(
        """
        struct { int x; } anon_ret(void);
        enum { A, B } anon_enum_ret(void);
        struct Tag { int y; } tagged_ret(void);
        int plain(int a);
        """
    )
    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    declarations = {
        symbol.spelling: symbol.forward_declaration
        for symbol in tr.symbols.values()
        if symbol.is_function
    }
    assert declarations["anon_ret"] is None
    assert declarations["anon_enum_ret"] is None
    assert str(declarations["tagged_ret"]).strip() == "struct Tag tagged_ret(void);"
    assert str(declarations["plain"]).strip() == "int plain(int a);"


def test_fake_quotes_unicode():
    # NOTE: Contains unicode character “
    code = dedent(
        r"""
        //“

        #include <stdio.h>

        int main() {
            printf("Hello World!\n");
            return 0;
        }
        """
    )
    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    assert (
        tr.symbols["c:@F@main"].code.code
        == dedent(
            r"""
            int main() {
                printf("Hello World!\n");
                return 0;
            }
            """
        ).strip()
        + "\n"
    )


def test_declaration_after_definition():
    code = dedent(
        """
        static const int a[10] = { 0 };
        static const int a[10];
        """
    )
    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    assert len(tr.symbols) == 1
    assert "c:file.c@a" in tr.symbols


def test_empty_statement():
    code = ";"
    tu = parse_c(code)
    tr = ast.extract_info_c(tu)
    assert len(tr.symbols) == 0
    assert len(tr.complete_graph) == 0


def test_nested_structs():
    code = dedent(
        """
        struct x {
            struct y {
                int c;
            } b;
        };

        void test() {
            struct z {
                int a;
            };
            struct z a;
            a.a = 1;
        }

        int main(int x) {
            struct y b;
            b.c = x;
        }
        """
    )
    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    assert len(tr.symbols) == 4
    assert "c:@S@x" in tr.symbols
    assert "c:@S@y" in tr.symbols
    assert "c:@F@test" in tr.symbols
    assert "c:@F@main" in tr.symbols

    assert len(tr.complete_graph) == 4
    assert "c:@S@x" in tr.complete_graph
    assert "c:@S@y" in tr.complete_graph
    assert "c:@F@test" in tr.complete_graph
    assert "c:@F@main" in tr.complete_graph

    # x has no dependencies
    assert len(tr.complete_graph["c:@S@x"]) == 0

    # test has no dependencies
    assert len(tr.complete_graph["c:@F@test"]) == 0

    # main depends upon x
    assert "c:@S@y" in tr.complete_graph["c:@F@main"]
    assert len(tr.complete_graph["c:@F@main"]) == 1


def test_nested_structs_carry_their_bindgen_name():
    code = dedent(
        """
        struct histindex {
            struct record {
                unsigned ptr;
            } **records;
        };

        struct a {
            struct b {
                struct c {
                    int x;
                } cc;
            } bb;
        };

        struct outer {
            struct {
                struct hidden { int x; } h;
            } anon;
        };

        void use(struct histindex *h, struct a *p, struct outer *o) {}
        """
    )
    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    # C keeps tags in a flat namespace, so the USR of a nested record is unqualified
    assert tr.symbols["c:@S@histindex"].bindgen_name == "histindex"
    assert tr.symbols["c:@S@record"].bindgen_name == "histindex_record"

    # The whole chain is walked, not just the outermost declaration
    assert tr.symbols["c:@S@c"].bindgen_name == "a_b_c"

    # bindgen invents a counter-dependent name for an anonymous record, so it is as
    # unnameable as anything nested inside it
    assert tr.symbols["c:@S@hidden"].bindgen_name is None
    assert any(
        s.bindgen_name is None and s.kind == CursorKind.STRUCT_DECL for s in tr.symbols.values()
    )


def test_forward_typedef_struct():
    code = dedent(
        """
        typedef struct s s_t;
        struct s {
            struct s *a;
        };
        """
    )
    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    assert "c:@S@s" in tr.symbols
    assert "c:@S@s" in tr.complete_graph
    assert (
        tr.symbols["c:@S@s"].code.code
        == dedent(
            """
            struct s {
                struct s *a;
            };
            """
        ).strip()
        + "\n"
    )

    assert "c:file.c@T@s_t" in tr.symbols
    assert "c:file.c@T@s_t" in tr.complete_graph
    assert (
        tr.symbols["c:file.c@T@s_t"].code.code
        == dedent(
            """
            typedef struct s s_t;
            """
        ).strip()
        + "\n"
    )

    assert len(tr.complete_graph["c:@S@s"]) == 1
    assert tr.complete_graph["c:@S@s"][0] == "c:@S@s"
    assert len(tr.complete_graph["c:file.c@T@s_t"]) == 1
    assert tr.complete_graph["c:file.c@T@s_t"][0] == "c:@S@s"


def test_backward_typedef_struct():
    code = dedent(
        """
        struct s {
            struct s *a;
        };
        typedef struct s s_t;
        """
    )
    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    assert "c:@S@s" in tr.symbols
    assert "c:@S@s" in tr.complete_graph
    assert (
        tr.symbols["c:@S@s"].code.code
        == dedent(
            """
            struct s {
                struct s *a;
            };
            """
        ).strip()
        + "\n"
    )

    assert "c:file.c@T@s_t" in tr.symbols
    assert "c:file.c@T@s_t" in tr.complete_graph
    assert (
        tr.symbols["c:file.c@T@s_t"].code.code
        == dedent(
            """
            typedef struct s s_t;
            """
        ).strip()
        + "\n"
    )

    assert len(tr.complete_graph["c:@S@s"]) == 1
    assert tr.complete_graph["c:@S@s"][0] == "c:@S@s"
    assert len(tr.complete_graph["c:file.c@T@s_t"]) == 1
    assert tr.complete_graph["c:file.c@T@s_t"][0] == "c:@S@s"


def test_tag_typedef_struct():
    code = dedent(
        """
        typedef struct s {
            struct s *a;
        } s_t;
        """
    )
    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    assert "c:@S@s" in tr.symbols
    assert "c:@S@s" in tr.complete_graph
    assert (
        tr.symbols["c:@S@s"].code.code
        == dedent(
            """
            typedef struct s {
                struct s *a;
            } s_t;
            """
        ).strip()
        + "\n"
    )

    assert "c:file.c@T@s_t" in tr.symbols
    assert "c:file.c@T@s_t" in tr.complete_graph
    assert (
        tr.symbols["c:file.c@T@s_t"].code.code
        == dedent(
            """
            typedef struct s {
                struct s *a;
            } s_t;
            """
        ).strip()
        + "\n"
    )

    assert len(tr.complete_graph["c:@S@s"]) == 1
    assert tr.complete_graph["c:@S@s"][0] == "c:@S@s"
    assert len(tr.complete_graph["c:file.c@T@s_t"]) == 1
    assert tr.complete_graph["c:file.c@T@s_t"][0] == "c:@S@s"


def test_local_struct():
    code = dedent(
        """
        struct a_s {
            int a;
        };

        void test1() {
            struct a_s {
                int b;
            };

            struct a_s a;
            a.b = 1;
        }

        void test2() {
            struct a_s {
                char c;
            };
            struct a_s a;
            a.c = 1;
        }

        void test() {
            struct b_s {
                struct a_s a;
            };
            struct b_s a;
            a.a.a = 1;
        }
        """
    )
    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    assert len(tr.symbols) == 4
    assert "c:@S@a_s" in tr.symbols
    assert "c:@F@test1" in tr.symbols
    assert "c:@F@test2" in tr.symbols
    assert "c:@F@test" in tr.symbols

    assert len(tr.complete_graph["c:@S@a_s"]) == 0
    assert len(tr.complete_graph["c:@F@test1"]) == 0
    assert len(tr.complete_graph["c:@F@test2"]) == 0
    assert len(tr.complete_graph["c:@F@test"]) == 1
    assert tr.complete_graph["c:@F@test"][0] == "c:@S@a_s"


def test_complex_typedef():
    code = dedent(
        """
        typedef struct t {
            struct s *a;
            struct t *b;
        } s_t;
        struct s {
            int a;
        };
       """
    )
    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    assert len(tr.symbols) == 3
    assert "c:@S@s" in tr.symbols
    assert "c:@S@t" in tr.symbols
    assert "c:file.c@T@s_t" in tr.symbols

    assert len(tr.complete_graph["c:@S@s"]) == 0
    assert len(tr.complete_graph["c:@S@t"]) == 2
    assert "c:@S@t" in tr.complete_graph["c:@S@t"]
    assert "c:@S@s" in tr.complete_graph["c:@S@t"]
    assert len(tr.complete_graph["c:file.c@T@s_t"]) == 2
    assert "c:@S@s" in tr.complete_graph["c:file.c@T@s_t"]
    assert "c:@S@t" in tr.complete_graph["c:file.c@T@s_t"]  # is this fine?


def test_multifile_typedef():
    header = dedent(
        """
       typedef struct s s_t;
       """
    )
    code = dedent(
        """
       #include "header.h"
       struct s {
           s_t a;
       };
       """
    )
    tu = TranslationUnit.from_source(
        "file.c", ["-I./"], unsaved_files=[("file.c", code), ("./header.h", header)]
    )
    tr = ast.extract_info_c(tu)

    assert "c:@S@s" in tr.symbols
    assert "c:@S@s" in tr.complete_graph

    assert "c:header.h@T@s_t" in tr.symbols
    assert "c:header.h@T@s_t" in tr.complete_graph

    assert len(tr.complete_graph["c:@S@s"]) == 1
    assert tr.complete_graph["c:@S@s"][0] == "c:header.h@T@s_t"
    assert len(tr.complete_graph["c:header.h@T@s_t"]) == 1
    assert tr.complete_graph["c:header.h@T@s_t"][0] == "c:@S@s"


def test_struct_var():
    code = dedent(
        """
        struct S {
            int f;
        } var[] = {
            { 0 },
        };
        """
    )
    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    assert "c:@var" in tr.symbols
    assert (
        tr.symbols["c:@var"].code.code
        == dedent(
            """
            struct S {
                int f;
            } var[] = {{0}};
            """
        ).strip()
        + "\n"
    )


def test_anonymous_struct_var():
    code = dedent(
        """
        struct {
            int f;
        } var[] = {
            { 0 },
        };
        """
    )
    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    assert "c:@var" in tr.symbols
    assert (
        tr.symbols["c:@var"].code.code
        == dedent(
            """
            struct {
                int f;
            } var[] = {{0}};
            """
        ).strip()
        + "\n"
    )


def test_anonymous_struct_function_pointer_var():
    code = dedent(
        """
        struct S {
            int f;
        };
        void fn(struct S s) {
            s.f = 1;
        }
        struct S2 {
            void (*f)(struct S s);
        } var[] = {{fn}};
        """
    )
    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    assert "c:@var" in tr.complete_graph
    assert "c:@S@S" in tr.complete_graph["c:@var"]
    assert "c:@F@fn" in tr.complete_graph["c:@var"]
    assert tr.symbols["c:@S@S2"].code.code == tr.symbols["c:@var"].code.code
    assert "c:@S@S" in tr.complete_graph["c:@S@S2"]
    assert "c:@F@fn" in tr.complete_graph["c:@S@S2"]


def test_struct_in_param():
    code = dedent(
        """
        struct S {
            int s;
        };

        void test(struct S s) {
            s.s = 1;
        }
        """
    )
    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    assert "c:@F@test" in tr.symbols
    assert (
        tr.symbols["c:@F@test"].code.code
        == dedent(
            """
            void test(struct S s) {
                s.s = 1;
            }
            """
        ).strip()
        + "\n"
    )


def test_enum_constant():
    code = dedent(
        """
        enum E {
            EC = 10
        };
        int main() {
            int a = EC;
        }
       """
    )
    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    assert "c:@E@E" in tr.symbols
    assert "c:@E@E" in tr.complete_graph
    assert len(tr.complete_graph["c:@E@E"]) == 0
    assert (
        tr.symbols["c:@E@E"].code.code
        == dedent(
            """
            enum E {
                EC = 10
            };
            """
        ).strip()
        + "\n"
    )

    assert "c:@E@E@EC" in tr.symbols
    assert "c:@E@E@EC" in tr.complete_graph
    assert len(tr.complete_graph["c:@E@E@EC"]) == 0
    assert tr.symbols["c:@E@E@EC"].code.code == tr.symbols["c:@E@E"].code.code

    assert "c:@F@main" in tr.symbols
    assert "c:@F@main" in tr.complete_graph
    assert len(tr.complete_graph["c:@F@main"]) == 1
    assert tr.complete_graph["c:@F@main"][0] == "c:@E@E@EC"


def test_anonymous_enum():
    code = dedent(
        """
        enum {
            EC = 10
        };
        int var[] = { EC };
        """
    )
    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    # Find anonymous enum constant EC
    anonymous_enum_constants = list(
        filter(lambda s: s.kind == CursorKind.ENUM_CONSTANT_DECL, tr.symbols.values())
    )
    assert len(anonymous_enum_constants) == 1
    assert "c:@var" in tr.complete_graph
    assert tr.complete_graph["c:@var"][0] == anonymous_enum_constants[0].name


def test_enum_in_struct():
    code = dedent(
        """
        struct S {
            enum { EC } e;
            struct S *s;
        };
        int i = EC;
        """
    )
    tu = parse_c(code)
    tr = ast.extract_info_c(tu)

    assert (
        tr.symbols["c:@S@S"].code.code
        == dedent(
            """
            struct S {
                enum {
                    EC
                } e;
                struct S *s;
            };
            """
        ).strip()
        + "\n"
    )


def test_clang_make_extern_multiple_declarations(tmp_path):
    c_path = tmp_path / "input.c"
    c_path.write_text(
        dedent(
            """
            static int f(int x);
            int f(int x);
            static int f(int x) {
                return x + 1;
            }
            """
        )
    )

    ast.clang_make_extern_(c_path, "f")
    transformed = c_path.read_text()

    assert transformed.count("extern int f(int x);") == 3
    assert "static int f" not in transformed
    assert "{" not in transformed


def test_clang_make_weak_definition_only(tmp_path):
    c_path = tmp_path / "input.c"
    c_path.write_text(
        dedent(
            """
            int f(int x);
            int f(int x) {
                return x + 1;
            }
            """
        )
    )

    ast.clang_make_weak_(c_path, "f")
    transformed = c_path.read_text()

    assert "__attribute__((weak)) int f(int x) {" in transformed
    assert transformed.count("__attribute__((weak))") == 1


def test_clang_make_weak_is_idempotent(tmp_path):
    c_path = tmp_path / "input.c"
    c_path.write_text(
        dedent(
            """
            int f(int x) {
                return x + 1;
            }
            """
        )
    )

    ast.clang_make_weak_(c_path, "f")
    once = c_path.read_text()
    ast.clang_make_weak_(c_path, "f")

    assert c_path.read_text() == once


def test_clang_make_weak_missing_symbol(tmp_path):
    c_path = tmp_path / "input.c"
    c_path.write_text("int f(int x) { return x; }\n")

    with pytest.raises(ValueError):
        ast.clang_make_weak_(c_path, "main")


def test_clang_make_extern_strips_weak_attribute(tmp_path):
    c_path = tmp_path / "input.c"
    c_path.write_text(
        dedent(
            """
            __attribute__((weak)) int f(int x) {
                return x + 1;
            }
            """
        )
    )

    ast.clang_make_extern_(c_path, "f")
    transformed = c_path.read_text()

    assert "__attribute__" not in transformed
    assert transformed.strip() == "extern int f(int x);"


def test_clang_make_global_multiple_declarations(tmp_path):
    c_path = tmp_path / "input.c"
    c_path.write_text(
        dedent(
            """
            static int v;
            extern int v;
            static int v = 42;
            """
        )
    )

    ast.clang_make_global_(c_path, "v")
    transformed = c_path.read_text()

    assert "static int v" not in transformed
    assert "int v = 42;" in transformed
    assert "extern int v;" in transformed
    assert transformed.count("int v;") == 2


def test_clang_make_global_variable_with_inline_struct_definition(tmp_path):
    c_path = tmp_path / "input.c"
    c_path.write_text(
        dedent(
            """
            static const struct typelen {
                const char *type;
                int length;
            } typelen[] = {{"seconds", 1}, {"minutes", 60}};
            """
        )
    )

    ast.clang_make_global_(c_path, "typelen")
    transformed = c_path.read_text()

    assert "static" not in transformed
    # The tag shares the variable's name, so a duplicated body would redefine the struct
    assert transformed.count("struct typelen") == 1
    assert parse_errors(transformed) == []


def test_clang_make_bindable_function_multiple_declarations(tmp_path):
    c_path = tmp_path / "input.c"
    c_path.write_text(
        dedent(
            """
            static int f(int x);
            int f(int x);
            static int f(int x) {
                return x + 1;
            }
            """
        )
    )

    ast.clang_make_bindable_(c_path, "f")
    transformed = c_path.read_text()

    assert transformed.count("extern int f(int x);") == 3
    assert "static int f" not in transformed
    assert "{" not in transformed


def test_clang_make_bindable_variable_with_initializer(tmp_path):
    c_path = tmp_path / "input.c"
    c_path.write_text("static int j = 0;\n")

    ast.clang_make_bindable_(c_path, "j")
    transformed = c_path.read_text()

    assert transformed == "extern int j;\nint j = 0;\n"


def test_clang_make_bindable_variable_without_initializer(tmp_path):
    c_path = tmp_path / "input.c"
    c_path.write_text("static int j;\n")

    ast.clang_make_bindable_(c_path, "j")
    transformed = c_path.read_text()

    assert transformed == "extern int j;\nint j;\n"


def test_clang_make_bindable_variable_already_extern(tmp_path):
    c_path = tmp_path / "input.c"
    c_path.write_text("extern int j;\n")

    ast.clang_make_bindable_(c_path, "j")
    transformed = c_path.read_text()

    assert transformed == "extern int j;\n"


def test_clang_make_bindable_variable_array_with_initializer(tmp_path):
    c_path = tmp_path / "input.c"
    c_path.write_text("static int arr[3] = {1, 2, 3};\n")

    ast.clang_make_bindable_(c_path, "arr")
    transformed = c_path.read_text()

    assert transformed == "extern int arr[3];\nint arr[3] = {1, 2, 3};\n"


def test_clang_make_bindable_variable_array_without_initializer(tmp_path):
    c_path = tmp_path / "input.c"
    c_path.write_text("int array[3];\n")

    ast.clang_make_bindable_(c_path, "array")
    transformed = c_path.read_text()

    assert transformed == "extern int array[3];\nint array[3];\n"


def test_clang_make_bindable_variable_with_inline_struct_definition(tmp_path):
    c_path = tmp_path / "input.c"
    c_path.write_text(
        dedent(
            """
            static const struct typelen {
                const char *type;
                int length;
            } typelen[] = {{"seconds", 1}, {"minutes", 60}};
            """
        )
    )

    ast.clang_make_bindable_(c_path, "typelen")
    transformed = c_path.read_text()

    # Aggregates already bind as `pub static`, so only the linkage change is needed and a
    # synthesized declaration would redefine the tag
    assert parse_errors(transformed) == []
    assert "static" not in transformed
    assert "extern" not in transformed
    assert transformed.count("struct typelen") == 1


def test_clang_make_bindable_variable_with_anonymous_struct_definition(tmp_path):
    c_path = tmp_path / "input.c"
    c_path.write_text(
        dedent(
            """
            static const struct {
                int parent;
                const char *name;
            } items[] = {{1, "index"}, {2, "objects"}};
            """
        )
    )

    ast.clang_make_bindable_(c_path, "items")
    transformed = c_path.read_text()

    # An anonymous record has no spelling, so no extern declaration of `items` is expressible
    assert parse_errors(transformed) == []
    assert "static" not in transformed
    assert "extern" not in transformed


def test_clang_make_bindable_variable_with_inline_enum_values(tmp_path):
    c_path = tmp_path / "input.c"
    c_path.write_text("static const enum tag { A = 1, B = 2 } e = A;\n")

    ast.clang_make_bindable_(c_path, "e")
    transformed = c_path.read_text()

    # The enumerator `=` must not be mistaken for the initializer
    assert parse_errors(transformed) == []
    assert transformed.count("enum tag") == 1


def test_clang_rename_updates_declarations_and_call_sites(tmp_path):
    c_path = tmp_path / "input.c"
    c_path.write_text(
        dedent(
            """
            int main(int argc, char **argv);
            int main(int argc, char **argv) {
                return argv ? argc : 0;
            }
            int again(void) { return main(0, 0); }
            """
        )
    )

    ast.clang_rename_(c_path, {"main": "ideas_c_main"})
    transformed = c_path.read_text()

    assert parse_errors(transformed) == []
    assert "main(" not in transformed.replace("ideas_c_main(", "")
    assert transformed.count("ideas_c_main") == 3


def test_clang_rename_typedef_leaves_same_spelled_tag_alone(tmp_path):
    # Tags and ordinary identifiers are separate namespaces, so `typedef struct s s;`
    # names two entities with one spelling; renaming the typedef must not touch the tag,
    # whose definition may live in a file this rename never visits.
    c_path = tmp_path / "input.c"
    c_path.write_text(
        dedent(
            """
            typedef struct session session;
            struct session { int x; };
            int get(session *s) { return s->x; }
            """
        )
    )

    tu = ast.create_translation_unit(c_path)
    assert tu.cursor is not None
    usr = next(
        cursor.get_usr()
        for cursor in tu.cursor.walk_preorder()
        if cursor.kind == CursorKind.TYPEDEF_DECL and cursor.spelling == "session"
    )
    edits = ast.clang_rename(tu, {usr: "renamed_session"})[c_path.resolve()]
    source = bytearray(c_path.read_bytes())
    for (start, end), replacement in sorted(edits.items(), reverse=True):
        source[start:end] = replacement
    transformed = source.decode()

    assert parse_errors(transformed) == []
    assert "typedef struct session renamed_session;" in transformed
    assert "struct session { int x; };" in transformed
    assert "int get(renamed_session *s)" in transformed


def test_clang_rename_updates_type_reference_in_macro_body(tmp_path):
    header_path = tmp_path / "types.h"
    header_path.write_text("typedef int old_type;\n#define CAST(value) ((old_type)(value))\n")
    c_path = tmp_path / "input.c"
    c_path.write_text('#include "types.h"\nold_type cast(int value) { return CAST(value); }\n')

    tu = ast.create_translation_unit(c_path)
    assert tu.cursor is not None
    usr = next(
        cursor.get_usr()
        for cursor in tu.cursor.walk_preorder()
        if cursor.kind == CursorKind.TYPEDEF_DECL and cursor.spelling == "old_type"
    )
    for path, edits in ast.clang_rename(tu, {usr: "new_type"}).items():
        source = bytearray(path.read_bytes())
        for (start, end), replacement in sorted(edits.items(), reverse=True):
            source[start:end] = replacement
        path.write_bytes(source)

    assert "#define CAST(value) ((new_type)(value))" in header_path.read_text()
    ast.create_translation_unit(c_path)


def test_clang_rename_missing_symbol(tmp_path):
    c_path = tmp_path / "input.c"
    c_path.write_text("int f(void) { return 0; }\n")

    with pytest.raises(ValueError):
        ast.clang_rename_(c_path, {"main": "ideas_c_main"})


@pytest.mark.parametrize(
    "source,expected",
    [
        ("int main(void) { return 0; }\n", 0),
        # Pre-C23 unspecified parameters read the same as `(void)` here
        ("int main() { return 0; }\n", 0),
        ("int main(int argc, char **argv) { return argv ? argc : 0; }\n", 2),
        ("int main(int c, char **v, char **e) { return v && e ? c : 0; }\n", 3),
    ],
)
def test_clang_function_arity(tmp_path, source, expected):
    c_path = tmp_path / "input.c"
    c_path.write_text(source)

    assert ast.clang_function_arity(c_path, "main") == expected


def test_clang_function_arity_ignores_declarations(tmp_path):
    c_path = tmp_path / "input.c"
    c_path.write_text("int main();\nint main(int argc, char **argv) { return argc + !argv; }\n")

    assert ast.clang_function_arity(c_path, "main") == 2
