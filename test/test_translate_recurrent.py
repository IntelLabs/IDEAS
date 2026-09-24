#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import json
import pytest
from pathlib import Path
from textwrap import dedent
from dataclasses import fields
from unittest.mock import MagicMock, patch

import dspy
import networkx as nx

from ideas import ast
from ideas.ast import CodeC
from ideas.ast_rust import BindgenName, CodeRust
from ideas.consolidate import get_symbols_and_dependencies, create_ast_order
from ideas.translate_recurrent import RecurrentTranslator

# Module-qualified so pytest does not try to collect `TestOracle` as a test class
from ideas import oracle as oracle_mod
from ideas.translate_recurrent import SymbolGroup, SymbolName
from ideas.translate_context import TranslationContext
from ideas.translate_snippet import SnippetTranslator
from ideas.tools import check_rust
from ideas.wrapper import C_INTEROP_TRAIT, WrapperGenerator, bindgen_types
from ideas.wrapper import split_items, wrap_values


def _make_graph(
    c_code: ast.CodeC,
) -> tuple[nx.DiGraph, list[SymbolGroup], dict[SymbolName, ast.Symbol]]:
    tu = ast.create_translation_unit(c_code)
    tree = ast.extract_info_c(tu)
    symbols, dependencies = get_symbols_and_dependencies([tree])
    ast_order = create_ast_order([], [tree])
    G = nx.from_dict_of_lists(dependencies, create_using=nx.DiGraph)  # type: ignore[arg-type]
    assert isinstance(G, nx.DiGraph)
    groups = list(
        nx.lexicographical_topological_sort(
            G.reverse(copy=False), key=ast.create_symbol_lexical_key_fn(symbols, ast_order)
        )
    )
    return G, groups, symbols


# Properties live on the class; fields without defaults only show up via `fields`
_SYMBOL_PREDICATES = {n for n in dir(ast.Symbol) if n.startswith("is_")} | {
    f.name for f in fields(ast.Symbol) if f.name.startswith("is_")
}


def _mock_function_symbol(sym: SymbolName, spelling: str) -> MagicMock:
    mock_symbol = MagicMock()
    # MagicMock invents truthy attributes, so a predicate added to `Symbol` would otherwise
    # silently flip on every mock symbol here
    for predicate in _SYMBOL_PREDICATES:
        setattr(mock_symbol, predicate, False)
    mock_symbol.name = sym
    mock_symbol.spelling = spelling
    mock_symbol.code = CodeC(f"int {spelling}(void) {{ return 0; }}")
    mock_symbol.forward_declaration = CodeC(f"int {spelling}(void);")
    mock_symbol.is_function = True
    mock_symbol.is_definition = True
    mock_symbol.difficulty = 0
    return mock_symbol


def _mock_bar_symbol(sym: SymbolName) -> MagicMock:
    return _mock_function_symbol(sym, "bar")


def test_context_isolated_node():
    G, groups, symbols = _make_graph(ast.CodeC("int standalone(void) { return 42; }"))

    (func_group,) = groups  # only one symbol in this translation unit

    ctx = TranslationContext.build(G, func_group, groups, symbols)

    # An isolated node has no dependents, no dependencies, and nothing translated yet.
    assert str(ctx.translate.dependent_code) == ""
    assert str(ctx.wrap.translated_code) == ""
    assert str(ctx.translate.crate_code) == ""
    assert str(ctx.translate.reference_code) == ""


_TYPEDEF_CHAIN = ast.CodeC("""
struct MyStruct { int x; };
typedef struct MyStruct my_struct_t;
int use_my(my_struct_t s) { return s.x; }
""")

_NESTED_STRUCT = ast.CodeC("""
struct Inner { int x; };
struct Outer { struct Inner inner; };
int process_outer(struct Outer* o) { return o->inner.x; }
""")

_NESTED_TYPEDEF = ast.CodeC("""
struct Point { int x; int y; };
typedef struct Point point_t;
typedef point_t* point_ptr_t;
int use_point(point_ptr_t p) { (void)p; return 0; }
""")

_ENUM_TYPEDEF = ast.CodeC("""
enum Status { STATUS_OK = 0, STATUS_ERROR = 1 };
typedef enum Status status_t;
int check_status(status_t s) { return (int)s; }
""")

_TWO_STRUCTS = ast.CodeC("""
struct A { int x; };
struct B { int y; };
""")

_VAR_INTERMEDIATE = ast.CodeC("""
struct Config { int timeout; };
struct Config global_config;
int get_timeout(void) { return global_config.timeout; }
""")


def test_context_typedef_indirection():
    G, groups, symbols = _make_graph(_TYPEDEF_CHAIN)

    struct_group: SymbolGroup = ("c:@S@MyStruct",)

    ctx = TranslationContext.build(G, struct_group, groups, symbols)

    # typedef is the immediate predecessor — should be in dependent_code
    assert symbols["c:file.c@T@my_struct_t"].code in ctx.translate.dependent_code
    # use_my consumes the struct (via typedef); its call signature reveals whether
    # the type is passed by value or pointer, which informs Copy/Clone/borrowing decisions
    assert symbols["c:@F@use_my"].code in ctx.translate.dependent_code


def test_context_linear_chain_middle():
    G, groups, symbols = _make_graph(_TYPEDEF_CHAIN)

    struct_group: SymbolGroup = ("c:@S@MyStruct",)
    typedef_group: SymbolGroup = ("c:file.c@T@my_struct_t",)

    struct_translation = CodeRust("pub struct MyStruct { pub x: i32 }")
    translations: dict[SymbolGroup, CodeRust] = {struct_group: struct_translation}

    ctx = TranslationContext.build(G, typedef_group, groups, symbols, translations=translations)

    # function is the immediate predecessor of typedef in the dependency graph
    assert symbols["c:@F@use_my"].code in ctx.translate.dependent_code
    # struct is the immediate successor of typedef → full code used
    assert symbols["c:@S@MyStruct"].code in ctx.wrap.translated_code
    # struct has been translated so its Rust code appears in crate_code
    assert struct_translation in ctx.translate.crate_code


def test_context_nested_struct_fields():
    G, groups, symbols = _make_graph(_NESTED_STRUCT)

    inner_group: SymbolGroup = ("c:@S@Inner",)

    ctx = TranslationContext.build(G, inner_group, groups, symbols)

    # Outer embeds Inner — should be in dependent_code
    assert symbols["c:@S@Outer"].code in ctx.translate.dependent_code
    # process_outer shows how Outer (and by extension Inner) is used via pointer,
    # informing borrowing semantics for Inner's translation
    assert symbols["c:@F@process_outer"].code in ctx.translate.dependent_code


def test_context_nested_typedefs():
    G, groups, symbols = _make_graph(_NESTED_TYPEDEF)

    struct_group: SymbolGroup = ("c:@S@Point",)

    ctx = TranslationContext.build(G, struct_group, groups, symbols)

    # point_t is 1 hop — should be in dependent_code
    assert symbols["c:file.c@T@point_t"].code in ctx.translate.dependent_code
    # point_ptr_t aliases a pointer to point_t — still part of the usage chain
    assert symbols["c:file.c@T@point_ptr_t"].code in ctx.translate.dependent_code
    # use_point is the actual consumer — reveals how the pointer type is used
    assert symbols["c:@F@use_point"].code in ctx.translate.dependent_code


def test_context_enum_typedef():
    G, groups, symbols = _make_graph(_ENUM_TYPEDEF)

    enum_group: SymbolGroup = ("c:@E@Status",)

    ctx = TranslationContext.build(G, enum_group, groups, symbols)

    # status_t is the immediate predecessor — should be in dependent_code
    assert symbols["c:file.c@T@status_t"].code in ctx.translate.dependent_code
    # check_status uses the enum; its usage informs how Status should be
    # represented in Rust (e.g., as a plain enum vs. integer newtype)
    assert symbols["c:@F@check_status"].code in ctx.translate.dependent_code


def test_context_variable_intermediate():
    G, groups, symbols = _make_graph(_VAR_INTERMEDIATE)

    struct_group: SymbolGroup = ("c:@S@Config",)

    ctx = TranslationContext.build(G, struct_group, groups, symbols)

    # global_config is the immediate predecessor — should be in dependent_code
    assert symbols["c:@global_config"].code in ctx.translate.dependent_code
    # get_timeout reveals that Config backs global state, which informs
    # Rust's ownership and synchronization strategy (Mutex, OnceCell, etc.)
    assert symbols["c:@F@get_timeout"].code in ctx.translate.dependent_code


def test_reference_code_none_hop():
    G, groups, symbols = _make_graph(_TWO_STRUCTS)

    a_group: SymbolGroup = ("c:@S@A",)
    b_group: SymbolGroup = ("c:@S@B",)

    a_translation = CodeRust("pub struct A { pub x: i32 }")
    translations: dict[SymbolGroup, CodeRust] = {a_group: a_translation}

    ctx = TranslationContext.build(G, b_group, groups, symbols, translations=translations)

    # A and B are unrelated — hops.get(a_group) is None when building context for B.
    # The None case falls through to strip_fns(..., delete=True), which deletes top-level
    # functions but keeps types as-is, since a type may still be needed even when the
    # dependency on it is not visible to static analysis.
    assert a_translation in ctx.translate.reference_code


# `bucket_lookup` is the only direct user of the struct; the `tiny_*` helpers reach
# `to_be_translated` only as its dependencies, so they never reference the struct.
_BUDGETED_USERS = ast.CodeC("""
struct Bucket { unsigned int *flags; char **keys; };

int tiny_01(void) { return 1; }
int tiny_02(void) { return 2; }
int tiny_03(void) { return 3; }
int tiny_04(void) { return 4; }
int tiny_05(void) { return 5; }
int tiny_06(void) { return 6; }
int tiny_07(void) { return 7; }
int tiny_08(void) { return 8; }

int bucket_lookup(struct Bucket *b, unsigned int i)
{
    if ((b->flags[i >> 4] >> ((i & 0xfU) << 1)) & 3)
        return -1;
    return (b->keys[i] != 0) + tiny_01() + tiny_02() + tiny_03() + tiny_04()
        + tiny_05() + tiny_06() + tiny_07() + tiny_08();
}
""")

_TINY_NAMES = [f"c:@F@tiny_{i:02d}" for i in range(1, 9)]


def test_dependent_code_prefers_direct_users(monkeypatch: pytest.MonkeyPatch):
    G, groups, symbols = _make_graph(_BUDGETED_USERS)

    struct_group: SymbolGroup = ("c:@S@Bucket",)
    user = symbols["c:@F@bucket_lookup"].code

    # Exactly enough budget for the one direct user, so nothing else can fit
    monkeypatch.setattr("ideas.translate_context.MAX_DEPENDENT_CHARS", len(str(user)))

    ctx = TranslationContext.build(G, struct_group, groups, symbols)

    assert user in ctx.translate.dependent_code
    for name in _TINY_NAMES:
        assert symbols[name].code not in ctx.translate.dependent_code


