#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import re
import sqlite3
import logging
from pathlib import Path
from textwrap import dedent, indent
from dataclasses import dataclass, field
from collections.abc import Iterable

import dspy

from ideas.tools import check_rust, run_subprocess
from ideas.ast import CodeC, Symbol, clang_make_bindable_
from tree_sitter import Node

from ideas.ast_rust import BindgenName, CodeRust, get_node_text, get_nodes, get_root
from ideas.ast_rust import is_unimplemented, mangle, uses_catch_unwind, validate_changes
from ideas.refine import CodeAttempt, Feedback, PredictSession
from ideas.model import predict

logger = logging.getLogger("ideas.wrapper")

C_INTEROP_TRAIT = CodeRust("""\
pub trait CInterop: Sized {
    type Rust;
    unsafe fn to_rust(cs: *const Self) -> Self::Rust;
    unsafe fn sync_to_c(rs: &Self::Rust, cs: *mut Self);
}
""")


class FunctionWrapperSignature(dspy.Signature):
    r"""
    Generate a C-compatible FFI wrapper for `{wrapped_crate}::{symbol_name}` in a hybrid C/Rust build where C globals and the Rust port must stay in sync.

    # Goal

    Produce a `wrapper` that callers of the original C symbol can link against unchanged. The implementation of `{wrapped_crate}::{symbol_name}` lives in dependency crate `{wrapped_crate}`.

    # Template

    - Use `example_wrapper` as the template for `wrapper`. Preserve its function signature, attributes, and module structure exactly.
    - Replace only the `unimplemented!()` body with an implementation that calls `{wrapped_crate}::{symbol_name}`.

    # Type conversions

    The bindgen-generated C-layout types in `crate` are *not* layout-compatible with the idiomatic Rust types in `{wrapped_crate}`. Each C-layout type is defined exactly once at the crate root, so the same C type is the same Rust type in every wrapper. The wrapper must:

    1. Copy field values from each C-layout parameter into a fresh value of the corresponding idiomatic Rust type before the call.
    2. Call `{wrapped_crate}::{symbol_name}` with the converted values.
    3. Copy result/output values back from those idiomatic Rust types into the C-layout types the C ABI expects.

    Use `support_code` to recover the original C types behind opaque or erased Rust types (notably `void*`, `*mut c_void`, and untyped byte buffers) so each field is converted at its true C type.

    Below, `CType` is a **placeholder**, not a name to emit. Every C-layout type is declared in `crate`, so read the real name out of `crate` instead of deriving it from the C spelling, because `bindgen` does not always preserve that spelling.

    When converting a struct parameter, check whether its C type implements `CInterop`. If it does you must call `CType::to_rust(ptr)` (passing a `*const` pointer) rather than converting fields inline. Likewise use `CType::sync_to_c(&rust_value, c_out_ptr)` (passing a reference and a `*mut` pointer) when writing a result or out-parameter back to a C-layout type, because `sync_to_c` writes in-place and returns nothing.

    The type and the trait are both already in scope, so prefer the bare C type name exactly as `crate` declares it: `CType::to_rust(ptr)`. The qualified forms `crate::CType::to_rust(ptr)` and `<crate::CType as crate::CInterop>::to_rust(ptr)` resolve to the same method, but the bare form is the expected output. C globals are the exception: `crate` declares them inside the `__c_globals` module, which is not re-exported, so name them `__c_globals::NAME`.

    # Global synchronization

    If `{wrapped_crate}::{symbol_name}` reads or writes globals, synchronize them by calling the functions `crate` already declares for that purpose:

    - `sync_<name>_to_rust()` before the call, for every global the C code reads.
    - `sync_<name>_to_c()` after the call, for every global the C code writes.

    Call them rather than reimplementing what they do. They are the only synchronization path the round-trip tests cover, so a wrapper that converts the global itself is untested no matter how correct it looks.

    Each one acquires and releases whatever lock its Rust global needs, so never hold a guard on that global across a call to one, or the wrapper deadlocks.

    A global with no `sync_<name>_to_rust` / `sync_<name>_to_c` pair in `crate` needs no synchronization, and you must not synchronize it by hand.

    # Raw pointer handling

    - Null-check every raw pointer parameter before its first dereference. On null, return the same value the C function returns for null input (typically an error code, `false`, `-1`, or `null`); never dereference and panic. Use `support_code` to determine the correct null-input sentinel.
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
    - When C truncates a buffer by writing `'\0'` at a position found by a search (e.g., `strstr`, `strchr`, or a manual scan), the wrapper must re-derive that same offset from the raw buffer and write the NUL byte explicitly, even if the Rust function has internalized the truncation and does not expose the offset. Use `support_code` to recover the exact search function, offset, and capacity assumptions the C original relied on.
    - Treat in-place buffer mutations as **primary observable effects**. Callers (and tests) assert them directly; omitting them silently fails every assertion on the buffer regardless of the parsed return values.

    # Panic safety at the ABI boundary

    Never use `std::panic::catch_unwind`. An uncaught panic in an `extern "C"` function aborts the process after printing the panic message and source location, which is the correct outcome here: a panic means the Rust port diverges from the C original, and that divergence must reach the tests. Catching it and returning a plausible C value (`0`, `false`, `-1`, `null`, ...) turns a diagnosable abort into silently wrong behavior in the C caller.

    This is not a licence to panic. Reproduce the C behavior for every input the C function accepts, including malformed or out-of-range ones, so the panic never happens in the first place.

    # Calling libc / system functions

    The crate already depends on the `libc` crate. When the wrapper needs to call a C standard library or POSIX function (e.g. `fdopen`, `close`, `malloc`, `free`, `memcpy`, `strlen`, `open`, `read`, `write`, `fopen`, `fclose`, ...), call it through the `libc` crate (`unsafe {{ ::libc::fdopen(fd, mode) }}`).

    - **Do not** emit `extern "C" {{ ... }}` (or `unsafe extern "C" {{ ... }}`) blocks declaring libc / POSIX / system functions, and do not emit `#[link(name = "c")]` (or similar) link attributes for libc symbols.
    - The only `extern "C"` items permitted in generated wrapper code are those already present in `example_wrapper`; do not introduce any new `extern "C"` items.
    - If a needed symbol is not available in `libc`, prefer a safe Rust equivalent from `std` (e.g. `std::ptr`, `std::ffi::CStr`, `std::fs`, `std::io`). If neither is available, do not declare a new foreign function; explain the limitation in your reasoning.

    # Hard constraints

    - Do not relax behavior, skip write-backs, or use placeholder/stub logic.
    - Do not declare libc / POSIX functions in `extern "C"` blocks; call them via the `libc` crate.
    - Do not use `std::panic::catch_unwind`.

    # Inputs and feedback

    - `crate`: the hybrid crate this wrapper is written into. Everything shown lives at the crate root, alongside your wrapper.
    - `wrapped_crate_code`: the contents of the separate `{wrapped_crate}` crate. Nothing shown there is reachable through `crate::`, so always name those items as `{wrapped_crate}::Item`.
    - `support_code`: the original C source that was translated to Rust.
    - `prior_wrapper`: a previous attempt to fix, if any.
    - `feedback`: a critique of `prior_wrapper` from running the program it was part of. Address it.
    - `build_feedback`: errors from `cargo build`. Address them.
    - `scope_feedback`: deviations from the `example_wrapper` template. Address them.
    """

    # FIXME: Move crate and example_wrapper into instructions?
    crate: CodeRust = dspy.InputField()
    wrapped_crate_code: CodeRust = dspy.InputField()
    support_code: CodeC = dspy.InputField()
    example_wrapper: CodeRust = dspy.InputField()
    prior_wrapper: CodeRust = dspy.InputField()
    feedback: str = dspy.InputField()
    build_feedback: str = dspy.InputField()
    scope_feedback: str = dspy.InputField()

    wrapper: CodeRust = dspy.OutputField()


