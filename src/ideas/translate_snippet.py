#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
import sqlite3
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Mapping

from .ast import CodeC
from .ast_rust import CodeRust
from .codex import CodexConfig, build_prompt, run_codex
from .refine import GeneratedAttempt, Feedback, PredictSession
from .model import Generation, ModelConfig


logger = logging.getLogger("ideas.translate_snippet")


SNIPPET_INSTRUCTIONS = """\
# Task

Translate exactly `snippet` into idiomatic, memory-safe Rust with the same observable
behavior as the original C. Return only this symbol group and still-missing types it
requires; do not translate unrelated functions. Treat `feedback`, `build_feedback`, and
`scope_feedback` as evidence about the prior attempt and fix their root causes.

# Crate constraints

This crate is a standalone safe Rust implementation of the C program, with no raw
pointers, foreign ABIs, or `libc`. A separate hybrid crate later exposes it to C under
the original interface. That crate only converts values at each call and cannot supply
behavior, so every effect C code can observe must happen here and be reachable through
these types and functions, never through a `thread_local!` or other side table keyed by an
object's identity, which the hybrid's per-call conversion cannot follow. Declare every
item and field you emit `pub`, whatever its C linkage: the hybrid calls each function and
names each type. Use only the existing dependencies and, when required, these additional
crates:
{crates}

The existing prefix of the Rust crate translated so far is immutable and was already
accepted for earlier symbols. It is the only translation: a C-layout definition anywhere
else, such as raw-pointer bindings in a `-sys` crate, is a C declaration, so a type found
only there still needs its translation here.

# Representing C

The first translation of a type is final, so first search the C for every place that
allocates, embeds, casts, or frees it, and choose one the hybrid can map to and from every
C state:
- **Pointers and buffers.** A pointer becomes an `Option` where C allows NULL; memory C
  treats as raw bytes (`memset`, `memcpy`, byte offsets) is held here as bytes, and a
  pointer into it becomes an index or offset, but a struct C casts onto those bytes is a
  typed value, and a pointer it holds to another block is a handle; an array of pointers
  or typed values is a `Vec` of their translations, even where C moves it with `memmove`;
  an object that several pointers share, or that C hands out and later takes back, becomes
  a shared handle (`Rc`, `Weak` for back-pointers, or an arena index) so its identity
  survives.
- **Embedded structs.** A struct embedded in others and recovered from its address
  (`offsetof`, `container_of`) links those containers: each link must keep its container
  alive while linked (a strong handle to it, or an index into an arena that owns them,
  never a `Weak`), so every container field stays reachable from a link, even when the
  containing struct is not in this group: declare it now as a still-missing type the link
  requires.
- **`void *`.** A `void *` this code only stores and passes on stays an opaque shared
  handle; give it a concrete type only where this C casts it to one, the same type every
  producer stores: a `T *` to one object becomes a handle to that `T`, not a collection.
  Downcast to exactly the type that was erased: an `Rc<RefCell<T>>` stored as
  `Rc<dyn Any>` downcasts to `RefCell<T>`.
- **Callbacks and allocators.** A function pointer stays a callable value, stored where C
  stores it and called exactly where C calls it, with arguments that identify the C values
  it was given (a pointer to a key, value, or other object the C code stores passes that
  object's handle, never a copy of its bytes), and with no `RefCell` borrow or lock held
  across the call, since it may reach any object C can. A configured allocator is such a
  callback: each block it returns is an opaque token, kept in a field of the object that
  occupies the block (including an object's own block), beside that object's data, never
  holding it, and passed to the configured free where C frees it, and a failed allocation
  takes C's failure path.
- **Fields.** Keep every field C code can read or write, with its C meaning: a length is
  not a capacity, and a size C returns or allocates is the C type's size, not a Rust
  `size_of`.
- **Globals.** A mutable C global becomes a `static` holding an atomic or a `Mutex`, so
  safe code can read and write it; never `static mut`, which safe code cannot touch.
- **Strings.** Represent a C string the way the crate already does, with or without its
  NUL, and write a literal passed as a C string the same way.
- **Variadics.** A variadic function or one taking a `va_list` is translated like any
  other. Give its variable arguments a safe parameter type of your choosing; the wrapper
  reads them from C and passes them in.
- **`main`.** C `main` becomes `pub fn main()`, the program's entry point: it reads its
  arguments from `std::env::args_os()` and, after flushing standard output, exits with C's
  status.

# Prior attempt

`prior_translation`, when nonempty, is the complete rejected candidate for this symbol
group: replace it as a whole. Do not treat it as another immutable prefix, append a
second implementation after it, or return a patch. The new output must contain exactly
one complete definition of each requested symbol. `feedback` was produced with
`prior_translation` appended to the crate, so a line it cites past the immutable prefix
points into `prior_translation`.

# Before returning

Compare the result statement by statement with C, and trace each allocation, mutation,
and callback through construction, use, failure, and destruction.
"""


def snippet_instructions(deps: Mapping[str, str]) -> str:
    crates = "\n".join(
        f"- `{name}`: {' '.join(desc.split())}" if desc else f"- `{name}`"
        for name, desc in deps.items()
    )
    return SNIPPET_INSTRUCTIONS.format(crates=crates or "- none")


@dataclass(kw_only=True)
class TranslationAttempt(GeneratedAttempt):
    translation: CodeRust
    name: str
    snippet: CodeC

    @property
    def summary(self) -> str:
        verb = "Translated" if self.success else "Failed to translate"
        return f"{verb} snippet `{self.name}`"