def test_dependent_code_does_not_spill_past_an_overflowing_tier(
    monkeypatch: pytest.MonkeyPatch,
):
    G, groups, symbols = _make_graph(_BUDGETED_USERS)

    struct_group: SymbolGroup = ("c:@S@Bucket",)
    user = symbols["c:@F@bucket_lookup"].code

    # One char short of the direct user, which every `tiny_*` helper would still fit into
    monkeypatch.setattr("ideas.translate_context.MAX_DEPENDENT_CHARS", len(str(user)) - 1)

    ctx = TranslationContext.build(G, struct_group, groups, symbols)

    assert str(ctx.translate.dependent_code) == ""


@pytest.mark.parametrize(
    "code",
    [
        "free(p);",
        "git__free(p);",
        "git__malloc(n);",
        "git__reallocarray(p, n, s);",
        "xmalloc(n);",
        "zfree(strm, p);",
    ],
)
def test_memory_pattern_matches_wrapped_allocators(code: str):
    assert TranslationContext._MEMORY_PATTERN.search(code)


def _make_translator(
    tmp_path: Path,
    *,
    is_bin: bool = False,
    c_src: str = "",
    tests: str | None = None,
    broken_tests: set[str] | None = None,
    **kwargs,
) -> tuple[RecurrentTranslator, Path, Path, Path]:
    # -sys crate
    sys_src_dir = tmp_path / "sys" / "src"
    sys_src_dir.mkdir(parents=True)
    sys_lib = sys_src_dir / "lib.rs"
    sys_lib.write_bytes(b"")
    # Must be in place before construction: a bin translator rewrites `main` there
    sys_lib.with_suffix(".c").write_text(c_src)

    sys_crate = MagicMock()
    sys_crate.lib_src_path = sys_lib
    sys_crate.lib_name = "libfoo_sys"

    # -rs crate
    rs_src_dir = tmp_path / "rs" / "src"
    rs_src_dir.mkdir(parents=True)
    (rs_src_dir / "lib.rs").write_bytes(b"")

    rs_crate = MagicMock()
    rs_crate.lib_src_path = rs_src_dir / "lib.rs"
    rs_crate.main_src_path = rs_src_dir / "main.rs" if is_bin else None
    rs_crate.lib_name = "foo_rs"
    rs_crate.cargo_build.return_value = (True, "")

    # hybrid crate
    hybrid_src_dir = tmp_path / "hybrid" / "src"
    hybrid_src_dir.mkdir(parents=True)
    (hybrid_src_dir / "lib.rs").write_bytes(b"")

    crate = MagicMock()
    crate.lib_src_path = hybrid_src_dir / "lib.rs"
    crate.main_src_path = hybrid_src_dir / "main.rs" if is_bin else None
    crate.src_dir = hybrid_src_dir
    crate.cargo_toml = tmp_path / "hybrid" / "Cargo.toml"
    crate.cargo_build.return_value = (True, "")
    crate.cargo_test.return_value = (True, "", "", 0)

    # RecurrentTranslator with mocked inputs
    oracle = oracle_mod.TestOracle(crate, tests) if tests is not None else None
    translator = RecurrentTranslator(
        sys_crate=sys_crate,
        crate=crate,
        rs_crate=rs_crate,
        symbol_oracle=oracle,
        **kwargs,
    )

    # Verification needs a real crate to run nextest against, so inject its result instead
    def _stub_baseline() -> None:
        translator._oracle._baseline_failures = frozenset(broken_tests or ())  # type: ignore[attr-defined]

    translator._oracle.baseline = _stub_baseline  # type: ignore[method-assign]
    return (
        translator,
        hybrid_src_dir / "lib.rs",
        rs_src_dir / "lib.rs",
        sys_lib.with_suffix(".c"),
    )


def test_wrapper_only_restore_on_final_wrapper_failure(tmp_path: Path) -> None:
    # Create a SnippetTranslator backed by a mock LM that always succeeds
    mock_translator = MagicMock(
        return_value=dspy.Prediction(translation=CodeRust("pub fn bar() {}"))
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    # Create a WrapperGenerator backed by a mock LM that returns a unique but invalid
    # wrapper each attempt because validate_changes produces scope_feedback cause a failure
    # because we patch ideas.hybrid.generate_unimplemented_function_wrapper below
    attempt = 0

    def mock_wrapper_side_effect(*args, **kwargs):
        nonlocal attempt
        attempt += 1
        return dspy.Prediction(wrapper=CodeRust(f"// attempt {attempt} wrapper"))

    mock_wrapper = MagicMock(side_effect=mock_wrapper_side_effect)
    symbol_wrapper = WrapperGenerator(
        wrapper=MagicMock(return_value=mock_wrapper),  # type: ignore[arg-type]
        max_iters=1,
    )

    # Create a RecurrentTranslator with max_iters=2 to exercise both restore paths
    # NOTE: Supplying tests is required so WrapperGenerator is called since the symbol is not global
    translator, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=2,
    )

    # Construct symbols and dependencies for a single function `bar` to be wrapped
    sym: SymbolName = "c:@F@bar"
    sym_group: SymbolGroup = (sym,)
    mock_symbol = _mock_bar_symbol(sym)

    symbols: dict[SymbolName, MagicMock] = {sym: mock_symbol}
    dependencies: dict[SymbolGroup, list[SymbolGroup]] = {sym_group: []}

    # Write the C definition that clang_make_extern_ will extern-ify
    sys_c.write_text("int bar(void) { return 0; }")

    # A previously wrapped symbol, so the rollback has something it must preserve
    original_wrapper = CodeRust("// previous good wrapper")
    translator._hybrid.set_wrapper("c:@F@baz", original_wrapper)

    # Run the translator
    with patch(
        "ideas.hybrid.generate_unimplemented_function_wrapper",
        return_value=CodeRust("pub fn bar() { unimplemented!() }"),
    ):
        pred = translator(symbols=symbols, dependencies=dependencies)
    assert pred.complete

    # Translator should have iterated twice
    assert mock_translator.call_count == 2
    assert mock_wrapper.call_count == 2

    # Rolling back must drop the failed wrapper while keeping the earlier one
    assert translator._hybrid.wrappers == {"c:@F@baz": original_wrapper}

    # Must not have duplicated the surviving wrapper across retries
    assert hybrid_lib.read_text().count("// previous good wrapper") == 1

    # Must not have duplicated the translation in the -rs crate across retries
    assert rs_lib.read_text().count("pub fn bar") == 1

    # The C definition comes back with the failed wrapper, so something still defines `bar`
    assert "extern int bar(void);" not in sys_c.read_text()
    assert "{ return 0; }" in sys_c.read_text()

    # Translation must not bleed into the hybrid crate
    assert "pub fn bar" not in hybrid_lib.read_text()


def test_wrapper_only_restore_keeps_wrapped_peers_defined(tmp_path: Path) -> None:
    # One group, two symbols: `baz` wraps cleanly and `bar` never does. Wrapping hands a
    # symbol's definition from C to Rust, so a rollback that drops a wrapper must put the C
    # definition back, or the symbol ends up defined in neither language.
    mock_translator = MagicMock(
        return_value=dspy.Prediction(translation=CodeRust("pub fn baz() {}\npub fn bar() {}"))
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    baz_stub = CodeRust("pub fn baz_wrapper() -> i32 { unimplemented!() }")
    bar_stub = CodeRust("pub fn bar_wrapper() -> i32 { unimplemented!() }")
    baz_wrapper = CodeRust("pub fn baz_wrapper() -> i32 { 0 }")

    c_src_at_bar = ""

    def mock_wrapper_side_effect(*args, **kwargs):
        nonlocal c_src_at_bar
        # Keeping the template's shape leaves validate_changes nothing to complain about, so
        # `baz` succeeds; anything else is off-template and fails every attempt.
        if kwargs["example_wrapper"] == baz_stub:
            return dspy.Prediction(wrapper=baz_wrapper)
        c_src_at_bar = sys_c.read_text()
        return dspy.Prediction(wrapper=CodeRust("// off-template wrapper"))

    mock_wrapper = MagicMock(side_effect=mock_wrapper_side_effect)
    symbol_wrapper = WrapperGenerator(
        wrapper=MagicMock(return_value=mock_wrapper),  # type: ignore[arg-type]
        max_iters=1,
    )

    # max_iters=1 so the wrap failure lands directly on the `hybrid_only=True` branch
    translator, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=1,
    )

    baz: SymbolName = "c:@F@baz"
    bar: SymbolName = "c:@F@bar"
    # One group, wrapped in tuple order: `baz` first and then `bar`
    sym_group: SymbolGroup = (baz, bar)
    symbols: dict[SymbolName, MagicMock] = {
        baz: _mock_function_symbol(baz, "baz"),
        bar: _mock_function_symbol(bar, "bar"),
    }
    dependencies: dict[SymbolGroup, list[SymbolGroup]] = {sym_group: []}

    sys_c.write_text("int baz(void) { return 0; }\nint bar(void) { return baz(); }\n")

    with patch(
        "ideas.hybrid.generate_unimplemented_function_wrapper",
        side_effect=lambda _path, spelling, _types: baz_stub if spelling == "baz" else bar_stub,
    ):
        translator(symbols=symbols, dependencies=dependencies)

    # Sanity: by the time `bar` was wrapped, `baz` had handed its definition over to Rust
    assert "extern int baz(void);" in c_src_at_bar

    # Every symbol the C source no longer defines must still have a wrapper standing in for it
    c_src = sys_c.read_text()
    externed = {s for s in ("baz", "bar") if f"extern int {s}(void);" in c_src}
    wrapped = {symbols[name].spelling for name in translator._hybrid.wrappers}
    assert externed <= wrapped, (
        f"undefined after rollback: {sorted(externed - wrapped)} "
        "(C definition removed, no wrapper left)"
    )


def test_full_restore_after_two_translation_failures(tmp_path: Path) -> None:
    # Each attempt produces a unique translation so the translation-loop guard does not
    # short-circuit before the second full restore runs.
    attempt = 0

    def mock_translator_side_effect(*args, **kwargs):
        nonlocal attempt
        attempt += 1
        return dspy.Prediction(
            translation=CodeRust(f"pub fn bar() {{ /* attempt {attempt} */ }}")
        )

    mock_translator = MagicMock(side_effect=mock_translator_side_effect)
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    # Wrapper generator is never reached because translation always fails; a plain mock is enough.
    translator, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=MagicMock(),
        tests="dummy_test",
        max_iters=2,
    )

    # Make cargo build always fail for the -rs crate so every translation attempt fails
    # so a full restore (not wrappers-only) is performed on *both* iterations.
    rs_crate_mock: MagicMock = translator.rust_crate  # type: ignore[assignment]
    rs_crate_mock.cargo_build.return_value = (False, "compilation error")

    sym: SymbolName = "c:@F@bar"
    sym_group: SymbolGroup = (sym,)
    mock_symbol = _mock_bar_symbol(sym)

    symbols: dict[SymbolName, MagicMock] = {sym: mock_symbol}
    dependencies: dict[SymbolGroup, list[SymbolGroup]] = {sym_group: []}

    # Write known content so the restore assertions check for something specific,
    # not just whatever RecurrentTranslator.__init__ happened to leave behind.
    initial_rs_lib = "#![forbid(unsafe_code)]\n\n// known rs baseline\n"
    initial_c_src = "int bar(void) { return 0; }"
    original_wrapper = CodeRust("// previous good wrapper")
    rs_lib.write_text(initial_rs_lib)
    sys_c.write_text(initial_c_src)
    translator._hybrid.set_wrapper("c:@F@baz", original_wrapper)
    initial_hybrid_lib = hybrid_lib.read_text()

    # Run the translator
    pred = translator(symbols=symbols, dependencies=dependencies)
    assert not pred.complete

    # Both iterations must have attempted translation
    assert mock_translator.call_count == 2

    # Full restore: -rs crate must be rolled back to its pre-call state
    assert rs_lib.read_text() == initial_rs_lib

    # Full restore: the hybrid crate re-renders to its pre-call state
    assert hybrid_lib.read_text() == initial_hybrid_lib

    # Full restore: C source must be rolled back (clang_make_extern_ must not have run)
    assert sys_c.read_text() == initial_c_src

    # Full restore: the pre-existing wrapper must be intact and nothing added
    assert translator._hybrid.wrappers == {"c:@F@baz": original_wrapper}

    # Translation must not bleed into the hybrid crate across retries
    assert "pub fn bar" not in hybrid_lib.read_text()