def generate_unimplemented_function_wrapper(
    path: Path, symbol_name: str, types: CodeRust
) -> CodeRust | None:
    # unsafe extern "C" {
    #     #[link_name = "\u{1}match"]
    #     pub fn match_(
    #         threshold: f64,
    #     ) -> ::std::os::raw::c_int;
    # }
    binding = bindgen_binding(path, symbol_name, types)

    # #[unsafe(export_name="match")]
    # pub extern "C" fn match_(
    #     threshold: f64,
    # ) -> ::std::os::raw::c_int {
    #     unimplemented!()
    # }
    template = re.sub(
        r'unsafe extern "C" {\s*.*\s+pub fn (.*);\s+}',
        rf'#[unsafe(export_name="{symbol_name}")]\npub extern "C" fn \1 {{\n    unimplemented!()\n}}',
        str(binding),
        flags=re.DOTALL,
    )
    if template == str(binding):
        raise ValueError(
            f"Failed to convert bindgen output to function for `{symbol_name}`!\nWrapper:\n{template}"
        )

    # Format unimplemented wrapper using rustfmt
    ok, template, error, _ = run_subprocess(["rustfmt"], input=template)
    if not ok:
        raise ValueError(f"rustfmt failed!\n{error}")
    wrapper = CodeRust(template)

    # Make sure the wrapper is valid Rust before sending it to the LLM for completion
    type_items, value_items = partition_values(types)
    success, output = check_rust(
        str(type_items + wrap_values(value_items) + wrapper),
        flags=["--crate-type=lib", "--emit=metadata"],
    )

    # Rust does not support C-compatible variadic functions on stable Rust (they require the nightly-only `c_variadic` feature):
    # https://github.com/rust-lang/rust/issues/44930
    if "error[E0658]: C-variadic functions are unstable" in output:
        return None

    if not success:
        raise ValueError(
            f"Failed to validate function wrapper for `{symbol_name}`!\nWrapper:\n{wrapper}\nError:\n{output}"
        )
    return wrapper


