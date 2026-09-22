#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from pathlib import Path
from textwrap import dedent, indent
from unittest.mock import MagicMock

import dspy
import pytest

from clang.cindex import CursorKind

from ideas import ast
from ideas import wrapper as wrapper_mod
from ideas.ast_rust import BindgenName, CodeRust
from ideas.refine import Feedback
from ideas.tools import check_rust


@pytest.mark.parametrize(
    ("source", "symbol", "expected"),
    [
        (
            "int foo = 7;\n",
            "foo",
            dedent(
                """
                unsafe extern "C" {
                    pub static mut foo: ::std::os::raw::c_int;
                }
                """
            ).strip(),
        ),
        (
            "static int sfoo = 7;\n",
            "sfoo",
            dedent(
                """
                unsafe extern "C" {
                    pub static mut sfoo: ::std::os::raw::c_int;
                }
                """
            ).strip(),
        ),
        (
            "int arr[3] = {1,2,3};\n",
            "arr",
            dedent(
                """
                unsafe extern "C" {
                    pub static mut arr: [::std::os::raw::c_int; 3usize];
                }
                """
            ).strip(),
        ),
        (
            "int arr_unsized[] = {1,2,3};\n",
            "arr_unsized",
            dedent(
                """
                unsafe extern "C" {
                    pub static mut arr_unsized: [::std::os::raw::c_int; 0usize];
                }
                """
            ).strip(),
        ),
        (
            "static int sarr[2] = {4,5};\n",
            "sarr",
            dedent(
                """
                unsafe extern "C" {
                    pub static mut sarr: [::std::os::raw::c_int; 2usize];
                }
                """
            ).strip(),
        ),
        (
            "struct Point { int x; int y; };\nstruct Point pt = {1,2};\n",
            "pt",
            dedent(
                """
                #[repr(C)]
                #[derive(Debug, Copy, Clone)]
                pub struct Point {
                    pub x: ::std::os::raw::c_int,
                    pub y: ::std::os::raw::c_int,
                }

                unsafe extern "C" {
                    pub static mut pt: Point;
                }
                """
            ).strip(),
        ),
        (
            "struct Point { int x; int y; };\nstatic struct Point spt = {3,4};\n",
            "spt",
            dedent(
                """
                #[repr(C)]
                #[derive(Debug, Copy, Clone)]
                pub struct Point {
                    pub x: ::std::os::raw::c_int,
                    pub y: ::std::os::raw::c_int,
                }

                unsafe extern "C" {
                    pub static mut spt: Point;
                }
                """
            ).strip(),
        ),
        (
            "const int c = 9;\n",
            "c",
            dedent(
                """
                unsafe extern "C" {
                    pub static c: ::std::os::raw::c_int;
                }
                """
            ).strip(),
        ),
        (
            "int x = 0; int *px = &x;\n",
            "px",
            dedent(
                """
                unsafe extern "C" {
                    pub static mut px: *mut ::std::os::raw::c_int;
                }
                """
            ).strip(),
        ),
        (
            "int match = 1;\n",
            "match",
            dedent(
                """
                unsafe extern "C" {
                    #[link_name = "\\u{1}match"]
                    pub static mut match_: ::std::os::raw::c_int;
                }
                """
            ).strip(),
        ),
    ],
)
def test_bindgen_emits_expected_text_for_global_shapes(
    tmp_path: Path, source: str, symbol: str, expected: str
):
    c_path = tmp_path / "input.c"
    c_path.write_text(source)

    types = wrapper_mod.bindgen_types(c_path, [symbol])
    binding = wrapper_mod.bindgen_binding(c_path, symbol, types)

    # The two segments are complementary, so joined they are the full recursive binding
    assert str(types + binding).strip() == expected
    assert c_path.read_text() == source


def test_bindgen_restores_source_when_bindgen_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    c_path = tmp_path / "input.c"
    original_src = "static int foo = 7;\n"
    c_path.write_text(original_src)

    monkeypatch.setattr(
        wrapper_mod,
        "run_subprocess",
        lambda *_args, **_kwargs: (False, "", "boom", 1),
    )

    with pytest.raises(ValueError, match="Bindgen failed"):
        wrapper_mod.bindgen_binding(c_path, "foo", CodeRust())

    assert c_path.read_text() == original_src


