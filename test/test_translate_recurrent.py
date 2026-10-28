#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import json
import pytest
from pathlib import Path
from textwrap import dedent
from dataclasses import dataclass, fields
from unittest.mock import MagicMock, patch

import networkx as nx

from ideas import ast
from ideas.ast import CodeC
from ideas.ast_rust import CodeRust
from ideas.consolidate import get_symbols_and_dependencies, create_ast_order
from ideas.hybrid import Hybrid
from ideas.model import Generation
from ideas.translate_recurrent import RecurrentTranslator, SymbolWrapper, TranslationReport
from ideas.translate_recurrent import SymbolTranslator

# Module-qualified so pytest does not try to collect `TestOracle` as a test class
from ideas import oracle as oracle_mod
from ideas.translate_recurrent import SymbolGroup, SymbolName
from ideas.translate_snippet import SnippetTranslator
from ideas.tools import ClippyResult, Crate, check_rust
from ideas.bindgen import C_INTEROP_TRAIT, bindgen_types, split_items, wrap_values
from ideas.wrapper import WrapperGenerator


# The real classes shell out to the Codex CLI; these keep their session and cache logic
class _StubSnippetTranslator(SnippetTranslator):
    def __init__(self, respond, max_iters: int = 1) -> None:
        self.max_iters = max_iters
        self.cache: Path | None = None
        self._respond = respond

    def translate(self, **kwargs) -> Generation:
        # Only generation is stubbed; replaying a cached candidate stays the real path
        if kwargs["translation"] is not None:
            return super().translate(**kwargs)
        gen = self._respond(**kwargs)
        gen.was_generated = True
        return gen


class _StubWrapperGenerator(WrapperGenerator):
    def __init__(self, respond, max_iters: int = 1) -> None:
        self.max_iters = max_iters
        self.cache = None
        self._respond = respond

    def generate(self, **kwargs) -> Generation:
        if kwargs["wrapper"] is not None:
            return super().generate(**kwargs)
        gen = self._respond(**kwargs)
        gen.was_generated = True
        return gen


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
    mock_symbol.deterministic_translation = None
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


# What `_make_translator` built, so tests inspect it without reaching into the agent
@dataclass
class _Harness:
    agent: RecurrentTranslator
    rs_crate: Crate
    hybrid: Hybrid
    oracle: oracle_mod.SymbolOracle


def _assert_generated_crate_lint_policy(source: str, output_dir: Path) -> None:
    assert "#![allow(warnings)]" not in source
    probe = source + (
        "\npub type lint_probe_c_type = u32;\n"
        "pub fn lintProbeFunction() {}\n"
        "pub const lint_probe_global: u32 = 0;\n"
        "#[cfg(test)]\n"
        "mod __lint_probe_tests {}\n"
        "pub fn after_lint_probe_tests() {}\n"
        "pub fn uses_disallowed_name() { let foo = 0; let _ = foo; }\n"
        "pub fn preserves_c_arity(a: u32, b: u32, c: u32, d: u32, "
        "e: u32, f: u32, g: u32, h: u32) { let _ = (a, b, c, d, e, f, g, h); }\n"
        "pub fn unsafe_probe(ptr: *mut u32) {\n"
        "    unsafe { ptr.write(1); let _ = ptr.read(); }\n"
        "}\n"
        "pub unsafe fn unsafe_op_probe(ptr: *const u32) -> u32 { ptr.read() }\n"
        "pub fn safe_pointer_deref_probe(ptr: *const u32) -> u32 {\n"
        "    // SAFETY: This probe is compiled but never executed.\n"
        "    unsafe { ptr.read() }\n"
        "}\n"
        "static mut LINT_PROBE_GLOBAL: u32 = 0;\n"
        "pub fn static_mut_reference_probe() -> &'static mut u32 {\n"
        "    // SAFETY: This probe is compiled but never executed.\n"
        "    unsafe { &mut LINT_PROBE_GLOBAL }\n"
        "}\n"
        'unsafe extern "C" { fn improper_declaration_probe(value: String); }\n'
        'pub extern "C" fn improper_definition_probe(value: String) {}\n'
    )
    crate = Crate(output_dir / "Cargo.toml", template="lib")
    assert crate.lib_src_path is not None
    safe_crate = Crate(output_dir / "safe_crate" / "Cargo.toml", template="lib")
    assert safe_crate.lib_src_path is not None
    safe_crate.lib_src_path.write_text("pub struct MyStruct;\n")
    with crate.cargo_toml.open("a") as manifest:
        manifest.write('safe_crate = { path = "safe_crate" }\n')
    generated = crate.lib_src_path.with_name("generated.rs")
    generated.write_text(probe)
    crate.lib_src_path.write_text(
        'extern crate self as libfoo_sys;\n#[path = "generated.rs"]\npub mod generated;\n'
    )
    clippy = crate.cargo_clippy("--cfg", "test")
    diagnostics = clippy.diagnostics
    lint_names = {
        "non_camel_case_types",
        "non_snake_case",
        "non_upper_case_globals",
        "disallowed_names",
        "too_many_arguments",
    }
    disallowed = [
        diagnostic
        for diagnostic in diagnostics
        if (code := diagnostic.get("code")) is not None
        and isinstance((name := code.get("code")), str)
        and name.removeprefix("clippy::") in lint_names
    ]
    enforced_lints = {
        diagnostic.get("code", {}).get("code")
        for diagnostic in diagnostics
        if isinstance(diagnostic.get("code"), dict)
    }
    assert not clippy.success, clippy.output
    assert {
        "non_camel_case_types",
        "non_snake_case",
        "non_upper_case_globals",
        "disallowed_names",
        "too_many_arguments",
    } <= {
        diagnostic["code"]["code"].removeprefix("clippy::")
        for diagnostic in diagnostics
        if isinstance(diagnostic.get("code"), dict)
        and isinstance(diagnostic["code"].get("code"), str)
    }, f"{clippy.output}\n{disallowed}"
    assert {
        "clippy::multiple_unsafe_ops_per_block",
        "clippy::undocumented_unsafe_blocks",
        "E0133",
        "static_mut_refs",
        "improper_ctypes",
        "improper_ctypes_definitions",
        "clippy::not_unsafe_ptr_arg_deref",
    } <= enforced_lints


def _clippy_result(
    success: bool = True, diagnostics: list[dict] | None = None, output: str = ""
) -> ClippyResult:
    return ClippyResult(success, diagnostics or [], [], output, Path(), None)


