#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
from pathlib import Path
from functools import cmp_to_key
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import astuple, dataclass, field, fields, replace
from typing import get_args

from clang.cindex import TranslationUnit, TranslationUnitLoadError, Diagnostic
from clang.cindex import Cursor, CursorKind, SourceRange, TokenKind, Type, TypeKind
from clang.cindex import PrintingPolicy, PrintingPolicyProperty, LinkageKind, StorageClass
from clang.cindex import conf, SourceLocation, _CXString
from ctypes import byref, pointer, c_size_t, c_char_p, c_uint

from .adapters import Code

logger = logging.getLogger("ideas.ast")
FILENAME = "file.c"
CodeC = Code["c"]

# Cursor kinds that become symbols, ranked by order in which they should be translated
_KIND_RANK = {
    CursorKind.ENUM_DECL: 0,
    CursorKind.ENUM_CONSTANT_DECL: 0,
    CursorKind.STRUCT_DECL: 1,
    CursorKind.UNION_DECL: 1,
    CursorKind.TYPEDEF_DECL: 1,
    CursorKind.VAR_DECL: 2,
    CursorKind.FUNCTION_DECL: 3,
}


@dataclass(frozen=True)
class TypeShape:
    # Declared hardest construct first, because `rank` reads the fields off in order. A
    # `void *` leads: it cannot be given a meaningful Rust type until whatever it is cast
    # to has been translated, and static analysis cannot recover that edge, so the more
    # erased of two types sorts last.
    void_pointers: int = 0
    function_pointers: int = 0
    unions: int = 0  # untagged unions need manual discrimination
    pointers: int = 0
    arrays: int = 0  # arrays and flexible array members
    fields: int = 0

    @property
    def rank(self) -> tuple[int, ...]:
        # Compared lexicographically, so the presence of a harder construct outweighs any
        # amount of an easier one and no weights have to be invented to say which is worse
        return astuple(self)

    # Deliberately left unannotated so that `dataclass` does not treat these as fields
    CURSOR_KINDS = (
        CursorKind.STRUCT_DECL,
        CursorKind.UNION_DECL,
        CursorKind.TYPEDEF_DECL,
        CursorKind.ENUM_DECL,
    )
    _ARRAY_KINDS = (
        TypeKind.CONSTANTARRAY,
        TypeKind.INCOMPLETEARRAY,
        TypeKind.VARIABLEARRAY,
        TypeKind.DEPENDENTSIZEDARRAY,
    )
    _FUNCTION_KINDS = (TypeKind.FUNCTIONPROTO, TypeKind.FUNCTIONNOPROTO)

    @classmethod
    def _ultimate_pointee(cls, c_type: Type) -> Type | None:
        canonical = c_type.get_canonical()
        while canonical.kind in cls._ARRAY_KINDS:
            canonical = canonical.get_array_element_type().get_canonical()
        if canonical.kind != TypeKind.POINTER:
            return None
        while canonical.kind == TypeKind.POINTER:
            canonical = canonical.get_pointee().get_canonical()
        return canonical

    @classmethod
    def from_cursor(cls, cursor: Cursor) -> "TypeShape":
        if cursor.kind not in cls.CURSOR_KINDS:
            return cls()

        num_fields = num_void_ptrs = num_fn_ptrs = num_ptrs = num_arrays = num_unions = 0

        # An inline anonymous record is presented both as a sibling declaration and as a
        # child of the field that uses it, so nodes must only be counted once.
        seen: set[tuple[str, str, str, str, int, int, int, int]] = set()

        for node in cursor.walk_preorder():
            if node.kind not in (CursorKind.UNION_DECL, CursorKind.FIELD_DECL):
                continue
            key = _cursor_key(node)
            if key in seen:
                continue
            seen.add(key)

            if node.kind == CursorKind.UNION_DECL:
                num_unions += 1
                continue

            num_fields += 1
            if node.type.get_canonical().kind in cls._ARRAY_KINDS:
                num_arrays += 1

            pointee = cls._ultimate_pointee(node.type)
            if pointee is None:
                continue
            if pointee.kind == TypeKind.VOID:
                num_void_ptrs += 1
            elif pointee.kind in cls._FUNCTION_KINDS:
                num_fn_ptrs += 1
            else:
                num_ptrs += 1

        return cls(
            void_pointers=num_void_ptrs,
            function_pointers=num_fn_ptrs,
            unions=num_unions,
            pointers=num_ptrs,
            arrays=num_arrays,
            fields=num_fields,
        )