def test_no_restore_on_test_failure(tmp_path: Path) -> None:
    # Translation and wrapping both succeed; only the test run fails.
    # On the last (only) iteration with failure == "test", the restore branch is:
    #   `elif result.failure == "test": pass`  — nothing is restored.
    mock_translator = MagicMock(
        return_value=dspy.Prediction(translation=CodeRust("pub fn bar() {}"))
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    # A wrapper that keeps the template's shape means validate_changes finds no diffs →
    # scope_feedback is empty → pred.success = True → wrapping succeeds.
    unimplemented_stub = CodeRust("pub fn bar_wrapper() -> i32 { unimplemented!() }")
    bar_wrapper = CodeRust("pub fn bar_wrapper() -> i32 { 0 }")
    mock_wrapper_gen = MagicMock(return_value=dspy.Prediction(wrapper=bar_wrapper))
    symbol_wrapper = WrapperGenerator(
        wrapper=MagicMock(return_value=mock_wrapper_gen),  # type: ignore[arg-type]
        max_iters=1,
    )

    translator, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=1,
    )

    # Make the test run fail so result.failure == "test"
    crate_mock: MagicMock = translator.crate  # type: ignore[assignment]
    crate_mock.cargo_test.side_effect = [
        # forward init gate: the crate and its test target compile
        (True, "", "", 0),
        # _wrap_function fail-fast gate after the unimplemented wrapper is written
        (True, "", "", 0),
        # wrapper build gate: the wrapped crate and its test target compile
        (True, "", "", 0),
        # _test_symbol build_only pre-pass: crate and test harness compile
        (True, "", "", 0),
        # _test_symbol run: the test itself fails
        (False, '{"type":"test","name":"test_bar","event":"failed"}', "1 test failed", 101),
    ]

    sym: SymbolName = "c:@F@bar"
    sym_group: SymbolGroup = (sym,)
    mock_symbol = _mock_bar_symbol(sym)

    symbols: dict[SymbolName, MagicMock] = {sym: mock_symbol}
    dependencies: dict[SymbolGroup, list[SymbolGroup]] = {sym_group: []}

    sys_c.write_text("int bar(void) { return 0; }")

    # Run the translator
    with patch(
        "ideas.hybrid.generate_unimplemented_function_wrapper",
        return_value=unimplemented_stub,
    ):
        pred = translator(symbols=symbols, dependencies=dependencies)
    assert pred.complete
    assert pred.regressions == {"test_bar": sym_group}

    # No restore: translation is kept in the -rs crate
    assert "pub fn bar" in rs_lib.read_text()

    # No restore: the wrapper written during wrapping is kept and rendered
    assert translator._hybrid.wrappers == {sym: bar_wrapper}
    assert "pub fn bar_wrapper" in hybrid_lib.read_text()

    # No restore: C source is kept with the extern declaration written by clang_make_extern_
    assert "extern int bar(void);" in sys_c.read_text()


def test_feedback_after_test_failure(tmp_path: Path) -> None:
    # Translation and wrapping succeed on both iterations
    mock_translator = MagicMock(
        return_value=dspy.Prediction(translation=CodeRust("pub fn bar() {}"))
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    # A wrapper that keeps the template's shape means validate_changes passes
    unimplemented_stub = CodeRust("pub fn bar_wrapper() -> i32 { unimplemented!() }")
    mock_wrapper_gen = MagicMock(
        return_value=dspy.Prediction(wrapper=CodeRust("pub fn bar_wrapper() -> i32 { 0 }"))
    )
    symbol_wrapper = WrapperGenerator(
        wrapper=MagicMock(return_value=mock_wrapper_gen),  # type: ignore[arg-type]
        max_iters=1,
    )

    translator, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=2,
    )

    # First test run fails, second succeeds.
    crate_mock: MagicMock = translator.crate  # type: ignore[assignment]
    crate_mock.cargo_test.side_effect = [
        # forward init gate
        (True, "", "", 0),
        # _wrap_function fail-fast gate (iter 1)
        (True, "", "", 0),
        # wrapper build gate (iter 1)
        (True, "", "", 0),
        # _test_symbol build_only pre-pass (iter 1)
        (True, "", "", 0),
        # _test_symbol run (iter 1): the test fails
        (False, '{"type":"test","name":"test_bar","event":"failed"}', "1 test failed", 101),
        # _wrap_function fail-fast gate (iter 2)
        (True, "", "", 0),
        # wrapper build gate (iter 2)
        (True, "", "", 0),
        # _test_symbol build_only pre-pass (iter 2)
        (True, "", "", 0),
        # _test_symbol run (iter 2): the test passes
        (True, '{"type":"test","name":"test_bar","event":"ok"}', "", 0),
    ]

    sym: SymbolName = "c:@F@bar"
    sym_group: SymbolGroup = (sym,)
    mock_symbol = _mock_bar_symbol(sym)

    symbols: dict[SymbolName, MagicMock] = {sym: mock_symbol}
    dependencies: dict[SymbolGroup, list[SymbolGroup]] = {sym_group: []}

    sys_c.write_text("int bar(void) { return 0; }")

    # Run the translator
    with patch(
        "ideas.hybrid.generate_unimplemented_function_wrapper",
        return_value=unimplemented_stub,
    ):
        pred = translator(symbols=symbols, dependencies=dependencies)
    assert pred.complete

    # Translator must have been called twice: once per outer iteration.
    assert mock_translator.call_count == 2

    # Extract the feedback forwarded to the translator on the second (retry) call.
    second_call_feedback: str = mock_translator.call_args_list[1].kwargs["feedback"]

    # Test failures should tell the translator its output doesn't match the C behavior.
    assert "does not match the behavior" in second_call_feedback

    # ... and which symbol was swapped in when the divergence appeared
    assert "`bar`" in second_call_feedback

    # The wrapper is a suspect too, so it hears about the divergence in its own words
    second_wrap_feedback: str = mock_wrapper_gen.call_args_list[1].kwargs["feedback"]
    assert "changed the program's observable behavior" in second_wrap_feedback
    assert "`bar`" in second_wrap_feedback


def test_feedback_after_wrap_failure(tmp_path: Path) -> None:
    # Translation succeeds on both iterations
    mock_translator = MagicMock(
        return_value=dspy.Prediction(translation=CodeRust("pub fn bar() {}"))
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    # A wrapper that keeps the template's shape means validate_changes passes
    unimplemented_stub = CodeRust("pub fn bar_wrapper() -> i32 { unimplemented!() }")
    mock_wrapper_gen = MagicMock(
        return_value=dspy.Prediction(wrapper=CodeRust("pub fn bar_wrapper() -> i32 { 0 }"))
    )
    symbol_wrapper = WrapperGenerator(
        wrapper=MagicMock(return_value=mock_wrapper_gen),  # type: ignore[arg-type]
        max_iters=1,
    )

    translator, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=2,
    )

    # Force failure on the first wrapper build attempt, then succeed on the second.
    crate_mock: MagicMock = translator.crate  # type: ignore[assignment]
    crate_mock.cargo_build.side_effect = [
        (True, ""),  # forward's pre-commit check of the types and bindings
        (True, ""),  # _wrap_function scaffold check (iter 1)
        (False, "build error"),  # build(wrapper) inside WrapperGenerator (iter 1)
        (True, ""),  # _wrap_function scaffold check (iter 2)
        (True, ""),  # build(wrapper) inside WrapperGenerator (iter 2)
        (True, ""),  # _test_symbol build gate (iter 2)
    ]
    crate_mock.cargo_test.return_value = (True, "", "", "")

    sym: SymbolName = "c:@F@bar"
    sym_group: SymbolGroup = (sym,)
    mock_symbol = _mock_bar_symbol(sym)

    symbols: dict[SymbolName, MagicMock] = {sym: mock_symbol}
    dependencies: dict[SymbolGroup, list[SymbolGroup]] = {sym_group: []}

    sys_c.write_text("int bar(void) { return 0; }")

    # Run the translator
    with patch(
        "ideas.hybrid.generate_unimplemented_function_wrapper",
        return_value=unimplemented_stub,
    ):
        pred = translator(symbols=symbols, dependencies=dependencies)
    assert pred.complete

    # Translator must have been called twice: once per outer iteration.
    assert mock_translator.call_count == 2

    # Extract the feedback forwarded to the translator on the second (retry) call.
    second_call_feedback: str = mock_translator.call_args_list[1].kwargs["feedback"]

    # Wrap failures should instruct the translator to produce wrapper-friendly code.
    assert "C-compatible FFI wrapper" in second_call_feedback

    # The retry hands the wrapper generator its own failed wrapper, so the reason goes with it
    second_wrap_feedback: str = mock_wrapper_gen.call_args_list[1].kwargs["build_feedback"]
    assert "build error" in second_wrap_feedback


@pytest.mark.parametrize(
    "kind, expected",
    [
        ("variable", "`static` or `const`"),
        ("type", "field by field"),
        ("function", "argument and return types"),
    ],
)
def test_wrap_feedback_matches_the_symbol_kind(kind: str, expected: str) -> None:
    symbol = MagicMock()
    symbol.spelling = "add_opts"
    symbol.is_variable = kind == "variable"
    symbol.is_type = kind == "type"

    candidate = oracle_mod.Candidate(
        symbol=symbol,
        snippet=CodeC("struct add_opts { int n; };"),
        translation=CodeRust("// translation"),
        wrapped=False,
        wrap_rejection="error[E0277]: the trait bound is not satisfied",
    )
    feedback = oracle_mod.NullOracle().judge(candidate).feedback

    assert "add_opts" in feedback
    assert expected in feedback
    # Advising "function boundaries" is what once turned a global into a factory function
    assert "function boundaries" not in feedback
    # A group is an SCC and can mix kinds, so the advice must not read as group-wide
    assert "The guidance above is about `add_opts`" in feedback
    # ... but a mutually recursive item may still have to change for the wrapper to work
    assert "only where `add_opts` requires it" in feedback
    # The compiler's own words are worth more than the generic prose around them
    assert "error[E0277]" in feedback


