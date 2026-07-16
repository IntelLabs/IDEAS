#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from pathlib import Path
from textwrap import dedent

import json

from ideas import ast
from ideas.init.consolidate import (
    create_ast_order,
    create_symbol_lexical_key_fn,
    init as consolidate_init,
)
from ideas.tools import check_c


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
            typedef struct X X;
            """
        )
    )
    thing_h.write_text(
        dedent(
            """\
            #include "types.h"

            struct X {
                int (*notify)(X *self, int status);
                void *payload;
            };

            int X_init(X *out);
            """
        )
    )
    thing_c.write_text(
        dedent(
            """\
            #include "thing.h"

            int X_init(X *out) {
                out->notify = 0;
                out->payload = 0;
                return 0;
            }
            """
        )
    )

    # Parse the C file (which transitively includes types.h via thing.h)
    compile_commands = _write_compile_commands(tmp_path, [thing_c])
    consolidated = consolidate_init(compile_commands, source_priority=[])

    success, error = check_c(consolidated, flags=["-fsyntax-only", "-Wall"])
    assert success, (
        f"Consolidated code does not compile:\n{error}\n\nConsolidated output:\n{consolidated}"
    )


def test_consolidation_typedef_before_struct_cross_tu(tmp_path: Path):
    types_h = tmp_path / "types.h"
    node_h = tmp_path / "node.h"
    api_c = tmp_path / "api.c"
    internal_c = tmp_path / "internal.c"

    types_h.write_text(
        dedent(
            """\
            typedef struct Node Node;
            """
        )
    )
    node_h.write_text(
        dedent(
            """\
            struct Node {
                int val;
                struct Node *next;
            };
            """
        )
    )
    api_c.write_text(
        dedent(
            """\
            #include "types.h"
            #include "node.h"

            Node *node_create(int val) {
                (void)val;
                return (Node *)0;
            }
            """
        )
    )
    internal_c.write_text(
        dedent(
            """\
            #include "node.h"

            int node_get_val(struct Node *n) {
                return n->val;
            }
            """
        )
    )

    # Parse both TUs — after merge_symbols, the struct may retain its cursor from
    # one TU and the typedef from another, making cross-TU location comparison undefined.
    compile_commands = _write_compile_commands(tmp_path, [internal_c, api_c])
    consolidated = consolidate_init(
        compile_commands, source_priority=[internal_c.resolve(), api_c.resolve()]
    )

    # The typedef must appear before usages of 'Node' as a bare type name.
    success, error = check_c(consolidated, flags=["-fsyntax-only", "-Wall"])
    assert success, (
        f"Consolidated code does not compile:\n{error}\n\nConsolidated output:\n{consolidated}"
    )


def test_consolidation_mutual_cross_tu_typedefs(tmp_path: Path):
    """
    Mutual cross-references create a cycle that merges symbols from different TUs
    into one SCC:
      - a_types.h: typedef struct A A;
      - b_types.h: typedef struct B B;
      - a.c: includes both, defines struct A { B *ref; }; + function using A
      - b.c: includes both, defines struct B { A *ref; }; + function using B

    After merge: struct A (from a.c) → typedef B → struct B (from b.c) → typedef A → struct A
    All 4 in one SCC with cross-TU cursors. clang_isBeforeInTranslationUnit is
    undefined across TUs, so the comparator must still produce compilable output.
    """
    a_types_h = tmp_path / "a_types.h"
    b_types_h = tmp_path / "b_types.h"
    a_c = tmp_path / "a.c"
    b_c = tmp_path / "b.c"

    a_types_h.write_text(
        dedent(
            """\
            typedef struct A A;
            """
        )
    )
    b_types_h.write_text(
        dedent(
            """\
            typedef struct B B;
            """
        )
    )
    a_c.write_text(
        dedent(
            """\
            #include "a_types.h"
            #include "b_types.h"

            struct A {
                B *ref;
                int val;
            };

            A *create_a(void) {
                return (A *)0;
            }
            """
        )
    )
    b_c.write_text(
        dedent(
            """\
            #include "a_types.h"
            #include "b_types.h"

            struct B {
                A *ref;
                int val;
            };

            B *create_b(void) {
                return (B *)0;
            }
            """
        )
    )

    compile_commands = _write_compile_commands(tmp_path, [a_c, b_c])
    consolidated = consolidate_init(
        compile_commands, source_priority=[a_c.resolve(), b_c.resolve()]
    )

    # Both typedefs must appear before the struct definitions that reference them.
    success, error = check_c(consolidated, flags=["-fsyntax-only", "-Wall"])
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

            typedef struct my_object my_object;

            LIB_EXPORT(void) my_free(my_object *obj);
            LIB_EXPORT(int) my_get_value(my_object *obj);
            """
        )
    )

    # my_free calls my_get_value and vice versa => mutual recursion => SCC
    impl_c.write_text(
        dedent(
            """\
            #include "api.h"

            struct my_object {
                int value;
                int refcount;
            };

            void my_free(my_object *obj)
            {
                if (obj && my_get_value(obj) < 0) {
                    /* free */
                }
            }

            int my_get_value(my_object *obj)
            {
                my_free(obj);
                return obj->value;
            }
            """
        )
    )

    compile_commands = _write_compile_commands(tmp_path, [impl_c])
    consolidated = consolidate_init(compile_commands, source_priority=[])

    # The consolidated output must not contain the unexpanded macro
    assert "LIB_EXPORT" not in str(consolidated), (
        f"Consolidated output contains unexpanded macro 'LIB_EXPORT':\n{consolidated}"
    )

    success, error = check_c(consolidated, flags=["-fsyntax-only", "-Wall"])
    assert success, (
        f"Consolidated code does not compile:\n{error}\n\nConsolidated output:\n{consolidated}"
    )


