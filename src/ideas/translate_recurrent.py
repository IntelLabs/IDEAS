#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
from pathlib import Path
from typing import Literal
from functools import partial
from contextlib import contextmanager
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace

import networkx as nx

from . import model
from .ast_rust import CodeRust
from .ast_rust import get_node_text, get_root, mangle, non_public_items
from .refine import Feedback
from .hybrid import SAFE_CRATE, Hybrid, HybridSnapshot
from .model import LMUsage
from .oracle import Candidate, SymbolOracle, Verdict
from .tools import Crate
from .ast import Symbol, SymbolName, SymbolGroup
from .ast import CodeC, TreeResult, create_symbol_ordering_key_fn
from .wrapper import WrapperAttempt, WrapperGenerator, scope_errors
from .wrapper import generate_unimplemented_function_wrapper
from .wrapper import generate_unimplemented_type_wrapper, type_wrapper_errors
from .wrapper import generate_unimplemented_variable_wrapper, variable_wrapper_errors
from .translate_snippet import SnippetTranslator, TranslationAttempt

logger = logging.getLogger("ideas.translate_recurrent")
snippet_logger = logging.getLogger("ideas.translate_snippet")
wrapper_logger = logging.getLogger("ideas.wrapper")


@dataclass
class _RsSnapshot:
    lib_src: bytes
    main_src: bytes | None


@dataclass
class _Snapshot:
    translator: _RsSnapshot
    wrapper: HybridSnapshot


@dataclass(frozen=True)
class _WrapperPlan:
    unimplemented_wrapper: CodeRust
    extra_errors: Callable[[CodeRust], list[str]] | None = None
    tests_mod: str | None = None
    c_symbol: str | None = None

    def errors(self, wrapper: CodeRust) -> list[str]:
        errors = scope_errors(wrapper, self.unimplemented_wrapper)
        if self.extra_errors is not None:
            errors += self.extra_errors(wrapper)
        return errors


@dataclass
class SymbolResult:
    attempt: WrapperAttempt | None = field(default=None, repr=False, compare=False)
    verdict: Verdict | None = None

    @property
    def success(self) -> bool:
        return self.verdict is None or not self.verdict.rejected

    @property
    def stage(self) -> Literal["wrap", "review"] | None:
        return self.verdict.stage if self.verdict is not None else None

    @property
    def wrapped(self) -> bool:
        return self.attempt is None or self.attempt.success

    @property
    def rejection(self) -> str:
        return "" if self.attempt is None or self.attempt.success else self.attempt.reason

    @property
    def wrapper(self) -> CodeRust | None:
        return self.attempt.wrapper if self.attempt is not None else None

    @property
    def regressed(self) -> set[str]:
        return self.verdict.blamed if self.verdict is not None else set()

    @property
    def feedback(self) -> str:
        return self.verdict.feedback if self.verdict is not None else ""

    @property
    def wrap_feedback(self) -> Feedback:
        review = self.verdict.wrap_feedback if self.verdict is not None else ""
        prior = self.attempt.next_feedback if self.attempt is not None else Feedback()
        return replace(prior, review=review)


@dataclass
class _TranslationResult:
    attempt: TranslationAttempt = field(repr=False, compare=False)

    @property
    def success(self) -> bool:
        return self.attempt.success

    @property
    def translation(self) -> CodeRust:
        return self.attempt.translation

    @property
    def feedback(self) -> Feedback:
        return self.attempt.next_feedback


@dataclass
class _Result:
    translation_result: _TranslationResult
    symbol_results: dict[SymbolName, SymbolResult] = field(default_factory=dict)

    @property
    def translation(self) -> CodeRust:
        return self.translation_result.translation

    @property
    def failure(self) -> Literal["translate", "wrap", "review"] | None:
        if not self.translation_result.success:
            return "translate"
        for r in self.symbol_results.values():
            if r.stage is not None:
                return r.stage
        return None

    @property
    def success(self) -> bool:
        return self.failure is None

    @property
    def regressed(self) -> set[str]:
        return {t for r in self.symbol_results.values() for t in r.regressed}

    @property
    def translation_feedback(self) -> Feedback:
        if not self.translation_result.success:
            return self.translation_result.feedback
        # The translation built and stayed in scope, so only the oracle has anything to add
        for r in self.symbol_results.values():
            if not r.success:
                return Feedback(review=r.feedback)
        return Feedback()

    @property
    def wrap_feedback(self) -> dict[SymbolName, Feedback]:
        return {n: fb for n, r in self.symbol_results.items() if (fb := r.wrap_feedback)}

    @property
    def wrappers(self) -> dict[SymbolName, CodeRust]:
        return {n: r.wrapper for n, r in self.symbol_results.items() if r.wrapper is not None}


