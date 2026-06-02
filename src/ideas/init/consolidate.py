#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import sys
import os
import logging
from pathlib import Path
from functools import cmp_to_key
from dataclasses import dataclass
from graphlib import TopologicalSorter, CycleError

import hydra
import networkx as nx
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig
from clang.cindex import CompilationDatabase, TranslationUnit, CursorKind, Cursor
from clang.cindex import TranslationUnitLoadError, Diagnostic, StorageClass

from ideas.ast import (
    extract_info_c,
    TreeResult,
    Symbol,
    clang_rename_,
    get_system_macro_undefs,
    mangle,
)
from ideas.tools import Crate, check_c, LARGE_PROJECT

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
    ast_order = create_ast_order(source_priority, asts)
    symbols, dependencies = get_symbols_and_dependencies(asts, ast_order=ast_order)
    logger.info(f"Found {len(symbols)} symbols in {compile_commands}!")

    # Consolidate C sources in lexicographical topological order
    symbol_lexical_key = create_symbol_lexical_key_fn(symbols, ast_order)
    sorted_symbol_groups = list(
        nx.lexicographical_topological_sort(
            nx.from_dict_of_lists(dependencies, create_using=nx.DiGraph).reverse(copy=False),  # type: ignore
            key=symbol_lexical_key,
        )
    )

    includes = get_includes(symbols)
    feature_defines = get_feature_defines(compile_commands)
    feature_undefs = [f"#undef {line.split()[1]}" for line in feature_defines]
    sources = feature_defines + includes + feature_undefs + [""]
    for group in sorted_symbol_groups:
        # Add forward declarations if more than one symbol in group
        if len(group) > 1:
            for name in group:
                declaration = symbols[name].declaration
                if declaration and declaration.text not in sources:
                    sources.append(declaration.text)

        # Add symbol definitions
        for name in group:
            definition = symbols[name].code.text
            if definition not in sources:
                sources.append(definition)

    # Prevent double-expansion of self-referencing macros from signal.h
    header_len = len(feature_defines) + len(includes) + len(feature_undefs)
    signal_includes = [inc for inc in includes if "signal.h" in inc]
    code_text = "\n".join(sources[header_len:])
    undefs = get_system_macro_undefs(signal_includes, code_text)
    if undefs:
        sources = (
            feature_defines + includes + feature_undefs + undefs + [""] + sources[header_len:]
        )

    return "\n".join(sources)


def get_symbols_and_dependencies(
    asts: list[TreeResult],
    external_symbol_names: list[str] | None = None,
    ast_order: dict[Path, TreeResult] | None = None,
) -> tuple[dict[str, Symbol], dict[tuple[str, ...], list[tuple[str, ...]]]]:
    # Merge ASTs into non-system project dependencies
    list_of_non_system_symbols = [
        {n: s for n, s in ast.symbols.items() if not s.is_system} for ast in asts
    ]
    project_symbols = merge_symbols(list_of_non_system_symbols, ast_order)
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
            if symbol.is_global
            and (symbol.is_variable or (symbol.is_function and symbol.is_definition))
        ]
    if external_symbol_names:
        paths = nx.multi_source_dijkstra_path(project_dependencies, external_symbol_names)
        symbols = {k: v for k, v in symbols.items() if k in paths}
        dependencies = dependencies.subgraph(symbols.keys()).copy()
    else:
        logger.warning("No external symbols were found/specified!")

    # Remove cycles from graph by combining strongly-connected components. Note that we sort
    # members in a SCC so they are ordered lexically.
    C = nx.condensation(dependencies)
    symbol_lexical_key = create_symbol_lexical_key_fn(symbols, ast_order)
    scc_map = {n: tuple(sorted(C.nodes[n]["members"], key=symbol_lexical_key)) for n in C.nodes}
    dependencies = {scc_map[n]: [scc_map[s] for s in C.successors(n)] for n in C.nodes}

    # Force pure type-declaration SCCs into one SCC unit without introducing cycles.
    if not LARGE_PROJECT:
        dependencies = _merge_pure_type_declaration_sccs(
            C, scc_map, symbols, symbol_lexical_key
        )

    # Make sure dependencies are topologically sortable
    try:
        list(TopologicalSorter(dependencies).static_order())
    except CycleError as ex:
        logger.error(ex)
        raise ex
    return symbols, dependencies


