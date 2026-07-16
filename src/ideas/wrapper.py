#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import re
import sqlite3
import logging
from pathlib import Path
from textwrap import indent
from collections import OrderedDict

import dspy
from dspy.utils.exceptions import AdapterParseError
from dspy.utils.usage_tracker import track_usage
from dspy.dsp.utils.settings import settings

from ideas.tools import Crate, check_rust, run_subprocess, LARGE_PROJECT
from ideas.ast_rust import CodeRust, validate_changes, mangle
from ideas.ast import CodeC, Symbol
from ideas.ast import clang_make_global_, clang_make_extern_, clang_make_bindable_
from ideas.model import format_usage

logger = logging.getLogger("ideas.wrapper")


class Signature(dspy.Signature):
    """
    Generate a C-compatible FFI wrapper for `crate::{symbol_name}`.

    # Goal

    Produce a `wrapper` that callers of the original C symbol can link against unchanged. The implementation of `crate::{symbol_name}` lives in the crate at "{crate_path}"; the wrapper will be written to "{wrapper_path}".

    # Template

    - Use `example_wrapper` as the template for `wrapper`. Preserve its function signature, attributes, and module structure exactly.
    - Replace only the `unimplemented!()` body with an implementation that calls `crate::{symbol_name}`.

    # Type conversions

    Types in `crate::wrapper::` (bindgen-generated, C-compatible layout) are *not* layout-compatible with those in `crate::` (idiomatic Rust). The wrapper must:

    1. Copy field values from each `crate::wrapper::` argument into a fresh `crate::` value before the call.
    2. Call `crate::{symbol_name}` with the converted values.
    3. Copy result/output values back from `crate::` types into the `crate::wrapper::` types the C ABI expects.

    Use `support_code` to recover the original C types behind opaque or erased Rust types (notably `void*`, `*mut c_void`, and untyped byte buffers) so each field is converted at its true C type.

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


class HybridSignature(Signature):
    """
    Generate a C-compatible FFI wrapper for `crate::{symbol_name}` in a hybrid C/Rust build where C globals and the Rust port must stay in sync.

    # Goal

    Produce a `wrapper` that callers of the original C symbol can link against unchanged. The implementation of `crate::{symbol_name}` lives in the crate at "{crate_path}"; the wrapper will be written to "{wrapper_path}".

    # Template

    - Use `example_wrapper` as the template for `wrapper`. Preserve its function signature, attributes, and module structure exactly.
    - Replace only the `unimplemented!()` body with an implementation that calls `crate::{symbol_name}`.

    # Type conversions

    Types in `crate::wrapper::` (bindgen-generated, C-compatible layout) are *not* layout-compatible with those in `crate::` (idiomatic Rust). The wrapper must:

    1. Copy field values from each `crate::wrapper::` argument into a fresh `crate::` value before the call.
    2. Call `crate::{symbol_name}` with the converted values.
    3. Copy result/output values back from `crate::` types into the `crate::wrapper::` types the C ABI expects.

    Use `support_code` to recover the original C types behind opaque or erased Rust types (notably `void*`, `*mut c_void`, and untyped byte buffers) so each field is converted at its true C type.

    # Global synchronization

    If `crate::{symbol_name}` reads or writes globals, synchronize them in the wrapper:

    - Before the call, copy each readable global from the bindgen-generated extern `crate::wrapper::{{var_name}}::{{var_name}}` into the Rust global `crate::{{var_name}}`.
    - After the call, copy each writable global from `crate::{{var_name}}` back to `crate::wrapper::{{var_name}}::{{var_name}}`.

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


def generate_unimplemented_wrapper(path: Path, symbol_name: str) -> CodeRust:
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
    if not success:
        raise ValueError(
            f"Failed to validate wrapper template for `{symbol_name}`!\nWrapper:\n{unimplemented_wrapper}\nError:\n{output}"
        )
    return CodeRust(unimplemented_wrapper)


