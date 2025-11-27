#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#
# These contents may have been developed with support from one or more
# Intel-operated generative artificial intelligence solutions.

import math
from dataclasses import dataclass

from clang.cindex import Cursor, CursorKind
from tree_sitter import Node


@dataclass(frozen=True)
class Symbol:
    name: str
    cursor: Cursor
    decl: str = ""
    usr: str | None = None

    @property
    def kind(self) -> CursorKind:
        return self.cursor.kind


@dataclass(frozen=True)
class RustSymbol:
    name: str
    node: Node
    decl: str = ""
    attributes: list[str] | None = None


# Recursively collect dependencies of a symbol
def get_all_deps(
    current_graph: dict[str, list[Symbol]],
    name: str,
    cutoffs: set[str] | None = None,
    cache: dict[str, list[Symbol]] | None = None,
    max_depth: int | float = math.inf,
    _visited: set[str] | None = None,
    _depth: int = 0,
) -> list[Symbol]:
    expanded_deps = []
    if not cache:
        cache = dict()
    if not cutoffs:
        cutoffs = set()
    if not _visited:
        _visited = set()

    # If we have already visited this symbol, return the cached result
    if name in cache:
        return cache[name]
    if name in _visited or _depth > max_depth:
        return expanded_deps
    _visited.add(name)

    # NOTE: Nameless symbols should not be further traversed
    if name not in current_graph:
        _visited.remove(name)
        cache[name] = expanded_deps
        return expanded_deps

    # Get all direct dependencies
    current_deps = current_graph[name]

    # Depth-first recursion
    seen_names = set()
    for dep_symbol in current_deps:
        if dep_symbol.name in cutoffs:
            continue

        # Collect transitive deps of this dependency
        trans_deps = get_all_deps(
            current_graph,
            dep_symbol.name,
            cutoffs,
            cache,
            max_depth,
            _visited,
            _depth + 1,
        )

        # Add transitive dependencies
        for trans_dep in trans_deps:
            if trans_dep.name not in seen_names and trans_dep.name != name:
                expanded_deps.append(trans_dep)
                seen_names.add(trans_dep.name)

        # Add the dependency itself
        if dep_symbol.name not in seen_names and dep_symbol.name != name:
            expanded_deps.append(dep_symbol)
            seen_names.add(dep_symbol.name)

    _visited.remove(name)
    cache[name] = expanded_deps
    return expanded_deps
