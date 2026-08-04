#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import pytest

from pathlib import Path
from textwrap import dedent
from itertools import permutations

import json

from ideas import ast
from ideas.ast import CodeC, create_symbol_lexical_key_fn
from ideas.consolidate import create_ast_order, analyze, consolidate
from ideas.tools import run_subprocess


def compile_c(code: CodeC, flags: list[str]) -> tuple[bool, str]:
    cmd = ["clang-21", *flags, "-march=native", "-c", "-x", "c", "-", "-o", "/dev/null"]
    success, output, error, _ = run_subprocess(cmd, input=str(code))
    return success, output + error


def consolidate_init(
    compile_commands: Path, source_priority: list[Path] | None = None
) -> CodeC:
    symbols, symbol_order = analyze(compile_commands, source_priority or [])
    return consolidate(symbols, symbol_order)


def _usr_by_spelling(symbols: dict[str, ast.Symbol], spelling: str) -> str:
    for name, symbol in symbols.items():
        if symbol.spelling == spelling:
            return name
    raise AssertionError(f"Unable to find symbol with spelling: {spelling}")


def _write_compile_commands(tmp_path: Path, c_files: list[Path], extra_flags: str = "") -> Path:
    compile_commands = tmp_path / "compile_commands.json"
    flags_part = f"{extra_flags} " if extra_flags else ""
    compile_commands.write_text(
        json.dumps(
            [
                {
                    "directory": str(tmp_path),
                    "file": str(f),
                    "command": f"cc {flags_part}-c {f}",
                }
                for f in c_files
            ]
        )
    )
    return compile_commands


def _build_tus(
    base: Path, sources: dict[str, str], cflags: list[str] | None = None
) -> list[Path]:
    """Materialize `sources` under `base`; return its translation units, sorted by name.

    Each .c is also checked to compile standalone.  Those checks are preconditions on the
    test *inputs*: they say nothing about consolidation and are invariant under
    compile-command order, so they belong here rather than in each parametrized test.  A
    broken input then surfaces as an ERROR rather than a FAILURE.
    """
    for name, code in sources.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dedent(code))

    tus = sorted((base / n for n in sources if n.endswith(".c")), key=lambda p: p.name)
    for tu in tus:
        ok, out, err, _ = run_subprocess(
            [
                "clang-21",
                "-Wall",
                "-Werror",
                f"-I{base}",
                *(cflags or []),
                "-c",
                str(tu),
                "-o",
                str(tu.with_suffix(".o")),
            ]
        )
        assert ok, f"{tu.name} does not compile standalone:\n{out}{err}"

    return tus


def _ast_order_from_symbols(
    symbols: dict[str, ast.Symbol], source_priority: list[Path] | None = None
) -> dict[Path, ast.TreeResult]:
    source_priority = source_priority or []
    tu_representatives: dict[Path, ast.Symbol] = {}
    for symbol in symbols.values():
        tu_representatives.setdefault(symbol.tu_path, symbol)
    fallback_asts = [
        ast.TreeResult(symbols={symbol.name: symbol}) for symbol in tu_representatives.values()
    ]
    return create_ast_order(source_priority, fallback_asts)


def test_symbol_key_is_callable():
    tu = ast.create_translation_unit(ast.CodeC("int alpha(void) { return 1; }"))
    symbols = ast.extract_info_c(tu).symbols

    usr = _usr_by_spelling(symbols, "alpha")
    key_fn = create_symbol_lexical_key_fn(symbols, _ast_order_from_symbols(symbols))

    # Comparing a symbol with itself should return equal keys
    assert key_fn(usr) == key_fn(usr)


def test_symbol_group_key_uses_first_symbol():
    tu = ast.create_translation_unit(
        ast.CodeC("int alpha(void) { return 1; } int beta(void) { return 2; }")
    )
    symbols = ast.extract_info_c(tu).symbols
    alpha_usr = _usr_by_spelling(symbols, "alpha")
    beta_usr = _usr_by_spelling(symbols, "beta")

    key_fn = create_symbol_lexical_key_fn(symbols, _ast_order_from_symbols(symbols))
    # Group key uses first element
    assert key_fn((alpha_usr, beta_usr)) == key_fn(alpha_usr)


def test_symbol_key_uses_tu_ast_order(tmp_path: Path):
    a_path = tmp_path / "a.c"
    b_path = tmp_path / "b.c"
    a_path.write_text("int alpha(void) { return 1; }")
    b_path.write_text("int beta(void) { return 2; }")

    a_tree = ast.extract_info_c(ast.create_translation_unit(a_path))
    b_tree = ast.extract_info_c(ast.create_translation_unit(b_path))
    a_symbols = a_tree.symbols
    b_symbols = b_tree.symbols
    symbols = {**a_symbols, **b_symbols}

    alpha_usr = _usr_by_spelling(symbols, "alpha")
    beta_usr = _usr_by_spelling(symbols, "beta")

    ast_order = create_ast_order([b_path, a_path], [a_tree, b_tree])
    key_fn = create_symbol_lexical_key_fn(symbols, ast_order=ast_order)

    # beta (from b.c) should sort before alpha (from a.c) because b.c has higher priority
    assert key_fn(beta_usr) < key_fn(alpha_usr)


def test_symbol_key_uses_include_order(tmp_path: Path):
    a_header = tmp_path / "a.h"
    b_header = tmp_path / "b.h"
    main_c = tmp_path / "main.c"

    a_header.write_text(
        dedent(
            """
            #ifndef A_H
            #define A_H
            static inline int from_a(void) { return 1; }
            #endif
            """
        )
    )
    b_header.write_text(
        dedent(
            """
            #ifndef B_H
            #define B_H
            static inline int from_b(void) { return 2; }
            #endif
            """
        )
    )
    main_c.write_text(
        dedent(
            """
            #include "b.h"
            #include "a.h"

            int main(void) {
                return from_a() + from_b();
            }
            """
        )
    )

    symbols = ast.extract_info_c(ast.create_translation_unit(main_c)).symbols
    from_a_usr = _usr_by_spelling(symbols, "from_a")
    from_b_usr = _usr_by_spelling(symbols, "from_b")

    key_fn = create_symbol_lexical_key_fn(symbols, _ast_order_from_symbols(symbols))

    # from_b is included first, so it should sort before from_a
    assert key_fn(from_b_usr) < key_fn(from_a_usr)


def test_nested_include_symbols_do_not_tie(tmp_path: Path):
    parent_header = tmp_path / "parent.h"
    nested_header = tmp_path / "nested.h"
    main_c = tmp_path / "main.c"

    nested_header.write_text(
        dedent(
            """
            #ifndef NESTED_H
            #define NESTED_H
            static inline int nested_sym(void) { return 7; }
            #endif
            """
        )
    )
    parent_header.write_text(
        dedent(
            """
            #ifndef PARENT_H
            #define PARENT_H
            #include "nested.h"
            static inline int parent_sym(void) { return 11; }
            #endif
            """
        )
    )
    main_c.write_text(
        dedent(
            """
            #include "parent.h"

            int main(void) {
                return parent_sym() + nested_sym();
            }
            """
        )
    )

    symbols = ast.extract_info_c(ast.create_translation_unit(main_c)).symbols
    parent_usr = _usr_by_spelling(symbols, "parent_sym")
    nested_usr = _usr_by_spelling(symbols, "nested_sym")

    key_fn = create_symbol_lexical_key_fn(symbols, _ast_order_from_symbols(symbols))

    # Desired behavior: nested_sym and parent_sym should not compare as equal
    assert key_fn(nested_usr) != key_fn(parent_usr)