class TypeWrapperSignature(dspy.Signature):
    """
    Implement the `CInterop` trait for `{type_name}` in a hybrid C/Rust build.

    # Goal

    Produce a `wrapper` containing one `impl CInterop for {type_name}` block with two unsafe synchronization methods:
    - `to_rust`: reads from a C-layout allocation (via `*const Self`) and produces an idiomatic Rust value. Does **not** take ownership of C memory.
    - `sync_to_c`: writes an idiomatic Rust value back into an **existing** C-layout allocation (via `*mut Self`). Does **not** allocate a new C value, but writes in-place.

    This impl is the FFI glue between the C-layout types exposed by `bindgen` and the safe Rust types in `{wrapped_crate}`.

    Below, `NodeCType` and `FieldCType` are **placeholders**, not names to emit. Every C-layout type is declared in `crate`, so read the real name out of `crate` instead of deriving it from the C spelling, because `bindgen` does not always preserve that spelling.

    For pointer fields that form a linked structure (e.g. a linked list), `sync_to_c` must walk the Rust chain and the existing C chain in parallel:
    - If both sides have a node: write data in-place and recurse.
    - If Rust has a node but C does not (Rust function added a node): allocate a new C node via `::libc::malloc(std::mem::size_of::<NodeCType>())`, where `NodeCType` is the C-layout type of the pointed-to node. This uses the same allocator as C's `malloc`, so C can safely `free()` it later. Do **not** use `Box::into_raw` for this; `Box` uses Rust's allocator which C cannot `free()`.
    - If C has a node but Rust does not (Rust function removed a node): `::libc::free()` the C node and write null.

    # Template

    - Use `example_wrapper` as the starting template for `wrapper`.
    - The **only** signature slot you may edit is the associated type. Replace `type Rust = ();` with the `{wrapped_crate}` type that `{type_name}` was translated to. The translation renames C types to Rust conventions (a C `foo_bar_t` typically becomes `FooBarT`), so read the real name out of `{wrapped_crate}` rather than assuming it is still `{type_name}`. It lives in a different crate, so neither a bare name nor a `crate::`-prefixed path resolves. Keeping `()` is not a valid implementation.
    - The `to_rust` and `sync_to_c` signatures are fixed by the trait. Do **not** change their parameter types, return types, or `unsafe` markers: write `Self` and `Self::Rust` exactly as the template does.
    - Replace the `todo!()` bodies with field-by-field conversion implementations.
    - Do **not** add, remove, or rename items beyond what the template contains. In particular keep the `#[cfg(test)]` module's name exactly as the template spells it, because every wrapper shares the crate root namespace and the test filter keys on that name. You may add `use` items if needed.

    # Type mapping

    The bindgen-generated C-layout types in `crate` are structurally equivalent to their C originals but are **not** the same types as the idiomatic Rust ones in `{wrapped_crate}`. Use `support_code` (the original C source) and `wrapped_crate_code` (the existing Rust translations) to determine the correct field-by-field mapping.

    - For each field in `{type_name}`, locate the corresponding field in the `{wrapped_crate}` type and convert its value.
    - Primitive numeric fields (`c_int`, `c_uint`, `c_long`, etc.) map to their Rust integer equivalents (`i32`, `u32`, `i64`, etc.) with an `as` cast.
    - `*const c_char` / `*mut c_char` fields that represent strings usually map to a borrowed `&[u8]` in `{wrapped_crate}`, sometimes to `String`, and either may be wrapped in `Option`. Read the real type out of `{wrapped_crate}` and decode with `std::ffi::CStr` in `to_rust`. Writing one back in `sync_to_c` is only sound when the slice is still the one `to_rust` read, so compare it against the pointer already in the slot and reuse that pointer when they match. Otherwise copy the bytes into a fresh NUL-terminated `::libc::malloc` allocation. A `&[u8]` carries no terminator, so passing its `as_ptr()` to C makes C read past the end.
    - Raw pointer fields (`*mut T`, `*const T`) that are nullable map to `Option<...>` in `{wrapped_crate}`, so use `ptr::NonNull` or a null check, but only once the governing field described below shows the pointer was initialized at all.
    - Nested struct fields: if the field's C type also implements `CInterop`, call `FieldCType::to_rust(ptr)` / `FieldCType::sync_to_c(&rust_value, ptr)` rather than converting inline; otherwise convert field-by-field inline. Prefer the bare C type name exactly as `crate` declares it (`FieldCType::to_rust(ptr)`) over the equivalent qualified forms `crate::FieldCType::to_rust(ptr)` or `<crate::FieldCType as crate::CInterop>::to_rust(ptr)`.
    - Array fields: convert element-by-element.

    # Fields that govern other fields

    A field being present does not make its contents readable. Before reading an array, buffer, or pointer field, look in `support_code` for a sibling field that decides which of its elements are initialized: an occupancy or tombstone bitmap, a count or length smaller than the allocated capacity, a tag selecting which union member is live, a sentinel index, or a flag marking the field unused. Allocation with `malloc` / `realloc` / `reallocarray` and no following `memset`, as opposed to `calloc`, is direct evidence that the remaining elements hold indeterminate values.

    Consult that governing field **before** loading the element, not after. Reading an uninitialized slot as a pointer is undefined behavior even if you never dereference it, so testing the loaded value for null has already lost. Map elements the governing field marks dead to whatever `{wrapped_crate}` uses for absent, and never pass them to `CStr::from_ptr`, `NonNull`, or a dereference.

    # Round-trip correctness

    The two methods must satisfy: given a valid C-layout input `cs`, after calling `{type_name}::sync_to_c(&{type_name}::to_rust(&raw const cs), &raw mut cs)` the fields of `cs` must equal their original values. Pay special attention to:
    - Fields that are zero-initialized in the C layout but have `Default` values in Rust.
    - Pointer fields: `null` must round-trip to `null`. For non-null pointers, the **data** at the pointed address must be preserved; the pointer address itself is preserved naturally because `sync_to_c` writes in-place. Do **not** use global state (provenance tables, `OnceLock`, `Mutex`, etc.) to track pointer addresses; that is a design smell indicating the wrong approach.
    - String fields where the C layout stores a pointer (document if a true round-trip is impossible without re-allocation).

    Only storage the C code itself reads has to round-trip. Elements a governing field marks dead are not part of the value: do not read them in `to_rust`, and do not write them in `sync_to_c`. Leave that storage exactly as C left it, because overwriting it with null or zero is still a mutation C would never make.

    # Hard constraints

    - `unsafe` is permitted and expected when crossing the C/Rust boundary (e.g., dereferencing raw pointers, reading C-allocated memory). These methods ARE the FFI bridge, so do not contort the implementation to avoid it.
    - Do not add `extern "C"` items or FFI exports, because these are conversion methods only, not ABI entry points.
    - Do not use placeholder/stub logic or `todo!()` / `unimplemented!()` in the final output.
    - Do not use `std::panic::catch_unwind`, neither in the impl nor in the tests, because catching a panic only hides a broken conversion from the round-trip tests that exist to find it.

    # Round-trip tests

    After the impl block, fill in the `#[cfg(test)]` skeleton from `example_wrapper` with at least two tests:

    - `round_trip_zeroed`: construct the simplest valid C-layout input (all primitive fields 0, all pointers null if null is a valid input). Call `let rs = unsafe {{ {type_name}::to_rust(&raw const cs) }}; unsafe {{ {type_name}::sync_to_c(&rs, &raw mut cs) }};` and assert each field of `cs` equals its original value with field-by-field `assert_eq!`. If a zeroed instance would cause undefined behavior inside the conversion (e.g. a null pointer would be dereferenced), construct instead the simplest valid input and document why zeroed is unsafe.
    - `round_trip_nontrivial`: construct an instance with representative non-zero field values that exercise the real conversion path: non-null pointers (via `Box::leak`, a stack address cast, or a small allocation), non-zero integers, and non-trivial sizes. Convert and assert exactly as above. This test is especially important for pointer-bearing types where `round_trip_zeroed` does not exercise the pointer path.
    - If a governing field decides which elements of another field are live, `round_trip_nontrivial` must set it so that at least one element is live and at least one is dead, and must fill the dead element's storage with a value that is invalid to read, such as a dangling non-null pointer or a poison byte pattern, rather than zero or null. Assert that storage is unchanged after the round trip. A fixture in which every element is live proves nothing about how the conversion treats the governing field.
    - Tests may use `unsafe`. The `todo!()` in the skeleton is not a valid test body, so replace it with a real implementation.

    # Inputs and feedback

    - `crate`: the hybrid crate this wrapper is written into. Everything shown lives at the crate root, alongside your wrapper.
    - `wrapped_crate_code`: the contents of the separate `{wrapped_crate}` crate. Nothing shown there is reachable through `crate::`, so always name those items as `{wrapped_crate}::Item`.
    - `support_code`: the original C source that was translated to Rust.
    - `prior_wrapper`: a previous attempt, if any.
    - `feedback`: a critique of `prior_wrapper` from running the program it was part of. Address it.
    - `build_feedback`: errors from `cargo build`. Address them.
    - `scope_feedback`: deviations from the `example_wrapper` template. Address them.
    """

    crate: CodeRust = dspy.InputField()
    wrapped_crate_code: CodeRust = dspy.InputField()
    support_code: CodeC = dspy.InputField()
    example_wrapper: CodeRust = dspy.InputField()
    prior_wrapper: CodeRust = dspy.InputField()
    feedback: str = dspy.InputField()
    build_feedback: str = dspy.InputField()
    scope_feedback: str = dspy.InputField()

    wrapper: CodeRust = dspy.OutputField()