def test_bindgen_raises_for_empty_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    c_path = tmp_path / "input.c"
    c_path.write_text("int foo(void) { return 1; }\n")

    monkeypatch.setattr(
        wrapper_mod,
        "run_subprocess",
        lambda *_args, **_kwargs: (True, "   \n", "", 0),
    )

    with pytest.raises(ValueError, match="no binding"):
        wrapper_mod.bindgen_binding(c_path, "foo", CodeRust())


def test_bindgen_bindings_batch_matches_one_call_per_symbol(tmp_path: Path):
    c_path = tmp_path / "input.c"
    source = dedent(
        """
        typedef struct { int x; int y; } point_t;
        static int alpha = 7;
        static point_t beta = {1, 2};
        static const char *gamma_s = "hi";
        static int match = 3;
        int use_alpha(void) { return alpha; }
        """
    ).lstrip()
    c_path.write_text(source)

    symbols = ["alpha", "beta", "gamma_s", "match", "use_alpha"]
    types = wrapper_mod.bindgen_types(c_path, symbols)
    batched = wrapper_mod.bindgen_bindings(c_path, symbols, types)

    # Splitting one run must give each symbol exactly what its own run would have
    assert batched == {s: wrapper_mod.bindgen_binding(c_path, s, types) for s in symbols}
    assert c_path.read_text() == source


def test_bindgen_bindings_keep_per_symbol_link_name_attributes(tmp_path: Path):
    c_path = tmp_path / "input.c"
    # A C name that is a Rust keyword gets renamed, so bindgen emits `#[link_name]` inside
    # the extern block rather than beside it
    source = dedent(
        """
        int match(double threshold) { return (int)threshold; }
        int loop = 3;
        int plain = 4;
        """
    ).lstrip()
    c_path.write_text(source)

    symbols = ["match", "loop", "plain"]
    types = wrapper_mod.bindgen_types(c_path, symbols)
    bindings = wrapper_mod.bindgen_bindings(c_path, symbols, types)

    # Each symbol keeps its own attribute, its own indentation, and none of the other's
    assert (
        str(bindings["match"]).strip()
        == dedent(
            """
            unsafe extern "C" {
                #[link_name = "\\u{1}match"]
                pub fn match_(threshold: f64) -> ::std::os::raw::c_int;
            }
            """
        ).strip()
    )
    assert (
        str(bindings["loop"]).strip()
        == dedent(
            """
            unsafe extern "C" {
                #[link_name = "\\u{1}loop"]
                pub static mut loop_: ::std::os::raw::c_int;
            }
            """
        ).strip()
    )
    assert (
        str(bindings["plain"]).strip()
        == dedent(
            """
            unsafe extern "C" {
                pub static mut plain: ::std::os::raw::c_int;
            }
            """
        ).strip()
    )


def _crate(types: CodeRust, *values: CodeRust) -> CodeRust:
    # Mirror the hybrid crate's layout: types bare at the root, values in `__c_globals`
    type_items, const_items = wrapper_mod.partition_values(types)
    return type_items + wrapper_mod.wrap_values(const_items, *values)


def test_values_module_reaches_the_types_when_the_crate_root_is_not_its_parent():
    types = CodeRust("#[repr(C)]\npub struct house_t {\n    pub floors: i32,\n}")
    values = wrapper_mod.wrap_values(
        CodeRust("pub static mut the_house: house_t = house_t { floors: 0 };")
    )

    # main.rs compiles lib.rs as `mod hybrid`, where `crate` is the binary root and the
    # types are one level down, so the module must reach them through `super`
    body = indent(str(types + values), "    ")
    success, output = check_rust(
        f"mod hybrid {{\n{body}\n}}", flags=["--crate-type=lib", "--emit=metadata"]
    )
    assert success, output


def test_bindgen_types_keep_a_bitfield_constructor_named_after_a_static(tmp_path: Path):
    c_path = tmp_path / "input.c"
    # `new_bitfield_1` takes a `pattern` parameter, which a crate-root static would make E0530
    c_path.write_text(
        dedent(
            """
            struct opts {
              unsigned int force : 1;
              unsigned int pattern : 1;
              int n;
            };
            static struct opts o = {0};
            static char *pattern = "x";
            """
        ).lstrip()
    )

    symbols = ["opts", "o", "pattern"]
    types = wrapper_mod.bindgen_types(c_path, symbols)
    assert "new_bitfield_" in str(types)
    assert "pub fn set_pattern" in str(types)

    bindings = wrapper_mod.bindgen_bindings(c_path, ["o", "pattern"], types)
    crate = _crate(types, *bindings.values())
    success, output = check_rust(str(crate), flags=["--crate-type=lib", "--emit=metadata"])
    assert success, output