def test_feedback_after_translate_failure(tmp_path: Path) -> None:
    # Translation fails on the first iteration (the -rs build rejects it) and succeeds
    # on the second.
    mock_translator = MagicMock(
        return_value=dspy.Prediction(translation=CodeRust("pub fn bar() {}"))
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    # tests=None: wrapping is skipped for non-global symbols, so only translation runs.
    translator, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=MagicMock(),
        tests=None,
        max_iters=2,
    )

    # Make the -rs build fail on iteration 1 and succeed on iteration 2.
    rs_crate_mock: MagicMock = translator.rust_crate  # type: ignore[assignment]
    rs_crate_mock.cargo_build.side_effect = [
        (False, "compile error"),  # iter 1: build rejects translation → failure="translate"
        (True, ""),  # iter 2: build accepts translation → success
    ]

    sym: SymbolName = "c:@F@bar"
    sym_group: SymbolGroup = (sym,)
    mock_symbol = _mock_bar_symbol(sym)

    symbols: dict[SymbolName, MagicMock] = {sym: mock_symbol}
    dependencies: dict[SymbolGroup, list[SymbolGroup]] = {sym_group: []}

    sys_c.write_text("int bar(void) { return 0; }")

    pred = translator(symbols=symbols, dependencies=dependencies)

    assert pred.complete

    # Translator must have been called twice: once per outer iteration.
    assert mock_translator.call_count == 2

    # Extract the feedback forwarded to the translator on the second (retry) call.
    second_call_feedback: str = mock_translator.call_args_list[1].kwargs["build_feedback"]

    # The build error from the first iteration must be surfaced to the retry so the
    # translator knows why its previous output was rejected.
    assert "compile error" in second_call_feedback


def test_feedback_after_scope_violation(tmp_path: Path) -> None:
    # The first translation repeats the crate-level attribute, which lib.rs already carries
    mock_translator = MagicMock(
        side_effect=[
            dspy.Prediction(translation=CodeRust("#![forbid(unsafe_code)]\npub fn bar() {}")),
            dspy.Prediction(translation=CodeRust("pub fn bar() {}")),
        ]
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    # tests=None: wrapping is skipped for non-global symbols, so only translation runs.
    translator, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=MagicMock(),
        tests=None,
        max_iters=2,
    )

    # The attribute is caught before anything is built, so every build that runs succeeds
    rs_crate_mock: MagicMock = translator.rust_crate  # type: ignore[assignment]
    rs_crate_mock.cargo_build.return_value = (True, "")

    sym: SymbolName = "c:@F@bar"
    sym_group: SymbolGroup = (sym,)
    mock_symbol = _mock_bar_symbol(sym)

    symbols: dict[SymbolName, MagicMock] = {sym: mock_symbol}
    dependencies: dict[SymbolGroup, list[SymbolGroup]] = {sym_group: []}

    sys_c.write_text("int bar(void) { return 0; }")

    pred = translator(symbols=symbols, dependencies=dependencies)

    assert pred.complete
    assert mock_translator.call_count == 2

    retry = mock_translator.call_args_list[1].kwargs

    # A hard-constraint violation is not a compiler error, so it must reach the retry through
    # `scope_feedback` and leave `build_feedback` empty
    assert "Do not include" in retry["scope_feedback"]
    assert retry["build_feedback"] == ""


_TYPEDEF_STRUCT = ast.CodeC("typedef struct MyStruct { int x; } my_struct_t;")

# clang gives an unnamed tag the typedef's name, so it reports spelling `my_struct_t` for
# the STRUCT_DECL, which is externally visible and names the wrapper after the typedef
# instead of the (absent) tag.
_ANONYMOUS_TYPEDEF_STRUCT = ast.CodeC("typedef struct { int x; } my_struct_t;")

# A tag may reuse the name of its typedef
_SAME_NAME_TYPEDEF_STRUCT = ast.CodeC("typedef struct my_struct_t { int x; } my_struct_t;")

_TYPEDEF_CONSUMER = "\nint use_it(my_struct_t s) { return s.x; }\n"


@pytest.mark.parametrize(
    "c_source",
    [_TYPEDEF_STRUCT, _ANONYMOUS_TYPEDEF_STRUCT, _SAME_NAME_TYPEDEF_STRUCT],
    ids=["named_tag", "anonymous_tag", "tag_named_after_typedef"],
)
def test_typedef_struct_is_the_only_struct_symbol(c_source: CodeC) -> None:
    # A consumer makes the type reachable from a global function, as in a real project.
    # Naming the type by its alias leaves no reference to the tag, so the STRUCT_DECL is
    # dropped as unreachable and the typedef is the only symbol left to carry the wrapper.
    tu = ast.create_translation_unit(CodeC(str(c_source) + _TYPEDEF_CONSUMER))
    tree = ast.extract_info_c(tu)
    symbols, _ = get_symbols_and_dependencies([tree])

    # Exactly one symbol must claim the struct, or the same type is wrapped twice
    structs = [s for s in symbols.values() if s.is_struct]
    assert len(structs) == 1
    (struct,) = structs

    assert struct.is_type
    assert struct.is_definition

    # `_wrap_type` allowlists this in bindgen, then resolves the struct it declares
    assert struct.spelling == "my_struct_t"

    # `_wrap_type` gates on this, and the tag it aliases is visible in every shape
    assert struct.is_externally_visible


@pytest.mark.parametrize(
    ("c_source", "consumer", "type_name"),
    [
        (_TYPEDEF_STRUCT, "", "MyStruct"),
        (_ANONYMOUS_TYPEDEF_STRUCT, "", "my_struct_t"),
        (_SAME_NAME_TYPEDEF_STRUCT, "", "my_struct_t"),
        (_TYPEDEF_STRUCT, _TYPEDEF_CONSUMER, "MyStruct"),
        (_ANONYMOUS_TYPEDEF_STRUCT, _TYPEDEF_CONSUMER, "my_struct_t"),
        (_SAME_NAME_TYPEDEF_STRUCT, _TYPEDEF_CONSUMER, "my_struct_t"),
    ],
    ids=[
        "named_tag",
        "anonymous_tag",
        "tag_named_after_typedef",
        "named_tag_alias_only",
        "anonymous_tag_alias_only",
        "tag_named_after_typedef_alias_only",
    ],
)
def test_type_wrapper_generated_for_typedef_struct(
    tmp_path: Path, c_source: CodeC, consumer: str, type_name: str
) -> None:
    # Without a consumer the tag carries the wrapper; with one that names the type only by
    # its alias the tag is pruned and the typedef carries it, and a typedef of a named tag
    # has no linkage of its own, so gating on linkage alone would silently skip it.
    source = CodeC(str(c_source) + consumer)

    mock_translator = MagicMock(
        return_value=dspy.Prediction(translation=CodeRust("pub struct MyStruct { pub x: i32 }"))
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    # Returning the unimplemented template unchanged means validate_changes finds no
    # diffs, so wrapping succeeds. Both round-trip tests must be present, and the impl
    # header must name the type, because `_wrap_type` rejects wrappers that drop them.
    type_wrapper = CodeRust(
        dedent(f"""
            impl CInterop for {type_name} {{
                type Rust = libfoo_rs::{type_name};
                unsafe fn to_rust(_cs: *const Self) -> Self::Rust {{ todo!() }}
                unsafe fn sync_to_c(_rs: &Self::Rust, _cs: *mut Self) {{ todo!() }}
            }}

            #[cfg(test)]
            mod test_{type_name} {{
                #[test]
                fn round_trip_zeroed() {{ todo!() }}
                #[test]
                fn round_trip_nontrivial() {{ todo!() }}
            }}
            """).strip()
    )
    mock_wrapper_gen = MagicMock(return_value=dspy.Prediction(wrapper=type_wrapper))
    symbol_wrapper = WrapperGenerator(
        wrapper=MagicMock(return_value=mock_wrapper_gen),  # type: ignore[arg-type]
        max_iters=1,
    )

    translator, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=1,
    )
    crate_mock: MagicMock = translator.crate  # type: ignore[assignment]
    crate_mock.cargo_test.return_value = (True, "", "", "")

    # Parse real C so the struct/typedef symbols carry genuine kind and linkage flags
    tu = ast.create_translation_unit(source)
    tree = ast.extract_info_c(tu)
    symbols, dependencies = get_symbols_and_dependencies([tree])

    sys_c.write_text(str(source))

    with (
        patch(
            "ideas.translate_recurrent.generate_unimplemented_type_wrapper",
            return_value=(type_wrapper, f"test_{type_name}"),
        ),
        # A consumer is only there to prune the tag, so keep it out of the generator
        patch(
            "ideas.hybrid.generate_unimplemented_function_wrapper",
            return_value=None,
        ),
    ):
        pred = translator(symbols=symbols, dependencies=dependencies)
    assert pred.complete

    # The struct definition is wrappable, so its wrapper must be rendered into the root
    assert list(translator._hybrid.wrappers.values()) == [type_wrapper]
    assert str(type_wrapper) in hybrid_lib.read_text()

    # The tag and its alias share one snippet, so they must not produce two wrappers
    assert mock_wrapper_gen.call_count == 1

    # The test filter keys on the module name, so it has to reach cargo test verbatim
    assert f"test_{type_name}" in [
        call.args[0] for call in crate_mock.cargo_test.call_args_list
    ]

    # Wrapper must not bleed into the -rs crate
    assert f"impl CInterop for {type_name}" not in rs_lib.read_text()


# A self-referential struct names its own tag, so the tag is reachable and survives
# alongside the typedef, and the two form a cycle that puts them in one group.
_SELF_REFERENTIAL_SAME_NAME = ast.CodeC("""
typedef struct MacroParam MacroParam;
struct MacroParam { MacroParam *next; int x; };
int use_it(MacroParam *p) { return p->x; }
""")

# bindgen renders this pair as `pub struct Tag` plus `pub type Alias = Tag`
_SELF_REFERENTIAL_ALIAS = ast.CodeC("""
typedef struct Tag Alias;
struct Tag { Alias *next; int x; };
int use_it(Alias *p) { return p->x; }
""")

# Naming both spellings keeps both reachable without making them mutually dependent,
# so they land in separate groups and the collision spans the whole run.
_TAG_AND_ALIAS_USED_SEPARATELY = ast.CodeC("""
typedef struct Tag Alias;
struct Tag { int x; };
int use_alias(Alias *p) { return p->x; }
int use_tag(struct Tag *p) { return p->x; }
""")

# The body is written inside the typedef, so both symbols own it and both are wrap
# candidates. Both render the whole typedef as their code, so the snippet dedupe in
# `forward` drops the second group before it can emit a second impl.
_INLINE_DEF_TAG_AND_ALIAS = ast.CodeC("""
typedef struct Tag { int x; } Alias;
int use_alias(Alias *p) { return p->x; }
int use_tag(struct Tag *p) { return p->x; }
""")


