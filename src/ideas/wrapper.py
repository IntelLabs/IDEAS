#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import re
import sqlite3
import logging
from pathlib import Path
from collections.abc import Callable

import dspy
from dspy.utils.exceptions import AdapterParseError
from dspy.utils.usage_tracker import track_usage
from dspy.dsp.utils.settings import settings

from ideas.tools import check_rust, run_subprocess
from ideas.ast import CodeC, Symbol, clang_make_bindable_
from ideas.ast_rust import CodeRust, validate_changes, mangle
from ideas.model import format_usage

logger = logging.getLogger("ideas.wrapper")


class FunctionWrapperSignature(dspy.Signature):
    """
    Generate a C-compatible FFI wrapper for `{wrapped_crate}::{symbol_name}` in a hybrid C/Rust build where C globals and the Rust port must stay in sync.

    # Goal

    Produce a `wrapper` that callers of the original C symbol can link against unchanged. The implementation of `{wrapped_crate}::{symbol_name}` lives in dependency crate `{wrapped_crate}`; the wrapper will be written to "{wrapper_path}".

    # Template

    - Use `example_wrapper` as the template for `wrapper`. Preserve its function signature, attributes, and module structure exactly.
    - Replace only the `unimplemented!()` body with an implementation that calls `{wrapped_crate}::{symbol_name}`.

    # Type conversions

    Types in `crate::` (bindgen-generated, C-compatible layout) are *not* layout-compatible with those in `{wrapped_crate}::` (idiomatic Rust). The wrapper must:

    1. Copy field values from each `crate::` argument into a fresh `{wrapped_crate}::` value before the call.
    2. Call `{wrapped_crate}::{symbol_name}` with the converted values.
    3. Copy result/output values back from `{wrapped_crate}::` types into the `crate::` types the C ABI expects.

    Use `support_code` to recover the original C types behind opaque or erased Rust types (notably `void*`, `*mut c_void`, and untyped byte buffers) so each field is converted at its true C type.

    When converting a struct argument, check `crate` for a `c_to_r` / `r_to_c` pair generated for that type. If one exists, call `c_to_r(ptr)` (passing a `*const` pointer) instead of converting fields inline. Likewise use `r_to_c(&rust_value, c_out_ptr)` (passing a reference and a `*mut` pointer) when writing a result or out-parameter back to a `crate::` type — `r_to_c` writes in-place and returns nothing.

    # Global synchronization

    If `{wrapped_crate}::{symbol_name}` reads or writes globals, synchronize them in the wrapper:

    - Each C global is exposed in `crate` as a `pub static mut` bearing the variable's name. Locate its full path by inspecting `crate` directly.
    - Before the call, copy each readable global from its `pub static mut` in `crate` into the corresponding Rust global in `{wrapped_crate}`. If the global's type has a `c_to_r` function in `crate`, use it for the conversion; otherwise copy field-by-field.
    - After the call, copy each writable global from `{wrapped_crate}` back to its `pub static mut` in `crate`. If the global's type has an `r_to_c` function in `crate`, use it for the conversion; otherwise copy field-by-field.

    # Raw pointer handling

    - Null-check every raw pointer parameter before its first dereference. On null, return the same value the C function returns for null input (typically an error code, `false`, `-1`, or `null`) — never dereference and panic. Use `support_code` to determine the correct null-input sentinel.
    - Treat every mutable raw pointer parameter as in-out unless `support_code` clearly proves it is read-only.
    - For every out / in-out pointer parameter, write the converted result back through the raw pointer after the Rust call. Struct and array out-parameters require full field/element write-back.
    - Do not route pointer-identity comparisons through detached clones/copies; identity must survive the boundary.

    # Mutable `char*` / byte buffers

    Reproduce C buffer-mutation semantics exactly:

    - If the Rust function computes a normalized or truncated value, write it back into the caller's buffer.
    - Classify each pointer+length input by C semantics before conversion: if the C code treats it as a string (`strcmp`, `strlen`, `%s`, token parsing, command dispatch, pattern matching), normalize at ingress by truncating at the first `\0`; if it is a fixed-length or binary buffer, preserve embedded `\0` bytes and use the explicit length.
    - Apply the same normalization policy to all operands in the same logical operation. Do not compare a length-decoded string that still contains trailing `\0` against a C-string-decoded operand that was truncated at `\0`.
    - When both pointer and length are present for string-style data, use length only as a safety bound for reads; derive semantic content from C string termination and stop at the first `\0`.
    - Preserve NUL termination wherever C expects it; never write past the C-implied capacity.
    - When C truncates a buffer by writing `'\0'` at a position found by a search (e.g., `strstr`, `strchr`, or a manual scan), the wrapper must re-derive that same offset from the raw buffer and write the NUL byte explicitly — even if the Rust function has internalized the truncation and does not expose the offset. Use `support_code` to recover the exact search function, offset, and capacity assumptions the C original relied on.
    - Treat in-place buffer mutations as **primary observable effects**. Callers (and tests) assert them directly; omitting them silently fails every assertion on the buffer regardless of the parsed return values.

    # Panic safety at the ABI boundary

    Wrap every call into Rust code that could panic in `std::panic::catch_unwind`. On a caught panic, return the appropriate C error value for the return type (`0`, `false`, `-1`, `null`, ...). Letting a panic cross an `extern "C"` boundary is undefined behavior and aborts the process in practice. Omit `catch_unwind` only when the called Rust code provably cannot panic.

    # Calling libc / system functions

    The crate already depends on the `libc` crate. When the wrapper needs to call a C standard library or POSIX function (e.g. `fdopen`, `close`, `malloc`, `free`, `memcpy`, `strlen`, `open`, `read`, `write`, `fopen`, `fclose`, ...), call it through the `libc` crate (`unsafe {{ ::libc::fdopen(fd, mode) }}`).

    - **Do not** emit `extern "C" {{ ... }}` (or `unsafe extern "C" {{ ... }}`) blocks declaring libc / POSIX / system functions, and do not emit `#[link(name = "c")]` (or similar) link attributes for libc symbols.
    - The only `extern "C"` items permitted in generated wrapper code are those already present in `example_wrapper`; do not introduce any new `extern "C"` items.
    - If a needed symbol is not available in `libc`, prefer a safe Rust equivalent from `std` (e.g. `std::ptr`, `std::ffi::CStr`, `std::fs`, `std::io`). If neither is available, do not declare a new foreign function; explain the limitation in your reasoning.

    # Hard constraints

    - Do not relax behavior, skip write-backs, or use placeholder/stub logic.
    - Do not declare libc / POSIX functions in `extern "C"` blocks; call them via the `libc` crate.

    # Inputs and feedback

    - `support_code`: the original C source that was translated to Rust.
    - `prior_wrapper`: a previous attempt to fix, if any.
    - `build_feedback`: errors from `cargo build`. Address them.
    - `scope_feedback`: deviations from the `example_wrapper` template. Address them.
    """

    # FIXME: Move crate and example_wrapper into instructions?
    crate: CodeRust = dspy.InputField()
    support_code: CodeC = dspy.InputField()
    example_wrapper: CodeRust = dspy.InputField()
    prior_wrapper: CodeRust = dspy.InputField()
    build_feedback: str = dspy.InputField()
    scope_feedback: str = dspy.InputField()

    wrapper: CodeRust = dspy.OutputField()


