#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import re
import sqlite3
import logging
from pathlib import Path
from textwrap import dedent
from dataclasses import dataclass

from ideas.tools import check_rust, run_subprocess, rustc_version
from ideas.ast import Symbol

from ideas.ast_rust import BindgenName, CodeRust, get_node_text, get_nodes, get_root
from ideas.ast_rust import is_unimplemented, mangle, top_level_value_names, uses_catch_unwind
from ideas.ast_rust import validate_changes
from ideas.bindgen import C_INTEROP_TRAIT, bindgen_binding, partition_values, wrap_values
from ideas.codex import CodexConfig, build_prompt, run_codex
from ideas.refine import GeneratedAttempt, Feedback, PredictSession
from ideas.model import Generation, ModelConfig

logger = logging.getLogger("ideas.wrapper")


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

    # /// # Safety
    # ///
    # /// The caller must uphold the contract of the C function this replaces.
    # #[unsafe(export_name="match")]
    # pub unsafe extern "C" fn match_(
    #     threshold: f64,
    # ) -> ::std::os::raw::c_int {
    #     unimplemented!()
    # }
    # `unsafe` because C hands it pointers it cannot check, and Rust only lets an `unsafe`
    # function define C variadics. The C name and parameters may not be snake case.
    template = re.sub(
        r'unsafe extern "C" {\s*.*\s+pub fn (.*);\s+}',
        lambda declaration: (
            "/// # Safety\n///\n"
            "/// The caller must uphold the contract of the C function this replaces.\n"
            f'#[unsafe(export_name="{symbol_name}")]\n'
            f'pub unsafe extern "C" fn {declaration.group(1)} {{\n'
            "    unimplemented!()\n}"
        ),
        str(binding),
        flags=re.DOTALL,
    )
    if template == str(binding):
        raise ValueError(
            f"Failed to convert bindgen output to function for `{symbol_name}`!\nWrapper:\n{template}"
        )
    if (adopted := _adopt_c_variadics(template)) is None:
        return None
    template = adopted

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
    if not success:
        raise ValueError(
            f"Failed to validate function wrapper for `{symbol_name}`!\nWrapper:\n{wrapper}\nError:\n{output}"
        )
    return wrapper