@pytest.mark.parametrize(
    ("c_source", "tag_name", "struct_spellings"),
    [
        (_SELF_REFERENTIAL_SAME_NAME, "MacroParam", ["MacroParam"]),
        (_SELF_REFERENTIAL_ALIAS, "Tag", ["Tag"]),
        (_TAG_AND_ALIAS_USED_SEPARATELY, "Tag", ["Tag"]),
        (_INLINE_DEF_TAG_AND_ALIAS, "Tag", ["Alias", "Tag"]),
    ],
    ids=[
        "tag_named_after_typedef",
        "named_tag_and_alias",
        "separate_groups",
        "inline_def_tag_and_alias",
    ],
)
def test_tag_and_typedef_share_one_type_wrapper(
    tmp_path: Path, c_source: CodeC, tag_name: str, struct_spellings: list[str]
) -> None:
    # A Rust type alias is not a distinct type, so wrapping both the tag and its typedef
    # emits two `impl CInterop` blocks for one type, which rustc rejects with E0119.
    mock_translator = MagicMock(
        return_value=dspy.Prediction(translation=CodeRust(f"pub struct {tag_name} {{}}"))
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    # `_wrap_type` rejects the `type Rust = ()` placeholder, and the rest of the template
    # comes back unchanged so validate_changes has no out-of-scope diff to reject
    mock_wrapper_gen = MagicMock(
        side_effect=lambda **kwargs: dspy.Prediction(
            wrapper=CodeRust(
                str(kwargs["example_wrapper"]).replace("type Rust = ();", "type Rust = u8;")
            )
        )
    )
    symbol_wrapper = WrapperGenerator(
        wrapper=MagicMock(return_value=mock_wrapper_gen),  # type: ignore[arg-type]
        max_iters=1,
    )

    translator, hybrid_lib, _, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=1,
    )
    crate_mock: MagicMock = translator.crate  # type: ignore[assignment]
    crate_mock.cargo_test.return_value = (True, "", "", "")

    # Compile the rendered crate for real: E0119 is exactly what a mocked build hides.
    # The -sys crate it links against does not exist here, so drop the `use` that names it.
    def rustc_build(*_args, **_kwargs) -> tuple[bool, str]:
        body = "\n".join(
            line
            for line in hybrid_lib.read_text().splitlines()
            if not line.startswith("use libfoo_sys")
        )
        return check_rust(body, flags=["--crate-type=lib", "--emit=metadata"])

    crate_mock.cargo_build.side_effect = rustc_build

    tu = ast.create_translation_unit(c_source)
    tree = ast.extract_info_c(tu)
    symbols, dependencies = get_symbols_and_dependencies([tree])

    # Both spellings survive as symbols; only those owning the body are wrap candidates
    assert len([s for s in symbols.values() if s.is_type]) == 2
    assert sorted(s.spelling for s in symbols.values() if s.is_struct) == struct_spellings

    sys_c.write_text(str(c_source))

    # Function wrappers are irrelevant here, and the consumers only exist to keep the
    # tag and the alias reachable
    with patch(
        "ideas.hybrid.generate_unimplemented_function_wrapper",
        return_value=None,
    ):
        pred = translator(symbols=symbols, dependencies=dependencies)
    assert pred.complete

    assert hybrid_lib.read_text().count("impl CInterop for") == 1
    assert len(translator._hybrid.wrappers) == 1
    assert mock_wrapper_gen.call_count == 1

    # The tag is the one name both symbols agree on: the alias resolves to it
    assert f"impl CInterop for {tag_name}" in hybrid_lib.read_text()


# Each of these compiles and passes `cargo test`, so without an explicit check the
# wrapper would be accepted as correct.
@pytest.mark.parametrize(
    ("wrapper", "expected"),
    [
        (
            dedent("""
                impl CInterop for MyStruct {
                    type Rust = ();
                    unsafe fn to_rust(_cs: *const Self) -> Self::Rust { todo!() }
                    unsafe fn sync_to_c(_rs: &Self::Rust, _cs: *mut Self) { todo!() }
                }
                """),
            "type Rust = ()",
        ),
        (
            dedent("""
                pub unsafe fn to_rust(_cs: *const MyStruct) {}
                pub unsafe fn sync_to_c(_cs: *mut MyStruct) {}
                """),
            "impl CInterop for MyStruct",
        ),
        (
            dedent("""
                impl CInterop for MyStruct {
                    type Rust = libfoo_rs::MyStruct;
                    unsafe fn to_rust(_cs: *const Self) -> Self::Rust { todo!() }
                    unsafe fn sync_to_c(_rs: &Self::Rust, _cs: *mut Self) { todo!() }
                }
                """),
            "must stay named `test_MyStruct`",
        ),
    ],
    ids=["placeholder_rust_type", "dropped_impl_header", "renamed_test_module"],
)
def test_type_wrapper_rejected_for_silent_failures(
    tmp_path: Path, wrapper: str, expected: str
) -> None:
    mock_translator = MagicMock(
        return_value=dspy.Prediction(translation=CodeRust("pub struct MyStruct { pub x: i32 }"))
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    tests_mod = "test_MyStruct"
    # The test module is deliberately named `tests`, so the third case trips the
    # module-name check while the first two trip theirs first
    bad_wrapper = CodeRust(
        wrapper
        + dedent("""
            #[cfg(test)]
            mod tests {
                #[test]
                fn round_trip_zeroed() { todo!() }
                #[test]
                fn round_trip_nontrivial() { todo!() }
            }
            """)
    )
    mock_wrapper_gen = MagicMock(return_value=dspy.Prediction(wrapper=bad_wrapper))

    # Two iterations so the rejection comes back as feedback on a second attempt
    symbol_wrapper = WrapperGenerator(
        wrapper=MagicMock(return_value=mock_wrapper_gen),  # type: ignore[arg-type]
        max_iters=2,
    )

    translator, _, _, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=1,
    )
    crate_mock: MagicMock = translator.crate  # type: ignore[assignment]
    crate_mock.cargo_test.return_value = (True, "", "", "")

    tu = ast.create_translation_unit(_TYPEDEF_STRUCT)
    tree = ast.extract_info_c(tu)
    symbols, dependencies = get_symbols_and_dependencies([tree])

    sys_c.write_text(str(_TYPEDEF_STRUCT))

    with patch(
        "ideas.translate_recurrent.generate_unimplemented_type_wrapper",
        return_value=(bad_wrapper, tests_mod),
    ):
        translator(symbols=symbols, dependencies=dependencies)

    assert mock_wrapper_gen.call_count == 2
    assert expected in mock_wrapper_gen.call_args.kwargs["scope_feedback"]


_SIMPLE_FUNCTION = ast.CodeC("int bar(void) { return 0; }")


def test_known_broken_tests_seed_the_baseline(tmp_path: Path) -> None:
    mock_translator = MagicMock(
        return_value=dspy.Prediction(translation=CodeRust("pub fn bar() -> i32 { 0 }"))
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    translator, _, _, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=None,
        tests="dummy_test",
        max_iters=1,
        broken_tests={"broken"},
    )
    crate_mock: MagicMock = translator.crate  # type: ignore[assignment]
    crate_mock.cargo_test.return_value = (True, "", "", "")

    tu = ast.create_translation_unit(_SIMPLE_FUNCTION)
    tree = ast.extract_info_c(tu)
    symbols, dependencies = get_symbols_and_dependencies([tree])
    sys_c.write_text(str(_SIMPLE_FUNCTION))

    translator(symbols=symbols, dependencies=dependencies)

    assert translator._oracle.baseline_failures == {"broken"}

    # The exclusion lowers the bar for the whole run, so it belongs in the bisectable log
    init_msg = crate_mock.vcs.commit.call_args_list[0].args[0]
    assert "broken" in init_msg


def _libtest_json(**events: str) -> str:
    return "\n".join(
        json.dumps({"type": "test", "name": f"smoke${name}", "event": event})
        for name, event in events.items()
    )


def _delta_translator(tmp_path: Path, *, broken_tests: set[str] | None = None):
    mock_translator = MagicMock(
        return_value=dspy.Prediction(translation=CodeRust("pub fn bar() {}"))
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )
    # The wrapper only has to keep the template's shape, so an implemented body passes
    unimplemented_stub = CodeRust("pub fn bar_wrapper() -> i32 { unimplemented!() }")
    symbol_wrapper = WrapperGenerator(
        wrapper=MagicMock(  # type: ignore[arg-type]
            return_value=MagicMock(
                return_value=dspy.Prediction(
                    wrapper=CodeRust("pub fn bar_wrapper() -> i32 { 0 }")
                )
            )
        ),
        max_iters=1,
    )

    translator, _, _, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=1,
        broken_tests=broken_tests,
    )
    sys_c.write_text("int bar(void) { return 0; }")
    return translator, unimplemented_stub


def _run_one_group(
    translator: RecurrentTranslator,
    unimplemented_stub: CodeRust,
    final_run: tuple,
) -> dspy.Prediction:
    crate_mock: MagicMock = translator.crate  # type: ignore[assignment]
    crate_mock.cargo_test.side_effect = [
        (True, "", "", 0),  # forward init gate
        (True, "", "", 0),  # _wrap_function fail-fast gate
        (True, "", "", 0),  # wrapper build gate
        (True, "", "", 0),  # _test_symbol build_only pre-pass
        final_run,
    ]

    sym: SymbolName = "c:@F@bar"
    mock_symbol = _mock_bar_symbol(sym)

    symbols: dict[SymbolName, MagicMock] = {sym: mock_symbol}
    dependencies: dict[SymbolGroup, list[SymbolGroup]] = {(sym,): []}

    with patch(
        "ideas.hybrid.generate_unimplemented_function_wrapper",
        return_value=unimplemented_stub,
    ):
        return translator(symbols=symbols, dependencies=dependencies)


def test_new_failure_beside_a_baseline_failure_is_blamed(tmp_path: Path) -> None:
    translator, stub = _delta_translator(tmp_path, broken_tests={"broken"})
    pred = _run_one_group(
        translator,
        stub,
        (False, _libtest_json(broken="failed", fresh="failed"), "2 tests failed", 101),
    )

    assert pred.regressions == {"fresh": ("c:@F@bar",)}

    crate_mock: MagicMock = translator.crate  # type: ignore[assignment]
    assert "Regressed test(s): fresh" in crate_mock.vcs.commit.call_args_list[-1].args[0]


def test_regression_is_reported_without_failing(tmp_path: Path) -> None:
    translator, stub = _delta_translator(tmp_path)
    pred = _run_one_group(translator, stub, (False, _libtest_json(fresh="failed"), "", 101))

    # The translation is complete, so only `regressions` says it diverged
    assert pred.complete
    assert pred.regressions == {"fresh": ("c:@F@bar",)}