def test_consolidation_places_typedef_before_struct_definition(tmp_path: Path):
    types_h = tmp_path / "types.h"
    thing_h = tmp_path / "thing.h"
    thing_c = tmp_path / "thing.c"

    types_h.write_text(
        dedent(
            """\
            typedef struct not_renamed not_renamed;
            """
        )
    )
    thing_h.write_text(
        dedent(
            """\
            #include "types.h"

            struct not_renamed {
                int (*notify)(not_renamed *self, int status);
                void *payload;
            };

            int not_renamed_init(not_renamed *out);
            """
        )
    )
    thing_c.write_text(
        dedent(
            """\
            #include "thing.h"

            int not_renamed_init(not_renamed *out) {
                out->notify = 0;
                out->payload = 0;
                return 0;
            }
            """
        )
    )

    # Fully compile the TU to an object file (resolves thing.h/types.h).
    ok, out, err, _ = run_subprocess(
        [
            "clang-21",
            "-Wall",
            "-Werror",
            f"-I{tmp_path}",
            "-c",
            str(thing_c),
            "-o",
            str(tmp_path / "thing.o"),
        ]
    )
    assert ok, f"thing.c failed to compile:\n{out}{err}"

    # Parse the C file (which transitively includes types.h via thing.h)
    compile_commands = _write_compile_commands(tmp_path, [thing_c])
    consolidated = consolidate_init(compile_commands, source_priority=[])

    text = str(consolidated)
    assert "not_renamed_init" in text
    assert "thing_not_renamed" not in text and "types_not_renamed" not in text

    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile:\n{error}\n\nConsolidated output:\n{consolidated}"
    )


@pytest.fixture(scope="module")
def cross_tu_typedef_tus(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    return _build_tus(
        tmp_path_factory.mktemp("cross_tu_typedef"),
        {
            "types.h": """\
                typedef struct not_renamed not_renamed;
                """,
            "node.h": """\
                struct not_renamed {
                    int val;
                    struct not_renamed *next;
                };
                """,
            "a.c": """\
                #include "node.h"

                int node_get_val(struct not_renamed *n) {
                    return n->val;
                }
                """,
            "b.c": """\
                #include "types.h"
                #include "node.h"

                not_renamed *node_create(int val) {
                    (void)val;
                    return (not_renamed *)0;
                }
                """,
        },
    )


@pytest.mark.parametrize("order", permutations(["a.c", "b.c"]), ids="-".join)
def test_consolidation_typedef_before_struct_cross_tu(
    cross_tu_typedef_tus: list[Path], order: tuple[str, ...]
):
    # After merge_symbols, the struct may retain its cursor from one TU and the typedef
    # from another, making cross-TU location comparison undefined.  The result must not
    # depend on the order the TUs appear in compile_commands.json.
    a_c, b_c = cross_tu_typedef_tus
    by_name = {tu.name: tu for tu in cross_tu_typedef_tus}

    compile_commands = _write_compile_commands(a_c.parent, [by_name[n] for n in order])
    consolidated = consolidate_init(
        compile_commands, source_priority=[a_c.resolve(), b_c.resolve()]
    )

    text = str(consolidated)
    assert "not_renamed" in text
    assert "a_not_renamed" not in text and "b_not_renamed" not in text, consolidated

    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile:\n{error}\n\nConsolidated output:\n{consolidated}"
    )


@pytest.fixture(scope="module")
def mutual_typedef_tus(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    return _build_tus(
        tmp_path_factory.mktemp("mutual_typedef"),
        {
            "a_types.h": """\
                typedef struct not_renamed_a not_renamed_a;
                """,
            "b_types.h": """\
                typedef struct not_renamed_b not_renamed_b;
                """,
            "a.c": """\
                #include "a_types.h"
                #include "b_types.h"

                struct not_renamed_a {
                    not_renamed_b *ref;
                    int val;
                };

                not_renamed_a *create_a(void) {
                    return (not_renamed_a *)0;
                }
                """,
            "b.c": """\
                #include "a_types.h"
                #include "b_types.h"

                struct not_renamed_b {
                    not_renamed_a *ref;
                    int val;
                };

                not_renamed_b *create_b(void) {
                    return (not_renamed_b *)0;
                }
                """,
        },
    )


@pytest.mark.parametrize("order", permutations(["a.c", "b.c"]), ids="-".join)
def test_consolidation_mutual_cross_tu_typedefs(
    mutual_typedef_tus: list[Path], order: tuple[str, ...]
):
    """
    Mutual cross-references create a cycle that merges symbols from different TUs
    into one SCC:
      - a_types.h: typedef struct not_renamed_a not_renamed_a;
      - b_types.h: typedef struct not_renamed_b not_renamed_b;
      - a.c: includes both, defines struct not_renamed_a { not_renamed_b *ref; };
      - b.c: includes both, defines struct not_renamed_b { not_renamed_a *ref; };

    After merge the two structs and two typedefs form one SCC with cross-TU cursors.
    clang_isBeforeInTranslationUnit is undefined across TUs, so the comparator must
    still produce compilable output. Neither type collides, so nothing is renamed.
    """
    a_c, b_c = mutual_typedef_tus
    by_name = {tu.name: tu for tu in mutual_typedef_tus}

    compile_commands = _write_compile_commands(a_c.parent, [by_name[n] for n in order])
    consolidated = consolidate_init(
        compile_commands, source_priority=[a_c.resolve(), b_c.resolve()]
    )

    text = str(consolidated)
    assert "create_a" in text and "create_b" in text
    assert "a_not_renamed_a" not in text and "b_not_renamed_b" not in text, consolidated

    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile:\n{error}\n\nConsolidated output:\n{consolidated}"
    )


def test_macro_wrapped_declaration(tmp_path: Path):
    api_h = tmp_path / "api.h"
    impl_c = tmp_path / "impl.c"

    api_h.write_text(
        dedent(
            """\
            #define LIB_EXPORT(type) extern type

            typedef struct not_renamed not_renamed;

            LIB_EXPORT(void) my_free(not_renamed *obj);
            LIB_EXPORT(int) my_get_value(not_renamed *obj);
            """
        )
    )

    # my_free calls my_get_value and vice versa => mutual recursion => SCC
    impl_c.write_text(
        dedent(
            """\
            #include "api.h"

            struct not_renamed {
                int value;
                int refcount;
            };

            void my_free(not_renamed *obj)
            {
                if (obj && my_get_value(obj) < 0) {
                    /* free */
                }
            }

            int my_get_value(not_renamed *obj)
            {
                my_free(obj);
                return obj->value;
            }
            """
        )
    )

    # Fully compile the TU to an object file (resolves api.h).
    ok, out, err, _ = run_subprocess(
        [
            "clang-21",
            "-Wall",
            "-Werror",
            f"-I{tmp_path}",
            "-c",
            str(impl_c),
            "-o",
            str(tmp_path / "impl.o"),
        ]
    )
    assert ok, f"impl.c failed to compile:\n{out}{err}"

    compile_commands = _write_compile_commands(tmp_path, [impl_c])
    consolidated = consolidate_init(compile_commands, source_priority=[])

    text = str(consolidated)
    assert "not_renamed" in text
    assert "impl_not_renamed" not in text

    # The consolidated output must not contain the unexpanded macro
    assert "LIB_EXPORT" not in str(consolidated), (
        f"Consolidated output contains unexpanded macro 'LIB_EXPORT':\n{consolidated}"
    )

    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile:\n{error}\n\nConsolidated output:\n{consolidated}"
    )


