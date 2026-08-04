#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
import sqlite3
from pathlib import Path
from collections.abc import Callable

import dspy
from dspy.utils.exceptions import AdapterParseError
from dspy.utils.usage_tracker import track_usage
from dspy.dsp.utils.settings import settings

from .ast import CodeC
from .ast_rust import CodeRust
from .model import format_usage


logger = logging.getLogger("ideas.translate_snippet")


class SnippetTranslatorSignature(dspy.Signature):
    """
    Generate an idiomatic, memory-safe Rust translation of a single C definition.

    # Inputs

    - `reference_code`: Existing Rust code the translation must build on. Use it as-is; do not refactor it.
    - `snippet`: The single C definition to translate.
    - `dependent_code`: C code that uses the snippet. Use it to determine concrete types for opaque and void pointers, ownership, lifetimes, and memory-management requirements; do not translate it.
    - `prior_translation` and `feedback`: If provided, treat the feedback as a critique of the prior translation and address it in the new translation.

    # Hard constraints

    - The translation must contain no `unsafe` constructs.
    - Do not include `#![forbid(unsafe_code)]` in the translation since it is included by default.
    - Do not define any `impl` blocks.
    - Define all top-level items (functions, structs, enums, type aliases, constants, statics, unions, traits, and modules) as fully public using plain `pub` (e.g., `pub fn ...`, `pub struct ...`). Also define every field of a top-level struct as `pub`, including unnamed fields in tuple structs (e.g., `pub struct Pair(pub i32, pub i32)`). Do not use restricted visibility such as `pub(crate)` or `pub(super)` anywhere; use only `pub`.
    - Do not weaken behavior with stubs, fallback defaults, relaxed assertions, or intentionally partial implementations.
    - Do not annotate any type with `#[repr(C)]`; the translation does not need C ABI compatibility.
    - Do not add `#[derive(...)]` attributes; they generate `impl` blocks and may impose behavior (e.g., `Default`, `Clone`) that does not match C semantics.
    - If the snippet references a C type not yet present in `reference_code`, include a correct translation of that type in the output so the translation compiles. Derive the referenced type's translation from `dependent_code` and any context visible in the snippet itself. Its translation will be reused as-is when the type's own snippet is processed later.

    # Faithfulness to C semantics

    The overarching rule: reproduce the C code's runtime behavior exactly. Do not "fix", simplify, or second-guess the C code's intent. For type definitions, use idiomatic Rust types that are semantically equivalent rather than structurally identical.

    ## Arithmetic and expressions

    - Treat all C integer arithmetic as wrapping. Use Rust's wrapping methods (`wrapping_add`, `wrapping_sub`, `wrapping_shr`, etc.).
    - Rust postfix operators (method calls, field access, indexing) bind tighter than unary operators (`-`, `!`), infix operators (`+`, `-`, `&`, `|`, `^`), and casts (`as`).
    - General receiver rule for postfix chaining: whenever the receiver is anything other than a simple identifier/path, parenthesize the full receiver first, then chain as `(EXPR).method(...)`, `(EXPR).field`, `(EXPR)[idx]`. Apply this uniformly to literals, unary expressions (including unary `-`), casts, and compound expressions; never rely on implicit precedence for the receiver.
    - Preserve C operator precedence and associativity exactly.
    - Preserve C's implicit signed/unsigned conversion behavior in mixed expressions and comparisons.
    - Implement bitwise operations (especially rotations and byte/bit shuffles) literally as written. Do not infer "intent" such as byte-swapping.
    - Reproduce inequality direction in bounds and length guards exactly (`>` vs `<`, `>=` vs `<=`). An inverted guard reverses the safety behavior.

    ## Integer text parsing

    - Accept the full range of C-valid inputs, including negatives that wrap into unsigned types (`-1i32 as u8 == 255`).
    - Always parse into a wide intermediate type that can represent the full C-valid input range before the final wrapping cast (at minimum `i64` for both narrow signed and narrow unsigned targets; `u64` is also acceptable where appropriate for unsigned-only flows), then apply a wrapping cast to the destination. Never parse digits directly as a narrow or unsigned destination type — that path rejects negatives and can overflow before the wrapping cast can run.
    - When the C code uses `scanf`/`sscanf`-style conversion, accept a leading numeric prefix and ignore trailing non-digit characters. Do not use bare `str::parse::<T>()` on the full trimmed string.

    ## scanf / sscanf behavior

    - Distinguish conversion failure (no match) from EOF; do not collapse them.
    - On conversion failure, leave destination variables holding their prior values. Declare such variables as `mut` bindings *outside* any retry loop so they retain their last successful value.
    - On conversion failure, do not advance the input position; the unmatched bytes must remain available for the next read.
    - Respect field widths and scansets exactly.

    ## Strings and NUL termination

    - Treat any length-sensitive operation on a C string buffer (`strlen`, `%s`-style usage, comparisons, hashing, etc.) as ending at the first NUL byte.
    - For pointer+length inputs, classify semantics before decoding: if the C code treats the data as a string (`strcmp`, `strlen`, `%s`, token parsing, command dispatch, pattern matching), normalize at ingress by truncating at the first `\0`; if the C path is fixed-length or binary, preserve embedded `\0` bytes and honor the explicit length.
    - When converting a NUL-terminated C string buffer into a Rust `String` for storage or downstream text processing, truncate at the first `\\0` *at the point of conversion*, not at the point of use. Stored string values that model C strings must never contain bytes at or after the NUL.
    - Before any comparison, pattern match, token parse, or command dispatch on C-origin string data, normalize at ingress by truncating at the first NUL byte.
    - Apply the same normalization policy to all operands in the same logical operation. Do not compare a length-decoded value that still includes trailing `\0` bytes against a C-string-decoded value already truncated at `\0`.
    - When both pointer and length are present for string-style data, use length only as a safety bound for reads; derive semantic content from C string termination and stop at the first `\0`.
    - Truncate once at the ingestion boundary and pass only normalized string values to downstream logic. Do not defer truncation to arbitrary leaf helpers when values are stored or reused across operations.
    - Do not compare raw decoded Rust `String` values that may include trailing NUL bytes when those values represent C strings.
    - This rule applies only when the original C code is treating the data as a NUL-terminated string (for example `strlen`, `%s`, string comparison, or string parsing). Do not truncate fixed-length, length-delimited, or binary buffers merely because they may contain `\\0`; preserve embedded NUL bytes unless the C code's semantics require string termination.

    ## Fixed-buffer line input (`fgets`)

    - Do not replace `fgets(buf, N, stdin)` with `read_line` or a bulk `io::stdin().read()`. Both consume too much input.
    - Replicate `fgets`: read at most N-1 bytes, stop after the first `'\\n'`, and leave all remaining input in stdin. Read byte-by-byte or use `BufRead::fill_buf` + `consume`.
    - When storing or comparing `fgets` output as a Rust `String`, truncate at the first `'\\n'` or `'\\0'`, whichever comes first. `fgets` retains the newline before the NUL, and keeping it breaks C-style trimmed comparisons.

    ## Return values and pointer arithmetic

    - Return exactly what the C function returns. If C returns a success/failure code, do not substitute a byte count or length.
    - C pointer subtraction (`end - start`) yields a count of elements, not bytes. Preserve the exact value.

    ## Pointer identity

    - When C compares pointers for identity, compare identity-equivalent Rust references. Cloning or copying changes identity and breaks the comparison.
    - Translate a C function that returns a pointer into a global/static container (e.g., `return &table[i]`) as a function returning `&T` into that container, not an owned clone.
    - If the container is locked: acquire the lock in the caller and borrow `&T` from the held guard. If the existing accessor locks internally and returns an owned value, the caller must bypass it — lock the container directly, borrow references from the guard, and finish all identity comparisons before releasing.

    ## Void pointers (`void *`)

    - A `void *` field or parameter is not inherently polymorphic. Inspect `dependent_code` to find every cast applied to the value. If all casts resolve to the same concrete type, translate the field using that concrete type (e.g., `Option<Box<ConcreteType>>`). Do not use `Box<dyn Any>`, `Box<dyn Trait>`, or any other type-erasure mechanism unless the pointer is genuinely polymorphic — i.e., cast to multiple structurally unrelated types in different code paths.
    - A `void *` used solely to break a forward-declaration cycle is not polymorphic. Resolve the concrete type from the casts and use it directly.
    - When the `void *` is nullable in C (compared to `NULL`, initialized to `0`, or conditionally assigned), translate it as `Option<Box<T>>` if the containing struct owns the allocation (evidenced by `free` being called through this field), or as `Option<&T>` / `Option<&mut T>` if it is a non-owning reference.

    ## Pointer fields in structs (`T *`, `T **`)

    Determine ownership from `dependent_code` before choosing a Rust type:

    - If `free(field)` or equivalent is called through the struct, the struct owns the pointee. Use `Box<T>` for a single value or `Vec<T>` for a heap-allocated array. Wrap in `Option<>` if the pointer may be null.
    - If the pointer is never freed through the struct (it aliases data owned elsewhere): use `&T` or `&mut T` with an appropriate lifetime.
    - If the field stores a heap array whose length is tracked separately (a C dynamic array): use `Vec<T>`, not `Box<[T]>`.
    - If the field is a fixed-length C array (`T arr[N]`): use `[T; N]`.
    - Do not use raw pointers (`*mut T`, `*const T`) as a shortcut when a safe owned or borrowed type is available.

    ## Mutable global state and locking

    - Never lock the same mutex/`RwLock` more than once in a single expression.
    - Acquire one guard, read/compute/write through it, then release.
    - Do not call helpers that acquire a lock (including stdin's implicit lock) from a scope that already holds that lock. Pick one locking model per code path.

    ## Binary parsers and slice contracts

    - Validate every length-derived slice with an explicit bounds check before slicing or copying.
    - Never reinterpret payload bytes as headers, and never re-derive a chunk's length by re-parsing its payload — use `slice.len()` on the bytes the helper was given.
    - When consuming bytes from a state-machine bitreader/bytestream, read directly from the reader. Do not reconstruct a backing slice and index into it; reconstructed slices may be short.
    - When the C source reads sequentially from a composite buffer (e.g., a primary `&[u32]` plus a trailing partial word), iterate through every component in order. Do not read only the primary array and silently drop the tail.

    ## Auxiliary state introduced by the translation

    If the translation introduces an auxiliary data structure (thread-local, `HashMap`, `RefCell<Vec<_>>`, etc.) to represent metadata that C tracked via struct fields or raw pointers, every function that conceptually reads or writes that metadata in C must read or write the auxiliary structure in Rust. A stub claiming the data is "not accessible in safe Rust" is never acceptable once such a mechanism exists.

    ## C primitive type mappings

    Use the following canonical mappings:

    - `char` (used as integer) → `i8`; `unsigned char` → `u8`
    - `short` → `i16`; `unsigned short` → `u16`
    - `int` → `i32`; `unsigned int` → `u32`
    - `long` → `i64`; `unsigned long` → `u64`
    - `long long` → `i64`; `unsigned long long` → `u64`
    - `float` → `f32`; `double` → `f64`
    - `size_t` → `usize`; `ptrdiff_t` → `isize`
    - `intptr_t` → `isize`; `uintptr_t` → `usize`
    - `int8_t`/`uint8_t` → `i8`/`u8`; `int16_t`/`uint16_t` → `i16`/`u16`; `int32_t`/`uint32_t` → `i32`/`u32`; `int64_t`/`uint64_t` → `i64`/`u64`
    - `char *` used as a string: see "Strings and NUL termination".

    ## C typedefs and forward declarations

    - A typedef that merely names an existing struct (`typedef struct Foo Foo;`) carries no information and should be omitted.
    - An anonymous struct typedef (`typedef struct { ... } Foo;`) translates to `pub struct Foo { ... }`.
    - A scalar typedef (`typedef unsigned int foo_t;`) translates to `pub type FooT = u32;`.
    - A function-pointer typedef (`typedef int (*cmp_fn)(int, int);`) translates to `pub type CmpFn = fn(i32, i32) -> i32;`.
    - Forward struct declarations (`struct Foo;`) are not translated; they become concrete when the full definition's snippet is processed.

    ## C enums

    - A C enum whose values are used as integers (assigned to integer variables, used in arithmetic, or used as array indices) translates to a group of `pub const` items with the appropriate integer type (default `i32`). Do not translate such enums as Rust `enum` variants; Rust enums are not freely interchangeable with integers.
    - A C enum whose values are used exclusively in switch/pattern-match contexts and never mixed with integers may be translated as a Rust `enum`.
    - Anonymous C enums (`enum { A = 0, B, C };`) follow the same rules; omit the type name.

    ## Observable output

    - Reproduce stdout/stderr text, spacing, punctuation, and line breaks exactly.
    - When the C source contains multi-byte UTF-8 literals (e.g., box-drawing characters), count Unicode scalar values, not bytes. Reproduce the same number of code points.

    # Allowed External Crates

    - `libc`: Raw FFI bindings to platform libraries like libc.
    - `openssl`: OpenSSL bindings
    - `flate2`: DEFLATE compression and decompression exposed as Read/BufRead/Write streams. Supports miniz_oxide and multiple zlib implementations. Supports zlib, gzip, and raw deflate streams.
    - `regex`: An implementation of regular expressions for Rust. This implementation uses finite automata and guarantees linear time matching on all inputs.

    Use functions from these crates, as needed, to translate the C code to equivalent, memory-safe Rust.
    """

    reference_code: CodeRust = dspy.InputField()
    snippet: CodeC = dspy.InputField()
    dependent_code: CodeC = dspy.InputField()
    prior_translation: CodeRust = dspy.InputField()
    feedback: str = dspy.InputField()
    translation: CodeRust = dspy.OutputField()