@dataclass
class TranslationReport:
    complete: bool
    regressions: dict[str, SymbolGroup]
    usage: LMUsage | None = None


class SymbolTranslator:
    def __init__(self, rs_crate: Crate, translator: SnippetTranslator):
        self._rs_crate = rs_crate
        self._translator = translator

    def write_cache(self, result: _TranslationResult):
        self._translator.write_cache(result.attempt)

    def snapshot(self) -> _RsSnapshot:
        assert self._rs_crate.lib_src_path is not None
        main_src_path = self._rs_crate.main_src_path
        return _RsSnapshot(
            lib_src=self._rs_crate.lib_src_path.read_bytes(),
            main_src=main_src_path.read_bytes() if main_src_path else None,
        )

    def restore(self, snapshot: _RsSnapshot):
        def write(path: Path | None, src: bytes | None):
            if path is not None and src is not None:
                path.write_bytes(src)
                self._rs_crate.vcs.add(path)

        write(self._rs_crate.lib_src_path, snapshot.lib_src)
        write(self._rs_crate.main_src_path, snapshot.main_src)

    def translate(
        self,
        name: str,
        crate_code: CodeRust,
        snippet: CodeC,
        prior_translation: CodeRust | None = None,
        feedback: Feedback = Feedback(),
        deterministic_translation: CodeRust | None = None,
    ) -> _TranslationResult:
        rs_crate = self._rs_crate
        assert rs_crate.lib_src_path is not None
        lib_src_path = rs_crate.lib_src_path
        main_src_path = rs_crate.main_src_path
        base_lib_src = CodeRust(lib_src_path.read_text())
        base_main_src = main_src_path.read_text() if main_src_path is not None else None

        def build(code: CodeRust) -> str:
            lib_src_path.write_text(str(base_lib_src + code))
            rs_crate.vcs.add(lib_src_path)
            builds, error = rs_crate.cargo_build()
            return "" if builds else f"Running `cargo build` fails!\n{error}"

        def lint(code: CodeRust) -> str:
            clippy = rs_crate.cargo_clippy()
            if ignored := clippy.not_in(code).render(failure=not clippy.has_errors):
                logger.warning(f"Ignored Clippy findings:\n{ignored}")
            clippy = clippy.only_in(code)
            findings = clippy.render()
            return f"Clippy feedback:\n{findings}" if findings else ""

        # Import the translated `main` from lib.rs so a binary's build feedback covers it
        is_main_function = name == "c:@F@main"
        if is_main_function and main_src_path is not None:
            if rs_crate.lib_name is None:
                raise ValueError(f"Expected a library target for {main_src_path}")
            main_src_path.write_bytes(
                _replace_main_with(main_src_path, f"use {rs_crate.lib_name}::main;")
            )
            rs_crate.vcs.add(main_src_path)

        snippet_logger.info(f"Translating snippet `{name}` ...")
        with self._translator.session(
            name=name,
            crate_code=crate_code,
            snippet=snippet,
            prior_translation=prior_translation,
            feedback=feedback,
            deterministic_translation=deterministic_translation,
        ) as session:
            attempt: TranslationAttempt | None = None
            for attempt in session:
                scope_errors = []
                if private := non_public_items(attempt.translation):
                    names = ", ".join(f"`{n}`" for n in private)
                    scope_errors.append(
                        f"{names} must be declared `pub`: the hybrid crate calls every "
                        "function and names every type and field."
                    )
                build_error = build(attempt.translation)

                if build_error:
                    attempt.reject(scope=scope_errors, build=[build_error])
                elif error := lint(attempt.translation):
                    attempt.reject(scope=scope_errors, lint=[error])
                elif scope_errors:
                    attempt.reject(scope=scope_errors)
                else:
                    attempt.accept()
                attempt.commit(rs_crate)

                # A rejected candidate must not be on disk when the next one is generated
                if not attempt.success:
                    lib_src_path.write_text(str(base_lib_src))

        assert attempt is not None  # `_max_iters` is at least 1, so the loop always ran
        # Nothing supplies the entrypoint the switch imports, so put the placeholder back
        if not attempt.success and (
            is_main_function and main_src_path is not None and base_main_src is not None
        ):
            main_src_path.write_text(base_main_src)
        return _TranslationResult(attempt)