def generate_unimplemented_function_wrapper(path: Path, symbol_name: str) -> CodeRust | None:
    # unsafe extern "C" {
    #     #[link_name = "\u{1}match"]
    #     pub fn match_(
    #         threshold: f64,
    #     ) -> ::std::os::raw::c_int;
    # }
    bindgen_wrapper = bindgen(path, symbol_name)

    # #[unsafe(export_name="match")]
    # pub extern "C" fn match_(
    #     threshold: f64,
    # ) -> ::std::os::raw::c_int {
    #     unimplemented!()
    # }
    unimplemented_wrapper = re.sub(
        r'unsafe extern "C" {\s*.*\s+pub fn (.*);\s+}',
        rf'#[unsafe(export_name="{symbol_name}")]\npub extern "C" fn \1 {{\n    unimplemented!()\n}}',
        str(bindgen_wrapper),
        flags=re.DOTALL,
    )
    if unimplemented_wrapper == str(bindgen_wrapper):
        raise ValueError(
            f"Failed to convert bindgen output to function for `{symbol_name}`!\nWrapper:\n{unimplemented_wrapper}"
        )

    # Format unimplemented wrapper using rustfmt
    ok, unimplemented_wrapper, error, _ = run_subprocess(
        ["rustfmt"], input=unimplemented_wrapper
    )
    if not ok:
        raise ValueError(f"rustfmt failed!\n{error}")

    # Validate the template
    success, output = check_rust(
        unimplemented_wrapper, flags=["--crate-type", "lib", "--emit", "metadata"]
    )

    # Rust does not support C-compatible variadic functions on stable Rust (they require the nightly-only `c_variadic` feature):
    # https://github.com/rust-lang/rust/issues/44930
    if "error[E0658]: C-variadic functions are unstable" in output:
        return None

    if not success:
        raise ValueError(
            f"Failed to validate wrapper template for `{symbol_name}`!\nWrapper:\n{unimplemented_wrapper}\nError:\n{output}"
        )
    return CodeRust(unimplemented_wrapper)


