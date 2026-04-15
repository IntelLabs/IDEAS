#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import sys
import os
import logging
from pathlib import Path
from dataclasses import dataclass
from itertools import combinations
from graphlib import TopologicalSorter, CycleError

import hydra
import networkx as nx
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig
from clang.cindex import CompilationDatabase, TranslationUnit
from clang.cindex import TranslationUnitLoadError, Diagnostic

from ideas.ast import extract_info_c, TreeResult, Symbol, clang_rename_
from ideas.tools import Crate, check_c

logger = logging.getLogger("ideas.init.consolidate")


@dataclass
class ConsolidateConfig:
    filename: Path = MISSING
    cargo_toml: Path = MISSING
    vcs: str = "none"

    source_priority: Path | None = None


cs = ConfigStore.instance()
cs.store(name="init.consolidate", node=ConsolidateConfig)


def init(compile_commands: Path, source_priority: list[Path]) -> str:
    # Get symbol table and dependencies taking into account source priority
    asts = get_asts(compile_commands, source_priority)
    symbols, dependencies = get_symbols_and_dependencies(asts, source_priority)
    logger.info(f"Found {len(symbols)} symbols in {compile_commands}!")

    # Consolidate C sources in topological order
    sources = get_includes(symbols)
    for group in TopologicalSorter(dependencies).static_order():
        # Add forward declarations if more than one symbol in group
        if len(group) > 1:
            for name in group:
                declaration = symbols[name].declaration
                if declaration and declaration not in sources:
                    sources.append(declaration)

        # Add symbol definitions
        for name in group:
            definition = symbols[name].code + "\n"
            if definition not in sources:
                sources.append(definition)
    return "\n".join(sources)


def get_symbols_and_dependencies(
    asts: list[TreeResult],
    source_priority: list[Path] | None = None,
    external_symbol_names: list[str] | None = None,
) -> tuple[dict[str, Symbol], dict[tuple[str, ...], list[tuple[str, ...]]]]:
    source_priority = source_priority or []

    # Merge ASTs into non-system project dependencies
    list_of_non_system_symbols = [
        {n: s for n, s in ast.symbols.items() if not s.is_system} for ast in asts
    ]
    project_symbols = merge_symbols(list_of_non_system_symbols, source_priority)
    project_dependencies = nx.compose_all(
        [
            nx.from_dict_of_lists(ast.complete_graph, create_using=nx.DiGraph)  # type: ignore
            for ast in asts
        ]
    ).subgraph(project_symbols.keys())

    # Find all reachable symbols and subgraph of dependencies from symbols with global functions/variables
    symbols = project_symbols.copy()
    dependencies = project_dependencies.copy()
    if external_symbol_names is None:
        # Use global function/variables as desired external symbol names
        external_symbol_names = [
            name
            for name, symbol in symbols.items()
            if symbol.is_global and (symbol.is_function or symbol.is_variable)
        ]
    if external_symbol_names:
        paths = nx.multi_source_dijkstra_path(project_dependencies, external_symbol_names)
        symbols = {k: v for k, v in symbols.items() if k in paths}
        dependencies = dependencies.subgraph(symbols.keys())
    else:
        logger.warning("No external symbols were found/specified!")

    # Remove cycles from graph by combining strongly-connected components. Note that we sort
    # members in a SCC so they are ordered lexically.
    def symbol_lexical_key(name: str) -> tuple[int, str, int, str]:
        sym = symbols[name]
        loc = sym.cursor.location
        tu_file = Path(sym.cursor.translation_unit.spelling).resolve()

        loc_file = tu_file
        if loc.file is not None:
            loc_file = Path(loc.file.name).resolve()

        file_rank = len(source_priority)
        if loc_file in source_priority:
            file_rank = source_priority.index(loc_file)
        return (file_rank, str(loc_file), loc.offset, name)

    C = nx.condensation(dependencies)
    scc_map = {n: tuple(sorted(C.nodes[n]["members"], key=symbol_lexical_key)) for n in C.nodes}
    dependencies = {scc_map[n]: [scc_map[s] for s in C.successors(n)] for n in C.nodes}

    # Make sure dependencies are topologically sortable
    try:
        list(TopologicalSorter(dependencies).static_order())
    except CycleError as ex:
        logger.error(ex)
        raise ex
    return symbols, dependencies