@dataclass
class TranslationSession(PredictSession[TranslationAttempt, CodeRust]):
    translator: "SnippetTranslator"
    name: str
    crate_code: CodeRust
    snippet: CodeC
    prior_translation: CodeRust | None = None
    feedback: Feedback = Feedback()
    deterministic_translation: CodeRust | None = None

    @property
    def _max_iters(self) -> int:
        return max(self.translator.max_iters, 1)

    def _prime(self) -> CodeRust | None:
        # Use cache when no prior translation
        if self.prior_translation is not None:
            logger.info("Ignoring snippet cache...")
            return None
        # Special case for empty snippets: fall back to a static comment
        if not str(self.snippet):
            return CodeRust(f"// Empty snippet `{self.name}`")
        if self.deterministic_translation is not None:
            logger.info(f"Deterministic translation for snippet `{self.name}`")
            return self.deterministic_translation
        return _read_cache(self.translator.cache, self.name, self.snippet)

    def _attempt(
        self, prior: TranslationAttempt | None, replay: CodeRust | None
    ) -> TranslationAttempt:
        # A failed deterministic translation is not a prior attempt for the model to fix
        if prior is None or prior.translation == self.deterministic_translation:
            feedback, prior_translation = self.feedback, self.prior_translation
        else:
            feedback, prior_translation = prior.next_feedback, prior.translation
        gen = self.translator.translate(
            snippet=self.snippet,
            prior_translation=prior_translation,
            feedback=feedback.review,
            build_feedback=feedback.build,
            lint_feedback=feedback.lint,
            scope_feedback=feedback.scope,
            translation=replay,
        )

        # A turn that came back without a translation leaves the caller nothing to build
        translation = gen.code or CodeRust()
        # Every crate contains the empty translation, so only a real one can be a duplicate
        if translation and translation in self.crate_code:
            translation = CodeRust(f"// duplicate snippet `{self.name}` detected")
        if translation == prior_translation:
            logger.warning("Snippet translation loop detected!")
        return TranslationAttempt(
            translation=translation,
            gen=gen,
            prior_feedback=feedback,
            name=self.name,
            snippet=self.snippet,
        )


class SnippetTranslator:
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
        deps: Mapping[str, str] | None = None,
    ):
        self.max_iters = max_iters
        self.cache = _init_cache(cache)
        self._codex = codex
        self._model = model
        self._workdir = workdir
        self._rust_src = rust_src
        self._c_src = c_src
        self._instructions = snippet_instructions(deps or {})

    def translate(
        self,
        *,
        snippet: CodeC,
        prior_translation: CodeRust | None,
        feedback: str,
        build_feedback: str,
        lint_feedback: str,
        scope_feedback: str,
        translation: CodeRust | None,
    ) -> Generation:
        if translation is not None:
            return Generation(code=translation)

        inputs = {
            "snippet": snippet,
            "prior_translation": prior_translation or CodeRust(),
            "feedback": feedback,
            "build_feedback": build_feedback,
            "lint_feedback": lint_feedback,
            "scope_feedback": scope_feedback,
        }
        # Codex runs with `--cd workdir`, so the prompt names files relative to it
        rust_src = str(self._rust_src.relative_to(self._workdir))
        c_src = str(self._c_src.relative_to(self._workdir))
        files = {
            rust_src: (
                "the Rust crate translated so far, and the only place a symbol counts as "
                "translated. Search it for the items your translation references; your "
                "output is appended to this file and must compile against it."
            ),
            c_src: (
                "the original C. Search it for the callers of the snippet and for any "
                "definition the snippet references. A definition already translated "
                "appears here as an extern declaration; read that one from the Rust crate."
            ),
        }
        # A binary's `-sys` executable cannot link once symbols are wrapped
        package = self._rust_src.parent.parent.name
        prompt = build_prompt(
            self._instructions,
            inputs,
            files,
            rust_src,
            build_cmd=f"cargo build -p {package}",
            lint_cmd=f"cargo clippy --lib -p {package}",
            task="translate",
        )
        restore = [self._rust_src, self._c_src]
        result = run_codex(
            prompt, self._codex, self._model, self._workdir, restore, self._rust_src
        )
        return result.as_generation(prompt, self._model.name)

    def session(
        self,
        name: str,
        crate_code: CodeRust,
        snippet: CodeC,
        prior_translation: CodeRust | None = None,
        feedback: Feedback = Feedback(),
        deterministic_translation: CodeRust | None = None,
    ) -> TranslationSession:
        return TranslationSession(
            translator=self,
            name=name,
            crate_code=crate_code,
            snippet=snippet,
            prior_translation=prior_translation,
            feedback=feedback,
            deterministic_translation=deterministic_translation,
        )

    def write_cache(self, attempt: TranslationAttempt) -> None:
        # Re-caching a cache hit would just duplicate the entry it came from
        if attempt.success and attempt.gen.was_generated:
            _write_cache(self.cache, attempt.name, attempt.snippet, attempt.translation)


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
    translation: CodeRust,
):
    if cache is None:
        return
    with sqlite3.connect(cache) as conn:
        conn.execute(
            "INSERT INTO snippet_translations"
            " (name, snippet, reference_code, dependent_code, prior_translation, feedback,"
            "  translation, success)"
            " VALUES (?, ?, '', '', '', '', ?, 1)",
            (name, str(snippet), str(translation)),
        )
