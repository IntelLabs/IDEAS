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
from ideas.ast import create_symbol_ordering_key_fn, clang_make_weak_
from ideas.agents.utils import write_instrumentation_script

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


def get_symbols_and_dependencies(
    asts: list[TreeResult],
    external_symbol_names: list[SymbolName] | None = None,
    ast_order: dict[Path, TreeResult] | None = None,
    filter_system_symbols: bool = True,
) -> tuple[dict[SymbolName, Symbol], dict[SymbolGroup, list[SymbolGroup]]]:
    list_of_symbols: list[dict[SymbolName, Symbol]] = [ast.symbols for ast in asts]
    if filter_system_symbols:
        list_of_symbols = [
            {name: symbol for name, symbol in symbols.items() if not symbol.is_system}
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
            if symbol.is_externally_visible
            and (symbol.is_variable or (symbol.is_function and symbol.is_definition))
            and not symbol.is_system
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


def _agreeing_types(
    symbols_with_spelling: dict[SymbolSpelling, list[Symbol]],
    type_references: dict[SymbolSpelling, set[SymbolSpelling]],
) -> set[SymbolSpelling]:
    # A tag spelling carries its keyword, so only a typedef can share a key with a function
    # or a variable. Judging that key by anything but its type symbols would let an
    # unrelated function decide what the typedef denotes.
    types_with_spelling = {
        spelling: type_symbols
        for spelling, symbols in symbols_with_spelling.items()
        if (type_symbols := [sym for sym in symbols if sym.is_type])
    }
    # Start from the types whose text agrees across TUs, then withdraw any that is written
    # in terms of one that does not, until nothing changes: disagreement travels up the
    # chain, so `typedef struct parser parser;` stops naming one type the moment the two
    # `struct parser` completions differ. Only definitions are compared, because a TU that
    # leaves a tag incomplete says nothing about what it is.
    agreeing = {
        spelling
        for spelling, symbols in types_with_spelling.items()
        if len({str(sym.code) for sym in symbols if sym.is_definition}) <= 1
    }
    # A spelling the corpus completes at most once has a single reading whatever it is
    # written in terms of, so there is no second reading for disagreement to travel to.
    settled = {
        spelling
        for spelling, symbols in types_with_spelling.items()
        if sum(sym.is_definition for sym in symbols) <= 1
    }
    while withdrawn := {
        s for s in agreeing - settled if not type_references.get(s, set()) <= agreeing
    }:
        agreeing -= withdrawn
    return agreeing


def _denotes_one_entity(definitions: list[Symbol], agreeing_types: set[SymbolSpelling]) -> bool:
    # Identical text is never proof on its own, because text carries no context: what it
    # means depends on what the names in it resolve to, and that is decided per TU.
    if len({str(sym.code) for sym in definitions}) > 1:
        return False

    symbol = definitions[0]

    # A type has no linkage to appeal to, so its identity is structural
    if symbol.is_type:
        return symbol.qualified_spelling in agreeing_types

    # Only linkage joins separately written functions and variables into one entity, and
    # internal linkage means each TU owns its own copy
    if symbol.kind in (CursorKind.FUNCTION_DECL, CursorKind.VAR_DECL):
        return all(sym.is_externally_visible and sym.is_top_level for sym in definitions)

    return True


def _get_conflicting_symbols(
    asts: list[TreeResult],
) -> dict[Path, dict[SymbolName, SymbolSpelling]]:
    # Gather best representative symbol per spelling per AST into a single dict
    symbols_with_spelling: dict[SymbolSpelling, list[Symbol]] = {}
    # Pooled across TUs, because a type only has to disagree in one of them to be two types
    type_references: dict[SymbolSpelling, set[SymbolSpelling]] = {}
    for ast in asts:
        seen: dict[SymbolSpelling, Symbol] = {}
        for usr, symbol in ast.symbols.items():
            if not symbol.spelling:
                continue
            spelling = symbol.qualified_spelling

            # Save this symbol if we haven't seen it before, or replace it if it's a
            # definition and the existing symbol is a declaration
            if spelling not in seen or (
                symbol.is_definition and not seen[spelling].is_definition
            ):
                seen[spelling] = symbol

            if symbol.is_type:
                type_references.setdefault(spelling, set()).update(
                    ast.symbols[dep].qualified_spelling
                    for dep in ast.complete_graph.get(usr, ())
                    if ast.symbols[dep].is_type
                )
        for spelling, sym in seen.items():
            symbols_with_spelling.setdefault(spelling, []).append(sym)

    agreeing_types = _agreeing_types(symbols_with_spelling, type_references)

    # Find symbols with common spelling but different definitions across ASTs.
    # Group definitions by code to avoid O(n^2) pairwise comparison.
    tu_renames: dict[Path, dict[SymbolName, SymbolSpelling]] = {}
    used_spellings = {sym.spelling for syms in symbols_with_spelling.values() for sym in syms}
    for ast in asts:
        used_spellings.update(ast.local_names)
    new_spellings: dict[tuple[Path, SymbolSpelling], SymbolSpelling] = {}
    for symbols in symbols_with_spelling.values():
        # Only definitions and variables can conflict
        definitions = [s for s in symbols if s.is_definition or s.is_variable]
        if len(definitions) <= 1:
            continue

        # If there is only one unique presumed path, then nothing to rename
        if len({sym.presumed_path or sym.tu_path for sym in definitions}) <= 1:
            continue

        # One entity redeclared in several TUs is not a conflict
        if _denotes_one_entity(definitions, agreeing_types):
            continue

        # Distinct entities share a spelling, so rename the ones that can be renamed. A
        # global function or variable is a linker symbol and has to keep its spelling; a
        # type has no linker presence, so each TU may spell it however it likes.
        for sym in definitions:
            if sym.in_system_header:
                continue
            if sym.is_externally_visible and sym.is_top_level and not sym.is_type:
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
            # A synthesized declaration has no source line of its own to point at.
            for name in group:
                symbol = symbols[name]
                declaration = symbol.forward_declaration
                if declaration is None:
                    continue
                if declaration in sources:
                    continue
                sources[declaration] = None

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


def _generate_build_rs(link_inputs: list[tuple[str, str]], link_search_dirs: list[str]) -> str:
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
        '    build.flag("-fsanitize=address,pointer-compare")',
        '        .flag("-ftrivial-auto-var-init=pattern")',
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
    # rustc-link-lib cannot express a path, so a library the C build named by absolute path
    # has to be re-resolved as `-l:<file>` against a search dir added for its own directory
    search_dirs = link_search_dirs + [
        str(Path(value).parent) for kind, value in link_inputs if kind == "lib_path"
    ]
    for search_dir in dict.fromkeys(search_dirs):
        body_lines.append(f'println!("cargo:rustc-link-search=native={search_dir}");')
    for kind, value in link_inputs:
        spec = f"dylib:+verbatim={Path(value).name}" if kind == "lib_path" else f"dylib={value}"
        body_lines.append(f'println!("cargo:rustc-link-lib={spec}");')
    body = textwrap.indent("\n".join(body_lines), "    ")
    return f"fn main() {{\n{body}\n}}\n"


def _generate_bindings(symbols: dict[SymbolName, Symbol], c_src_path: Path) -> str:
    allowed_functions = [
        mangle_rs(s.spelling)
        for s in symbols.values()
        if s.is_externally_visible and s.is_function and s.is_definition and not s.is_system
    ]
    allowed_variables = [
        mangle_rs(s.spelling)
        for s in symbols.values()
        if s.is_externally_visible and s.is_variable and not s.is_system
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
    link_inputs: list[tuple[str, str]] = []
    link_search_dirs: list[str] = []
    for entry in links_data.get("entries", []):
        if "source" in entry:
            source_priority.append(Path(entry["source"]).resolve())
        elif "lib" in entry:
            link_inputs.append(("lib", entry["lib"]))
        elif "lib_path" in entry:
            link_inputs.append(("lib_path", entry["lib_path"]))
        elif "search_dir" in entry:
            link_search_dirs.append(entry["search_dir"])

    symbols, symbol_order = analyze(cfg.compile_commands, source_priority)
    logger.info(
        f"Found {len(symbols)} symbols and {len(symbol_order)} groups in {cfg.compile_commands}!"
    )

    assert crate.lib_src_path is not None, "Expected lib.rs to exist in -sys crate!"
    c_src_path = crate.lib_src_path.with_suffix(".c")

    # Consolidate and write C code to disk, then check that it compiles
    c_src = consolidate(symbols, symbol_order, cfg.include_line_directives)
    c_src_path.parent.mkdir(exist_ok=True, parents=True)
    c_src_path.write_text(str(c_src))
    ok, _, error, _ = run_subprocess(["clang", "-fsyntax-only", "-w", str(c_src_path)])
    if not ok:
        raise ValueError(f"Consolidated C in {c_src_path} does not compile!\n{error}")

    if cfg.template == "bin":
        # The -sys bin target is `#![no_main]`, so C must own the entrypoint, and until `main`
        # is wrapped it has to stay weak to avoid colliding with the libtest harness `main`
        assert any(sym.spelling == "main" and sym.is_definition for sym in symbols.values()), (
            f"Expected a `main` definition in {cfg.compile_commands}!"
        )
        clang_make_weak_(c_src_path, "main")

        # Add dev dependencies for driving the binary through its entrypoint
        crate.cargo_add(dep="assert_cmd@2.0.17", section="dev")
        crate.cargo_add(dep="predicates@3.1.3", section="dev")

    # Add dev dependencies common to every -sys crate
    crate.cargo_add(dep="libc@0.2", section="dev")
    crate.cargo_add(dep="insta@1.48.0", section="dev", features=["json"])
    crate.cargo_add(dep="serde@1", section="dev", features=["derive"])
    crate.cargo_add(dep="serde_json@1", section="dev")
    crate.cargo_add(dep="walkdir@2", section="dev")

    # Write build.rs to disk with dependencies and the sanitizer/coverage config
    (crate.cargo_toml.parent / "build.rs").write_text(
        _generate_build_rs(link_inputs, link_search_dirs)
    )
    crate.cargo_add("cc@1.2.53", section="build")
    crate.cargo_feature(cc_ubsan=[], cc_asan=[], cc_coverage=[])

    # Write bindings to disk
    crate.lib_src_path.write_text(_generate_bindings(symbols, c_src_path))
    if crate.main_src_path is not None:
        assert crate.lib_name is not None, "Expected a library target in the -sys crate!"
        crate.main_src_path.write_text(f"#![no_main]\nuse {crate.lib_name}::*;\n")
        # `#![no_main]` suppresses the libtest harness `main` too, so a `--test` build of this
        # bin target is just the C entrypoint; nextest would then choke on `--list`
        crate.configure_target("bin", name=crate.name, test=False, doctest=False)

    # Write cargo configurations to disk
    # NOTE: We configure nextest for both the crate and the workspace such that the -sys crate is standalone
    crate.cargo_nextest_config(crate.cargo_toml.parent / ".config" / "nextest.toml")
    crate.cargo_nextest_config(crate.workspace_root / ".config" / "nextest.toml")
    crate.vcs.add(crate.workspace_root / ".config")

    # Write instrumentation script
    write_instrumentation_script(
        crate.cargo_toml.parent / "instrument.sh", features=["cc_asan", "cc_ubsan"]
    )

    # Commit the crate
    crate.vcs.add(crate.cargo_toml.parent)
    workspace_cargo_toml = crate.workspace_root / "Cargo.toml"
    if workspace_cargo_toml != crate.cargo_toml and workspace_cargo_toml.exists():
        crate.vcs.add(workspace_cargo_toml)
    workspace_cargo_lock = crate.workspace_root / "Cargo.lock"
    if workspace_cargo_lock.exists():
        crate.vcs.add(workspace_cargo_lock)
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
