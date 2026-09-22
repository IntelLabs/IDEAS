#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import re
import math
import logging
from collections.abc import Mapping
from dataclasses import dataclass

import networkx as nx

from .ast_rust import BindgenName, CodeRust, strip_fns
from .tools import MAX_DEPENDENT_CHARS, REDUCED_CONTEXT
from .ast import CodeC, Symbol, SymbolName, SymbolGroup

logger = logging.getLogger("ideas.translate_context")


@dataclass(frozen=True)
class TranslateContext:
    crate_code: CodeRust
    reference_code: CodeRust
    dependent_code: CodeC


@dataclass(frozen=True)
class WrapContext:
    wrapped_crate_code: CodeRust
    wrappers: Mapping[BindgenName, CodeRust]
    translated_code: CodeC
    untranslated_code: CodeC

    def support_code(self, snippet: CodeC) -> CodeC:
        return self.translated_code + snippet + self.untranslated_code


@dataclass(frozen=True)
class TranslationContext:
    translate: TranslateContext
    wrap: WrapContext

    @classmethod
    def build(
        cls,
        G: nx.DiGraph,
        group: SymbolGroup,
        groups: list[SymbolGroup],
        symbols: dict[SymbolName, Symbol],
        translations: dict[SymbolGroup, CodeRust] | None = None,
        wrappers: dict[SymbolName, CodeRust] | None = None,
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
        dependent_hops = nx.single_source_shortest_path_length(G.reverse(copy=False), group)
        crate_code = cls._build_crate_code(reference_groups, translations)
        dependent_code = cls._build_dependent_code(to_be_translated, symbols, dependent_hops)
        return cls(
            translate=TranslateContext(
                crate_code=crate_code,
                reference_code=cls._build_reference_code(reference_groups, translations, hops),
                dependent_code=dependent_code,
            ),
            wrap=WrapContext(
                wrapped_crate_code=strip_fns(crate_code),
                wrappers=cls._build_wrapper_context(wrappers, symbols),
                translated_code=cls._build_translated_code(
                    already_translated, immediate_already_translated, symbols
                ),
                untranslated_code=dependent_code,
            ),
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
    def _build_translated_code(
        already_translated: list[SymbolGroup],
        immediate_already_translated: set[SymbolGroup],
        symbols: dict[SymbolName, Symbol],
    ) -> CodeC:
        # Gather already-translated C code in topological order.
        # Reduce C support code context by turning non-immediate symbols into declarations. We keep
        # immediate C code in full since they are more likely to be relevant for wrappers.
        return CodeC.join(
            symbols[name].code
            if g in immediate_already_translated
            else symbols[name].forward_declaration or symbols[name].code
            for g in already_translated
            for name in g
        )

    @classmethod
    def _build_dependent_code(
        cls,
        to_be_translated: list[SymbolGroup],
        symbols: dict[SymbolName, Symbol],
        hops: Mapping[SymbolGroup, int],
        max_chars: int | None = None,
    ) -> CodeC:
        if max_chars is None:
            max_chars = MAX_DEPENDENT_CHARS

        # Gather dependent C code in topological order.
        dependent_code = CodeC.join(symbols[name].code for g in to_be_translated for name in g)
        if len(str(dependent_code)) > max_chars:
            logger.warning(f"Dependent code exceeds max {len(str(dependent_code))}/{max_chars}")
            dependent_code = cls._select_c_code(to_be_translated, symbols, hops, max_chars)
        return dependent_code

    _MEMORY_PATTERN = re.compile(
        r"malloc|calloc|realloc|free|memcpy|memmove|memset|strdup|strndup|fopen|freopen|fclose"
    )
    _POINTER_PATTERN = re.compile(r"->|\*|&|\[|\bNULL\b|\bsizeof\b")

    @dataclass(frozen=True)
    class _DependentCandidate:
        group: SymbolGroup
        full: CodeC
        full_chars: int
        score: float
        tier: int

    @classmethod
    def _select_c_code(
        cls,
        groups: list[SymbolGroup],
        symbols: dict[SymbolName, Symbol],
        hops: Mapping[SymbolGroup, int],
        max_chars: int,
    ) -> CodeC:
        candidates = cls._collect_dependent_candidates(groups, symbols, hops)
        chosen: set[SymbolGroup] = set()
        total_chars = 0
        exhausted_tier: int | None = None

        for candidate in sorted(
            candidates,
            key=lambda c: (c.tier, -c.score / math.sqrt(max(c.full_chars, 1))),
        ):
            if exhausted_tier is not None and candidate.tier > exhausted_tier:
                break
            if total_chars + candidate.full_chars > max_chars:
                exhausted_tier = candidate.tier
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
        hops: Mapping[SymbolGroup, int],
    ) -> list["TranslationContext._DependentCandidate"]:
        candidates = []
        for group in groups:
            full = CodeC.join(symbols[name].code for name in group)
            candidates.append(
                cls._DependentCandidate(
                    group=group,
                    full=full,
                    full_chars=len(str(full)),
                    score=cls._score_dependent_group(group, symbols),
                    tier=cls._dependent_tier(hops.get(group)),
                )
            )
        return candidates

    @staticmethod
    def _dependent_tier(hops: int | None) -> int:
        match hops:
            case 1:
                return 1
            case 2:
                return 2
            case None:  # not reachable
                return 4
            case _:
                return 3

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
    def _build_wrapper_context(
        cls,
        wrappers: dict[SymbolName, CodeRust],
        symbols: dict[SymbolName, Symbol],
    ) -> dict[BindgenName, CodeRust]:
        context: dict[BindgenName, CodeRust] = {}
        for name, wrapper in wrappers.items():
            symbol = symbols[name]
            # An anonymous record has no stable name to key its context by
            if (bindgen_name := symbol.bindgen_name) is None:
                continue
            if symbol.is_type:
                context[bindgen_name] = cls._build_type_wrapper_context(wrapper)
            elif symbol.is_variable:
                context[bindgen_name] = cls._build_variable_wrapper_context(wrapper)
            # Drop other wrappers if requested
            elif not REDUCED_CONTEXT:
                context[bindgen_name] = wrapper
        return context

    @classmethod
    def _build_variable_wrapper_context(cls, wrapper: CodeRust) -> CodeRust:
        return strip_fns(cls._strip_tests(wrapper))

    @classmethod
    def _build_type_wrapper_context(cls, wrapper: CodeRust) -> CodeRust:
        # Delete functions because the trait already fixes their signatures
        return strip_fns(cls._strip_tests(wrapper), delete=True)

    @staticmethod
    def _strip_tests(wrapper: CodeRust) -> CodeRust:
        wrapper_src = str(wrapper)
        test_idx = wrapper_src.find("#[cfg(test)]")
        if test_idx != -1:
            wrapper_src = wrapper_src[:test_idx].strip()
        return CodeRust(wrapper_src)
