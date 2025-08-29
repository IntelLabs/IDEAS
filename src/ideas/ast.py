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

from ideas.utils import Symbol, filter_edges_by_set, get_all_deps

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
    CursorKind.STRUCT_DECL: "struct",  # type: ignore[reportAttributeAccessIssue]
    CursorKind.UNION_DECL: "union",  # type: ignore[reportAttributeAccessIssue]
    CursorKind.ENUM_DECL: "enum",  # type: ignore[reportAttributeAccessIssue]
    CursorKind.ENUM_CONSTANT_DECL: "enum",  # type: ignore[reportAttributeAccessIssue]
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
    top_level_ref_graph: dict[str, list[Symbol]] = field(
        default_factory=lambda: defaultdict(lambda: list())
    )

    def get_top_level_symbols_for_name(self, name: str) -> list[str]:
        ref_fn = [
            self.symbols[symbol.name].decl
            for symbol in self.top_level_ref_graph[name]
            if symbol.name in self.symbols
        ]

        return ref_fn


def create_translation_unit(code: str) -> TranslationUnit:
    # Parse the code using clang
    tu = TranslationUnit.from_source(FILENAME, unsaved_files=[(FILENAME, code)])
    return tu


# Traverse the AST, extract symbols and resolve deep references
def extract_info_c(tu: TranslationUnit) -> TreeResult:
    result = TreeResult()
    previous_name: str = ""
    for node in tu.cursor.get_children():
        name, kind, usr = node.spelling, node.kind, node.get_usr()
        # Top-level declaration
        decl_range = get_declaration_range(node)
        decl = get_code_from_tu_range(tu, decl_range)

        match (name, kind, node.is_definition()):
            # Nameless symbols
            case ("", _, _):
                assert kind in DATA_STRUCT_NODE_MAP, (
                    f"An unexpected nameless declaration was encountered: {kind}"
                )

                # Capture nameless data structures using their usr
                pseudo_name = " ".join([DATA_STRUCT_NODE_MAP[kind], usr])
                result.symbols[pseudo_name] = Symbol(pseudo_name, kind, decl)

                previous_name = usr
                for child in node.get_children():
                    # Register a dependency on the underlying enum for each enumerator
                    if child.kind == CursorKind.ENUM_CONSTANT_DECL:  # type: ignore[reportAttributeAccessIssue]
                        result.complete_graph[child.spelling].append(
                            result.symbols[pseudo_name]
                        )

            # Function definition
            case (_, CursorKind.FUNCTION_DECL, True):  # type: ignore[reportAttributeAccessIssue]
                result.symbols[name] = Symbol(name, kind, decl)
                fn_defn = get_code_from_tu_range(tu, node.extent)
                result.fn_definitions[name] = fn_defn

            # Data structures
            case (_, kind, _) if kind in DATA_STRUCT_NODE_MAP:
                # Modify the name to match the data structure type
                # e.g., "struct Point" instead of just "Point"
                original_name = name
                name = " ".join((DATA_STRUCT_NODE_MAP[kind], name))
                result.symbols[name] = Symbol(name, kind, decl)

                previous_name = original_name
                for child in node.get_children():
                    # Register a dependency on the underlying enum for each enumerator
                    if child.kind == CursorKind.ENUM_CONSTANT_DECL:  # type: ignore[reportAttributeAccess]
                        result.complete_graph[child.spelling].append(result.symbols[name])

            # Typedefs
            case (_, CursorKind.TYPEDEF_DECL, _):  # type: ignore[reportAttributeAccessIssue]
                result.symbols[name] = Symbol(name, kind, decl)

                # NOTE: If this is a typedef <struct/union/enum> the data structure was visited just before
                for child in node.walk_preorder():
                    if child.kind not in DATA_STRUCT_NODE_MAP:
                        continue
                    full_name = " ".join((DATA_STRUCT_NODE_MAP[child.kind], previous_name))

                    # Suppress the data structure and force a dependence on the typedef
                    if (
                        child.kind in {CursorKind.STRUCT_DECL, CursorKind.UNION_DECL}  # type: ignore[reportAttributeAccessIssue]
                        and full_name in result.symbols
                        and result.symbols[full_name].decl in result.symbols[name].decl
                    ):
                        assert full_name != name, (
                            "Typedef cannot be named the same as its underlying data structure!"
                        )
                        del result.symbols[full_name]
                        result.complete_graph[full_name].append(result.symbols[name])

                    # Register a dependency on the typedef for each (possibly deeply nested) enumerator
                    if child.kind == CursorKind.ENUM_CONSTANT_DECL:  # type: ignore[reportAttributeAccessIssue]
                        result.complete_graph[child.spelling].append(result.symbols[name])

            # All other declarations
            case (_, kind, _) if kind in DECL_NODE_KIND:
                result.symbols[name] = Symbol(name, kind, decl)

        # All referenced symbols
        result.complete_graph[name] = extract_referenced_symbols(node)

    # Resolve top-level dependencies
    cache = {}
    for name in result.symbols.keys():
        # Get all dependencies of this symbol
        expanded_deps = get_all_deps(result.complete_graph, name, cache=cache)
        # Add the dependencies to the top-level graph
        result.top_level_ref_graph[name] = filter_edges_by_set(
            expanded_deps, result.symbols.keys()
        )

    return result


def extract_referenced_symbols(node: Cursor) -> list[Symbol]:
    symbol_uses = []

    for node in node.walk_preorder():
        # Reference to a symbol
        if node.kind in REF_NODE_KIND:
            symbol_uses.append(Symbol(node.spelling, node.kind))

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
    clang_PrintingPolicy_setProperty(policy, 3, 1)  # IncludeTagDefinition
    clang_PrintingPolicy_setProperty(policy, 23, 1)  # ConstantsAsWritten

    return clang_getCursorPrettyPrinted(cursor, policy)