@dataclass(frozen=True)
class Symbol:
    # Symbol identity
    name: str
    spelling: str
    kind: CursorKind

    # Rendered C snippets
    llm_context_declaration: str
    declaration: CodeC | None
    code: CodeC

    # Symbol semantics
    is_definition: bool
    is_global: bool
    is_system: bool
    is_top_level: bool
    storage_class: StorageClass

    # Source and lexical metadata
    tu_path: Path
    presumed_path: Path | None
    tu_preorder_index: int
    line_directive: CodeC | None
    declaration_line_directive: CodeC | None

    # Structural summary of the type, empty for symbols that are not types
    type_shape: TypeShape = field(default_factory=TypeShape)

    @property
    def difficulty(self) -> tuple[int, ...]:
        # A sort key that puts the easiest symbol first: the symbol kind decides the
        # ordering, and how complicated its type is only breaks ties within a kind.
        return (_KIND_RANK[self.kind], *self.type_shape.rank)

    @classmethod
    def from_cursor(
        cls,
        name: str,
        cursor: Cursor,
        parent: Cursor | None = None,
        decl: Cursor | None = None,
        tu_preorder_index: int = -1,
    ) -> "Symbol":
        parent_or_cursor = parent or cursor
        code = get_cursor_code(parent_or_cursor, pretty_print=True)
        presumed_location = clang_get_presumed_location(parent_or_cursor)
        return cls(
            name=name,
            spelling=cursor.spelling,
            kind=cursor.kind,
            llm_context_declaration=_synthesize_llm_context_declaration(
                cursor, fallback_code=code
            ),
            declaration=get_cursor_code(decl, pretty_print=True) if decl else None,
            code=code,
            is_definition=cursor.is_definition(),
            is_global=cursor.linkage == LinkageKind.EXTERNAL,
            is_system=cursor.location.is_in_system_header,
            tu_path=Path(cursor.translation_unit.spelling).resolve(),
            presumed_path=Path(presumed_location[0]) if presumed_location is not None else None,
            tu_preorder_index=tu_preorder_index,
            line_directive=_line_directive_for(parent_or_cursor),
            declaration_line_directive=_line_directive_for(decl),
            storage_class=cursor.storage_class,
            is_top_level=parent is None,
            type_shape=TypeShape.from_cursor(parent_or_cursor),
        )

    @property
    def is_variable(self) -> bool:
        return self.kind == CursorKind.VAR_DECL

    @property
    def is_function(self) -> bool:
        return self.kind == CursorKind.FUNCTION_DECL

    @property
    def is_type(self) -> bool:
        return self.kind in (
            CursorKind.STRUCT_DECL,
            CursorKind.UNION_DECL,
            CursorKind.TYPEDEF_DECL,
        )

    @property
    def is_struct(self) -> bool:
        return self.kind == CursorKind.STRUCT_DECL

    def with_declaration(self, decl_symbol: "Symbol") -> "Symbol":
        return replace(
            self,
            declaration=decl_symbol.code,
            declaration_line_directive=decl_symbol.line_directive,
        )

    def __getstate__(self) -> dict[str, object]:
        state = dict(self.__dict__)
        for field_name, value in state.items():
            if isinstance(value, CodeC):
                state[field_name] = str(value)
        return state

    def __setstate__(self, state: dict[str, object]):
        annotations = {f.name: f.type for f in fields(type(self))}
        for field_name, value in state.items():
            annotation = annotations.get(field_name)
            if value is not None and (annotation is CodeC or CodeC in get_args(annotation)):
                assert isinstance(value, str)
                value = CodeC(value)
            object.__setattr__(self, field_name, value)


@dataclass
class TreeResult:
    symbols: dict[str, Symbol] = field(default_factory=dict)
    complete_graph: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    filename: str | None = None
    arguments: list[str] | None = None
    local_names: frozenset[str] = field(default_factory=frozenset)