def _bitfield_global_crate(c_path: Path, global_name: str) -> CodeRust:
    c_path.write_text(
        dedent(
            f"""
            struct opts {{
              unsigned int force : 1;
              int n;
            }};
            static struct opts o = {{0}};
            static int {global_name} = 1;
            """
        ).lstrip()
    )

    types = wrapper_mod.bindgen_types(c_path, ["opts", "o", global_name])
    bindings = wrapper_mod.bindgen_bindings(c_path, ["o", global_name], types)
    return _crate(types, *bindings.values())


@pytest.mark.parametrize("global_name", ["val", "this", "storage", "mask"])
def test_bindgen_types_survive_a_global_named_after_an_accessor_binding(
    tmp_path: Path, global_name: str
):
    crate = _bitfield_global_crate(tmp_path / "input.c", global_name)
    success, output = check_rust(str(crate), flags=["--crate-type=lib", "--emit=metadata"])
    assert success, output


@pytest.mark.xfail(
    strict=True,
    reason="`clang_make_bindable_` emits `extern int index;`, which clashes with clang's "
    "implicit POSIX builtin `index()`, so bindgen dies before rustc ever sees the crate.",
)
def test_bindgen_types_survive_a_global_named_after_a_clang_builtin(tmp_path: Path):
    crate = _bitfield_global_crate(tmp_path / "input.c", "index")
    success, output = check_rust(str(crate), flags=["--crate-type=lib", "--emit=metadata"])
    assert success, output


def test_function_wrapper_survives_a_parameter_named_after_an_enum_constant(tmp_path: Path):
    c_path = tmp_path / "input.c"
    # An anonymous enum's constants reach the crate root unprefixed, and C lets a parameter
    # shadow one; Rust instead reads the parameter as a constant pattern (E0005)
    c_path.write_text(
        dedent(
            """
            enum { flags = 1 };
            int use_flags(unsigned int flags) { return (int)flags; }
            """
        ).lstrip()
    )

    types = wrapper_mod.bindgen_types(c_path, ["use_flags", "flags"])
    assert "pub const flags:" in str(types)

    wrapper = wrapper_mod.generate_unimplemented_function_wrapper(c_path, "use_flags", types)
    assert wrapper is not None
    success, output = check_rust(
        str(_crate(types) + wrapper),
        flags=["--crate-type=lib", "--emit=metadata"],
    )
    assert success, output


def test_function_wrapper_survives_a_parameter_named_after_a_global(tmp_path: Path):
    c_path = tmp_path / "input.c"
    # C lets a parameter shadow a global, but the global's binding is a crate-root static
    # that a same-named parameter cannot shadow (E0530)
    c_path.write_text(
        dedent(
            """
            char *name;
            int use_name(const char *name) { return name != 0; }
            """
        ).lstrip()
    )

    types = wrapper_mod.bindgen_types(c_path, ["name", "use_name"])
    bindings = wrapper_mod.bindgen_bindings(c_path, ["name"], types)
    wrapper = wrapper_mod.generate_unimplemented_function_wrapper(c_path, "use_name", types)
    assert wrapper is not None

    # The wrapper's own validation never sees the bindings, so only the whole crate catches it
    crate = _crate(types, *bindings.values()) + wrapper
    success, output = check_rust(str(crate), flags=["--crate-type=lib", "--emit=metadata"])
    assert success, output


def _binding(c_path: Path, symbol: str) -> CodeRust:
    types = wrapper_mod.bindgen_types(c_path, [symbol])
    return types + wrapper_mod.bindgen_binding(c_path, symbol, types)


def test_bindgen_handles_dependent_declarations_for_target_global(tmp_path: Path):
    c_path = tmp_path / "input.c"
    array_decl = "int arr[] = {1,2,3};\n"
    dependent_decl = "static const int num_arr = sizeof(arr) / sizeof(arr[0]);\n"

    c_path.write_text(array_decl)
    baseline_binding = _binding(c_path, "arr")
    assert c_path.read_text() == array_decl

    c_path.write_text(array_decl + dependent_decl)
    dependent_binding = _binding(c_path, "arr")
    assert c_path.read_text() == array_decl + dependent_decl

    assert dependent_binding == baseline_binding


