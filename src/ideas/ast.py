#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
from pathlib import Path
from collections import defaultdict
from collections.abc import Iterable
from functools import cached_property
from dataclasses import dataclass, field

from clang.cindex import TranslationUnit, TranslationUnitLoadError, Diagnostic
from clang.cindex import Cursor, CursorKind, SourceRange, TokenKind
from clang.cindex import PrintingPolicy, PrintingPolicyProperty, LinkageKind
from clang.cindex import conf, SourceLocation
from ctypes import pointer, c_size_t, c_char_p

from .tools import run_subprocess

logger = logging.getLogger("ideas.ast")
FILENAME = "file.c"


@dataclass(frozen=True)
class Symbol:
    name: str
    cursor: Cursor
    parent: Cursor | None = None
    decl: Cursor | None = None

    @property
    def spelling(self) -> str:
        return self.cursor.spelling

    @property
    def kind(self) -> CursorKind:
        return self.cursor.kind

    @property
    def declaration(self) -> str | None:
        return get_cursor_code(self.decl) if self.decl else None

    @property
    def code(self) -> str:
        return get_cursor_code(self.parent or self.cursor, pretty_print=True)

    @property
    def is_definition(self) -> bool:
        return self.cursor.is_definition()

    @property
    def is_variable(self) -> bool:
        return self.cursor.kind == CursorKind.VAR_DECL

    @property
    def is_function(self) -> bool:
        return self.cursor.kind == CursorKind.FUNCTION_DECL

    @property
    def is_global(self) -> bool:
        return self.cursor.linkage == LinkageKind.EXTERNAL

    @property
    def is_system(self) -> bool:
        return self.cursor.location.is_in_system_header

    @cached_property
    def static_translation(self) -> str:
        # FIXME: Handle VAR_DECL via c2rust?
        # Ignore non-containers
        if self.kind not in (
            CursorKind.STRUCT_DECL,
            CursorKind.UNION_DECL,
            CursorKind.ENUM_DECL,
            CursorKind.TYPEDEF_DECL,
        ):
            return ""

        # Ignore anonymous containers
        symbol_name = (self.parent or self.cursor).spelling
        if not symbol_name:
            return ""

        # Generate translation of container
        bindgen = [
            "bindgen",
            "--disable-header-comment",
            "--no-doc-comments",
            "--no-layout-tests",
            "--no-recursive-allowlist",
            "--allowlist-item",
            symbol_name,
            self.cursor.translation_unit.spelling,
        ]
        ok, output, _, _ = run_subprocess(bindgen)
        return output if ok else ""

    def with_declaration(self, decl: Cursor) -> "Symbol":
        return Symbol(self.name, self.cursor, self.parent, decl=decl)


@dataclass
class TreeResult:
    symbols: dict[str, Symbol] = field(default_factory=dict)
    complete_graph: dict[str, list[str]] = field(
        default_factory=lambda: defaultdict(lambda: list())
    )


def create_translation_unit(path_or_code: Path | str) -> TranslationUnit:
    # Parse the code using clang
    if isinstance(path_or_code, str):
        tu = TranslationUnit.from_source(FILENAME, unsaved_files=[(FILENAME, path_or_code)])
    else:
        tu = TranslationUnit.from_source(str(path_or_code.resolve()))
    if any(d.severity >= Diagnostic.Error for d in tu.diagnostics):
        raise TranslationUnitLoadError("\n".join([d.format() for d in tu.diagnostics]))
    return tu


# Traverse the AST, extract symbols and resolve deep references
def extract_info_c(tu: TranslationUnit) -> TreeResult:
    assert tu.cursor is not None
    symbols = extract_symbol_info_c(tu.cursor)
    graph = {
        # Prefer parent over cursor
        name: extract_referenced_symbols(symbol.parent or symbol.cursor, symbols.keys())
        for name, symbol in symbols.items()
    }
    return TreeResult(symbols=symbols, complete_graph=graph)


def extract_symbol_info_c(node: Cursor, parent: Cursor | None = None) -> dict[str, Symbol]:
    symbols: dict[str, Symbol] = {}

    # If enter new scope then exit early
    if node.kind == CursorKind.COMPOUND_STMT:
        return symbols

    # Add declarative nodes to symbols
    usr = node.get_usr()
    # FIXME: Use node.kind.is_declaration()?
    if node.kind in (
        CursorKind.STRUCT_DECL,
        CursorKind.UNION_DECL,
        CursorKind.ENUM_DECL,
        CursorKind.ENUM_CONSTANT_DECL,
        CursorKind.FUNCTION_DECL,
        CursorKind.VAR_DECL,
        CursorKind.TYPEDEF_DECL,
    ):
        symbols[usr] = Symbol(usr, node, parent=parent)

    # Recurse through children and merge them into symbols
    for child_node in node.get_children():
        parent = node if parent is None and node.kind != CursorKind.TRANSLATION_UNIT else parent
        child_symbols = extract_symbol_info_c(child_node, parent=parent)
        for child_name, child_symbol in child_symbols.items():
            if child_name not in symbols:
                # Found a new symbol
                symbols[child_name] = child_symbol
            elif symbols[child_name].is_definition and child_symbol.is_definition:
                # Always keep current definition
                symbols[child_name] = child_symbol
            elif not symbols[child_name].is_definition and child_symbol.is_definition:
                # Previous symbol was a declaration so replace it with new definitional symbol
                symbols[child_name] = child_symbol.with_declaration(symbols[child_name].cursor)
            elif symbols[child_name].is_definition and not child_symbol.is_definition:
                if not symbols[child_name].is_system or not child_symbol.is_system:
                    logger.warning(f"Ignoring declaration after definition of `{child_name}`")
            elif not symbols[child_name].is_definition and not child_symbol.is_definition:
                if not symbols[child_name].is_system or not child_symbol.is_system:
                    logger.warning(f"Ignoring re-declaration of `{child_name}`")
    return symbols