def _synthesize_llm_context_declaration(cursor: Cursor, fallback_code: CodeC) -> str:
    # Synthesize forward declaration from cursor
    if cursor.kind == CursorKind.FUNCTION_DECL:
        result_type = cursor.result_type.spelling if cursor.result_type else "void"
        params = ", ".join(
            p.type.spelling + (" " + p.spelling if p.spelling else "")  # type: ignore[reportOptionalMemberAccess]
            for p in cursor.get_arguments()
        )
        return f"{result_type} {cursor.spelling}({params});"
    elif cursor.kind in (
        CursorKind.STRUCT_DECL,
        CursorKind.UNION_DECL,
        CursorKind.ENUM_DECL,
    ):
        kind_name = {
            CursorKind.STRUCT_DECL: "struct",
            CursorKind.UNION_DECL: "union",
            CursorKind.ENUM_DECL: "enum",
        }[cursor.kind]
        return f"{kind_name} {cursor.spelling};"
    elif cursor.kind == CursorKind.TYPEDEF_DECL:
        underlying = cursor.underlying_typedef_type.spelling
        return f"typedef {underlying} {cursor.spelling};"
    elif cursor.kind == CursorKind.VAR_DECL:
        return f"{cursor.type.spelling} {cursor.spelling};"

    # Fallback: return full code
    return str(fallback_code)


def _line_directive_for(cursor: Cursor | None) -> CodeC | None:
    if cursor is None:
        return None

    location = cursor.location
    if location.file is None or location.line == 0:
        return None

    source_path = Path(str(location.file)).resolve().as_posix().replace('"', '\\"')
    return CodeC(f'#line {location.line} "{source_path}"')


def _cursor_key(cursor: Cursor) -> tuple[str, str, str, str, int, int, int, int]:
    location = cursor.location
    source_path = Path(str(location.file)).resolve().as_posix() if location.file else ""
    kind = str(cursor.kind)
    return (
        cursor.get_usr(),
        cursor.spelling,
        kind,
        source_path,
        int(location.line),
        int(location.column),
        int(cursor.extent.start.offset),
        int(cursor.extent.end.offset),
    )


def _cursor_order_map(root: Cursor) -> dict[tuple[str, str, str, str, int, int, int, int], int]:
    order: dict[tuple[str, str, str, str, int, int, int, int], int] = {}
    for i, cursor in enumerate(root.walk_preorder()):
        key = _cursor_key(cursor)
        if key not in order:
            order[key] = i
    return order


def clang_get_presumed_location(cursor: Cursor | None) -> tuple[str, int, int] | None:
    if cursor is None:
        return None

    filename = _CXString()
    line = c_uint(0)
    column = c_uint(0)
    conf.lib.clang_getPresumedLocation(
        cursor.location, byref(filename), byref(line), byref(column)
    )

    path_text = _CXString.from_result(filename)
    if not path_text or line.value == 0:
        return None

    path = Path(path_text).resolve().as_posix()
    return path, int(line.value), int(column.value)


def create_translation_unit(path_or_code: Path | CodeC) -> TranslationUnit:
    # Parse the code using clang
    if isinstance(path_or_code, CodeC):
        code = path_or_code
        tu = TranslationUnit.from_source(FILENAME, unsaved_files=[(FILENAME, str(code))])
    else:
        tu = TranslationUnit.from_source(str(path_or_code.resolve()))
    if any(d.severity >= Diagnostic.Error for d in tu.diagnostics):
        raise TranslationUnitLoadError("\n".join([d.format() for d in tu.diagnostics]))
    return tu


# Traverse the AST, extract symbols and resolve deep references
def extract_info_c(tu: TranslationUnit) -> TreeResult:
    assert tu.cursor is not None
    tu_preorder_index_map = _cursor_order_map(tu.cursor)
    symbols, reference_nodes = _extract_symbol_info_c(
        tu.cursor, tu_preorder_index_map=tu_preorder_index_map
    )
    graph = {
        name: extract_referenced_symbols(reference_nodes[name], symbols.keys())
        for name in symbols
    }
    local_names = frozenset(
        cursor.spelling
        for cursor in tu.cursor.walk_preorder()
        if cursor.spelling and not cursor.location.is_in_system_header
    )
    return TreeResult(symbols=symbols, complete_graph=graph, local_names=local_names)