class TypeWrapperSignature(dspy.Signature):
    """
    Generate `c_to_r` and `r_to_c` conversion functions for `{wrapped_crate}::{type_name}` in a hybrid C/Rust build.

    # Goal

    Produce a `wrapper` containing two unsafe Rust synchronization functions:
    - `c_to_r`: reads from a C-layout allocation (via `*const`) and produces an idiomatic Rust value. Does **not** take ownership of C memory.
    - `r_to_c`: writes an idiomatic Rust value back into an **existing** C-layout allocation (via `*mut`). Does **not** allocate a new C value — it writes in-place.

    These functions will be written to "{wrapper_path}" and used as FFI glue between the C-layout types exposed by `bindgen` and the safe Rust types in `{wrapped_crate}`.

    For pointer fields that form a linked structure (e.g. a linked list), `r_to_c` must walk the Rust chain and the existing C chain in parallel:
    - If both sides have a node: write data in-place and recurse.
    - If Rust has a node but C does not (Rust function added a node): allocate a new C node via `::libc::malloc(std::mem::size_of::<CType>())` — this uses the same allocator as C's `malloc`, so C can safely `free()` it later. Do **not** use `Box::into_raw` for this; `Box` uses Rust's allocator which C cannot `free()`.
    - If C has a node but Rust does not (Rust function removed a node): `::libc::free()` the C node and write null.

    # Template

    - Use `example_wrapper` as the starting template for `wrapper`.
    - The `()` placeholder types in the `c_to_r` and `r_to_c` signatures **must** be replaced with the actual idiomatic Rust type from `{wrapped_crate}::{type_name}`. Keeping `()` is not a valid implementation.
    - Replace the `todo!()` bodies with field-by-field conversion implementations.
    - Do **not** add, remove, or rename functions beyond what the template contains. You may add `use` items if needed.

    # Type mapping

    Types in `crate::` (bindgen-generated, C-compatible layout) are structurally equivalent to their C originals but are **not** the same types as those in `{wrapped_crate}::` (idiomatic Rust). Use `support_code` (the original C source) and `crate` (the existing Rust translations) to determine the correct field-by-field mapping.

    - For each field in `crate::{type_name}`, locate the corresponding field in `{wrapped_crate}::{type_name}` and convert its value.
    - Primitive numeric fields (`c_int`, `c_uint`, `c_long`, etc.) map to their Rust integer equivalents (`i32`, `u32`, `i64`, etc.) with an `as` cast.
    - `*const c_char` / `*mut c_char` fields that represent strings map to `String` or `Option<String>` in `{wrapped_crate}::` — use `std::ffi::CStr` for the conversion.
    - Raw pointer fields (`*mut T`, `*const T`) that are nullable map to `Option<...>` in `{wrapped_crate}::` — use `ptr::NonNull` or a null check.
    - Nested struct fields: call the corresponding `c_to_r` / `r_to_c` for that field's type if one exists in `other_wrappers`; otherwise convert field-by-field inline.
    - Array fields: convert element-by-element.

    # Round-trip correctness

    The two functions must satisfy: given a valid C-layout input `cs`, after calling `r_to_c(&c_to_r(&cs), &mut cs)` the fields of `cs` must equal their original values. Pay special attention to:
    - Fields that are zero-initialized in the C layout but have `Default` values in Rust.
    - Pointer fields: `null` must round-trip to `null`. For non-null pointers, the **data** at the pointed address must be preserved; the pointer address itself is preserved naturally because `r_to_c` writes in-place. Do **not** use global state (provenance tables, `OnceLock`, `Mutex`, etc.) to track pointer addresses — that is a design smell indicating the wrong approach.
    - String fields where the C layout stores a pointer (document if a true round-trip is impossible without re-allocation).

    # Hard constraints

    - `unsafe` is permitted and expected when crossing the C/Rust boundary (e.g., dereferencing raw pointers, reading C-allocated memory). These functions ARE the FFI bridge — the no-unsafe constraint belongs to the safe Rust translation, not here.
    - Do not add `extern "C"` items or FFI exports — these are conversion functions only, not ABI entry points.
    - Do not use placeholder/stub logic or `todo!()` / `unimplemented!()` in the final output.

    # Round-trip tests

    After the conversion functions, fill in the `#[cfg(test)]` skeleton from `example_wrapper` with at least two tests:

    - `round_trip_zeroed`: construct the simplest valid C-layout input (all primitive fields 0, all pointers null if null is a valid input). Call `let rs = unsafe {{ c_to_r(&cs) }}; unsafe {{ r_to_c(&rs, &mut cs) }};` and assert each field of `cs` equals its original value with field-by-field `assert_eq!`. If a zeroed instance would cause undefined behavior inside the conversion (e.g. a null pointer would be dereferenced), construct instead the simplest valid input and document why zeroed is unsafe.
    - `round_trip_nontrivial`: construct an instance with representative non-zero field values that exercise the real conversion path — non-null pointers (via `Box::leak`, a stack address cast, or a small allocation), non-zero integers, and non-trivial sizes. Call `let rs = unsafe {{ c_to_r(&cs) }}; unsafe {{ r_to_c(&rs, &mut cs) }};` and assert each field of `cs` equals its original value with field-by-field `assert_eq!`. This test is especially important for pointer-bearing types where `round_trip_zeroed` does not exercise the pointer path.
    - Tests may use `unsafe`. The `todo!()` in the skeleton is not a valid test body — replace it with a real implementation.

    # Inputs and feedback

    - `support_code`: the original C source that was translated to Rust.
    - `prior_wrapper`: a previous attempt, if any.
    - `build_feedback`: errors from `cargo build`. Address them.
    - `scope_feedback`: deviations from the `example_wrapper` template. Address them.
    """

    crate: CodeRust = dspy.InputField()
    support_code: CodeC = dspy.InputField()
    example_wrapper: CodeRust = dspy.InputField()
    prior_wrapper: CodeRust = dspy.InputField()
    build_feedback: str = dspy.InputField()
    scope_feedback: str = dspy.InputField()

    wrapper: CodeRust = dspy.OutputField()