def _adopt_c_variadics(template: str) -> str | None:
    source = template.encode()
    fn = get_nodes(get_root(source), "function_item")[0]
    params = fn.child_by_field_name("parameters")
    assert params is not None  # bindgen always emits a parameter list

    taken = {get_node_text(p.child_by_field_name("pattern")) for p in params.named_children}
    args = "args"
    while args in taken:
        args = f"_{args}"

    edits: list[tuple[int, int, str]] = []
    for param in params.named_children:
        if param.type == "variadic_parameter":
            edits.append((param.start_byte, param.end_byte, f"mut {args}: ..."))
        elif param.type == "parameter":
            # bindgen's spelling of a `va_list` parameter once it decays to a pointer on x86_64
            ty = param.child_by_field_name("type")
            if ty is not None and get_node_text(ty) == "*mut __va_list_tag":
                edits.append((ty.start_byte, ty.end_byte, "::std::ffi::VaList<'_>"))
                # `next_arg` borrows the list mutably
                pattern = param.child_by_field_name("pattern")
                assert pattern is not None  # bindgen names every parameter
                edits.append((pattern.start_byte, pattern.start_byte, "mut "))

    # Rust cannot define a C-variadic function before 1.99, so there is no template to give
    if edits and rustc_version() < (1, 99, 0):
        return None

    for start, end, text in sorted(edits, reverse=True):
        source = source[:start] + text.encode() + source[end:]
    return source.decode()


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

            #[test]
            fn round_trip_mutated() {{
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
        /// # Safety
        ///
        /// Nothing else may access `{variable_name}` during the call.
        pub unsafe fn sync_{rust_name}_to_rust() {{
            todo!()
        }}

        /// # Safety
        ///
        /// Nothing else may access `{variable_name}` during the call.
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
    # nothing wrong with it; this is also what a turn with no wrapper at all falls back to
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


def variable_wrapper_errors(
    wrapper: CodeRust,
    *,
    spelling: str,
    tests_mod: str,
    sync_fns: tuple[str, ...],
    rust_lib_name: str,
) -> list[str]:
    # Report every static violation at once so one round of feedback fixes them all
    src = str(wrapper)
    errors: list[str] = []
    c_global = f"__c_globals::{mangle(spelling)}"

    # A renamed test module makes the filter below match nothing, which reads as a pass
    if f"mod {tests_mod}" not in src:
        errors.append(f"The test module must stay named `{tests_mod}`.")

    # `initial_value_matches` only means something if it reads the translated global and
    # reads it unaided, so check its body for both
    if "fn initial_value_matches" not in src:
        errors.append("The test function `initial_value_matches` must be implemented.")
    else:
        body = _test_body(src, "initial_value_matches")
        if rust_lib_name not in body:
            errors.append(
                "`initial_value_matches` must read the translated global as "
                f"`{rust_lib_name}::ITEM` and compare it against the C global "
                f"`{c_global}`. Comparing the C global against "
                "itself passes no matter how wrong the translation is. Spell that path "
                "out in the test body rather than importing it or reading it through a "
                "helper."
            )
        if called := [fn for fn in sync_fns if fn in body]:
            errors.append(
                "`initial_value_matches` must not call "
                + ", ".join(f"`{fn}`" for fn in called)
                + ". That test is meant to measure whether the translated initializer "
                "already agrees with C, and any synchronization function overwrites "
                "one side with the other first, so the assertion would compare a value "
                "against itself and pass for any translation."
            )

    # The sync functions are legal all or none, never partly gone, and the test goes
    # with them
    present_sync_fns = tuple(fn for fn in sync_fns if f"fn {fn}" in src)
    has_round_trip = "fn round_trip_nontrivial" in src
    names = ", ".join(f"`{fn}`" for fn in sync_fns)
    if not sync_fns and has_round_trip:
        errors.append(
            "This global has no synchronization functions, so "
            "`round_trip_nontrivial` has nothing to test. Remove it and implement "
            "`initial_value_matches` only."
        )
    elif 0 < len(present_sync_fns) < len(sync_fns):
        errors.append(
            f"Keep all of {names} or none of them, spelled exactly as the template spells them."
        )
    elif present_sync_fns and not has_round_trip:
        errors.append(
            f"You kept {names}, so `round_trip_nontrivial` must be implemented: it is "
            "the only test that exercises them. Dropping it means dropping them with "
            "it, which is allowed only when the translated global cannot be written."
        )
    elif not present_sync_fns and has_round_trip:
        errors.append(
            f"`round_trip_nontrivial` exists to test {names}, so without them it "
            "asserts nothing. Restore them and keep the test, unless the translated "
            "global cannot be written, in which case remove the test too."
        )
    elif present_sync_fns and has_round_trip:
        body = _test_body(src, "round_trip_nontrivial")
        if uncalled := [fn for fn in sync_fns if f"{fn}(" not in body]:
            errors.append(
                "`round_trip_nontrivial` must call "
                + ", ".join(f"`{fn}`" for fn in uncalled)
                + ", since it is the only test that exercises them."
            )
        if c_global not in body:
            errors.append(
                "`round_trip_nontrivial` must write the C global "
                f"`{c_global}` and assert on it afterwards. Spell "
                "that path out in the test body rather than importing it or reaching "
                "it through a helper."
            )
        if rust_lib_name not in body:
            errors.append(
                "`round_trip_nontrivial` must read the translated global as "
                f"`{rust_lib_name}::ITEM` after `{sync_fns[0]}`. Without that "
                "assertion the value never leaves the C global, and two "
                "empty-bodied synchronization functions pass the test. Spell that "
                "path out in the test body rather than importing it or reading it "
                "through a helper."
            )

    if "todo!()" in src:
        errors.append("The `todo!()` placeholder must be replaced with a real implementation.")

    return errors


def type_wrapper_errors(
    wrapper: CodeRust,
    *,
    bindgen_name: BindgenName,
    spelling: str,
    tests_mod: str,
    rust_lib_name: str,
    c_item_names: frozenset[str] = frozenset(),
) -> list[str]:
    # Report every static violation at once so one round of feedback fixes them all
    src = str(wrapper)
    errors: list[str] = []

    # Enforce that every required round-trip test function is present
    required = ("round_trip_zeroed", "round_trip_nontrivial", "round_trip_mutated")
    missing = [fn for fn in required if f"fn {fn}" not in src]
    if missing:
        names = ", ".join(f"`{fn}`" for fn in missing)
        errors.append(
            f"Required test functions are missing: {names}. "
            + ", ".join(f"`{fn}`" for fn in required)
            + " must all be implemented."
        )

    # A dropped impl header or a surviving `()` placeholder still compiles, so
    # neither cargo build nor cargo test would catch them
    impl_header = f"impl CInterop for {bindgen_name}"
    if impl_header not in src:
        errors.append(f"The wrapper must contain an `{impl_header}` block.")
    if "type Rust = ()" in src:
        errors.append(
            "`type Rust = ()` is still the placeholder. Replace it with "
            f"`{rust_lib_name}::<TranslatedType>`, or a tuple of it and "
            "C-only data it cannot hold, such as function pointers and the C address "
            "of each object its handles refer to, so `sync_to_c` writes back the "
            "address of whatever the Rust value refers to after the call, never the "
            "address `to_rust` read. `<TranslatedType>` is the actual name the C type "
            f"`{spelling}` was translated to in the Rust crate (it may have been "
            "renamed)."
        )

    # A renamed test module makes the filter below match nothing, which reads as a pass
    if f"mod {tests_mod}" not in src:
        errors.append(f"The test module must stay named `{tests_mod}`.")

    # A later function or global wrapper defines these names at the crate root, and that
    # clash only surfaces once its template is inserted
    if clashes := sorted(top_level_value_names(wrapper) & c_item_names):
        names = ", ".join(f"`{name}`" for name in clashes)
        errors.append(
            f"{names} would clash with a C function or global this crate will define "
            "at its root. Rename every helper you add at crate level."
        )

    return errors


def _test_body(wrapper: str, name: str) -> str:
    # Crude, but avoids a full parse
    start = wrapper.find(f"fn {name}")
    if start == -1:
        return ""
    body = wrapper[start:]
    end = body.find("#[test]")
    return body if end == -1 else body[:end]


@dataclass(kw_only=True)
class WrapperAttempt(GeneratedAttempt):
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
    unimplemented_wrapper: CodeRust
    wrapped_crate: str
    hybrid_src: Path
    prior_wrapper: CodeRust | None = None
    # Why an earlier session for this symbol was rejected, since `prior_wrapper` carries the
    # code across sessions but not the reason
    feedback: Feedback = Feedback()

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
        gen = self.generator.generate(
            example_wrapper=self.unimplemented_wrapper,
            prior_wrapper=prior_wrapper,
            feedback=feedback.review,
            build_feedback=feedback.build,
            lint_feedback=feedback.lint,
            scope_feedback=feedback.scope,
            wrapper=replay,
            symbol=self.symbol,
            wrapped_crate=self.wrapped_crate,
            hybrid_src=self.hybrid_src,
        )

        # A turn that came back without a wrapper leaves the caller the template to build
        wrapper = gen.code or self.unimplemented_wrapper

        return WrapperAttempt(
            wrapper=wrapper,
            gen=gen,
            prior_feedback=feedback,
            symbol=self.symbol,
            bindgen_template=self.unimplemented_wrapper,
        )


FUNCTION_WRAPPER_INSTRUCTIONS = """\
# Task

Implement only the C ABI adapter for `{wrapped_crate}::{symbol_name}` by completing
`example_wrapper`. Preserve its exported name, signature, types, and module structure
exactly, and keep every existing attribute unchanged. `{wrapped_crate}` is safe Rust and
holds all of the C semantics; this hybrid crate holds all of the `unsafe` and only converts
between C values and safe values. Use feedback about prior attempts to fix the conversion
rather than the C algorithm.

# Conversion

- **Pointers.** Within a call, map each C pointer to the one Rust value it stands for and
  back, so aliases, NULL, and pointers C already holds come out unchanged. A constant
  address C uses as a marker, such as `(int *)1`, is not an object: map it by address
  alone and never read through it. A C pointer into a block that the Rust value holds as
  an index or offset converts against the base that index or offset counts from, in both
  directions.
- **What to convert.** Do not convert an object the C function only writes, such as an
  out-parameter or a struct it initializes, since it may hold garbage. Convert every other
  object the call reaches in full, as its C invariants define it, never only the fields C
  happens to read: whatever you sync back overwrites the C object. Never read what those
  invariants leave undefined.
- **The safe call.** Call the safe function on every path and leave every decision to it:
  never repeat its checks, branches, or growth policy, and size and fill C storage from
  the state it returns. The safe call may consume what you pass it: sync back after it
  only through handles you still hold, and never into C objects it freed.
- **Writing results.** Write results into the C objects and buffers C already owns;
  storage C will free or resize comes from the C allocator, or the configured one, and is
  published only once it holds every live result. C storage may be larger than what it
  holds; copy only the live part.
- **Object identity.** A handle that stands for a C object already mapped in this call
  reuses that object; only an object the safe call created gets new storage, from the C
  allocator, and never write a Rust address (`Box::into_raw`, `Rc::as_ptr`,
  `Vec::as_ptr`) into a C field. Find those objects by following every link from the Rust
  values the call touched, starting from the values you passed in and their owner fields,
  even when nothing was mapped, with a visited set of your own, never stopping at objects
  already mapped. Key every map between C and Rust objects by C address and keep it for
  the whole call: re-reading C after a callback adds to it, and drops a mapping only when
  its object was freed. Find a Rust value's C address with `Rc::ptr_eq`, never by keying
  on `Rc::as_ptr`.
- **Allocator tokens.** A token the safe code got from a configured allocator is whatever
  that allocator returned: map it back to the C pointer it stands for, including tokens
  for blocks that existed before the call, since the safe code may free them, and never
  assume its Rust type; an object that occupies that block lives at that address.
- **Callbacks.** To give the safe code a C callback, pass a trampoline that maps its
  arguments back to the original C values, syncs the live Rust state into every C object
  the callback can reach before calling it, and afterward re-reads what C still holds,
  updating the existing Rust values in place, never replacing storage the safe code may
  still be using. A configured allocator callback reaches none of these objects: around
  it, only map the block it returns or frees. Where the safe call installs one of the
  crate's own functions as a callback and the C program defines a function of that name,
  store in the C object this hybrid's wrapper of it by its bare Rust identifier; never
  redeclare it in an `extern "C"` block. A crate function that stands in for a C library
  function, such as a default allocator, maps back to that library function.
- **Globals.** For each mutable C global the C function or its callbacks can read or
  write (when unsure, all of them), call its `sync_` functions in this crate:
  `sync_<name>_to_rust()` before the safe call and `sync_<name>_to_c()` after it, and
  inside each callback trampoline the reverse, `sync_<name>_to_c()` before calling C and
  `sync_<name>_to_rust()` after, so the callback's writes win over the earlier copy.
  Immutable globals have no `sync_` functions and need nothing.
- **Variadics.** Read each variable argument with `next_arg::<T>()` where C reads it,
  using C's promoted type (`double` for `float`, `c_int` for narrower integers), and
  never more than C reads; then pass them in whatever form the safe signature takes.
- **`RefCell` borrows.** Never hold a `RefCell` borrow across a call that may borrow it
  again (a callback, recursion, or another conversion); a borrow in an `if let`, `match`,
  or `for` head lives until the end of that block or loop, so never borrow the same cell
  mutably inside it.

# Template

For a function template, replace only the existing `unimplemented!()` body. Emit the
same single top-level function as `example_wrapper`: add no top-level `use`, helper,
static, thread-local, module, or second export. Do not add attributes except a specific,
local lint allowance for an unresolvable warning caused by unchanged template content, as
described above. Any helper function, trampoline, or scoped callback context must be
declared inside that body.

A warning caused by unchanged template content, such as the fixed signature or an existing
attribute, is not a reason to change that template content. If the warning cannot otherwise
be resolved without changing the template, add a specific local `#[allow(lint_name)]` on
the smallest applicable item. Do not suppress warnings caused by the generated body; fix
those in the body instead.

# Prior attempt

`prior_wrapper` is a rejected candidate to replace, not text to combine with the
template. `feedback` was produced with `prior_wrapper` in the crate in place of the
template, so the paths and line numbers it cites point into `prior_wrapper`: find the cited
code there, not at those lines of the file you see.

# Temporary C edit

For the temporary build of the wrapper, change the definition of `{symbol_name}` in
`{c_src}` into an extern declaration with the same prototype; change no other C
definition. This edit must not appear in the returned Rust.

# Before returning

Search the final body and verify the exact safe call is present, every mapped C object is
synchronized, all allocation ownership is balanced, and no branch implements or discards
part of the semantic operation.
"""


TYPE_WRAPPER_INSTRUCTIONS = """\
# Task

Implement only the `CInterop` conversion between C-layout `{type_name}` and its existing
safe representation in `{wrapped_crate}`. Complete the exact `example_wrapper`, including
its tests. Conversion maps representation; it never performs the data structure's
semantic algorithms.

# Template

Preserve every top-level item, signature, test-module name, and test-function name from
`example_wrapper`. Replace `type Rust = ()` with the translated type, or a tuple of it and
C-only data it cannot hold, such as function pointers and the C address of each object its
handles refer to, so `sync_to_c` writes back the address of whatever the Rust value refers
to after the call, never the address `to_rust` read. A unit type, no-op conversion, or
tests that merely prove the C object was never touched are not a conversion.

# Conversion

- **Object identity.** `sync_to_c` reaches every object from the Rust value, including
  ones the safe code created. A handle that stands for a C object already mapped reuses
  that object; only an object the safe code created gets new storage, from the C
  allocator, and never write a Rust address (`Box::into_raw`, `Rc::as_ptr`,
  `Vec::as_ptr`) into a C field. Key every map between C and Rust objects by C address,
  and keep one such map for everything a `to_rust` reaches, shared by nested conversions,
  so a pointer stored twice becomes one handle; find a Rust value's C address with
  `Rc::ptr_eq`, never by keying on `Rc::as_ptr`.
- **Marker addresses.** A constant address C uses as a marker, such as `(int *)1`, is not
  an object: map it by address alone and never read through it.
- **Interior pointers.** A C pointer into a block that the Rust value holds as an index or
  offset converts against the base that index or offset counts from, in both directions.
- **Helpers.** Give any helper you add at crate level a name that no C function or global
  has.
- **`RefCell` borrows.** Never hold a `RefCell` borrow across a call that may borrow it
  again (recursion or another conversion); a borrow in an `if let`, `match`, or `for` head
  lives until the end of that block or loop, so never borrow the same cell mutably inside
  it.

# Tests

- **Inputs.** Build each C value by hand. `round_trip_zeroed` starts from the simplest
  valid empty value; `round_trip_nontrivial` from one with nonzero lengths, links, aliases,
  and function pointers; `round_trip_mutated` from a value the safe crate builds and
  changes with its own functions, including objects it creates.
- **Assertions.** Check C fields, pointees, and addresses directly, never by comparing two
  outputs of the same conversion.
- **Re-entry.** In `round_trip_mutated`, convert the synced C value back with `to_rust`
  and pass it to a safe function that mutates it, so the safe crate accepts what `to_rust`
  returns.

# Prior attempt

`prior_wrapper` is a rejected candidate to replace, not text to combine with the template.
`feedback` was produced with `prior_wrapper` in the crate in place of the template, so the
paths and line numbers it cites point into `prior_wrapper`: find the cited code there, not
at those lines of the file you see.
"""


VARIABLE_WRAPPER_INSTRUCTIONS = """\
# Task

Implement the exact `example_wrapper` template for C global `{variable_name}` and its
translation `{wrapped_crate}::{rust_variable_name}`, two representations of one logical
object. `sync_` functions in the template mean the global is mutable and must be
synchronized; without them it is immutable, and only `initial_value_matches` is needed.

# Template

Preserve the template's complete top-level shape and names.

# Conversion

Where the global's type has a `CInterop` conversion, use it rather than converting
field by field again.

# Tests

`initial_value_matches` compares `{wrapped_crate}::{rust_variable_name}` with the C
global as both were initialized, before any `sync_` call. `round_trip_nontrivial`
writes a nontrivial value to one side, syncs once, and checks the other side before
syncing back.

# Prior attempt

`prior_wrapper` is a rejected candidate to replace, not another definition to retain or
extend. `feedback` was produced with `prior_wrapper` in the crate in place of the
template, so the paths and line numbers it cites point into `prior_wrapper`.
"""


class WrapperGenerator:
    def __init__(
        self,
        cache: Path | None = None,
        *,
        codex: CodexConfig,
        model: ModelConfig,
        workdir: Path,
        rust_src: Path,
        c_src: Path,
        max_iters: int = 5,
    ) -> None:
        self.max_iters = max_iters
        self.cache = _init_cache(cache)
        self._codex = codex
        self._model = model
        self._workdir = workdir
        self._rust_src = rust_src
        self._c_src = c_src

    def generate(
        self,
        *,
        example_wrapper: CodeRust,
        prior_wrapper: CodeRust | None,
        feedback: str,
        build_feedback: str,
        lint_feedback: str,
        scope_feedback: str,
        wrapper: CodeRust | None,
        symbol: Symbol,
        wrapped_crate: str,
        hybrid_src: Path,
    ) -> Generation:
        if wrapper is not None:
            return Generation(code=wrapper)

        inputs = {
            "example_wrapper": example_wrapper,
            "prior_wrapper": prior_wrapper or CodeRust(),
            "feedback": feedback,
            "build_feedback": build_feedback,
            "lint_feedback": lint_feedback,
            "scope_feedback": scope_feedback,
        }
        if symbol.is_type:
            template = TYPE_WRAPPER_INSTRUCTIONS
        elif symbol.is_function:
            template = FUNCTION_WRAPPER_INSTRUCTIONS
        elif symbol.is_variable:
            template = VARIABLE_WRAPPER_INSTRUCTIONS
        else:
            raise ValueError(f"No wrapper prompt for `{symbol.kind}` `{symbol.name}`")

        # Codex runs with `--cd workdir`, so the prompt names files relative to it
        rust_src = str(self._rust_src.relative_to(self._workdir))
        c_src = str(self._c_src.relative_to(self._workdir))
        hybrid_rel = str(hybrid_src.relative_to(self._workdir))
        instructions = template.format(
            symbol_name=symbol.spelling,
            type_name=symbol.bindgen_name,
            variable_name=symbol.spelling,
            rust_variable_name=mangle(symbol.spelling),
            wrapped_crate=wrapped_crate,
            c_src=c_src,
        )
        files = {
            hybrid_rel: (
                "the hybrid crate: the bindgen C-layout types, the `CInterop` trait, the "
                "`__c_globals` externs, and the wrappers written so far. It can be very "
                "large, so search it for the specific declarations you need rather than "
                "reading it end to end; your output is appended to this file and must "
                "compile against it."
            ),
            rust_src: "the safe Rust crate being wrapped.",
            c_src: (
                "the C that goes into the build. It still defines the symbol you are "
                "wrapping; a symbol wrapped earlier appears as an extern declaration, and "
                "its translation is in the Rust crate above."
            ),
        }
        package = hybrid_src.parent.parent.name
        prompt = build_prompt(
            instructions,
            inputs,
            files,
            hybrid_rel,
            build_cmd=f"cargo test --no-run --lib -p {package}",
            lint_cmd=f"cargo clippy --lib -p {package}",
            task="wrap",
        )
        restore = [hybrid_src, self._rust_src, self._c_src]
        result = run_codex(prompt, self._codex, self._model, self._workdir, restore, hybrid_src)
        return result.as_generation(prompt, self._model.name)

    def session(
        self,
        symbol: Symbol,
        unimplemented_wrapper: CodeRust,
        wrapped_crate: str,
        hybrid_src: Path,
        prior_wrapper: CodeRust | None = None,
        feedback: Feedback = Feedback(),
    ) -> WrapperSession:
        return WrapperSession(
            generator=self,
            symbol=symbol,
            unimplemented_wrapper=unimplemented_wrapper,
            wrapped_crate=wrapped_crate,
            hybrid_src=hybrid_src,
            prior_wrapper=prior_wrapper,
            feedback=feedback,
        )

    def write_cache(self, attempt: WrapperAttempt) -> None:
        # Re-caching a cache hit would just duplicate the entry it came from
        if attempt.success and attempt.gen.was_generated:
            _write_cache(
                self.cache, attempt.symbol.spelling, attempt.bindgen_template, attempt.wrapper
            )


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
