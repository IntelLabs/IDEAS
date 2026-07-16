#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
import sqlite3
from pathlib import Path
from textwrap import indent

import dspy
from dspy.utils.exceptions import AdapterParseError
from dspy.utils.usage_tracker import track_usage
from dspy.dsp.utils.settings import settings

from .tools import Crate, LARGE_PROJECT
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
    - `dependent_code`: C code that uses the snippet. Use it only to understand ownership, lifetime, and memory-management requirements; do not translate it.
    - `prior_translation` and `feedback`: If provided, treat the feedback as a critique of the prior translation and address it in the new translation.

    # Hard constraints

    - The translation must contain no `unsafe` constructs.
    - Do not include `#![forbid(unsafe_code)]` in the translation since it is included by default.
    - Do not define any `impl` blocks.
    - Do not weaken behavior with stubs, fallback defaults, relaxed assertions, or intentionally partial implementations.

    # Faithfulness to C semantics

    The overarching rule: reproduce the C code's observable behavior exactly. Do not "fix", simplify, or second-guess the C code's intent.

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

    ## Observable output

    - Reproduce stdout/stderr text, spacing, punctuation, and line breaks exactly.
    - When the C source contains multi-byte UTF-8 literals (e.g., box-drawing characters), count Unicode scalar values, not bytes. Reproduce the same number of code points.
    """

    reference_code: CodeRust = dspy.InputField()
    snippet: CodeC = dspy.InputField()
    dependent_code: CodeC = dspy.InputField()
    prior_translation: CodeRust = dspy.InputField()
    feedback: str = dspy.InputField()
    translation: CodeRust = dspy.OutputField()


_crate_dependencies = """
# Crate dependencies:
The Rust project has visibility into the following crates:
- `flate2` for DEFLATE compression and decompression
- `regex` for regular expression parsing and matching

Use functions from these crates as needed to translate the C code to equivalent, memory-safe Rust.
"""


class SnippetTranslator(dspy.Module):
    def __init__(
        self,
        crate: Crate,
        translator: type[dspy.Module],
        max_iters: int = 5,
    ):
        super().__init__()
        signature = SnippetTranslatorSignature
        if LARGE_PROJECT:
            signature = signature.with_instructions(
                "\n\n".join([signature.instructions, _crate_dependencies])
            )

        self.crate = crate
        self._translate = translator(signature)
        self.max_iters = max_iters
        self.cache = _init_cache(crate.workspace_root / "cache.db")

    def forward(
        self,
        name: str,
        reference_code: CodeRust,
        reference_context: CodeRust,
        snippet: CodeC,
        dependent_code: CodeC,
        prior_translation: CodeRust | None = None,
        feedback: str = "",
    ) -> dspy.Prediction:
        logger.info(f"Translating snippet `{name}` ...")

        # Use cache when no prior translation
        if prior_translation is None and not str(snippet):
            # Special case for empty snippets: fall back to a static comment
            translation = CodeRust(f"// Empty snippet `{name}`")
        elif prior_translation is None:
            translation = _read_cache(self.cache, name, snippet)
        else:
            logger.info("Ignoring snippet cache...")
            translation = None

        orig_rust_src = self.crate.rust_src_path.read_bytes()
        pred = dspy.Prediction()
        builds = False
        for i in range(max(self.max_iters, 1)):
            # Use the translation from the prior iteration as feedback for the next iteration
            if i > 0:
                prior_translation = translation

            # Ensure any translated snippet is safe
            rust_src = CodeRust("#![forbid(unsafe_code)]")
            rust_src += reference_code

            # Use prior translation as the translation on first iteration only.
            # This allows static translations that violate safety, which will be fixed by the LLM!
            try:
                pred = self.translate(
                    reference_code if not LARGE_PROJECT else reference_context,
                    snippet,
                    dependent_code,
                    prior_translation,
                    feedback,
                    translation if i == 0 else None,
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
            if translation in reference_code:
                translation = CodeRust(f"// duplicate snippet `{name}` detected")
            if translation == prior_translation:
                logger.warning("Snippet translation loop detected!")

            # Append translation and check if it builds
            rust_src += translation
            self.crate.rust_src_path.write_text(str(rust_src))
            self.crate.vcs.add(self.crate.rust_src_path)
            # FIXME: Checking name for c:@F@main is brittle but we have no better way here.
            #        The proper way to fix is to yield the translation back to the caller so it can
            #        build and tell us whether to translation is successful.
            builds, feedback = self.crate.cargo_build(fix_E0601="c:@F@main" not in name)
            if not builds:
                feedback = "Running `cargo build` fails!\n" + feedback

            if CodeRust("#![forbid(unsafe_code)]") in translation:
                feedback = "Do not include `#![forbid(unsafe_code)]` in the translation!"
                builds = False

            usage = format_usage(pred)

            # Exit early if we build
            if builds:
                msg = f"Translated snippet `{name}`: {usage}"
                logger.info(msg)
                if "reasoning" in pred:
                    msg += f"\n\n# Reasoning\n{indent(pred.reasoning, '  ')}"
                self.crate.vcs.commit(msg)
                break

            msg = f"Failed to translate snippet `{name}` ({i + 1}/{self.max_iters}): {usage}"
            logger.error(msg)
            if "reasoning" in pred:
                msg += f"\n\n# Reasoning\n{indent(pred.reasoning, '  ')}"
            msg += f"\n\n# Feedback\n{indent(feedback, '  ')}" if feedback else ""
            self.crate.vcs.commit(msg)
        self.crate.rust_src_path.write_bytes(orig_rust_src)
        pred.name = name
        pred.snippet = snippet
        pred.reference_code = reference_code
        pred.dependent_code = dependent_code
        pred.prior_translation = prior_translation or CodeRust()
        pred.feedback = feedback
        pred.translation = translation
        pred.success = builds
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
        """Get a prediction for the current iteration."""
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
            pred.reference_code,
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


def _write_cache(
    cache: Path | None,
    name: str,
    snippet: CodeC,
    reference_code: CodeRust,
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
                str(reference_code),
                str(dependent_code),
                str(prior_translation),
                feedback,
                str(translation),
                int(success),
            ),
        )
