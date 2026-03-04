#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from clang.cindex import TranslationUnit, Cursor, CursorKind, SourceRange
from clang.cindex import PrintingPolicy, PrintingPolicyProperty, LinkageKind
from clang.cindex import conf
from ctypes import pointer, c_size_t, c_char_p

FILENAME = "file.c"


@dataclass(frozen=True)
class Symbol:
    name: str
    cursor: Cursor
    parent: Cursor | None = None

    @property
    def kind(self) -> CursorKind:
        return self.cursor.kind

    @property
    def code(self):
        return get_cursor_code(self.parent or self.cursor, pretty_print=True)


@dataclass
class TreeResult:
    symbols: dict[str, Symbol] = field(default_factory=dict)
    complete_graph: dict[str, list[str]] = field(
        default_factory=lambda: defaultdict(lambda: list())
    )


def create_translation_unit(code: str) -> TranslationUnit:
    # Parse the code using clang
    tu = TranslationUnit.from_source(FILENAME, unsaved_files=[(FILENAME, code)])
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


def extract_symbol_info_c(node: Cursor) -> dict[str, Symbol]:
    symbols = {}

    # If enter new scope then exit early
    if node.kind == CursorKind.COMPOUND_STMT:
        return symbols

    # Add declarative nodes to symbols
    usr = node.get_usr()
    if node.kind in (
        CursorKind.STRUCT_DECL,
        CursorKind.UNION_DECL,
        CursorKind.ENUM_DECL,
        CursorKind.ENUM_CONSTANT_DECL,
        CursorKind.FUNCTION_DECL,
        CursorKind.VAR_DECL,
        CursorKind.TYPEDEF_DECL,
    ):
        symbols[usr] = Symbol(usr, node)

    # Recurse through children and merge any definitional symbol or unseen symbol
    for child_node in node.get_children():
        child_symbols = extract_symbol_info_c(child_node)
        for child_name, child_symbol in child_symbols.items():
            # Set child's parent
            if node.kind != CursorKind.TRANSLATION_UNIT:
                child_symbol = Symbol(child_symbol.name, child_symbol.cursor, parent=node)
            if child_name not in symbols or child_symbol.cursor.is_definition():
                symbols[child_name] = child_symbol
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


def get_internally_linked_cursors(cursor: Cursor, filter_system: bool = True) -> list[Cursor]:
    statics: dict[str, Cursor] = {}
    for node in cursor.walk_preorder():
        if node.linkage == LinkageKind.INTERNAL:
            statics[node.get_usr()] = node
        elif node.referenced is not None and node.referenced.linkage == LinkageKind.INTERNAL:
            statics[node.referenced.get_usr()] = node.referenced

    # FIXME: Use set when Cursors are hashable
    list_of_statics = list(statics.values())
    if filter_system:
        list_of_statics = [c for c in list_of_statics if not c.location.is_in_system_header]
    return list_of_statics