def _extract_symbol_info_c(
    node: Cursor,
    parent: Cursor | None = None,
    tu_preorder_index_map: dict[tuple[str, str, str, str, int, int, int, int], int]
    | None = None,
) -> tuple[dict[str, Symbol], dict[str, Cursor]]:
    symbols: dict[str, Symbol] = {}
    reference_nodes: dict[str, Cursor] = {}
    tu_preorder_index_map = tu_preorder_index_map or _cursor_order_map(node)

    # If enter new scope then exit early
    if node.kind == CursorKind.COMPOUND_STMT:
        return symbols, reference_nodes

    # Add declarative nodes to symbols
    usr = node.get_usr()
    if node.kind in _KIND_RANK:
        symbols[usr] = Symbol.from_cursor(
            usr,
            node,
            parent=parent,
            tu_preorder_index=tu_preorder_index_map.get(_cursor_key(node), -1),
        )
        reference_nodes[usr] = parent or node

    # Recurse through children and merge them into symbols
    for child_node in node.get_children():
        parent = node if parent is None and node.kind != CursorKind.TRANSLATION_UNIT else parent
        child_symbols, child_refs = _extract_symbol_info_c(
            child_node,
            parent=parent,
            tu_preorder_index_map=tu_preorder_index_map,
        )
        for child_name, child_symbol in child_symbols.items():
            if child_name not in symbols:
                # Found a new symbol
                symbols[child_name] = child_symbol
                reference_nodes[child_name] = child_refs[child_name]
            elif symbols[child_name].is_definition and child_symbol.is_definition:
                # Always keep current definition
                symbols[child_name] = child_symbol
                reference_nodes[child_name] = child_refs[child_name]
            elif not symbols[child_name].is_definition and child_symbol.is_definition:
                # Previous symbol was a declaration so replace it with new definitional symbol
                symbols[child_name] = child_symbol.with_declaration(symbols[child_name])
                reference_nodes[child_name] = child_refs[child_name]
            elif symbols[child_name].is_definition and not child_symbol.is_definition:
                if not symbols[child_name].is_system or not child_symbol.is_system:
                    logger.debug(f"Ignoring declaration after definition of `{child_name}`")
            elif not symbols[child_name].is_definition and not child_symbol.is_definition:
                if (
                    child_symbol.kind == CursorKind.VAR_DECL
                    and symbols[child_name].storage_class == StorageClass.EXTERN
                    and child_symbol.storage_class != StorageClass.EXTERN
                ):
                    # Prefer non-extern variable declaration (e.g. tentative definition) over extern one
                    symbols[child_name] = child_symbol
                    reference_nodes[child_name] = child_refs[child_name]
                elif not symbols[child_name].is_system or not child_symbol.is_system:
                    logger.debug(f"Ignoring re-declaration of `{child_name}`")
    return symbols, reference_nodes


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
) -> CodeC:
    assert source_range.start.file == source_range.end.file, (
        f"{source_range.start.file} != {source_range.end.file}"
    )
    conf.lib.clang_getFileContents.restype = c_char_p
    length = pointer(c_size_t())
    code = conf.lib.clang_getFileContents(tu, source_range.start.file, length)
    assert code is not None
    return CodeC(code[source_range.start.offset : source_range.end.offset].decode(encoding))


def get_cursor_prettyprinted(cursor: Cursor) -> CodeC:
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
    # Emit C99 builtin spelling to avoid dependence on stdbool.h macro context.
    policy.set_property(PrintingPolicyProperty.Bool, 0)
    return CodeC(cursor.pretty_printed(policy))


def get_cursor_code(cursor: Cursor, pretty_print: bool = False) -> CodeC:
    if pretty_print:
        code = get_cursor_prettyprinted(cursor)
    else:
        code = get_code_from_tu_range(cursor.translation_unit, cursor.extent)

    # Non-function definitions require statement terminations
    if cursor.kind != CursorKind.FUNCTION_DECL or not cursor.is_definition():
        code = CodeC(str(code).rstrip() + ";")

    return code