def _make_translator(
    tmp_path: Path,
    *,
    is_bin: bool = False,
    c_src: str = "",
    tests: str | None = None,
    broken_tests: set[str] | None = None,
    **kwargs,
) -> tuple[_Harness, Path, Path, Path]:
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
    # What _init_rs_crate leaves in the -rs crate
    (rs_src_dir / "lib.rs").write_text(
        "#![forbid(unsafe_code)]\n"
        "#![allow(clippy::items_after_test_module, non_snake_case)]\n\n"
    )
    if is_bin:
        (rs_src_dir / "main.rs").write_text(
            "#![forbid(unsafe_code)]\n"
            "#![allow(non_snake_case)]\n\n"
            'fn main() {\n    unimplemented!("`main` was never translated to Rust");\n}\n'
        )

    rs_crate = MagicMock()
    rs_crate.lib_src_path = rs_src_dir / "lib.rs"
    rs_crate.main_src_path = rs_src_dir / "main.rs" if is_bin else None
    rs_crate.lib_name = "foo_rs"
    rs_crate.cargo_build.return_value = (True, "")
    rs_crate.cargo_clippy.return_value.only_in.return_value = _clippy_result()

    # hybrid crate
    hybrid_src_dir = tmp_path / "hybrid" / "src"
    hybrid_src_dir.mkdir(parents=True)
    (hybrid_src_dir / "lib.rs").write_bytes(b"")

    # Stands in for `cargo init` and `cargo metadata`, so the crate's paths are the real ones
    targets = [
        {"kind": ["lib"], "name": "foo_hybrid", "src_path": str(hybrid_src_dir / "lib.rs")}
    ]
    if is_bin:
        targets.append(
            {"kind": ["bin"], "name": "foo-hybrid", "src_path": str(hybrid_src_dir / "main.rs")}
        )

    def init_crate(self: Crate, cargo_toml: Path, *_args, **_kwargs) -> None:
        self.cargo_toml = cargo_toml
        self.vcs = MagicMock()
        self.__dict__["metadata"] = {
            "resolve": {"root": "foo-hybrid"},
            "packages": [{"id": "foo-hybrid", "name": "foo-hybrid", "targets": targets}],
            "workspace_root": str(tmp_path),
        }

    with patch.object(Crate, "__init__", init_crate):
        hybrid_crate = Crate(tmp_path / "hybrid" / "Cargo.toml")
    hybrid_crate.cargo_build = MagicMock(return_value=(True, ""))  # type: ignore[method-assign]
    hybrid_crate.cargo_test = MagicMock(return_value=(True, "", "", 0))  # type: ignore[method-assign]
    hybrid_crate.cargo_clippy = MagicMock()  # type: ignore[method-assign]
    hybrid_crate.cargo_clippy.return_value.only_in.return_value = _clippy_result()
    hybrid = Hybrid(hybrid_crate, sys_crate, kwargs.pop("ffi_crate", None))

    # RecurrentTranslator with mocked inputs
    oracle = oracle_mod.TestOracle(hybrid_crate, tests) if tests is not None else None
    hybrid_oracle = (oracle if oracle is not None else oracle_mod.NullOracle()).on(hybrid_crate)
    wrapper = SymbolWrapper(hybrid, kwargs.pop("symbol_wrapper", None), hybrid_oracle)
    agent = RecurrentTranslator(
        SymbolTranslator(rs_crate, kwargs.pop("symbol_translator")),
        wrapper,
        hybrid_oracle,
        **kwargs,
    )

    # Verification needs a real crate to run nextest against, so inject its result instead
    def _stub_baseline() -> None:
        if oracle is not None:
            oracle._record.baseline_failures = frozenset(broken_tests or ())

    hybrid_oracle.baseline = _stub_baseline  # type: ignore[method-assign]
    return (
        _Harness(agent, rs_crate, hybrid, hybrid_oracle),
        hybrid_src_dir / "lib.rs",
        rs_src_dir / "lib.rs",
        sys_lib.with_suffix(".c"),
    )


# What `translate` does: prepare, then translate
def _run(harness: _Harness, **kwargs) -> TranslationReport:
    harness.agent.prepare(kwargs["symbols"].values())
    return harness.agent(**kwargs)


def test_wrapper_only_restore_on_final_wrapper_failure(tmp_path: Path) -> None:
    # Create a SnippetTranslator backed by a mock LM that always succeeds
    mock_translator = MagicMock(return_value=Generation(code=CodeRust("pub fn bar() {}")))
    symbol_translator = _StubSnippetTranslator(mock_translator)

    # Create a WrapperGenerator backed by a mock LM that returns a unique but invalid
    # wrapper each attempt because validate_changes produces scope_feedback cause a failure
    # because we patch ideas.translate_recurrent.generate_unimplemented_function_wrapper below
    attempt = 0

    def mock_wrapper_side_effect(*args, **kwargs):
        nonlocal attempt
        attempt += 1
        return Generation(code=CodeRust(f"// attempt {attempt} wrapper"))

    mock_wrapper = MagicMock(side_effect=mock_wrapper_side_effect)
    symbol_wrapper = _StubWrapperGenerator(mock_wrapper)

    # Create a RecurrentTranslator with max_iters=2 to exercise both restore paths
    # NOTE: Supplying tests is required so WrapperGenerator is called since the symbol is not global
    harness, hybrid_lib, rs_lib, sys_c = _make_translator(
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
    harness.hybrid.wrap("c:@F@baz", original_wrapper)

    # Run the translator
    with patch(
        "ideas.translate_recurrent.generate_unimplemented_function_wrapper",
        return_value=CodeRust("pub fn bar() { unimplemented!() }"),
    ):
        pred = _run(harness, symbols=symbols, dependencies=dependencies)
    assert pred.complete

    # Translator should have iterated twice
    assert mock_translator.call_count == 2
    assert mock_wrapper.call_count == 2

    # Rolling back must drop the failed wrapper while keeping the earlier one
    assert harness.hybrid.wrappers == {"c:@F@baz": original_wrapper}

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
        return_value=Generation(code=CodeRust("pub fn baz() {}\npub fn bar() {}"))
    )
    symbol_translator = _StubSnippetTranslator(mock_translator)

    baz_stub = CodeRust("pub fn baz_wrapper() -> i32 { unimplemented!() }")
    bar_stub = CodeRust("pub fn bar_wrapper() -> i32 { unimplemented!() }")
    baz_wrapper = CodeRust("pub fn baz_wrapper() -> i32 { 0 }")

    c_src_at_bar = ""

    def mock_wrapper_side_effect(*args, **kwargs):
        nonlocal c_src_at_bar
        # Keeping the template's shape leaves validate_changes nothing to complain about, so
        # `baz` succeeds; anything else is off-template and fails every attempt.
        if kwargs["example_wrapper"] == baz_stub:
            return Generation(code=baz_wrapper)
        c_src_at_bar = sys_c.read_text()
        return Generation(code=CodeRust("// off-template wrapper"))

    mock_wrapper = MagicMock(side_effect=mock_wrapper_side_effect)
    symbol_wrapper = _StubWrapperGenerator(mock_wrapper)

    # max_iters=1 so the wrap failure lands directly on the `hybrid_only=True` branch
    harness, hybrid_lib, rs_lib, sys_c = _make_translator(
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
        "ideas.translate_recurrent.generate_unimplemented_function_wrapper",
        side_effect=lambda _path, spelling, _types: baz_stub if spelling == "baz" else bar_stub,
    ):
        _run(harness, symbols=symbols, dependencies=dependencies)

    # Sanity: by the time `bar` was wrapped, `baz` had handed its definition over to Rust
    assert "extern int baz(void);" in c_src_at_bar

    # Every symbol the C source no longer defines must still have a wrapper standing in for it
    c_src = sys_c.read_text()
    externed = {s for s in ("baz", "bar") if f"extern int {s}(void);" in c_src}
    wrapped = {symbols[name].spelling for name in harness.hybrid.wrappers}
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
        return Generation(code=CodeRust(f"pub fn bar() {{ /* attempt {attempt} */ }}"))

    mock_translator = MagicMock(side_effect=mock_translator_side_effect)
    symbol_translator = _StubSnippetTranslator(mock_translator)

    # Wrapper generator is never reached because translation always fails; a plain mock is enough.
    harness, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=MagicMock(),
        tests="dummy_test",
        max_iters=2,
    )

    # Make cargo build always fail for the -rs crate so every translation attempt fails
    # so a full restore (not wrappers-only) is performed on *both* iterations.
    rs_crate_mock: MagicMock = harness.rs_crate  # type: ignore[assignment]
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
    harness.hybrid.wrap("c:@F@baz", original_wrapper)
    initial_hybrid_lib = hybrid_lib.read_text()

    # Run the translator
    pred = _run(harness, symbols=symbols, dependencies=dependencies)
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
    assert harness.hybrid.wrappers == {"c:@F@baz": original_wrapper}

    # Translation must not bleed into the hybrid crate across retries
    assert "pub fn bar" not in hybrid_lib.read_text()