def test_bindgen_handles_dependent_declarations_for_target_function(tmp_path: Path):
    c_path = tmp_path / "input.c"
    baseline_source = "int f(int x) { return x + 1; }\n"
    dependent_source = baseline_source + "int (*pf)(int) = f;\n"

    c_path.write_text(baseline_source)
    baseline_binding = _binding(c_path, "f")
    assert c_path.read_text() == baseline_source

    c_path.write_text(dependent_source)
    dependent_binding = _binding(c_path, "f")
    assert c_path.read_text() == dependent_source

    assert dependent_binding == baseline_binding


@pytest.mark.parametrize(
    "signature",
    [
        wrapper_mod.FunctionWrapperSignature,
        wrapper_mod.TypeWrapperSignature,
        wrapper_mod.VariableWrapperSignature,
    ],
    ids=["function", "type", "variable"],
)
def test_wrapper_signatures_separate_the_two_crates(signature) -> None:
    # Merging these into one field makes the LLM reach for `crate::` paths that
    # actually live in the wrapped crate
    assert "crate" in signature.input_fields
    assert "wrapped_crate_code" in signature.input_fields


def _capture_wrapper_inputs(**session_kwargs) -> dict:
    captured = {}

    def record(**kwargs):
        captured.update(kwargs)
        return dspy.Prediction(wrapper=CodeRust("// wrapper"))

    symbol = MagicMock()
    symbol.name = "c:@F@bar"
    symbol.spelling = "bar"
    symbol.is_type = False
    symbol.is_function = True

    generator = wrapper_mod.WrapperGenerator(
        wrapper=MagicMock(return_value=MagicMock(side_effect=record)),  # type: ignore[arg-type]
        max_iters=1,
    )
    session = generator.session(
        symbol=symbol,
        unimplemented_wrapper=CodeRust("// template"),
        wrapped_crate="libfoo_rs",
        **session_kwargs,
    )
    with session:
        for attempt in session:
            attempt.accept()
    return captured


def test_translations_reach_the_prompt_as_wrapped_crate_code():
    translation = CodeRust("pub fn bar() {}")
    other = CodeRust("pub fn baz_wrapper() -> i32 { 0 }")
    captured = _capture_wrapper_inputs(
        wrapped_crate_code=CodeRust("pub struct Prior;"),
        translation=translation,
        other_wrappers=other,
    )

    # The -rs crate's code is not reachable via `crate::`, so it must not be labelled `crate`
    assert translation in captured["wrapped_crate_code"]
    assert CodeRust("pub struct Prior;") in captured["wrapped_crate_code"]
    assert translation not in captured["crate"]

    # `crate` carries only what actually lives in the hybrid crate
    assert other in captured["crate"]


def test_prior_rejection_reaches_the_first_prompt():
    # A retry hands over `prior_wrapper` but the code alone does not say what was wrong with it
    captured = _capture_wrapper_inputs(
        wrapped_crate_code=CodeRust(),
        translation=CodeRust(),
        prior_wrapper=CodeRust("// broken"),
        feedback=Feedback(
            review="the wrapper changed the program's behavior",
            build="error[E0308]: mismatched types",
            scope="do not add public items",
        ),
    )

    # Each channel keeps its own field so the model knows what produced it
    assert captured["feedback"] == "the wrapper changed the program's behavior"
    assert captured["build_feedback"] == "error[E0308]: mismatched types"
    assert captured["scope_feedback"] == "do not add public items"


def _generate_wrapper(
    generated: CodeRust | None,
    cache: Path | None = None,
    attempts: list[wrapper_mod.WrapperAttempt] | None = None,
    template: CodeRust = CodeRust("// template"),
) -> wrapper_mod.WrapperAttempt:
    symbol = MagicMock()
    symbol.name = "c:@F@bar"
    symbol.spelling = "bar"
    symbol.is_type = False
    symbol.is_function = True

    generator = wrapper_mod.WrapperGenerator(
        wrapper=MagicMock(  # type: ignore[arg-type]
            return_value=MagicMock(
                return_value=dspy.Prediction(wrapper=generated)
                if generated is not None
                else dspy.Prediction()
            )
        ),
        max_iters=1,
        cache=cache,
    )
    session = generator.session(
        symbol=symbol,
        wrapped_crate_code=CodeRust(),
        translation=CodeRust(),
        unimplemented_wrapper=template,
        wrapped_crate="libfoo_rs",
    )
    # Nothing here builds the wrapper, so the scope check alone decides the verdict
    attempt: wrapper_mod.WrapperAttempt | None = None
    with session:
        for attempt in session:
            if errors := wrapper_mod.scope_errors(attempt.wrapper, template):
                attempt.reject(scope=errors)
            else:
                attempt.accept()
            if attempts is not None:
                attempts.append(attempt)
    assert attempt is not None  # `max_iters` is at least 1, so the loop always ran
    return attempt