def get_includes(symbols: dict[str, Symbol]) -> list[str]:
    includes: list[str] = []
    for symbol in symbols.values():
        tu = symbol.cursor.translation_unit
        for inclusion in tu.get_includes():
            # Source of the include should be in same path as TU while the include should NOT be
            tu_path = str(Path(tu.spelling).resolve())
            inclusion_source_path = str(Path(inclusion.source.name).resolve())
            inclusion_include_path = str(Path(inclusion.include.name).resolve())
            # FIXME: Use is_in_system_header? Inclusion locations are always false though.
            if (os.path.commonprefix((tu_path, inclusion_source_path)) != "/") and (
                os.path.commonprefix((tu_path, inclusion_include_path)) == "/"
            ):
                # Get include directive from source
                with open(inclusion.location.file.name, "rb") as f:
                    f.seek(inclusion.location.offset)
                    include = f.readline().decode().strip()
                include = f"#include {include}"
                if include not in includes:
                    includes.append(include)
    return includes


def get_asts(
    compile_commands: Path, valid_paths: list[Path], rename_conflicting_symbols: bool = True
) -> list[TreeResult]:
    assert compile_commands.name == "compile_commands.json"
    db = CompilationDatabase.fromDirectory(compile_commands.parent)
    cmds = db.getAllCompileCommands()
    assert cmds is not None
    asts = []
    for cmd in cmds:
        try:
            tu = TranslationUnit.from_source(None, args=list(cmd.arguments))
        except TranslationUnitLoadError as e:
            raise TranslationUnitLoadError(
                f"Error parsing '{cmd.filename}' using args `{' '.join(cmd.arguments)}`\n{e}"
            )
        if any(d.severity >= Diagnostic.Error for d in tu.diagnostics):
            raise TranslationUnitLoadError("\n".join([d.format() for d in tu.diagnostics]))
        assert tu.cursor is not None
        if not valid_paths or Path(tu.cursor.spelling).resolve() in valid_paths:
            ast = extract_info_c(tu)
            asts.append(ast)
    if rename_conflicting_symbols:
        original_sources = rename_conflicting_symbols_(asts)
        if original_sources:
            try:
                asts = get_asts(compile_commands, valid_paths, rename_conflicting_symbols=False)
            finally:
                # Always restore original source code after reparsing renamed symbols.
                for path, source in original_sources.items():
                    path.write_bytes(source)
    return asts


def rename_conflicting_symbols_(asts: list[TreeResult]) -> dict[Path, bytes]:
    # Gather best representative symbol per spelling per AST into a single dict
    symbols_with_spelling: dict[str, list[Symbol]] = {}
    for ast in asts:
        seen: dict[str, Symbol] = {}
        for symbol in ast.symbols.values():
            spelling = symbol.spelling
            if not spelling:
                continue
            # Save this symbol if we haven't seen it before
            if spelling not in seen:
                seen[spelling] = symbol
            # Or replace it if it's a definition and existing symbol is a declaration
            elif symbol.is_definition and not seen[spelling].is_definition:
                seen[spelling] = symbol
        for spelling, sym in seen.items():
            symbols_with_spelling.setdefault(spelling, []).append(sym)

    # Find symbols with common spelling but different definitions across ASTs
    tu_renames: dict[TranslationUnit, dict[str, str]] = {}
    for spelling, symbol1, symbol2 in (
        (spelling, *symbol_pair)
        for spelling, symbols in symbols_with_spelling.items()
        for symbol_pair in combinations(symbols, r=2)
    ):
        # Two symbols can only clash if they have same spelling but different definitions
        if not (symbol1.is_definition and symbol2.is_definition):
            continue
        if symbol1.code == symbol2.code:
            continue
        # Rename non-global, non-system symbols using TU stem as prefix
        for symbol in (symbol1, symbol2):
            if symbol.is_global or symbol.is_system:
                continue
            path = Path(symbol.cursor.translation_unit.spelling).resolve()
            new_spelling = path.stem + "_" + spelling
            tu_renames.setdefault(symbol.cursor.translation_unit, {})[symbol.name] = (
                new_spelling
            )
    if not tu_renames:
        return {}

    # Check that renaming won't cause clashes with existing symbols with the same spelling
    existing_spellings = set(symbols_with_spelling.keys())
    for renames in tu_renames.values():
        new_spellings = set(renames.values())
        if existing_spellings.intersection(new_spellings):
            raise NotImplementedError(
                "Renaming symbols would cause clashes with existing symbols with the same spelling!"
            )
        existing_spellings.update(new_spellings)

    # Write renames to disk while keeping track of original source bytes
    sources: dict[Path, bytes] = {}
    try:
        for tu, renames in tu_renames.items():
            # Reparse translation unit in case anything has changed on disk
            tu.reparse()
            clang_rename_(tu, renames, sources)

    except Exception:
        # Restore original source code if renaming fails before caller can reparse.
        for path, source in sources.items():
            path.write_bytes(source)
        raise
    return sources