def test_no_restore_on_test_failure(tmp_path: Path) -> None:
    # Translation and wrapping both succeed; only the test run fails.
    # On the last (only) iteration with failure == "test", the restore branch is:
    #   `elif result.failure == "test": pass`  — nothing is restored.
    mock_translator = MagicMock(return_value=Generation(code=CodeRust("pub fn bar() {}")))
    symbol_translator = _StubSnippetTranslator(mock_translator)

    # A wrapper that keeps the template's shape means validate_changes finds no diffs →
    # scope_feedback is empty → the attempt succeeds → wrapping succeeds.
    unimplemented_stub = CodeRust("pub fn bar_wrapper() -> i32 { unimplemented!() }")
    bar_wrapper = CodeRust("pub fn bar_wrapper() -> i32 { 0 }")
    mock_wrapper_gen = MagicMock(return_value=Generation(code=bar_wrapper))
    symbol_wrapper = _StubWrapperGenerator(mock_wrapper_gen)

    harness, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=1,
    )

    # Make the test run fail so result.failure == "test"
    crate_mock: MagicMock = harness.hybrid.crate  # type: ignore[assignment]
    crate_mock.cargo_test.side_effect = [
        # init audit: the imported hybrid still passes the baseline
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
        "ideas.translate_recurrent.generate_unimplemented_function_wrapper",
        return_value=unimplemented_stub,
    ):
        pred = _run(harness, symbols=symbols, dependencies=dependencies)
    assert pred.complete
    assert pred.regressions == {"test_bar": sym_group}

    # No restore: translation is kept in the -rs crate
    assert "pub fn bar" in rs_lib.read_text()

    # No restore: the wrapper written during wrapping is kept and rendered
    assert harness.hybrid.wrappers == {sym: bar_wrapper}
    assert "pub fn bar_wrapper" in hybrid_lib.read_text()

    # No restore: C source is kept with the extern declaration written by clang_make_extern_
    assert "extern int bar(void);" in sys_c.read_text()


def test_feedback_after_test_failure(tmp_path: Path) -> None:
    # Translation and wrapping succeed on both iterations
    mock_translator = MagicMock(return_value=Generation(code=CodeRust("pub fn bar() {}")))
    symbol_translator = _StubSnippetTranslator(mock_translator)

    # A wrapper that keeps the template's shape means validate_changes passes
    unimplemented_stub = CodeRust("pub fn bar_wrapper() -> i32 { unimplemented!() }")
    mock_wrapper_gen = MagicMock(
        return_value=Generation(code=CodeRust("pub fn bar_wrapper() -> i32 { 0 }"))
    )
    symbol_wrapper = _StubWrapperGenerator(mock_wrapper_gen)

    harness, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=2,
    )

    # First test run fails, second succeeds.
    crate_mock: MagicMock = harness.hybrid.crate  # type: ignore[assignment]
    crate_mock.cargo_test.side_effect = [
        # init audit
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
        "ideas.translate_recurrent.generate_unimplemented_function_wrapper",
        return_value=unimplemented_stub,
    ):
        pred = _run(harness, symbols=symbols, dependencies=dependencies)
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
    mock_translator = MagicMock(return_value=Generation(code=CodeRust("pub fn bar() {}")))
    symbol_translator = _StubSnippetTranslator(mock_translator)

    # A wrapper that keeps the template's shape means validate_changes passes
    unimplemented_stub = CodeRust("pub fn bar_wrapper() -> i32 { unimplemented!() }")
    mock_wrapper_gen = MagicMock(
        return_value=Generation(code=CodeRust("pub fn bar_wrapper() -> i32 { 0 }"))
    )
    symbol_wrapper = _StubWrapperGenerator(mock_wrapper_gen)

    harness, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=2,
    )

    # Force failure on the first wrapper build attempt, then succeed on the second.
    crate_mock: MagicMock = harness.hybrid.crate  # type: ignore[assignment]
    crate_mock.cargo_build.side_effect = [
        (True, ""),  # prepare_hybrid's build of the types and bindings
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
        "ideas.translate_recurrent.generate_unimplemented_function_wrapper",
        return_value=unimplemented_stub,
    ):
        pred = _run(harness, symbols=symbols, dependencies=dependencies)
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
    mock_translator = MagicMock(return_value=Generation(code=CodeRust("pub fn bar() {}")))
    symbol_translator = _StubSnippetTranslator(mock_translator)

    # tests=None: wrapping is skipped for non-global symbols, so only translation runs.
    harness, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=MagicMock(),
        tests=None,
        max_iters=2,
    )

    # Make the -rs build fail on iteration 1 and succeed on iteration 2.
    rs_crate_mock: MagicMock = harness.rs_crate  # type: ignore[assignment]
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

    pred = _run(harness, symbols=symbols, dependencies=dependencies)

    assert pred.complete

    # Translator must have been called twice: once per outer iteration.
    assert mock_translator.call_count == 2

    # Extract the feedback forwarded to the translator on the second (retry) call.
    second_call_feedback: str = mock_translator.call_args_list[1].kwargs["build_feedback"]

    # The build error from the first iteration must be surfaced to the retry so the
    # translator knows why its previous output was rejected.
    assert "compile error" in second_call_feedback