class WrapperGenerator(dspy.Module):
    def __init__(
        self,
        crate: Crate,
        max_iters: int = 5,
    ) -> None:
        super().__init__()
        self.crate = crate
        self.max_iters = max_iters
        self.cache = _init_cache(crate.workspace_root / "cache.db")

        # Make sure wrapper module is in known state (i.e., empty)
        self.wrapper_path = crate.rust_src_path.parent / "wrapper.rs"
        self.wrapper_path.write_text("")

    def forward(
        self,
        symbol: Symbol,
        reference_code: CodeRust,
        translation: CodeRust,
        prior_wrapper: CodeRust | None = None,
        support_code: CodeC | None = None,
    ) -> dspy.Prediction:
        if symbol.is_function and symbol.is_definition:
            return self.wrap_function(
                symbol,
                reference_code,
                translation,
                prior_wrapper=prior_wrapper,
                support_code=support_code,
            )
        elif symbol.is_variable:
            self.wrap_variable_(symbol)
            return dspy.Prediction(success=True)
        else:
            raise NotImplementedError

    def wrap_variable_(self, symbol: Symbol):
        logger.info(f"Generating wrapper for variable `{symbol.name}` ...")

        # Variable wrappers are just bindings to C symbols
        rust_spelling = mangle(symbol.spelling)
        wrapper = bindgen(self.crate.c_src_path, symbol.spelling)
        symbol_wrapper_path = self.wrapper_path.parent / "wrapper" / f"{rust_spelling}.rs"
        symbol_wrapper_path.parent.mkdir(exist_ok=True, parents=True)
        symbol_wrapper_path.write_text(str(wrapper))
        self.crate.vcs.add(symbol_wrapper_path)

        success, output = self._build(symbol)
        if not success:
            raise RuntimeError(f"Failed to build crate!\n{output}")

        # Permanently make variable global
        clang_make_global_(self.crate.c_src_path, symbol.spelling)
        self.crate.vcs.add(self.crate.c_src_path)

        # Reference symbol wrapper in wrapper module.
        with self.wrapper_path.open("a") as f:
            f.write(f"pub mod {rust_spelling};\n")
        self.crate.vcs.add(self.wrapper_path)

        msg = f"Wrapped variable `{symbol.name}`"
        logger.info(msg)
        self.crate.vcs.commit(msg)

    def wrap_function(
        self,
        symbol: Symbol,
        reference_code: CodeRust,
        translation: CodeRust,
        prior_wrapper: CodeRust | None = None,
        support_code: CodeC | None = None,
    ) -> dspy.Prediction:
        # Don't bother wrapping main in binary crates
        if symbol.spelling == "main" and self.crate.is_bin:
            # Permanently make main function extern
            clang_make_extern_(self.crate.c_src_path, symbol.spelling)
            self.crate.vcs.add(self.crate.c_src_path)
            self.crate.vcs.commit(f"Made function `{symbol.name}` extern")
            return dspy.Prediction(success=True)

        logger.info(f"Generating wrapper for function `{symbol.name}` ...")

        # Use bindgen to generate unimplemented wrapper and write to disk to make sure we can actually build
        unimplemented_wrapper = generate_unimplemented_wrapper(
            self.crate.c_src_path, symbol.spelling
        )
        rust_spelling = mangle(symbol.spelling)
        symbol_wrapper_path = self.wrapper_path.parent / "wrapper" / f"{rust_spelling}.rs"
        symbol_wrapper_path.parent.mkdir(exist_ok=True, parents=True)
        symbol_wrapper_path.write_text(str(unimplemented_wrapper))
        success, build_feedback = self._build(symbol)
        if not success:
            raise RuntimeError(f"The crate does not build!\n\n{build_feedback}")

        # Use cache when no prior wrapper
        if prior_wrapper is None:
            wrapper = _read_cache(self.cache, symbol.spelling, unimplemented_wrapper)
        else:
            logger.info("Ignoring wrapper cache...")
            wrapper = None

        # Generate dynamic signature and module for symbol
        signature_class = HybridSignature if not LARGE_PROJECT else Signature
        signature = signature_class.with_instructions(
            signature_class.instructions.format(
                symbol_name=symbol.spelling,
                crate_path=self.crate.rust_src_path.relative_to(self.crate.cargo_toml.parent),
                wrapper_path=symbol_wrapper_path.relative_to(self.crate.cargo_toml.parent),
            )
        )
        generate_wrapper = dspy.ChainOfThought(signature)

        # Construct crate context for generate_wrapper and format it
        crate = (
            reference_code + translation + self.gather_wrappers(exclude_wrapper=rust_spelling)
        )

        # Try generating wrapper up to max_iter times
        msg = ""
        success, build_feedback, scope_feedback = False, "", ""
        pred = dspy.Prediction()
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
                    build_feedback,
                    scope_feedback,
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

            # Reset scope feedback
            if "wrapper" not in pred or not isinstance(pred.wrapper, CodeRust):
                wrapper = unimplemented_wrapper
                scope_feedback = "No wrapper was generated. You must respect the template and instructions **exactly**!"
            else:
                wrapper = pred.wrapper
                # Validate that changes are in scope
                scope_feedback = "\n\n".join(
                    validate_changes(wrapper, unimplemented_wrapper).values()
                )
                # TODO: Check for a single crate function call in scope

            # Write wrapper to disk and check if we build with unsafe code since wrappers can use unsafe code
            symbol_wrapper_path.write_text(str(wrapper))
            self.crate.vcs.add(symbol_wrapper_path)
            success, build_feedback = self._build(symbol)
            success = success and not build_feedback and not scope_feedback

            usage = format_usage(pred)

            # Exit early if we build
            if success:
                # Permanently make function extern
                clang_make_extern_(self.crate.c_src_path, symbol.spelling)
                self.crate.vcs.add(self.crate.c_src_path)

                # Reference successful symbol wrapper in wrapper module
                with self.wrapper_path.open("a") as f:
                    f.write(f"pub mod {rust_spelling};\n")
                self.crate.vcs.add(self.wrapper_path)

                # Log and commit success
                msg = f"Wrapped function `{symbol.name}`: {usage}"
                logger.info(msg)
                if "reasoning" in pred:
                    msg += f"\n\n# Reasoning\n{indent(pred.reasoning, '  ')}"
                self.crate.vcs.commit(msg)
                break

            # Log and commit failure
            msg = f"Failed to wrap function `{symbol.name}` ({i + 1}/{self.max_iters}): {usage}"
            logger.error(msg)
            if "reasoning" in pred:
                msg += f"\n\n# Reasoning\n{indent(pred.reasoning, '  ')}"
            msg += f"\n\n# Build Feedback\n{indent(build_feedback, '  ')}"
            msg += f"\n\n# Scope Feedback\n{indent(scope_feedback, '  ')}"
            self.crate.vcs.commit(msg)

        pred.success = success
        pred.name = symbol.spelling
        pred.wrapper = wrapper
        pred.bindgen_template = unimplemented_wrapper
        pred.prior_wrapper = prior_wrapper or CodeRust()
        pred.build_feedback = build_feedback
        pred.scope_feedback = scope_feedback
        if not success:
            # Feedback for translator
            pred.feedback = "It was difficult to generate a C-compatible FFI wrapper for the translation. Regenerate the translation with clear, explicit, wrapper-friendly Rust function boundaries and straightforward ownership, while keeping the translation fully memory-safe and free of unsafe constructs."
        return pred

    def generate(
        self,
        generate_wrapper: dspy.ChainOfThought,
        crate: CodeRust,
        support_code: CodeC | None,
        example_wrapper: CodeRust,
        prior_wrapper: CodeRust | None,
        build_feedback: str,
        scope_feedback: str,
        wrapper: CodeRust | None,
    ) -> dspy.Prediction:
        """Generate a wrapper prediction, using cached wrapper or calling the LLM."""
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

    def gather_wrappers(self, exclude_wrapper: str = "") -> CodeRust:
        wrapper_dir = self.wrapper_path.parent / "wrapper"
        if not wrapper_dir.is_dir():
            return CodeRust()

        modules: OrderedDict[str, str] = OrderedDict()
        for symbol_wrapper_path in sorted(wrapper_dir.glob("*.rs")):
            rust_spelling = symbol_wrapper_path.stem
            if exclude_wrapper and rust_spelling == exclude_wrapper:
                continue
            if rust_spelling in modules:
                continue

            wrapper_src = symbol_wrapper_path.read_text().strip()
            if not wrapper_src:
                continue

            modules[rust_spelling] = (
                f"pub mod {rust_spelling} {{\n" + indent(wrapper_src, "    ") + "\n}"
            )

        if not modules:
            return CodeRust()

        return CodeRust(
            "pub mod wrapper {\n" + indent("\n\n".join(modules.values()), "    ") + "\n}\n"
        )

    def _build(self, symbol: Symbol) -> tuple[bool, str]:
        orig_c_src = self.crate.c_src_path.read_bytes()
        orig_rust_src = self.crate.rust_src_path.read_bytes()
        orig_wrapper_src = self.wrapper_path.read_bytes()

        if symbol.is_function:
            # Make C function extern so that we use the Rust function definition
            clang_make_extern_(self.crate.c_src_path, symbol.spelling)
        elif symbol.is_variable:
            # Make C variable global so we can reference it in the Rust wrapper
            clang_make_global_(self.crate.c_src_path, symbol.spelling)
        else:
            raise NotImplementedError
        self.crate.vcs.add(self.crate.c_src_path)

        # Remove forbid unsafe from Rust source
        rust_src = orig_rust_src.decode().replace("#![forbid(unsafe_code)]", "")

        # Reference wrapper module in Rust source
        rust_src += "pub mod wrapper;\n"
        self.crate.rust_src_path.write_text(rust_src)
        self.crate.vcs.add(self.crate.rust_src_path)

        # Reference symbol wrapper module to wrapper module
        with self.wrapper_path.open("a") as f:
            f.write(f"pub mod {mangle(symbol.spelling)};\n")
        self.crate.vcs.add(self.wrapper_path)

        # Check whether all of the changes compile and commit them
        success, feedback = self.crate.cargo_build()

        # Restore original source
        self.crate.c_src_path.write_bytes(orig_c_src)
        self.crate.rust_src_path.write_bytes(orig_rust_src)
        self.wrapper_path.write_bytes(orig_wrapper_src)

        return success, feedback

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