def test_catch_unwind_in_a_generated_wrapper_is_rejected():
    attempt = _generate_wrapper(
        CodeRust("fn bar() { let _ = std::panic::catch_unwind(|| ()); }")
    )

    # Swallowing the panic turns a divergence from C into a plausible return value
    assert "catch_unwind" in attempt.rejection.scope
    assert not attempt.success


@pytest.mark.parametrize(
    "body",
    ["unimplemented!()", 'unimplemented!("no i128 yet")', "unimplemented![]"],
    ids=["bare", "with_message", "brackets"],
)
def test_an_unimplemented_body_is_rejected(body: str):
    attempt = _generate_wrapper(CodeRust(f"pub fn bar_wrapper() -> i32 {{ {body} }}"))

    # A left-behind stub compiles and deviates from nothing, so only this rule catches it
    assert "unimplemented!()" in attempt.rejection.scope
    assert not attempt.success


def test_mentioning_unimplemented_in_a_comment_passes_scope_validation():
    # The instructions name the macro, so a model echoing them back must not be rejected
    attempt = _generate_wrapper(
        CodeRust("pub fn bar_wrapper() -> i32 { // not unimplemented!() anymore\n    0\n}")
    )

    assert attempt.rejection.scope == ""


def test_a_prediction_without_a_wrapper_is_rejected():
    stub = CodeRust("pub fn bar_wrapper() -> i32 { unimplemented!() }")
    attempt = _generate_wrapper(None, template=stub)

    # The caller is handed the template so it still has code to write out and build
    assert attempt.wrapper == stub
    assert "unimplemented!()" in attempt.rejection.scope
    assert not attempt.success


def test_a_wrapper_without_catch_unwind_passes_scope_validation():
    attempt = _generate_wrapper(CodeRust("fn bar() { libfoo_rs::bar() }"))

    assert attempt.rejection.scope == ""


def test_importing_catch_unwind_is_rejected():
    # The import makes every later call site a bare identifier, so it is the last place
    # the panic API is still recognizable
    attempt = _generate_wrapper(
        CodeRust(
            "use std::panic::{catch_unwind, AssertUnwindSafe};\n"
            "fn bar() { let _ = catch_unwind(AssertUnwindSafe(|| ())); }"
        )
    )

    assert "catch_unwind" in attempt.rejection.scope
    assert not attempt.success


@pytest.mark.xfail(
    strict=True,
    reason="`uses_catch_unwind` only accepts a matched path whose tail is literally "
    "`panic`, which neither an aliased module nor a glob import leaves at the call site.",
)
@pytest.mark.parametrize(
    "generated",
    [
        "use std::panic as p;\nfn bar() { let _ = p::catch_unwind(|| ()); }",
        "use std::panic::*;\nfn bar() { let _ = catch_unwind(|| ()); }",
    ],
    ids=["module_alias", "glob_import"],
)
def test_aliased_catch_unwind_is_rejected(generated: str):
    attempt = _generate_wrapper(CodeRust(generated))

    assert "catch_unwind" in attempt.rejection.scope
    assert not attempt.success


def test_wrapping_a_c_function_named_catch_unwind_passes_scope_validation():
    attempt = _generate_wrapper(
        CodeRust(
            '#[unsafe(export_name = "catch_unwind")]\n'
            'pub extern "C" fn catch_unwind() { libfoo_rs::catch_unwind() }'
        )
    )

    assert attempt.rejection.scope == ""


def test_mentioning_catch_unwind_in_a_comment_passes_scope_validation():
    attempt = _generate_wrapper(
        CodeRust("fn bar() { // no catch_unwind here\n    libfoo_rs::bar()\n}")
    )

    assert attempt.rejection.scope == ""