def create_ast_order(
    source_priority: list[Path], asts: list[TreeResult]
) -> dict[Path, TreeResult]:
    # Preserve explicit source priority ordering first, then deterministically append
    # any remaining TUs.
    ast_by_path: dict[Path, TreeResult] = {}
    for tree in asts:
        first_symbol = next(iter(tree.symbols.values()), None)
        if first_symbol is None:
            continue
        path = first_symbol.source_path
        ast_by_path[path] = tree

    ast_order: dict[Path, TreeResult] = {}
    seen: set[Path] = set()

    for path in source_priority:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            if resolved in ast_by_path:
                ast_order[resolved] = ast_by_path[resolved]

    for tu_path in sorted(ast_by_path.keys()):
        if tu_path not in seen:
            logger.info("Adding translation unit not seen in source_priority: %s", tu_path)
            seen.add(tu_path)
            ast_order[tu_path] = ast_by_path[tu_path]
    return ast_order


def create_symbol_lexical_key_fn(
    symbols: dict[str, Symbol],
    ast_order: dict[Path, TreeResult] | None = None,
):
    def compare_symbol_lexical(a: str | tuple[str, ...], b: str | tuple[str, ...]) -> int:
        # Support symbol groups by using the first symbol in the group.
        a_name = a[0] if isinstance(a, tuple) else a
        b_name = b[0] if isinstance(b, tuple) else b

        a_symbol = symbols[a_name]
        b_symbol = symbols[b_name]

        a_tu = a_symbol.source_path
        b_tu = b_symbol.source_path

        # If symbols are from the same translation unit, then we can directly
        # compare their locations for lexical ordering.
        if a_tu == b_tu:
            return _cmp_cursor_loc(a_symbol.cursor, b_symbol.cursor)

        if ast_order is None:
            raise RuntimeError(
                f"Cannot compare symbols from different translation units without ast_order: {a} ({a_tu}) vs {b} ({b_tu})."
            )

        # If a's USR appears in b's TU with matching code, both symbols are
        # present in b_tu and can be compared by their locations there
        b_ast = ast_order.get(b_tu)
        if (
            b_ast is not None
            and a_name in b_ast.symbols
            and b_ast.symbols[a_name].code.text == a_symbol.code.text
        ):
            return _cmp_cursor_loc(b_ast.symbols[a_name].cursor, b_symbol.cursor)

        # If b's USR appears in a's TU with matching code, both symbols are
        # present in a_tu and can be compared by their locations there
        a_ast = ast_order.get(a_tu)
        if (
            a_ast is not None
            and b_name in a_ast.symbols
            and a_ast.symbols[b_name].code.text == b_symbol.code.text
        ):
            return _cmp_cursor_loc(a_symbol.cursor, a_ast.symbols[b_name].cursor)

        # The symbol's USR is not shared across TUs, so fall back to ordering
        # by the position of each symbol's TU in ast_order (source priority)
        ast_rank = {path: i for i, path in enumerate(ast_order)}
        try:
            a_rank = ast_rank[a_tu]
            b_rank = ast_rank[b_tu]
        except KeyError as ex:
            raise RuntimeError(
                f"Cannot compare symbols because one or both translation units are missing from ast_order: {a_tu}, {b_tu}."
            ) from ex

        if a_rank < b_rank:
            return -1
        if a_rank > b_rank:
            return 1
        raise RuntimeError("Distinct translation units cannot have identical ranks!")

    return cmp_to_key(compare_symbol_lexical)


def _cmp_cursor_loc(cursor_a: Cursor, cursor_b: Cursor) -> int:
    loc_a = cursor_a.location
    loc_b = cursor_b.location
    if loc_a < loc_b:
        return -1
    if loc_b < loc_a:
        return 1
    if cursor_a.get_usr() != cursor_b.get_usr():
        raise ValueError(
            f"Unable to order distinct symbols with identical lexical priority and location:"
            f" {cursor_a.get_usr()} @ {loc_a} vs {cursor_b.get_usr()} @ {loc_b}"
        )
    return 0