def test_best_review_attempt_is_restored(tmp_path: Path) -> None:
    # Two attempts that both diverge, the second one regressing more than the first
    mock_translator = MagicMock(
        side_effect=[
            dspy.Prediction(translation=CodeRust("pub fn bar() { /* first */ }")),
            dspy.Prediction(translation=CodeRust("pub fn bar() { /* second */ }")),
        ]
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )
    unimplemented_stub = CodeRust("pub fn bar_wrapper() -> i32 { unimplemented!() }")
    symbol_wrapper = WrapperGenerator(
        wrapper=MagicMock(  # type: ignore[arg-type]
            return_value=MagicMock(
                return_value=dspy.Prediction(
                    wrapper=CodeRust("pub fn bar_wrapper() -> i32 { 0 }")
                )
            )
        ),
        max_iters=1,
    )

    translator, _, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=2,
    )
    sys_c.write_text("int bar(void) { return 0; }")

    crate_mock: MagicMock = translator.crate  # type: ignore[assignment]
    gates = [(True, "", "", 0)] * 3  # fail-fast, wrapper build, build_only pre-pass
    crate_mock.cargo_test.side_effect = [
        (True, "", "", 0),  # forward init gate
        *gates,
        (False, _libtest_json(one="failed", two="ok"), "1 test failed", 101),
        *gates,
        (False, _libtest_json(one="failed", two="failed"), "2 tests failed", 101),
    ]

    sym: SymbolName = "c:@F@bar"
    symbols: dict[SymbolName, MagicMock] = {sym: _mock_bar_symbol(sym)}
    dependencies: dict[SymbolGroup, list[SymbolGroup]] = {(sym,): []}

    with patch(
        "ideas.hybrid.generate_unimplemented_function_wrapper",
        return_value=unimplemented_stub,
    ):
        pred = translator(symbols=symbols, dependencies=dependencies)

    # Only the first attempt's regression is blamed, and its translation is what survives
    assert pred.regressions == {"one": (sym,)}
    assert "first" in rs_lib.read_text()


def test_best_review_attempt_survives_a_final_translate_failure(tmp_path: Path) -> None:
    # First attempt diverges, second fails to build: the group must keep the first
    mock_translator = MagicMock(
        side_effect=[
            dspy.Prediction(translation=CodeRust("pub fn bar() { /* first */ }")),
            dspy.Prediction(translation=CodeRust("pub fn bar() { /* second */ }")),
        ]
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )
    unimplemented_stub = CodeRust("pub fn bar_wrapper() -> i32 { unimplemented!() }")
    symbol_wrapper = WrapperGenerator(
        wrapper=MagicMock(  # type: ignore[arg-type]
            return_value=MagicMock(
                return_value=dspy.Prediction(
                    wrapper=CodeRust("pub fn bar_wrapper() -> i32 { 0 }")
                )
            )
        ),
        max_iters=1,
    )

    translator, _, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=2,
    )
    sys_c.write_text("int bar(void) { return 0; }")

    rs_crate_mock: MagicMock = translator.rust_crate  # type: ignore[assignment]
    rs_crate_mock.cargo_build.side_effect = [(True, ""), (False, "compilation error")]

    crate_mock: MagicMock = translator.crate  # type: ignore[assignment]
    crate_mock.cargo_test.side_effect = [
        (True, "", "", 0),  # forward init gate
        (True, "", "", 0),  # _wrap_function fail-fast gate
        (True, "", "", 0),  # wrapper build gate
        (True, "", "", 0),  # _test_symbol build_only pre-pass
        (False, _libtest_json(one="failed", two="ok"), "1 test failed", 101),
    ]

    sym: SymbolName = "c:@F@bar"
    symbols: dict[SymbolName, MagicMock] = {sym: _mock_bar_symbol(sym)}
    dependencies: dict[SymbolGroup, list[SymbolGroup]] = {(sym,): []}

    with patch(
        "ideas.hybrid.generate_unimplemented_function_wrapper",
        return_value=unimplemented_stub,
    ):
        pred = translator(symbols=symbols, dependencies=dependencies)

    # The unbuildable retry neither aborts the run nor discards the attempt that did run
    assert pred.complete
    assert pred.regressions == {"one": (sym,)}
    assert "first" in rs_lib.read_text()
    assert "second" not in rs_lib.read_text()


_STATIC_VARIABLE = ast.CodeC("static int counter = 0;")
_GLOBAL_VARIABLE = ast.CodeC("int counter = 0;")


@pytest.mark.parametrize(
    ("c_source", "is_externally_visible"),
    [(_STATIC_VARIABLE, False), (_GLOBAL_VARIABLE, True)],
    ids=["static", "global"],
)
def test_variable_wrapper_generated_without_tests(
    tmp_path: Path, c_source: CodeC, is_externally_visible: bool
) -> None:
    mock_translator = MagicMock(
        return_value=dspy.Prediction(translation=CodeRust("pub static mut COUNTER: i32 = 0;"))
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    # A variable wrapper is the only bridge between C storage and the unsafe-free -rs crate,
    # so it must be emitted even with no tests and no LLM wrapper generator.
    translator, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=None,
        tests=None,
        max_iters=1,
    )

    # Parse real C so the variable symbol carries genuine kind and linkage flags
    tu = ast.create_translation_unit(c_source)
    tree = ast.extract_info_c(tu)
    symbols, dependencies = get_symbols_and_dependencies([tree])
    assert [(s.is_variable, s.is_externally_visible) for s in symbols.values()] == [
        (True, is_externally_visible)
    ]

    sys_c.write_text(str(c_source))

    var_binding = CodeRust(
        'extern "C" {\n    pub static mut counter: ::std::os::raw::c_int;\n}'
    )
    with patch(
        "ideas.hybrid.bindgen_bindings", return_value={"counter": var_binding}
    ) as mock_bindgen:
        pred = translator(symbols=symbols, dependencies=dependencies)
    assert pred.complete

    mock_bindgen.assert_called_once_with(sys_c, ["counter"], translator._hybrid.types_code)

    # Pure bindgen output, so it is hoisted rather than given a wrapper of its own
    assert translator._hybrid.wrappers == {}
    assert translator._hybrid.externs == (var_binding,)
    assert str(wrap_values(var_binding)) in hybrid_lib.read_text()

    # The C variable needs external linkage for the bindings to resolve at link time
    assert "static" not in sys_c.read_text()

    # Bindings must not bleed into the -rs crate
    assert "pub static mut counter" not in rs_lib.read_text()


def test_variable_bindings_precede_translation(tmp_path: Path) -> None:
    # A static cannot be shadowed by a local binding of the same name (E0530), so a
    # binding that lands after a wrapper is accepted breaks code that no generator can still
    # edit. Every binding must be in the crate before the first symbol is translated.
    hybrid_lib = tmp_path / "hybrid" / "src" / "lib.rs"
    seen: list[str] = []

    def translate(*_args, **_kwargs) -> dspy.Prediction:
        seen.append(hybrid_lib.read_text())
        return dspy.Prediction(translation=CodeRust("pub static mut COUNTER: i32 = 0;"))

    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=MagicMock(side_effect=translate)),  # type: ignore[arg-type]
        max_iters=1,
    )
    translator, _, _, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=None,
        tests=None,
        max_iters=1,
    )

    tu = ast.create_translation_unit(_GLOBAL_VARIABLE)
    symbols, dependencies = get_symbols_and_dependencies([ast.extract_info_c(tu)])
    sys_c.write_text(str(_GLOBAL_VARIABLE))

    var_binding = CodeRust(
        'extern "C" {\n    pub static mut counter: ::std::os::raw::c_int;\n}'
    )
    with patch("ideas.hybrid.bindgen_bindings", return_value={"counter": var_binding}):
        assert translator(symbols=symbols, dependencies=dependencies).complete

    assert seen
    assert all(str(wrap_values(var_binding)) in crate_code for crate_code in seen)


_SYNC_FNS = ("sync_counter_to_rust", "sync_counter_to_c")

# Names both globals and calls both sync functions, which is the minimum the wrapper
# validator accepts
_ROUND_TRIP = (
    "unsafe { __c_globals::counter = 7; sync_counter_to_rust(); "
    "assert_eq!(foo_rs::COUNTER, 7); sync_counter_to_c(); "
    "assert_eq!(__c_globals::counter, 7); }"
)


def _variable_wrapper(
    body: str,
    tests_mod: str = "test_var_counter",
    round_trip: str | None = _ROUND_TRIP,
    sync_fns: tuple[str, ...] = _SYNC_FNS,
    sync_body: str = "",
) -> CodeRust:
    syncs = "\n".join(f"pub unsafe fn {fn}() {{ {sync_body} }}" for fn in sync_fns)
    round_trip_test = (
        "" if round_trip is None else f"#[test]\nfn round_trip_nontrivial() {{ {round_trip} }}"
    )
    return CodeRust(
        dedent(f"""
            {syncs}
            #[cfg(test)]
            mod {tests_mod} {{
                use super::*;

                #[test]
                fn initial_value_matches() {{
                    {body}
                }}

                {round_trip_test}
            }}
            """).strip()
    )


def test_variable_wrapper_tests_the_translated_global(tmp_path: Path) -> None:
    mock_translator = MagicMock(
        return_value=dspy.Prediction(translation=CodeRust("pub static COUNTER: i32 = 0;"))
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    tests_mod = "test_var_counter"
    var_wrapper = _variable_wrapper("assert_eq!(unsafe { counter }, foo_rs::COUNTER);")
    mock_wrapper_gen = MagicMock(return_value=dspy.Prediction(wrapper=var_wrapper))
    symbol_wrapper = WrapperGenerator(
        wrapper=MagicMock(return_value=mock_wrapper_gen),  # type: ignore[arg-type]
        max_iters=1,
    )

    translator, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=1,
    )
    crate_mock: MagicMock = translator.crate  # type: ignore[assignment]
    crate_mock.cargo_test.return_value = (True, "", "", "")

    tu = ast.create_translation_unit(_GLOBAL_VARIABLE)
    tree = ast.extract_info_c(tu)
    symbols, dependencies = get_symbols_and_dependencies([tree])

    sys_c.write_text(str(_GLOBAL_VARIABLE))

    var_binding = CodeRust(
        'extern "C" {\n    pub static mut counter: ::std::os::raw::c_int;\n}'
    )
    with (
        patch("ideas.hybrid.bindgen_bindings", return_value={"counter": var_binding}),
        patch(
            "ideas.translate_recurrent.generate_unimplemented_variable_wrapper",
            return_value=(var_wrapper, tests_mod, _SYNC_FNS),
        ),
    ):
        pred = translator(symbols=symbols, dependencies=dependencies)
    assert pred.complete

    # The extern is still hoisted; the wrapper adds the test on top of it
    assert translator._hybrid.externs == (var_binding,)
    assert translator._hybrid.wrappers == {"c:@counter": var_wrapper}
    assert str(var_wrapper) in hybrid_lib.read_text()

    # The test filter keys on the module name, so it has to reach cargo test verbatim
    assert tests_mod in [call.args[0] for call in crate_mock.cargo_test.call_args_list]

    # Tests must not bleed into the -rs crate
    assert tests_mod not in rs_lib.read_text()