def test_a_cached_wrapper_using_catch_unwind_fails_scope_validation(tmp_path: Path):
    cache = wrapper_mod._init_cache(tmp_path / "cache.db")
    poisoned = CodeRust("fn bar() { let _ = std::panic::catch_unwind(|| ()); }")
    wrapper_mod._write_cache(cache, "bar", CodeRust("// template"), poisoned)

    attempts: list[wrapper_mod.WrapperAttempt] = []

    # The cache key does not cover the instructions, so entries predating the rule survive
    _generate_wrapper(
        CodeRust("fn bar() { libfoo_rs::bar() }"),
        cache=cache,
        attempts=attempts,
    )

    # The rejection costs an iteration, so the clean regeneration is the session's result
    replayed = attempts[0]

    # A replayed entry is scope-checked like a fresh one, so the rule still applies to it
    assert not replayed.pred.was_generated
    assert "catch_unwind" in replayed.rejection.scope
    assert not replayed.success


def test_type_wrapper_template_is_an_interop_impl(tmp_path: Path):
    c_path = tmp_path / "input.c"
    c_path.write_text("typedef struct { int floors; } house_t;\nhouse_t the_house;\n")

    types = wrapper_mod.bindgen_types(c_path, ["house_t"])
    wrapper, tests_mod = wrapper_mod.generate_unimplemented_type_wrapper("house_t", types)
    assert tests_mod == "test_house_t"

    # The C layout is hoisted, so only the impl and its test skeleton reach the wrapper
    assert "pub struct house_t" in str(types)
    assert "pub struct house_t" not in str(wrapper)

    assert "impl CInterop for house_t" in str(wrapper)
    # The associated type is the single slot the LLM is asked to fill
    assert "type Rust = ();" in str(wrapper)
    assert "fn round_trip_zeroed" in str(wrapper)
    assert "fn round_trip_nontrivial" in str(wrapper)

    # todo!() keeps validate_changes from constraining the signature edits
    assert "unimplemented!()" not in str(wrapper)


def test_nested_record_is_wrapped_under_its_bindgen_name(tmp_path: Path):
    c_path = tmp_path / "input.c"
    c_path.write_text(
        "struct histindex { struct record { unsigned ptr; } **records; };\n"
        "void use_histindex(struct histindex *h);\n"
    )

    # bindgen keys its allowlist by the name it generates, so the bare tag misses
    assert "histindex_record" not in str(wrapper_mod.bindgen_types(c_path, ["record"]))

    types = wrapper_mod.bindgen_types(c_path, ["histindex_record"])
    assert "pub struct histindex_record" in str(types)

    # ...while the impl must name the record after its parent, or it would not compile
    wrapper, tests_mod = wrapper_mod.generate_unimplemented_type_wrapper(
        "histindex_record", types
    )
    assert tests_mod == "test_histindex_record"
    assert "impl CInterop for histindex_record" in str(wrapper)


@pytest.mark.parametrize(
    "code",
    [
        "typedef struct MyStruct { int x; } my_struct_t;",
        "typedef struct { int x; } my_struct_t;",
        "typedef struct my_struct_t { int x; } my_struct_t;",
    ],
    ids=["named_tag", "anonymous_tag", "tag_named_after_typedef"],
)
def test_struct_typedef_is_wrapped_under_the_struct_bindgen_declares(tmp_path: Path, code: str):
    # bindgen renders a named tag as `pub struct MyStruct` plus `pub type my_struct_t =
    # MyStruct`, and `_wrap_type` implements `CInterop` on the typedef's `bindgen_name`, so
    # that name has to be the struct bindgen declares rather than the alias.
    c_path = tmp_path / "input.c"
    c_path.write_text(code + "\n")

    tr = ast.extract_info_c(ast.create_translation_unit(ast.CodeC(code)))

    # An unnamed tag changes the typedef's USR, so find it by kind
    (typedef,) = [s for s in tr.symbols.values() if s.kind == CursorKind.TYPEDEF_DECL]
    assert (bindgen_name := typedef.bindgen_name) is not None

    # The alias would compile as an `impl` target, so only the declaration catches the miss
    types = wrapper_mod.bindgen_types(c_path, [bindgen_name])
    assert f"pub struct {bindgen_name}" in str(types)


