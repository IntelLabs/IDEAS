#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import re
import math
import json
import logging
from pathlib import Path
from dataclasses import dataclass, field
from collections.abc import Iterable
from textwrap import indent
from typing import Literal

import dspy
import networkx as nx

from .ast_rust import CodeRust, strip_fns, mangle
from .tools import Crate, MAX_DEPENDENT_CHARS, REDUCED_CONTEXT
from .ast import Symbol, SymbolName, SymbolGroup
from .ast import CodeC, TreeResult, create_symbol_ordering_key_fn
from .ast import clang_make_global_, clang_make_extern_
from .wrapper import bindgen, generate_unimplemented_function_wrapper
from .wrapper import generate_unimplemented_type_wrapper

logger = logging.getLogger("ideas.translate_recurrent")


WrapperName = str


@dataclass(frozen=True)
class TranslationContext:
    crate_code: CodeRust
    reference_code: CodeRust
    dependent_code: CodeC
    support_code: CodeC
    wrappers: CodeRust

    @classmethod
    def build(
        cls,
        G: nx.DiGraph,
        group: SymbolGroup,
        groups: list[SymbolGroup],
        symbols: dict[SymbolName, Symbol],
        translations: dict[SymbolGroup, CodeRust] | None = None,
        wrappers: dict[WrapperName, CodeRust] | None = None,
    ) -> "TranslationContext":
        if translations is None:
            translations = {}
        if wrappers is None:
            wrappers = {}
        descendants = nx.descendants(G, group)
        already_translated = [g for g in groups if g in descendants]
        immediate_already_translated = set(G.successors(group))
        ancestors = nx.ancestors(G, group)
        ancestor_deps = {succ for a in ancestors for succ in G.successors(a)}
        to_be_translated = [
            g for g in groups if g in ancestors | (ancestor_deps - descendants - {group})
        ]
        reference_groups = [g for g in groups if g in translations]
        hops = nx.single_source_shortest_path_length(G, group)
        return cls(
            crate_code=cls._build_crate_code(reference_groups, translations),
            reference_code=cls._build_reference_code(reference_groups, translations, hops),
            dependent_code=cls._build_dependent_code(to_be_translated, symbols),
            support_code=cls._build_support_code(
                already_translated, immediate_already_translated, symbols
            ),
            wrappers=cls._build_wrapper_context(wrappers),
        )

    @staticmethod
    def _build_crate_code(
        reference_groups: list[SymbolGroup],
        translations: dict[SymbolGroup, CodeRust],
    ) -> CodeRust:
        # Use all unique (dict.fromkeys) translations as the crate's current contents since many symbol names can map to the same translation
        return CodeRust.join(dict.fromkeys(translations[g] for g in reference_groups))

    @staticmethod
    def _build_reference_code(
        reference_groups: list[SymbolGroup],
        translations: dict[SymbolGroup, CodeRust],
        hops: dict[SymbolGroup, int],
    ) -> CodeRust:
        def trim_by_distance(ref_group: SymbolGroup) -> CodeRust:
            match hops.get(ref_group):
                case 1:
                    # 1-hop successors keep full function bodies since they are likely to be directly relevant
                    return translations[ref_group]
                case 2:
                    # 2-hop successors strip top-level function bodies since they are less likely to be directly relevant
                    return strip_fns(translations[ref_group])
                case _:
                    # For distant or unreachable groups delete top-level functions but keep types,
                    # since types may still be needed even when not reachable via static analysis
                    return strip_fns(translations[ref_group], delete=True)

        return CodeRust.join(dict.fromkeys(trim_by_distance(g) for g in reference_groups))

    @staticmethod
    def _build_support_code(
        already_translated: list[SymbolGroup],
        immediate_already_translated: set[SymbolGroup],
        symbols: dict[SymbolName, Symbol],
    ) -> CodeC:
        # Gather support code in topological order.
        # Reduce C support code context by turning non-immediate symbols into declarations. We keep
        # immediate C code in full since they are more likely to be relevant for wrappers.
        return CodeC.join(
            symbols[name].code
            if g in immediate_already_translated
            else CodeC(symbols[name].llm_context_declaration)
            for g in already_translated
            for name in g
        )

    @classmethod
    def _build_dependent_code(
        cls,
        to_be_translated: list[SymbolGroup],
        symbols: dict[SymbolName, Symbol],
        max_chars: int = MAX_DEPENDENT_CHARS,
    ) -> CodeC:
        # Gather dependent C code in topological order.
        dependent_code = CodeC.join(symbols[name].code for g in to_be_translated for name in g)
        if len(str(dependent_code)) > max_chars:
            logger.warning(f"Dependent code exceeds max {len(str(dependent_code))}/{max_chars}")
            dependent_code = cls._select_c_code(to_be_translated, symbols, max_chars)
        return dependent_code

    _MEMORY_PATTERN = re.compile(
        r"\b(malloc|calloc|realloc|free|memcpy|memmove|memset|strdup|strndup|fopen|freopen|fclose)\b"
    )
    _POINTER_PATTERN = re.compile(r"->|\*|&|\[|\bNULL\b|\bsizeof\b")

    @dataclass(frozen=True)
    class _DependentCandidate:
        group: SymbolGroup
        full: CodeC
        full_chars: int
        score: float

    @classmethod
    def _select_c_code(
        cls,
        groups: list[SymbolGroup],
        symbols: dict[SymbolName, Symbol],
        max_chars: int,
    ) -> CodeC:
        candidates = cls._collect_dependent_candidates(groups, symbols)
        chosen: set[SymbolGroup] = set()
        total_chars = 0

        # Select the most informative dependent bodies that fit within the remaining budget.
        for candidate in sorted(
            candidates,
            key=lambda candidate: candidate.score / math.sqrt(max(candidate.full_chars, 1)),
            reverse=True,
        ):
            if total_chars + candidate.full_chars > max_chars:
                continue
            chosen.add(candidate.group)
            total_chars += candidate.full_chars

        return CodeC.join(
            candidate.full for candidate in candidates if candidate.group in chosen
        )

    @classmethod
    def _collect_dependent_candidates(
        cls,
        groups: list[SymbolGroup],
        symbols: dict[SymbolName, Symbol],
    ) -> list:
        candidates = []
        for group in groups:
            full = CodeC.join(symbols[name].code for name in group)
            candidates.append(
                cls._DependentCandidate(
                    group=group,
                    full=full,
                    full_chars=len(str(full)),
                    score=cls._score_dependent_group(group, symbols),
                )
            )
        return candidates

    @classmethod
    def _score_dependent_group(
        cls, group: SymbolGroup, symbols: dict[SymbolName, Symbol]
    ) -> float:
        score = 0.0

        # Favor groups with function definitions
        if any(symbols[name].is_function and symbols[name].is_definition for name in group):
            score += 3.0

        # Favor groups with memory or pointer-related code patterns
        code = "\n".join(str(symbols[name].code) for name in group)
        if cls._MEMORY_PATTERN.search(code):
            score += 3.0
        if cls._POINTER_PATTERN.search(code):
            score += 2.0

        # Favor smaller groups
        return score + 1.0 / math.sqrt(max(len(code), 1))

    @classmethod
    def _build_wrapper_context(cls, wrappers: dict[WrapperName, CodeRust]) -> CodeRust:
        context = CodeRust("")
        for name, wrapper in wrappers.items():
            if "fn c_to_r" in str(wrapper):
                wrapper = cls._build_type_wrapper_context(wrapper)
            elif "pub static mut" in str(wrapper):
                pass
            elif REDUCED_CONTEXT:
                wrapper = None

            if wrapper:
                context += CodeRust(
                    f"pub mod {name} {{\n" + indent(str(wrapper), " " * 4) + "\n}"
                )
        return context

    @classmethod
    def _build_type_wrapper_context(cls, wrapper: CodeRust) -> CodeRust:
        # Strip tests from type wrapper
        wrapper_src = str(wrapper)
        test_idx = wrapper_src.find("#[cfg(test)]")
        if test_idx != -1:
            wrapper_src = wrapper_src[:test_idx].strip()
        wrapper = CodeRust(wrapper_src)

        # Strip functions from wrapper
        wrapper = strip_fns(wrapper)

        return wrapper