def generate_unimplemented_type_wrapper(path: Path, type_name: str) -> CodeRust:
    # bindgen matches `--allowlist-item` against the mangled Rust name and emits that
    # name in its output, so the template must reference the mangled name rather than
    # the raw C spelling (they differ for names colliding with Rust keywords).
    rust_name = mangle(type_name)

    # Get the C-layout struct definition from bindgen so the LLM sees the exact
    # field names and types.  The () placeholder marks where the LLM should fill
    # in the idiomatic Rust type from the wrapped crate.
    # Unlike functions/variables, struct types are already visible to bindgen
    # without clang_make_bindable_, so we call bindgen directly.
    ok, binding, error, _ = run_subprocess(
        [
            "bindgen",
            "--disable-header-comment",
            "--no-doc-comments",
            "--no-layout-tests",
            "--sort-semantically",
            str(path),
            "--allowlist-item",
            rust_name,
        ]
    )
    if not ok:
        raise ValueError(f"Bindgen failed for `{type_name}` in '{path}'!\nError:\n{error}")
    binding = binding.strip()
    if not binding:
        raise ValueError(f"Bindgen failed to generate a binding for `{type_name}` in '{path}'!")

    # todo!() is intentional here (not unimplemented!()): validate_changes uses the
    # presence of unimplemented!() as a marker for allowed-change nodes. Since the
    # LLM must replace the () placeholder *signatures* as well as the bodies, we
    # use todo!() so that validate_changes returns no scope constraints, letting
    # build_feedback from cargo test enforce correctness instead.
    template = (
        f"{binding}\n\n"
        f"pub unsafe fn c_to_r(_cs: *const {rust_name}) -> () {{\n    todo!()\n}}\n\n"
        f"pub unsafe fn r_to_c(_rs: &(), _cs: *mut {rust_name}) {{\n    todo!()\n}}\n\n"
        f"#[cfg(test)]\nmod tests {{\n    use super::*;\n\n"
        f"    #[test]\n    fn round_trip_zeroed() {{\n        todo!()\n    }}\n\n"
        f"    #[test]\n    fn round_trip_nontrivial() {{\n        todo!()\n    }}\n}}\n"
    )

    ok, template, error, _ = run_subprocess(["rustfmt"], input=template)
    if not ok:
        raise ValueError(f"rustfmt failed for type wrapper template `{type_name}`!\n{error}")

    success, output = check_rust(template, flags=["--crate-type", "lib", "--emit", "metadata"])
    if not success:
        raise ValueError(
            f"Failed to validate type wrapper template for `{type_name}`!\nTemplate:\n{template}\nError:\n{output}"
        )
    return CodeRust(template)