def test_variable_wrapper_template_pairs_sync_fns_with_tests(tmp_path: Path):
    c_path = tmp_path / "input.c"
    c_path.write_text("typedef struct { int floors; } house_t;\nhouse_t the_house;\n")

    types = wrapper_mod.bindgen_types(c_path, ["the_house"])
    binding = CodeRust('unsafe extern "C" { pub static mut the_house: house_t; }')
    wrapper, tests_mod, sync_fns = wrapper_mod.generate_unimplemented_variable_wrapper(
        "the_house", types, binding
    )
    assert tests_mod == "test_var_the_house"
    assert sync_fns == ("sync_the_house_to_rust", "sync_the_house_to_c")

    # The extern binding is hoisted separately, so the wrapper never redeclares it
    assert "pub static mut the_house" not in str(wrapper)
    assert "mod test_var_the_house {" in str(wrapper)
    for fn in sync_fns:
        assert f"pub unsafe fn {fn}()" in str(wrapper)
    assert "fn initial_value_matches" in str(wrapper)
    assert "fn round_trip_nontrivial" in str(wrapper)

    # todo!() keeps validate_changes from constraining the generated bodies
    assert "unimplemented!()" not in str(wrapper)


def test_const_variable_wrapper_template_omits_the_sync_pair(tmp_path: Path):
    c_path = tmp_path / "input.c"
    c_path.write_text("const int limit = 7;\n")
    types = wrapper_mod.bindgen_types(c_path, ["limit"])

    # bindgen drops `mut` for a const-qualified global, whose Rust counterpart is therefore
    # immutable: sync_to_rust could only be a stub and the round trip could never hold
    binding = CodeRust('unsafe extern "C" { pub static limit: ::std::os::raw::c_int; }')
    wrapper, tests_mod, sync_fns = wrapper_mod.generate_unimplemented_variable_wrapper(
        "limit", types, binding
    )

    assert tests_mod == "test_var_limit"
    assert sync_fns == ()
    assert "sync_" not in str(wrapper)
    assert "fn round_trip_nontrivial" not in str(wrapper)
    assert "fn initial_value_matches" in str(wrapper)


def test_variable_test_module_cannot_collide_with_a_type_of_the_same_name(tmp_path: Path):
    c_path = tmp_path / "input.c"
    c_path.write_text("typedef struct { int floors; } house_t;\nhouse_t house_t_value;\n")
    types = wrapper_mod.bindgen_types(c_path, ["house_t"])

    # C keeps tags and ordinary identifiers in separate namespaces, so a variable and a
    # type can share a spelling while both wrappers land in the same crate root
    type_wrapper, type_mod = wrapper_mod.generate_unimplemented_type_wrapper("house_t", types)
    var_wrapper, var_mod, _ = wrapper_mod.generate_unimplemented_variable_wrapper(
        "house_t", types, CodeRust("pub static mut house_t: house_t;")
    )

    assert type_mod != var_mod
    assert f"mod {var_mod} " in str(var_wrapper)
    assert f"mod {type_mod} " in str(type_wrapper)


def test_type_wrapper_template_names_its_test_module(tmp_path: Path):
    c_path = tmp_path / "input.c"
    c_path.write_text(
        "typedef struct { int floors; } house_t;\n"
        "typedef struct { int wheels; } car_t;\n"
        "house_t the_house;\n"
        "car_t the_car;\n"
    )
    types = wrapper_mod.bindgen_types(c_path, ["house_t", "car_t"])

    # Every wrapper shares the crate root, so a fixed `mod tests` would collide
    house, _ = wrapper_mod.generate_unimplemented_type_wrapper("house_t", types)
    car, _ = wrapper_mod.generate_unimplemented_type_wrapper("car_t", types)

    assert "mod test_house_t {" in str(house)
    assert "mod test_car_t {" in str(car)
    assert "mod tests" not in str(house)


def test_narrowing_the_allowlist_only_deletes_types(tmp_path: Path):
    c_path = tmp_path / "input.c"
    c_path.write_text(
        "typedef struct { union { int i; float f; } payload; } house_t;\n"
        "typedef struct { int wheels; } car_t;\n"
        "void use_house(house_t *h);\n"
        "void use_car(car_t *c);\n"
    )

    full = wrapper_mod.bindgen_types(c_path, ["house_t", "car_t", "use_house", "use_car"])
    sliced = wrapper_mod.bindgen_types(c_path, ["use_house"])

    # Types unreachable from the allowlisted symbol drop out
    assert "pub struct car_t" in str(full)
    assert "car_t" not in str(sliced)

    # Anonymous types are named after their parent rather than a per-run counter, so a
    # narrowed run never renames them out of sync with the crate written to disk
    assert "house_t__bindgen_ty_1" in str(full)
    for item in str(sliced).split("#[repr(C)]"):
        assert item.strip() in str(full)