@dataclass
class _State:
    c_src: bytes
    rust_lib_src: bytes
    rust_main_src: bytes | None
    hybrid_lib_src: bytes | None
    hybrid_main_src: bytes | None
    wrappers: dict[Path, bytes]


@dataclass
class _TranslationResult:
    pred: dspy.Prediction = field(repr=False, compare=False)

    @property
    def success(self) -> bool:
        return self.pred.success

    @property
    def translation(self) -> CodeRust | None:
        return self.pred.translation if "translation" in self.pred else None

    @property
    def feedback(self) -> str:
        return self.pred.feedback if "feedback" in self.pred else ""

    @classmethod
    def from_pred(cls, pred: dspy.Prediction) -> "_TranslationResult":
        return cls(pred=pred)


@dataclass
class _WrapperResult:
    pred: dspy.Prediction | None = field(default=None, repr=False, compare=False)
    failure: Literal["wrap", "test"] | None = None
    failed_tests: set[str] = field(default_factory=set)

    @property
    def success(self) -> bool:
        return self.failure is None

    @property
    def wrapper(self) -> CodeRust | None:
        return self.pred.wrapper if self.pred is not None and "wrapper" in self.pred else None

    @property
    def feedback(self) -> str:
        if self.failure is None:
            return ""

        if self.failure == "wrap":
            return (
                "It was difficult to generate a C-compatible FFI wrapper for the translation. "
                "Regenerate the translation with clear, explicit, wrapper-friendly Rust function boundaries and straightforward ownership, "
                "while keeping the translation fully memory-safe and free of unsafe constructs."
            )

        return (
            "The current Rust translation in `prior_translation` does not match the behavior of the C `snippet`. "
            "Carefully compare `prior_translation` against the C `snippet` and regenerate the Rust `translation` to match the C behavior exactly. "
            "Do not assume inputs are well-formed: if the tests exercise malformed, invalid, partial, or adversarial input, preserve the C behavior for those cases too, including error returns, boundary handling, or other observable effects. "
            "Make minimal, targeted changes to `prior_translation`, and only modify what is necessary to match the C behavior. "
            "Treat the C `snippet` as the source of truth, even if it contains a bug."
        )

    @classmethod
    def from_pred(cls, pred: dspy.Prediction) -> "_WrapperResult":
        failure: Literal["wrap", "test"] | None = None if pred.success else "wrap"
        return cls(pred=pred, failure=failure)