def test_scope_feedback_is_combined_with_lint_feedback(tmp_path: Path) -> None:
    mock_translator = MagicMock(
        side_effect=[
            Generation(code=CodeRust("fn bar() {}")),
            Generation(code=CodeRust("pub fn bar() {}")),
        ]
    )
    symbol_translator = _StubSnippetTranslator(mock_translator)

    # tests=None: wrapping is skipped for non-global symbols, so only translation runs.
    harness, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=MagicMock(),
        tests=None,
        max_iters=2,
    )

    rs_crate_mock: MagicMock = harness.rs_crate  # type: ignore[assignment]
    rs_crate_mock.cargo_build.side_effect = [
        (True, ""),
        (True, ""),
    ]
    warning = "warning: function `bar` is never used"
    rs_crate_mock.cargo_clippy.return_value.only_in.side_effect = [
        _clippy_result(diagnostics=[{"level": "warning", "message": warning}]),
        _clippy_result(),
    ]

    sym: SymbolName = "c:@F@bar"
    sym_group: SymbolGroup = (sym,)
    mock_symbol = _mock_bar_symbol(sym)

    symbols: dict[SymbolName, MagicMock] = {sym: mock_symbol}
    dependencies: dict[SymbolGroup, list[SymbolGroup]] = {sym_group: []}

    sys_c.write_text("int bar(void) { return 0; }")

    pred = _run(harness, symbols=symbols, dependencies=dependencies)

    assert pred.complete
    assert mock_translator.call_count == 2

    retry = mock_translator.call_args_list[1].kwargs

    assert "`bar` must be declared `pub`" in retry["scope_feedback"]
    assert warning in retry["lint_feedback"]


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
        return_value=Generation(code=CodeRust("pub struct MyStruct { pub x: i32 }"))
    )
    symbol_translator = _StubSnippetTranslator(mock_translator)

    # Returning the unimplemented template unchanged means validate_changes finds no
    # diffs, so wrapping succeeds. Every round-trip test must be present, and the impl
    # header must name the type, because `_wrap_type` rejects wrappers that drop them.
    type_wrapper = CodeRust(
        dedent(f"""
            impl CInterop for {type_name} {{
                type Rust = safe_crate::{type_name};
                unsafe fn to_rust(_cs: *const Self) -> Self::Rust {{ todo!() }}
                unsafe fn sync_to_c(_rs: &Self::Rust, _cs: *mut Self) {{ todo!() }}
            }}

            #[cfg(test)]
            mod test_{type_name} {{
                #[test]
                fn round_trip_zeroed() {{ todo!() }}
                #[test]
                fn round_trip_nontrivial() {{ todo!() }}
                #[test]
                fn round_trip_mutated() {{ todo!() }}
            }}
            """).strip()
    )
    # A function template comes back unchanged, which validate_changes accepts as is
    mock_wrapper_gen = MagicMock(
        side_effect=lambda **kwargs: Generation(
            code=type_wrapper
            if kwargs["example_wrapper"] == type_wrapper
            else kwargs["example_wrapper"]
        )
    )
    symbol_wrapper = _StubWrapperGenerator(mock_wrapper_gen)

    harness, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=1,
    )
    crate_mock: MagicMock = harness.hybrid.crate  # type: ignore[assignment]
    crate_mock.cargo_test.return_value = (True, "", "", "")

    # Parse real C so the struct/typedef symbols carry genuine kind and linkage flags
    tu = ast.create_translation_unit(source)
    tree = ast.extract_info_c(tu)
    symbols, dependencies = get_symbols_and_dependencies([tree])

    sys_c.write_text(str(source))

    with patch(
        "ideas.translate_recurrent.generate_unimplemented_type_wrapper",
        return_value=(type_wrapper, f"test_{type_name}"),
    ):
        pred = _run(harness, symbols=symbols, dependencies=dependencies)
    assert pred.complete

    # The struct definition is wrappable, so its wrapper must be rendered into the root
    type_wrappers = [w for n, w in harness.hybrid.wrappers.items() if symbols[n].is_type]
    assert type_wrappers == [type_wrapper]
    assert str(type_wrapper) in hybrid_lib.read_text()

    # The tag and its alias share one snippet, so they must not produce two wrappers
    type_calls = [
        c
        for c in mock_wrapper_gen.call_args_list
        if c.kwargs["example_wrapper"] == type_wrapper
    ]
    assert len(type_calls) == 1

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
        return_value=Generation(code=CodeRust(f"pub struct {tag_name} {{}}"))
    )
    symbol_translator = _StubSnippetTranslator(mock_translator)

    # `_wrap_type` rejects the `type Rust = ()` placeholder, and the rest of the template
    # comes back unchanged so validate_changes has no out-of-scope diff to reject
    mock_wrapper_gen = MagicMock(
        side_effect=lambda **kwargs: Generation(
            code=CodeRust(
                str(kwargs["example_wrapper"]).replace("type Rust = ();", "type Rust = u8;")
            )
        )
    )
    symbol_wrapper = _StubWrapperGenerator(mock_wrapper_gen)

    harness, hybrid_lib, _, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=1,
    )
    crate_mock: MagicMock = harness.hybrid.crate  # type: ignore[assignment]
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

    pred = _run(harness, symbols=symbols, dependencies=dependencies)
    assert pred.complete

    assert hybrid_lib.read_text().count("impl CInterop for") == 1
    assert len([n for n in harness.hybrid.wrappers if symbols[n].is_type]) == 1
    type_calls = [
        c
        for c in mock_wrapper_gen.call_args_list
        if "impl CInterop for" in str(c.kwargs["example_wrapper"])
    ]
    assert len(type_calls) == 1

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
                    type Rust = safe_crate::MyStruct;
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
        return_value=Generation(code=CodeRust("pub struct MyStruct { pub x: i32 }"))
    )
    symbol_translator = _StubSnippetTranslator(mock_translator)

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
                #[test]
                fn round_trip_mutated() { todo!() }
            }
            """)
    )
    mock_wrapper_gen = MagicMock(return_value=Generation(code=bad_wrapper))

    # Two iterations so the rejection comes back as feedback on a second attempt
    symbol_wrapper = _StubWrapperGenerator(mock_wrapper_gen, max_iters=2)

    harness, _, _, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=1,
    )
    crate_mock: MagicMock = harness.hybrid.crate  # type: ignore[assignment]
    crate_mock.cargo_test.return_value = (True, "", "", "")

    tu = ast.create_translation_unit(_TYPEDEF_STRUCT)
    tree = ast.extract_info_c(tu)
    symbols, dependencies = get_symbols_and_dependencies([tree])

    sys_c.write_text(str(_TYPEDEF_STRUCT))

    with patch(
        "ideas.translate_recurrent.generate_unimplemented_type_wrapper",
        return_value=(bad_wrapper, tests_mod),
    ):
        _run(harness, symbols=symbols, dependencies=dependencies)

    assert mock_wrapper_gen.call_count == 2
    assert expected in mock_wrapper_gen.call_args.kwargs["scope_feedback"]


_STRUCT_WITH_DESTRUCTOR = ast.CodeC(
    str(_TYPEDEF_STRUCT) + "\nvoid my_struct_free(my_struct_t *s) { (void)s; }\n"
)


@pytest.mark.parametrize(
    ("helper", "clashes"),
    [("my_struct_free", True), ("my_struct_free_cb", False)],
    ids=["named_after_c_function", "distinct_name"],
)
def test_type_wrapper_rejected_for_helper_named_after_c_function(
    tmp_path: Path, helper: str, clashes: bool
) -> None:
    # The clash only breaks the build once the function's template is inserted, long after
    # the type wrapper was accepted, so it has to be caught while that wrapper can still change
    mock_translator = MagicMock(
        return_value=Generation(code=CodeRust("pub struct MyStruct { pub x: i32 }"))
    )
    symbol_translator = _StubSnippetTranslator(mock_translator)

    tests_mod = "test_MyStruct"
    type_wrapper = CodeRust(
        dedent(f"""
            impl CInterop for MyStruct {{
                type Rust = safe_crate::MyStruct;
                unsafe fn to_rust(_cs: *const Self) -> Self::Rust {{ todo!() }}
                unsafe fn sync_to_c(_rs: &Self::Rust, _cs: *mut Self) {{ todo!() }}
            }}

            unsafe extern "C" fn {helper}(_p: *mut ::std::os::raw::c_void) {{}}

            #[cfg(test)]
            mod {tests_mod} {{
                #[test]
                fn round_trip_zeroed() {{ todo!() }}
                #[test]
                fn round_trip_nontrivial() {{ todo!() }}
                #[test]
                fn round_trip_mutated() {{ todo!() }}
            }}
            """).strip()
    )
    # Every template comes back as this wrapper; only the type wrapper's calls are inspected
    mock_wrapper_gen = MagicMock(return_value=Generation(code=type_wrapper))
    symbol_wrapper = _StubWrapperGenerator(mock_wrapper_gen, max_iters=2)

    harness, _, _, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=1,
    )
    crate_mock: MagicMock = harness.hybrid.crate  # type: ignore[assignment]
    crate_mock.cargo_test.return_value = (True, "", "", "")

    tu = ast.create_translation_unit(_STRUCT_WITH_DESTRUCTOR)
    tree = ast.extract_info_c(tu)
    symbols, dependencies = get_symbols_and_dependencies([tree])

    sys_c.write_text(str(_STRUCT_WITH_DESTRUCTOR))

    with patch(
        "ideas.translate_recurrent.generate_unimplemented_type_wrapper",
        return_value=(type_wrapper, tests_mod),
    ):
        _run(harness, symbols=symbols, dependencies=dependencies)

    type_feedback = [
        c.kwargs["scope_feedback"]
        for c in mock_wrapper_gen.call_args_list
        if c.kwargs["example_wrapper"] == type_wrapper
    ]
    clash = f"`{helper}` would clash with a C function or global"
    assert any(clash in str(f) for f in type_feedback) == clashes


_SIMPLE_FUNCTION = ast.CodeC("int bar(void) { return 0; }")


def test_known_broken_tests_seed_the_baseline(tmp_path: Path) -> None:
    mock_translator = MagicMock(
        return_value=Generation(code=CodeRust("pub fn bar() -> i32 { 0 }"))
    )
    symbol_translator = _StubSnippetTranslator(mock_translator)

    harness, _, _, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=None,
        tests="dummy_test",
        max_iters=1,
        broken_tests={"broken"},
    )
    crate_mock: MagicMock = harness.hybrid.crate  # type: ignore[assignment]
    crate_mock.cargo_test.return_value = (True, "", "", "")

    tu = ast.create_translation_unit(_SIMPLE_FUNCTION)
    tree = ast.extract_info_c(tu)
    symbols, dependencies = get_symbols_and_dependencies([tree])
    sys_c.write_text(str(_SIMPLE_FUNCTION))

    _run(harness, symbols=symbols, dependencies=dependencies)

    assert harness.oracle.baseline_failures == {"broken"}

    # The exclusion lowers the bar for the whole run, so it belongs in the bisectable log
    init_msg = crate_mock.vcs.commit.call_args_list[0].args[0]
    assert "broken" in init_msg


def _libtest_json(**events: str) -> str:
    return "\n".join(
        json.dumps({"type": "test", "name": f"smoke${name}", "event": event})
        for name, event in events.items()
    )


def _delta_translator(tmp_path: Path, *, broken_tests: set[str] | None = None):
    mock_translator = MagicMock(return_value=Generation(code=CodeRust("pub fn bar() {}")))
    symbol_translator = _StubSnippetTranslator(mock_translator)
    # The wrapper only has to keep the template's shape, so an implemented body passes
    unimplemented_stub = CodeRust("pub fn bar_wrapper() -> i32 { unimplemented!() }")
    symbol_wrapper = _StubWrapperGenerator(
        MagicMock(return_value=Generation(code=CodeRust("pub fn bar_wrapper() -> i32 { 0 }")))
    )

    harness, _, _, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=1,
        broken_tests=broken_tests,
    )
    sys_c.write_text("int bar(void) { return 0; }")
    return harness, unimplemented_stub


def _run_one_group(
    harness: _Harness,
    unimplemented_stub: CodeRust,
    final_run: tuple,
) -> TranslationReport:
    crate_mock: MagicMock = harness.hybrid.crate  # type: ignore[assignment]
    crate_mock.cargo_test.side_effect = [
        (True, "", "", 0),  # init audit
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
        "ideas.translate_recurrent.generate_unimplemented_function_wrapper",
        return_value=unimplemented_stub,
    ):
        return _run(harness, symbols=symbols, dependencies=dependencies)


def test_new_failure_beside_a_baseline_failure_is_blamed(tmp_path: Path) -> None:
    harness, stub = _delta_translator(tmp_path, broken_tests={"broken"})
    pred = _run_one_group(
        harness,
        stub,
        (False, _libtest_json(broken="failed", fresh="failed"), "2 tests failed", 101),
    )

    assert pred.regressions == {"fresh": ("c:@F@bar",)}

    crate_mock: MagicMock = harness.hybrid.crate  # type: ignore[assignment]
    assert "Regressed test(s): fresh" in crate_mock.vcs.commit.call_args_list[-1].args[0]


def test_regression_is_reported_without_failing(tmp_path: Path) -> None:
    harness, stub = _delta_translator(tmp_path)
    pred = _run_one_group(harness, stub, (False, _libtest_json(fresh="failed"), "", 101))

    # The translation is complete, so only `regressions` says it diverged
    assert pred.complete
    assert pred.regressions == {"fresh": ("c:@F@bar",)}


def test_best_review_attempt_is_restored(tmp_path: Path) -> None:
    # Two attempts that both diverge, the second one regressing more than the first
    mock_translator = MagicMock(
        side_effect=[
            Generation(code=CodeRust("pub fn bar() { /* first */ }")),
            Generation(code=CodeRust("pub fn bar() { /* second */ }")),
        ]
    )
    symbol_translator = _StubSnippetTranslator(mock_translator)
    unimplemented_stub = CodeRust("pub fn bar_wrapper() -> i32 { unimplemented!() }")
    symbol_wrapper = _StubWrapperGenerator(
        MagicMock(return_value=Generation(code=CodeRust("pub fn bar_wrapper() -> i32 { 0 }")))
    )

    harness, _, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=2,
    )
    sys_c.write_text("int bar(void) { return 0; }")

    crate_mock: MagicMock = harness.hybrid.crate  # type: ignore[assignment]
    gates = [(True, "", "", 0)] * 3  # fail-fast, wrapper build, build_only pre-pass
    crate_mock.cargo_test.side_effect = [
        (True, "", "", 0),  # init audit
        *gates,
        (False, _libtest_json(one="failed", two="ok"), "1 test failed", 101),
        *gates,
        (False, _libtest_json(one="failed", two="failed"), "2 tests failed", 101),
    ]

    sym: SymbolName = "c:@F@bar"
    symbols: dict[SymbolName, MagicMock] = {sym: _mock_bar_symbol(sym)}
    dependencies: dict[SymbolGroup, list[SymbolGroup]] = {(sym,): []}

    with patch(
        "ideas.translate_recurrent.generate_unimplemented_function_wrapper",
        return_value=unimplemented_stub,
    ):
        pred = _run(harness, symbols=symbols, dependencies=dependencies)

    # Only the first attempt's regression is blamed, and its translation is what survives
    assert pred.regressions == {"one": (sym,)}
    assert "first" in rs_lib.read_text()


def test_best_review_attempt_survives_a_final_translate_failure(tmp_path: Path) -> None:
    # First attempt diverges, second fails to build: the group must keep the first
    mock_translator = MagicMock(
        side_effect=[
            Generation(code=CodeRust("pub fn bar() { /* first */ }")),
            Generation(code=CodeRust("pub fn bar() { /* second */ }")),
        ]
    )
    symbol_translator = _StubSnippetTranslator(mock_translator)
    unimplemented_stub = CodeRust("pub fn bar_wrapper() -> i32 { unimplemented!() }")
    symbol_wrapper = _StubWrapperGenerator(
        MagicMock(return_value=Generation(code=CodeRust("pub fn bar_wrapper() -> i32 { 0 }")))
    )

    harness, _, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=2,
    )
    sys_c.write_text("int bar(void) { return 0; }")

    rs_crate_mock: MagicMock = harness.rs_crate  # type: ignore[assignment]
    rs_crate_mock.cargo_build.side_effect = [(True, ""), (False, "compilation error")]

    crate_mock: MagicMock = harness.hybrid.crate  # type: ignore[assignment]
    crate_mock.cargo_test.side_effect = [
        (True, "", "", 0),  # init audit
        (True, "", "", 0),  # _wrap_function fail-fast gate
        (True, "", "", 0),  # wrapper build gate
        (True, "", "", 0),  # _test_symbol build_only pre-pass
        (False, _libtest_json(one="failed", two="ok"), "1 test failed", 101),
    ]

    sym: SymbolName = "c:@F@bar"
    symbols: dict[SymbolName, MagicMock] = {sym: _mock_bar_symbol(sym)}
    dependencies: dict[SymbolGroup, list[SymbolGroup]] = {(sym,): []}

    with patch(
        "ideas.translate_recurrent.generate_unimplemented_function_wrapper",
        return_value=unimplemented_stub,
    ):
        pred = _run(harness, symbols=symbols, dependencies=dependencies)

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
        return_value=Generation(code=CodeRust("pub static mut COUNTER: i32 = 0;"))
    )
    symbol_translator = _StubSnippetTranslator(mock_translator)

    # A variable wrapper is the only bridge between C storage and the unsafe-free -rs crate,
    # so it must be emitted even with no tests and no LLM wrapper generator.
    harness, hybrid_lib, rs_lib, sys_c = _make_translator(
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
        pred = _run(harness, symbols=symbols, dependencies=dependencies)
    assert pred.complete

    mock_bindgen.assert_called_once_with(sys_c, ["counter"], harness.hybrid.types_code)

    # Pure bindgen output, so it is hoisted rather than given a wrapper of its own
    assert harness.hybrid.wrappers == {}
    assert harness.hybrid.externs == (var_binding,)
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

    def translate(*_args, **_kwargs) -> Generation:
        seen.append(hybrid_lib.read_text())
        return Generation(code=CodeRust("pub static mut COUNTER: i32 = 0;"))

    symbol_translator = _StubSnippetTranslator(MagicMock(side_effect=translate))
    harness, _, _, sys_c = _make_translator(
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
        assert _run(harness, symbols=symbols, dependencies=dependencies).complete

    assert seen
    assert all(str(wrap_values(var_binding)) in crate_code for crate_code in seen)


_SYNC_FNS = ("sync_counter_to_rust", "sync_counter_to_c")

# Names both globals and calls both sync functions, which is the minimum the wrapper
# validator accepts
_ROUND_TRIP = (
    "unsafe { __c_globals::counter = 7; sync_counter_to_rust(); "
    "assert_eq!(safe_crate::COUNTER, 7); sync_counter_to_c(); "
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
        return_value=Generation(code=CodeRust("pub static COUNTER: i32 = 0;"))
    )
    symbol_translator = _StubSnippetTranslator(mock_translator)

    tests_mod = "test_var_counter"
    var_wrapper = _variable_wrapper("assert_eq!(unsafe { counter }, safe_crate::COUNTER);")
    mock_wrapper_gen = MagicMock(return_value=Generation(code=var_wrapper))
    symbol_wrapper = _StubWrapperGenerator(mock_wrapper_gen)

    harness, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=1,
    )
    crate_mock: MagicMock = harness.hybrid.crate  # type: ignore[assignment]
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
        pred = _run(harness, symbols=symbols, dependencies=dependencies)
    assert pred.complete

    # The extern is still hoisted; the wrapper adds the test on top of it
    assert harness.hybrid.externs == (var_binding,)
    assert harness.hybrid.wrappers == {"c:@counter": var_wrapper}
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
                sync_body="unsafe { counter = safe_crate::COUNTER };",
            ),
            "Comparing the C global against itself",
        ),
        (
            _variable_wrapper(
                "unsafe { sync_counter_to_rust() };"
                "assert_eq!(unsafe { counter }, safe_crate::COUNTER);"
            ),
            "`initial_value_matches` must not call `sync_counter_to_rust`",
        ),
        (
            _variable_wrapper("let _ = safe_crate::COUNTER; todo!()"),
            "`todo!()` placeholder",
        ),
        (
            _variable_wrapper(
                "assert_eq!(unsafe { counter }, safe_crate::COUNTER);", tests_mod="tests"
            ),
            "must stay named `test_var_counter`",
        ),
        (
            CodeRust("#[cfg(test)]\nmod test_var_counter {\n    // safe_crate\n}"),
            "`initial_value_matches` must be implemented",
        ),
        (
            _variable_wrapper(
                "assert_eq!(unsafe { counter }, safe_crate::COUNTER);",
                sync_fns=("sync_counter_to_c",),
            ),
            "Keep all of `sync_counter_to_rust`, `sync_counter_to_c`",
        ),
        (
            _variable_wrapper(
                "assert_eq!(unsafe { counter }, safe_crate::COUNTER);", round_trip=None
            ),
            "`round_trip_nontrivial` must be implemented",
        ),
        (
            # Dropping the pair and rewriting the test not to call it leaves a test that
            # exercises nothing
            _variable_wrapper(
                "assert_eq!(unsafe { counter }, safe_crate::COUNTER);",
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
        return_value=Generation(code=CodeRust("pub static COUNTER: i32 = 0;"))
    )
    symbol_translator = _StubSnippetTranslator(mock_translator)

    mock_wrapper_gen = MagicMock(return_value=Generation(code=wrapper))

    # Two iterations so the rejection comes back as feedback on a second attempt
    symbol_wrapper = _StubWrapperGenerator(mock_wrapper_gen, max_iters=2)

    harness, _, _, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=1,
    )
    crate_mock: MagicMock = harness.hybrid.crate  # type: ignore[assignment]
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
        _run(harness, symbols=symbols, dependencies=dependencies)

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
        return_value=Generation(code=CodeRust("pub struct Placeholder;"))
    )
    symbol_translator = _StubSnippetTranslator(mock_translator)

    mock_wrapper_gen = MagicMock()
    symbol_wrapper = _StubWrapperGenerator(mock_wrapper_gen)

    harness, hybrid_lib, rs_lib, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests="dummy_test",
        max_iters=1,
    )
    crate_mock: MagicMock = harness.hybrid.crate  # type: ignore[assignment]
    crate_mock.cargo_test.return_value = (True, "", "", "")

    tu = ast.create_translation_unit(c_source)
    tree = ast.extract_info_c(tu)
    symbols, dependencies = get_symbols_and_dependencies([tree])

    sys_c.write_text(str(c_source))

    mock_unimplemented = MagicMock()
    with patch(
        "ideas.translate_recurrent.generate_unimplemented_type_wrapper", mock_unimplemented
    ):
        pred = _run(harness, symbols=symbols, dependencies=dependencies)

    # Translation still succeeds; only wrapping is skipped
    assert pred.complete

    # `_wrap_type` must bail out before seeding a template or invoking the generator
    assert mock_unimplemented.call_count == 0
    assert mock_wrapper_gen.call_count == 0
    assert harness.hybrid.wrappers == {}
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
        return_value=Generation(code=CodeRust("pub struct Placeholder;"))
    )
    symbol_translator = _StubSnippetTranslator(mock_translator)

    harness, _, _, sys_c = _make_translator(
        tmp_path,
        symbol_translator=symbol_translator,
        symbol_wrapper=None,
        tests=None,
        max_iters=1,
    )

    tu = ast.create_translation_unit(_REDUNDANT_TYPEDEF)
    symbols, dependencies = get_symbols_and_dependencies([ast.extract_info_c(tu)])
    sys_c.write_text(str(_REDUNDANT_TYPEDEF))

    pred = _run(harness, symbols=symbols, dependencies=dependencies)

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
            type Rust = safe_crate::MyStruct;
            unsafe fn to_rust(_cs: *const Self) -> Self::Rust { todo!() }
            unsafe fn sync_to_c(_rs: &Self::Rust, _cs: *mut Self) { todo!() }
        }

        #[cfg(test)]
        mod test_my_struct_t {
            #[test]
            fn round_trip_zeroed() { todo!() }
            #[test]
            fn round_trip_nontrivial() { todo!() }
            #[test]
            fn round_trip_mutated() { todo!() }
        }
        """).strip()
)


