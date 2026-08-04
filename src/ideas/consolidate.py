#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import os
import sys
import json
import logging
import textwrap
from pathlib import Path
from dataclasses import dataclass
from graphlib import TopologicalSorter, CycleError
from concurrent.futures import ProcessPoolExecutor

import hydra
import networkx as nx
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from clang.cindex import CompilationDatabase, CompileCommand, TranslationUnit, CursorKind
from clang.cindex import TranslationUnitLoadError, Diagnostic, StorageClass

from ideas.tools import run_subprocess, Crate
from ideas.ast_rust import mangle as mangle_rs
from ideas.ast import extract_info_c, TreeResult, Symbol, clang_rename, mangle, CodeC
from ideas.ast import SymbolName, SymbolGroup, create_symbol_lexical_key_fn
from ideas.ast import create_symbol_ordering_key_fn

logger = logging.getLogger("ideas.consolidate")

SymbolSpelling = str


@dataclass
class ConsolidateConfig:
    cargo_toml: Path = MISSING
    vcs: str = "none"
    template: str = "bin"

    compile_commands: Path = MISSING
    links: Path = MISSING
    include_line_directives: bool = True


cs = ConfigStore.instance()
cs.store(name="consolidate", node=ConsolidateConfig)


def analyze(
    compile_commands: Path, source_priority: list[Path]
) -> tuple[dict[SymbolName, Symbol], list[SymbolGroup]]:
    # Get symbol table and dependencies taking into account source priority
    asts = get_asts(compile_commands, source_priority)
    ast_order = create_ast_order(source_priority, asts)
    symbols, dependencies = get_symbols_and_dependencies(
        asts, ast_order=ast_order, filter_system_symbols=False
    )

    # Sort symbols in topological order, ordering types before functions and simple types
    # before complex ones so the consolidated source matches the translation order.
    symbol_ordering_key = create_symbol_ordering_key_fn(symbols, ast_order)
    sorted_symbol_groups: list[SymbolGroup] = list(
        nx.lexicographical_topological_sort(
            nx.from_dict_of_lists(dependencies, create_using=nx.DiGraph).reverse(copy=False),  # type: ignore
            key=symbol_ordering_key,
        )
    )
    return symbols, sorted_symbol_groups


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


def get_symbols_and_dependencies(
    asts: list[TreeResult],
    external_symbol_names: list[SymbolName] | None = None,
    ast_order: dict[Path, TreeResult] | None = None,
    filter_system_symbols: bool = True,
) -> tuple[dict[SymbolName, Symbol], dict[SymbolGroup, list[SymbolGroup]]]:
    list_of_symbols: list[dict[SymbolName, Symbol]] = [ast.symbols for ast in asts]
    if filter_system_symbols:
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
            and not is_system_symbol(symbol)
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


def get_asts(compile_commands: Path, valid_paths: list[Path]) -> list[TreeResult]:
    assert compile_commands.name == "compile_commands.json"
    db = CompilationDatabase.fromDirectory(compile_commands.parent)
    cmds = db.getAllCompileCommands()
    if cmds is None or len(cmds) == 0:
        return []

    valid_paths_set = {path.resolve() for path in valid_paths}
    source_root = Path(
        os.path.commonpath([Path(cmd.filename).resolve().parent for cmd in cmds])
    )
    maybe_asts = _parse_asts_parallel(
        [(cmd.filename, _get_args(cmd, source_root), valid_paths_set) for cmd in cmds]
    )
    asts = [ast for ast in maybe_asts if ast is not None]
    if tu_renames := _get_conflicting_symbols(asts):
        asts = _apply_renames(asts, tu_renames)
    return asts


def _get_args(cmd: CompileCommand, source_root: Path) -> list[str]:
    args = list(cmd.arguments)

    # Make `__FILE__` expand relative to the project root rather than an absolute path.
    # A degenerate root (e.g. "/") would only strip the leading slash, so skip it.
    if source_root.parent != source_root:
        args.append(f"-fmacro-prefix-map={source_root.as_posix()}/=")

    return args