def _default_feedback_fn(translation: CodeRust) -> str:
    return ""


def _default_on_attempt(msg: str, pred: dspy.Prediction) -> None:
    pass


class SnippetTranslator(dspy.Module):
    def __init__(
        self,
        translator: type[dspy.Module],
        max_iters: int = 5,
        cache: Path | None = None,
    ):
        super().__init__()
        signature = SnippetTranslatorSignature
        self._translate = translator(signature)
        self.max_iters = max_iters
        self.cache = _init_cache(cache)

    def forward(
        self,
        name: str,
        crate_code: CodeRust,
        reference_code: CodeRust,
        snippet: CodeC,
        dependent_code: CodeC,
        prior_translation: CodeRust | None = None,
        feedback: str = "",
        feedback_fn: Callable[[CodeRust], str] | None = None,
        on_attempt: Callable[[str, dspy.Prediction], None] | None = None,
    ) -> dspy.Prediction:
        if feedback_fn is None:
            feedback_fn = _default_feedback_fn
        if on_attempt is None:
            on_attempt = _default_on_attempt

        logger.info(f"Translating snippet `{name}` ...")

        # Use cache when no prior translation
        if prior_translation is None and not str(snippet):
            # Special case for empty snippets: fall back to a static comment
            translation = CodeRust(f"// Empty snippet `{name}`")
        elif prior_translation is None:
            translation = _read_cache(self.cache, name, snippet)
            if translation is not None:
                translation = _make_public(translation)
        else:
            logger.info("Ignoring snippet cache...")
            translation = None

        pred = dspy.Prediction(feedback=feedback)
        for i in range(max(self.max_iters, 1)):
            # Use the translation from the prior iteration as feedback for the next iteration
            if i > 0:
                prior_translation = translation

            # Use prior translation as the translation on first iteration only.
            # This allows static translations that violate safety, which will be fixed by the LLM!
            try:
                pred = self.translate(
                    reference_code=reference_code,
                    snippet=snippet,
                    dependent_code=dependent_code,
                    prior_translation=prior_translation,
                    feedback=pred.feedback,
                    translation=translation if i == 0 else None,
                )
            except AdapterParseError:
                logger.exception(
                    f"DSPy exception while translating snippet `{name}` on iteration {i + 1}/{self.max_iters}!"
                )
                # If this is the last iteration, raise
                if i == max(self.max_iters, 1) - 1:
                    raise
                # Otherwise attempt again before any build logic
                continue

            translation = pred.translation
            assert isinstance(translation, CodeRust)
            if translation in crate_code:
                translation = CodeRust(f"// duplicate snippet `{name}` detected")
            if translation == prior_translation:
                logger.warning("Snippet translation loop detected!")
            pred.name = name
            pred.snippet = snippet
            pred.crate_code = crate_code
            pred.dependent_code = dependent_code
            pred.prior_translation = prior_translation or CodeRust()
            pred.translation = translation

            parts = []
            if CodeRust("#![forbid(unsafe_code)]") in translation:
                parts.append("Do not include `#![forbid(unsafe_code)]` in the translation!")
            if feedback := feedback_fn(translation):
                parts.append(feedback)
            pred.feedback = "\n\n".join(parts)
            pred.success = not pred.feedback

            if pred.success:
                msg = f"Translated snippet `{name}`: {format_usage(pred)}"
                logger.info(msg)
                on_attempt(msg, pred)
                break
            else:
                msg = f"Failed to translate snippet `{name}` ({i + 1}/{self.max_iters}): {format_usage(pred)}"
                logger.error(msg)
                on_attempt(msg, pred)
        return pred

    def translate(
        self,
        reference_code: CodeRust,
        snippet: CodeC,
        dependent_code: CodeC,
        prior_translation: CodeRust | None,
        feedback: str,
        translation: CodeRust | None,
    ) -> dspy.Prediction:
        parent_usage_tracker = settings.usage_tracker
        if translation is not None:
            pred = dspy.Prediction(translation=translation)
            if parent_usage_tracker is not None:
                pred.set_lm_usage({})
        else:
            if parent_usage_tracker is None:
                pred = self._translate(
                    reference_code=reference_code,
                    snippet=snippet,
                    dependent_code=dependent_code,
                    prior_translation=prior_translation or CodeRust(),
                    feedback=feedback,
                )
            else:
                with track_usage() as local_usage_tracker:
                    pred = self._translate(
                        reference_code=reference_code,
                        snippet=snippet,
                        dependent_code=dependent_code,
                        prior_translation=prior_translation or CodeRust(),
                        feedback=feedback,
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

        _write_cache(
            self.cache,
            pred.name,
            pred.snippet,
            pred.crate_code,
            pred.dependent_code,
            pred.prior_translation,
            pred.feedback,
            pred.translation,
            pred.success,
        )


def _init_cache(cache: Path | None) -> Path | None:
    if cache is None:
        return None
    with sqlite3.connect(cache) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snippet_translations (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                name              TEXT    NOT NULL,
                snippet           TEXT    NOT NULL,
                reference_code    TEXT    NOT NULL,
                dependent_code    TEXT    NOT NULL,
                prior_translation TEXT    NOT NULL,
                feedback          TEXT    NOT NULL,
                translation       TEXT    NOT NULL,
                success           INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_snippet_lookup"
            " ON snippet_translations (name, snippet)"
        )
    return cache


def _read_cache(cache: Path | None, name: str, snippet: CodeC) -> CodeRust | None:
    if cache is None:
        return None
    with sqlite3.connect(cache) as conn:
        try:
            row = conn.execute(
                "SELECT translation FROM snippet_translations WHERE snippet=? AND success=1 ORDER BY id DESC LIMIT 1",
                (str(snippet),),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT translation FROM snippet_translations WHERE name=? AND success=1 ORDER BY id DESC LIMIT 1",
                    (name,),
                ).fetchone()
        except Exception:
            row = None
    if row:
        logger.info(f"Cache hit for snippet `{name}`")
        return CodeRust(row[0])
    else:
        logger.info(f"Cache miss for snippet `{name}`")
        return None


def _make_public(translation: CodeRust) -> CodeRust:
    source = str(translation)
    from typing import Any
    from tree_sitter import Language, Parser, Query, QueryCursor
    from tree_sitter_rust import language as rust_language

    language = Language(rust_language())
    parser = Parser(language)
    tree = parser.parse(source.encode("utf-8"))
    query = Query(
        language,
        """
        (function_item
          (visibility_modifier)? @vis
          "fn" @fn_kw
        ) @item

        (struct_item
          (visibility_modifier)? @vis
          "struct" @kw
        ) @item

        (type_item
          (visibility_modifier)? @vis
          "type" @kw
        ) @item

        (enum_item
          (visibility_modifier)? @vis
          "enum" @kw
        ) @item

        (static_item
          (visibility_modifier)? @vis
          "static" @kw
        ) @item

        (union_item
          (visibility_modifier)? @vis
          "union" @kw
        ) @item

        (const_item
          (visibility_modifier)? @vis
          "const" @kw
        ) @item

        (trait_item
          (visibility_modifier)? @vis
          "trait" @kw
        ) @item

        (mod_item
          (visibility_modifier)? @vis
          "mod" @kw
        ) @item

        (struct_item
          body: (field_declaration_list
            (field_declaration
              (visibility_modifier)? @field_vis
              (field_identifier) @field_name
            ) @field
          )
        ) @struct

        """,
    )

    by_item: dict[tuple[int, int], dict[str, Any]] = {}
    by_field: dict[tuple[int, int], dict[str, Any]] = {}
    item_types = {
        "function_item",
        "struct_item",
        "type_item",
        "enum_item",
        "static_item",
        "union_item",
        "const_item",
        "trait_item",
        "mod_item",
    }

    cursor = QueryCursor(query)
    captures = cursor.captures(tree.root_node)
    for capture_name, nodes in captures.items():
        for node in nodes:
            if capture_name == "item":
                key = (node.start_byte, node.end_byte)
                by_item.setdefault(key, {"item": node, "vis": None, "kw": None})
                continue

            if capture_name == "field":
                key = (node.start_byte, node.end_byte)
                by_field.setdefault(key, {"field": node, "vis": None, "name": None})
                continue

            parent = node.parent
            while parent is not None and parent.type not in item_types | {
                "field_declaration",
            }:
                parent = parent.parent
            if parent is None:
                continue

            if parent.type == "field_declaration":
                key = (parent.start_byte, parent.end_byte)
                entry = by_field.setdefault(key, {"field": parent, "vis": None, "name": None})
                if capture_name == "field_vis":
                    entry["vis"] = node
                elif capture_name == "field_name":
                    entry["name"] = node
                continue

            key = (parent.start_byte, parent.end_byte)
            entry = by_item.setdefault(key, {"item": parent, "vis": None, "kw": None})
            if capture_name == "vis":
                entry["vis"] = node
            elif capture_name in {"fn_kw", "kw"}:
                entry["kw"] = node

    edits: list[tuple[int, int, bytes]] = []
    source_bytes = source.encode("utf-8")

    for entry in by_item.values():
        item_node = entry["item"]
        vis_node = entry["vis"]
        kw_node = entry["kw"]

        # Only rewrite module-level items.
        if item_node.parent is None or item_node.parent.type != "source_file":
            continue

        if vis_node is None:
            if kw_node is None:
                continue
            edits.append((kw_node.start_byte, kw_node.start_byte, b"pub "))
            continue

        vis_text = source_bytes[vis_node.start_byte : vis_node.end_byte].strip()
        if vis_text != b"pub":
            edits.append((vis_node.start_byte, vis_node.end_byte, b"pub"))

    for entry in by_field.values():
        field_node = entry["field"]
        vis_node = entry["vis"]
        name_node = entry["name"]

        # Only rewrite fields of top-level structs.
        struct_node = field_node.parent
        while struct_node is not None and struct_node.type != "struct_item":
            struct_node = struct_node.parent
        if struct_node is None or struct_node.parent is None:
            continue
        if struct_node.parent.type != "source_file":
            continue

        if vis_node is None:
            if name_node is None:
                continue
            edits.append((name_node.start_byte, name_node.start_byte, b"pub "))
            continue

        vis_text = source_bytes[vis_node.start_byte : vis_node.end_byte].strip()
        if vis_text != b"pub":
            edits.append((vis_node.start_byte, vis_node.end_byte, b"pub"))

    # Handle tuple struct fields programmatically: tree-sitter-rust has no
    # "ordered_field_declaration" wrapper node; fields are direct children of
    # "ordered_field_declaration_list".
    for struct_node in tree.root_node.children:
        if struct_node.type != "struct_item":
            continue
        for child in struct_node.children:
            if child.type != "ordered_field_declaration_list":
                continue
            pending_vis = None
            for fc in child.children:
                if fc.type in ("(", ")", ","):
                    pending_vis = None
                elif fc.type == "visibility_modifier":
                    pending_vis = fc
                elif fc.type == "attribute_item":
                    pass
                else:
                    # fc is a type node — this is a tuple field
                    if pending_vis is None:
                        edits.append((fc.start_byte, fc.start_byte, b"pub "))
                    else:
                        vis_text = source_bytes[
                            pending_vis.start_byte : pending_vis.end_byte
                        ].strip()
                        if vis_text != b"pub":
                            edits.append((pending_vis.start_byte, pending_vis.end_byte, b"pub"))
                    pending_vis = None
            break

    if not edits:
        return translation

    out = bytearray(source_bytes)
    for start, end, replacement in sorted(edits, key=lambda item: item[0], reverse=True):
        out[start:end] = replacement
    return CodeRust(out.decode("utf-8"))


def _write_cache(
    cache: Path | None,
    name: str,
    snippet: CodeC,
    crate_code: CodeRust,
    dependent_code: CodeC,
    prior_translation: CodeRust,
    feedback: str,
    translation: CodeRust,
    success: bool,
):
    if cache is None:
        return
    with sqlite3.connect(cache) as conn:
        conn.execute(
            """
            INSERT INTO snippet_translations
                (name, snippet, reference_code, dependent_code, prior_translation, feedback, translation, success)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                str(snippet),
                str(crate_code),
                str(dependent_code),
                str(prior_translation),
                feedback,
                str(translation),
                int(success),
            ),
        )
