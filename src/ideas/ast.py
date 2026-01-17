#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from collections import defaultdict
from dataclasses import dataclass, field

from clang.cindex import TranslationUnit, Cursor, CursorKind, TokenKind, SourceRange
from clang.cindex import PrintingPolicy, PrintingPolicyProperty, LinkageKind
from clang.cindex import conf
from ctypes import pointer, c_size_t, c_char_p

from ideas.utils import Symbol

FILENAME = "file.c"

DECL_NODE_KIND = {
    CursorKind.FUNCTION_DECL,
    CursorKind.TYPEDEF_DECL,
    CursorKind.VAR_DECL,
    CursorKind.STRUCT_DECL,
    CursorKind.UNION_DECL,
    CursorKind.ENUM_DECL,
}

REF_NODE_KIND = {
    CursorKind.CALL_EXPR,
    CursorKind.TYPE_REF,
    CursorKind.DECL_REF_EXPR,
}

DATA_STRUCT_NODE_MAP = {
    CursorKind.STRUCT_DECL,
    CursorKind.UNION_DECL,
    CursorKind.ENUM_DECL,
    CursorKind.ENUM_CONSTANT_DECL,
}


@dataclass
class TreeResult:
    symbols: dict[str, Symbol] = field(default_factory=dict)
    fn_definitions: dict[str, str | None] = field(
        default_factory=lambda: defaultdict(lambda: None)
    )

    complete_graph: dict[str, list[Symbol]] = field(
        default_factory=lambda: defaultdict(lambda: list())
    )


def create_translation_unit(code: str) -> TranslationUnit:
    # Parse the code using clang
    tu = TranslationUnit.from_source(FILENAME, unsaved_files=[(FILENAME, code)])
    return tu


# Traverse the AST, extract symbols and resolve deep references
def extract_info_c(tu: TranslationUnit) -> TreeResult:
    result = TreeResult()
    previous_usr: str = ""
    assert tu.cursor is not None
    for node in tu.cursor.get_children():
        kind, usr = node.kind, node.get_usr()
        # Top-level declaration
        decl_range = get_declaration_range(node)
        decl = get_code_from_tu_range(tu, decl_range)

        match (kind, node.is_definition()):
            # Function definition
            case (CursorKind.FUNCTION_DECL, True):
                result.symbols[usr] = Symbol(usr, node, decl)
                fn_defn = get_code_from_tu_range(tu, node.extent)
                result.fn_definitions[usr] = fn_defn

            # Data structures
            case (kind, _) if kind in DATA_STRUCT_NODE_MAP:
                result.symbols[usr] = Symbol(usr, node, decl)

                previous_usr = usr
                for child in node.get_children():
                    # Register a dependency on the underlying enum for each enumerator
                    if child.kind == CursorKind.ENUM_CONSTANT_DECL:
                        result.symbols[child.get_usr()] = Symbol(child.get_usr(), child)
                        result.complete_graph[child.get_usr()].append(result.symbols[usr])

            # Typedefs
            case (CursorKind.TYPEDEF_DECL, _):
                result.symbols[usr] = Symbol(usr, node, decl)

                # NOTE: If this is a typedef <struct/union/enum> the data structure was visited just before
                for child in node.walk_preorder():
                    if child.kind not in DATA_STRUCT_NODE_MAP:
                        continue
                    full_usr = previous_usr

                    # Suppress the data structure and force a dependence on the typedef
                    if (
                        child.kind in {CursorKind.STRUCT_DECL, CursorKind.UNION_DECL}
                        and full_usr in result.symbols
                        and result.symbols[full_usr].decl in result.symbols[usr].decl
                    ):
                        assert full_usr != usr, (
                            "Typedef cannot be named the same as its underlying data structure!"
                        )
                        del result.symbols[full_usr]
                        result.complete_graph[full_usr].append(result.symbols[usr])

                    # Register a dependency on the typedef for each (possibly deeply nested) enumerator
                    if child.kind == CursorKind.ENUM_CONSTANT_DECL:
                        result.symbols[child.get_usr()] = Symbol(child.get_usr(), child)
                        result.complete_graph[child.get_usr()].append(result.symbols[usr])

            # All other declarations
            case (kind, _) if kind in DECL_NODE_KIND:
                # Handle the case where we're a declaration occurs after a definition, e.g.:
                #   static const tflac_u16 tflac_crc16_tables[8][256] = { .. };
                #   static const tflac_u16 tflac_crc16_tables[8][256];
                if usr not in result.symbols or (
                    usr in result.symbols and node.is_definition()
                ):
                    result.symbols[usr] = Symbol(usr, node, decl)

            case (_, _):
                raise NotImplementedError()

        # All referenced symbols
        result.complete_graph[usr] = extract_referenced_symbols(node)

    return result


def extract_referenced_symbols(node: Cursor) -> list[Symbol]:
    symbol_uses = []

    for child_node in node.walk_preorder():
        # Reference to a symbol
        if child_node.kind in REF_NODE_KIND:
            # Ignore internal references to, e.g., function parameters
            if child_node.referenced is None:
                continue
            # Ignore self references
            if child_node.referenced.get_usr() == node.get_usr():
                continue
            symbol_uses.append(Symbol(child_node.referenced.get_usr(), child_node))

    return symbol_uses


def get_declaration_range(node: Cursor) -> SourceRange:
    prev_token = None

    stop_set = {}
    if node.kind == CursorKind.FUNCTION_DECL:
        stop_set = {"{"}

    # TODO: Add an option to include variable assignments (not just declarations)
    if node.kind == CursorKind.VAR_DECL:
        stop_set = {"="}

    for token in node.get_tokens():
        if token.kind != TokenKind.PUNCTUATION or token.spelling not in stop_set:
            prev_token = token
            continue
        assert prev_token is not None
        return SourceRange.from_locations(node.extent.start, prev_token.extent.end)

    # For some nodes return the full extent of the node
    return node.extent


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
    # Include tag definition when typedef cursor with non-typeref child
    include_tag_definition = 0
    if cursor.kind == CursorKind.TYPEDEF_DECL:
        children = list(cursor.get_children())
        include_tag_definition = len(children) == 1 and children[0].kind != CursorKind.TYPE_REF

    policy = PrintingPolicy.create(cursor)
    policy.set_property(PrintingPolicyProperty.IncludeTagDefinition, include_tag_definition)
    policy.set_property(PrintingPolicyProperty.ConstantsAsWritten, 0)

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