def test_untested_binary_wraps_types_but_not_functions(tmp_path: Path) -> None:
    mock_translator = MagicMock(
        return_value=Generation(code=CodeRust("pub fn helper() -> i32 { 1 }"))
    )
    symbol_translator = _StubSnippetTranslator(mock_translator)

    mock_wrapper_gen = MagicMock(return_value=Generation(code=_BINARY_TYPE_WRAPPER))
    symbol_wrapper = _StubWrapperGenerator(mock_wrapper_gen)

    harness, _, _, sys_c = _make_translator(
        tmp_path,
        is_bin=True,
        c_src=str(_BINARY),
        symbol_translator=symbol_translator,
        symbol_wrapper=symbol_wrapper,
        tests=None,
        max_iters=1,
    )
    crate_mock: MagicMock = harness.hybrid.crate  # type: ignore[assignment]
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
            "ideas.translate_recurrent.generate_unimplemented_function_wrapper",
            mock_unimplemented,
        ),
        patch(
            "ideas.translate_recurrent.generate_unimplemented_type_wrapper",
            return_value=(_BINARY_TYPE_WRAPPER, "test_my_struct_t"),
        ),
    ):
        pred = _run(harness, symbols=symbols, dependencies=dependencies)
    assert pred.complete

    # `helper` keeps its C definition: no wrapper claims it and no test needs it to
    assert mock_unimplemented.call_count == 0
    assert "int helper(my_struct_t s) { return s.x; }" in sys_c.read_text()

    # The struct is still wrapped: its round-trip tests validate the translated type
    # regardless of whether anything calls into Rust through a function wrapper
    assert harness.hybrid.wrappers == {struct_name: _BINARY_TYPE_WRAPPER}
    assert mock_wrapper_gen.call_count == 1
    rs_main_src = harness.rs_crate.main_src_path
    assert rs_main_src is not None
    assert rs_main_src.read_text().startswith(
        "#![forbid(unsafe_code)]\n#![allow(non_snake_case)]"
    )
    assert "use foo_rs::main;" in rs_main_src.read_text()
    assert "unimplemented!" not in rs_main_src.read_text()
    hybrid_lib_src = harness.hybrid.crate.lib_src_path
    assert hybrid_lib_src is not None
    _assert_generated_crate_lint_policy(
        hybrid_lib_src.read_text(), tmp_path / "hybrid-lint-probe"
    )

    # `main` is still wrapped, handing the entrypoint to the -rs crate
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