def test_typedef_after_struct_cross_tu_three_tus(tmp_path: Path):
    """
      - types.h: typedef struct X X;   (forward-declares struct X via typedef)
      - TU1 (a.c): #include "types.h", defines struct X { X *self; int val; };
                    The struct body uses the typedef name 'X' → creates cycle:
                    struct X → typedef X → struct X
      - TU2 (b.c): #include "types.h" only, uses X* in a function signature
      - TU3 (c.c): #include "types.h", defines struct Y { X *member; }; and a
                    function returning Y*

    After merge_symbols with asts=[TU2, TU3, TU1]:
      - typedef X cursor retained from TU2 (first encounter, definition)
      - struct X cursor from TU1 (only TU with full definition)

    With ast_order=[a.c, b.c, c.c]:
      - struct X from a.c → rank 0
      - typedef X from b.c → rank 1

    They share an SCC (mutual dependency via X *self in struct body), so
    _merge_pure_type_declaration_sccs sorts them by symbol_lexical_key which
    uses ast_order ranks. struct X (rank 0) sorts before typedef X (rank 1).

    Result: consolidated output places struct X { X *self; ... } BEFORE
    typedef struct X X; → 'X' is unknown at that point → compilation failure.
    """
    types_h = tmp_path / "types.h"
    a_c = tmp_path / "a.c"
    b_c = tmp_path / "b.c"
    c_c = tmp_path / "c.c"

    types_h.write_text(
        dedent(
            """\
            typedef struct X X;
            """
        )
    )
    a_c.write_text(
        dedent(
            """\
            #include "types.h"

            struct X {
                X *self;
                int val;
            };

            X *create_x(int v) {
                (void)v;
                return (X *)0;
            }
            """
        )
    )
    b_c.write_text(
        dedent(
            """\
            #include "types.h"

            void consume_x(X *p) {
                (void)p;
            }
            """
        )
    )
    c_c.write_text(
        dedent(
            """\
            #include "types.h"

            struct Y {
                X *member;
                int id;
            };

            struct Y *alloc_y(void) {
                return (struct Y *)0;
            }
            """
        )
    )

    # Parse TUs — process b first so merge_symbols retains typedef X cursor from b.c
    # In init(), get_asts processes in compile_commands order, so list b first.
    # ast_order is derived from source_priority: a.c first so struct X gets rank 0.
    compile_commands = _write_compile_commands(tmp_path, [b_c, c_c, a_c])
    consolidated = consolidate_init(
        compile_commands, source_priority=[a_c.resolve(), b_c.resolve(), c_c.resolve()]
    )

    # The typedef MUST appear before the struct definition that uses 'X' as a
    # bare type name in its body. If the cross-TU lexical key comparison
    # incorrectly places struct X before typedef X, this will fail.
    success, error = check_c(consolidated, flags=["-fsyntax-only", "-Wall"])
    assert success, (
        f"Consolidated code does not compile:\n{error}\n\nConsolidated output:\n{consolidated}"
    )


