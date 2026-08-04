#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

import dspy
import networkx as nx

from ideas import ast
from ideas.ast import CodeC
from ideas.ast_rust import CodeRust
from ideas.consolidate import get_symbols_and_dependencies, create_ast_order
from ideas.translate_recurrent import RecurrentTranslator
from ideas.translate_recurrent import SymbolGroup, SymbolName, TranslationContext
from ideas.translate_snippet import SnippetTranslator
from ideas.wrapper import WrapperGenerator


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


def test_context_isolated_node():
    G, groups, symbols = _make_graph(ast.CodeC("int standalone(void) { return 42; }"))

    (func_group,) = groups  # only one symbol in this translation unit

    ctx = TranslationContext.build(G, func_group, groups, symbols)

    # An isolated node has no dependents, no dependencies, and nothing translated yet.
    assert str(ctx.dependent_code) == ""
    assert str(ctx.support_code) == ""
    assert str(ctx.crate_code) == ""
    assert str(ctx.reference_code) == ""


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
    assert symbols["c:file.c@T@my_struct_t"].code in ctx.dependent_code
    # use_my consumes the struct (via typedef); its call signature reveals whether
    # the type is passed by value or pointer, which informs Copy/Clone/borrowing decisions
    assert symbols["c:@F@use_my"].code in ctx.dependent_code


def test_context_linear_chain_middle():
    G, groups, symbols = _make_graph(_TYPEDEF_CHAIN)

    struct_group: SymbolGroup = ("c:@S@MyStruct",)
    typedef_group: SymbolGroup = ("c:file.c@T@my_struct_t",)

    struct_translation = CodeRust("pub struct MyStruct { pub x: i32 }")
    translations: dict[SymbolGroup, CodeRust] = {struct_group: struct_translation}

    ctx = TranslationContext.build(G, typedef_group, groups, symbols, translations=translations)

    # function is the immediate predecessor of typedef in the dependency graph
    assert symbols["c:@F@use_my"].code in ctx.dependent_code
    # struct is the immediate successor of typedef → full code used
    assert symbols["c:@S@MyStruct"].code in ctx.support_code
    # struct has been translated so its Rust code appears in crate_code
    assert struct_translation in ctx.crate_code


def test_context_nested_struct_fields():
    G, groups, symbols = _make_graph(_NESTED_STRUCT)

    inner_group: SymbolGroup = ("c:@S@Inner",)

    ctx = TranslationContext.build(G, inner_group, groups, symbols)

    # Outer embeds Inner — should be in dependent_code
    assert symbols["c:@S@Outer"].code in ctx.dependent_code
    # process_outer shows how Outer (and by extension Inner) is used via pointer,
    # informing borrowing semantics for Inner's translation
    assert symbols["c:@F@process_outer"].code in ctx.dependent_code


def test_context_nested_typedefs():
    G, groups, symbols = _make_graph(_NESTED_TYPEDEF)

    struct_group: SymbolGroup = ("c:@S@Point",)

    ctx = TranslationContext.build(G, struct_group, groups, symbols)

    # point_t is 1 hop — should be in dependent_code
    assert symbols["c:file.c@T@point_t"].code in ctx.dependent_code
    # point_ptr_t aliases a pointer to point_t — still part of the usage chain
    assert symbols["c:file.c@T@point_ptr_t"].code in ctx.dependent_code
    # use_point is the actual consumer — reveals how the pointer type is used
    assert symbols["c:@F@use_point"].code in ctx.dependent_code


def test_context_enum_typedef():
    G, groups, symbols = _make_graph(_ENUM_TYPEDEF)

    enum_group: SymbolGroup = ("c:@E@Status",)

    ctx = TranslationContext.build(G, enum_group, groups, symbols)

    # status_t is the immediate predecessor — should be in dependent_code
    assert symbols["c:file.c@T@status_t"].code in ctx.dependent_code
    # check_status uses the enum; its usage informs how Status should be
    # represented in Rust (e.g., as a plain enum vs. integer newtype)
    assert symbols["c:@F@check_status"].code in ctx.dependent_code


def test_context_variable_intermediate():
    G, groups, symbols = _make_graph(_VAR_INTERMEDIATE)

    struct_group: SymbolGroup = ("c:@S@Config",)

    ctx = TranslationContext.build(G, struct_group, groups, symbols)

    # global_config is the immediate predecessor — should be in dependent_code
    assert symbols["c:@global_config"].code in ctx.dependent_code
    # get_timeout reveals that Config backs global state, which informs
    # Rust's ownership and synchronization strategy (Mutex, OnceCell, etc.)
    assert symbols["c:@F@get_timeout"].code in ctx.dependent_code


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
    assert a_translation in ctx.reference_code


