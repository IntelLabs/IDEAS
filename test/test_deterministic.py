#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ideas import ast
from ideas.ast_rust import CodeRust
from ideas.consolidate import get_symbols_and_dependencies
from ideas.model import Generation
from ideas.translate_snippet import _init_cache, _write_cache
from ideas.tools import check_rust

from test_translate_recurrent import _make_translator, _run, _StubSnippetTranslator


def _symbols(c: str) -> dict[str, ast.Symbol]:
    return ast.extract_info_c(ast.create_translation_unit(ast.CodeC(c))).symbols


def _translate(
    tmp_path: Path,
    c: str,
    *,
    cached: dict[str, CodeRust] | None = None,
    build: Callable[[str], tuple[bool, str]] | None = None,
) -> tuple[dict[str, ast.Symbol], list[tuple[str, ...]], list[dict[str, Any]], str]:
    symbols, dependencies = get_symbols_and_dependencies(
        [ast.extract_info_c(ast.create_translation_unit(ast.CodeC(c)))]
    )

    # The arguments of every model call
    calls: list[dict[str, Any]] = []

    def respond(**kwargs) -> Generation:
        calls.append(kwargs)
        return Generation(code=CodeRust(f"// translation {len(calls)}"))

    stub = _StubSnippetTranslator(respond)
    stub.cache = _init_cache(tmp_path / "cache.db")
    for name, rust in (cached or {}).items():
        _write_cache(stub.cache, name, ast.CodeC.join([symbols[name].code]), rust)

    harness, _, rs_lib, _ = _make_translator(
        tmp_path,
        c_src=c,
        symbol_translator=stub,
        symbol_wrapper=None,
        tests=None,
        max_iters=1,
    )
    if build is not None:
        harness.rs_crate.cargo_build.side_effect = lambda: build(rs_lib.read_text())  # type: ignore[attr-defined]

    pred = _run(harness, symbols=symbols, dependencies=dependencies)
    assert pred.complete
    return symbols, list(dependencies), calls, rs_lib.read_text()


def _snippet(symbols: dict[str, ast.Symbol], group: tuple[str, ...]) -> str:
    return str(ast.CodeC.join(symbols[n].code for n in group))


# Enums that translate statically


def test_enums_translate_to_integer_constants() -> None:
    symbols = _symbols("""
        enum N { NA, NB = -2 };
        enum Q { QA, QB = 0xffffffff };
        enum { AN = 0x100000000, type };
        typedef enum { UA } U_t;
        typedef U_t U2_t;
        typedef enum { DA1 } DA, DB;
        enum E { A = 1, B = A + 1, C };
        typedef enum E E2;
        int *cp = (int[]){ (enum { CL = 1 })0 };
    """).values()

    # Every symbol with one of these spellings shares the translation
    expected = {
        ("N", "NA", "NB"): "pub const NA: i32 = 0;\npub const NB: i32 = -2;\n",
        ("Q", "QA", "QB"): "pub const QA: u32 = 0;\npub const QB: u32 = 4294967295;\n",
        ("AN", "type"): (
            "pub const AN: u64 = 4294967296;\n"
            "#[allow(non_upper_case_globals)]\n"
            "pub const type_: u64 = 4294967297;\n"
        ),
        ("U_t", "UA"): "pub const UA: u32 = 0;\n",
        ("U2_t",): "// `U2_t` declares no enum constants\n",
        ("DA", "DB", "DA1"): "pub const DA1: u32 = 0;\n",
        ("E", "A", "B", "C"): (
            "pub const A: u32 = 1;\npub const B: u32 = 2;\npub const C: u32 = 3;\n"
        ),
        ("E2",): "// `E2` declares no enum constants\n",
        ("CL",): "pub const CL: u32 = 1;\n",
    }
    for spellings, rust in expected.items():
        found = [s for s in symbols if s.spelling in spellings]
        assert {s.spelling for s in found} == set(spellings)
        assert {str(s.deterministic_translation) for s in found} == {rust}


def test_translated_enums_build_together() -> None:
    symbols = _symbols("""
        enum N { NA, NB = -2 };
        enum { AN = 0x100000000, type };
        typedef enum { UA } U_t;
    """).values()
    rust = CodeRust.join(t for s in symbols if (t := s.deterministic_translation) is not None)

    # Repeated across an enum, its constants, and its typedefs, yet emitted once
    assert [line for line in str(rust).splitlines() if line] == [
        "pub const NA: i32 = 0;",
        "pub const NB: i32 = -2;",
        "pub const AN: u64 = 4294967296;",
        "#[allow(non_upper_case_globals)]",
        "pub const type_: u64 = 4294967297;",
        "pub const UA: u32 = 0;",
    ]
    success, output = check_rust(
        "#![forbid(unsafe_code)]\n" + str(rust),
        flags=["--crate-type=lib", "--emit=metadata"],
    )
    assert success, output


