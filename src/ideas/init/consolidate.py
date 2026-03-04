#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import os
import logging
import sys
from pathlib import Path
from graphlib import TopologicalSorter, CycleError
from collections.abc import Iterable, Container
from dataclasses import dataclass

import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig
from clang.cindex import CompilationDatabase, TranslationUnit, CursorKind
from clang.cindex import Rewriter, TokenKind, SourceRange
from clang.cindex import TranslationUnitLoadError, Diagnostic

from ideas.ast import extract_info_c, TreeResult, Symbol
from ideas.ast import get_internally_linked_cursors
from ideas.tools import Crate, clang_rename_, check_c

logger = logging.getLogger("ideas.preprocess")


@dataclass
class ConsolidateConfig:
    filename: Path = MISSING
    cargo_toml: Path = MISSING

    export_symbols: Path | None = None
    source_priority: Path | None = None


cs = ConfigStore.instance()
cs.store(name="init.consolidate", node=ConsolidateConfig)


def init(
    compile_commands: Path,
    export_symbols: list[str] | None = None,
    source_priority: list[Path] | None = None,
) -> str:
    # Get symbol table and dependencies taking into account source priority and exported symbols,
    # and prefix internally linked declarations/references since there can be name collisions
    # between translation units.
    asts = get_asts(
        compile_commands,
        valid_paths=source_priority,
        prefix_internally_linked=False,
    )
    symbols, dependencies = get_symbols_and_dependencies(asts, source_priority, export_symbols)
    logger.info(f"Found {len(symbols)} symbols in {compile_commands}!")

    # Assemble C sources in topological order
    includes = get_includes(symbols)
    sources = []
    sorted_symbol_names = list(TopologicalSorter(dependencies).static_order())
    for symbol_name in sorted_symbol_names:
        # Ignore function declarations
        if symbol_name not in symbols:
            logger.warning(f"Skipping `{symbol_name}` ...")
            continue
        symbol_code = symbols[symbol_name].code
        if symbol_code in sources:
            continue
        sources.append(symbol_code)
    return "\n".join(includes) + "\n\n" + "\n\n".join(sources)


def get_symbols_and_dependencies(
    asts: list[TreeResult],
    source_priority: list[Path] | None = None,
    export_symbols: list[str] | None = None,
) -> tuple[dict[str, Symbol], dict[str, list[str]]]:
    asts_symbols = [filter_symbols(ast.symbols) for ast in asts]
    global_symbols = merge_symbols(asts_symbols, source_priority=source_priority)

    # Filter global symbols to create project symbols
    project_symbols = filter_symbols(global_symbols, filter_system=True)
    project_dependencies = merge_complete_graphs(asts, valid_names=project_symbols)

    # Use export_symbols to filter project symbols and dependencies
    dependencies = remove_cycles_from_graph(project_dependencies, project_symbols)
    if export_symbols is not None:
        export_symbols = [c14n_symbol_name(name, project_symbols) for name in export_symbols]
        dependencies = reachable_subgraph(dependencies, export_symbols)
    symbols = filter_symbols(project_symbols, filter_function_declarations=True)

    return symbols, dependencies


def get_includes(symbols: dict[str, Symbol]) -> set[str]:
    includes: set[str] = set()
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
                includes.add(f"#include {include}")
    return includes