def generate_unimplemented_type_wrapper(
    type_name: str, types: CodeRust
) -> tuple[CodeRust, str]:
    # todo!() is intentional in the template (not unimplemented!()): validate_changes
    # keys on unimplemented!() to mark allowed-change nodes, and the LLM must also
    # replace the `type Rust` placeholder. todo!() leaves it unconstrained so
    # build_feedback from cargo test enforces correctness instead.
    rust_name = mangle(type_name)
    tests_mod = f"test_{rust_name}"
    template = dedent(f"""\
        impl CInterop for {rust_name} {{
            type Rust = ();

            unsafe fn to_rust(cs: *const Self) -> Self::Rust {{
                todo!()
            }}

            unsafe fn sync_to_c(rs: &Self::Rust, cs: *mut Self) {{
                todo!()
            }}
        }}

        #[cfg(test)]
        mod {tests_mod} {{
            use super::*;

            #[test]
            fn round_trip_zeroed() {{
                todo!()
            }}

            #[test]
            fn round_trip_nontrivial() {{
                todo!()
            }}
        }}
    """)
    ok, template, error, _ = run_subprocess(["rustfmt"], input=template)
    if not ok:
        raise ValueError(f"rustfmt failed for type wrapper template `{type_name}`!\n{error}")
    wrapper = CodeRust(template)

    # Make sure the template is valid Rust before sending it to the LLM for completion
    type_items, value_items = partition_values(types)
    success, output = check_rust(
        str(C_INTEROP_TRAIT + type_items + wrap_values(value_items) + wrapper),
        flags=["--crate-type=lib", "--emit=metadata"],
    )
    if not success:
        raise ValueError(
            f"Failed to validate type wrapper for `{type_name}`!\nWrapper:\n{wrapper}\nError:\n{output}"
        )
    return wrapper, tests_mod