def _apply_renames(
    asts: list[TreeResult], tu_renames: dict[Path, dict[SymbolName, SymbolSpelling]]
) -> list[TreeResult]:
    ast_paths: dict[Path, TreeResult] = {}
    for ast in asts:
        assert ast.filename is not None
        ast_paths[Path(ast.filename).resolve()] = ast

    # Collect raw edits from each renamed TU parsed against original on-disk sources.
    # Parsing without any modified_sources avoids poisoning the header view seen by
    # subsequent TUs before all renames have been computed.  Since _get_conflicting_symbols
    # assigns a single canonical new spelling per USR, the same token in a shared header
    # will produce identical (offset, replacement) pairs from every TU that includes it,
    # so merging edit dicts with update() is safe.
    merged_edits: dict[Path, dict[tuple[int, int], bytes]] = {}
    for tu_path, renames in tu_renames.items():
        ast = ast_paths[tu_path]
        assert ast.filename is not None
        assert ast.arguments is not None
        tu = _get_tu(ast.filename, ast.arguments)
        for file_path, edits in clang_rename(tu, renames).items():
            file_edits = merged_edits.setdefault(file_path, {})
            for extent, replacement in edits.items():
                existing = file_edits.get(extent)
                if existing is not None and existing != replacement:
                    raise ValueError(
                        f"Conflicting rename edits for {file_path} at {extent}: {existing!r} vs {replacement!r}"
                    )
                file_edits[extent] = replacement

    # Apply all merged edits once to produce the final in-memory sources.
    modified_sources: dict[Path, bytes] = {}
    for file_path, edits in merged_edits.items():
        source = file_path.read_bytes()
        for (start, end), replacement in sorted(
            edits.items(), key=lambda e: e[0][0], reverse=True
        ):
            source = source[:start] + replacement + source[end:]
        modified_sources[file_path] = source

    # Re-parse ALL TUs (not just renamed ones) with modified_sources so that TUs
    # that only include a renamed header also get updated ASTs.
    reparsed = _parse_asts_parallel(
        [
            (ast.filename, ast.arguments, set(), modified_sources)
            for ast in ast_paths.values()
            if ast.filename is not None and ast.arguments is not None
        ]
    )
    updated: dict[Path, TreeResult] = {}
    for ast, ast_reparsed in zip(ast_paths.values(), reparsed):
        assert ast.filename is not None
        tu_path = Path(ast.filename).resolve()
        updated[tu_path] = ast_reparsed if ast_reparsed is not None else ast
    return list(updated.values())


def _parse_asts_parallel(args: list[tuple]) -> list[TreeResult | None]:
    if len(args) <= 1:
        return [_get_ast(*a) for a in args]
    with ProcessPoolExecutor() as pool:
        futures = [pool.submit(_get_ast, *a) for a in args]
        return [f.result() for f in futures]


def _get_tu(
    filename: str, arguments: list[str], unsaved_files: dict[Path, bytes] | None = None
) -> TranslationUnit:
    try:
        uf = [(str(p), content) for p, content in (unsaved_files or {}).items()] or None
        tu = TranslationUnit.from_source(None, args=arguments, unsaved_files=uf)
        if any(d.severity >= Diagnostic.Error for d in tu.diagnostics):
            raise TranslationUnitLoadError("\n".join(d.format() for d in tu.diagnostics))
    except TranslationUnitLoadError as e:
        raise TranslationUnitLoadError(
            f"Failed to parse '{filename}' with compile arguments:\n"
            f"  {' '.join(arguments)}\n\n"
            f"{e}"
        )
    return tu


def _get_ast(
    filename: str,
    arguments: list[str],
    valid_paths: set[Path],
    unsaved_files: dict[Path, bytes] | None = None,
) -> TreeResult | None:
    logger.info(f"Parsing {filename} ...")
    tu = _get_tu(filename, arguments, unsaved_files)
    assert tu.cursor is not None
    source_path = Path(tu.cursor.spelling).resolve()
    if valid_paths and source_path not in valid_paths:
        return None
    tree = extract_info_c(tu)
    tree.filename = filename
    tree.arguments = arguments
    return tree


