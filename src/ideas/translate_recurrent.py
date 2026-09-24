#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import shutil
import logging
from pathlib import Path
from typing import Literal
from contextlib import contextmanager
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace

import dspy
import networkx as nx

from . import model
from .ast_rust import BindgenName, CodeRust
from .ast_rust import mangle
from .refine import CodeAttempt, Feedback
from .hybrid import HybridSnapshot, HybridWriter
from .oracle import Candidate, NullOracle, SymbolOracle, Verdict
from .tools import Crate
from .translate_context import TranslateContext, TranslationContext, WrapContext
from .ast import Symbol, SymbolName, SymbolGroup
from .ast import CodeC, TreeResult, create_symbol_ordering_key_fn
from .wrapper import generate_unimplemented_type_wrapper
from .wrapper import generate_unimplemented_variable_wrapper
from .wrapper import WrapperGenerator, scope_errors, WrapperAttempt
from .translate_snippet import SnippetTranslator, TranslationAttempt

logger = logging.getLogger("ideas.translate_recurrent")
snippet_logger = logging.getLogger("ideas.translate_snippet")
wrapper_logger = logging.getLogger("ideas.wrapper")


@dataclass
class _State:
    rust_lib_src: bytes
    rust_main_src: bytes | None
    hybrid: HybridSnapshot


@dataclass(frozen=True)
class _WrapperPlan:
    unimplemented_wrapper: CodeRust
    extra_errors: Callable[[CodeRust], list[str]] | None = None
    tests_mod: str | None = None
    extern_symbol: str | None = None

    def errors(self, wrapper: CodeRust) -> list[str]:
        errors = scope_errors(wrapper, self.unimplemented_wrapper)
        if self.extra_errors is not None:
            errors += self.extra_errors(wrapper)
        return errors


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
class _SymbolResult:
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
class _Result:
    translation_result: _TranslationResult
    symbol_results: dict[SymbolName, _SymbolResult] = field(default_factory=dict)

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