@pytest.fixture(scope="module")
def three_tu_typedef_tus(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    return _build_tus(
        tmp_path_factory.mktemp("three_tu_typedef"),
        {
            "types.h": """\
                typedef struct not_renamed not_renamed;
                """,
            "a.c": """\
                #include "types.h"

                struct not_renamed {
                    not_renamed *self;
                    int val;
                };

                not_renamed *create_x(int v) {
                    (void)v;
                    return (not_renamed *)0;
                }
                """,
            "b.c": """\
                #include "types.h"

                void consume_x(not_renamed *p) {
                    (void)p;
                }
                """,
            "c.c": """\
                #include "types.h"

                struct Y {
                    not_renamed *member;
                    int id;
                };

                struct Y *alloc_y(void) {
                    return (struct Y *)0;
                }
                """,
        },
    )


@pytest.mark.parametrize("order", permutations(["a.c", "b.c", "c.c"]), ids="-".join)
def test_typedef_after_struct_cross_tu_three_tus(
    three_tu_typedef_tus: list[Path], order: tuple[str, ...]
):
    """
      - types.h: typedef struct not_renamed not_renamed;  (forward-declares the struct)
      - TU1 (a.c): #include "types.h", defines struct not_renamed { not_renamed *self; };
                    The struct body uses the typedef name -> creates cycle:
                    struct not_renamed -> typedef not_renamed -> struct not_renamed
      - TU2 (b.c): #include "types.h" only, uses not_renamed* in a function signature
      - TU3 (c.c): #include "types.h", defines struct Y { not_renamed *member; }; and a
                    function returning Y*

    After merge_symbols with asts=[TU2, TU3, TU1]:
      - typedef not_renamed cursor retained from TU2 (first encounter, definition)
      - struct not_renamed cursor from TU1 (only TU with full definition)

    With ast_order=[a.c, b.c, c.c]:
      - struct not_renamed from a.c -> rank 0
      - typedef not_renamed from b.c -> rank 1

    They share an SCC (mutual dependency via not_renamed *self in the struct body),
    so _merge_pure_type_declaration_sccs sorts them by symbol_lexical_key which uses
    ast_order ranks. struct not_renamed (rank 0) must not sort before the typedef.

    'not_renamed' is a single shared entity, so it must not be renamed; the typedef
    must still precede the struct definition that uses it as a bare type name.
    """
    a_c, b_c, c_c = three_tu_typedef_tus
    by_name = {tu.name: tu for tu in three_tu_typedef_tus}

    compile_commands = _write_compile_commands(a_c.parent, [by_name[n] for n in order])
    consolidated = consolidate_init(
        compile_commands, source_priority=[a_c.resolve(), b_c.resolve(), c_c.resolve()]
    )

    text = str(consolidated)
    assert "create_x" in text
    assert (
        "a_not_renamed" not in text
        and "b_not_renamed" not in text
        and "types_not_renamed" not in text
    ), consolidated

    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile:\n{error}\n\nConsolidated output:\n{consolidated}"
    )


@pytest.fixture(scope="module")
def static_fn_and_var_tus(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    return _build_tus(
        tmp_path_factory.mktemp("static_fn_and_var"),
        {
            "a.c": """\
                static int renamed(int x) { return x; }

                int use_a(void) { return renamed(42); }
                """,
            "b.c": """\
                static int renamed;

                int use_b(void) { return renamed; }
                """,
        },
    )


@pytest.mark.parametrize("order", permutations(["a.c", "b.c"]), ids="-".join)
def test_static_function_and_static_variable_same_name_renamed(
    static_fn_and_var_tus: list[Path], order: tuple[str, ...]
):
    a_c, b_c = static_fn_and_var_tus
    by_name = {tu.name: tu for tu in static_fn_and_var_tus}

    compile_commands = _write_compile_commands(a_c.parent, [by_name[n] for n in order])
    consolidated = consolidate_init(
        compile_commands, source_priority=[a_c.resolve(), b_c.resolve()]
    )

    text = str(consolidated)
    assert "a_renamed" in text and "b_renamed" in text, consolidated

    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile (missing rename for static name collision):\n"
        f"{error}\n\nConsolidated output:\n{consolidated}"
    )


@pytest.fixture(scope="module")
def static_tentative_def_tus(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    return _build_tus(
        tmp_path_factory.mktemp("static_tentative_def"),
        {
            "a.c": """\
                static int renamed;

                int get_a(void) { renamed += 2; return renamed; }
                """,
            "b.c": """\
                static char renamed;

                int get_b(void) { return (int)renamed + 1; }
                """,
        },
    )


@pytest.mark.parametrize("order", permutations(["a.c", "b.c"]), ids="-".join)
def test_static_variable_tentative_defs_same_name_renamed(
    static_tentative_def_tus: list[Path], order: tuple[str, ...]
):
    a_c, b_c = static_tentative_def_tus
    by_name = {tu.name: tu for tu in static_tentative_def_tus}

    compile_commands = _write_compile_commands(a_c.parent, [by_name[n] for n in order])
    consolidated = consolidate_init(
        compile_commands, source_priority=[a_c.resolve(), b_c.resolve()]
    )

    text = str(consolidated)
    assert "a_renamed" in text and "b_renamed" in text, consolidated

    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile (missing rename for static variable collision):\n"
        f"{error}\n\nConsolidated output:\n{consolidated}"
    )