# These tests only exercise the bindings plumbing, so no LLM modules are needed
def _make_bindings_translator(tmp_path: Path):
    return _make_translator(tmp_path, symbol_translator=MagicMock(), symbol_wrapper=None)


def test_bindings_handle_type_free_bindings(tmp_path: Path) -> None:
    harness, _, _, _ = _make_bindings_translator(tmp_path)

    item = CodeRust('unsafe extern "C" {\n    pub static mut counter: i32;\n}\n')
    harness.hybrid.add_externs([item])

    # Nothing was hoisted alongside it, so only the trait and the values module render
    assert harness.hybrid.types == {}
    assert str(harness.hybrid.render()) == str(C_INTEROP_TRAIT + wrap_values(item))


def test_crate_context_always_exposes_the_interop_trait(tmp_path: Path) -> None:
    harness, _, _, _ = _make_bindings_translator(tmp_path)

    # Wrappers implement this trait, so it must be visible before anything is hoisted
    assert "pub trait CInterop" in str(harness.hybrid.render())

    types, consts = split_items(_BINDGEN_TYPES)
    harness.hybrid.set_types(types, consts)
    harness.hybrid.add_externs([_BINDGEN_BINDING])
    context = str(harness.hybrid.render())
    assert context.startswith("pub trait CInterop")
    assert "pub struct house_t {" in context
    assert "pub static mut the_house" in context

    # Reduced wrappers sit alongside the types they convert, at the same level
    impl = CodeRust("impl CInterop for house_t {\n    type Rust = libdriver_rs::HouseT;\n}")
    harness.hybrid.wrap(SymbolName("house_t"), impl)
    assert "impl CInterop for house_t {" in str(harness.hybrid.render())