def _default_feedback_fn(wrapper: CodeRust) -> str:
    return ""


def _default_on_attempt(msg: str, pred: dspy.Prediction) -> None:
    pass


class WrapperGenerator(dspy.Module):
    def __init__(
        self,
        wrapper: type[dspy.Module] = dspy.ChainOfThought,
        max_iters: int = 5,
        cache: Path | None = None,
    ) -> None:
        super().__init__()
        self._wrapper = wrapper
        self.max_iters = max_iters
        self.cache = _init_cache(cache)

    def forward(
        self,
        symbol: Symbol,
        crate_code: CodeRust,
        translation: CodeRust,
        unimplemented_wrapper: CodeRust,
        wrapper_path: Path,
        wrapped_crate: str,
        other_wrappers: CodeRust = CodeRust(),
        prior_wrapper: CodeRust | None = None,
        support_code: CodeC | None = None,
        feedback_fn: Callable[[CodeRust], str] | None = None,
        on_attempt: Callable[[str, dspy.Prediction], None] | None = None,
    ) -> dspy.Prediction:
        if feedback_fn is None:
            feedback_fn = _default_feedback_fn
        if on_attempt is None:
            on_attempt = _default_on_attempt

        # Dynamically select signature based on the symbol kind
        if symbol.is_type:
            sig_cls = TypeWrapperSignature
        elif symbol.is_function:
            sig_cls = FunctionWrapperSignature
        else:
            raise ValueError(
                f"WrapperGenerator only supports function and type symbols, "
                f"got `{symbol.kind}` for `{symbol.name}`"
            )

        logger.info(f"Generating wrapper for `{symbol.name}` ...")

        # Use cache when no prior wrapper
        if prior_wrapper is None:
            wrapper = _read_cache(self.cache, symbol.spelling, unimplemented_wrapper)
        else:
            logger.info("Ignoring wrapper cache...")
            wrapper = None

        # Generate dynamic signature and module for symbol
        signature = sig_cls.with_instructions(
            sig_cls.instructions.format(
                symbol_name=symbol.spelling,
                type_name=symbol.spelling,
                wrapped_crate=wrapped_crate,
                wrapper_path=wrapper_path,
            )
        )
        generate_wrapper = self._wrapper(signature)

        # Construct crate context for generate_wrapper
        crate = crate_code + translation + other_wrappers

        pred = dspy.Prediction(build_feedback="", scope_feedback="")
        for i in range(max(self.max_iters, 1)):
            # Use the wrapper from the prior iteration as feedback for the next iteration
            if i > 0:
                prior_wrapper = wrapper

            try:
                pred = self.generate(
                    generate_wrapper,
                    crate,
                    support_code,
                    unimplemented_wrapper,
                    prior_wrapper,
                    pred.build_feedback,
                    pred.scope_feedback,
                    wrapper if i == 0 else None,
                )
            except AdapterParseError:
                logger.exception(
                    f"DSPy exception while generating wrapper for `{symbol.name}` on iteration {i + 1}/{self.max_iters}!"
                )
                # If this is the last iteration, raise
                if i == max(self.max_iters, 1) - 1:
                    raise
                # Otherwise attempt again before any build logic
                continue

            # Scope validation
            if "wrapper" not in pred or not isinstance(pred.wrapper, CodeRust):
                wrapper = unimplemented_wrapper
                scope_feedback = "No wrapper was generated. You must respect the template and instructions **exactly**!"
            else:
                wrapper = pred.wrapper
                # Validate that changes are in scope
                scope_feedback = "\n\n".join(
                    validate_changes(wrapper, unimplemented_wrapper).values()
                )

            pred.name = symbol.spelling
            pred.bindgen_template = unimplemented_wrapper
            pred.prior_wrapper = prior_wrapper or CodeRust()
            pred.wrapper = wrapper
            pred.scope_feedback = scope_feedback
            pred.build_feedback = feedback_fn(wrapper)
            pred.success = not pred.scope_feedback and not pred.build_feedback

            if pred.success:
                msg = f"Wrapped `{symbol.name}`: {format_usage(pred)}"
                logger.info(msg)
                on_attempt(msg, pred)
                break
            else:
                msg = f"Failed to wrap `{symbol.name}` ({i + 1}/{self.max_iters}): {format_usage(pred)}"
                logger.error(msg)
                on_attempt(msg, pred)
        return pred

    def generate(
        self,
        generate_wrapper: dspy.Module,
        crate: CodeRust,
        support_code: CodeC | None,
        example_wrapper: CodeRust,
        prior_wrapper: CodeRust | None,
        build_feedback: str,
        scope_feedback: str,
        wrapper: CodeRust | None,
    ) -> dspy.Prediction:
        parent_usage_tracker = settings.usage_tracker
        if wrapper is not None:
            pred = dspy.Prediction(wrapper=wrapper)
            if parent_usage_tracker is not None:
                pred.set_lm_usage({})
        else:
            if parent_usage_tracker is None:
                pred = generate_wrapper(
                    crate=crate,
                    support_code=support_code or CodeC(),
                    example_wrapper=example_wrapper,
                    prior_wrapper=prior_wrapper or CodeRust(),
                    build_feedback=build_feedback,
                    scope_feedback=scope_feedback,
                )
            else:
                with track_usage() as local_usage_tracker:
                    pred = generate_wrapper(
                        crate=crate,
                        support_code=support_code or CodeC(),
                        example_wrapper=example_wrapper,
                        prior_wrapper=prior_wrapper or CodeRust(),
                        build_feedback=build_feedback,
                        scope_feedback=scope_feedback,
                    )
                lm_usage = local_usage_tracker.get_total_tokens()
                pred.set_lm_usage(lm_usage)
                for lm_name, usage_entry in lm_usage.items():
                    parent_usage_tracker.add_usage(lm_name, usage_entry)
        return pred

    def write_cache(self, pred: dspy.Prediction) -> None:
        # If prediction was not generated by an LM then don't write it to cache
        if not pred.get_lm_usage():
            return

        required_fields = (
            "name",
            "bindgen_template",
            "prior_wrapper",
            "build_feedback",
            "scope_feedback",
            "wrapper",
            "success",
        )
        if not all(hasattr(pred, field) for field in required_fields):
            return

        _write_cache(
            self.cache,
            pred.name,
            pred.bindgen_template,
            pred.prior_wrapper,
            pred.build_feedback,
            pred.scope_feedback,
            pred.wrapper,
            pred.success,
        )