# Each of these compiles and passes `cargo test`, so without an explicit check the
# wrapper would be accepted as correct.
@pytest.mark.parametrize(
    ("wrapper", "expected"),
    [
        (
            _variable_wrapper("assert_eq!(unsafe { counter }, 0);"),
            "Comparing the C global against itself",
        ),
        (
            # The sync functions name the translated crate, so only a body-scoped check
            # catches the vacuous assertion
            _variable_wrapper(
                "assert_eq!(unsafe { counter }, 0);",
                sync_body="unsafe { counter = foo_rs::COUNTER };",
            ),
            "Comparing the C global against itself",
        ),
        (
            _variable_wrapper(
                "unsafe { sync_counter_to_rust() };"
                "assert_eq!(unsafe { counter }, foo_rs::COUNTER);"
            ),
            "`initial_value_matches` must not call `sync_counter_to_rust`",
        ),
        (
            _variable_wrapper("let _ = foo_rs::COUNTER; todo!()"),
            "`todo!()` placeholder",
        ),
        (
            _variable_wrapper(
                "assert_eq!(unsafe { counter }, foo_rs::COUNTER);", tests_mod="tests"
            ),
            "must stay named `test_var_counter`",
        ),
        (
            CodeRust("#[cfg(test)]\nmod test_var_counter {\n    // foo_rs\n}"),
            "`initial_value_matches` must be implemented",
        ),
        (
            _variable_wrapper(
                "assert_eq!(unsafe { counter }, foo_rs::COUNTER);",
                sync_fns=("sync_counter_to_c",),
            ),
            "Keep all of `sync_counter_to_rust`, `sync_counter_to_c`",
        ),
        (
            _variable_wrapper(
                "assert_eq!(unsafe { counter }, foo_rs::COUNTER);", round_trip=None
            ),
            "`round_trip_nontrivial` must be implemented",
        ),
        (
            # Dropping the pair and rewriting the test not to call it leaves a test that
            # exercises nothing
            _variable_wrapper(
                "assert_eq!(unsafe { counter }, foo_rs::COUNTER);",
                round_trip="unsafe { counter = 7 };",
                sync_fns=(),
            ),
            "`round_trip_nontrivial` exists to test",
        ),
    ],
    ids=[
        "vacuous",
        "vacuous_with_sync_fns",
        "synced_before_compare",
        "unfinished",
        "renamed_test_module",
        "dropped_test",
        "dropped_sync_fn",
        "dropped_round_trip",
        "dropped_sync_pair_kept_round_trip",
    ],
)
def test_variable_wrapper_rejected_for_silent_failures(
    tmp_path: Path, wrapper: CodeRust, expected: str
) -> None:
    mock_translator = MagicMock(
        return_value=dspy.Prediction(translation=CodeRust("pub static COUNTER: i32 = 0;"))
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    mock_wrapper_gen = MagicMock(return_value=dspy.Prediction(wrapper=wrapper))

    # Two iterations so the rejection comes back as feedback on a second attempt
    symbol_wrapper = WrapperGenerator(
        wrapper=MagicMock(return_value=mock_wrapper_gen),  # type: ignore[arg-type]
        max_iters=2,
    )

    translator, _, _, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=1,
    )
    crate_mock: MagicMock = translator.crate  # type: ignore[assignment]
    crate_mock.cargo_test.return_value = (True, "", "", "")

    tu = ast.create_translation_unit(_GLOBAL_VARIABLE)
    tree = ast.extract_info_c(tu)
    symbols, dependencies = get_symbols_and_dependencies([tree])

    sys_c.write_text(str(_GLOBAL_VARIABLE))

    var_binding = CodeRust(
        'extern "C" {\n    pub static mut counter: ::std::os::raw::c_int;\n}'
    )
    with (
        patch("ideas.hybrid.bindgen_bindings", return_value={"counter": var_binding}),
        patch(
            "ideas.translate_recurrent.generate_unimplemented_variable_wrapper",
            return_value=(wrapper, "test_var_counter", _SYNC_FNS),
        ),
    ):
        translator(symbols=symbols, dependencies=dependencies)

    assert mock_wrapper_gen.call_count == 2
    assert expected in mock_wrapper_gen.call_args.kwargs["scope_feedback"]


# Both symbols are global definitions and reach `_wrap_type`, but neither is a
# STRUCT_DECL, so there is no field-by-field `to_rust`/`sync_to_c` pair to generate.
_UNION_TYPEDEF = ast.CodeC("typedef union { int x; } u_t;")

# The STRUCT_DECL here is only a forward declaration (no fields to convert) and the
# typedef merely aliases it, so neither symbol is wrappable.
_STRUCT_ALIAS_TYPEDEF = ast.CodeC("typedef struct Foo foo_alias_t;")


@pytest.mark.parametrize(
    "c_source",
    [_UNION_TYPEDEF, _STRUCT_ALIAS_TYPEDEF],
    ids=["union_typedef", "struct_alias_typedef"],
)
def test_no_type_wrapper_for_non_struct_definition(tmp_path: Path, c_source: CodeC) -> None:
    mock_translator = MagicMock(
        return_value=dspy.Prediction(translation=CodeRust("pub struct Placeholder;"))
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    mock_wrapper_gen = MagicMock()
    symbol_wrapper = WrapperGenerator(
        wrapper=MagicMock(return_value=mock_wrapper_gen),  # type: ignore[arg-type]
        max_iters=1,
    )

    translator, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=1,
    )
    crate_mock: MagicMock = translator.crate  # type: ignore[assignment]
    crate_mock.cargo_test.return_value = (True, "", "", "")

    tu = ast.create_translation_unit(c_source)
    tree = ast.extract_info_c(tu)
    symbols, dependencies = get_symbols_and_dependencies([tree])

    sys_c.write_text(str(c_source))

    mock_unimplemented = MagicMock()
    with patch(
        "ideas.translate_recurrent.generate_unimplemented_type_wrapper", mock_unimplemented
    ):
        pred = translator(symbols=symbols, dependencies=dependencies)

    # Translation still succeeds; only wrapping is skipped
    assert pred.complete

    # `_wrap_type` must bail out before seeding a template or invoking the generator
    assert mock_unimplemented.call_count == 0
    assert mock_wrapper_gen.call_count == 0
    assert translator._hybrid.wrappers == {}
    assert "impl CInterop for" not in hybrid_lib.read_text()
    assert "impl CInterop for" not in rs_lib.read_text()


# The typedef only restates the tag's name, so it lands in its own group with no Rust form
_REDUNDANT_TYPEDEF = ast.CodeC("""
struct Tag { int x; };
typedef struct Tag Tag;
int use_it(Tag *p) { return p->x; }
""")


def test_alias_typedef_group_is_skipped(tmp_path: Path) -> None:
    mock_translator = MagicMock(
        return_value=dspy.Prediction(translation=CodeRust("pub struct Placeholder;"))
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    translator, _, _, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=None,
        tests=None,
        max_iters=1,
    )

    tu = ast.create_translation_unit(_REDUNDANT_TYPEDEF)
    symbols, dependencies = get_symbols_and_dependencies([ast.extract_info_c(tu)])
    sys_c.write_text(str(_REDUNDANT_TYPEDEF))

    pred = translator(symbols=symbols, dependencies=dependencies)

    # A skipped group still counts as translated, so the run stays green
    assert pred.complete

    asked = [str(call.kwargs["snippet"]) for call in mock_translator.call_args_list]
    assert str(symbols["c:file.c@T@Tag"].code) not in asked
    assert str(symbols["c:@S@Tag"].code) in asked


# The struct is anonymous so the typedef carries the linkage `_wrap_type` demands
_BINARY = ast.CodeC("""
typedef struct { int x; } my_struct_t;
int helper(my_struct_t s) { return s.x; }
int main(void) { my_struct_t s = {1}; return helper(s); }
""")

_BINARY_TYPE_WRAPPER = CodeRust(
    dedent("""
        impl CInterop for my_struct_t {
            type Rust = foo_rs::MyStruct;
            unsafe fn to_rust(_cs: *const Self) -> Self::Rust { todo!() }
            unsafe fn sync_to_c(_rs: &Self::Rust, _cs: *mut Self) { todo!() }
        }

        #[cfg(test)]
        mod test_my_struct_t {
            #[test]
            fn round_trip_zeroed() { todo!() }
            #[test]
            fn round_trip_nontrivial() { todo!() }
        }
        """).strip()
)


