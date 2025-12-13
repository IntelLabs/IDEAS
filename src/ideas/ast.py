#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from collections import defaultdict
from dataclasses import dataclass, field

from clang.cindex import TranslationUnit, Cursor, CursorKind, TokenKind, SourceRange, _CXString
from clang.cindex import conf, c_object_p
from ctypes import pointer, c_size_t, c_char_p, c_uint, c_int

from ideas.utils import Symbol

FILENAME = "file.c"

DECL_NODE_KIND = {
    CursorKind.FUNCTION_DECL,  # type: ignore[reportAttributeAccessIssue]
    CursorKind.TYPEDEF_DECL,  # type: ignore[reportAttributeAccessIssue]
    CursorKind.VAR_DECL,  # type: ignore[reportAttributeAccessIssue]
    CursorKind.STRUCT_DECL,  # type: ignore[reportAttributeAccessIssue]
    CursorKind.UNION_DECL,  # type: ignore[reportAttributeAccessIssue]
    CursorKind.ENUM_DECL,  # type: ignore[reportAttributeAccessIssue]
}

REF_NODE_KIND = {
    CursorKind.CALL_EXPR,  # type: ignore[reportAttributeAccessIssue]
    CursorKind.TYPE_REF,  # type: ignore[reportAttributeAccessIssue]
    CursorKind.DECL_REF_EXPR,  # type: ignore[reportAttributeAccessIssue]
}