class SymbolWrapper:
    def __init__(
        self,
        hybrid: Hybrid,
        generator: WrapperGenerator | None,
        oracle: SymbolOracle,
    ):
        self._hybrid = hybrid
        self._generator = generator
        self._oracle = oracle
        self._prepared = False
        # Crate-root names the hybrid will later give C functions and globals
        self._c_item_names: frozenset[str] = frozenset()

    @property
    def prepared(self) -> bool:
        return self._prepared

    def prepare(self, symbols: Iterable[Symbol]):
        symbols = list(symbols)
        hybrid = self._hybrid
        self._c_item_names = frozenset(
            mangle(s.spelling) for s in symbols if s.is_function or s.is_variable
        )

        # The hybrid's `main` calls the `__ideas_entry` this defines, so the baseline needs it; the
        # symbols were extracted before, so they still see the original `main`
        if hybrid.crate.main_src_path is not None:
            hybrid.adopt_c_entrypoint()

        # Tests that already fail against the untranslated C never count against a translation
        self._oracle.baseline()

        # Import types and variables before any generated code, which must reference the types and
        # cannot shadow the statics (E0530)
        hybrid.import_symbols(symbols)

        builds, feedback = hybrid.crate.cargo_build()
        if not builds:
            raise RuntimeError(f"Running `cargo build` fails!\n{feedback}")
        # Importing must not change behaviour, so it answers to the baseline like any later change
        if feedback := self._oracle.audit():
            raise RuntimeError(feedback)

        msg = f"Initialized hybrid `{hybrid.crate.name}` ({len(symbols)} symbols)"
        logger.info(msg)
        if baseline_failures := self._oracle.baseline_failures:
            msg += (
                "\n\n# Excluded Tests\n"
                "Excluded test(s) that already fail against the untranslated C: "
                f"{', '.join(sorted(baseline_failures))}"
            )
        hybrid.crate.vcs.commit(msg)
        self._prepared = True

    def _build(self) -> str:
        builds, feedback = self._hybrid.crate.cargo_build()
        if not builds:
            return f"Running `cargo build` fails!\n{feedback}"
        # cargo build skips test targets, so a wrapper could still break the tests' build
        return self._oracle.build()

    def _test(self, name: str) -> str:
        passes, _, error, _ = self._hybrid.crate.cargo_test(name, lib=True)
        return "" if passes else f"Running `cargo test {name}` fails!\n{error}"

    def write_cache(self, result: SymbolResult):
        if result.attempt is not None and self._generator is not None:
            self._generator.write_cache(result.attempt)

    def snapshot(self) -> HybridSnapshot:
        return self._hybrid.snapshot()

    def restore(self, snapshot: HybridSnapshot):
        self._hybrid.restore(snapshot)

    def wrap(
        self,
        symbols: Iterable[Symbol],
        snippet: CodeC,
        translation: CodeRust,
        prior_wrappers: Mapping[SymbolName, CodeRust],
        feedback: Mapping[SymbolName, Feedback],
    ) -> dict[SymbolName, SymbolResult]:
        results: dict[SymbolName, SymbolResult] = {}
        for symbol in symbols:
            result = self._wrap_symbol(
                symbol, prior_wrappers.get(symbol.name), feedback.get(symbol.name, Feedback())
            )
            if result is None:
                continue
            # Judged whether or not the wrapper stage succeeded: a failed wrap is a rejection
            # the oracle words, and a successful one is what the tests then run against
            candidate = Candidate(
                symbol=symbol,
                snippet=snippet,
                translation=translation,
                wrapper=result.wrapper,
                wrap_rejection=result.rejection,
                wrapped=result.wrapped,
            )
            result = replace(result, verdict=self._judge(candidate))
            results[symbol.name] = result
            if not result.success:
                break
        return results

    @contextmanager
    def _checkpoint(self) -> Iterator[HybridSnapshot]:
        # Restored unconditionally, and the yielded state outlives the block so a caller can
        # return to it again later
        state = self._hybrid.snapshot()
        try:
            yield state
        finally:
            self._hybrid.restore(state)

    def _wrap_symbol(
        self,
        symbol: Symbol,
        prior_wrapper: CodeRust | None,
        feedback: Feedback = Feedback(),
    ) -> SymbolResult | None:
        is_bin = self._hybrid.crate.main_src_path is not None

        result = None
        if is_bin and symbol.spelling == "main":
            # Rust has to own the entrypoint or its `main` collides with C's at link time
            result = self._wrap_main()
        elif is_bin and symbol.is_function and not self._oracle.exercises(symbol):
            # As a policy, an untested binary gets no function wrappers other than `main`
            pass
        elif symbol.is_type and symbol.is_definition:
            result = self._wrap_type(symbol, prior_wrapper, feedback)
        elif symbol.is_variable:
            result = self._wrap_variable(symbol, prior_wrapper, feedback)
        elif symbol.is_function and symbol.is_definition:
            result = self._wrap_function(symbol, prior_wrapper, feedback)
        if result is None:
            logger.debug("Skipped wrapping of symbol `%s`", symbol.name)
            return None
        return result

    def _wrap_main(self) -> SymbolResult:
        logger.info("Wrapping function `main` ...")

        # The bin's entrypoint now comes from the -rs crate instead of the C object
        self._hybrid.take_over_main()

        # Fail fast so linker errors surface before any more wrapping
        if feedback := self._build():
            raise RuntimeError(feedback)
        self._hybrid.crate.vcs.commit("Wrapped function `main`")
        return SymbolResult()

    def _wrap_variable(
        self,
        symbol: Symbol,
        prior_wrapper: CodeRust | None,
        feedback: Feedback = Feedback(),
    ) -> SymbolResult | None:
        if self._generator is None:
            return SymbolResult()

        unimplemented_wrapper, tests_mod, sync_fns = generate_unimplemented_variable_wrapper(
            symbol.spelling, self._hybrid.types_code, self._hybrid.bindings[symbol.name]
        )
        static_errors = partial(
            variable_wrapper_errors,
            spelling=symbol.spelling,
            tests_mod=tests_mod,
            sync_fns=sync_fns,
            rust_lib_name=SAFE_CRATE,
        )

        return self._run_wrapper_session(
            symbol,
            prior_wrapper,
            feedback,
            _WrapperPlan(unimplemented_wrapper, static_errors, tests_mod=tests_mod),
        )

    def _wrap_type(
        self,
        symbol: Symbol,
        prior_wrapper: CodeRust | None,
        feedback: Feedback = Feedback(),
    ) -> SymbolResult | None:
        if self._generator is None:
            return None
        # Only an externally visible struct has a meaningful `to_rust`/`sync_to_c` pair to bridge
        if not (symbol.is_struct and symbol.is_externally_visible):
            return None
        # An anonymous record, or one nested in one, has no predictable bindgen name to implement on
        if (bindgen_name := symbol.bindgen_name) is None:
            logger.warning(
                f"Cannot wrap type `{symbol.name}`: it is or is nested in an anonymous "
                "record, which bindgen names with a run-wide counter"
            )
            return None

        # A stub keeps the hybrid building while the generator implements it
        unimplemented_wrapper, tests_mod = generate_unimplemented_type_wrapper(
            bindgen_name, self._hybrid.types_code
        )
        static_errors = partial(
            type_wrapper_errors,
            bindgen_name=bindgen_name,
            spelling=symbol.spelling,
            tests_mod=tests_mod,
            rust_lib_name=SAFE_CRATE,
            c_item_names=self._c_item_names,
        )

        return self._run_wrapper_session(
            symbol,
            prior_wrapper,
            feedback,
            _WrapperPlan(unimplemented_wrapper, static_errors, tests_mod=tests_mod),
        )

    def _wrap_function(
        self,
        symbol: Symbol,
        prior_wrapper: CodeRust | None,
        feedback: Feedback = Feedback(),
    ) -> SymbolResult | None:
        if self._generator is None:
            return None
        # No point in wrapping non-globals if the oracle does not exercise it
        if not symbol.is_externally_visible and not self._oracle.exercises(symbol):
            return None

        # A stub keeps the hybrid building while the generator implements it
        unimplemented_wrapper = generate_unimplemented_function_wrapper(
            self._hybrid.c_src_path,
            symbol.spelling,
            self._hybrid.types_code,
        )
        if unimplemented_wrapper is None:
            logger.warning(f"Skipping wrap of function symbol `{symbol.name}`")
            return SymbolResult()

        return self._run_wrapper_session(
            symbol,
            prior_wrapper,
            feedback,
            _WrapperPlan(unimplemented_wrapper, c_symbol=symbol.spelling),
        )

    def _run_wrapper_session(
        self,
        symbol: Symbol,
        prior_wrapper: CodeRust | None,
        feedback: Feedback,
        plan: _WrapperPlan,
    ) -> SymbolResult:
        assert self._generator is not None  # every caller checks before building a plan
        hybrid = self._hybrid

        def build(wrapper: CodeRust) -> str:
            # Only C callers need a static function's symbol, and none are left once C is stranded
            hybrid.wrap(
                symbol.name,
                wrapper,
                c_symbol=plan.c_symbol,
                scaffolding=symbol.is_function and not symbol.is_externally_visible,
            )
            return self._build()

        def lint(wrapper: CodeRust) -> tuple[str, str]:
            clippy = hybrid.crate.cargo_clippy()
            if ignored := clippy.not_in(wrapper).render(failure=not clippy.has_errors):
                logger.warning(f"Ignored Clippy findings:\n{ignored}")
            clippy = clippy.only_in(wrapper)
            fatal = clippy.render(("error",))
            warnings = clippy.render(("warning",), failure=False)
            return (
                f"Clippy errors/failure:\n{fatal}" if fatal else "",
                f"Clippy warnings:\n{warnings}" if warnings else "",
            )

        def test() -> str:
            return "" if plan.tests_mod is None else self._test(plan.tests_mod)

        # Fail fast so compile or linker errors surface before iterating on the wrapper
        with self._checkpoint() as state:
            if error := build(plan.unimplemented_wrapper):
                raise RuntimeError(error)

        # Both templates give the hybrid a lib.rs
        assert hybrid.crate.lib_src_path is not None
        wrapper_logger.info(f"Generating wrapper for `{symbol.name}` ...")
        with self._generator.session(
            symbol=symbol,
            unimplemented_wrapper=plan.unimplemented_wrapper,
            wrapped_crate=SAFE_CRATE,
            hybrid_src=hybrid.crate.lib_src_path,
            prior_wrapper=prior_wrapper,
            feedback=feedback,
        ) as session:
            attempt: WrapperAttempt | None = None
            for attempt in session:
                scope_errors = plan.errors(attempt.wrapper)
                build_error = build(attempt.wrapper)
                test_error = lint_errors = lint_warnings = ""
                if not build_error:
                    test_error = test()
                    lint_errors, lint_warnings = lint(attempt.wrapper)
                lint_text = (lint_errors + "\n\n" + lint_warnings).strip()
                lint_feedback = [lint_text] if lint_text else []

                if build_error:
                    attempt.reject(scope=scope_errors, build=[build_error])
                elif test_error:
                    attempt.reject(scope=scope_errors, build=[test_error], lint=lint_feedback)
                elif lint_errors or scope_errors:
                    attempt.reject(scope=scope_errors, lint=lint_feedback)
                elif lint_warnings and attempt.index < attempt.total:
                    attempt.reject(scope=scope_errors, lint=lint_feedback)
                else:
                    attempt.accept()
                attempt.commit(hybrid.crate)

                # A rejected candidate must not be on disk when the next one is generated
                if not attempt.success:
                    hybrid.restore(state)
        return SymbolResult(attempt=attempt)

    def _judge(self, candidate: Candidate) -> Verdict:
        # Only a hybrid that builds can be tested, and only an exercised symbol will be
        oracle = self._oracle
        if candidate.wrapped and oracle.exercises(candidate.symbol):
            if error := self._build():
                raise RuntimeError(error)

        verdict = oracle.judge(candidate)
        # A wrap rejection leaves no message: the session already committed every attempt
        if verdict.message:
            self._hybrid.crate.vcs.commit(f"{verdict.message}\n\n{verdict.evidence}".strip())
        return verdict


