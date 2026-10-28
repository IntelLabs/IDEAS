#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from collections.abc import Iterable

from clang.cindex import Cursor, CursorKind, TypeKind

from .ast_rust import CodeRust, mangle


_SIGNED_KINDS = frozenset(
    {
        TypeKind.CHAR_S,
        TypeKind.SCHAR,
        TypeKind.SHORT,
        TypeKind.INT,
        TypeKind.LONG,
        TypeKind.LONGLONG,
        TypeKind.INT128,
    }
)


def translate(cursor: Cursor) -> CodeRust | None:
    if _is_enum(cursor):
        return _translate_enum(cursor)

    return None


def _is_enum(cursor: Cursor) -> bool:
    if cursor.kind in (CursorKind.ENUM_DECL, CursorKind.ENUM_CONSTANT_DECL):
        return True
    return (
        cursor.kind == CursorKind.TYPEDEF_DECL
        and cursor.underlying_typedef_type.get_canonical().kind == TypeKind.ENUM
    )


def _enums_outside_bodies(cursor: Cursor) -> Iterable[Cursor]:
    if cursor.kind == CursorKind.COMPOUND_STMT:
        return
    if _is_enum(cursor):
        yield cursor
    for child in cursor.get_children():
        yield from _enums_outside_bodies(child)


def _translate_enum(cursor: Cursor) -> CodeRust | None:
    items: list[str] = []
    for c in _enums_outside_bodies(cursor):
        if c.kind == CursorKind.ENUM_CONSTANT_DECL:
            # A type in a value can tie enums into one SCC (C23 or GNU forward declarations)
            if any(
                d.kind in (CursorKind.TYPE_REF, CursorKind.ENUM_DECL) for d in c.walk_preorder()
            ):
                return None
            assert (enum := c.semantic_parent) is not None
            integer = enum.enum_type.get_canonical()
            signed = "i" if integer.kind in _SIGNED_KINDS else "u"
            bits = integer.get_size() * 8
            name = mangle(c.spelling)
            if name != name.upper():
                # A strict linting policy in the generated crate may reject preserved C names
                items.append("#[allow(non_upper_case_globals)]")
            items.append(f"pub const {name}: {signed}{bits} = {c.enum_value};")
    if not items:
        return CodeRust(f"// `{cursor.spelling}` declares no enum constants")
    return CodeRust("\n".join(items))