def clang_rename(
    tu: TranslationUnit, renames: dict[str, str]
) -> dict[Path, dict[tuple[int, int], bytes]]:
    renames_str = "\n    ".join([f"{k} => {v}" for k, v in renames.items()])
    logger.info(f"Renaming {len(renames)} symbols in {tu.spelling}:\n    {renames_str}")

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

    return edits_by_file


DEFINITION_START_TOKEN = {CursorKind.FUNCTION_DECL: "{", CursorKind.VAR_DECL: "="}


def clang_make_global_(path: Path, spelling: str):
    tu = create_translation_unit(path)
    tu_path = Path(tu.spelling).resolve()
    edits: dict[tuple[int, int], bytes] = {}

    for cursor in _find_cursors(tu, spelling):
        # We don't handle cursors not in the provided translation unit or anything without a definition
        if (
            cursor.location.file is None
            or Path(cursor.location.file.name).resolve() != tu_path
            or Path(cursor.extent.start.file.name).resolve() != tu_path
            or Path(cursor.extent.end.file.name).resolve() != tu_path
        ):
            raise NotImplementedError(f"Found `{spelling}` cursor {cursor}` not in {tu_path}!")
        if cursor.kind not in DEFINITION_START_TOKEN:
            raise ValueError(f"Unhandled cursor kind {cursor.kind}!")

        tokens = list(_get_tokens(cursor))
        assert len(tokens) > 0

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
    tu_path = Path(tu.spelling).resolve()
    edits: dict[tuple[int, int], bytes] = {}

    for cursor in _find_cursors(tu, spelling):
        # We don't handle cursors not in the provided translation unit or anything without a definition
        if (
            cursor.location.file is None
            or Path(cursor.location.file.name).resolve() != tu_path
            or Path(cursor.extent.start.file.name).resolve() != tu_path
            or Path(cursor.extent.end.file.name).resolve() != tu_path
        ):
            raise NotImplementedError(f"Found `{spelling}` cursor `{cursor}` not in {tu_path}!")
        if cursor.kind not in DEFINITION_START_TOKEN:
            raise ValueError(f"Unhandled cursor kind {cursor.kind}!")

        tokens = list(_get_tokens(cursor))
        assert len(tokens) > 0

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


def clang_make_bindable_(path: Path, spelling: str):
    source = path.read_bytes()
    tu = create_translation_unit(path)
    tu_path = Path(tu.spelling).resolve()
    edits: dict[tuple[int, int], bytes] = {}
    cursors = _find_cursors(tu, spelling)
    has_variable_initializer_definition = any(
        cursor.kind == CursorKind.VAR_DECL
        and any(
            token.kind == TokenKind.PUNCTUATION and token.spelling == "="
            for token in _get_tokens(cursor)
        )
        for cursor in cursors
    )
    inserted_fallback_variable_extern = False

    for cursor in cursors:
        # We don't handle cursors not in the provided translation unit or anything without a definition
        if (
            cursor.location.file is None
            or Path(cursor.location.file.name).resolve() != tu_path
            or Path(cursor.extent.start.file.name).resolve() != tu_path
            or Path(cursor.extent.end.file.name).resolve() != tu_path
        ):
            raise NotImplementedError(f"Found `{spelling}` cursor `{cursor}` not in {tu_path}!")
        if cursor.kind not in DEFINITION_START_TOKEN:
            raise ValueError(f"Unhandled cursor kind {cursor.kind}!")

        tokens = list(_get_tokens(cursor))
        assert len(tokens) > 0

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

        # clang_make_bindable_ matches clang_make_extern_ behavior for functions.
        if cursor.kind == CursorKind.FUNCTION_DECL:
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
            continue

        # For variables, keep the definition and insert an extern declaration before it.
        assert cursor.kind == CursorKind.VAR_DECL

        declaration_end_idx = None
        for i, token in enumerate(tokens):
            if token.kind != TokenKind.PUNCTUATION:
                continue
            if token.spelling in ("=", ";"):
                declaration_end_idx = i
                break
        if declaration_end_idx is None:
            declaration_end_idx = len(tokens)

        declaration_tokens = [
            token
            for token in tokens[:declaration_end_idx]
            if not (
                token.kind == TokenKind.KEYWORD
                and token.spelling in ("static", "inline", "extern")
            )
        ]
        if len(declaration_tokens) == 0:
            continue

        should_insert_extern = definition_start_token_idx is not None
        if (
            not should_insert_extern
            and not has_variable_initializer_definition
            and not is_extern
            and not inserted_fallback_variable_extern
        ):
            should_insert_extern = True
            inserted_fallback_variable_extern = True

        if should_insert_extern:
            declaration_start = declaration_tokens[0].extent.start.offset
            declaration_end = declaration_tokens[-1].extent.end.offset
            declaration = source[declaration_start:declaration_end].decode().rstrip()
            extern_decl = f"extern {declaration};\n".encode()
            extern_insert_pos = cursor.extent.start.offset
            edits[(extern_insert_pos, extern_insert_pos)] = extern_decl

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