def get_asts(
    compile_commands: Path,
    valid_paths: list[Path] | None = None,
    prefix_internally_linked: bool = False,
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
        if prefix_internally_linked:
            # FIXME: It would be nicer to add a prefix to only those symbols that collide but we
            #        cannot know that until symbol merge time.
            tu = add_prefix_to_internally_linked_cursors(tu, compile_commands)
        assert tu.cursor is not None
        if valid_paths is None or Path(tu.cursor.spelling).resolve() in valid_paths:
            ast = extract_info_c(tu)
            asts.append(ast)
    return asts


def add_prefix_to_internally_linked_cursors(
    tu: TranslationUnit,
    compile_commands: Path,
) -> TranslationUnit:
    assert tu.cursor is not None
    cursors = get_internally_linked_cursors(tu.cursor)

    # Prefix internally-linked declarations using TU stem
    # FIXME: TU stem isn't guaranteed to be a non-clashing since folder1/stem.c
    #        and folder2/stem.c will produce same prefix.
    source = Path(tu.spelling)
    prefix = source.stem + "_"
    renames = {cursor.spelling: prefix + cursor.spelling for cursor in cursors}

    # XXX: This is an in-place rename! Would be nice to have a context manager that can automatically
    #      restore the contents of the file. We need an in-place rename because downstream code may
    #      use tu.cursor.spelling which needs to point to a valid file.
    source_bytes = source.read_bytes()
    try:
        # Remove static visibility from internally-linked cursors
        remove_static_keyword_(tu)

        # Add prefix to internally-linked declarations
        clang_rename_(source, renames, compile_commands=compile_commands)
        tu.reparse()
    finally:
        source.write_bytes(source_bytes)

    # There should be no more internally linked cursors because we made them externally visible
    assert len(get_internally_linked_cursors(tu.cursor)) == 0

    return tu


def remove_static_keyword_(tu: TranslationUnit):
    assert tu.cursor is not None
    cursors = get_internally_linked_cursors(tu.cursor)

    rewriter = Rewriter.create(tu)
    for cursor in cursors:
        # Find static keyword in cursor tokens
        tokens = list(cursor.get_tokens())
        for i, token in enumerate(tokens):
            if token.kind == TokenKind.KEYWORD and token.spelling == "static":
                # Use next token's start as end of extent so we capture the spacing between the static
                # keyword and the next token.
                extent = SourceRange.from_locations(
                    token.extent.start,
                    tokens[i + 1].extent.start if i + 1 < len(tokens) else token.extent.end,
                )
                rewriter.remove_text(extent)
    rewriter.overwrite_changed_files()


def filter_symbols(
    symbols: dict[str, Symbol],
    filter_system: bool = True,
    filter_function_declarations: bool = False,
) -> dict[str, Symbol]:
    filtered_symbols = {}
    for name, symbol in symbols.items():
        # Ignore "system" symbols
        if filter_system and symbol.cursor.location.is_in_system_header:
            continue

        # Filter function declarations
        if filter_function_declarations:
            if (
                symbol.cursor.kind == CursorKind.FUNCTION_DECL
                and not symbol.cursor.is_definition()
            ):
                continue

        filtered_symbols[name] = symbols[name]
    return filtered_symbols


def merge_symbols(
    list_of_symbols: list[dict[str, Symbol]], source_priority: list[Path] | None = None
) -> dict[str, Symbol]:
    if source_priority is None:
        source_priority = []

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


def merge_complete_graphs(
    asts: list[TreeResult], valid_names: Container[str]
) -> dict[str, list[str]]:
    graph: dict[str, list[str]] = {}
    for ast in asts:
        for node, neighbors in ast.complete_graph.items():
            if node not in valid_names:
                continue
            if node not in graph:
                graph[node] = []
            for neighbor in neighbors:
                if neighbor not in valid_names or neighbor in graph[node]:
                    continue
                graph[node].append(neighbor)
    return dict(graph)


def remove_cycles_from_graph(
    graph: dict[str, list[str]], symbols: dict[str, Symbol]
) -> dict[str, list[str]]:
    # Remove self-dependencies from graph: symbol -> [symbol] => symbol -> []
    for dependent, dependencies in graph.items():
        graph[dependent] = [
            dependency for dependency in dependencies if dependency != dependent
        ]

    # FIXME: Add more C-specific heuristics to remove cycles from the graph

    # Make sure graph is topologically sortable
    try:
        list(TopologicalSorter(graph).static_order())
    except CycleError as ex:
        logger.error(ex)
        raise ex
    return graph


def reachable_subgraph(
    dependencies: dict[str, list[str]], names: Iterable[str]
) -> dict[str, list[str]]:
    subgraph: dict[str, list[str]] = {}
    for name in names:
        subgraph[name] = dependencies[name]
        subgraph.update(reachable_subgraph(dependencies, subgraph[name]))
    return subgraph


def c14n_symbol_name(name: str, symbols: dict[str, Symbol]):
    if name in symbols:
        return name
    if f"c:@F@{name}" in symbols:
        return f"c:@F@{name}"

    # Find symbols with spelling of name
    potential_names = {s.name for s in symbols.values() if name in s.cursor.spelling}
    if len(potential_names) == 0:
        symbol_names = "\n".join(symbols.keys())
        raise ValueError(f"Unable to find {name} in symbols:\n{symbol_names}")
    elif len(potential_names) != 1:
        raise ValueError(f"Unable to find {name} in symbols! Found: {potential_names}")
    return potential_names.pop()


@hydra.main(version_base=None, config_name="init.consolidate")
def main(cfg: ConsolidateConfig) -> None:
    output_dir = Path(HydraConfig.get().runtime.output_dir)

    # Get crate information
    crate = Crate(cargo_toml=cfg.cargo_toml)

    export_symbols = None
    if isinstance(cfg.export_symbols, Path):
        export_symbols = cfg.export_symbols.read_text().splitlines()

    source_priority = None
    if isinstance(cfg.source_priority, Path):
        source_priority = [
            Path(path).resolve() for path in cfg.source_priority.read_text().splitlines()
        ]

    output = init(
        cfg.filename,
        export_symbols=export_symbols,
        source_priority=source_priority,
    )

    # Only run preprocess, compile, and assemble steps on C code
    compiles, compile_errors = check_c(output, flags=["-c"])

    # Write C code to disk
    crate.rust_src_path.parent.mkdir(exist_ok=True, parents=True)
    crate.rust_src_path.with_suffix(".c").write_text(output)
    crate.add(crate.rust_src_path.with_suffix(".c"))

    # Add hydra directory
    if (output_subdir := HydraConfig.get().output_subdir) is not None:
        crate.add(output_dir / output_subdir)

    # If the C code didn't compile, then error loudly
    name = crate.root_package["name"]
    if not compiles:
        logger.error(f"Failed to compile `{name}` C code!")
        crate.commit(
            f"Failed to compile `{name}` C code!\n\n{' '.join(sys.argv)}\n\n{compile_errors}"
        )
        sys.exit(1)
    logger.info(f"Consolidated `{name}` in {output_dir}")
    crate.commit(f"Consolidated `{name}`\n\n{' '.join(sys.argv)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(e)
        raise e