@pytest.fixture(scope="module")
def isystem_tus(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    base = tmp_path_factory.mktemp("isystem")
    return _build_tus(
        base,
        {
            "util/alloc.h": """\
                #include <stdlib.h>
                static inline void *not_renamed(size_t len) {
                    return malloc(len);
                }
                """,
            "ext/bridge.h": """\
                #include "alloc.h"
                #define ext_malloc(x) not_renamed(x)
                """,
            "ext/a.c": """\
                #include "bridge.h"

                typedef struct { int val; } item_t;

                static item_t *make_item(int val) {
                    item_t *p;
                    if (!(p = (item_t *)ext_malloc(sizeof(item_t))))
                        return (void *)0;
                    p->val = val;
                    return p;
                }

                int do_work(int x) {
                    item_t *item = make_item(x);
                    if (item) return item->val;
                    return -1;
                }
                """,
            "util/b.c": """\
                #include "alloc.h"

                void *my_calloc(size_t n, size_t sz) {
                    void *p = not_renamed(n * sz);
                    return p;
                }
                """,
        },
        cflags=[f"-I{base / 'util'}", f"-I{base / 'ext'}"],
    )


@pytest.mark.parametrize("order", permutations(["a.c", "b.c"]), ids="-".join)
def test_isystem_inline_function_dependency_not_lost(
    isystem_tus: list[Path], order: tuple[str, ...]
):
    """
    When a header is included via -isystem in one TU but via -I in another,
    clang generates different USRs for the same static inline function
    (e.g. "c:@F@fn" vs "c:file.h@F@fn"). This causes the dependency edge
    from a caller in the -isystem TU to be silently dropped during the
    .subgraph(project_symbols.keys()) step, because the system-style USR
    doesn't match the non-system USR retained in project_symbols.

    This manifests as the inline function definition being placed AFTER
    its caller in the consolidated output, causing:
      error: call to undeclared function 'not_renamed'; ISO C99 and later do not
      support implicit function declarations
    """
    a_c, b_c = isystem_tus
    ext_dir, util_dir = a_c.parent, b_c.parent

    # Keep mixed include modes across TUs to exercise the USR normalization path.
    entries = {
        "a.c": {
            "directory": str(ext_dir.parent),
            "file": str(a_c),
            "command": f"cc -isystem {util_dir} -I{ext_dir} -c {a_c}",
        },
        "b.c": {
            "directory": str(ext_dir.parent),
            "file": str(b_c),
            "command": f"cc -I{util_dir} -c {b_c}",
        },
    }
    compile_commands = ext_dir.parent / "compile_commands.json"
    compile_commands.write_text(json.dumps([entries[n] for n in order]))

    consolidated = consolidate_init(
        compile_commands, source_priority=[a_c.resolve(), b_c.resolve()]
    )

    text = str(consolidated)
    assert "not_renamed" in text
    assert "alloc_not_renamed" not in text and "b_not_renamed" not in text, consolidated
    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile (isystem USR mismatch lost dependency):\n"
        f"{error}\n\nConsolidated output:\n{consolidated}"
    )


@pytest.fixture(scope="module")
def scc_static_inline_tus(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    return _build_tus(
        tmp_path_factory.mktemp("scc_static_inline"),
        {
            "header.h": """\
                struct vtable_t { int (*fn)(int); };
                extern struct vtable_t vtable;
                static inline int not_renamed(int x) {
                    return vtable.fn(x);
                }
                """,
            # a.c (rank 0): defines compute() which calls not_renamed()
            "a.c": """\
                #include "header.h"
                int compute(int x) {
                    return not_renamed(x) + 1;
                }
                """,
            # b.c (rank 1): includes header.h, defines vtable referencing compute
            "b.c": """\
                #include "header.h"
                int compute(int x);
                struct vtable_t vtable = { .fn = compute };
                """,
        },
    )


@pytest.mark.parametrize("order", permutations(["a.c", "b.c"]), ids="-".join)
def test_static_inline_in_scc_emitted_before_caller(
    scc_static_inline_tus: list[Path], order: tuple[str, ...]
):
    a_c, b_c = scc_static_inline_tus
    base = a_c.parent
    by_name = {tu.name: tu for tu in scc_static_inline_tus}

    compile_commands = _write_compile_commands(
        base, [by_name[n] for n in order], extra_flags=f"-I{base}"
    )
    consolidated = consolidate_init(
        compile_commands, source_priority=[a_c.resolve(), b_c.resolve()]
    )

    text = str(consolidated)
    assert "not_renamed" in text and "compute" in text
    assert "a_compute" not in text and "b_compute" not in text, consolidated
    assert "header_not_renamed" not in text, consolidated
    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code fails "
        f"(static inline in SCC emitted after caller due to TU rank):\n"
        f"{error}\n\nConsolidated output:\n{consolidated}"
    )


def test_system_macro_double_expansion(tmp_path: Path):
    main_c = tmp_path / "main.c"
    main_c.write_text(
        dedent(
            """\
            #include <signal.h>

            void not_renamed(void) {
                struct sigaction ign_handler;
                ign_handler.sa_handler = SIG_IGN;
            }
            """
        )
    )

    # Fully compile the TU to an object file (resolves <signal.h>).
    ok, out, err, _ = run_subprocess(
        ["clang-21", "-Wall", "-Werror", "-c", str(main_c), "-o", str(tmp_path / "main.o")]
    )
    assert ok, f"main.c failed to compile:\n{out}{err}"

    compile_commands = _write_compile_commands(tmp_path, [main_c])
    consolidated = consolidate_init(compile_commands, source_priority=[])

    text = str(consolidated)
    assert "not_renamed" in text
    assert "main_not_renamed" not in text
    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile (system macro double expansion):\n{error}\n\n"
        f"Consolidated output:\n{consolidated}"
    )


def test_cc_defines_preserved_in_consolidation(tmp_path: Path):
    main_c = tmp_path / "main.c"
    main_c.write_text(
        dedent(
            """\
            #include <pcre2.h>
            #include <unistd.h>
            #include <stdlib.h>
            #include <stddef.h>
            #include <fcntl.h>

            int not_renamed(void) {
                int count = 0;
                char **kv;
                for (kv = environ; *kv; kv++)
                    count++;
                return count;
            }

            int check_access(const char *path) {
                return euidaccess(path, R_OK);
            }

            int make_pipe(int fd[2]) {
                return pipe2(fd, O_CLOEXEC);
            }

            static int cmp_with_ctx(const void *a, const void *b, void *ctx) {
                int offset = *(int *)ctx;
                return (*(const int *)a + offset) - (*(const int *)b + offset);
            }

            void sort_with_context(int *arr, size_t n, int offset) {
                qsort_r(arr, n, sizeof(int), cmp_with_ctx, &offset);
            }

            const char *get_safe_env(const char *name) {
                return secure_getenv(name);
            }
            """
        )
    )

    # Fully compile the TU to an object file (needs the same -D feature macros).
    ok, out, err, _ = run_subprocess(
        [
            "clang-21",
            "-Wall",
            "-Werror",
            "-D_GNU_SOURCE",
            "-DPCRE2_CODE_UNIT_WIDTH=8",
            "-c",
            str(main_c),
            "-o",
            str(tmp_path / "main.o"),
        ]
    )
    assert ok, f"main.c failed to compile:\n{out}{err}"

    compile_commands = _write_compile_commands(
        tmp_path, [main_c], extra_flags="-D_GNU_SOURCE -DPCRE2_CODE_UNIT_WIDTH=8"
    )
    consolidated = consolidate_init(compile_commands, source_priority=[])

    for sym in ("environ", "euidaccess", "pipe2", "qsort_r", "secure_getenv"):
        assert sym in str(consolidated), (
            f"Consolidated output is missing '{sym}' usage:\n{consolidated}"
        )

    text = str(consolidated)
    assert "not_renamed" in text
    assert "main_not_renamed" not in text and "main_sort_with_context" not in text

    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile without -D_GNU_SOURCE and -DPCRE2_CODE_UNIT_WIDTH=8 "
        f"(macros lost during consolidation):\n"
        f"{error}\n\nConsolidated output:\n{consolidated}"
    )


