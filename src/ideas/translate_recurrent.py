#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
from pathlib import Path
from collections.abc import Iterable

import dspy
import networkx as nx

from .ast import CodeC, Symbol, TreeResult
from .ast_rust import CodeRust, get_signatures
from .tools import Crate, LARGE_PROJECT, MAX_DEPENDENT_CHARS
from .init.consolidate import create_symbol_lexical_key_fn

logger = logging.getLogger("ideas.translate_recurrent")

SymbolName = str
SymbolGroup = tuple[SymbolName, ...]


class RecurrentTranslator(dspy.Module):
    def __init__(
        self,
        crate: Crate,
        symbol_translator: dspy.Module,
        symbol_wrapper: dspy.Module,
        symbol_tester: dspy.Module | None = None,
        max_iters: int = 1,
    ):
        super().__init__()
        self.crate = crate
        self.translate_symbol = symbol_translator
        self.wrap_symbol = symbol_wrapper
        self.test_symbol = symbol_tester
        self.max_iters = max_iters
        self._failed_tests: set[str] = set()

    def forward(
        self,
        symbols: dict[SymbolName, Symbol],
        dependencies: dict[SymbolGroup, Iterable[SymbolGroup]],
        ast_order: dict[Path, TreeResult] | None = None,
    ) -> dspy.Prediction:
        # We always start with an empty crate
        self.crate.rust_src_path.write_text("")
        self._failed_tests = set()

        # Translate symbols in topological order
        G = nx.from_dict_of_lists(dependencies, create_using=nx.DiGraph)
        assert isinstance(G, nx.DiGraph)
        groups = list(
            nx.lexicographical_topological_sort(
                G.reverse(copy=False), key=create_symbol_lexical_key_fn(symbols, ast_order)
            )
        )

        snippets: dict[CodeC, SymbolGroup] = {}
        translations: dict[SymbolGroup, CodeRust] = {}
        count = len(groups)
        for i, group in enumerate(groups, start=1):
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

            # FIXME: We could save context here by only including translations of descendants of the current symbol.
            #        However, one must prompt the LLM to never generate use statements since those could conflict.
            # Use all unique (dict.fromkeys) translations as reference code since many symbol names can map to the same translation
            already_translated = nx.descendants(G, group)
            immediate_already_translated = set(G.successors(group))
            immediate_to_be_translated = set(G.predecessors(group))

            reference_code = CodeRust.join(
                dict.fromkeys(translations[g] for g in groups if g in translations)
            )
            reference_context = CodeRust.join(
                dict.fromkeys(
                    translations[g]
                    if g in immediate_already_translated
                    else get_signatures(translations[g])
                    for g in groups
                    if g in translations
                )
            )

            # Gather support code in topological order
            support_code = CodeC.join(
                symbols[name].code
                if g in immediate_already_translated and LARGE_PROJECT
                else CodeC(symbols[name].llm_context_declaration)
                for g in groups
                if g in already_translated
                for name in g
            )

            # Gather dependent code in topological order
            dependent_parts: list[CodeC] = []
            total_chars, exceeded = 0, False
            for g in groups:
                if exceeded:
                    break
                if g in immediate_to_be_translated:
                    for name in g:
                        code = symbols[name].code
                        char_count = len(str(code))
                        if LARGE_PROJECT and total_chars + char_count > MAX_DEPENDENT_CHARS:
                            exceeded = True
                            break
                        dependent_parts.append(code)
                        total_chars += char_count
            dependent_code = CodeC.join(dependent_parts)

            # Translate snippet and save it if successful
            pred = self.translate_with_retries(
                reference_code=reference_code,
                reference_context=reference_context,
                symbols=[symbols[name] for name in group],
                dependent_code=dependent_code,
                support_code=support_code,
            )

            if pred.failure == "translate":
                # Translate failures (as opposed to wrap/test failures) are fatal
                break
            else:
                # Once a test fails, skip it for all future groups in this run
                newly_failed_tests = pred.failed_tests - self._failed_tests
                if newly_failed_tests:
                    self._failed_tests.update(newly_failed_tests)
                    logger.info(
                        "Disabled the following failing tests: %s",
                        ", ".join(sorted(newly_failed_tests)),
                    )
            translations[group] = pred.translation

        # Re-assemble unique (dict.fromkeys) translations in order
        translation = CodeRust.join(
            dict.fromkeys(translations[group] for group in groups if group in translations)
        )
        if not self.crate.is_bin:
            translation += CodeRust("pub mod wrapper;")
        pred = dspy.Prediction(
            translation=translation, success=len(translations) == len(groups)
        )
        return pred

    def translate_with_retries(
        self,
        reference_code: CodeRust,
        reference_context: CodeRust,
        symbols: list[Symbol],
        dependent_code: CodeC,
        support_code: CodeC,
        prior_translation: CodeRust | None = None,
        prior_wrappers: dict[str, CodeRust] | None = None,
        feedback: str = "",
    ) -> dspy.Prediction:
        name = " ".join([f"`{s.name}`" for s in symbols])
        pred = dspy.Prediction()
        num_iters = max(self.max_iters, 1)
        for i in range(num_iters):
            # Save these in case translation fails
            orig_c_src = self.crate.c_src_path.read_bytes()
            orig_rust_src = self.crate.rust_src_path.read_bytes()
            orig_wrappers_src = self._snapshot_wrappers()

            # Attempt translation and exit early on success
            pred = self.translate(
                reference_code,
                reference_context,
                symbols,
                dependent_code,
                support_code,
                prior_translation=prior_translation,
                prior_wrappers=prior_wrappers,
                feedback=feedback,
            )
            if pred.success:
                break

            # If neither translation nor wrappers differ from previous try, then stop retrying
            if (
                prior_translation is not None
                and prior_translation == pred.translation
                and prior_wrappers is not None
                and prior_wrappers == pred.wrappers
            ):
                logger.error(
                    f"Failed to translate symbol(s) {name} due to translation loop ({i + 1}/{num_iters})!"
                )
                break

            # Translation differs so allow another retry but log an error
            logger.error(f"Failed to translate symbol(s) {name} ({i + 1}/{num_iters})!")

            # On failure, restore state based on which stage failed
            if i + 1 < num_iters:
                # Full restore for next retry since we haven't exhausted retries yet
                self.crate.c_src_path.write_bytes(orig_c_src)
                self.crate.rust_src_path.write_bytes(orig_rust_src)
                self._restore_wrappers(orig_wrappers_src)
            elif pred.failure == "translate":
                # Full restore since a failure at this stage is fatal
                self.crate.c_src_path.write_bytes(orig_c_src)
                self.crate.rust_src_path.write_bytes(orig_rust_src)
                self._restore_wrappers(orig_wrappers_src)
            elif pred.failure == "wrap":
                # Wrapper restore since they failed but hopefully translation is good
                # FIXME: What if a wrapper is being tested? Seems fatal?
                self._restore_wrappers(orig_wrappers_src)
            elif pred.failure == "test":
                # Keep wrappers and translation even though they didn't pass tests
                pass

            # Create feedback for next iteration
            prior_translation = pred.translation
            prior_wrappers = pred.wrappers
            feedback = pred.feedback
        return pred

    def _snapshot_wrappers(self) -> dict[Path, bytes]:
        wrappers: dict[Path, bytes] = {}

        path = self.crate.rust_src_path.parent / "wrapper.rs"
        if path.exists() and path.is_file():
            wrappers[path] = path.read_bytes()

        wrapper_dir = self.crate.rust_src_path.parent / "wrapper"
        if wrapper_dir.exists():
            for path in wrapper_dir.glob("*.rs"):
                if path.exists() and path.is_file():
                    wrappers[path] = path.read_bytes()

        return wrappers

    def _restore_wrappers(self, wrappers: dict[Path, bytes]) -> None:
        for path, src in wrappers.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(src)
            self.crate.vcs.add(path)

    def translate(
        self,
        reference_code: CodeRust,
        reference_context: CodeRust,
        symbols: list[Symbol],
        dependent_code: CodeC,
        support_code: CodeC,
        prior_translation: CodeRust | None = None,
        prior_wrappers: dict[str, CodeRust] | None = None,
        feedback: str = "",
    ) -> dspy.Prediction:
        prior_wrappers = prior_wrappers or {}

        # Translate symbols and save it if successful
        snippet = CodeC.join(symbol.code for symbol in symbols)
        pred = self.translate_symbol(
            name=" ".join(symbol.name for symbol in symbols),
            reference_code=reference_code,
            reference_context=reference_context,
            snippet=snippet,
            dependent_code=dependent_code,
            prior_translation=prior_translation,
            feedback=feedback,
        )
        pred.failure = None
        pred.wrappers = {}
        pred.failed_tests = set()
        if not pred.success:
            pred.failure = "translate"
            return pred

        # Write translation to crate
        translation = pred.translation
        with self.crate.rust_src_path.open("a") as f:
            f.write(str(translation) + "\n")

        # Generate wrapper for each symbol
        wrappers: dict[str, dspy.Prediction] = {}
        for symbol in symbols:
            # We can only hybrid build-test functions and variables
            if not (symbol.is_function and symbol.is_definition) and not symbol.is_variable:
                continue
            # If we can't test symbols, then only wrap globals
            if self.test_symbol is None and not symbol.is_global:
                continue

            # Wrap function or annotate variable
            prior_wrapper = prior_wrappers.get(symbol.name, None)
            wrapper = self.wrap_symbol(
                symbol=symbol,
                reference_code=reference_code,
                translation=pred.translation,
                support_code=support_code + snippet + dependent_code,
                prior_wrapper=prior_wrapper,
            )

            # Save function wrappers for next retry and caching
            if symbol.is_function and symbol.is_definition and "wrapper" in wrapper:
                wrappers[symbol.name] = wrapper

            # If wrapping failed exit early
            if not wrapper.success:
                pred.success = False
                pred.failure = "wrap"
                pred.feedback = wrapper.feedback
                break

            # Try testing symbol and exit early if it fails
            test_symbol = self.test_symbol
            if test_symbol is None:
                continue
            test = test_symbol(symbol, skip=sorted(self._failed_tests))
            pred.failed_tests.update(
                name for name, success in test.results.items() if not success
            )
            if not test.success:
                pred.success = False
                pred.failure = "test"
                pred.feedback = test.feedback
                break

        # Cache successful translation and wrappers
        if pred.success:
            self.translate_symbol.write_cache(pred)
            for wrapper in wrappers.values():
                self.wrap_symbol.write_cache(wrapper)

        # Return wrappers for next retry
        pred.wrappers = {name: wrapper.wrapper for name, wrapper in wrappers.items()}
        return pred
