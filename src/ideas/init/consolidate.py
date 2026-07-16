#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import os
import sys
import logging
from pathlib import Path
from functools import cmp_to_key
from dataclasses import dataclass
from graphlib import TopologicalSorter, CycleError
from concurrent.futures import ProcessPoolExecutor

import hydra
import networkx as nx
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig
from clang.cindex import CompilationDatabase, TranslationUnit, CursorKind
from clang.cindex import TranslationUnitLoadError, Diagnostic, StorageClass

from ideas.ast import extract_info_c, TreeResult, Symbol, clang_rename_, mangle, CodeC
from ideas.tools import Crate, check_c

logger = logging.getLogger("ideas.init.consolidate")


@dataclass
class ConsolidateConfig:
    cargo_toml: Path = MISSING
    vcs: str = "none"

    compile_commands: Path = MISSING
    source_priority: Path | None = None


cs = ConfigStore.instance()
cs.store(name="init.consolidate", node=ConsolidateConfig)


def init(compile_commands: Path, source_priority: list[Path]) -> CodeC:
    # Get symbol table and dependencies taking into account source priority
    asts = get_asts(compile_commands, source_priority)
    ast_order = create_ast_order(source_priority, asts)
    symbols, dependencies = get_symbols_and_dependencies(
        asts, ast_order=ast_order, filter_system_symbols=False
    )
    logger.info(f"Found {len(symbols)} symbols in {compile_commands}!")

    # Sort symbols in lexicographical topological order
    symbol_lexical_key = create_symbol_lexical_key_fn(symbols, ast_order)
    sorted_symbol_groups = list(
        nx.lexicographical_topological_sort(
            nx.from_dict_of_lists(dependencies, create_using=nx.DiGraph).reverse(copy=False),  # type: ignore
            key=symbol_lexical_key,
        )
    )

    # Consolidate C sources keyed by raw snippet with optional line directive.
    sources: dict[CodeC, str | None] = {}
    for group in sorted_symbol_groups:
        # Add forward declarations if more than one symbol in group
        if len(group) > 1:
            for name in group:
                symbol = symbols[name]
                declaration = symbol.declaration
                if declaration is None:
                    continue
                if declaration in sources:
                    continue
                sources[declaration] = (
                    str(symbol.declaration_line_directive)
                    if symbol.declaration_line_directive is not None
                    else None
                )

        # Add symbol code
        for name in group:
            symbol = symbols[name]
            code = symbol.code
            if code in sources:
                continue
            sources[code] = (
                str(symbol.line_directive) if symbol.line_directive is not None else None
            )

    return CodeC.join(
        snippet if directive is None else CodeC(directive + str(snippet))
        for snippet, directive in sources.items()
    )


def get_symbols_and_dependencies(
    asts: list[TreeResult],
    external_symbol_names: list[str] | None = None,
    ast_order: dict[Path, TreeResult] | None = None,
    filter_system_symbols: bool = True,
) -> tuple[dict[str, Symbol], dict[tuple[str, ...], list[tuple[str, ...]]]]:
    list_of_symbols: list[dict[str, Symbol]] = [ast.symbols for ast in asts]
    if filter_system_symbols:

        def is_system_symbol(symbol: Symbol) -> bool:
            if symbol.is_system:
                logger.debug(f"Ignoring system symbol `{symbol.name}`")
                return True
            if symbol.presumed_path is None:
                return False
            if os.path.commonpath([symbol.presumed_path, symbol.tu_path]) == "/":
                logger.debug(f"Ignoring system symbol {symbol.name}")
                return True
            return False

        list_of_symbols = [
            {name: symbol for name, symbol in symbols.items() if not is_system_symbol(symbol)}
            for symbols in list_of_symbols
        ]

    # Merge ASTs into project dependencies
    project_symbols = merge_symbols(list_of_symbols, ast_order)
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
        path = first_symbol.tu_path
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

        a_tu = a_symbol.tu_path
        b_tu = b_symbol.tu_path

        # If symbols are from the same translation unit, compare their
        # preorder traversal indices for lexical ordering.
        if a_tu == b_tu:
            return _cmp_symbol_tu_order(a_symbol, b_symbol)

        if ast_order is None:
            raise RuntimeError(
                f"Cannot compare symbols from different translation units without ast_order: {a} ({a_tu}) vs {b} ({b_tu})."
            )

        # If a's USR appears in b's TU with matching code, both symbols are
        # present in b_tu and can be compared by TU preorder index there.
        b_ast = ast_order.get(b_tu)
        if (
            b_ast is not None
            and a_name in b_ast.symbols
            and b_ast.symbols[a_name].code == a_symbol.code
        ):
            return _cmp_symbol_tu_order(b_ast.symbols[a_name], b_symbol)

        # If b's USR appears in a's TU with matching code, both symbols are
        # present in a_tu and can be compared by TU preorder index there.
        a_ast = ast_order.get(a_tu)
        if (
            a_ast is not None
            and b_name in a_ast.symbols
            and a_ast.symbols[b_name].code == b_symbol.code
        ):
            return _cmp_symbol_tu_order(a_symbol, a_ast.symbols[b_name])

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


def _cmp_symbol_tu_order(symbol_a: Symbol, symbol_b: Symbol) -> int:
    if symbol_a.tu_path != symbol_b.tu_path:
        raise ValueError(
            "Cannot compare TU preorder indices for symbols from different translation units:"
            f" {symbol_a.name} @ {symbol_a.tu_path} vs {symbol_b.name} @ {symbol_b.tu_path}"
        )

    order_a = symbol_a.tu_preorder_index
    order_b = symbol_b.tu_preorder_index
    if order_a < order_b:
        return -1
    if order_b < order_a:
        return 1
    if symbol_a.name != symbol_b.name:
        raise ValueError(
            f"Unable to order distinct symbols with identical lexical priority and location:"
            f" {symbol_a.name} @ {order_a} vs {symbol_b.name} @ {order_b}"
        )
    return 0