def test_posix_c_source_preserved_in_consolidation(tmp_path: Path):
    main_c = tmp_path / "main.c"
    main_c.write_text(
        dedent(
            """\
            #include <time.h>
            #include <string.h>
            #include <stdlib.h>

            long get_monotonic_ns(void) {
                struct timespec ts;
                clock_gettime(CLOCK_MONOTONIC, &ts);
                return ts.tv_sec * 1000000000L + ts.tv_nsec;
            }

            char *not_renamed(const char *s) {
                return strdup(s);
            }

            char *next_token(char *str, char **saveptr) {
                return strtok_r(str, ":", saveptr);
            }
            """
        )
    )

    # Fully compile the TU to an object file (needs the same -std/-D feature macros).
    ok, out, err, _ = run_subprocess(
        [
            "clang-21",
            "-Wall",
            "-Werror",
            "-std=c11",
            "-D_POSIX_C_SOURCE=200809L",
            "-c",
            str(main_c),
            "-o",
            str(tmp_path / "main.o"),
        ]
    )
    assert ok, f"main.c failed to compile:\n{out}{err}"

    compile_commands = _write_compile_commands(
        tmp_path, [main_c], extra_flags="-std=c11 -D_POSIX_C_SOURCE=200809L"
    )
    consolidated = consolidate_init(compile_commands, source_priority=[])

    # All _POSIX_C_SOURCE-gated symbols must appear in the consolidated output
    for sym in ("clock_gettime", "strdup", "strtok_r"):
        assert sym in str(consolidated), (
            f"Consolidated output is missing '{sym}' usage:\n{consolidated}"
        )

    text = str(consolidated)
    assert "not_renamed" in text and "next_token" in text
    assert "main_not_renamed" not in text and "main_next_token" not in text

    success, error = compile_c(consolidated, flags=["-std=c11", "-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile with -std=c11 without _POSIX_C_SOURCE "
        f"(clock_gettime/strdup/strtok_r undeclared — feature-test macro lost):\n"
        f"{error}\n\nConsolidated output:\n{consolidated}"
    )


def test_system_macro_undefs_preserve_benign_macros(tmp_path: Path):
    main_c = tmp_path / "main.c"
    main_c.write_text(
        dedent(
            """\
            #include <stdio.h>
            #include <stdlib.h>
            #include <math.h>
            #include <float.h>
            #include <stdbool.h>
            #include <stdint.h>
            #include <limits.h>

            int not_renamed(void) {
                char *p = NULL;
                if (p == NULL)
                    return EXIT_FAILURE;
                int c = fgetc(stdin);
                if (c == EOF)
                    return EXIT_FAILURE;
                if (fseek(stdin, 0, SEEK_SET) != 0)
                    return EXIT_FAILURE;
                return EXIT_SUCCESS;
            }

            double compute(double x) {
                if (x > DBL_MAX)
                    return HUGE_VAL;
                if (x != x)
                    return NAN;
                return x;
            }

            bool check(int x) {
                if (x > INT_MAX / 2)
                    return false;
                if (x < INT_MIN / 2)
                    return false;
                return true;
            }
            """
        )
    )

    # Fully compile the TU to an object file (resolves the C stdlib headers).
    ok, out, err, _ = run_subprocess(
        ["clang-21", "-Wall", "-Werror", "-c", str(main_c), "-o", str(tmp_path / "main.o")]
    )
    assert ok, f"main.c failed to compile:\n{out}{err}"

    compile_commands = _write_compile_commands(tmp_path, [main_c])
    consolidated = consolidate_init(compile_commands, source_priority=[])

    text = str(consolidated)
    assert "not_renamed" in text and "compute" in text and "check" in text
    assert "main_not_renamed" not in text and "main_compute" not in text

    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile (benign macros broken):\n{error}\n\n"
        f"Consolidated output:\n{consolidated}"
    )


def test_source_level_defines_required_by_system_headers_preserved(tmp_path: Path):
    """
    Source-level #defines that affect system header behavior must be preserved
    in consolidated output.  Two sub-cases:
      1. PCRE2_CODE_UNIT_WIDTH: pcre2.h fires #error if missing.
      2. _GNU_SOURCE: changes strerror_r signature from int (POSIX) to char* (GNU).
    """
    main_c = tmp_path / "main.c"
    main_c.write_text(
        dedent(
            """\
            #define _GNU_SOURCE
            #define PCRE2_CODE_UNIT_WIDTH 8
            #include <pcre2.h>
            #include <string.h>

            int not_renamed(const char *pat) {
                (void)pat;
                return 0;
            }

            const char *get_err(int errnum) {
                static char buf[256];
                const char *errstr = strerror_r(errnum, buf, sizeof(buf));
                return errstr;
            }
            """
        )
    )

    # Fully compile the TU to an object file (in-source #defines gate <pcre2.h>).
    ok, out, err, _ = run_subprocess(
        ["clang-21", "-Wall", "-Werror", "-c", str(main_c), "-o", str(tmp_path / "main.o")]
    )
    assert ok, f"main.c failed to compile:\n{out}{err}"

    compile_commands = _write_compile_commands(tmp_path, [main_c])
    consolidated = consolidate_init(compile_commands, source_priority=[])

    text = str(consolidated)
    assert "not_renamed" in text and "get_err" in text
    assert "main_not_renamed" not in text and "main_get_err" not in text

    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile (source-level defines dropped):\n{error}\n\n"
        f"Consolidated output:\n{consolidated}"
    )


@pytest.fixture(scope="module")
def shadowing_local_tus(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    return _build_tus(
        tmp_path_factory.mktemp("shadowing_local"),
        {
            "a.c": """\
                static int renamed(int x) { return x * 2; }

                int use_a(int val) {
                    int a_renamed = val + 1;
                    return renamed(a_renamed);
                }
                """,
            "b.c": """\
                static int renamed(int x) { return x + 1; }

                int use_b(void) { return renamed(42); }
                """,
        },
    )


@pytest.mark.parametrize("order", permutations(["a.c", "b.c"]), ids="-".join)
def test_renamed_symbol_shadowed_by_local_variable(
    shadowing_local_tus: list[Path], order: tuple[str, ...]
):
    """
    rename_conflicting_symbols_ builds candidate names from the TU stem plus the
    original spelling (e.g. "a_renamed" for "renamed" in "a.c"), but only checks
    used_spellings against top-level symbol names.  Local variable names inside
    function bodies are never added to used_spellings, so the generated name can
    collide with a local variable in the same TU.

    Scenario:
      a.c – static renamed(int x) + use_a() which declares  int a_renamed = ...
            and calls renamed(a_renamed).
      b.c – conflicting static renamed(int x).

    After renaming, a.c's "renamed" → "a_renamed".  The call site becomes:
        return a_renamed(a_renamed);
    where the first "a_renamed" now resolves to the local int, not the function,
    producing: "called object type 'int' is not a function or function pointer".
    """
    a_c, b_c = shadowing_local_tus
    by_name = {tu.name: tu for tu in shadowing_local_tus}

    compile_commands = _write_compile_commands(a_c.parent, [by_name[n] for n in order])
    consolidated = consolidate_init(
        compile_commands, source_priority=[a_c.resolve(), b_c.resolve()]
    )

    text = str(consolidated)
    assert "b_renamed" in text, consolidated
    assert "_a_renamed" in text, consolidated

    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile (renamed symbol shadowed by local variable):\n"
        f"{error}\n\nConsolidated output:\n{consolidated}"
    )