def _merge_pure_type_declaration_sccs(
    condensed: nx.DiGraph,
    scc_map: dict[int, tuple[str, ...]],
    symbols: dict[str, Symbol],
    symbol_lexical_key_fn,
) -> dict[tuple[str, ...], list[tuple[str, ...]]]:
    base_dependencies: dict[tuple[str, ...], list[tuple[str, ...]]] = {
        scc_map[n]: sorted(
            (scc_map[s] for s in condensed.successors(n)), key=symbol_lexical_key_fn
        )
        for n in condensed.nodes
    }

    # Only include SCCs where every member is a type declaration
    type_scc_nodes = [
        n
        for n, members in scc_map.items()
        if members
        and all(
            symbols[name].kind
            in (
                CursorKind.STRUCT_DECL,
                CursorKind.UNION_DECL,
                CursorKind.ENUM_DECL,
                CursorKind.FIELD_DECL,
                CursorKind.ENUM_CONSTANT_DECL,
                CursorKind.TYPEDEF_DECL,
            )
            for name in members
        )
    ]
    if len(type_scc_nodes) <= 1:
        return base_dependencies

    # Merge all pure type declaration SCCs into one SCC unit and update dependencies
    # accordingly without introducing cycles. Preserve the dependency order
    # between the original type SCCs so by-value type definitions remain valid.
    type_scc_set = set(type_scc_nodes)
    ordered_type_members_by_scc = {
        n: tuple(sorted(scc_map[n], key=symbol_lexical_key_fn)) for n in type_scc_nodes
    }
    ordered_type_scc_nodes = sorted(
        type_scc_nodes,
        key=lambda n: tuple(
            symbol_lexical_key_fn(name) for name in ordered_type_members_by_scc[n]
        ),
    )
    type_scc_dependencies = {
        n: tuple(
            succ
            for succ in ordered_type_scc_nodes
            if succ in set(condensed.successors(n)) and succ in type_scc_set
        )
        for n in ordered_type_scc_nodes
    }
    try:
        merged_group = tuple(
            name
            for n in TopologicalSorter(type_scc_dependencies).static_order()
            for name in ordered_type_members_by_scc[n]
        )
    except CycleError:
        merged_group = tuple(
            name for n in ordered_type_scc_nodes for name in ordered_type_members_by_scc[n]
        )

    merged_dependencies: dict[tuple[str, ...], set[tuple[str, ...]]] = {}
    merged_successors: set[tuple[str, ...]] = set()

    for n in condensed.nodes:
        if n in type_scc_set:
            for succ in condensed.successors(n):
                if succ not in type_scc_set:
                    merged_successors.add(scc_map[succ])
            continue

        group = scc_map[n]
        merged_dependencies.setdefault(group, set())
        for succ in condensed.successors(n):
            if succ in type_scc_set:
                merged_dependencies[group].add(merged_group)
            else:
                merged_dependencies[group].add(scc_map[succ])

    merged_dependencies.setdefault(merged_group, set())
    merged_dependencies[merged_group].update(merged_successors)

    return {
        group: sorted(successors, key=symbol_lexical_key_fn)
        for group, successors in merged_dependencies.items()
    }