def merge_symbols(
    list_of_symbols: list[dict[str, Symbol]], source_priority: list[Path]
) -> dict[str, Symbol]:
    global_symbols: dict[str, Symbol] = {}
    for symbols in list_of_symbols:
        # Gather symbols
        for name, symbol in symbols.items():
            # If not in global symbol table add it
            if name not in global_symbols:
                global_symbols[name] = symbol
                continue

            # If code matches, then don't bother replacing
            if global_symbols[name].code == symbol.code:
                continue

            global_source = Path(
                global_symbols[name].cursor.translation_unit.spelling
            ).resolve()
            symbol_source = Path(symbol.cursor.translation_unit.spelling).resolve()

            # If overwriting a symbol, then prefer one with a definition
            if (
                global_symbols[name].cursor.is_definition()
                and not symbol.cursor.is_definition()
            ):
                continue
            elif (
                not global_symbols[name].cursor.is_definition()
                and symbol.cursor.is_definition()
            ):
                global_symbols[name] = symbol
            # Or prefer the symbol with source priority
            elif global_source in source_priority and symbol_source not in source_priority:
                continue
            elif global_source not in source_priority and symbol_source in source_priority:
                global_symbols[name] = symbol
            elif (
                global_source in source_priority
                and symbol_source in source_priority
                and source_priority.index(global_source) > source_priority.index(symbol_source)
            ):
                global_symbols[name] = symbol
            elif (
                global_source in source_priority
                and symbol_source in source_priority
                and source_priority.index(global_source) < source_priority.index(symbol_source)
            ):
                continue
            else:
                # Two symbols have similar names but different declarations or definitions and no source priority!
                raise NotImplementedError(
                    f"Unable to handle symbol {name} with multiple different definitions and unknown source priority!\nSymbol found in {global_source} and {symbol_source}."
                )
    return global_symbols


def _main(cfg: ConsolidateConfig):
    output_dir = Path(HydraConfig.get().runtime.output_dir)

    # Get crate information
    crate = Crate(cargo_toml=cfg.cargo_toml, vcs=cfg.vcs)  # type: ignore[reportArgumentType]

    source_priority: list[Path] = []
    if cfg.source_priority:
        lines = cfg.source_priority.read_text().splitlines()
        source_priority = [Path(line.strip()).resolve() for line in lines if line.strip()]

    output = init(cfg.filename, source_priority)

    # Only run preprocess, compile, and assemble steps on C code
    compiles, compile_errors = check_c(output, flags=["-c"])

    # Write C code to disk
    crate.c_src_path.parent.mkdir(exist_ok=True, parents=True)
    crate.c_src_path.write_text(output)
    crate.vcs.add(crate.c_src_path)

    # Add hydra directory
    if (output_subdir := HydraConfig.get().output_subdir) is not None:
        crate.vcs.add(output_dir / output_subdir)

    # If the C code didn't compile, then error loudly
    name = crate.root_package["name"]
    msg = f"Consolidated `{name}` in {output_dir}"
    if not compiles:
        msg = f"Failed to consolidate `{name}` C code!"
        msg += f"\n\n{compile_errors}"
        logger.error(msg)
    else:
        logger.info(msg)
    crate.vcs.commit(msg)
    if not compiles:
        raise ValueError(f"Failed to compile consolidated `{name}` C code!")


@hydra.main(version_base=None, config_name="init.consolidate")
def main(cfg: ConsolidateConfig):
    try:
        _main(cfg)
    except Exception as e:
        logger.exception(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