def _get_conflicting_symbols(
    asts: list[TreeResult],
) -> dict[Path, dict[SymbolName, SymbolSpelling]]:
    # Gather best representative symbol per spelling per AST into a single dict
    symbols_with_spelling: dict[SymbolSpelling, list[Symbol]] = {}
    for ast in asts:
        seen: dict[SymbolSpelling, Symbol] = {}
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
    tu_renames: dict[Path, dict[SymbolName, SymbolSpelling]] = {}
    used_spellings = set(symbols_with_spelling.keys())
    for ast in asts:
        used_spellings.update(ast.local_names)
    new_spellings: dict[tuple[Path, SymbolSpelling], SymbolSpelling] = {}
    for spelling, symbols in symbols_with_spelling.items():
        # Only definitions and variables can conflict
        definitions = [s for s in symbols if s.is_definition or s.is_variable]
        if len(definitions) <= 1:
            continue

        # If there is only one unique presumed path, then nothing to rename
        if len({sym.presumed_path or sym.tu_path for sym in definitions}) <= 1:
            continue

        # Byte-identical definitions are one entity redeclared, not a conflict
        if len({str(sym.code) for sym in definitions}) <= 1:
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

            path = sym.presumed_path or sym.tu_path
            if (path, sym.spelling) in new_spellings:
                new_spelling = new_spellings[(path, sym.spelling)]
            else:
                new_spelling = mangle(path.stem) + "_" + sym.spelling
                while new_spelling in used_spellings:
                    path = path.parent
                    new_spelling = mangle(path.stem) + "_" + new_spelling
                used_spellings.add(new_spelling)
                new_spellings[(path, sym.spelling)] = new_spelling
            tu_renames.setdefault(sym.tu_path, {})[sym.name] = new_spelling
    return tu_renames


def merge_symbols(
    list_of_symbols: list[dict[SymbolName, Symbol]],
    ast_order: dict[Path, TreeResult] | None = None,
) -> dict[SymbolName, Symbol]:
    ast_order = ast_order or {}
    ast_rank: dict[Path, int] = {path: i for i, path in enumerate(ast_order)}
    global_symbols: dict[SymbolName, Symbol] = {}
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


def consolidate(
    symbols: dict[SymbolName, Symbol],
    symbol_order: list[SymbolGroup],
    include_line_directives: bool = True,
) -> CodeC:
    # Consolidate C sources keyed by raw snippet with optional line directive.
    sources: dict[CodeC, str | None] = {}
    for group in symbol_order:
        # Add forward declarations if more than one symbol in group
        if len(group) > 1:
            for name in group:
                symbol = symbols[name]
                declaration = symbol.declaration
                if declaration is None:
                    continue
                if declaration in sources:
                    continue
                sources[declaration] = None
                if include_line_directives and symbol.declaration_line_directive is not None:
                    sources[declaration] = str(symbol.declaration_line_directive)

        # Add symbol code
        for name in group:
            symbol = symbols[name]
            code = symbol.code
            if code in sources:
                continue
            sources[code] = None
            if include_line_directives and symbol.line_directive is not None:
                sources[code] = str(symbol.line_directive)

    return CodeC.join(
        snippet if directive is None else CodeC(directive + str(snippet))
        for snippet, directive in sources.items()
    )


def _generate_build_rs(extra_link_libs: list[str]) -> str:
    body_lines = [
        'println!("cargo:rerun-if-changed=src/lib.c");',
        'let ubsan = std::env::var("CARGO_FEATURE_CC_UBSAN").is_ok();',
        'let asan = std::env::var("CARGO_FEATURE_CC_ASAN").is_ok();',
        'let coverage = std::env::var("CARGO_FEATURE_CC_COVERAGE").is_ok();',
        "assert!(",
        "    [ubsan, asan, coverage].iter().filter(|&&x| x).count() <= 1,",
        '    "features `ubsan`, `asan`, and `coverage` are mutually exclusive"',
        ");",
        "let mut build = cc::Build::new();",
        "build",
        '    .compiler("clang")',
        "    .warnings(false)",
        '    .file("src/lib.c");',
        "if ubsan {",
        '    build.flag("-fsanitize=undefined,nullability")',
        '        .flag("-fno-sanitize-recover=all");',
        "}",
        "",
        "if asan {",
        '    build.flag("-fsanitize=address")',
        '        .flag("-fno-sanitize-recover=all");',
        "}",
        "",
        "if coverage {",
        '    build.flag("-fprofile-instr-generate")',
        '        .flag("-fcoverage-mapping");',
        "}",
        'build.compile("library");',
        "",
        "if ubsan {",
        '    println!("cargo:rustc-link-search=/usr/lib/llvm-21/lib/clang/21/lib/linux/");',
        '    println!("cargo:rustc-link-lib=static=clang_rt.ubsan_standalone-x86_64");',
        "}",
        "if asan {",
        '    println!("cargo:rustc-link-search=/usr/lib/llvm-21/lib/clang/21/lib/linux/");',
        '    println!("cargo:rustc-link-lib=static=clang_rt.asan-x86_64");',
        "}",
    ]
    for lib in extra_link_libs:
        body_lines.append(f'println!("cargo:rustc-link-lib=dylib={lib}");')
    body = textwrap.indent("\n".join(body_lines), "    ")
    return f"fn main() {{\n{body}\n}}\n"