def test_static_function_and_static_variable_same_name_renamed(tmp_path: Path):
    a_c = tmp_path / "a.c"
    b_c = tmp_path / "b.c"

    a_c.write_text(
        dedent(
            """\
            static int some(int x) { return x; }

            int use_a(void) { return some(42); }
            """
        )
    )
    b_c.write_text(
        dedent(
            """\
            static int some;

            int use_b(void) { return some; }
            """
        )
    )

    compile_commands = _write_compile_commands(tmp_path, [a_c, b_c])
    consolidated = consolidate_init(
        compile_commands, source_priority=[a_c.resolve(), b_c.resolve()]
    )

    success, error = check_c(consolidated, flags=["-fsyntax-only", "-Wall"])
    assert success, (
        f"Consolidated code does not compile (missing rename for static name collision):\n"
        f"{error}\n\nConsolidated output:\n{consolidated}"
    )


def test_static_variable_tentative_defs_same_name_renamed(tmp_path: Path):
    a_c = tmp_path / "a.c"
    b_c = tmp_path / "b.c"

    a_c.write_text(
        dedent(
            """\
            static int count;

            int get_a(void) { count += 2; return count; }
            """
        )
    )
    b_c.write_text(
        dedent(
            """\
            static char count;

            int get_b(void) { return (int)count + 1; }
            """
        )
    )

    compile_commands = _write_compile_commands(tmp_path, [a_c, b_c])
    consolidated = consolidate_init(
        compile_commands, source_priority=[a_c.resolve(), b_c.resolve()]
    )

    success, error = check_c(consolidated, flags=["-fsyntax-only", "-Wall"])
    assert success, (
        f"Consolidated code does not compile (missing rename for static variable collision):\n"
        f"{error}\n\nConsolidated output:\n{consolidated}"
    )