@pytest.fixture(scope="module")
def shared_header_rename_tus(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    return _build_tus(
        tmp_path_factory.mktemp("shared_header_rename"),
        {
            "header.h": """\
                typedef struct renamed {
                    int a;
                } renamed;
                """,
            "a.c": """\
                #include "header.h"

                static void local_push(void) {
                    renamed *spec;
                    (void)spec;
                }

                int call_local_push(void) { local_push(); return 0; }
                """,
            "b.c": """\
                #include "header.h"

                static void push_spec_rref_cmp(void) {
                    const renamed *push_spec_a;
                    (void)push_spec_a;
                }

                int call_rref_cmp(void) { push_spec_rref_cmp(); return 0; }
                """,
            "c.c": """\
                static void renamed(void) { return; }

                int use_push(void) { renamed(); return 0; }
                """,
        },
    )


@pytest.mark.parametrize("order", permutations(["a.c", "b.c", "c.c"]), ids="-".join)
def test_rename_conflict_in_shared_header_across_tus(
    shared_header_rename_tus: list[Path], order: tuple[str, ...]
):
    """
    When a typedef in a shared header conflicts with a same-named symbol in
    another TU, _apply_renames renames the typedef and updates header.h in
    modified_sources while processing the first TU that includes it.  Parsing a
    subsequent TU that includes the same header then fails: the header no longer
    defines the original spelling, but the TU's own source still references it,
    causing a TranslationUnitLoadError.

    Scenario:
      header.h – typedef struct renamed { int a; } renamed;
      a.c      – #include "header.h", uses renamed
      b.c      – #include "header.h", uses renamed (also has a local named push_spec_a)
      c.c      – static void renamed(void) { ... }  ← conflicts with the typedef

    c.c's renamed function forces the typedef to be renamed.  The rename for
    a.c modifies header.h in modified_sources.  When b.c is subsequently parsed
    with that modified header, b.c's references to 'renamed' become
    unresolved and the parse fails.
    """
    a_c, b_c, c_c = shared_header_rename_tus
    base = a_c.parent
    by_name = {tu.name: tu for tu in shared_header_rename_tus}

    compile_commands = _write_compile_commands(
        base, [by_name[n] for n in order], extra_flags=f"-I{base}"
    )
    consolidated = consolidate_init(
        compile_commands,
        source_priority=[a_c.resolve(), b_c.resolve(), c_c.resolve()],
    )

    text = str(consolidated)
    assert "header_renamed" in text and "c_renamed" in text, consolidated

    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile "
        f"(rename of shared header breaks subsequent TU parse):\n"
        f"{error}\n\nConsolidated output:\n{consolidated}"
    )


@pytest.fixture(scope="module")
def forward_typedef_tus(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    return _build_tus(
        tmp_path_factory.mktemp("forward_typedef"),
        {
            "common.h": """\
                typedef struct not_renamed not_renamed;

                struct Bar {
                    not_renamed *hideset;
                };
                """,
            "a.c": """\
                #include "common.h"

                int use_a(not_renamed *f) {
                    (void)f;
                    return 0;
                }
                """,
            "b.c": """\
                #include "common.h"

                typedef struct not_renamed not_renamed;
                struct not_renamed {
                    not_renamed *next;
                    char *name;
                };

                int use_b(void) {
                    not_renamed *h = 0;
                    (void)h;
                    return 0;
                }
                """,
        },
    )


@pytest.mark.parametrize("order", permutations(["a.c", "b.c"]), ids="-".join)
def test_forward_typedef_completed_in_one_tu_rename_conflict(
    forward_typedef_tus: list[Path], order: tuple[str, ...]
):
    """
    A forward typedef declared in a shared header but completed (full struct
    definition) in exactly one TU triggers conflicting rename edits on the
    header token.

    Scenario:
      common.h – typedef struct not_renamed not_renamed;   (forward decl only)
                 struct Bar { not_renamed *hideset; };
      a.c      – #include "common.h", global fn referencing not_renamed
      b.c      – #include "common.h", re-declares `typedef struct not_renamed not_renamed;`
                 AND completes `struct not_renamed { ... };`, global fn referencing not_renamed

    Because a same-spelled definition of the `not_renamed` typedef exists in more than
    one presumed path, consolidation renames it.  The new spelling is derived
    from each representative symbol's presumed_path in _get_conflicting_symbols:
    in a.c the representative resolves to common.h -> `common_not_renamed`, while in b.c
    (where the completing struct lives) it resolves to b.c -> `b_not_renamed`.  Both map
    the same USR `c:common.h@T@Foo`, so _apply_renames emits two different
    replacements for the same header token and raises
    `ValueError: Conflicting rename edits ...`.
    """
    a_c, b_c = forward_typedef_tus
    base = a_c.parent
    by_name = {tu.name: tu for tu in forward_typedef_tus}

    compile_commands = _write_compile_commands(
        base, [by_name[n] for n in order], extra_flags=f"-I{base}"
    )
    consolidated = consolidate_init(
        compile_commands,
        source_priority=[a_c.resolve(), b_c.resolve()],
    )

    text = str(consolidated)
    assert "not_renamed" in text
    assert "common_not_renamed" not in text and "b_not_renamed" not in text, consolidated

    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile "
        f"(forward typedef completed in one TU yields conflicting header rename):\n"
        f"{error}\n\nConsolidated output:\n{consolidated}"
    )


@pytest.fixture(scope="module")
def odr_struct_tus(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    # Each TU compiles fine in isolation — the ODR violation only surfaces when
    # the two definitions are combined into a single translation unit.
    return _build_tus(
        tmp_path_factory.mktemp("odr_struct"),
        {
            "common.h": """\
                struct token;
                """,
            "a.c": """\
                #include "common.h"

                struct token { int type; int line; };

                int token_line(struct token *t) {
                    return t->line;
                }
                """,
            "b.c": """\
                #include "common.h"

                struct token { int type; char *text; };

                const char *token_text(struct token *t) {
                    return t->text;
                }
                """,
        },
    )


@pytest.mark.xfail(reason="UB due to conflicting struct definitions in different TUs")
@pytest.mark.parametrize("order", permutations(["a.c", "b.c"]), ids="-".join)
def test_struct_forward_decl_completed_differently_in_two_tus(
    odr_struct_tus: list[Path], order: tuple[str, ...]
):
    """
    A struct forward-declared in a shared header but completed with *different*
    field layouts in two TUs is an ODR violation.  Both completions may share
    the same USR (from the canonical forward-declaration location in common.h),
    so the USR-identity check in _get_conflicting_symbols must not skip renaming
    here — the code bodies differ, so this is a genuine conflict.

    Scenario:
      common.h – struct token;   (forward declaration only)
      a.c      – #include "common.h", completes struct token { int type; int line; };
                 and a function that only uses it locally
      b.c      – #include "common.h", completes struct token { int type; char *text; };
                 and a function that only uses it locally

    Expected: the two incompatible definitions are renamed (a_token / b_token)
    and the consolidated output compiles without errors.
    """
    a_c, b_c = odr_struct_tus
    base = a_c.parent
    by_name = {tu.name: tu for tu in odr_struct_tus}

    compile_commands = _write_compile_commands(
        base, [by_name[n] for n in order], extra_flags=f"-I{base}"
    )
    consolidated = consolidate_init(
        compile_commands,
        source_priority=[a_c.resolve(), b_c.resolve()],
    )

    text = str(consolidated)
    # The two incompatible struct definitions must be renamed so they can coexist,
    # and all usages within each TU must be updated consistently.
    assert "struct a_token {\n    int type;\n    int line;\n};\n" in text
    assert "int token_line(struct a_token *t) {\n    return t->line;\n}\n" in text
    assert "struct b_token {\n    int type;\n    char *text;\n};\n" in text
    assert "const char *token_text(struct b_token *t) {\n    return t->text;\n}\n" in text

    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile:\n{error}\n\nConsolidated output:\n{consolidated}"
    )