def _generate_bindings(symbols: dict[SymbolName, Symbol], c_src_path: Path) -> str:
    allowed_functions = [
        mangle_rs(s.spelling)
        for s in symbols.values()
        if s.is_global and s.is_function and s.is_definition and not is_system_symbol(s)
    ]
    allowed_variables = [
        mangle_rs(s.spelling)
        for s in symbols.values()
        if s.is_global and s.is_variable and not is_system_symbol(s)
    ]
    return _bindgen(
        c_src_path,
        allowlist_functions=allowed_functions,
        allowlist_vars=allowed_variables,
    )


def _bindgen(
    c_src_path: Path,
    allowlist_functions: list[str],
    allowlist_vars: list[str],
) -> str:
    cmd = [
        "bindgen",
        "--disable-header-comment",
        "--no-doc-comments",
        "--no-layout-tests",
        "--merge-extern-blocks",
    ]
    for fn in allowlist_functions:
        cmd += ["--allowlist-function", fn]
    for var in allowlist_vars:
        cmd += ["--allowlist-var", var]
    cmd.append(str(c_src_path))
    logger.info(f"Running `{' '.join(cmd)}` ...")

    ok, bindings, error, _ = run_subprocess(cmd)
    if not ok:
        raise ValueError(f"`{' '.join(cmd)}` failed!\n{bindings + error}")
    return bindings


def _main(cfg: ConsolidateConfig):
    # Create fresh Cargo.toml so cargo init always runs and registers the crate in workspace.members
    crate = Crate(cfg.cargo_toml, vcs=cfg.vcs, template=cfg.template, reinit=True)  # type: ignore[reportArgumentType]
    # Binary -sys crates always have a lib.rs file with bindings
    if cfg.template == "bin":
        assert crate.main_src_path is not None, "Expected main.rs to exist in -sys crate!"
        (crate.main_src_path.parent / "lib.rs").touch()
        crate.invalidate_metadata()

    # Read source priority and link libs from the links file.
    links_data = json.loads(cfg.links.read_text())
    source_priority: list[Path] = []
    extra_link_libs: list[str] = []
    for entry in links_data.get("entries", []):
        if "source" in entry:
            source_priority.append(Path(entry["source"]).resolve())
        elif "lib" in entry:
            extra_link_libs.append(entry["lib"])

    symbols, symbol_order = analyze(cfg.compile_commands, source_priority)
    logger.info(
        f"Found {len(symbols)} symbols and {len(symbol_order)} groups in {cfg.compile_commands}!"
    )

    assert crate.lib_src_path is not None, "Expected lib.rs to exist in -sys crate!"
    c_src_path = crate.lib_src_path.with_suffix(".c")

    # Consolidate and write C code to disk
    c_src = consolidate(symbols, symbol_order, cfg.include_line_directives)
    c_src_path.parent.mkdir(exist_ok=True, parents=True)
    c_src_path.write_text(str(c_src))

    # Write build.rs to disk with dependencies and the sanitizer/coverage config
    (crate.cargo_toml.parent / "build.rs").write_text(_generate_build_rs(extra_link_libs))
    crate.cargo_add("cc@1.2.53", section="build")
    crate.cargo_feature(cc_ubsan=[], cc_asan=[], cc_coverage=[])

    # Write bindings to disk
    crate.lib_src_path.write_text(_generate_bindings(symbols, c_src_path))
    if crate.main_src_path is not None:
        assert crate.lib_name is not None, "Expected a library target in the -sys crate!"
        crate.main_src_path.write_text(f"#![no_main]\nuse {crate.lib_name}::*;\n")

    # Write cargo configurations to disk
    # NOTE: We configure nextest for both the crate and the workspace such that the -sys crate is standalone
    crate.cargo_nextest_config(crate.cargo_toml.parent / ".config" / "nextest.toml")
    crate.cargo_nextest_config(crate.workspace_root / ".config" / "nextest.toml")

    # Commit the crate
    crate.vcs.add(crate.cargo_toml.parent)
    workspace_cargo_toml = crate.workspace_root / "Cargo.toml"
    if workspace_cargo_toml != crate.cargo_toml and workspace_cargo_toml.exists():
        crate.vcs.add(workspace_cargo_toml)
    crate.vcs.commit(f"Created C bindings crate '{crate.name}'")


@hydra.main(version_base=None, config_name="consolidate")
def main(cfg: ConsolidateConfig):
    try:
        _main(cfg)
    except Exception as e:
        logger.exception(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