def test_ffi_crate_follows_exported_wrappers(tmp_path: Path) -> None:
    ffi_src = tmp_path / "ffi" / "src" / "lib.rs"
    ffi_src.parent.mkdir(parents=True)
    ffi_crate = MagicMock()
    ffi_crate.lib_src_path = ffi_src
    harness, _, _, _ = _make_translator(
        tmp_path, symbol_translator=MagicMock(), symbol_wrapper=None, ffi_crate=ffi_crate
    )
    hybrid = harness.hybrid
    before = hybrid.snapshot()

    hybrid.wrap(
        SymbolName("c:@F@add"),
        CodeRust(
            '#[unsafe(export_name = "add")]\npub extern "C" fn add() { install(helper); }'
        ),
    )
    hybrid.wrap(
        SymbolName("c:mini.c@F@helper"),
        CodeRust('#[unsafe(export_name = "helper")]\npub extern "C" fn helper() {}'),
        scaffolding=True,
    )

    # Only the C library's own interface is exported from -ffi, but `add` can still name `helper`
    ffi = ffi_src.read_text()
    assert 'export_name = "add"' in ffi
    assert '\nextern "C" fn helper() {}' in ffi
    assert 'export_name = "helper"' not in ffi

    # Rewinding the hybrid rewinds -ffi with it
    hybrid.restore(before)
    assert 'export_name = "add"' not in ffi_src.read_text()