def test_isystem_inline_function_dependency_not_lost(tmp_path: Path):
    """
    When a header is included via -isystem in one TU but via -I in another,
    clang generates different USRs for the same static inline function
    (e.g. "c:@F@fn" vs "c:file.h@F@fn"). This causes the dependency edge
    from a caller in the -isystem TU to be silently dropped during the
    .subgraph(project_symbols.keys()) step, because the system-style USR
    doesn't match the non-system USR retained in project_symbols.

    This manifests as the inline function definition being placed AFTER
    its caller in the consolidated output, causing:
      error: call to undeclared function 'my_alloc'; ISO C99 and later do not
      support implicit function declarations
    """
    # alloc.h in util/ with a static inline function
    util_dir = tmp_path / "util"
    util_dir.mkdir()
    alloc_h = util_dir / "alloc.h"
    alloc_h.write_text(
        dedent(
            """\
            #include <stdlib.h>
            static inline void *my_alloc(size_t len) {
                return malloc(len);
            }
            """
        )
    )

    # bridge.h wraps my_alloc in a macro
    ext_dir = tmp_path / "ext"
    ext_dir.mkdir()
    bridge_h = ext_dir / "bridge.h"
    bridge_h.write_text(
        dedent(
            """\
            #include "alloc.h"
            #define ext_malloc(x) my_alloc(x)
            """
        )
    )

    # caller.c in ext/ - calls my_alloc via ext_malloc macro
    caller_c = ext_dir / "caller.c"
    caller_c.write_text(
        dedent(
            """\
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
            """
        )
    )

    # user.c in util/ - calls my_alloc directly
    user_c = util_dir / "user.c"
    user_c.write_text(
        dedent(
            """\
            #include "alloc.h"

            void *my_calloc(size_t n, size_t sz) {
                void *p = my_alloc(n * sz);
                return p;
            }
            """
        )
    )

    # Keep mixed include modes across TUs to exercise the USR normalization path.
    compile_commands = tmp_path / "compile_commands.json"
    compile_commands.write_text(
        json.dumps(
            [
                {
                    "directory": str(tmp_path),
                    "file": str(caller_c),
                    "command": f"cc -isystem {util_dir} -I{ext_dir} -c {caller_c}",
                },
                {
                    "directory": str(tmp_path),
                    "file": str(user_c),
                    "command": f"cc -I{util_dir} -c {user_c}",
                },
            ]
        )
    )

    consolidated = consolidate_init(
        compile_commands, source_priority=[caller_c.resolve(), user_c.resolve()]
    )
    success, error = check_c(consolidated, flags=["-fsyntax-only", "-Wall"])
    assert success, (
        f"Consolidated code does not compile (isystem USR mismatch lost dependency):\n"
        f"{error}\n\nConsolidated output:\n{consolidated}"
    )


def test_static_inline_in_scc_emitted_before_caller(tmp_path: Path):
    # header.h: static inline helper reads extern vtable
    header_h = tmp_path / "header.h"
    header_h.write_text(
        dedent(
            """\
            struct vtable_t { int (*fn)(int); };
            extern struct vtable_t vtable;
            static inline int helper(int x) {
                return vtable.fn(x);
            }
            """
        )
    )

    # caller.c (rank 0): defines compute() which calls helper()
    caller_c = tmp_path / "caller.c"
    caller_c.write_text(
        dedent(
            """\
            #include "header.h"
            int compute(int x) {
                return helper(x) + 1;
            }
            """
        )
    )

    # state.c (rank 1): includes header.h, defines vtable referencing compute
    state_c = tmp_path / "state.c"
    state_c.write_text(
        dedent(
            """\
            #include "header.h"
            int compute(int x);
            struct vtable_t vtable = { .fn = compute };
            """
        )
    )

    compile_commands = _write_compile_commands(
        tmp_path, [state_c, caller_c], extra_flags=f"-I{tmp_path}"
    )
    consolidated = consolidate_init(
        compile_commands, source_priority=[caller_c.resolve(), state_c.resolve()]
    )
    success, error = check_c(consolidated, flags=["-fsyntax-only", "-Wall"])
    assert success, (
        f"Consolidated code fails (static inline in SCC emitted after caller due to TU rank):\n"
        f"{error}\n\nConsolidated output:\n{consolidated}"
    )