@dataclass
class _Result:
    translation_result: _TranslationResult
    wrapper_results: dict[WrapperName, _WrapperResult] = field(default_factory=dict)

    @property
    def translation(self) -> CodeRust | None:
        return self.translation_result.translation

    @property
    def failure(self) -> Literal["translate", "wrap", "test"] | None:
        if not self.translation_result.success:
            return "translate"
        for r in self.wrapper_results.values():
            if r.failure is not None:
                return r.failure
        return None

    @property
    def success(self) -> bool:
        return self.failure is None

    @property
    def failed_tests(self) -> set[str]:
        return {t for r in self.wrapper_results.values() for t in r.failed_tests}

    @property
    def feedback(self) -> str:
        if not self.translation_result.success:
            return self.translation_result.feedback
        for r in self.wrapper_results.values():
            if not r.success:
                return r.feedback
        return ""

    @property
    def wrappers(self) -> dict[WrapperName, CodeRust]:
        return {n: r.wrapper for n, r in self.wrapper_results.items() if r.wrapper is not None}


class RecurrentTranslator(dspy.Module):
    def __init__(
        self,
        sys_crate: Crate,
        crate: Crate,
        rs_crate: Crate,
        symbol_translator: dspy.Module,
        symbol_wrapper: dspy.Module | None,
        tests: str | None = None,
        max_iters: int = 1,
    ):
        super().__init__()
        assert sys_crate.lib_src_path is not None
        self.c_src_path = sys_crate.lib_src_path.with_suffix(".c")
        self.crate = crate
        self._translator = symbol_translator
        self._wrapper = symbol_wrapper
        self._tests = tests
        self.max_iters = max_iters
        self._failed_tests: set[str] = set()

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
        if crate.lib_src_path is None and crate.main_src_path is None:
            raise ValueError(f"Crate {crate.name} has neither lib.rs nor main.rs!")

        assert sys_crate.lib_name is not None
        use_stmt = f"use {sys_crate.lib_name} as _;\n"

        if crate.lib_src_path is not None:
            crate.lib_src_path.write_text(use_stmt)
            crate.vcs.add(crate.lib_src_path)
        if crate.main_src_path is not None:
            crate.main_src_path.write_text(f"#![no_main]\n\n{use_stmt}\n\n")
            crate.vcs.add(crate.main_src_path)

    def forward(
        self,
        symbols: dict[SymbolName, Symbol],
        dependencies: dict[SymbolGroup, Iterable[SymbolGroup]],
        ast_order: dict[Path, TreeResult] | None = None,
    ) -> dspy.Prediction:
        self._failed_tests = set()

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
        wrappers: dict[WrapperName, CodeRust] = {}
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

            ctx = TranslationContext.build(G, group, groups, symbols, translations, wrappers)

            # Translate and wrap snippet, saving it if it tests
            group_result = self._translate_and_wrap_with_retries(
                context=ctx, symbols=[symbols[name] for name in group]
            )
            if group_result.failure == "translate":
                # Translate failures (as opposed to wrap/test failures) are fatal
                break
            assert group_result.translation is not None  # group_result.failure != translate
            translations[group] = group_result.translation
            wrappers.update(group_result.wrappers)

            # Once a test fails, skip it for all future groups in this run
            newly_failed_tests = group_result.failed_tests - self._failed_tests
            if newly_failed_tests:
                self._failed_tests.update(newly_failed_tests)
                logger.info(
                    f"Disabled the following failing tests: {', '.join(sorted(newly_failed_tests))}"
                )
        pred = dspy.Prediction(success=len(translations) == len(groups))
        return pred

    def _translate_and_wrap_with_retries(
        self,
        context: TranslationContext,
        symbols: list[Symbol],
        prior_translation: CodeRust | None = None,
        prior_wrappers: dict[WrapperName, CodeRust] | None = None,
        feedback: str = "",
    ) -> _Result:
        name = " ".join([f"`{s.name}`" for s in symbols])
        num_iters = max(self.max_iters, 1)

        for i in range(num_iters):
            state = self._snapshot()

            # Attempt translation and exit early on success
            result = self._translate_and_wrap(
                context,
                symbols,
                prior_translation=prior_translation,
                prior_wrappers=prior_wrappers,
                feedback=feedback,
            )
            if result.success:
                break

            # If neither translation nor wrappers differ from previous try, then stop retrying
            if (
                prior_translation is not None
                and prior_translation == result.translation
                and prior_wrappers is not None
                and prior_wrappers == result.wrappers
            ):
                logger.error(
                    f"Failed to translate symbol(s) {name} due to translation loop ({i + 1}/{num_iters})!"
                )
                break

            # Translation differs so allow another retry but log an error
            logger.error(f"Failed to translate symbol(s) {name} ({i + 1}/{num_iters})!")

            # On failure, restore state based on which stage failed
            if i + 1 < num_iters or result.failure == "translate":
                # Full restore for next retry or if translation failed (graceful exit)
                self._restore(state)
            elif result.failure == "wrap":
                # Wrapper restore since they failed but hopefully translation is good
                # FIXME: What if a wrapper is being tested? Seems fatal?
                self._restore(state, wrappers_only=True)
            elif result.failure == "test":
                # Keep wrappers and translation even though they didn't pass tests
                pass

            # Create feedback for next iteration
            prior_translation = result.translation
            prior_wrappers = result.wrappers
            feedback = result.feedback
        return result  # pyright: ignore[reportPossiblyUnboundVariable] because num_iters is always >= 1

    def _snapshot(self) -> _State:
        def read(path: Path | None) -> bytes | None:
            return path.read_bytes() if path else None

        assert self.rust_crate.lib_src_path is not None
        wrapper_files = self.crate.src_dir.glob("wrap_*.rs")
        return _State(
            c_src=self.c_src_path.read_bytes(),
            rust_lib_src=self.rust_crate.lib_src_path.read_bytes(),
            rust_main_src=read(self.rust_crate.main_src_path),
            hybrid_lib_src=read(self.crate.lib_src_path),
            hybrid_main_src=read(self.crate.main_src_path),
            wrappers={path: path.read_bytes() for path in wrapper_files if path.is_file()},
        )

    def _restore(self, state: _State, *, wrappers_only: bool = False):
        # Wrappers are always restored
        for path, src in state.wrappers.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(src)

        if wrappers_only:
            return

        def write(path: Path | None, src: bytes | None):
            if path is not None and src is not None:
                path.write_bytes(src)

        assert self.rust_crate.lib_src_path is not None
        self.c_src_path.write_bytes(state.c_src)
        self.rust_crate.lib_src_path.write_bytes(state.rust_lib_src)
        write(self.rust_crate.main_src_path, state.rust_main_src)
        write(self.crate.lib_src_path, state.hybrid_lib_src)
        write(self.crate.main_src_path, state.hybrid_main_src)

    def _translate_and_wrap(
        self,
        context: TranslationContext,
        symbols: list[Symbol],
        prior_translation: CodeRust | None = None,
        prior_wrappers: dict[WrapperName, CodeRust] | None = None,
        feedback: str = "",
    ) -> _Result:
        prior_wrappers = prior_wrappers or {}

        # Translate snippet and exit early if it fails
        snippet = CodeC.join(symbol.code for symbol in symbols)
        name = " ".join(symbol.name for symbol in symbols)
        translation_result = self._translate_snippet(
            name=name,
            context=context,
            snippet=snippet,
            prior_translation=prior_translation,
            feedback=feedback,
        )
        if not translation_result.success:
            return _Result(translation_result=translation_result)

        out = _Result(translation_result=translation_result)
        assert out.translation is not None  # translation_result.success == True

        # Generate wrapper for each symbol
        for symbol in symbols:
            wrapper_name: WrapperName = f"wrap_{mangle(symbol.spelling)}"
            wrapper_result = self._wrap_symbol(
                name=wrapper_name,
                symbol=symbol,
                context=context,
                snippet=snippet,
                translation=out.translation,
                prior_wrapper=prior_wrappers.get(wrapper_name),
            )
            if wrapper_result is None:
                continue
            out.wrapper_results[wrapper_name] = wrapper_result
            if not wrapper_result.success:
                break

        # Cache successful translation and wrappers
        if out.success:
            self._translator.write_cache(out.translation_result.pred)
            for wrapper_result in out.wrapper_results.values():
                if wrapper_result.pred is not None and self._wrapper is not None:
                    self._wrapper.write_cache(wrapper_result.pred)
        return out

    def _translate_snippet(
        self,
        name: str,
        context: TranslationContext,
        snippet: CodeC,
        prior_translation: CodeRust | None = None,
        feedback: str = "",
    ) -> _TranslationResult:
        assert self.rust_crate.lib_src_path is not None
        lib_src_path = self.rust_crate.lib_src_path
        base_rust_src = CodeRust(lib_src_path.read_text())

        def build(translation: CodeRust) -> str:
            # Append translation to lib.rs and check if it builds
            lib_src_path.write_text(str(base_rust_src + translation))
            self.rust_crate.vcs.add(lib_src_path)
            # Import translated `main` symbol from lib.rs for binaries to get build feedback
            if self.rust_crate.main_src_path is not None and name == "c:@F@main":
                self.rust_crate.main_src_path.write_text(
                    f"#![forbid(unsafe_code)]\n\nuse {self.rust_crate.lib_name}::*;\n"
                )
                self.rust_crate.vcs.add(self.rust_crate.main_src_path)
            builds, build_feedback = self.rust_crate.cargo_build()
            return "Running `cargo build` fails!\n" + build_feedback if not builds else ""

        def commit(msg: str, pred: dspy.Prediction):
            if "reasoning" in pred and pred.reasoning:
                msg += f"\n\n# Reasoning\n{indent(pred.reasoning, '  ')}"
            if "feedback" in pred and pred.feedback:
                msg += f"\n\n# Feedback\n{indent(pred.feedback, '  ')}"
            self.rust_crate.vcs.commit(msg)

        pred = self._translator(
            name=name,
            crate_code=context.crate_code,
            reference_code=context.reference_code,
            snippet=snippet,
            dependent_code=context.dependent_code,
            prior_translation=prior_translation,
            feedback=feedback,
            feedback_fn=build,
            on_attempt=commit,
        )
        return _TranslationResult.from_pred(pred)

    def _wrap_symbol(
        self,
        name: WrapperName,
        symbol: Symbol,
        context: TranslationContext,
        snippet: CodeC,
        translation: CodeRust,
        prior_wrapper: CodeRust | None,
    ) -> _WrapperResult | None:
        result = None

        # Main function must always wrapped
        if self.crate.main_src_path is not None and symbol.spelling == "main":
            result = self._wrap_main()
        elif symbol.is_type and symbol.is_definition:
            result = self._wrap_type(name, symbol, context, snippet, translation, prior_wrapper)
        elif symbol.is_variable:
            result = self._wrap_variable(name, symbol)
        elif symbol.is_function and symbol.is_definition:
            result = self._wrap_function(
                name, symbol, context, snippet, translation, prior_wrapper
            )
        if result is None:
            return None

        # Don't bother testing if the wrapping failed or no tests
        if not result.success or self._tests is None:
            return result

        # Test the hybrid crate
        passes, results = self._test_symbol(symbol, skip=sorted(self._failed_tests))
        result.failed_tests = {name for name, success in results.items() if not success}
        if not passes:
            result.failure = "test"
        return result

    def _test_symbol(self, symbol: Symbol, skip: list[str]) -> tuple[bool, dict[str, bool]]:
        assert self._tests is not None
        logger.info(f"Testing symbol `{symbol.name}` ...")

        # Make sure the crate builds before testing
        builds, feedback = self.crate.cargo_build()
        if not builds:
            raise RuntimeError(f"Crate does not build!\n{feedback}")

        # Run cargo test
        passes, jsonl, feedback, _ = self.crate.cargo_test(
            self._tests, skip=skip, test_harness="nextest run", message_format="libtest-json"
        )
        results = _extract_test_results(jsonl)
        if passes:
            msg = f"Tested symbol `{symbol.name}`"
            logger.info(msg)
        else:
            feedback = "Running `cargo test` fails!\n" + feedback
            msg = f"Failed to test symbol `{symbol.name}`"
            logger.error(msg)
            msg += f"\n\n{feedback}"
        self.crate.vcs.commit(msg)
        return passes, results

    def _wrap_main(self) -> _WrapperResult:
        logger.info("Generating wrapper for function `main` ...")

        # Declare C `main` as extern so Rust owns the definition and we avoid
        # duplicate entrypoint symbols at link time.
        clang_make_extern_(self.c_src_path, "main")
        self.crate.vcs.add(self.c_src_path)

        # Import the Rust translation crate from each hybrid root so linker-visible
        # symbols stay reachable from the test/build target.
        for root_path in (self.crate.lib_src_path, self.crate.main_src_path):
            if root_path is None:
                continue
            root_path.write_text(f"use {self.rust_crate.lib_name}::*;\n")
            self.crate.vcs.add(root_path)

        # Fail fast after entrypoint/linkage edits so linker or compile regressions
        # are caught before continuing with additional wrapper work.
        success, output = self.crate.cargo_build()
        if not success:
            raise RuntimeError(f"Failed to build crate!\n{output}")
        self.crate.vcs.commit("Wrapped function `main`")
        return _WrapperResult()

    def _wrap_variable(self, name: WrapperName, symbol: Symbol) -> _WrapperResult | None:
        # No point in wrapping non-globals if no tests
        if self._tests is None and not symbol.is_global:
            return None

        # Emit a Rust module containing FFI variable bindings and persist it
        # under src/ so crate-root pub mod declarations can include it.
        var_wrapper = bindgen(self.c_src_path, symbol.spelling)
        wrapper_path = self.crate.src_dir / f"{name}.rs"
        wrapper_path.parent.mkdir(exist_ok=True, parents=True)
        wrapper_path.write_text(str(var_wrapper))
        self.crate.vcs.add(wrapper_path)

        # Ensure the C variable has external linkage so the wrapper can resolve
        # the symbol at link time and access the same storage across crates.
        clang_make_global_(self.c_src_path, symbol.spelling)
        self.crate.vcs.add(self.c_src_path)

        # Register the wrapper module in each crate root so tests and callers can
        # resolve it by path and rustc includes it in the build graph.
        for root_path in (self.crate.lib_src_path, self.crate.main_src_path):
            if root_path is None:
                continue
            with root_path.open("a") as f:
                f.write(f"pub mod {name};\n")
            self.crate.vcs.add(root_path)

        # Fail fast after wrapper/linkage edits so linker or compile regressions
        # are caught before continuing with additional wrapper work.
        success, output = self.crate.cargo_build()
        if not success:
            raise RuntimeError(f"Failed to build crate!\n{output}")

        msg = f"Wrapped variable `{symbol.name}`"
        logger.info(msg)
        self.crate.vcs.commit(msg)
        return _WrapperResult(pred=dspy.Prediction(wrapper=var_wrapper, success=True))

    def _wrap_type(
        self,
        name: WrapperName,
        symbol: Symbol,
        context: TranslationContext,
        snippet: CodeC,
        translation: CodeRust,
        prior_wrapper: CodeRust | None,
    ) -> _WrapperResult | None:
        if self._wrapper is None:
            return None
        # Only struct declarations with linkage are wrappable: other type kinds have no
        # meaningful field-by-field `c_to_r`/`r_to_c` pair, and structs without linkage
        # are unnameable.
        if not (symbol.is_struct and symbol.is_global):
            return None

        # Seed a concrete wrapper module on disk so the hybrid crate can build
        # and the wrapper generator has a stable file path to iteratively replace.
        unimplemented_wrapper = generate_unimplemented_type_wrapper(
            self.c_src_path, symbol.spelling
        )
        wrapper_path = self.crate.src_dir / f"{name}.rs"
        wrapper_path.parent.mkdir(exist_ok=True, parents=True)
        wrapper_path.write_text(str(unimplemented_wrapper))
        self.crate.vcs.add(wrapper_path)

        # Register the wrapper module in each crate root so tests and callers can
        # resolve it by path and rustc includes it in the build graph.
        for root_path in (self.crate.lib_src_path, self.crate.main_src_path):
            if root_path is None:
                continue
            with root_path.open("a") as f:
                f.write(f"pub mod {name};\n")
            self.crate.vcs.add(root_path)

        # Fail fast after wrapper/linkage edits so we surface compiler errors
        # before iterative wrapper generation starts.
        builds, build_feedback = self.crate.cargo_build()
        if not builds:
            raise RuntimeError(f"The crate does not build!\n\n{build_feedback}")

        def test(wrapper: CodeRust) -> str:
            # Write wrapper to disk and add to VCS so commit can commit it
            wrapper_path.write_text(str(wrapper))
            self.crate.vcs.add(wrapper_path)

            # Enforce that both required round-trip test functions are present
            missing = [
                fn
                for fn in ("round_trip_zeroed", "round_trip_nontrivial")
                if f"fn {fn}" not in str(wrapper)
            ]
            if missing:
                return (
                    "Required test functions are missing: "
                    + ", ".join(f"`{fn}`" for fn in missing)
                    + ". Both `round_trip_zeroed` and `round_trip_nontrivial` must be implemented."
                )

            # Run the round-trip tests: cargo test provides both compilation errors
            # and test failure messages, giving richer feedback than cargo build alone.
            passes, _, feedback, _ = self.crate.cargo_test(f"{name}::tests", lib=True)
            return "" if passes else f"Running `cargo test` fails!\n{feedback}"

        def commit(msg: str, pred: dspy.Prediction):
            if "reasoning" in pred and pred.reasoning:
                msg += f"\n\n# Reasoning\n{indent(pred.reasoning, '  ')}"
            if "build_feedback" in pred and pred.build_feedback:
                msg += f"\n\n# Build Feedback\n{indent(pred.build_feedback, '  ')}"
            self.crate.vcs.commit(msg)

        pred = self._wrapper(
            symbol=symbol,
            crate_code=context.crate_code,
            translation=translation,
            unimplemented_wrapper=unimplemented_wrapper,
            wrapper_path=wrapper_path.relative_to(self.crate.cargo_toml.parent),
            wrapped_crate=self.rust_crate.lib_name,
            other_wrappers=context.wrappers,
            prior_wrapper=prior_wrapper,
            support_code=context.support_code + snippet + context.dependent_code,
            feedback_fn=test,
            on_attempt=commit,
        )
        return _WrapperResult.from_pred(pred)

    def _wrap_function(
        self,
        name: WrapperName,
        symbol: Symbol,
        context: TranslationContext,
        snippet: CodeC,
        translation: CodeRust,
        prior_wrapper: CodeRust | None,
    ) -> _WrapperResult | None:
        if self._wrapper is None:
            return None
        # No point in wrapping non-globals if no tests
        if self._tests is None and not symbol.is_global:
            return None

        # Seed a placeholder wrapper module so the crate can compile immediately
        # and the wrapper generator has a stable file to iteratively overwrite.
        unimplemented_wrapper = generate_unimplemented_function_wrapper(
            self.c_src_path, symbol.spelling
        )
        if unimplemented_wrapper is None:
            logger.warning(f"Skipping wrap of function symbol `{symbol.name}`")
            return _WrapperResult()
        wrapper_path = self.crate.src_dir / f"{name}.rs"
        wrapper_path.parent.mkdir(exist_ok=True, parents=True)
        wrapper_path.write_text(str(unimplemented_wrapper))
        self.crate.vcs.add(wrapper_path)

        # Switch the C function declaration to extern so the Rust wrapper crate
        # can provide the callable definition without duplicate symbol ownership.
        clang_make_extern_(self.c_src_path, symbol.spelling)
        self.crate.vcs.add(self.c_src_path)

        # Register the wrapper module in each crate root so tests and callers can
        # resolve it by path and rustc includes it in the build graph.
        for root_path in (self.crate.lib_src_path, self.crate.main_src_path):
            if root_path is None:
                continue
            with root_path.open("a") as f:
                f.write(f"pub mod {name};\n")
            self.crate.vcs.add(root_path)

        # Fail fast after wrapper/linkage edits so linker or compile regressions
        # are caught before iterative wrapper generation continues.
        builds, build_feedback = self.crate.cargo_build()
        if not builds:
            raise RuntimeError(f"The crate does not build!\n\n{build_feedback}")

        def build(wrapper: CodeRust) -> str:
            wrapper_path.write_text(str(wrapper))
            self.crate.vcs.add(wrapper_path)
            builds, build_feedback = self.crate.cargo_build()
            return build_feedback if not builds else ""

        def commit(msg: str, pred: dspy.Prediction):
            if "reasoning" in pred and pred.reasoning:
                msg += f"\n\n# Reasoning\n{indent(pred.reasoning, '  ')}"
            if "build_feedback" in pred and pred.build_feedback:
                msg += f"\n\n# Build Feedback\n{indent(pred.build_feedback, '  ')}"
            if "scope_feedback" in pred and pred.scope_feedback:
                msg += f"\n\n# Scope Feedback\n{indent(pred.scope_feedback, '  ')}"
            self.crate.vcs.commit(msg)

        pred = self._wrapper(
            symbol=symbol,
            crate_code=context.crate_code,
            translation=translation,
            unimplemented_wrapper=unimplemented_wrapper,
            wrapper_path=wrapper_path.relative_to(self.crate.cargo_toml.parent),
            wrapped_crate=self.rust_crate.lib_name,
            other_wrappers=context.wrappers,
            prior_wrapper=prior_wrapper,
            support_code=context.support_code + snippet + context.dependent_code,
            feedback_fn=build,
            on_attempt=commit,
        )
        return _WrapperResult.from_pred(pred)


def _extract_test_results(output: str) -> dict[str, bool]:
    test_results: dict[str, bool] = {}

    for line in output.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "test":
            continue
        event = obj.get("event")
        if event not in {"ok", "failed", "ignored"}:
            continue
        name = str(obj.get("name", "")).rsplit("$", 1)[-1].strip()
        if name:
            # Treat ignored as non-failing for disable-list purposes
            test_results[name] = event != "failed"

    return test_results