def get_feature_defines(compile_commands: Path) -> list[str]:
    db = CompilationDatabase.fromDirectory(compile_commands.parent)
    cmds = db.getAllCompileCommands()
    if cmds is None:
        return []

    defines: dict[str, str | None] = {}  # name -> value (None if no value)
    for cmd in cmds:
        args = iter(cmd.arguments)
        for arg in args:
            # Glued "-Dstuff"
            if arg.startswith("-D") and len(arg) > 2:
                macro = arg[2:]
            # Separate ["-D", "stuff"]
            elif arg == "-D":
                macro = next(args, None)
                assert macro is not None, (
                    f"Malformed compile command: -D without value in {cmd.filename}"
                )
            else:
                continue

            name, _, value = macro.partition("=")
            # Implementation-reserved namespace (_[A-Z]...) per the C standard.
            if not (len(name) >= 2 and name[0] == "_" and name[1].isupper()):
                continue

            current_value = value if value else None
            if name in defines and defines[name] != current_value:
                logger.warning(
                    "Feature-test macro %s has conflicting values across TUs: "
                    "%s vs %s (keeping latter)",
                    name,
                    defines[name],
                    current_value,
                )
            defines[name] = current_value

    return [f"#define {n} {v}" if v else f"#define {n}" for n, v in defines.items()]


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
    for i in range(len(cmds)):
        cmd = cmds[i]
        logger.info(f"Parsing TU {cmd.filename} ({i + 1}/{len(cmds)}) ...")
        try:
            tu = TranslationUnit.from_source(None, args=list(cmd.arguments))
        except TranslationUnitLoadError as e:
            raise TranslationUnitLoadError(
                f"Error parsing '{cmd.filename}' using args `{' '.join(cmd.arguments)}`\n{e}"
            )
        if any(d.severity >= Diagnostic.Error for d in tu.diagnostics):
            raise TranslationUnitLoadError(
                "\n".join(
                    [d.format() for d in tu.diagnostics]
                    + [f"Error parsing '{cmd.filename}' using args `{' '.join(cmd.arguments)}`"]
                )
            )
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

            if symbol.kind == CursorKind.STRUCT_DECL:
                spelling = "struct " + spelling
            if symbol.kind == CursorKind.UNION_DECL:
                spelling = "union " + spelling
            if symbol.kind == CursorKind.ENUM_DECL:
                spelling = "enum " + spelling

            # Save this symbol if we haven't seen it before
            if spelling not in seen:
                seen[spelling] = symbol
            # Or replace it if it's a definition and existing symbol is a declaration
            elif symbol.is_definition and not seen[spelling].is_definition:
                seen[spelling] = symbol
        for spelling, sym in seen.items():
            symbols_with_spelling.setdefault(spelling, []).append(sym)

    # Find symbols with common spelling but different definitions across ASTs.
    # Group definitions by code to avoid O(n^2) pairwise comparison.
    tu_renames: dict[TranslationUnit, dict[str, str]] = {}
    used_spellings = set(symbols_with_spelling.keys())
    for spelling, symbols in symbols_with_spelling.items():
        # Only definitions and variables can conflict
        definitions = [s for s in symbols if s.is_definition or s.is_variable]
        if len(definitions) <= 1:
            continue

        # Group definitions by their code text - identical code means no conflict
        code_groups: dict[str, list[Symbol]] = {}
        for sym in definitions:
            code_groups.setdefault(sym.code.text, []).append(sym)
        if len(code_groups) <= 1:
            continue

        # Multiple distinct definitions exist - rename any symbol that can safely be
        # renamed. Only true linker symbols (global functions and global variables) must
        # preserve their spelling across TUs. Struct/union/enum tags and typedefs have
        # no linker visibility in C, so they can differ freely between TUs. However,
        # clang reports EXTERNAL linkage for all of these — including anonymous tags that
        # inherit the name of their enclosing typedef — so we cannot rely on is_global
        # to filter them out and must check the cursor kind explicitly.
        NON_LINKED_KINDS = (
            CursorKind.STRUCT_DECL,
            CursorKind.UNION_DECL,
            CursorKind.ENUM_DECL,
            CursorKind.TYPEDEF_DECL,
        )
        for sym in definitions:
            if sym.is_system:
                continue
            if sym.is_global and sym.parent is None and sym.kind not in NON_LINKED_KINDS:
                continue

            path = sym.source_path
            new_spelling = mangle(path.stem) + "_" + sym.spelling
            while new_spelling in used_spellings:
                path = path.parent
                new_spelling = mangle(path.stem) + "_" + new_spelling
            used_spellings.add(new_spelling)
            tu_renames.setdefault(sym.cursor.translation_unit, {})[sym.name] = new_spelling
    if not tu_renames:
        return {}

    # Check that renaming won't cause clashes with existing symbols with the same spelling
    existing_spellings = set(symbols_with_spelling.keys())
    for renames in tu_renames.values():
        new_spellings = set(renames.values())
        if existing_spellings.intersection(new_spellings):
            raise NotImplementedError(
                "Renaming symbols would cause clashes with existing symbols with the same spelling!\n"
                f"Clashing: {existing_spellings.intersection(new_spellings)}"
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
    list_of_symbols: list[dict[str, Symbol]], ast_order: dict[Path, TreeResult] | None = None
) -> dict[str, Symbol]:
    ast_order = ast_order or {}
    ast_rank: dict[Path, int] = {path: i for i, path in enumerate(ast_order)}
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

            global_source = global_symbols[name].source_path
            symbol_source = symbol.source_path

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
            # Prefer non-extern variable declaration over extern one (e.g. tentative definition)
            elif (
                symbol.cursor.kind == CursorKind.VAR_DECL
                and global_symbols[name].cursor.storage_class == StorageClass.EXTERN
                and symbol.cursor.storage_class != StorageClass.EXTERN
            ):
                global_symbols[name] = symbol
            # Never replace a non-extern variable with an extern one
            elif (
                global_symbols[name].cursor.kind == CursorKind.VAR_DECL
                and global_symbols[name].cursor.storage_class != StorageClass.EXTERN
                and symbol.cursor.storage_class == StorageClass.EXTERN
            ):
                continue
            # Or prefer the symbol with source priority
            elif global_source in ast_order and symbol_source not in ast_order:
                continue
            elif global_source not in ast_order and symbol_source in ast_order:
                global_symbols[name] = symbol
            elif (
                global_source in ast_order
                and symbol_source in ast_order
                and ast_rank[global_source] > ast_rank[symbol_source]
            ):
                global_symbols[name] = symbol
            elif (
                global_source in ast_order
                and symbol_source in ast_order
                and ast_rank[global_source] < ast_rank[symbol_source]
            ):
                continue
            else:
                # Two symbols have similar names but different declarations or definitions and no source priority!
                raise RuntimeError(
                    f"Unable to handle symbol {name} with multiple different definitions and unknown source priority!\nSymbol found in {global_source} and {symbol_source}."
                )
    return global_symbols


def _main(cfg: ConsolidateConfig):
    if LARGE_PROJECT:
        logger.info("LARGE_PROJECT mode enabled: consolidation is disabled!")
        return

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