class VariableWrapperSignature(dspy.Signature):
    """
    Keep the C global `{variable_name}` and its `{wrapped_crate}` counterpart in agreement in a hybrid C/Rust build.

    # Goal

    The same global exists twice: as C-layout storage in `crate` and as an idiomatic Rust global in `{wrapped_crate}`. C code writes the first, translated Rust code writes the second, and they must not drift apart. Fill in every `todo!()` in `example_wrapper`.

    # Reading the two sides

    - The C side is exposed in `crate` as a `static` inside an `unsafe extern "C"` block in the `__c_globals` module, bearing the variable's name. That module is not re-exported, so name it `__c_globals::{rust_variable_name}`. Read it through `unsafe {{ &raw const __c_globals::{rust_variable_name} }}` and, where `crate` declares it `static mut`, write it through `unsafe {{ &raw mut __c_globals::{rust_variable_name} }}`.
    - The Rust side lives in `{wrapped_crate}` and must be named as `{wrapped_crate}::ITEM`. The translation renames C identifiers to Rust conventions, so read the real name out of `wrapped_crate_code` rather than assuming it is still `{variable_name}`.
    - The Rust global may be wrapped for interior mutability (`Mutex`, `RwLock`, `OnceLock`, `LazyLock`, `RefCell`, ...). Acquire the guard or force the initializer as that type requires, then read or write through it. Never lock the same guard twice in one expression.

    # Synchronization functions

    Below, `CType` is a **placeholder**, not a name to emit. Every C-layout type is declared in `crate`, so read the real name out of `crate` instead of deriving it from the C spelling, because `bindgen` does not always preserve that spelling.

    - `sync_{rust_variable_name}_to_rust`: copy the current C value into the `{wrapped_crate}` global, so translated Rust code observes what C last wrote. If the C type implements `CInterop`, use `CType::to_rust(&raw const __c_globals::{rust_variable_name})`; otherwise convert field by field or cast the scalar.
    - `sync_{rust_variable_name}_to_c`: copy the `{wrapped_crate}` value back into the C storage, so C code observes what translated Rust last wrote. If the C type implements `CInterop`, use `CType::sync_to_c(&rust_value, &raw mut __c_globals::{rust_variable_name})`, which writes in-place and returns nothing; otherwise write field by field.

    These two must be exact inverses of each other. Anything one direction drops, the other cannot restore.

    Keep them unless the `{wrapped_crate}` global cannot be written, the one case where `sync_{rust_variable_name}_to_rust` could only be a stub. Writing it needs a `static mut`, or interior mutability that accepts repeated updates: a `Mutex`, `RwLock`, `Cell`, `RefCell`, `UnsafeCell` or an `Atomic*`. `OnceLock`, `OnceCell` and `LazyLock` accept a value only once, so they count only when the value they hold supplies the mutability, as `OnceLock<Mutex<T>>` does and `OnceLock<Vec<u8>>` does not. Mutability does not spread sideways either: a `Mutex` on one field of a struct says nothing about its siblings. Follow named types into their definitions in `wrapped_crate_code` rather than judging by the spelling of the `static` alone, and never write through an immutable `static` with `mprotect` or a `*mut` cast of `&raw const`; that is undefined behavior whatever the page permissions say.

    Where nothing can write it, delete them all, and `round_trip_nontrivial` with them, leaving `initial_value_matches` as the only test to implement. Delete all of them or none, since other wrappers call them together.

    # Comparing

    Both tests compare in the Rust-to-C direction: project the `{wrapped_crate}` value into C layout and assert it equals the C global.

    - If the C type implements `CInterop`, zero a C-layout temporary, call `CType::sync_to_c(&rust_value, &raw mut tmp)`, and assert the temporary equals the C global field by field.
    - Otherwise (scalars, arrays of scalars, and other types with no `CInterop` impl) compare directly, casting with `as` where the integer or float widths differ.
    - For pointer-valued globals compare the **pointed-to data**, not the address. The two crates hold separate allocations, so addresses can never match and asserting on them is always wrong. For a `*const c_char` / `*mut c_char` string, decode the C side with `std::ffi::CStr` and compare the contents.
    - Arrays compare element by element.

    # Tests

    Each test runs in its own process, so neither global has been touched when it starts and one test cannot disturb the other. Both tests must spell `{wrapped_crate}::ITEM` and `__c_globals::{rust_variable_name}` out in their own bodies rather than importing either global or reaching it through a helper, so that each test visibly reads both sides.

    - `initial_value_matches`: assert that the `{wrapped_crate}` global, projected into C layout, already equals the C global, **without calling any synchronization function first**. This measures whether the translation of `{variable_name}` and its initializer is faithful, so calling `sync_{rust_variable_name}_to_rust` first would copy C's value over the translated one and make the assertion compare C against itself. Such a test passes no matter how wrong the translation is and is worthless.
    - `round_trip_nontrivial`: write a representative non-zero value into the C global, then assert twice:
        1. Call `sync_{rust_variable_name}_to_rust()` and assert the `{wrapped_crate}` global now holds that value, projected into C layout as the section above describes.
        2. Call `sync_{rust_variable_name}_to_c()` and assert the C global still holds it.

      Both assertions are required. Without the first, two synchronization functions with empty bodies pass this test, because the value never left the C global and nothing checks that Rust ever saw it. Choose values that exercise the real conversion path: non-zero integers, non-empty strings, populated arrays, non-null pointers. A value that would survive a conversion that drops fields is a wasted test.

    # Hard constraints

    - Do **not** compare raw bytes with `memcmp`, `slice::from_raw_parts`, or a transmute to a byte array. Padding bytes are unspecified and differ between a zeroed temporary and the C global, so a bytewise comparison reports failures that are not real. Compare C-layout struct values field by field with `assert_eq!`; `bindgen` does not derive `PartialEq` for them, so asserting on two whole struct values does not compile.
    - The assertions **must** read the value from `{wrapped_crate}`. A test that only reads the C global (directly, or by copying it into a local and comparing it against itself) verifies nothing.
    - Do not weaken a comparison to make it pass. If the values genuinely differ, the translation or the synchronization is wrong and the test should fail.
    - Do not add or rename items beyond what the template contains, and keep the `#[cfg(test)]` module's name and the synchronization function names exactly as the template spells them, because every wrapper shares the crate root namespace, other wrappers call these functions by name, and the test filter keys on the module name. The only items you may remove are the synchronization functions together with `round_trip_nontrivial`, as described above. You may add `use` items, but not to shorten either global's path inside a test.
    - Do not use `std::panic::catch_unwind`, neither in the synchronization functions nor in the tests, because catching a panic only hides broken synchronization from the tests that exist to find it.
    - Tests may use `unsafe`. The `todo!()` placeholders are not valid bodies, so replace every one of them with a real implementation.

    # Inputs and feedback

    - `crate`: the hybrid crate this wrapper is written into. Everything shown lives at the crate root, alongside your wrapper.
    - `wrapped_crate_code`: the contents of the separate `{wrapped_crate}` crate. Nothing shown there is reachable through `crate::`, so always name those items as `{wrapped_crate}::Item`.
    - `support_code`: the original C source that was translated to Rust, including the definition and initializer of `{variable_name}`.
    - `prior_wrapper`: a previous attempt, if any.
    - `feedback`: a critique of `prior_wrapper` from running the program it was part of. Address it.
    - `build_feedback`: errors from `cargo build` and failures from `cargo test`. Address them.
    - `scope_feedback`: deviations from the `example_wrapper` template. Address them.
    """

    crate: CodeRust = dspy.InputField()
    wrapped_crate_code: CodeRust = dspy.InputField()
    support_code: CodeC = dspy.InputField()
    example_wrapper: CodeRust = dspy.InputField()
    prior_wrapper: CodeRust = dspy.InputField()
    feedback: str = dspy.InputField()
    build_feedback: str = dspy.InputField()
    scope_feedback: str = dspy.InputField()

    wrapper: CodeRust = dspy.OutputField()