def test_system_macro_double_expansion(tmp_path: Path):
    main_c = tmp_path / "main.c"
    main_c.write_text(
        dedent(
            """\
            #include <signal.h>

            void setup_signal(void) {
                struct sigaction ign_handler;
                ign_handler.sa_handler = SIG_IGN;
            }
            """
        )
    )

    compile_commands = _write_compile_commands(tmp_path, [main_c])
    consolidated = consolidate_init(compile_commands, source_priority=[])

    # The consolidated code must compile without double macro expansion
    success, error = check_c(consolidated, flags=["-fsyntax-only"])
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

            int count_env(void) {
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

    compile_commands = _write_compile_commands(
        tmp_path, [main_c], extra_flags="-D_GNU_SOURCE -DPCRE2_CODE_UNIT_WIDTH=8"
    )
    consolidated = consolidate_init(compile_commands, source_priority=[])

    for sym in ("environ", "euidaccess", "pipe2", "qsort_r", "secure_getenv"):
        assert sym in str(consolidated), (
            f"Consolidated output is missing '{sym}' usage:\n{consolidated}"
        )

    success, error = check_c(consolidated, flags=["-fsyntax-only"])
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

            char *duplicate(const char *s) {
                return strdup(s);
            }

            char *next_token(char *str, char **saveptr) {
                return strtok_r(str, ":", saveptr);
            }
            """
        )
    )

    compile_commands = _write_compile_commands(
        tmp_path, [main_c], extra_flags="-std=c11 -D_POSIX_C_SOURCE=200809L"
    )
    consolidated = consolidate_init(compile_commands, source_priority=[])

    # All _POSIX_C_SOURCE-gated symbols must appear in the consolidated output
    for sym in ("clock_gettime", "strdup", "strtok_r"):
        assert sym in str(consolidated), (
            f"Consolidated output is missing '{sym}' usage:\n{consolidated}"
        )

    success, error = check_c(consolidated, flags=["-std=c11", "-fsyntax-only"])
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

            int run(void) {
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

    compile_commands = _write_compile_commands(tmp_path, [main_c])
    consolidated = consolidate_init(compile_commands, source_priority=[])

    success, error = check_c(consolidated, flags=["-fsyntax-only"])
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

            int use_pcre(const char *pat) {
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

    compile_commands = _write_compile_commands(tmp_path, [main_c])
    consolidated = consolidate_init(compile_commands, source_priority=[])

    success, error = check_c(consolidated, flags=["-fsyntax-only", "-Werror"])
    assert success, (
        f"Consolidated code does not compile (source-level defines dropped):\n{error}\n\n"
        f"Consolidated output:\n{consolidated}"
    )


def test_header_defined_gnu_source_with_default_source_flag(tmp_path: Path):
    """
    Compile commands have -D_DEFAULT_SOURCE, but _GNU_SOURCE is defined inside
    a project header (config.h/first.h).  The consolidator preserves
    _DEFAULT_SOURCE (from -D flags) but loses _GNU_SOURCE (from the header).
    Code using the GNU strerror_r (returns char*) then fails with:
      error: incompatible integer to pointer conversion

    Uses two TUs to exercise the union: both include config.h which defines
    _GNU_SOURCE, so the union should emit it exactly once.
    """
    config_h = tmp_path / "config.h"
    config_h.write_text(
        dedent(
            """\
            #define _GNU_SOURCE
            """
        )
    )

    main_c = tmp_path / "main.c"
    main_c.write_text(
        dedent(
            """\
            #include "config.h"
            #include <string.h>

            const char *get_err(int errnum) {
                static char buf[256];
                const char *errstr = strerror_r(errnum, buf, sizeof(buf));
                return errstr;
            }
            """
        )
    )

    util_c = tmp_path / "util.c"
    util_c.write_text(
        dedent(
            """\
            #include "config.h"
            #include <string.h>

            const char *get_err2(int errnum) {
                static char buf[128];
                const char *errstr = strerror_r(errnum, buf, sizeof(buf));
                return errstr;
            }
            """
        )
    )

    compile_commands = _write_compile_commands(
        tmp_path, [main_c, util_c], extra_flags=f"-D_DEFAULT_SOURCE -I{tmp_path}"
    )
    consolidated = consolidate_init(compile_commands, source_priority=[])

    success, error = check_c(consolidated, flags=["-fsyntax-only", "-Werror"])
    assert success, (
        f"Consolidated code does not compile (header-defined _GNU_SOURCE lost):\n{error}\n\n"
        f"Consolidated output:\n{consolidated}"
    )
