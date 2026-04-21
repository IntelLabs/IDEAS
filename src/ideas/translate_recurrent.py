#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
from pathlib import Path
from difflib import unified_diff
from collections.abc import Iterable

import dspy
import networkx as nx

from .ast import Symbol
from .tools import Crate, STATIC_TRANSLATIONS

logger = logging.getLogger("ideas.translate_recurrent")


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

    def forward(
        self,
        symbols: dict[str, Symbol],
        dependencies: dict[tuple[str, ...], Iterable[tuple[str, ...]]],
    ) -> dspy.Prediction:
        G = nx.from_dict_of_lists(dependencies, create_using=nx.DiGraph)
        assert isinstance(G, nx.DiGraph)

        # FIXME: This is from SnippetTranslator
        self.crate.rust_src_path.write_text("use std::sync::{Mutex, MutexGuard};\n\n")

        # Translate symbols in topological order
        snippets: dict[str, tuple[str, ...]] = {}
        translations: dict[tuple[str, ...], str] = {}
        sorted_symbol_names = list(reversed(list(nx.topological_sort(G))))
        symbol_names_with_variable = list(
            filter(
                lambda symbol_names: any(symbols[n].is_variable for n in symbol_names),
                sorted_symbol_names,
            )
        )
        symbols_count = len(sorted_symbol_names)
        for i, symbol_names in enumerate(sorted_symbol_names, start=1):
            logger.info(
                f"Translating symbol group `{' '.join(symbol_names)}` ({i}/{symbols_count}) ..."
            )
            # Gather code for each symbol and check if we have already translated such a snippet
            snippet = "\n".join(symbols[name].code.strip() + "\n" for name in symbol_names)
            if snippet in snippets:
                logger.info(
                    f"Skipping translation of `{' '.join(symbol_names)}` because it was already translated by `{' '.join(snippets[snippet])}`..."
                )
                translations[symbol_names] = translations[snippets[snippet]]
                continue
            snippets[snippet] = symbol_names

            # FIXME: We could save context here by only including translations of descendants of the current symbol.
            #        However, one must prompt the LLM to never generate use statements since those could conflict.
            # Use all unique translations as reference code since many symbol names can map to the same translation
            reference_code = "\n".join(
                dict.fromkeys(
                    translations[name] for name in sorted_symbol_names if name in translations
                )
            )

            # Gather dependent code in topological order
            predecessors = list(G.predecessors(symbol_names))
            dependent_code = "\n".join(
                symbols[name].code.strip() + "\n"
                for names in sorted_symbol_names
                if names in predecessors
                for name in names
            )

            # Use static translation for any symbol that a variable depends on
            static_translation = ""
            if STATIC_TRANSLATIONS and any(
                nx.has_path(G, group_with_variable, symbol_names)
                for group_with_variable in symbol_names_with_variable
            ):
                static_translation = "\n".join(
                    symbols[name].static_translation.strip() + "\n"
                    for name in symbol_names
                    if symbols[name].static_translation != ""
                ).strip()
            if static_translation:
                logger.info(f"Using static translation for `{' '.join(symbol_names)}`")

            # Translate snippet and save it if successful
            pred = self.translate_with_retries(
                reference_code=reference_code,
                symbols=[symbols[name] for name in symbol_names],
                dependent_code=dependent_code,
                translation=static_translation,
            )
            if not pred.success:
                break
            translations[symbol_names] = pred.translation.code.strip() + "\n"

        # Re-assemble translation in order
        translation = "use std::sync::{Mutex, MutexGuard};\n\n"
        translation += "\n".join(
            dict.fromkeys(
                translations[name] for name in sorted_symbol_names if name in translations
            )
        )
        if not self.crate.is_bin:
            translation += "\npub mod wrapper;\n"
        pred = dspy.Prediction(
            translation=translation, success=len(translations) == len(sorted_symbol_names)
        )
        return pred

    def translate_with_retries(
        self,
        reference_code: str,
        symbols: list[Symbol],
        dependent_code: str,
        translation: str = "",
    ) -> dspy.Prediction:
        prior_translation, feedback = "", ""
        pred = dspy.Prediction()
        for i in range(max(self.max_iters, 1)):
            # Save these in case translation fails
            orig_c_src = self.crate.c_src_path.read_bytes()
            orig_rust_src = self.crate.rust_src_path.read_bytes()
            orig_wrappers_src = self._snapshot_wrappers()

            # Attempt translation and exit early on success
            pred = self.translate(
                reference_code,
                symbols,
                dependent_code,
                prior_translation=prior_translation,
                feedback=feedback,
                translation=translation if i == 0 else "",
            )
            if pred.success:
                break

            # Restore to original state since translation failed
            self.crate.c_src_path.write_bytes(orig_c_src)
            self.crate.rust_src_path.write_bytes(orig_rust_src)
            self._restore_wrappers(orig_wrappers_src)

            # On failure log a diff against prior translation
            name = " ".join([f"`{s.name}`" for s in symbols])
            msg = f"Failed to translate symbol(s) {name} ({i + 1}/{self.max_iters})!"
            if prior_translation:
                diff = "\n".join(
                    unified_diff(
                        prior_translation.splitlines(),
                        pred.translation.code.splitlines(),
                        lineterm="",
                        fromfile="prior_translation",
                        tofile="current_translation",
                    )
                )
                if "reasoning" in pred:
                    msg += f"\n# Reason\n{pred.reasoning.strip()}\n"
                msg += f"\n# Translation Diff\n{diff.strip()}\n"
            logger.error(msg)

            # Create feedback for next iteration
            prior_translation = pred.translation.code
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
        reference_code: str,
        symbols: list[Symbol],
        dependent_code: str,
        prior_translation: str = "",
        feedback: str = "",
        translation: str = "",
    ) -> dspy.Prediction:
        # Translate symbols and save it if successful
        pred = self.translate_symbol(
            name=" ".join(symbol.name for symbol in symbols),
            reference_code=reference_code,
            snippet="\n".join(symbol.code.strip() + "\n" for symbol in symbols),
            dependent_code=dependent_code,
            prior_translation=prior_translation,
            feedback=feedback,
            translation=translation,
        )
        if not pred.success:
            return pred

        # Write translation to crate
        translation = pred.translation.code.strip() + "\n"
        with self.crate.rust_src_path.open("a") as f:
            f.write(translation + "\n")

        # Generate wrapper, that may modify the translation, for each symbol
        unsafe_translation = pred.translation.code
        wrappers: list[dspy.Prediction] = []
        for symbol in symbols:
            # We can only hybrid build-test functions and variables
            if not (symbol.is_function and symbol.is_definition) and not symbol.is_variable:
                continue
            # If we can't test symbols, then only wrap globals
            if self.test_symbol is None and not symbol.is_global:
                continue

            # Wrap function or annotate variable
            wrapper = self.wrap_symbol(symbol, reference_code, unsafe_translation)
            unsafe_translation = wrapper.translation

            # Only functions needs to be cached since an LLM does not operate on variables
            if symbol.is_function and symbol.is_definition:
                wrappers.append(wrapper)

            # If wrapping failed exit early
            if not wrapper.success:
                pred.success = False
                pred.feedback = wrapper.feedback
                break

            # Try testing symbol and exit early if it fails
            if not self.test_symbol:
                continue
            test = self.test_symbol(symbol)
            if not test.success:
                pred.success = False
                pred.feedback = test.feedback
                break

        # Cache successful translation and wrappers
        if pred.success:
            self.translate_symbol.write_cache(pred)
            for wrapper in wrappers:
                self.wrap_symbol.write_cache(wrapper)
        return pred