def extract_referenced_symbols(node: Cursor, global_symbols: Iterable[str]) -> list[str]:
    symbol_uses = []

    for child_node in node.walk_preorder():
        # Ignore non-reference symbols
        if child_node.kind not in (
            CursorKind.CALL_EXPR,
            CursorKind.TYPE_REF,
            CursorKind.DECL_REF_EXPR,
        ):
            continue
        # Ignore internal references to, e.g., function parameters
        if child_node.referenced is None:
            continue
        # Ignore references that are not allowed (e.g., not global)
        if child_node.referenced.get_usr() not in global_symbols:
            continue

        symbol_uses.append(child_node.referenced.get_usr())

    return symbol_uses


def get_code_from_tu_range(
    tu: TranslationUnit, source_range: SourceRange, encoding: str = "utf-8"
) -> str:
    assert source_range.start.file == source_range.end.file, (
        f"{source_range.start.file} != {source_range.end.file}"
    )
    conf.lib.clang_getFileContents.restype = c_char_p
    length = pointer(c_size_t())
    code = conf.lib.clang_getFileContents(tu, source_range.start.file, length)
    assert code is not None
    return code[source_range.start.offset : source_range.end.offset].decode(encoding)


def get_cursor_prettyprinted(cursor: Cursor) -> str:
    # Include tag definition when:
    #    node is not struct/enum/union
    #    and any child is a struct/enum/union definition
    CONTAINER_DECL = (CursorKind.STRUCT_DECL, CursorKind.UNION_DECL, CursorKind.ENUM_DECL)
    include_tag_definition = 0
    if (cursor.kind not in CONTAINER_DECL) and any(
        child.kind in CONTAINER_DECL and child.is_definition()
        for child in cursor.get_children()
    ):
        include_tag_definition = 1

    policy = PrintingPolicy.create(cursor)
    policy.set_property(PrintingPolicyProperty.IncludeTagDefinition, include_tag_definition)
    return cursor.pretty_printed(policy).rstrip()


def get_cursor_code(cursor: Cursor, pretty_print: bool = False) -> str:
    if pretty_print:
        code = get_cursor_prettyprinted(cursor)
    else:
        code = get_code_from_tu_range(cursor.translation_unit, cursor.extent)

    # Non-function definitions require statement terminations
    if cursor.kind != CursorKind.FUNCTION_DECL or not cursor.is_definition():
        code += ";"

    return code


def clang_rename_(
    tu: TranslationUnit, renames: dict[str, str], sources: dict[Path, bytes] | None = None
):
    logger.info(
        f"Renaming {len(renames)} symbols in {tu.spelling}: {', '.join(renames.keys())}"
    )
    # Group edits by file path and source offsets because cursor traversal may revisit tokens.
    edits_by_file: dict[Path, dict[tuple[int, int], bytes]] = {}
    assert tu.cursor is not None
    for cursor in tu.cursor.walk_preorder():
        target_usr = cursor.get_usr()
        target_spelling = cursor.spelling

        # If the cursor itself is not a symbol we want to rename, check if it's a reference to one.
        if target_usr not in renames:
            referenced = cursor.referenced
            if referenced is None:
                continue
            target_usr = referenced.get_usr()
            target_spelling = referenced.spelling
            if target_usr not in renames:
                continue
        if not target_spelling:
            continue

        # Record edits for all tokens that match the symbol's spelling and are not in system headers
        for token in _get_tokens(cursor):
            if token.spelling != target_spelling or token.location.is_in_system_header:
                continue
            file_path = Path(token.location.file.name).resolve()
            extent = (token.extent.start.offset, token.extent.end.offset)
            edits_by_file.setdefault(file_path, {})[extent] = renames[target_usr].encode()

    # Apply edits for each file and optionally save the pre-edit source snapshot.
    for file_path, edits in edits_by_file.items():
        if sources is not None and file_path not in sources:
            sources[file_path] = file_path.read_bytes()
        _apply_edits(file_path, edits)