DATA_STRUCT_NODE_MAP = {
    CursorKind.STRUCT_DECL,  # type: ignore[reportAttributeAccessIssue]
    CursorKind.UNION_DECL,  # type: ignore[reportAttributeAccessIssue]
    CursorKind.ENUM_DECL,  # type: ignore[reportAttributeAccessIssue]
    CursorKind.ENUM_CONSTANT_DECL,  # type: ignore[reportAttributeAccessIssue]
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
    for node in tu.cursor.get_children():
        kind, usr = node.kind, node.get_usr()
        # Top-level declaration
        decl_range = get_declaration_range(node)
        decl = get_code_from_tu_range(tu, decl_range)

        match (kind, node.is_definition()):
            # Function definition
            case (CursorKind.FUNCTION_DECL, True):  # type: ignore[reportAttributeAccessIssue]
                result.symbols[usr] = Symbol(usr, node, decl)
                fn_defn = get_code_from_tu_range(tu, node.extent)
                result.fn_definitions[usr] = fn_defn

            # Data structures
            case (kind, _) if kind in DATA_STRUCT_NODE_MAP:
                result.symbols[usr] = Symbol(usr, node, decl)

                previous_usr = usr
                for child in node.get_children():
                    # Register a dependency on the underlying enum for each enumerator
                    if child.kind == CursorKind.ENUM_CONSTANT_DECL:  # type: ignore[reportAttributeAccess]
                        result.symbols[child.get_usr()] = Symbol(child.get_usr(), child)
                        result.complete_graph[child.get_usr()].append(result.symbols[usr])

            # Typedefs
            case (CursorKind.TYPEDEF_DECL, _):  # type: ignore[reportAttributeAccessIssue]
                result.symbols[usr] = Symbol(usr, node, decl)

                # NOTE: If this is a typedef <struct/union/enum> the data structure was visited just before
                for child in node.walk_preorder():
                    if child.kind not in DATA_STRUCT_NODE_MAP:
                        continue
                    full_usr = previous_usr

                    # Suppress the data structure and force a dependence on the typedef
                    if (
                        child.kind in {CursorKind.STRUCT_DECL, CursorKind.UNION_DECL}  # type: ignore[reportAttributeAccessIssue]
                        and full_usr in result.symbols
                        and result.symbols[full_usr].decl in result.symbols[usr].decl
                    ):
                        assert full_usr != usr, (
                            "Typedef cannot be named the same as its underlying data structure!"
                        )
                        del result.symbols[full_usr]
                        result.complete_graph[full_usr].append(result.symbols[usr])

                    # Register a dependency on the typedef for each (possibly deeply nested) enumerator
                    if child.kind == CursorKind.ENUM_CONSTANT_DECL:  # type: ignore[reportAttributeAccessIssue]
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
    if node.kind == CursorKind.FUNCTION_DECL:  # type: ignore[reportAttributeAccessIssue]
        stop_set = {"{"}

    # TODO: Add an option to include variable assignments (not just declarations)
    if node.kind == CursorKind.VAR_DECL:  # type: ignore[reportAttributeAccessIssue]
        stop_set = {"="}

    for token in node.get_tokens():
        if token.kind != TokenKind.PUNCTUATION or token.spelling not in stop_set:  # type: ignore[reportAttributeAccessIssue]
            prev_token = token
            continue
        assert prev_token is not None
        return SourceRange.from_locations(node.extent.start, prev_token.extent.end)

    # For some nodes return the full extent of the node
    return node.extent


def get_code_from_tu_range(
    tu: TranslationUnit, source_range: SourceRange, encoding: str = "utf-8"
) -> str:
    assert source_range.start.file != source_range.end.file, (
        f"{source_range.start.file} != {source_range.end.file}"
    )
    length = pointer(c_size_t())
    conf.lib.clang_getFileContents.restype = c_char_p  # type: ignore[reportAttributeAccessIssue]
    code: bytes = conf.lib.clang_getFileContents(tu, source_range.start.file, length)  # type: ignore[reportAttributeAccessIssue]
    if code is None:
        return ""
    return code[source_range.start.offset : source_range.end.offset].decode(encoding)


def get_cursor_prettyprinted(cursor: Cursor) -> str:
    # Include tag definition when typedef cursor with non-typeref child
    include_tag_definition = 0
    if cursor.kind == CursorKind.TYPEDEF_DECL:  # type: ignore[reportAttributeAccessIssue]
        children = list(cursor.get_children())
        include_tag_definition = len(children) == 1 and children[0].kind != CursorKind.TYPE_REF  # type: ignore[reportAttributeAccessIssue]

    # Setup FFI for unsupported python libclang functions
    # NOTE: Upgrade libclang to get these?
    clang_getCursorPrintingPolicy = conf.lib.clang_getCursorPrintingPolicy  # type: ignore[reportAttributeAccessIssue]
    clang_getCursorPrintingPolicy.argtypes = [Cursor]
    clang_getCursorPrintingPolicy.restype = c_object_p

    clang_PrintingPolicy_getProperty = conf.lib.clang_PrintingPolicy_getProperty  # type: ignore[reportAttributeAccessIssue]
    clang_PrintingPolicy_getProperty.argtypes = [c_object_p, c_int]
    clang_PrintingPolicy_getProperty.restype = c_uint

    clang_PrintingPolicy_setProperty = conf.lib.clang_PrintingPolicy_setProperty  # type: ignore[reportAttributeAccessIssue]
    clang_PrintingPolicy_setProperty.argtypes = [c_object_p, c_int, c_uint]

    clang_getCursorPrettyPrinted = conf.lib.clang_getCursorPrettyPrinted  # type: ignore[reportAttributeAccessIssue]
    clang_getCursorPrettyPrinted.argtypes = [Cursor, c_object_p]
    clang_getCursorPrettyPrinted.restype = _CXString
    clang_getCursorPrettyPrinted.errcheck = _CXString.from_result

    policy = clang_getCursorPrintingPolicy(cursor)
    clang_PrintingPolicy_setProperty(policy, 3, include_tag_definition)
    clang_PrintingPolicy_setProperty(policy, 23, 0)  # ConstantsAsWritten
    # clang_PrintingPolicy_setProperty(policy, 26, 1)  # PrintAsCanonical

    return clang_getCursorPrettyPrinted(cursor, policy).rstrip()


def get_cursor_code(cursor: Cursor, pretty_print: bool = False) -> str:
    if pretty_print:
        code = get_cursor_prettyprinted(cursor)
    else:
        code = get_code_from_tu_range(cursor.translation_unit, cursor.extent)

    # Non-function definitions require statement terminations
    if cursor.kind != CursorKind.FUNCTION_DECL or not cursor.is_definition():  # type: ignore[reportAttributeAccessIssue]
        code += ";"

    return code