def _find_cursors(tu: TranslationUnit, spelling: str) -> list[Cursor]:
    candidates: list[Cursor] = []

    assert tu.cursor is not None
    for cursor in tu.cursor.walk_preorder():
        if cursor.kind not in (CursorKind.FUNCTION_DECL, CursorKind.VAR_DECL):
            continue
        if cursor.spelling != spelling:
            continue
        if cursor.semantic_parent is None:
            continue
        if cursor.semantic_parent.kind != CursorKind.TRANSLATION_UNIT:
            continue
        if cursor.location.is_in_system_header:
            continue
        candidates.append(cursor)

    if len(candidates) == 0:
        raise ValueError(f"Unable to find function or variable with spelling `{spelling}`")

    definitions = [cursor for cursor in candidates if cursor.is_definition()]
    definition_usrs = {cursor.get_usr() for cursor in definitions if cursor.get_usr()}
    if len(definition_usrs) > 1:
        raise ValueError(
            f"Ambiguous symbol `{spelling}` with multiple definitions: {sorted(definition_usrs)}"
        )

    if len(definition_usrs) == 1:
        target_usr = next(iter(definition_usrs))
    else:
        declaration_usrs = {cursor.get_usr() for cursor in candidates if cursor.get_usr()}
        if len(declaration_usrs) > 1:
            raise ValueError(
                f"Ambiguous symbol `{spelling}` with multiple declarations: {sorted(declaration_usrs)}"
            )
        target_usr = next(iter(declaration_usrs)) if len(declaration_usrs) == 1 else ""

    if not target_usr:
        return candidates

    return [cursor for cursor in candidates if cursor.get_usr() == target_usr]


def _apply_edits(path: Path, edits: dict[tuple[int, int], bytes]):
    source = path.read_bytes()

    # Apply edits in reverse offset order to preserve validity of remaining offsets
    for (start, end), replacement in sorted(edits.items(), key=lambda e: e[0][0], reverse=True):
        source = source[:start] + replacement + source[end:]

    path.write_bytes(source)


def mangle(name: str) -> str:
    name = name.replace(" ", "_")
    name = name.replace(".", "_")
    name = name.replace(":", "_")
    name = name.replace("-", "_")

    # Cannot start with a digit
    if name and name[0].isdigit():
        name = "_" + name

    return name


SymbolName = str
SymbolGroup = tuple[SymbolName, ...]


def create_symbol_lexical_key_fn(
    symbols: dict[SymbolName, Symbol],
    ast_order: dict[Path, TreeResult] | None = None,
):
    def compare_symbol_lexical(a: SymbolName | SymbolGroup, b: SymbolName | SymbolGroup) -> int:
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


def create_symbol_ordering_key_fn(
    symbols: dict[SymbolName, Symbol],
    ast_order: dict[Path, TreeResult] | None = None,
):
    # Order symbols by translation difficulty, falling back to lexical source order. This
    # only breaks ties between symbols that are incomparable in the dependency graph, so
    # the difficulty preference is one the topological constraint silently overrides.
    lexical_key = create_symbol_lexical_key_fn(symbols, ast_order)

    def symbol_ordering_key(node: SymbolName | SymbolGroup):
        # Rank a group by its hardest member, then break any remaining tie lexically
        names = node if isinstance(node, tuple) else (node,)
        return max(symbols[name].difficulty for name in names), lexical_key(node)

    return symbol_ordering_key


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