def get_asts(
    compile_commands: Path, valid_paths: list[Path], rename_conflicting_symbols: bool = True
) -> list[TreeResult]:
    assert compile_commands.name == "compile_commands.json"
    db = CompilationDatabase.fromDirectory(compile_commands.parent)
    cmds = db.getAllCompileCommands()
    if cmds is None or len(cmds) == 0:
        return []

    valid_paths_set = {path.resolve() for path in valid_paths}
    maybe_asts: list[TreeResult | None]
    if len(cmds) <= 1:
        maybe_asts = [_get_ast(cmds[0].filename, list(cmds[0].arguments), valid_paths_set)]
    else:
        with ProcessPoolExecutor() as pool:
            futures = [
                pool.submit(_get_ast, cmd.filename, list(cmd.arguments), valid_paths_set)
                for cmd in cmds
            ]
            maybe_asts = [future.result() for future in futures]
    asts = [ast for ast in maybe_asts if ast is not None]

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


def _get_ast(filename: str, arguments: list[str], valid_paths: set[Path]) -> TreeResult | None:
    logger.info(f"Parsing {filename} ...")
    try:
        tu = TranslationUnit.from_source(None, args=arguments)
        if any(d.severity >= Diagnostic.Error for d in tu.diagnostics):
            raise TranslationUnitLoadError("\n".join(d.format() for d in tu.diagnostics))
    except TranslationUnitLoadError as e:
        raise TranslationUnitLoadError(
            f"Failed to parse '{filename}' with compile arguments:\n"
            f"  {' '.join(arguments)}\n\n"
            f"{e}"
        )

    assert tu.cursor is not None
    source_path = Path(tu.cursor.spelling).resolve()
    if valid_paths and source_path not in valid_paths:
        return None
    tree = extract_info_c(tu)
    tree.filename = filename
    tree.arguments = arguments
    return tree


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
    tu_renames: dict[Path, dict[str, str]] = {}
    used_spellings = set(symbols_with_spelling.keys())
    for spelling, symbols in symbols_with_spelling.items():
        # Only definitions and variables can conflict
        definitions = [s for s in symbols if s.is_definition or s.is_variable]
        if len(definitions) <= 1:
            continue

        # Group definitions by their code text - identical code means no conflict
        code_groups: dict[CodeC, list[Symbol]] = {}
        for sym in definitions:
            code_groups.setdefault(sym.code, []).append(sym)
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
            if sym.is_global and sym.is_top_level and sym.kind not in NON_LINKED_KINDS:
                continue

            path = sym.tu_path
            new_spelling = mangle(path.stem) + "_" + sym.spelling
            while new_spelling in used_spellings:
                path = path.parent
                new_spelling = mangle(path.stem) + "_" + new_spelling
            used_spellings.add(new_spelling)
            tu_renames.setdefault(sym.tu_path, {})[sym.name] = new_spelling
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
    tu_args: dict[Path, list[str]] = {}
    for ast in asts:
        if ast.filename is None or ast.arguments is None:
            continue
        tu_args[Path(ast.filename).resolve()] = list(ast.arguments)

    original_sources: dict[Path, bytes] = {}
    try:
        for tu_path, renames in tu_renames.items():
            args = tu_args.get(tu_path)
            if args is not None:
                tu = TranslationUnit.from_source(None, args=list(args))
            else:
                tu = TranslationUnit.from_source(str(tu_path))
            clang_rename_(tu, renames, original_sources)
    except Exception:
        # Restore original source code if renaming fails before caller can reparse.
        for path, source in original_sources.items():
            path.write_bytes(source)
        raise
    return original_sources


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

            global_source = global_symbols[name].tu_path
            symbol_source = symbol.tu_path

            # If overwriting a symbol, then prefer one with a definition
            if global_symbols[name].is_definition and not symbol.is_definition:
                continue
            elif not global_symbols[name].is_definition and symbol.is_definition:
                global_symbols[name] = symbol
            # Prefer non-extern variable declaration over extern one (e.g. tentative definition)
            elif (
                symbol.kind == CursorKind.VAR_DECL
                and global_symbols[name].storage_class == StorageClass.EXTERN
                and symbol.storage_class != StorageClass.EXTERN
            ):
                global_symbols[name] = symbol
            # Never replace a non-extern variable with an extern one
            elif (
                global_symbols[name].kind == CursorKind.VAR_DECL
                and global_symbols[name].storage_class != StorageClass.EXTERN
                and symbol.storage_class == StorageClass.EXTERN
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
    output_dir = Path(HydraConfig.get().runtime.output_dir)

    # Get crate information
    crate = Crate(cfg.cargo_toml, vcs=cfg.vcs)  # type: ignore[reportArgumentType]

    source_priority: list[Path] = []
    if cfg.source_priority:
        lines = cfg.source_priority.read_text().splitlines()
        source_priority = [Path(line.strip()).resolve() for line in lines if line.strip()]

    output = init(cfg.compile_commands, source_priority)

    # Only run preprocess, compile, and assemble steps on C code
    compiles, compile_errors = check_c(output, flags=["-c"])

    # Write C code to disk
    crate.c_src_path.parent.mkdir(exist_ok=True, parents=True)
    crate.c_src_path.write_text(str(output))
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