DEFINITION_START_TOKEN = {CursorKind.FUNCTION_DECL: "{", CursorKind.VAR_DECL: "="}


def clang_make_global_(path: Path, spelling: str):
    tu = create_translation_unit(path)
    cursor = _find_cursor(tu, spelling)
    if cursor.kind not in DEFINITION_START_TOKEN:
        raise ValueError(f"Unhandled cursor kind {cursor.kind}!")

    tokens = list(_get_tokens(cursor))
    assert len(tokens) > 0

    edits: dict[tuple[int, int], bytes] = {}

    for i, token in enumerate(tokens):
        # Remove storage specifiers from declaration while preserving offsets
        if token.kind == TokenKind.KEYWORD and token.spelling in ("static", "inline"):
            assert i + 1 < len(tokens), "storage specifier should always come before name"
            start_offset = token.extent.start.offset
            # Use start of next token as end offset to remove any whitespace
            end_offset = tokens[i + 1].extent.start.offset
            edits[(start_offset, end_offset)] = b""

        # Don't change anything after definition start
        elif (
            token.kind == TokenKind.PUNCTUATION
            and token.spelling == DEFINITION_START_TOKEN[cursor.kind]
        ):
            break

    if edits:
        _apply_edits(path, edits)


def clang_make_extern_(path: Path, spelling: str):
    tu = create_translation_unit(path)
    cursor = _find_cursor(tu, spelling)
    # Determine punctuation token to find based on cursor kind (function or variable)
    if cursor.kind not in DEFINITION_START_TOKEN:
        raise ValueError(f"Unhandled cursor kind {cursor.kind}!")

    tokens = list(_get_tokens(cursor))
    assert len(tokens) > 0

    edits: dict[tuple[int, int], bytes] = {}
    is_extern = False
    definition_start_token_idx = None

    for i, token in enumerate(tokens):
        # Remove storage specifiers from declaration while preserving offsets
        if token.kind == TokenKind.KEYWORD and token.spelling in ("static", "inline"):
            assert i + 1 < len(tokens), "storage specifier should always come before name"
            start_offset = token.extent.start.offset
            # Use start of next token as end offset to remove any whitespace
            end_offset = tokens[i + 1].extent.start.offset
            edits[(start_offset, end_offset)] = b""

        # Check if extern keyword already present
        elif token.kind == TokenKind.KEYWORD and token.spelling == "extern":
            is_extern = True

        # Record the first definition-opening token.
        elif (
            definition_start_token_idx is None
            and token.kind == TokenKind.PUNCTUATION
            and token.spelling == DEFINITION_START_TOKEN[cursor.kind]
        ):
            definition_start_token_idx = i
            break

    # Replace definition portion with ';'
    if definition_start_token_idx is not None:
        assert definition_start_token_idx > 0
        # Use end of prior token as end offset to remove any whitespace
        start_pos = tokens[definition_start_token_idx - 1].extent.end.offset
        end_pos = cursor.extent.end.offset
        edits[(start_pos, end_pos)] = b";"

    # Add 'extern ' prefix if not already present
    if not is_extern:
        extern_insert_pos = cursor.extent.start.offset
        edits[(extern_insert_pos, extern_insert_pos)] = b"extern "

    if edits:
        _apply_edits(path, edits)


def _get_tokens(cursor: Cursor):
    # Use get_tokens if it actually returns a non-empty list
    tokens = list(cursor.get_tokens())
    if len(tokens) > 0:
        yield from tokens
        return

    # Ideally we would use cursor.get_tokens() but does not work with macros:
    # https://github.com/llvm/llvm-project/issues/43451
    tu = cursor.translation_unit

    start = cursor.extent.start
    start = SourceLocation.from_position(tu, start.file, start.line, start.column)

    end = cursor.extent.end
    end = SourceLocation.from_position(tu, end.file, end.line, end.column)

    extent = SourceRange.from_locations(start, end)

    yield from tu.get_tokens(extent=extent)


def _find_cursor(tu: TranslationUnit, spelling: str) -> Cursor:
    definition: Cursor | None = None
    declaration: Cursor | None = None

    assert tu.cursor is not None
    for cursor in tu.cursor.walk_preorder():
        if cursor.kind not in (CursorKind.FUNCTION_DECL, CursorKind.VAR_DECL):
            continue
        if cursor.spelling != spelling:
            continue
        if cursor.is_definition():
            definition = cursor
            break
        if declaration is None:
            declaration = cursor

    target = definition or declaration
    if target is None:
        raise ValueError(f"Unable to find function or variable with spelling `{spelling}`")
    return target


def _apply_edits(path: Path, edits: dict[tuple[int, int], bytes]):
    source = path.read_bytes()

    # Apply edits in reverse offset order to preserve validity of remaining offsets
    for (start, end), replacement in sorted(edits.items(), key=lambda e: e[0][0], reverse=True):
        source = source[:start] + replacement + source[end:]

    path.write_bytes(source)