def test_splitting_types_keys_every_item_by_the_type_it_belongs_to(tmp_path: Path):
    c_path = tmp_path / "input.c"
    c_path.write_text(
        "typedef enum { RED, GREEN } color_t;\n"
        "typedef struct { union { int i; float f; } payload; color_t hue; } house_t;\n"
        "typedef house_t alias_t;\n"
        "void use_house(alias_t *h);\n"
    )

    types = wrapper_mod.bindgen_types(c_path, ["house_t", "alias_t", "use_house"])
    split = wrapper_mod.split_types(types)

    # Attributes stay with the item they decorate rather than starting a bucket
    assert "#[repr(C)]\n#[derive" in str(split[BindgenName("house_t")])
    assert str(split[BindgenName("house_t")]).count("pub struct") == 1

    # Enum companion constants have no declaration of their own, so they ride with the enum
    assert "pub const color_t_RED: color_t = 0;" in str(split[BindgenName("color_t")])
    assert "pub type color_t" in str(split[BindgenName("color_t")])

    # Nested anonymous records are separate items, reachable through their parent's fields
    assert "house_t__bindgen_ty_1" in split

    # Nothing is dropped or duplicated on the way in
    lines = sorted(
        line for code in split.values() for line in str(code).splitlines() if line.strip()
    )
    assert lines == sorted(line for line in str(types).splitlines() if line.strip())


def test_splitting_types_attaches_bindgen_helper_impls_to_their_type(tmp_path: Path):
    c_path = tmp_path / "input.c"
    c_path.write_text(
        "typedef struct { unsigned flag : 1; } flags_t;\n"
        "typedef struct { int plain; } plain_t;\n"
        "void use_both(flags_t *f, plain_t *p);\n"
    )

    split = wrapper_mod.split_types(
        wrapper_mod.bindgen_types(c_path, ["flags_t", "plain_t", "use_both"])
    )

    # The helper's generic impl blocks are ~130 lines that would otherwise land in the
    # preamble and be charged to every slice, including ones with no bitfields at all
    assert wrapper_mod.TYPES_PREAMBLE not in split
    assert "impl<Storage> __BindgenBitfieldUnit<Storage>" in str(
        split[BindgenName("__BindgenBitfieldUnit")]
    )


def test_libc_internals_are_collapsed_to_opaque_blobs(tmp_path: Path):
    c_path = tmp_path / "input.c"
    c_path.write_text(
        "#include <stdio.h>\n"
        "#include <pthread.h>\n"
        "#include <locale.h>\n"
        "struct probe { FILE *fp; pthread_mutex_t mtx; locale_t loc; };\n"
        "void use_probe(struct probe *p);\n"
    )

    types = wrapper_mod.bindgen_types(c_path, ["probe", "use_probe"])

    # A single `FILE *` otherwise drags ~30 unreadable glibc fields into every prompt
    assert "_bindgen_opaque_blob" in str(types)
    assert "pub struct _IO_marker" not in str(types)
    assert "pub _IO_read_ptr" not in str(types)

    # The typedefs themselves must stay concrete: making them opaque drops their
    # definition while references survive, which fails to compile
    assert "pub struct probe" in str(types)
    assert "pub union pthread_mutex_t" in str(types)


@pytest.mark.parametrize(
    ("source", "symbol", "item"),
    [
        ("struct twin { int x; };\nstruct twin twin;\n", "twin", "pub static mut twin: twin;"),
        ("struct twin { int x; };\nvoid twin(void);\n", "twin", "pub fn twin();"),
    ],
)
def test_tag_namespace_survives_a_same_named_variable_or_function(
    tmp_path: Path, source: str, symbol: str, item: str
):
    c_path = tmp_path / "input.c"
    c_path.write_text(source)

    types = wrapper_mod.bindgen_types(c_path, [symbol])
    binding = wrapper_mod.bindgen_binding(c_path, symbol, types)

    # bindgen keys items by name alone, so a by-name blocklist would take the tag down
    # with the identifier and leave the binding referring to a type nobody defines
    assert "pub struct twin" in str(types)

    # ...and a by-name allowlist would re-emit that same tag alongside the item
    assert "pub struct twin" not in str(binding)
    assert item in str(binding)
