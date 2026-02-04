#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
from graphlib import TopologicalSorter
from collections import OrderedDict, defaultdict, deque

import dspy

from .utils import Symbol

logger = logging.getLogger("ideas.translate_recurrent")


class RecurrentTranslator(dspy.Module):
    def __init__(self, symbol_translator: dspy.Module):
        super().__init__()
        self.translate_symbol = symbol_translator

    def forward(
        self, symbols: dict[str, Symbol], dependencies: dict[str, list[str]]
    ) -> dspy.Prediction:
        references = transpose_graph(dependencies)
        sorted_symbol_names = list(TopologicalSorter(dependencies).static_order())

        # Translate symbols in topological order
        translations: dict[str, str] = OrderedDict()
        for symbol_name in sorted_symbol_names:
            # Ignore tag definitions and function declarations
            if symbol_name not in symbols:
                logger.warning(f"Skipping symbol `{symbol_name}` ...")
                continue

            symbol = symbols[symbol_name]
            dep_names = [
                name for name in bfs(symbol_name, references, max_depth=1) if name in symbols
            ]

            # Gather reference and dependent code in order of translations and sorted symbols, respectively
            ref_translations = "\n\n".join(translations.values())
            dep_symbols = [symbols[name] for name in sorted_symbol_names if name in dep_names]

            pred = self.translate_symbol(ref_translations, symbol, dep_symbols)
            if not pred.success:
                break

            # Save translation if it builds
            translations[symbol_name] = pred.translation.code

        translation = "\n\n".join(translations.values())
        return dspy.Prediction(
            translation=translation, success=len(translations) == len(sorted_symbol_names)
        )


def transpose_graph(graph: dict[str, list[str]]) -> dict[str, list[str]]:
    transpose: dict[str, list[str]] = defaultdict(list)
    for node, neighbors in graph.items():
        for neighbor in neighbors:
            transpose[neighbor].append(node)
    return dict(transpose)


def bfs(node: str, graph: dict[str, list[str]], max_depth: int = -1) -> list[str]:
    nodes = [node]
    queue = deque()
    queue.append((node, 0))
    while queue:
        curr_node, level = queue.popleft()
        for neighbor in graph.get(curr_node, []):
            # ignore visited or too deep nodes
            if neighbor in nodes or (max_depth >= 0 and level + 1 > max_depth):
                continue
            nodes.append(neighbor)
            queue.append((neighbor, level + 1))
    # ignore initial node
    return nodes[1:]
