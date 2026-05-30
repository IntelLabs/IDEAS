#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#


from textwrap import dedent
from ideas import ast
from clang.cindex import TranslationUnit, CursorKind


def parse_c(code: str) -> TranslationUnit:
    return ast.create_translation_unit(ast.CodeC(code))


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