def test_untested_binary_wraps_types_but_not_functions(tmp_path: Path) -> None:
    mock_translator = MagicMock(
        return_value=dspy.Prediction(translation=CodeRust("pub fn helper() -> i32 { 1 }"))
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    mock_wrapper_gen = MagicMock(return_value=dspy.Prediction(wrapper=_BINARY_TYPE_WRAPPER))
    symbol_wrapper = WrapperGenerator(
        wrapper=MagicMock(return_value=mock_wrapper_gen),  # type: ignore[arg-type]
        max_iters=1,
    )

    translator, _, _, sys_c = _make_translator(
        tmp_path,
        is_bin=True,
        c_src=str(_BINARY),
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests=None,
        max_iters=1,
    )
    crate_mock: MagicMock = translator.crate  # type: ignore[assignment]
    crate_mock.cargo_test.return_value = (True, "", "", "")

    tu = ast.create_translation_unit(_BINARY)
    tree = ast.extract_info_c(tu)
    symbols, dependencies = get_symbols_and_dependencies(
        [tree], external_symbol_names=["c:@F@main"]
    )
    assert symbols["c:@F@helper"].is_externally_visible
    (struct_name,) = [name for name, symbol in symbols.items() if symbol.is_struct]

    mock_unimplemented = MagicMock()
    with (
        patch(
            "ideas.hybrid.generate_unimplemented_function_wrapper",
            mock_unimplemented,
        ),
        patch(
            "ideas.translate_recurrent.generate_unimplemented_type_wrapper",
            return_value=(_BINARY_TYPE_WRAPPER, "test_my_struct_t"),
        ),
    ):
        pred = translator(symbols=symbols, dependencies=dependencies)
    assert pred.complete

    # `helper` keeps its C definition: no wrapper claims it and no test needs it to
    assert mock_unimplemented.call_count == 0
    assert "int helper(my_struct_t s) { return s.x; }" in sys_c.read_text()

    # The struct is still wrapped: its round-trip tests validate the translated type
    # regardless of whether anything calls into Rust through a function wrapper
    assert translator._hybrid.wrappers == {struct_name: _BINARY_TYPE_WRAPPER}
    assert mock_wrapper_gen.call_count == 1

    # `main` is still wrapped, handing the entrypoint to the -rs crate
    assert translator._hybrid.main_wrapped
    transformed = sys_c.read_text()
    assert "extern int main(int argc, char **argv);" in transformed
    assert "extern int __ideas_entry(int argc, char **argv);" in transformed
    assert "extern int __ideas_c_main(void);" in transformed


# Shaped like the types segment of bindgen output: attributes are siblings of the item
# they decorate, and enums arrive with companion constants that must travel with them
_BINDGEN_TYPES = CodeRust(
    dedent("""
        #[repr(C)]
        #[derive(Debug, Copy, Clone)]
        pub struct house_t {
            pub floors: ::std::os::raw::c_int,
        }
        pub type house_alias_t = house_t;
        pub const tag_t_A: tag_t = 0;
        pub type tag_t = ::std::os::raw::c_uint;
        """).strip()
)

_BINDGEN_BINDING = CodeRust(
    dedent("""
        unsafe extern "C" {
            pub static mut the_house: house_t;
        }
        """).strip()
)


def test_type_wrapper_context_drops_impl_method_bodies() -> None:
    wrapper = CodeRust(
        dedent("""
            impl CInterop for house_t {
                type Rust = libdriver_rs::HouseT;

                unsafe fn to_rust(cs: *const Self) -> Self::Rust {
                    libdriver_rs::HouseT { floors: unsafe { (*cs).floors } as i32 }
                }

                unsafe fn sync_to_c(rs: &Self::Rust, cs: *mut Self) {
                    unsafe { (*cs).floors = rs.floors as ::std::os::raw::c_int };
                }
            }

            #[cfg(test)]
            mod test_house_t {
                #[test]
                fn round_trip_zeroed() { todo!() }
            }
            """).strip()
    )

    context = str(TranslationContext._build_type_wrapper_context(wrapper))

    # The impl header and the associated type are the only per-type information ...
    assert "impl CInterop for house_t {" in context
    assert "type Rust = libdriver_rs::HouseT;" in context

    # ... the method signatures are fixed by the trait, which the rendered types already show
    assert "fn to_rust" not in context
    assert "fn sync_to_c" not in context
    assert "libdriver_rs::HouseT { floors" not in context
    assert "mod test_house_t" not in context


def test_wrapper_context_reduces_type_impls_to_their_headers() -> None:
    type_wrapper = CodeRust(
        dedent("""
            impl CInterop for house_t {
                type Rust = libdriver_rs::HouseT;
                unsafe fn to_rust(_cs: *const Self) -> Self::Rust { todo!() }
                unsafe fn sync_to_c(_rs: &Self::Rust, _cs: *mut Self) { todo!() }
            }
            """).strip()
    )
    global_wrapper = CodeRust("pub fn counter_wrapper() -> i32 { 0 }")

    # Pinned so the split under test is not masked by the reduced-context filter
    with patch("ideas.translate_context.REDUCED_CONTEXT", False):
        wrappers = TranslationContext._build_wrapper_context(
            {
                "c:@S@house_t": type_wrapper,
                "c:@counter": global_wrapper,
            },
            {
                "c:@S@house_t": MagicMock(bindgen_name="house_t", is_type=True),
                "c:@counter": MagicMock(
                    bindgen_name="counter", is_type=False, is_variable=False
                ),
            },
        )

    context = str(CodeRust.join(wrappers.values()))

    # Nothing is reachable by module path any more, so no wrapper carries scaffolding
    assert "pub mod" not in context
    assert "impl CInterop for house_t {\n    type Rust = libdriver_rs::HouseT;\n}" in context
    assert str(global_wrapper).strip() in context

    # Every kept wrapper is keyed by its bindgen name so the type slice can allowlist it
    assert tuple(wrappers) == ("house_t", "counter")


def test_wrapper_context_omits_spellings_of_dropped_wrappers() -> None:
    global_wrapper = CodeRust("pub fn counter_wrapper() -> i32 { 0 }")

    with patch("ideas.translate_context.REDUCED_CONTEXT", True):
        wrappers = TranslationContext._build_wrapper_context(
            {"c:@counter": global_wrapper},
            {"c:@counter": MagicMock(bindgen_name="counter", is_type=False, is_variable=False)},
        )

    assert wrappers == {}


# These tests only exercise the bindings plumbing, so no LLM modules are needed
def _make_bindings_translator(tmp_path: Path):
    return _make_translator(tmp_path, symbol_translator=MagicMock(), symbol_wrapper=None)


def test_bindings_handle_type_free_bindings(tmp_path: Path) -> None:
    translator, _, _, _ = _make_bindings_translator(tmp_path)

    item = CodeRust('unsafe extern "C" {\n    pub static mut counter: i32;\n}\n')
    translator._hybrid.add_externs([item])

    # Nothing was hoisted alongside it, so only the trait and the values module render
    assert translator._hybrid.types == {}
    assert str(translator._hybrid.render()) == str(C_INTEROP_TRAIT + wrap_values(item))


def test_crate_context_always_exposes_the_interop_trait(tmp_path: Path) -> None:
    translator, _, _, _ = _make_bindings_translator(tmp_path)

    # Wrappers implement this trait, so it must be visible before anything is hoisted
    assert "pub trait CInterop" in str(translator._hybrid.render([CodeRust("")]))

    types, consts = split_items(_BINDGEN_TYPES)
    translator._hybrid.set_types(types, consts)
    translator._hybrid.add_externs([_BINDGEN_BINDING])
    context = str(translator._hybrid.render([CodeRust("")]))
    assert context.startswith("pub trait CInterop")
    assert "pub struct house_t {" in context

    # Reduced wrappers sit alongside the types they convert, at the same level
    impl = CodeRust("impl CInterop for house_t {\n    type Rust = libdriver_rs::HouseT;\n}")
    assert "impl CInterop for house_t {" in str(translator._hybrid.render([impl]))


def test_crate_context_can_narrow_the_types_it_shows(tmp_path: Path) -> None:
    translator, _, _, _ = _make_bindings_translator(tmp_path)
    types, consts = split_items(_BINDGEN_TYPES)
    translator._hybrid.set_types(types, consts)
    translator._hybrid.add_externs([_BINDGEN_BINDING])

    # The crate on disk takes no override, so it keeps every type
    assert "pub struct house_t {" in str(translator._hybrid.render())

    sliced = CodeRust("pub type tag_t = ::std::os::raw::c_uint;")
    context = str(translator._hybrid.render([CodeRust("")], [sliced]))

    # Only the types segment narrows; the trait and the externs are still needed in full
    assert "pub struct house_t {" not in context
    assert str(sliced) in context
    assert "pub trait CInterop" in context
    assert "pub static mut the_house" in context


def test_type_slice_keeps_only_what_the_symbol_reaches(tmp_path: Path) -> None:
    translator, _, _, _ = _make_bindings_translator(tmp_path)
    types, consts = split_items(_BINDGEN_TYPES)
    translator._hybrid.set_types(types, consts)

    sliced, _ = translator._hybrid.type_slice(BindgenName("house_alias_t"), CodeRust())

    # The alias drags in the struct it names, but nothing reaches the unrelated enum
    text = str(CodeRust.join(sliced))
    assert "pub type house_alias_t" in text
    assert "pub struct house_t {" in text
    assert "tag_t" not in text


def test_type_slice_keeps_what_the_unimplemented_wrapper_names(tmp_path: Path) -> None:
    translator, _, _, _ = _make_bindings_translator(tmp_path)
    types, consts = split_items(_BINDGEN_TYPES)
    translator._hybrid.set_types(types, consts)
    translator._hybrid.add_externs([_BINDGEN_BINDING])

    wrapper = CodeRust("impl CInterop for tag_t {}")
    sliced_types, sliced_consts = translator._hybrid.type_slice(BindgenName("paint"), wrapper)

    # The extern block and the wrapper render in full, so their types cannot be dropped
    assert "pub struct house_t {" in str(CodeRust.join(sliced_types))
    # A const rides with the type it names, so the same slice carries it
    assert "pub const tag_t_A" in str(CodeRust.join(sliced_consts))


def test_type_slice_keeps_everything_without_reduced_context(tmp_path: Path) -> None:
    translator, _, _, _ = _make_bindings_translator(tmp_path)
    types, consts = split_items(_BINDGEN_TYPES)
    translator._hybrid.set_types(types, consts)

    with patch("ideas.hybrid.REDUCED_CONTEXT", False):
        sliced, sliced_consts = translator._hybrid.type_slice(
            BindgenName("house_alias_t"), CodeRust()
        )

    assert str(CodeRust.join(sliced)) == str(CodeRust.join(translator._hybrid.types.values()))
    assert str(CodeRust.join(sliced_consts)) == str(
        CodeRust.join(translator._hybrid.consts.values())
    )


def test_crate_types_keep_enums_as_struct_references(tmp_path: Path) -> None:
    source = (
        "enum color { RED, GREEN };\n"
        "struct paint { enum color hue; };\n"
        "void use_paint(struct paint *p);\n"
    )
    c_path = tmp_path / "input.c"
    c_path.write_text(source)
    _, _, symbols = _make_graph(CodeC(source))

    # Mirrors the crate-wide run in `forward`: allowlist every symbol
    types = bindgen_types(c_path, [n for s in symbols.values() if (n := s.bindgen_name)])

    # The enum counts as a type, so it survives the blocklist and the struct field resolves
    assert "pub struct paint" in str(types)
    assert "pub type color" in str(types)


def test_wrapper_inputs_keep_the_two_crates_apart(tmp_path: Path) -> None:
    translation = CodeRust("pub struct MyStructT { pub x: i32 }")
    mock_translator = MagicMock(return_value=dspy.Prediction(translation=translation))
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    # The types the wrapper names come from the crate-wide bindgen run, so only the impl
    # itself is stubbed here
    type_wrapper = CodeRust(
        dedent("""
            impl CInterop for my_struct_t {
                type Rust = libfoo_rs::my_struct_t;
                unsafe fn to_rust(_cs: *const Self) -> Self::Rust { todo!() }
                unsafe fn sync_to_c(_rs: &Self::Rust, _cs: *mut Self) { todo!() }
            }

            #[cfg(test)]
            mod tests {
                #[test]
                fn round_trip_zeroed() { todo!() }
                #[test]
                fn round_trip_nontrivial() { todo!() }
            }
            """).strip()
    )
    # The generator echoes back whatever `_wrap_type` wrote to disk so wrapping succeeds
    mock_wrapper_gen = MagicMock(
        side_effect=lambda **kwargs: dspy.Prediction(wrapper=kwargs["example_wrapper"])
    )
    symbol_wrapper = WrapperGenerator(
        wrapper=MagicMock(return_value=mock_wrapper_gen),  # type: ignore[arg-type]
        max_iters=1,
    )

    translator, _, _, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=1,
    )
    crate_mock: MagicMock = translator.crate  # type: ignore[assignment]
    crate_mock.cargo_test.return_value = (True, "", "", "")

    tu = ast.create_translation_unit(_ANONYMOUS_TYPEDEF_STRUCT)
    tree = ast.extract_info_c(tu)
    symbols, dependencies = get_symbols_and_dependencies([tree])

    sys_c.write_text(str(_ANONYMOUS_TYPEDEF_STRUCT))

    with patch(
        "ideas.translate_recurrent.generate_unimplemented_type_wrapper",
        return_value=(type_wrapper, "test_my_struct_t"),
    ):
        pred = translator(symbols=symbols, dependencies=dependencies)
    assert pred.complete

    kwargs = mock_wrapper_gen.call_args.kwargs
    crate, wrapped = str(kwargs["crate"]), str(kwargs["wrapped_crate_code"])

    # Mixing these in one field is what made the LLM write `crate::MyStructT`
    assert "pub struct my_struct_t" in crate
    assert "pub struct MyStructT" not in crate
    assert "pub struct MyStructT" in wrapped
    assert "pub struct my_struct_t" not in wrapped