def _make_translator(tmp_path: Path, **kwargs) -> tuple[RecurrentTranslator, Path, Path, Path]:
    # -sys crate
    sys_src_dir = tmp_path / "sys" / "src"
    sys_src_dir.mkdir(parents=True)
    sys_lib = sys_src_dir / "lib.rs"
    sys_lib.write_bytes(b"")
    sys_lib.with_suffix(".c").write_bytes(b"")

    sys_crate = MagicMock()
    sys_crate.lib_src_path = sys_lib
    sys_crate.lib_name = "libfoo_sys"

    # -rs crate
    rs_src_dir = tmp_path / "rs" / "src"
    rs_src_dir.mkdir(parents=True)
    (rs_src_dir / "lib.rs").write_bytes(b"")

    rs_crate = MagicMock()
    rs_crate.lib_src_path = rs_src_dir / "lib.rs"
    rs_crate.main_src_path = None
    rs_crate.lib_name = "foo_rs"
    rs_crate.cargo_build.return_value = (True, "")

    # hybrid crate
    hybrid_src_dir = tmp_path / "hybrid" / "src"
    hybrid_src_dir.mkdir(parents=True)
    (hybrid_src_dir / "lib.rs").write_bytes(b"")

    crate = MagicMock()
    crate.lib_src_path = hybrid_src_dir / "lib.rs"
    crate.main_src_path = None
    crate.src_dir = hybrid_src_dir
    crate.cargo_toml = tmp_path / "hybrid" / "Cargo.toml"
    crate.cargo_build.return_value = (True, "")

    # RecurrentTranslator with mocked inputs
    translator = RecurrentTranslator(
        sys_crate=sys_crate,
        crate=crate,
        rs_crate=rs_crate,
        **kwargs,
    )
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
    # because we patch ideas.translate_recurrent.generate_unimplemented_function_wrapper below
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
    mock_symbol = MagicMock()
    mock_symbol.name = sym
    mock_symbol.spelling = "bar"
    mock_symbol.code = CodeC("int bar(void) { return 0; }")
    mock_symbol.llm_context_declaration = "int bar(void);"
    mock_symbol.is_function = True
    mock_symbol.is_definition = True
    mock_symbol.is_global = False
    mock_symbol.is_variable = False
    mock_symbol.is_type = False

    symbols: dict[SymbolName, MagicMock] = {sym: mock_symbol}
    dependencies: dict[SymbolGroup, list[SymbolGroup]] = {sym_group: []}

    # Write the C definition that clang_make_extern_ will extern-ify
    sys_c.write_text("int bar(void) { return 0; }")

    # Create wrapper file with known content
    wrap_bar = hybrid_lib.parent / "wrap_bar.rs"
    original_content = "// previous good wrapper"
    wrap_bar.write_text(original_content)

    # Run the translator
    with patch(
        "ideas.translate_recurrent.generate_unimplemented_function_wrapper",
        return_value=CodeRust("pub fn bar() { unimplemented!() }"),
    ):
        pred = translator(symbols=symbols, dependencies=dependencies)
    assert pred.success

    # Translator should have iterated twice
    assert mock_translator.call_count == 2
    assert mock_wrapper.call_count == 2

    # Must have rolled back wrap_bar.rs to its pre-call state
    assert wrap_bar.read_text() == original_content

    # Must not have duplicated `pub mod wrap_bar;` across retries
    assert hybrid_lib.read_text().count("pub mod wrap_bar;") == 1

    # Must not have created spurious wrapper files across retries
    assert list(hybrid_lib.parent.glob("wrap_*.rs")) == [wrap_bar]

    # Must not have duplicated the translation in the -rs crate across retries
    assert rs_lib.read_text().count("pub fn bar") == 1

    # Final try does not restore the C file because translation succeeded
    assert "extern int bar(void);" in sys_c.read_text()
    assert "{ return 0; }" not in sys_c.read_text()

    # Translation must not bleed into the hybrid crate
    assert "pub fn bar" not in hybrid_lib.read_text()

    # Module declaration must not bleed into the -rs crate
    assert "pub mod wrap_bar;" not in rs_lib.read_text()


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
    mock_symbol = MagicMock()
    mock_symbol.name = sym
    mock_symbol.spelling = "bar"
    mock_symbol.code = CodeC("int bar(void) { return 0; }")
    mock_symbol.llm_context_declaration = "int bar(void);"
    mock_symbol.is_function = True
    mock_symbol.is_definition = True
    mock_symbol.is_global = False
    mock_symbol.is_variable = False
    mock_symbol.is_type = False

    symbols: dict[SymbolName, MagicMock] = {sym: mock_symbol}
    dependencies: dict[SymbolGroup, list[SymbolGroup]] = {sym_group: []}

    # Write known content so the restore assertions check for something specific,
    # not just whatever RecurrentTranslator.__init__ happened to leave behind.
    initial_rs_lib = "#![forbid(unsafe_code)]\n\n// known rs baseline\n"
    initial_hybrid_lib = "use libfoo_sys as _;\n// known hybrid baseline\n"
    initial_c_src = "int bar(void) { return 0; }"
    original_wrapper_content = "// previous good wrapper"
    rs_lib.write_text(initial_rs_lib)
    hybrid_lib.write_text(initial_hybrid_lib)
    sys_c.write_text(initial_c_src)
    wrap_bar = hybrid_lib.parent / "wrap_bar.rs"
    wrap_bar.write_text(original_wrapper_content)

    # Run the translator
    pred = translator(symbols=symbols, dependencies=dependencies)
    assert not pred.success

    # Both iterations must have attempted translation
    assert mock_translator.call_count == 2

    # Full restore: -rs crate must be rolled back to its pre-call state
    assert rs_lib.read_text() == initial_rs_lib

    # Full restore: hybrid crate must be rolled back to its pre-call state
    assert hybrid_lib.read_text() == initial_hybrid_lib

    # Full restore: C source must be rolled back (clang_make_extern_ must not have run)
    assert sys_c.read_text() == initial_c_src

    # Full restore: the pre-existing wrapper file must be intact
    assert wrap_bar.read_text() == original_wrapper_content

    # No spurious wrapper files must have been created across the two iterations
    assert list(hybrid_lib.parent.glob("wrap_*.rs")) == [wrap_bar]

    # Translation must not bleed into the hybrid crate across retries
    assert "pub fn bar" not in hybrid_lib.read_text()

    # Module declaration must not bleed into the -rs crate across retries
    assert "pub mod wrap_bar;" not in rs_lib.read_text()


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

    # Returning the same CodeRust as the unimplemented wrapper means validate_changes
    # finds no diffs → scope_feedback is empty → pred.success = True → wrapping succeeds.
    unimplemented_stub = CodeRust("pub fn bar_wrapper() -> i32 { unimplemented!() }")
    mock_wrapper_gen = MagicMock(return_value=dspy.Prediction(wrapper=unimplemented_stub))
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

    # Make cargo_test fail so result.failure == "test"
    crate_mock: MagicMock = translator.crate  # type: ignore[assignment]
    crate_mock.cargo_test.return_value = (
        False,
        '{"type":"test","name":"test_bar","event":"failed"}',
        "1 test failed",
        "",
    )

    sym: SymbolName = "c:@F@bar"
    sym_group: SymbolGroup = (sym,)
    mock_symbol = MagicMock()
    mock_symbol.name = sym
    mock_symbol.spelling = "bar"
    mock_symbol.code = CodeC("int bar(void) { return 0; }")
    mock_symbol.llm_context_declaration = "int bar(void);"
    mock_symbol.is_function = True
    mock_symbol.is_definition = True
    mock_symbol.is_global = False
    mock_symbol.is_variable = False
    mock_symbol.is_type = False

    symbols: dict[SymbolName, MagicMock] = {sym: mock_symbol}
    dependencies: dict[SymbolGroup, list[SymbolGroup]] = {sym_group: []}

    sys_c.write_text("int bar(void) { return 0; }")

    # Run the translator
    with patch(
        "ideas.translate_recurrent.generate_unimplemented_function_wrapper",
        return_value=unimplemented_stub,
    ):
        pred = translator(symbols=symbols, dependencies=dependencies)
    assert pred.success

    # No restore: translation is kept in the -rs crate
    assert "pub fn bar" in rs_lib.read_text()

    # No restore: wrapper module declaration is kept in the hybrid crate
    assert "pub mod wrap_bar;" in hybrid_lib.read_text()

    # No restore: C source is kept with the extern declaration written by clang_make_extern_
    assert "extern int bar(void);" in sys_c.read_text()

    # No restore: wrapper file written during wrapping is kept
    wrap_bar = hybrid_lib.parent / "wrap_bar.rs"
    assert wrap_bar.exists()


