#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
import sqlite3
from pathlib import Path

import dspy

from .tools import Crate
from .adapters import Code


logger = logging.getLogger("ideas.translate_snippet")

CodeC = Code["c"]
CodeRust = Code["rust"]


class SnippetTranslatorSignature(dspy.Signature):
    """
    Generate an idiomatic, memory-safe Rust translation of the snippet.
    The reference_code contains Rust code that should be used by the translation.
    The snippet contains a single C definition to translate to idiomatic, memory-safe Rust.
    The dependent_code contains C code that uses the C snippet.
    Reason about the dependent_code to understand any special memory management or complex ownership requirements a safe and idiomatic translation may need to take into account.
    Ensure the translation of the snippet does not use any unsafe constructs!
    Do not refactor the reference_code in the translation!
    Do not translate dependent_code to Rust in the translation!
    Do not define any implementations (`impl`) in the translation!
    Always assume all C integer arithmetic operations on the underlying value are intended to have wrapping semantics, and thus any translation should use Rust's wrapping arithmetic functions like `wrapping_add`, `wrapping_shr`, etc..
    Analyze all bitwise operations carefully, especially rotations.
    For all bitwise operations, including those that may appear to swap bits for bytes, implement the behavior exactly as written in the C code, without making assumptions about intent.
    For mutable global state, always translate to `std::sync::Mutex`-backed statics, use only the short names `Mutex` and `MutexGuard` (never `::std::sync::Mutex` nor `std::sync::Mutex` in emitted code), and require all accesses to go through `lock()`/`try_lock()` guards instead of `static mut` or other unsafe global mutation patterns.
    Use the feedback about the prior_translation, if provided, when generating the Rust translation.
    """

    reference_code: CodeRust = dspy.InputField()
    snippet: CodeC = dspy.InputField()
    dependent_code: CodeC = dspy.InputField()
    prior_translation: CodeRust = dspy.InputField()
    feedback: str = dspy.InputField()
    translation: CodeRust = dspy.OutputField()


class SnippetTranslator(dspy.Module):
    def __init__(
        self,
        translator: type[dspy.Module],
        crate: Crate,
        max_iters: int = 5,
        readonly_cache: Path | None = None,
    ):
        super().__init__()
        self.translate = translator(SnippetTranslatorSignature)
        self.crate = crate
        self.max_iters = max_iters
        self.readonly_cache = readonly_cache
        self.cache = _init_cache(crate.workspace_root / "cache.db")

    def forward(
        self,
        name: str,
        reference_code: str,
        snippet: str,
        dependent_code: str,
        prior_translation: str = "",
        feedback: str = "",
        translation: str = "",
    ) -> dspy.Prediction:
        logger.info(f"Translating snippet `{name}` ...")

        # If the snippet is empty, use static translation
        if not snippet:
            translation = f"// Empty snippet `{name}`"

        # Prefer supplied translation, crate cache, then read-only cache.
        translation = (
            translation
            or _read_cache(self.cache, name, snippet)
            or _read_cache(self.readonly_cache, name, snippet)
        )
        orig_rust_src = self.crate.rust_src_path.read_bytes()
        pred = dspy.Prediction()
        builds = False
        dspy_exception = None
        for i in range(max(self.max_iters, 1)):
            # Use the translation from the prior iteration as feedback for the next iteration
            if i > 0:
                prior_translation = translation

            # Ensure any translated snippet is safe and uses std::sync::Mutex
            rust_src = "#![forbid(unsafe_code)]\n"
            rust_src += "use std::sync::{Mutex, MutexGuard};\n\n"
            rust_src += (reference_code + "\n") if reference_code else ""

            # Use prior translation as the translation on first iteration only.
            # This allows static translations that violate safety, which will be fixed by the LLM!
            if i == 0 and translation:
                pred = dspy.Prediction(translation=CodeRust(code=translation))
            else:
                try:
                    pred = self.translate(
                        reference_code=CodeRust(code=rust_src),
                        snippet=CodeC(code=snippet),
                        dependent_code=CodeC(code=dependent_code),
                        prior_translation=CodeRust(code=prior_translation),
                        feedback=feedback,
                    )
                    dspy_exception = None
                except Exception as e:
                    logger.exception(
                        f"DSPy exception while translating snippet `{name}` on iteration {i + 1}/{self.max_iters}!"
                    )
                    dspy_exception = e
                    # Attempt again before any build logic
                    continue

            translation = pred.translation.code
            if translation in reference_code:
                translation = f"// duplicate snippet `{name}` detected"
            if translation == prior_translation:
                logger.warning("Snippet translation loop detected!")

            # Append translation and check if it builds
            rust_src += translation.strip() + "\n"
            self.crate.rust_src_path.write_text(rust_src)
            self.crate.vcs.add(self.crate.rust_src_path)
            # FIXME: Checking name for c:@F@main is brittle but we have no better way here.
            #        The proper way to fix is to yield the translation back to the caller so it can
            #        build and tell us whether to translation is successful.
            builds, feedback = self.crate.cargo_build(fix_E0601="c:@F@main" not in name)
            if not builds:
                feedback = "Running `cargo build` fails!\n" + feedback

            # Exit early if we build
            if builds:
                msg = f"Translated snippet `{name}`"
                logger.info(msg)
                msg += f"\n\n# Reasoning\n{pred.reasoning}" if "reasoning" in pred else ""
                self.crate.vcs.commit(msg)
                break

            msg = f"Failed to translate snippet `{name}` ({i + 1}/{self.max_iters})"
            logger.error(msg)
            msg += f"\n\n# Reasoning\n{pred.reasoning}" if "reasoning" in pred else ""
            msg += f"\n\n# Feedback\n{feedback}" if feedback else ""
            self.crate.vcs.commit(msg)
        self.crate.rust_src_path.write_bytes(orig_rust_src)
        # All iterations failed because of DSPy exceptions
        if dspy_exception:
            raise dspy_exception
        pred.name = name
        pred.snippet = snippet
        pred.reference_code = reference_code
        pred.dependent_code = dependent_code
        pred.prior_translation = prior_translation
        pred.feedback = feedback
        pred.translation = CodeRust(code=translation)
        pred.success = builds
        return pred

    def write_cache(self, pred: dspy.Prediction) -> None:
        _write_cache(
            self.cache,
            pred.name,
            pred.snippet,
            pred.reference_code,
            pred.dependent_code,
            pred.prior_translation,
            pred.feedback,
            pred.translation.code,
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


def _read_cache(cache: Path | None, name: str, snippet: str) -> str:
    translation = ""
    if cache is None:
        return translation
    with sqlite3.connect(cache) as conn:
        try:
            row = conn.execute(
                "SELECT translation FROM snippet_translations WHERE snippet=? AND success=1 ORDER BY id DESC LIMIT 1",
                (snippet,),
            ).fetchone()
        except Exception:
            row = None
    if row:
        logger.info(f"Cache hit for `{name}`")
        translation = row[0]
    return translation


def _write_cache(
    cache: Path | None,
    name: str,
    snippet: str,
    reference_code: str,
    dependent_code: str,
    prior_translation: str,
    feedback: str,
    translation: str,
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
                snippet,
                reference_code,
                dependent_code,
                prior_translation,
                feedback,
                translation,
                int(success),
            ),
        )