@pytest.fixture(scope="module")
def odr_typedef_struct_tus(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    # Each TU compiles fine in isolation — the ODR violation only surfaces when
    # the two definitions are combined into a single translation unit.
    return _build_tus(
        tmp_path_factory.mktemp("odr_typedef_struct"),
        {
            "common.h": """\
                typedef struct token token;
                """,
            "a.c": """\
                #include "common.h"

                struct token { int type; int line; };

                int token_line(token *t) {
                    return t->line;
                }
                """,
            "b.c": """\
                #include "common.h"

                struct token { int type; char *text; };

                const char *token_text(token *t) {
                    return t->text;
                }
                """,
        },
    )


@pytest.mark.xfail(
    reason="Shared typedef bound to a struct with conflicting completions cannot "
    "be renamed to two spellings, leaving 'token' as an incomplete type."
)
@pytest.mark.parametrize("order", permutations(["a.c", "b.c"]), ids="-".join)
def test_typedef_struct_forward_decl_completed_differently_in_two_tus(
    odr_typedef_struct_tus: list[Path], order: tuple[str, ...]
):
    """
    Same ODR-violation scenario as
    test_struct_forward_decl_completed_differently_in_two_tus, but the struct is
    reached through a typedef declared in the shared header:

      common.h – typedef struct token token;   (forward typedef)
      a.c      – #include "common.h", completes struct token { int type; int line; };
                 and a function that refers to the type as the bare typedef `token`
      b.c      – #include "common.h", completes struct token { int type; char *text; };
                 and a function that refers to the type as the bare typedef `token`

    Renaming the two incompatible struct completions apart (a_token / b_token)
    would require the shared `typedef struct token token;` in common.h to become
    two different typedefs as well.  Because that single token span can only carry
    one spelling, the shared edit is dropped, leaving `token` pointing at an
    (now undefined) `struct token` — so dereferencing `t->line` no longer compiles.
    """
    a_c, b_c = odr_typedef_struct_tus
    base = a_c.parent
    by_name = {tu.name: tu for tu in odr_typedef_struct_tus}

    compile_commands = _write_compile_commands(
        base, [by_name[n] for n in order], extra_flags=f"-I{base}"
    )
    consolidated = consolidate_init(
        compile_commands,
        source_priority=[a_c.resolve(), b_c.resolve()],
    )

    text = str(consolidated)
    # Desired behavior: the typedef is split alongside the struct so each TU keeps
    # a complete type under its own spelling.
    assert "struct a_token {\n    int type;\n    int line;\n};\n" in text
    assert "int token_line(a_token *t) {\n    return t->line;\n}\n" in text
    assert "struct b_token {\n    int type;\n    char *text;\n};\n" in text
    assert "const char *token_text(b_token *t) {\n    return t->text;\n}\n" in text

    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile:\n{error}\n\nConsolidated output:\n{consolidated}"
    )


@pytest.fixture(scope="module")
def genuine_conflict_tus(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    return _build_tus(
        tmp_path_factory.mktemp("genuine_conflict"),
        {
            "common.h": """\
                typedef struct renamed renamed;

                struct holder {
                    renamed *p;
                };
                """,
            "a.c": """\
                #include "common.h"

                int use_a(renamed *f) {
                    (void)f;
                    return 0;
                }
                """,
            "b.c": """\
                #include "common.h"

                typedef struct renamed renamed;
                struct renamed {
                    renamed *next;
                    int x;
                };

                int use_b(void) {
                    renamed *h = 0;
                    (void)h;
                    return 0;
                }
                """,
            "c.c": """\
                static int renamed(void) {
                    return 0;
                }

                int use_c(void) { return renamed(); }
                """,
        },
    )


@pytest.mark.xfail(reason="Known limitation")
@pytest.mark.parametrize("order", permutations(["a.c", "b.c", "c.c"]), ids="-".join)
def test_shared_header_entity_inside_genuine_conflict_gets_single_spelling(
    genuine_conflict_tus: list[Path], order: tuple[str, ...]
):
    """
    A shared-header entity whose presumed path diverges across TUs must receive
    ONE canonical new spelling even when it is caught up in a *genuine* naming
    conflict with an unrelated same-spelled symbol.

    Scenario:
      common.h – typedef struct renamed renamed;  struct holder { renamed *p; };
      a.c      – #include "common.h", uses renamed         (USR-A, presumed common.h)
      b.c      – #include "common.h", REDECLARES+completes renamed, uses renamed
                                                          (USR-A, presumed b.c)
      c.c      – static int renamed(void) { ... }          (USR-B, a genuine clash)

    c.c's `renamed` supplies the second distinct USR so the group is a real conflict;
    the shared typedef (USR-A) must still be renamed consistently for both a.c
    and b.c so _apply_renames does not raise on the common.h token.

    The spelling must also come from the header that declares the entity, not from
    whichever TU leads compile_commands.json: a first-seen-wins rule would yield
    `b_renamed` for every order in which b.c precedes a.c.
    """
    base = genuine_conflict_tus[0].parent
    by_name = {tu.name: tu for tu in genuine_conflict_tus}
    ordered = [by_name[n] for n in order]

    compile_commands = _write_compile_commands(base, ordered, extra_flags=f"-I{base}")
    consolidated = consolidate_init(
        compile_commands, source_priority=[tu.resolve() for tu in ordered]
    )

    text = str(consolidated)
    assert "common_renamed" in text and "c_renamed" in text, consolidated
    assert "a_renamed" not in text and "b_renamed" not in text, consolidated

    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile "
        f"(shared-header entity in a genuine conflict got inconsistent renames):\n"
        f"{error}\n\nConsolidated output:\n{consolidated}"
    )