def generate_unimplemented_variable_wrapper(
    variable_name: str, types: CodeRust, binding: CodeRust
) -> tuple[CodeRust, str, tuple[str, ...]]:
    rust_name = mangle(variable_name)
    tests_mod = f"test_var_{rust_name}"

    # Nothing can write a C global bindgen did not declare `mut`, so there is nothing to sync
    is_mutable = f"static mut {rust_name}" in str(binding)
    sync_fns = (f"sync_{rust_name}_to_rust", f"sync_{rust_name}_to_c") if is_mutable else ()

    # todo!() rather than unimplemented!() because validate_changes keys on unimplemented!()
    # to mark allowed-change nodes, and here every body is generated.
    sync_src = round_trip_src = ""
    if is_mutable:
        sync_src = f"""
        pub unsafe fn sync_{rust_name}_to_rust() {{
            todo!()
        }}

        pub unsafe fn sync_{rust_name}_to_c() {{
            todo!()
        }}
        """
        round_trip_src = """
            #[test]
            fn round_trip_nontrivial() {
                todo!()
            }
        """

    template = f"""
        {sync_src}

        #[cfg(test)]
        mod {tests_mod} {{
            use super::*;

            #[test]
            fn initial_value_matches() {{
                todo!()
            }}

            {round_trip_src}
        }}
    """.strip()
    ok, template, error, _ = run_subprocess(["rustfmt"], input=template)
    if not ok:
        raise ValueError(
            f"rustfmt failed for variable wrapper template `{variable_name}`!\n{error}"
        )
    wrapper = CodeRust(template)

    # Make sure the template is valid Rust before sending it to the LLM for completion
    type_items, value_items = partition_values(types)
    success, output = check_rust(
        str(C_INTEROP_TRAIT + type_items + wrap_values(value_items) + wrapper),
        flags=["--crate-type=lib", "--emit=metadata"],
    )
    if not success:
        raise ValueError(
            f"Failed to validate variable wrapper for `{variable_name}`!\nWrapper:\n{wrapper}\nError:\n{output}"
        )
    return wrapper, tests_mod, sync_fns


def scope_errors(wrapper: CodeRust, template: CodeRust) -> list[str]:
    # Deviations from `template` that still compile, so no build and no test would catch them
    errors = list(validate_changes(wrapper, template).values())
    # An untouched body is indistinguishable from the template, so `validate_changes` sees
    # nothing wrong with it; this is also what a prediction with no wrapper at all falls back to
    if is_unimplemented(wrapper):
        errors.append(
            "The wrapper still has an `unimplemented!()` body. You must respect the "
            "template and instructions **exactly** and replace that body with a real "
            "implementation."
        )
    if uses_catch_unwind(wrapper):
        errors.append(
            "The wrapper must not use `std::panic::catch_unwind`. An uncaught "
            'panic in an `extern "C"` function aborts the process after printing the '
            "panic message and source location, and that is the outcome we want: a "
            "panic means the Rust port diverges from the C original. Catching it and "
            "returning a plausible C value hides the divergence from the tests. "
            "Remove the `catch_unwind` and let the Rust call's value flow out "
            "directly."
        )
    return errors


@dataclass(kw_only=True)
class WrapperAttempt(CodeAttempt):
    wrapper: CodeRust
    symbol: Symbol
    bindgen_template: CodeRust

    @property
    def summary(self) -> str:
        verb = "Wrapped" if self.success else "Failed to wrap"
        return f"{verb} `{self.symbol.name}`"


@dataclass
class WrapperSession(PredictSession[WrapperAttempt, CodeRust]):
    generator: "WrapperGenerator"
    symbol: Symbol
    wrapped_crate_code: CodeRust
    translation: CodeRust
    unimplemented_wrapper: CodeRust
    wrapped_crate: str
    other_wrappers: CodeRust = CodeRust()
    prior_wrapper: CodeRust | None = None
    # Why an earlier session for this symbol was rejected, since `prior_wrapper` carries the
    # code across sessions but not the reason
    feedback: Feedback = Feedback()
    support_code: CodeC | None = None
    _predictor: dspy.Module = field(init=False, repr=False)

    def __post_init__(self):
        self._predictor = self.generator._wrapper(self._signature())

    def _signature(self) -> type[dspy.Signature]:
        # Dynamically select signature based on the symbol kind
        symbol = self.symbol
        if symbol.is_type:
            sig_cls = TypeWrapperSignature
        elif symbol.is_function:
            sig_cls = FunctionWrapperSignature
        elif symbol.is_variable:
            sig_cls = VariableWrapperSignature
        else:
            raise ValueError(
                f"WrapperGenerator only supports function, type and variable symbols, "
                f"got `{symbol.kind}` for `{symbol.name}`"
            )
        return sig_cls.with_instructions(
            sig_cls.instructions.format(
                symbol_name=symbol.spelling,
                type_name=symbol.bindgen_name or symbol.spelling,
                variable_name=symbol.spelling,
                rust_variable_name=mangle(symbol.spelling),
                wrapped_crate=self.wrapped_crate,
            )
        )

    @property
    def _max_iters(self) -> int:
        return max(self.generator.max_iters, 1)

    def _prime(self) -> CodeRust | None:
        # Use cache when no prior wrapper
        if self.prior_wrapper is not None:
            logger.info("Ignoring wrapper cache...")
            return None
        return _read_cache(
            self.generator.cache, self.symbol.spelling, self.unimplemented_wrapper
        )

    def _attempt(self, prior: WrapperAttempt | None, replay: CodeRust | None) -> WrapperAttempt:
        if prior is None:
            feedback, prior_wrapper = self.feedback, self.prior_wrapper
        else:
            feedback, prior_wrapper = prior.next_feedback, prior.wrapper
        pred = self.generator.generate(
            self._predictor,
            crate=self.other_wrappers,
            wrapped_crate_code=self.wrapped_crate_code + self.translation,
            support_code=self.support_code,
            example_wrapper=self.unimplemented_wrapper,
            prior_wrapper=prior_wrapper,
            feedback=feedback.review,
            build_feedback=feedback.build,
            scope_feedback=feedback.scope,
            wrapper=replay,
            symbol=self.symbol,
            wrapped_crate=self.wrapped_crate,
        )

        if "wrapper" not in pred or not isinstance(pred.wrapper, CodeRust):
            wrapper = self.unimplemented_wrapper
        else:
            wrapper = pred.wrapper

        return WrapperAttempt(
            wrapper=wrapper,
            pred=pred,
            prior_feedback=feedback,
            symbol=self.symbol,
            bindgen_template=self.unimplemented_wrapper,
        )


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

    def session(
        self,
        symbol: Symbol,
        wrapped_crate_code: CodeRust,
        translation: CodeRust,
        unimplemented_wrapper: CodeRust,
        wrapped_crate: str,
        other_wrappers: CodeRust = CodeRust(),
        prior_wrapper: CodeRust | None = None,
        feedback: Feedback = Feedback(),
        support_code: CodeC | None = None,
    ) -> WrapperSession:
        return WrapperSession(
            generator=self,
            symbol=symbol,
            wrapped_crate_code=wrapped_crate_code,
            translation=translation,
            unimplemented_wrapper=unimplemented_wrapper,
            wrapped_crate=wrapped_crate,
            other_wrappers=other_wrappers,
            prior_wrapper=prior_wrapper,
            feedback=feedback,
            support_code=support_code,
        )

    def generate(
        self,
        generate_wrapper: dspy.Module,
        crate: CodeRust,
        wrapped_crate_code: CodeRust,
        support_code: CodeC | None,
        example_wrapper: CodeRust,
        prior_wrapper: CodeRust | None,
        feedback: str,
        build_feedback: str,
        scope_feedback: str,
        wrapper: CodeRust | None,
        symbol: Symbol,
        wrapped_crate: str,
    ) -> dspy.Prediction:
        inputs = {
            "crate": crate,
            "wrapped_crate_code": wrapped_crate_code,
            "support_code": support_code or CodeC(),
            "example_wrapper": example_wrapper,
            "prior_wrapper": prior_wrapper or CodeRust(),
            "feedback": feedback,
            "build_feedback": build_feedback,
            "scope_feedback": scope_feedback,
        }
        return predict(generate_wrapper, inputs, "wrapper", wrapper)

    def write_cache(self, attempt: WrapperAttempt) -> None:
        # Re-caching a cache hit would just duplicate the entry it came from
        if attempt.success and attempt.pred.get("was_generated", False):
            _write_cache(
                self.cache, attempt.symbol.spelling, attempt.bindgen_template, attempt.wrapper
            )