class RecurrentTranslator(dspy.Module):
    def __init__(
        self,
        sys_crate: Crate,
        crate: Crate,
        rs_crate: Crate,
        symbol_translator: SnippetTranslator,
        symbol_wrapper: WrapperGenerator | None,
        symbol_oracle: SymbolOracle | None = None,
        max_iters: int = 1,
    ):
        super().__init__()
        self.max_iters = max_iters
        self._translator = symbol_translator
        self._wrapper = symbol_wrapper
        self._oracle: SymbolOracle = (
            symbol_oracle if symbol_oracle is not None else NullOracle()
        )

        self._init_rust_crate(rs_crate)
        self._init_hybrid_crate(sys_crate, crate)

    def _init_rust_crate(self, rs_crate: Crate):
        if rs_crate.lib_src_path is None:
            raise ValueError("Expected lib.rs to exist in -rs crate!")
        if rs_crate.lib_name is None:
            raise ValueError("Expected a library target in the -rs crate!")
        self.rust_crate = rs_crate
        rs_crate.lib_src_path.write_text("#![forbid(unsafe_code)]\n\n")
        rs_crate.vcs.add(rs_crate.lib_src_path)
        if rs_crate.main_src_path is not None:
            rs_crate.main_src_path.write_text(
                '#![forbid(unsafe_code)]\n\nfn main() { println!("main not yet translated"); }\n'
            )
            rs_crate.vcs.add(rs_crate.main_src_path)

    def _init_hybrid_crate(self, sys_crate: Crate, crate: Crate):
        assert self.rust_crate.lib_name is not None
        self.crate = crate
        self._hybrid = HybridWriter(sys_crate, crate, self.rust_crate.lib_name)

    def _cargo_build(self) -> str:
        builds, feedback = self.crate.cargo_build()
        if not builds:
            return f"Running `cargo build` fails!\n{feedback}"
        return self._oracle.build()

    def strand_c(self):
        self._hybrid.strand_c()

    def _cargo_test(self, name: str, *, lib: bool = False) -> str:
        passes, _, error, _ = self.crate.cargo_test(name, lib=lib)
        # Feedback is empty when the tests pass, so callers can treat it as the failure signal
        return "" if passes else f"Running `cargo test {name}` fails!\n{error}"

    def _test_counts(self) -> str:
        return (
            f"regressed={len(self._oracle.regressions)} "
            f"total={len(self._oracle.expected_tests)} "
            f"baseline_failures={len(self._oracle.baseline_failures)}"
        )

    def forward(
        self,
        symbols: dict[SymbolName, Symbol],
        dependencies: dict[SymbolGroup, Iterable[SymbolGroup]],
        ast_order: dict[Path, TreeResult] | None = None,
    ) -> dspy.Prediction:
        # Tests that already fail against the untranslated C never count against a translation
        self._oracle.baseline()

        # Write types to the hybrid crate so symbol wrappers can reference them
        self._hybrid.write_types(symbols.values())

        # Write variables first so any generated code sees statics it cannot shadow (E0530)
        self._hybrid.add_bindings(symbols.values())

        if feedback := self._cargo_build():
            raise RuntimeError(feedback)

        msg = f"Initialized translation of `{self.crate.name}` ({len(symbols)} symbols)"
        logger.info(msg)
        if self._oracle.baseline_failures:
            excluded = (
                "Excluded test(s) that already fail against the untranslated C: "
                f"{', '.join(sorted(self._oracle.baseline_failures))}"
            )
            logger.warning(excluded)
            msg += f"\n\n# Excluded Tests\n{excluded}"
        if missing := _undeclared_types(self._hybrid.types, symbols.values()):
            undeclared = (
                f"bindgen did not declare wrappable type(s): {', '.join(missing)}. "
                "Wrapping them will fail to build."
            )
            logger.warning(undeclared)
            msg += f"\n\n# Undeclared Types\n{undeclared}"
        self.crate.vcs.commit(msg)

        # Process symbols in topological order
        G = nx.from_dict_of_lists(dependencies, create_using=nx.DiGraph)
        assert isinstance(G, nx.DiGraph)  # create_using guarantees this
        groups = list(
            nx.lexicographical_topological_sort(
                G.reverse(copy=False), key=create_symbol_ordering_key_fn(symbols, ast_order)
            )
        )
        logger.debug(f"Symbol group order: {[' '.join(g) for g in groups]}")

        snippets: dict[CodeC, SymbolGroup] = {}
        translations: dict[SymbolGroup, CodeRust] = {}
        wrappers: dict[SymbolName, CodeRust] = {}
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

            ctx = TranslationContext.build(G, group, groups, symbols, translations, wrappers)

            # Translate and wrap snippet, saving it if it tests
            with model.track_lm_usage() as usage:
                group_result = self._translate_and_wrap_with_retries(
                    context=ctx, symbols=[symbols[name] for name in group]
                )
            if group_result.failure == "translate":
                # Translate failures (as opposed to wrap/review failures) are fatal
                logger.error(
                    f"Failed to translate symbol group `{' '.join(group)}` [{i}/{count}] "
                    f"to Rust: {model.format_lm_usage(usage)} {self._test_counts()}"
                )
                break
            translations[group] = group_result.translation
            wrappers.update(group_result.wrappers)

            # Record the culprit so no later group is blamed for the same test
            if regressed := group_result.regressed:
                self._oracle.blame(group, regressed)
                logger.warning(
                    f"Test(s) regressed by symbol group `{' '.join(group)}`: "
                    f"{', '.join(sorted(regressed))}"
                )

            logger.info(
                f"Translated symbol group `{' '.join(group)}` [{i}/{count}] to Rust: "
                f"{model.format_lm_usage(usage)} {self._test_counts()}"
            )

        pred = dspy.Prediction(
            complete=len(translations) == len(groups),
            regressions=self._oracle.regressions,
        )
        return pred

    def _translate_and_wrap_with_retries(
        self,
        context: TranslationContext,
        symbols: list[Symbol],
        prior_translation: CodeRust | None = None,
        prior_wrappers: dict[SymbolName, CodeRust] | None = None,
        translation_feedback: Feedback = Feedback(),
        wrap_feedback: dict[SymbolName, Feedback] | None = None,
    ) -> _Result:
        name = " ".join([f"`{s.name}`" for s in symbols])
        num_iters = max(self.max_iters, 1)
        # The last state the oracle accepted; no outcome of this call may land below it
        entry = self._snapshot()
        best: tuple[int, _State, _Result] | None = None
        result: _Result | None = None

        for i in range(num_iters):
            # Attempt translation and exit early on success
            result = self._translate_and_wrap(
                context,
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
                self._restore(entry, hybrid_only=True)
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

    def _snapshot(self) -> _State:
        def read(path: Path | None) -> bytes | None:
            return path.read_bytes() if path else None

        assert self.rust_crate.lib_src_path is not None
        return _State(
            rust_lib_src=self.rust_crate.lib_src_path.read_bytes(),
            rust_main_src=read(self.rust_crate.main_src_path),
            hybrid=self._hybrid.snapshot(),
        )

    def _restore(self, state: _State, *, hybrid_only: bool = False):
        self._hybrid.restore(state.hybrid)
        if hybrid_only:
            return

        def write(path: Path | None, src: bytes | None):
            if path is not None and src is not None:
                path.write_bytes(src)
                self.rust_crate.vcs.add(path)

        assert self.rust_crate.lib_src_path is not None
        write(self.rust_crate.lib_src_path, state.rust_lib_src)
        write(self.rust_crate.main_src_path, state.rust_main_src)

    @contextmanager
    def _checkpoint(self, *, hybrid_only: bool = False) -> Iterator[_State]:
        # Restored unconditionally, and the yielded state outlives the block so a caller can
        # return to it again later
        state = self._snapshot()
        try:
            yield state
        finally:
            self._restore(state, hybrid_only=hybrid_only)

    def _translate_and_wrap(
        self,
        context: TranslationContext,
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
        translation_result = self._translate_snippet(
            name=name,
            context=context.translate,
            snippet=snippet,
            prior_translation=prior_translation,
            feedback=translation_feedback,
        )
        if not translation_result.success:
            return _Result(translation_result=translation_result)

        out = _Result(translation_result=translation_result)

        # Generate wrapper for each symbol
        for symbol in symbols:
            symbol_result = self._wrap_symbol(
                symbol=symbol,
                context=context.wrap,
                snippet=snippet,
                translation=out.translation,
                prior_wrapper=prior_wrappers.get(symbol.name),
                feedback=wrap_feedback.get(symbol.name, Feedback()),
            )
            if symbol_result is None:
                continue
            # Judged whether or not the wrapper stage succeeded: a failed wrap is a rejection
            # the oracle words, and a successful one is what the tests then run against
            candidate = Candidate(
                symbol=symbol,
                snippet=snippet,
                translation=out.translation,
                wrapper=symbol_result.wrapper,
                wrap_rejection=symbol_result.rejection,
                wrapped=symbol_result.wrapped,
            )
            symbol_result = replace(symbol_result, verdict=self._judge_symbol(candidate))
            out.symbol_results[symbol.name] = symbol_result
            if not symbol_result.success:
                break

        # Cache successful translation and wrappers
        if out.success:
            self._translator.write_cache(out.translation_result.attempt)
            for symbol_result in out.symbol_results.values():
                if symbol_result.attempt is not None and self._wrapper is not None:
                    self._wrapper.write_cache(symbol_result.attempt)
        return out

    def _translate_snippet(
        self,
        name: str,
        context: TranslateContext,
        snippet: CodeC,
        prior_translation: CodeRust | None = None,
        feedback: Feedback = Feedback(),
    ) -> _TranslationResult:
        assert self.rust_crate.lib_src_path is not None
        lib_src_path = self.rust_crate.lib_src_path
        main_src_path = self.rust_crate.main_src_path
        base_lib_src = CodeRust(lib_src_path.read_text())
        base_main_src = main_src_path.read_text() if main_src_path is not None else None

        def build(code: CodeRust) -> str:
            lib_src_path.write_text(str(base_lib_src + code))
            self.rust_crate.vcs.add(lib_src_path)
            builds, error = self.rust_crate.cargo_build()
            return "" if builds else f"Running `cargo build` fails!\n{error}"

        # Import the translated `main` from lib.rs so a binary's build feedback covers it
        switch_main = main_src_path is not None and name == "c:@F@main"
        if switch_main:
            assert main_src_path is not None
            main_src_path.write_text(
                f"#![forbid(unsafe_code)]\n\nuse {self.rust_crate.lib_name}::main;\n"
            )
            self.rust_crate.vcs.add(main_src_path)

        snippet_logger.info(f"Translating snippet `{name}` ...")
        with self._translator.session(
            name=name,
            crate_code=context.crate_code,
            reference_code=context.reference_code,
            snippet=snippet,
            dependent_code=context.dependent_code,
            prior_translation=prior_translation,
            feedback=feedback,
        ) as session:
            success = False
            attempt: TranslationAttempt | None = None
            for attempt in session:
                # lib.rs already carries the crate-level attribute, so a second one cannot build
                if CodeRust("#![forbid(unsafe_code)]") in attempt.translation:
                    attempt.reject(
                        scope=["Do not include `#![forbid(unsafe_code)]` in the translation!"]
                    )
                elif error := build(attempt.translation):
                    attempt.reject(build=[error])
                else:
                    attempt.accept()
                success = attempt.success
                _commit(self.rust_crate, attempt)

                # A rejected candidate must not be on disk when the next one is generated
                if not attempt.success:
                    lib_src_path.write_text(str(base_lib_src))

        # Nothing supplies the entrypoint the switch imports, so put the placeholder back
        if switch_main and not success and base_main_src is not None:
            assert main_src_path is not None
            main_src_path.write_text(base_main_src)
        assert attempt is not None  # `_max_iters` is at least 1, so the loop always ran
        return _TranslationResult(attempt)

    def _wrap_symbol(
        self,
        symbol: Symbol,
        context: WrapContext,
        snippet: CodeC,
        translation: CodeRust,
        prior_wrapper: CodeRust | None,
        feedback: Feedback = Feedback(),
    ) -> _SymbolResult | None:
        is_bin = self.crate.main_src_path is not None

        result = None
        if is_bin and symbol.spelling == "main":
            # Rust has to own the entrypoint or its `main` collides with C's at link time
            result = self._wrap_main()
        elif is_bin and symbol.is_function and not self._oracle.exercises(symbol):
            # As a policy, an untested binary gets no function wrappers other than `main`
            pass
        elif symbol.is_type and symbol.is_definition:
            result = self._wrap_type(
                symbol, context, snippet, translation, prior_wrapper, feedback
            )
        elif symbol.is_variable:
            result = self._wrap_variable(
                symbol, context, snippet, translation, prior_wrapper, feedback
            )
        elif symbol.is_function and symbol.is_definition:
            result = self._wrap_function(
                symbol, context, snippet, translation, prior_wrapper, feedback
            )
        if result is None:
            logger.debug("Skipped wrapping of symbol `%s`", symbol.name)
            return None
        return result

    def _wrap_main(self) -> _SymbolResult:
        logger.info("Wrapping function `main` ...")

        # The bin's entrypoint now comes from the -rs crate instead of the C object
        self._hybrid.take_over_main()

        # Fail fast after entrypoint/linkage edits so linker or compile regressions
        # are caught before continuing with additional wrapper work.
        if feedback := self._cargo_build():
            raise RuntimeError(feedback)
        self.crate.vcs.commit("Wrapped function `main`")
        return _SymbolResult()

    def _wrap_variable(
        self,
        symbol: Symbol,
        context: WrapContext,
        snippet: CodeC,
        translation: CodeRust,
        prior_wrapper: CodeRust | None,
        feedback: Feedback = Feedback(),
    ) -> _SymbolResult | None:
        if self._wrapper is None:
            return _SymbolResult()

        # Generate a test asserting that the translated Rust global matches what the C
        # compiler initialized
        unimplemented_wrapper, tests_mod, sync_fns = generate_unimplemented_variable_wrapper(
            symbol.spelling, self._hybrid.types_code, self._hybrid.bindings[symbol.name]
        )

        rust_lib_name = self.rust_crate.lib_name
        assert rust_lib_name is not None  # _init_rust_crate rejects a crate without one
        c_global = f"__c_globals::{mangle(symbol.spelling)}"

        def static_errors(wrapper: CodeRust) -> list[str]:
            # Report every static violation at once so one round of feedback fixes them all
            src = str(wrapper)
            errors: list[str] = []

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
                        f"`initial_value_matches` must read the translated global as "
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
                    f"Keep all of {names} or none of them, spelled exactly as the template "
                    "spells them."
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
                        f"`round_trip_nontrivial` must write the C global "
                        f"`{c_global}` and assert on it afterwards. Spell "
                        "that path out in the test body rather than importing it or reaching "
                        "it through a helper."
                    )
                if rust_lib_name not in body:
                    errors.append(
                        f"`round_trip_nontrivial` must read the translated global as "
                        f"`{rust_lib_name}::ITEM` after `{sync_fns[0]}`. Without that "
                        "assertion the value never leaves the C global, and two "
                        "empty-bodied synchronization functions pass the test. Spell that "
                        "path out in the test body rather than importing it or reading it "
                        "through a helper."
                    )

            if "todo!()" in src:
                errors.append(
                    "The `todo!()` placeholder must be replaced with a real implementation."
                )

            return errors

        return self._run_wrapper_session(
            symbol,
            context,
            snippet,
            translation,
            prior_wrapper,
            feedback,
            _WrapperPlan(unimplemented_wrapper, static_errors, tests_mod=tests_mod),
        )

    def _wrap_type(
        self,
        symbol: Symbol,
        context: WrapContext,
        snippet: CodeC,
        translation: CodeRust,
        prior_wrapper: CodeRust | None,
        feedback: Feedback = Feedback(),
    ) -> _SymbolResult | None:
        if self._wrapper is None:
            return None
        # Only externally visible structs are wrappable: other type kinds have no meaningful
        # field-by-field `to_rust`/`sync_to_c` pair, and a struct the rest of the program
        # cannot name has nothing to bridge.
        if not (symbol.is_struct and symbol.is_externally_visible):
            return None
        # An anonymous record, or one nested in one, has no predictable bindgen name to implement on
        if (bindgen_name := symbol.bindgen_name) is None:
            logger.warning(
                f"Cannot wrap type `{symbol.name}`: it is or is nested in an anonymous "
                "record, which bindgen names with a run-wide counter"
            )
            return None

        # Generate an unimplemented wrapper so the hybrid crate can build and the wrapper
        # generator has something to iteratively implement.
        unimplemented_wrapper, tests_mod = generate_unimplemented_type_wrapper(
            bindgen_name, self._hybrid.types_code
        )

        def static_errors(wrapper: CodeRust) -> list[str]:
            # Report every static violation at once so one round of feedback fixes them all
            src = str(wrapper)
            errors: list[str] = []

            # Enforce that both required round-trip test functions are present
            missing = [
                fn
                for fn in ("round_trip_zeroed", "round_trip_nontrivial")
                if f"fn {fn}" not in src
            ]
            if missing:
                names = ", ".join(f"`{fn}`" for fn in missing)
                errors.append(
                    f"Required test functions are missing: {names}. Both "
                    "`round_trip_zeroed` and `round_trip_nontrivial` must be implemented."
                )

            # A dropped impl header or a surviving `()` placeholder still compiles, so
            # neither cargo build nor cargo test would catch them
            impl_header = f"impl CInterop for {bindgen_name}"
            if impl_header not in src:
                errors.append(f"The wrapper must contain an `{impl_header}` block.")
            if "type Rust = ()" in src:
                errors.append(
                    "`type Rust = ()` is still the placeholder. Replace it with "
                    f"`type Rust = {self.rust_crate.lib_name}::<TranslatedType>;`, where "
                    f"`<TranslatedType>` is the actual name the C type `{symbol.spelling}` was "
                    "translated to in `wrapped_crate_code` (it may have been renamed)."
                )

            # A renamed test module makes the filter below match nothing, which reads as a pass
            if f"mod {tests_mod}" not in src:
                errors.append(f"The test module must stay named `{tests_mod}`.")

            return errors

        return self._run_wrapper_session(
            symbol,
            context,
            snippet,
            translation,
            prior_wrapper,
            feedback,
            _WrapperPlan(unimplemented_wrapper, static_errors, tests_mod=tests_mod),
        )

    def _wrap_function(
        self,
        symbol: Symbol,
        context: WrapContext,
        snippet: CodeC,
        translation: CodeRust,
        prior_wrapper: CodeRust | None,
        feedback: Feedback = Feedback(),
    ) -> _SymbolResult | None:
        if self._wrapper is None:
            return None
        # No point in wrapping non-globals if the oracle does not exercise it
        if not symbol.is_externally_visible and not self._oracle.exercises(symbol):
            return None

        # Generate an unimplemented wrapper so the hybrid crate can build and the wrapper
        # generator has something to iteratively implement.
        unimplemented_wrapper = self._hybrid.unimplemented_function_wrapper(symbol.spelling)
        if unimplemented_wrapper is None:
            logger.warning(f"Skipping wrap of function symbol `{symbol.name}`")
            return _SymbolResult()

        return self._run_wrapper_session(
            symbol,
            context,
            snippet,
            translation,
            prior_wrapper,
            feedback,
            _WrapperPlan(unimplemented_wrapper, extern_symbol=symbol.spelling),
        )

    def _run_wrapper_session(
        self,
        symbol: Symbol,
        context: WrapContext,
        snippet: CodeC,
        translation: CodeRust,
        prior_wrapper: CodeRust | None,
        feedback: Feedback,
        plan: _WrapperPlan,
    ) -> _SymbolResult:
        assert self._wrapper is not None  # every caller checks before building a plan
        rust_lib_name = self.rust_crate.lib_name
        assert rust_lib_name is not None  # _init_rust_crate rejects a crate without one

        def build(wrapper: CodeRust) -> str:
            self._hybrid.set_wrapper(symbol.name, wrapper)
            if plan.extern_symbol is not None:
                self._hybrid.make_extern(plan.extern_symbol)
            return self._cargo_build()

        # Fail fast after wrapper/linkage edits so compile or linker regressions are caught
        # before iterative wrapper generation starts
        with self._checkpoint(hybrid_only=True) as state:
            if error := build(plan.unimplemented_wrapper):
                raise RuntimeError(error)

        wrapper_logger.info(f"Generating wrapper for `{symbol.name}` ...")
        with self._wrapper.session(
            symbol=symbol,
            wrapped_crate_code=context.wrapped_crate_code,
            translation=translation,
            unimplemented_wrapper=plan.unimplemented_wrapper,
            wrapped_crate=rust_lib_name,
            other_wrappers=self._hybrid.render(
                context.wrappers.values(),
                *self._hybrid.type_slice(symbol.bindgen_name, plan.unimplemented_wrapper),
            ),
            prior_wrapper=prior_wrapper,
            feedback=feedback,
            support_code=context.support_code(snippet),
        ) as session:
            attempt: WrapperAttempt | None = None
            for attempt in session:
                if errors := plan.errors(attempt.wrapper):
                    attempt.reject(scope=errors)
                # Build first so a compile error is not reported as a test failure
                elif error := build(attempt.wrapper):
                    attempt.reject(build=[error])
                elif plan.tests_mod is not None and (
                    error := self._cargo_test(plan.tests_mod, lib=True)
                ):
                    attempt.reject(build=[error])
                else:
                    attempt.accept()
                _commit(self.crate, attempt)

                # A rejected candidate must not be on disk when the next one is generated
                if not attempt.success:
                    self._restore(state, hybrid_only=True)
        return _SymbolResult(attempt=attempt)

    def _judge_symbol(self, candidate: Candidate) -> Verdict:
        # Only a hybrid that builds can be tested, and only an exercised symbol will be
        if candidate.wrapped and self._oracle.exercises(candidate.symbol):
            if error := self._cargo_build():
                raise RuntimeError(error)

        verdict = self._oracle.judge(candidate)
        # A wrap rejection leaves no message: the session already committed every attempt
        if verdict.message:
            self.crate.vcs.commit(f"{verdict.message}\n\n{verdict.evidence}".strip())
        return verdict


def _undeclared_types(
    types: Mapping[BindgenName, CodeRust], symbols: Iterable[Symbol]
) -> list[BindgenName]:
    # bindgen renames nested records after their whole ancestor chain, so a wrong
    # `bindgen_name` silently yields a type slice that never declares the item the
    # wrapper will `impl CInterop for`.
    return sorted(
        {
            name
            for s in symbols
            if s.is_struct and s.is_externally_visible
            if (name := s.bindgen_name) is not None and name not in types
        }
    )


def _commit(crate: Crate, attempt: CodeAttempt):
    # The attempt's own module is what names the stage being committed
    log = logging.getLogger(type(attempt).__module__)
    (log.info if attempt.success else log.error)(attempt.headline)

    # Cleared unconditionally so the commit never carries the previous attempt's prompt
    prompt_dir = crate.cargo_toml.parent / ".prompt"
    crate.vcs.rm(prompt_dir, force=True)
    shutil.rmtree(prompt_dir, ignore_errors=True)
    if prompt_files := attempt.pred.get("prompt_files"):
        prompt_dir.mkdir(parents=True, exist_ok=True)
        for name, text in prompt_files.items():
            (prompt_dir / name).write_text(text, encoding="utf-8")
        crate.vcs.add(prompt_dir)

    crate.vcs.commit(attempt.message)


def _test_body(wrapper: str, name: str) -> str:
    # Crude slice of one `#[test]` function's source, used to check what a single test calls
    # without paying for a full parse.
    start = wrapper.find(f"fn {name}")
    if start == -1:
        return ""
    body = wrapper[start:]
    end = body.find("#[test]")
    return body if end == -1 else body[:end]