def test_feedback_after_test_failure(tmp_path: Path) -> None:
    # Translation and wrapping succeed on both iterations
    mock_translator = MagicMock(
        return_value=dspy.Prediction(translation=CodeRust("pub fn bar() {}"))
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    # Returning the same CodeRust as the unimplemented wrapper means validate_changes passes
    unimplemented_stub = CodeRust("pub fn bar_wrapper() -> i32 { unimplemented!() }")
    mock_wrapper_gen = MagicMock(return_value=dspy.Prediction(wrapper=unimplemented_stub))
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

    # First cargo_test call fails, second succeeds.
    crate_mock: MagicMock = translator.crate  # type: ignore[assignment]
    crate_mock.cargo_test.side_effect = [
        (False, '{"type":"test","name":"test_bar","event":"failed"}', "1 test failed", ""),
        (True, '{"type":"test","name":"test_bar","event":"ok"}', "", ""),
    ]

    sym: SymbolName = "c:@F@bar"
    sym_group: SymbolGroup = (sym,)
    mock_symbol = MagicMock()
    mock_symbol.name = sym
    mock_symbol.spelling = "bar"
    mock_symbol.code = CodeC("int bar(void) { return 0; }")
    mock_symbol.llm_context_declaration = "int bar(void);"
    mock_symbol.is_function = True
    mock_symbol.is_definition = True
    mock_symbol.is_global = False
    mock_symbol.is_variable = False
    mock_symbol.is_type = False

    symbols: dict[SymbolName, MagicMock] = {sym: mock_symbol}
    dependencies: dict[SymbolGroup, list[SymbolGroup]] = {sym_group: []}

    sys_c.write_text("int bar(void) { return 0; }")

    # Run the translator
    with patch(
        "ideas.translate_recurrent.generate_unimplemented_function_wrapper",
        return_value=unimplemented_stub,
    ):
        pred = translator(symbols=symbols, dependencies=dependencies)
    assert pred.success

    # Translator must have been called twice: once per outer iteration.
    assert mock_translator.call_count == 2

    # Extract the feedback forwarded to the translator on the second (retry) call.
    second_call_feedback: str = mock_translator.call_args_list[1].kwargs["feedback"]

    # Test failures should tell the translator its output doesn't match the C behavior.
    assert "does not match the behavior" in second_call_feedback


def test_feedback_after_wrap_failure(tmp_path: Path) -> None:
    # Translation succeeds on both iterations
    mock_translator = MagicMock(
        return_value=dspy.Prediction(translation=CodeRust("pub fn bar() {}"))
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    # Returning the same CodeRust as the unimplemented wrapper means validate_changes passes
    unimplemented_stub = CodeRust("pub fn bar_wrapper() -> i32 { unimplemented!() }")
    mock_wrapper_gen = MagicMock(return_value=dspy.Prediction(wrapper=unimplemented_stub))
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
        (True, ""),  # _wrap_function scaffold check (iter 1)
        (False, "build error"),  # build(wrapper) inside WrapperGenerator (iter 1)
        (True, ""),  # _wrap_function scaffold check (iter 2)
        (True, ""),  # build(wrapper) inside WrapperGenerator (iter 2)
        (True, ""),  # _test_symbol pre-test cargo_build (iter 2)
    ]
    crate_mock.cargo_test.return_value = (True, "", "", "")

    sym: SymbolName = "c:@F@bar"
    sym_group: SymbolGroup = (sym,)
    mock_symbol = MagicMock()
    mock_symbol.name = sym
    mock_symbol.spelling = "bar"
    mock_symbol.code = CodeC("int bar(void) { return 0; }")
    mock_symbol.llm_context_declaration = "int bar(void);"
    mock_symbol.is_function = True
    mock_symbol.is_definition = True
    mock_symbol.is_global = False
    mock_symbol.is_variable = False
    mock_symbol.is_type = False

    symbols: dict[SymbolName, MagicMock] = {sym: mock_symbol}
    dependencies: dict[SymbolGroup, list[SymbolGroup]] = {sym_group: []}

    sys_c.write_text("int bar(void) { return 0; }")

    # Run the translator
    with patch(
        "ideas.translate_recurrent.generate_unimplemented_function_wrapper",
        return_value=unimplemented_stub,
    ):
        pred = translator(symbols=symbols, dependencies=dependencies)
    assert pred.success

    # Translator must have been called twice: once per outer iteration.
    assert mock_translator.call_count == 2

    # Extract the feedback forwarded to the translator on the second (retry) call.
    second_call_feedback: str = mock_translator.call_args_list[1].kwargs["feedback"]

    # Wrap failures should instruct the translator to produce wrapper-friendly code.
    assert "C-compatible FFI wrapper" in second_call_feedback


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
    mock_symbol = MagicMock()
    mock_symbol.name = sym
    mock_symbol.spelling = "bar"
    mock_symbol.code = CodeC("int bar(void) { return 0; }")
    mock_symbol.llm_context_declaration = "int bar(void);"
    mock_symbol.is_function = True
    mock_symbol.is_definition = True
    mock_symbol.is_global = False
    mock_symbol.is_variable = False
    mock_symbol.is_type = False

    symbols: dict[SymbolName, MagicMock] = {sym: mock_symbol}
    dependencies: dict[SymbolGroup, list[SymbolGroup]] = {sym_group: []}

    sys_c.write_text("int bar(void) { return 0; }")

    pred = translator(symbols=symbols, dependencies=dependencies)

    assert pred.success

    # Translator must have been called twice: once per outer iteration.
    assert mock_translator.call_count == 2

    # Extract the feedback forwarded to the translator on the second (retry) call.
    second_call_feedback: str = mock_translator.call_args_list[1].kwargs["feedback"]

    # The build error from the first iteration must be surfaced to the retry so the
    # translator knows why its previous output was rejected.
    assert "compile error" in second_call_feedback


_TYPEDEF_STRUCT = ast.CodeC("typedef struct MyStruct { int x; } my_struct_t;")

# An unnamed struct still gets external linkage: C11 6.7.8p3 makes the typedef name the
# tag's name for linkage purposes, so clang reports spelling `my_struct_t` for the
# STRUCT_DECL and the wrapper is named after the typedef instead of the (absent) tag.
_ANONYMOUS_TYPEDEF_STRUCT = ast.CodeC("typedef struct { int x; } my_struct_t;")


@pytest.mark.parametrize(
    ("c_source", "wrapper_name"),
    [
        (_TYPEDEF_STRUCT, "wrap_MyStruct"),
        (_ANONYMOUS_TYPEDEF_STRUCT, "wrap_my_struct_t"),
    ],
    ids=["named_tag", "anonymous_tag"],
)
def test_type_wrapper_generated_for_typedef_struct(
    tmp_path: Path, c_source: CodeC, wrapper_name: str
) -> None:
    mock_translator = MagicMock(
        return_value=dspy.Prediction(translation=CodeRust("pub struct MyStruct { pub x: i32 }"))
    )
    symbol_translator = SnippetTranslator(
        translator=MagicMock(return_value=mock_translator),  # type: ignore[arg-type]
        max_iters=1,
    )

    # Returning the unimplemented template unchanged means validate_changes finds no
    # diffs, so wrapping succeeds. Both round-trip tests must be present because
    # `_wrap_type` rejects wrappers that drop them.
    type_wrapper = CodeRust(
        "pub unsafe fn c_to_r(_cs: *const MyStruct) -> () { todo!() }\n"
        "pub unsafe fn r_to_c(_rs: &(), _cs: *mut MyStruct) { todo!() }\n"
        "#[cfg(test)]\nmod tests {\n"
        "    #[test]\n    fn round_trip_zeroed() { todo!() }\n"
        "    #[test]\n    fn round_trip_nontrivial() { todo!() }\n}\n"
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
    tu = ast.create_translation_unit(c_source)
    tree = ast.extract_info_c(tu)
    symbols, dependencies = get_symbols_and_dependencies([tree])

    sys_c.write_text(str(c_source))

    with patch(
        "ideas.translate_recurrent.generate_unimplemented_type_wrapper",
        return_value=type_wrapper,
    ):
        pred = translator(symbols=symbols, dependencies=dependencies)
    assert pred.success

    # The struct definition is wrappable, so its wrapper module must be on disk
    wrap_struct = hybrid_lib.parent / f"{wrapper_name}.rs"
    assert wrap_struct.read_text() == str(type_wrapper)

    # ... and registered in the hybrid crate root exactly once
    assert hybrid_lib.read_text().count(f"pub mod {wrapper_name};") == 1

    # The typedef alias shares the struct's code, so it must not produce a second wrapper
    assert list(hybrid_lib.parent.glob("wrap_*.rs")) == [wrap_struct]
    assert mock_wrapper_gen.call_count == 1

    # Wrapper must not bleed into the -rs crate
    assert f"pub mod {wrapper_name};" not in rs_lib.read_text()


# Both symbols are global definitions and reach `_wrap_type`, but neither is a
# STRUCT_DECL, so there is no field-by-field `c_to_r`/`r_to_c` pair to generate.
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
    assert pred.success

    # `_wrap_type` must bail out before seeding a template or invoking the generator
    assert mock_unimplemented.call_count == 0
    assert mock_wrapper_gen.call_count == 0
    assert list(hybrid_lib.parent.glob("wrap_*.rs")) == []
    assert "pub mod wrap_" not in hybrid_lib.read_text()
    assert "pub mod wrap_" not in rs_lib.read_text()