def _run_bindgen(path: Path, what: str, *flags: str) -> CodeRust:
    ok, out, error, _ = run_subprocess(
        [
            "bindgen",
            "--disable-header-comment",
            "--no-doc-comments",
            "--no-layout-tests",
            "--sort-semantically",
            # Make glibc internals behind `FILE`, `pthread_*_t` and `locale_t` opaque
            # since they're not usable beyond the FFI boundary.
            *("--opaque-type", "_IO_.*"),
            *("--opaque-type", "__pthread_.*"),
            *("--opaque-type", "__locale_struct"),
            *flags,
            str(path),
        ]
    )
    if not ok:
        raise ValueError(f"Bindgen failed for {what} in '{path}'!\nError:\n{error}")
    return CodeRust(out.strip())


def bindgen_types(path: Path, symbol_names: Iterable[str]) -> CodeRust:
    types = _run_bindgen(
        path,
        "the crate's types",
        # Every symbol in one run, so bindgen assigns anonymous-type names once for the
        # whole crate instead of restarting the counter per symbol
        *(f for n in symbol_names for f in ("--allowlist-item", mangle(n))),
        # The allowlist above reaches types through the symbols referencing them, so
        # dropping every var and function leaves exactly the types behind.
        *("--blocklist-var", ".*"),
        *("--blocklist-function", ".*"),
    )
    type_items, value_items = partition_values(types)
    success, output = check_rust(
        str(type_items + wrap_values(value_items)),
        flags=["--crate-type=lib", "--emit=metadata"],
    )
    if not success:
        raise ValueError(
            f"Failed to validate types in '{path}'!\nTypes:\n{types}\nError:\n{output}"
        )
    return types


VALUES_MODULE = "__c_globals"


def partition_values(code: CodeRust) -> tuple[CodeRust, CodeRust]:
    # Separate the value namespace (bindgen's consts, the C globals' externs) from the types,
    # so only the latter reach the crate root
    types: list[str] = []
    values: list[str] = []
    pending: list[str] = []
    for node in get_nodes(get_root(str(code))):
        # Attributes are siblings of the item they decorate, so they wait for it
        if node.type == "attribute_item":
            pending.append(get_node_text(node))
            continue
        bucket = values if node.type in ("const_item", "static_item") else types
        bucket.extend([*pending, get_node_text(node)])
        pending.clear()
    types.extend(pending)

    parts = (CodeRust("\n".join(types)), CodeRust("\n".join(values)))
    kept = sorted(line for part in parts for line in _significant_lines(part))
    if kept != sorted(_significant_lines(code)):
        raise ValueError(f"Partitioning dropped or duplicated items!\nCode:\n{code}")
    return parts


def wrap_values(*parts: CodeRust, module: str = VALUES_MODULE) -> CodeRust:
    # A crate-root `static` or `const` is not shadowable: every same-named binding elsewhere in
    # the crate becomes E0530/E0005, be it a wrapper parameter, one of bindgen's own bitfield
    # accessor parameters, or a `let` the model writes. A module with no re-export keeps the
    # crate-root value namespace empty, so nothing can collide. `super` rather than `crate`
    # because lib.rs is also compiled as `mod hybrid` inside main.rs, where `crate` is the bin.
    body = CodeRust.join([part for part in parts if str(part).strip()])
    if not str(body).strip():
        return CodeRust()
    return CodeRust(
        f"pub mod {module} {{\n"
        "    #[allow(unused_imports)]\n"
        "    use super::*;\n\n"
        f"{indent(str(body).rstrip(), '    ')}\n}}"
    )


# Bucket for items that belong to no single type, always kept in every slice. Not spellable
# as a Rust identifier, so no name read out of bindgen's output can collide with it.
TYPES_PREAMBLE = BindgenName("<types-preamble>")


def _item_key(node: Node) -> BindgenName | None:
    # A const or impl belongs to the type it names, e.g. `pub const tag_t_A: tag_t = 0;`
    # joins `tag_t` and `impl<S> __BindgenBitfieldUnit<S>` joins `__BindgenBitfieldUnit`
    if node.type not in ("const_item", "impl_item"):
        name = get_node_text(node.child_by_field_name("name"))
    else:
        target = node.child_by_field_name("type")
        while target is not None and target.type == "generic_type":
            target = target.child_by_field_name("type")
        name = get_node_text(target)
    return BindgenName(name) if name else None