def test_translated_enums_keep_names_that_differ_only_in_case() -> None:
    symbols = _symbols("""
        enum lower { red };
        enum upper { RED = 1 };
    """).values()
    rust = CodeRust.join(t for s in symbols if (t := s.deterministic_translation) is not None)

    # C is case-sensitive, so upper-casing `red` would define `RED` twice; it keeps C's
    # spelling and allows the lint instead
    assert [line for line in str(rust).splitlines() if line] == [
        "#[allow(non_upper_case_globals)]",
        "pub const red: u32 = 0;",
        "pub const RED: u32 = 1;",
    ]
    success, output = check_rust(
        str(rust), flags=["--crate-type=lib", "--emit=metadata", "-Dnon_upper_case_globals"]
    )
    assert success, output


def test_translated_enums_skip_the_model(tmp_path: Path) -> None:
    symbols, _, calls, rust = _translate(
        tmp_path,
        """
        enum E { A = 1, B = A + 1, C };
        typedef enum T { TA, TB } T_t;
        int f(enum E e, T_t t) { return (e == C) + (t == TB); }
        """,
    )

    assert [str(c["snippet"]) for c in calls] == [str(symbols["c:@F@f"].code)]
    # Each enum lands once and whole, including constants nothing references
    for item in ("A: u32 = 1", "B: u32 = 2", "C: u32 = 3", "TA: u32 = 0", "TB: u32 = 1"):
        assert rust.count(f"pub const {item};") == 1, item
    assert "pub struct" not in rust
    assert "pub type" not in rust

    success, output = check_rust(rust, flags=["--crate-type=lib", "--emit=metadata"])
    assert success, output


def test_translated_enums_skip_the_cache(tmp_path: Path) -> None:
    cached = CodeRust("pub const A: i64 = 1;\npub const B: i64 = 2;")
    _, _, calls, rust = _translate(
        tmp_path, "enum E { A = 1, B };\n", cached={"c:@E@E": cached}
    )

    assert calls == []
    assert "pub const B: u32 = 2;" in rust
    assert "i64" not in rust


## Enums left to a model
def test_enums_with_a_type_in_a_value_go_to_the_model(tmp_path: Path) -> None:
    symbols, dependencies, calls, rust = _translate(
        tmp_path, "enum O { OA = sizeof(enum { IA = 3 }) };\n"
    )

    assert all(s.deterministic_translation is None for s in symbols.values())
    assert {str(c["snippet"]) for c in calls} == {_snippet(symbols, g) for g in dependencies}
    for name in ("OA", "IA"):
        assert not re.search(rf"\b{name}\b", rust), name


def test_enums_inside_other_declarations_go_to_the_model(tmp_path: Path) -> None:
    symbols, dependencies, calls, rust = _translate(
        tmp_path,
        """
        struct S { enum { SA, SB } kind; };
        int in_struct(struct S *s) { return s->kind == SB; }
        int in_param(enum P { PA, PB } p) { return p == PB; }
        void (*in_fn_ptr_param)(enum FP { FPA } x);
        int in_body(void) { enum L { LA = 7 }; return LA; }
        """,
    )

    assert all(s.deterministic_translation is None for s in symbols.values())
    assert {str(c["snippet"]) for c in calls} == {_snippet(symbols, g) for g in dependencies}
    for name in ("SA", "SB", "PA", "PB", "FPA", "LA"):
        assert not re.search(rf"\b{name}\b", rust), name


def test_enum_cycles_go_to_the_model(tmp_path: Path) -> None:
    # A GNU forward-declared enum puts `F1` and `enum G` in one SCC
    symbols, dependencies, calls, rust = _translate(
        tmp_path,
        """
        enum { N = sizeof(struct Arr *) };
        struct Arr { int a[N]; };
        enum G; enum F { F1 = sizeof(enum G *) }; enum G { G1 = F1 };
        """,
    )
    cycles = [g for g in dependencies if len(g) > 1]
    assert [set(g) for g in cycles] == [{"c:@Ea@N@N", "c:@S@Arr"}, {"c:@E@F@F1", "c:@E@G"}]

    # `G` translates alone, yet not in its cycle with `F1`
    assert symbols["c:@E@G"].deterministic_translation is not None
    asked = {str(c["snippet"]) for c in calls}
    for cycle in cycles:
        assert _snippet(symbols, cycle) in asked
    for name in ("N", "F1"):
        assert not re.search(rf"\b{name}\b", rust), name


def test_enums_that_fail_to_build_go_to_the_model(tmp_path: Path) -> None:
    # As if every enum item clashed with the crate
    symbols, _, calls, rust = _translate(
        tmp_path,
        """
        enum E { A = 1, B };
        typedef enum E E2;
        int f(E2 e) { return e == B; }
        """,
        build=lambda r: ("pub " not in r, "error[E0428]"),
    )

    asked = {str(c["snippet"]) for c in calls}
    assert str(symbols["c:@E@E"].code) in asked
    assert "pub " not in rust
    # A translation without items still builds
    assert str(symbols["c:file.c@T@E2"].code) not in asked
    # The failed static translation is never shown to the model as its prior attempt
    assert all(not c["prior_translation"] for c in calls)