def test_ffi_crate_keeps_c_global_storage_and_sync(tmp_path: Path) -> None:
    ffi_src = tmp_path / "ffi" / "src" / "lib.rs"
    ffi_src.parent.mkdir(parents=True)
    ffi_crate = MagicMock()
    ffi_crate.lib_src_path = ffi_src
    harness, _, _, _ = _make_translator(
        tmp_path, symbol_translator=MagicMock(), symbol_wrapper=None, ffi_crate=ffi_crate
    )
    hybrid = harness.hybrid
    # Stands in for `import_symbols`, which needs a real C global to run bindgen on
    binding = CodeRust('unsafe extern "C" {\n    pub static mut counter: i32;\n}')
    hybrid.add_externs([binding])

    hybrid.wrap(
        SymbolName("c:@F@peek"),
        CodeRust(
            '#[unsafe(export_name = "peek")]\n'
            'pub extern "C" fn peek() -> i32 { unsafe { sync_counter_to_rust(); __c_globals::counter } }'
        ),
    )
    # A `static` C global is not part of the interface, but the wrappers still sync it
    hybrid.wrap(
        SymbolName("c:lib.c@counter"),
        CodeRust(
            "pub unsafe fn sync_counter_to_rust() { let _ = __c_globals::counter; }\n\n"
            "#[cfg(test)]\nmod test_var_counter {\n    #[test]\n    fn initial_value_matches() {}\n}"
        ),
    )

    # The C left in -sys only holds the globals' storage, which the wrappers reach as before
    ffi = ffi_src.read_text()
    _assert_generated_crate_lint_policy(ffi, tmp_path / "ffi-lint-probe")
    assert "use libfoo_sys as _;" in ffi
    assert "pub static mut counter: i32;" in ffi
    assert 'export_name = "peek"' in ffi
    assert "pub unsafe fn sync_counter_to_rust() { let _ = __c_globals::counter; }" in ffi
    assert "test_var_counter" not in ffi


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