def split_types(types: CodeRust) -> dict[BindgenName, CodeRust]:
    # Index bindgen's type output by item name so prompts can show a subset of it
    entries = []
    pending: list[str] = []
    for node in get_nodes(get_root(str(types))):
        # Attributes are siblings of the item they decorate, so they wait for it
        if node.type == "attribute_item":
            pending.append(get_node_text(node))
            continue
        entries.append((node, [*pending, get_node_text(node)]))
        pending.clear()

    declared = {
        get_node_text(n.child_by_field_name("name"))
        for n, _ in entries
        if n.type in ("struct_item", "union_item", "enum_item", "type_item")
    }
    items: dict[BindgenName, list[str]] = {TYPES_PREAMBLE: pending}
    for node, text in entries:
        name = _item_key(node)
        key = name if name is not None and name in declared else TYPES_PREAMBLE
        items.setdefault(key, []).extend(text)

    split = {name: CodeRust("\n".join(lines)) for name, lines in items.items() if lines}
    # Buckets reorder items but must never drop or duplicate one
    kept = sorted(line for code in split.values() for line in _significant_lines(code))
    if kept != sorted(_significant_lines(types)):
        raise ValueError(f"Splitting dropped or duplicated type items!\nTypes:\n{types}")
    return split


def _significant_lines(code: CodeRust) -> list[str]:
    # `CodeRust.join` inserts blank lines between parts, so compare only the content
    return [line for line in str(code).splitlines() if line.strip()]


def split_items(
    code: CodeRust,
) -> tuple[dict[BindgenName, CodeRust], dict[BindgenName, CodeRust]]:
    # Split before partitioning so the two maps share a key: `_item_key` already files a
    # const under the type it names, e.g. `pub const tag_t_A: tag_t` under `tag_t`
    parts = {name: partition_values(part) for name, part in split_types(code).items()}
    return (
        {n: t for n, (t, _) in parts.items() if str(t).strip()},
        {n: v for n, (_, v) in parts.items() if str(v).strip()},
    )


def bindgen_binding(path: Path, symbol_name: str, types: CodeRust) -> CodeRust:
    return bindgen_bindings(path, [symbol_name], types)[symbol_name]


def bindgen_bindings(
    path: Path, symbol_names: Iterable[str], types: CodeRust
) -> dict[str, CodeRust]:
    names = list(dict.fromkeys(symbol_names))
    if not names:
        return {}

    orig_src = path.read_bytes()
    try:
        # An initialized global otherwise comes back as `pub const foo: c_int = 7`, a value
        # with no linkage. Extern'ing the declaration in place yields a linkable
        # `pub static mut` instead; functions already emit `pub fn` either way.
        clang_make_bindable_(path, names)

        # unsafe extern "C" {
        #     pub static mut foo: ::std::os::raw::c_int;
        # }
        bindings = _run_bindgen(
            path,
            ", ".join(f"`{name}`" for name in names),
            # A binding is only ever a variable or a function, never a type
            *(f for name in names for f in ("--allowlist-var", mangle(name))),
            *(f for name in names for f in ("--allowlist-function", mangle(name))),
            # Every type the binding references is already defined in `types`
            "--no-recursive-allowlist",
        )
    finally:
        path.write_bytes(orig_src)

    split = _split_bindings(bindings, names)
    if missing := [name for name in names if name not in split]:
        raise ValueError(
            f"Bindgen generated no binding for {', '.join(f'`{n}`' for n in missing)} "
            f"in '{path}'!"
        )

    # Ensure the bindings compile with the given `types`
    type_items, value_items = partition_values(types)
    success, output = check_rust(
        str(type_items + wrap_values(value_items, bindings)),
        flags=["--crate-type=lib", "--emit=metadata"],
    )
    if not success:
        raise ValueError(
            f"Failed to validate bindings for {', '.join(f'`{n}`' for n in names)} in "
            f"'{path}'!\nBindings:\n{bindings}\nError:\n{output}"
        )
    return split


def _split_bindings(bindings: CodeRust, symbol_names: Iterable[str]) -> dict[str, CodeRust]:
    # bindgen names items after mangling, so map back to the C spelling the caller asked for
    by_rust_name = {mangle(name): name for name in symbol_names}
    source = str(bindings).encode()

    items: dict[str, list[str]] = {}
    pending: list[str] = []
    for node in get_nodes(get_root(source)):
        # Attributes are siblings of the item they decorate, so they wait for it
        if node.type == "attribute_item":
            pending.append(get_node_text(node))
            continue
        for rust_name, text in _binding_items(node, source):
            if (name := by_rust_name.get(rust_name)) is None:
                raise ValueError(f"Bindgen emitted unrequested item `{rust_name}`!\n{bindings}")
            items.setdefault(name, []).extend([*pending, text])
        pending.clear()

    return {name: CodeRust("\n".join(lines)) for name, lines in items.items() if lines}


def _binding_items(node: Node, source: bytes) -> Iterable[tuple[str, str]]:
    body = node.child_by_field_name("body") if node.type == "foreign_mod_item" else None
    if body is None:
        yield get_node_text(node.child_by_field_name("name")), get_node_text(node)
        return

    inner = [n for n in get_nodes(body) if n.type not in ("{", "}")]
    if len(inner) == 1:
        # The common case: bindgen already gave this item a block of its own
        yield get_node_text(inner[0].child_by_field_name("name")), get_node_text(node)
        return

    # Re-wrap each item so one shared block never lands whole in two symbols' bindings
    header = source[node.start_byte : body.children[0].end_byte].decode()
    for item in inner:
        name = get_node_text(item.child_by_field_name("name"))
        # Only the item's first line lost its indentation; the rest keep the source's
        pad = " " * item.start_point.column
        yield name, f"{header}\n{pad}{get_node_text(item)}\n}}"


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
    wrapper: CodeRust,
) -> None:
    if cache is None:
        return
    with sqlite3.connect(cache) as conn:
        conn.execute(
            "INSERT INTO wrapper_translations"
            " (name, bindgen_template, prior_wrapper, build_feedback, scope_feedback,"
            "  wrapper, success)"
            " VALUES (?, ?, '', '', '', ?, 1)",
            (name, str(bindgen_template), str(wrapper)),
        )