def _replace_main_with(main_src_path: Path, replacement: str) -> bytes:
    main_src = main_src_path.read_bytes()
    main_items = [
        item
        for item in get_root(main_src).children
        if item.type == "function_item"
        and get_node_text(item.child_by_field_name("name")) == "main"
    ]
    if len(main_items) != 1:
        raise ValueError(f"Expected one top-level `main` function in {main_src_path}")

    main_item = main_items[0]
    return (
        main_src[: main_item.start_byte] + replacement.encode() + main_src[main_item.end_byte :]
    )


class RecurrentTranslator:
    def __init__(
        self,
        translator: SymbolTranslator,
        wrapper: SymbolWrapper,
        oracle: SymbolOracle,
        *,
        max_iters: int = 1,
    ):
        self._translator = translator
        self._wrapper = wrapper
        self._oracle = oracle
        self._max_iters = max_iters

    # Separate from `__call__` so a broken setup is fatal, not a failed translation
    def prepare(self, symbols: Iterable[Symbol]):
        self._wrapper.prepare(symbols)

    def __call__(
        self,
        symbols: Mapping[SymbolName, Symbol],
        dependencies: Mapping[SymbolGroup, Iterable[SymbolGroup]],
        ast_order: dict[Path, TreeResult] | None = None,
    ) -> TranslationReport:
        assert self._wrapper.prepared, "Call `prepare` before translating!"
        # Process symbols in topological order
        G = nx.from_dict_of_lists(dict(dependencies), create_using=nx.DiGraph)
        assert isinstance(G, nx.DiGraph)  # create_using guarantees this
        groups = list(
            nx.lexicographical_topological_sort(
                G.reverse(copy=False), key=create_symbol_ordering_key_fn(symbols, ast_order)
            )
        )
        logger.debug(f"Symbol group order: {[' '.join(g) for g in groups]}")

        snippets: dict[CodeC, SymbolGroup] = {}
        translations: dict[SymbolGroup, CodeRust] = {}
        count = len(groups)
        for i, group in enumerate(groups, start=1):
            # A group that is nothing but alias typedefs has no Rust form to ask a model for
            if all(symbols[name].is_alias_typedef for name in group):
                logger.info(f"Skipping alias typedef `{' '.join(group)}`...")
                translations[group] = CodeRust(f"// alias typedef `{' '.join(group)}`")
                continue

            logger.info(f"Translating symbol group `{' '.join(group)}` [{i}/{count}] ...")

            # Gather code for each symbol and check if we have already translated such a snippet
            snippet = CodeC.join(symbols[name].code for name in group)
            if snippet in snippets:
                logger.info(
                    f"Skipping translation of `{' '.join(group)}` because it was already translated by `{' '.join(snippets[snippet])}`..."
                )
                translations[group] = translations[snippets[snippet]]
                continue
            snippets[snippet] = group

            # Unique translations only: many symbol names can map to the same translation
            crate_code = CodeRust.join(
                dict.fromkeys(translations[g] for g in groups if g in translations)
            )

            # Translate and wrap snippet, saving it if it tests
            with model.track_lm_usage() as usage:
                group_result = self._translate_and_wrap_with_retries(
                    crate_code=crate_code, symbols=[symbols[name] for name in group]
                )
            if group_result.failure == "translate":
                # Translate failures (as opposed to wrap/review failures) are fatal
                logger.error(
                    f"Failed to translate symbol group `{' '.join(group)}` [{i}/{count}] "
                    f"to Rust: {self._summary(usage)}"
                )
                break
            translations[group] = group_result.translation

            # Record the culprit so no later group is blamed for the same test
            if regressed := group_result.regressed:
                self._oracle.blame(group, regressed)
                logger.warning(
                    f"Test(s) regressed by symbol group `{' '.join(group)}`: "
                    f"{', '.join(sorted(regressed))}"
                )

            logger.info(
                f"Translated symbol group `{' '.join(group)}` [{i}/{count}] to Rust: "
                f"{self._summary(usage)}"
            )
        return TranslationReport(
            complete=len(translations) == len(groups),
            regressions=self._oracle.regressions,
        )

    def _translate_and_wrap_with_retries(
        self,
        crate_code: CodeRust,
        symbols: list[Symbol],
        prior_translation: CodeRust | None = None,
        prior_wrappers: dict[SymbolName, CodeRust] | None = None,
        translation_feedback: Feedback = Feedback(),
        wrap_feedback: dict[SymbolName, Feedback] | None = None,
    ) -> _Result:
        name = " ".join([f"`{s.name}`" for s in symbols])
        num_iters = max(self._max_iters, 1)
        # The last state the oracle accepted; no outcome of this call may land below it
        entry = self._snapshot()
        best: tuple[int, _Snapshot, _Result] | None = None
        result: _Result | None = None

        for i in range(num_iters):
            # Attempt translation and exit early on success
            result = self._translate_and_wrap(
                crate_code,
                symbols,
                prior_translation=prior_translation,
                prior_wrappers=prior_wrappers,
                translation_feedback=translation_feedback,
                wrap_feedback=wrap_feedback,
            )
            if result.success:
                return result

            if result.failure == "review" and (
                best is None or len(result.regressed) <= best[0]
            ):
                best = (len(result.regressed), self._snapshot(), result)

            # If neither translation nor wrappers differ from previous try, then stop retrying
            looping = (
                prior_translation is not None
                and prior_translation == result.translation
                and prior_wrappers is not None
                and prior_wrappers == result.wrappers
            )
            final = looping or i + 1 == num_iters

            # On failure, restore state based on which stage failed
            if not final or result.failure == "translate":
                # Full restore for next retry or if translation failed (graceful exit)
                self._restore(entry)
            elif result.failure == "wrap":
                # Wrapper restore since they failed but hopefully translation is good
                # FIXME: What if a wrapper is being tested? Seems fatal?
                self._wrapper.restore(entry.wrapper)
            elif result.failure == "review":
                # Keep wrappers and translation even though the oracle rejected them
                pass

            if looping:
                logger.error(
                    f"Failed to translate symbol(s) {name} due to translation loop ({i + 1}/{num_iters})!"
                )
                break

            # Translation differs so allow another retry but log an error
            logger.error(f"Failed to translate symbol(s) {name} ({i + 1}/{num_iters})!")

            # Create feedback for next iteration
            prior_translation = result.translation
            prior_wrappers = result.wrappers
            translation_feedback = result.translation_feedback
            wrap_feedback = result.wrap_feedback

        # Rewind to the best attempt that regressed the least. A later attempt that never ran
        # does not invalidate an earlier one that did, so this also rescues a group whose final
        # try failed to translate.
        assert result is not None  # num_iters is at least 1
        if best is not None and best[2] is not result:
            score, state, result = best
            self._restore(state)
            logger.warning(
                f"Restored the attempt at {name} that regressed the fewest test(s): {score}"
            )
        return result

    def _translate_and_wrap(
        self,
        crate_code: CodeRust,
        symbols: list[Symbol],
        prior_translation: CodeRust | None = None,
        prior_wrappers: dict[SymbolName, CodeRust] | None = None,
        translation_feedback: Feedback = Feedback(),
        wrap_feedback: dict[SymbolName, Feedback] | None = None,
    ) -> _Result:
        prior_wrappers = prior_wrappers or {}
        wrap_feedback = wrap_feedback or {}

        # Translate snippet and exit early if it fails
        snippet = CodeC.join(symbol.code for symbol in symbols)
        name = " ".join(symbol.name for symbol in symbols)
        # Only an SCC where every symbol has a deterministic translation can use it
        known = [t for s in symbols if (t := s.deterministic_translation) is not None]
        deterministic_translation = CodeRust.join(known) if len(known) == len(symbols) else None
        translation_result = self._translator.translate(
            name=name,
            crate_code=crate_code,
            snippet=snippet,
            prior_translation=prior_translation,
            feedback=translation_feedback,
            deterministic_translation=deterministic_translation,
        )
        if not translation_result.success:
            return _Result(translation_result=translation_result)

        out = _Result(translation_result=translation_result)
        out.symbol_results = self._wrapper.wrap(
            symbols, snippet, out.translation, prior_wrappers, wrap_feedback
        )

        # Cache successful translation and wrappers
        if out.success:
            self._translator.write_cache(out.translation_result)
            for symbol_result in out.symbol_results.values():
                self._wrapper.write_cache(symbol_result)
        return out

    def _summary(self, usage: LMUsage) -> str:
        return " ".join(filter(None, (model.format_usage(usage), self._oracle.counts())))

    def _snapshot(self) -> _Snapshot:
        return _Snapshot(
            translator=self._translator.snapshot(),
            wrapper=self._wrapper.snapshot(),
        )

    def _restore(self, state: _Snapshot):
        self._wrapper.restore(state.wrapper)
        self._translator.restore(state.translator)