@pytest.fixture(scope="module")
def cv_qualified_alias_tus(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    # Each TU compiles fine in isolation — the ODR violation only surfaces when
    # the two definitions are combined into a single translation unit.
    return _build_tus(
        tmp_path_factory.mktemp("cv_qualified_alias"),
        {
            "common.h": """\
                typedef const struct token const_token;
                typedef volatile struct token volatile_token;
                """,
            "a.c": """\
                #include "common.h"

                struct token { int type; int line; };

                int token_line(const_token *t) {
                    return t->line;
                }

                int token_tick(volatile_token *t) {
                    return t->type;
                }
                """,
            "b.c": """\
                #include "common.h"

                struct token { int type; char *text; };

                const char *token_text(const_token *t) {
                    return t->text;
                }

                int token_kind(volatile_token *t) {
                    return t->type;
                }
                """,
        },
    )


@pytest.mark.xfail(
    reason="A cv-qualified typedef is a type of its own, so it cannot adopt the split "
    "tag's spelling and is rejected rather than silently losing its qualifier.",
)
@pytest.mark.parametrize("order", permutations(["a.c", "b.c"]), ids="-".join)
def test_cv_qualified_typedef_completed_differently_in_two_tus(
    cv_qualified_alias_tus: list[Path], order: tuple[str, ...]
):
    """
    Same ODR-violation scenario as
    test_typedef_struct_forward_decl_completed_differently_in_two_tus, but each typedef
    applies a cv-qualifier on top of the tag:

      common.h – typedef const struct token const_token;
                 typedef volatile struct token volatile_token;
      a.c      – #include "common.h", completes struct token { int type; int line; };
      b.c      – #include "common.h", completes struct token { int type; char *text; };

    Each typedef still names the tag *directly*, so this isolates qualifiers from any
    question of typedef chains.

    A qualifier belongs to the alias, not to the tag, so splitting the tag apart must
    carry it across.  Note that a lost qualifier would still compile — dropping `const`
    only widens the type — so the qualifiers are asserted on the emitted text rather
    than left for the compiler to catch.
    """
    a_c, b_c = cv_qualified_alias_tus
    base = a_c.parent
    by_name = {tu.name: tu for tu in cv_qualified_alias_tus}

    compile_commands = _write_compile_commands(
        base, [by_name[n] for n in order], extra_flags=f"-I{base}"
    )
    consolidated = consolidate_init(
        compile_commands,
        source_priority=[a_c.resolve(), b_c.resolve()],
    )

    text = str(consolidated)
    # Desired behavior: each typedef is split alongside the struct, keeping its qualifier
    # and a spelling of its own.
    assert "struct a_token {\n    int type;\n    int line;\n};\n" in text
    assert "struct b_token {\n    int type;\n    char *text;\n};\n" in text
    assert "typedef const struct a_token a_const_token;" in text
    assert "typedef volatile struct a_token a_volatile_token;" in text
    assert "typedef const struct b_token b_const_token;" in text
    assert "typedef volatile struct b_token b_volatile_token;" in text

    # Each use keeps the alias it was written with, qualifier intact.
    assert "int token_line(a_const_token *t) {\n    return t->line;\n}\n" in text
    assert "int token_tick(a_volatile_token *t) {\n    return t->type;\n}\n" in text
    assert "const char *token_text(b_const_token *t) {\n    return t->text;\n}\n" in text
    assert "int token_kind(b_volatile_token *t) {\n    return t->type;\n}\n" in text

    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile:\n{error}\n\nConsolidated output:\n{consolidated}"
    )


@pytest.fixture(scope="module")
def chained_alias_tus(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    # Each TU compiles fine in isolation — the ODR violation only surfaces when
    # the two definitions are combined into a single translation unit.
    return _build_tus(
        tmp_path_factory.mktemp("chained_alias"),
        {
            "common.h": """\
                typedef struct token token;
                typedef token token_ref;
                """,
            "a.c": """\
                #include "common.h"

                struct token { int type; int line; };

                int token_line(token_ref *t) {
                    return t->line;
                }
                """,
            "b.c": """\
                #include "common.h"

                struct token { int type; char *text; };

                const char *token_text(token_ref *t) {
                    return t->text;
                }
                """,
        },
    )


@pytest.mark.xfail(
    reason="A typedef of a typedef must be rebuilt on the link it names rather than "
    "collapsed onto the tag, so it is rejected instead of flattening the chain.",
)
@pytest.mark.parametrize("order", permutations(["a.c", "b.c"]), ids="-".join)
def test_chained_typedef_completed_differently_in_two_tus(
    chained_alias_tus: list[Path], order: tuple[str, ...]
):
    """
    Same ODR-violation scenario as
    test_typedef_struct_forward_decl_completed_differently_in_two_tus, but the type is
    reached through a chain of typedefs:

      common.h – typedef struct token token;
                 typedef token token_ref;
      a.c      – #include "common.h", completes struct token { int type; int line; };
      b.c      – #include "common.h", completes struct token { int type; char *text; };

    Neither typedef is qualified, so this isolates chains from any question of
    cv-qualifiers.

    `token_ref` names `token`, not the tag, so splitting the tag apart must rebuild it on
    whatever `token` became.  Re-expressing it against the tag instead would flatten the
    chain into a second alias of the tag — which still compiles, so the indirection is
    asserted on the emitted text rather than left for the compiler to catch.
    """
    a_c, b_c = chained_alias_tus
    base = a_c.parent
    by_name = {tu.name: tu for tu in chained_alias_tus}

    compile_commands = _write_compile_commands(
        base, [by_name[n] for n in order], extra_flags=f"-I{base}"
    )
    consolidated = consolidate_init(
        compile_commands,
        source_priority=[a_c.resolve(), b_c.resolve()],
    )

    text = str(consolidated)
    # Desired behavior: the whole chain is split, each link still naming the one before it.
    assert "struct a_token {\n    int type;\n    int line;\n};\n" in text
    assert "struct b_token {\n    int type;\n    char *text;\n};\n" in text
    assert "typedef a_token a_token_ref;" in text
    assert "typedef b_token b_token_ref;" in text

    # Each use keeps the link it was written with rather than the tag it resolves to.
    assert "int token_line(a_token_ref *t) {\n    return t->line;\n}\n" in text
    assert "const char *token_text(b_token_ref *t) {\n    return t->text;\n}\n" in text

    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile:\n{error}\n\nConsolidated output:\n{consolidated}"
    )


@pytest.fixture(scope="module")
def header_gnu_source_tus(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    return _build_tus(
        tmp_path_factory.mktemp("header_gnu_source"),
        {
            "config.h": """\
                #define _GNU_SOURCE
                """,
            "a.c": """\
                #include "config.h"
                #include <string.h>

                const char *not_renamed(int errnum) {
                    static char buf[256];
                    const char *errstr = strerror_r(errnum, buf, sizeof(buf));
                    return errstr;
                }
                """,
            "b.c": """\
                #include "config.h"
                #include <string.h>

                const char *not_renamed2(int errnum) {
                    static char buf[128];
                    const char *errstr = strerror_r(errnum, buf, sizeof(buf));
                    return errstr;
                }
                """,
        },
        cflags=["-D_DEFAULT_SOURCE"],
    )


@pytest.mark.parametrize("order", permutations(["a.c", "b.c"]), ids="-".join)
def test_header_defined_gnu_source_with_default_source_flag(
    header_gnu_source_tus: list[Path], order: tuple[str, ...]
):
    """
    Compile commands have -D_DEFAULT_SOURCE, but _GNU_SOURCE is defined inside
    a project header (config.h/first.h).  The consolidator preserves
    _DEFAULT_SOURCE (from -D flags) but loses _GNU_SOURCE (from the header).
    Code using the GNU strerror_r (returns char*) then fails with:
      error: incompatible integer to pointer conversion

    Uses two TUs to exercise the union: both include config.h which defines
    _GNU_SOURCE, so the union should emit it exactly once.
    """
    base = header_gnu_source_tus[0].parent
    by_name = {tu.name: tu for tu in header_gnu_source_tus}

    compile_commands = _write_compile_commands(
        base, [by_name[n] for n in order], extra_flags=f"-D_DEFAULT_SOURCE -I{base}"
    )
    consolidated = consolidate_init(compile_commands, source_priority=[])

    text = str(consolidated)
    assert "not_renamed" in text and "not_renamed2" in text
    assert "a_not_renamed" not in text and "b_not_renamed2" not in text, consolidated

    success, error = compile_c(consolidated, flags=["-Wall", "-Werror"])
    assert success, (
        f"Consolidated code does not compile (header-defined _GNU_SOURCE lost):\n{error}\n\n"
        f"Consolidated output:\n{consolidated}"
    )