def bindgen(path: Path, symbol_name: str) -> CodeRust:
    orig_src = path.read_bytes()
    try:
        # We want bindgen to run against an in-place extern'd declaration so it emits
        # a linkable item (`pub fn` / `pub static mut`) instead of value-style
        # constants for initialized globals, which we can't link against from Rust.
        clang_make_bindable_(path, symbol_name)

        # unsafe extern "C" {
        #     pub static mut foo: ::std::os::raw::c_int;
        # }
        ok, binding, error, _ = run_subprocess(
            [
                "bindgen",
                "--disable-header-comment",
                "--no-doc-comments",
                "--no-layout-tests",
                "--sort-semantically",
                str(path),
                "--allowlist-item",
                mangle(symbol_name),
            ]
        )
    finally:
        path.write_bytes(orig_src)
    if not ok:
        raise ValueError(f"Bindgen failed for `{symbol_name}` in '{path}'!\nError:\n{error}")

    binding = binding.strip()
    if binding == "":
        raise ValueError(f"Bindgen generated an empty binding for `{symbol_name}` in '{path}'!")

    success, output = check_rust(binding, flags=["--crate-type", "lib", "--emit", "metadata"])
    if not success:
        raise ValueError(
            f"Failed to validate binding for `{symbol_name}` in '{path}'!\nWrapper:\n{binding}\nError:\n{output}"
        )
    return CodeRust(binding)


def _init_cache(cache: Path | None) -> Path | None:
    if cache is None:
        return None
    with sqlite3.connect(cache) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wrapper_translations (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT    NOT NULL,
                bindgen_template TEXT    NOT NULL,
                prior_wrapper    TEXT    NOT NULL,
                build_feedback   TEXT    NOT NULL,
                scope_feedback   TEXT    NOT NULL,
                wrapper          TEXT    NOT NULL,
                success          INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wrapper_lookup"
            " ON wrapper_translations (name, bindgen_template)"
        )
    return cache


def _read_cache(cache: Path | None, name: str, bindgen_template: CodeRust) -> CodeRust | None:
    if cache is None:
        return None
    with sqlite3.connect(cache) as conn:
        try:
            row = conn.execute(
                "SELECT wrapper FROM wrapper_translations WHERE bindgen_template=? AND success=1 ORDER BY id DESC LIMIT 1",
                (str(bindgen_template),),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT wrapper FROM wrapper_translations WHERE name=? AND success=1 ORDER BY id DESC LIMIT 1",
                    (name,),
                ).fetchone()
        except Exception:
            row = None
    if row:
        logger.info(f"Cache hit for wrapper `{name}`")
        return CodeRust(row[0])
    else:
        logger.info(f"Cache miss for wrapper `{name}`")
        return None


def _write_cache(
    cache: Path | None,
    name: str,
    bindgen_template: CodeRust,
    prior_wrapper: CodeRust,
    build_feedback: str,
    scope_feedback: str,
    wrapper: CodeRust,
    success: bool,
) -> None:
    if cache is None:
        return
    with sqlite3.connect(cache) as conn:
        conn.execute(
            """
            INSERT INTO wrapper_translations
                (name, bindgen_template, prior_wrapper, build_feedback, scope_feedback, wrapper, success)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                str(bindgen_template),
                str(prior_wrapper),
                build_feedback,
                scope_feedback,
                str(wrapper),
                int(success),
            ),
        )
