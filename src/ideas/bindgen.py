#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from pathlib import Path
from textwrap import indent
from collections.abc import Iterable

from clang.cindex import CursorKind, TypeKind
from tree_sitter import Node

from ideas.tools import check_rust, run_subprocess
from ideas.ast import clang_make_bindable_, create_translation_unit
from ideas.ast_rust import BindgenName, CodeRust, get_node_text, get_nodes, get_root, mangle

C_INTEROP_TRAIT = CodeRust("""\
pub trait CInterop: Sized {
    type Rust;
    /// # Safety
    ///
    /// `cs` must point to a valid `Self`.
    unsafe fn to_rust(cs: *const Self) -> Self::Rust;
    /// # Safety
    ///
    /// `cs` must point to a valid `Self`, writable for the duration of the call.
    unsafe fn sync_to_c(rs: &Self::Rust, cs: *mut Self);
}
""")


def _run_bindgen(path: Path, what: str, *flags: str) -> CodeRust:
    ok, out, error, _ = run_subprocess(
        [
            "bindgen",
            "--disable-header-comment",
            "--no-doc-comments",
            "--no-layout-tests",
            "--sort-semantically",
            # Make glibc internals behind `FILE`, `pthread_*_t` and `locale_t` opaque
            # since they're not usable beyond the FFI boundary.
            *("--opaque-type", "_IO_.*"),
            *("--opaque-type", "__pthread_.*"),
            *("--opaque-type", "__locale_struct"),
            *flags,
            str(path),
        ]
    )
    if not ok:
        raise ValueError(f"Bindgen failed for {what} in '{path}'!\nError:\n{error}")
    return CodeRust(out.strip())


def bindgen_types(path: Path, symbol_names: Iterable[str]) -> CodeRust:
    types = _run_bindgen(
        path,
        "the crate's types",
        # Every symbol in one run, so bindgen assigns anonymous-type names once for the
        # whole crate instead of restarting the counter per symbol
        *(f for n in symbol_names for f in ("--allowlist-item", mangle(n))),
        # The allowlist above reaches types through the symbols referencing them, so
        # dropping every var and function leaves exactly the types behind.
        *("--blocklist-var", ".*"),
        *("--blocklist-function", ".*"),
    )
    type_items, value_items = partition_values(types)
    success, output = check_rust(
        str(type_items + wrap_values(value_items)),
        flags=["--crate-type=lib", "--emit=metadata"],
    )
    if not success:
        raise ValueError(
            f"Failed to validate types in '{path}'!\nTypes:\n{types}\nError:\n{output}"
        )
    return types


VALUES_MODULE = "__c_globals"


def partition_values(code: CodeRust) -> tuple[CodeRust, CodeRust]:
    # Separate the value namespace (bindgen's consts, the C globals' externs) from the types,
    # so only the latter reach the crate root
    types: list[str] = []
    values: list[str] = []
    pending: list[str] = []
    for node in get_nodes(get_root(str(code))):
        # Attributes are siblings of the item they decorate, so they wait for it
        if node.type == "attribute_item":
            pending.append(get_node_text(node))
            continue
        bucket = values if node.type in ("const_item", "static_item") else types
        bucket.extend([*pending, get_node_text(node)])
        pending.clear()
    types.extend(pending)

    parts = (CodeRust("\n".join(types)), CodeRust("\n".join(values)))
    kept = sorted(line for part in parts for line in _significant_lines(part))
    if kept != sorted(_significant_lines(code)):
        raise ValueError(f"Partitioning dropped or duplicated items!\nCode:\n{code}")
    return parts


def wrap_values(*parts: CodeRust, module: str = VALUES_MODULE) -> CodeRust:
    # A crate-root `static` or `const` cannot be shadowed, so any same-named parameter or `let`
    # fails with E0530/E0005. `super`, not `crate`, since lib.rs is also `mod hybrid` in main.rs
    body = CodeRust.join([part for part in parts if str(part).strip()])
    if not str(body).strip():
        return CodeRust()
    return CodeRust(
        f"pub mod {module} {{\n    use super::*;\n\n{indent(str(body).rstrip(), '    ')}\n}}"
    )


# Bucket for items that belong to no single type, always kept in every slice. Not spellable
# as a Rust identifier, so no name read out of bindgen's output can collide with it.
TYPES_PREAMBLE = BindgenName("<types-preamble>")


def _item_key(node: Node) -> BindgenName | None:
    # A const or impl belongs to the type it names, e.g. `pub const tag_t_A: tag_t = 0;`
    # joins `tag_t` and `impl<S> __BindgenBitfieldUnit<S>` joins `__BindgenBitfieldUnit`
    if node.type not in ("const_item", "impl_item"):
        name = get_node_text(node.child_by_field_name("name"))
    else:
        target = node.child_by_field_name("type")
        while target is not None and target.type == "generic_type":
            target = target.child_by_field_name("type")
        name = get_node_text(target)
    return BindgenName(name) if name else None


def split_types(types: CodeRust) -> dict[BindgenName, CodeRust]:
    # Index bindgen's type output by item name so prompts can show a subset of it
    entries = []
    pending: list[str] = []
    for node in get_nodes(get_root(str(types))):
        # Attributes are siblings of the item they decorate, so they wait for it
        if node.type == "attribute_item":
            pending.append(get_node_text(node))
            continue
        entries.append((node, [*pending, get_node_text(node)]))
        pending.clear()

    declared = {
        get_node_text(n.child_by_field_name("name"))
        for n, _ in entries
        if n.type in ("struct_item", "union_item", "enum_item", "type_item")
    }
    items: dict[BindgenName, list[str]] = {TYPES_PREAMBLE: pending}
    for node, text in entries:
        name = _item_key(node)
        key = name if name is not None and name in declared else TYPES_PREAMBLE
        items.setdefault(key, []).extend(text)

    split = {name: CodeRust("\n".join(lines)) for name, lines in items.items() if lines}
    # Buckets reorder items but must never drop or duplicate one
    kept = sorted(line for code in split.values() for line in _significant_lines(code))
    if kept != sorted(_significant_lines(types)):
        raise ValueError(f"Splitting dropped or duplicated type items!\nTypes:\n{types}")
    return split


def _significant_lines(code: CodeRust) -> list[str]:
    # `CodeRust.join` inserts blank lines between parts, so compare only the content
    return [line for line in str(code).splitlines() if line.strip()]


def split_items(
    code: CodeRust,
) -> tuple[dict[BindgenName, CodeRust], dict[BindgenName, CodeRust]]:
    # Split first so both maps key a const by the type it names
    parts = {name: partition_values(part) for name, part in split_types(code).items()}
    return (
        {n: t for n, (t, _) in parts.items() if str(t).strip()},
        {n: v for n, (_, v) in parts.items() if str(v).strip()},
    )


def bindgen_binding(path: Path, symbol_name: str, types: CodeRust) -> CodeRust:
    return bindgen_bindings(path, [symbol_name], types)[symbol_name]


def bindgen_bindings(
    path: Path, symbol_names: Iterable[str], types: CodeRust
) -> dict[str, CodeRust]:
    names = list(dict.fromkeys(symbol_names))
    if not names:
        return {}

    const_variables = _const_object_variables(path, names)
    orig_src = path.read_bytes()
    try:
        # An initialized global would otherwise become a `pub const` with no linkage
        clang_make_bindable_(path, names)

        # unsafe extern "C" {
        #     pub static mut foo: ::std::os::raw::c_int;
        # }
        bindings = _run_bindgen(
            path,
            ", ".join(f"`{name}`" for name in names),
            # A binding is only ever a variable or a function, never a type
            *(f for name in names for f in ("--allowlist-var", mangle(name))),
            *(f for name in names for f in ("--allowlist-function", mangle(name))),
            # Every type the binding references is already defined in `types`
            "--no-recursive-allowlist",
        )
    finally:
        path.write_bytes(orig_src)

    split = _split_bindings(bindings, names)
    if missing := [name for name in names if name not in split]:
        raise ValueError(
            f"Bindgen generated no binding for {', '.join(f'`{n}`' for n in missing)} "
            f"in '{path}'!"
        )

    for name in const_variables:
        rust_name = mangle(name)
        binding = str(split[name])
        mutable_decl = f"static mut {rust_name}:"
        if mutable_decl in binding:
            split[name] = CodeRust(binding.replace(mutable_decl, f"static {rust_name}:", 1))

    type_items, value_items = partition_values(types)
    success, output = check_rust(
        str(type_items + wrap_values(value_items, bindings)),
        flags=["--crate-type=lib", "--emit=metadata"],
    )
    if not success:
        raise ValueError(
            f"Failed to validate bindings for {', '.join(f'`{n}`' for n in names)} in "
            f"'{path}'!\nBindings:\n{bindings}\nError:\n{output}"
        )
    return split


def _const_object_variables(path: Path, names: Iterable[str]) -> set[str]:
    wanted = set(names)
    if not wanted:
        return set()

    tu = create_translation_unit(path)
    const_variables = set()
    assert tu.cursor is not None
    array_kinds = {
        TypeKind.CONSTANTARRAY,
        TypeKind.INCOMPLETEARRAY,
        TypeKind.VARIABLEARRAY,
        TypeKind.DEPENDENTSIZEDARRAY,
    }
    for cursor in tu.cursor.walk_preorder():
        if (
            cursor.kind != CursorKind.VAR_DECL
            or cursor.spelling not in wanted
            or cursor.semantic_parent is None
            or cursor.semantic_parent.kind != CursorKind.TRANSLATION_UNIT
            or cursor.location.is_in_system_header
        ):
            continue

        object_type = cursor.type
        while object_type.kind in array_kinds:
            object_type = object_type.get_array_element_type()
        if object_type.is_const_qualified():
            const_variables.add(cursor.spelling)
    return const_variables


def _split_bindings(bindings: CodeRust, symbol_names: Iterable[str]) -> dict[str, CodeRust]:
    # bindgen names items after mangling, so map back to the C spelling the caller asked for
    by_rust_name = {mangle(name): name for name in symbol_names}
    source = str(bindings).encode()

    items: dict[str, list[str]] = {}
    pending: list[str] = []
    for node in get_nodes(get_root(source)):
        # Attributes are siblings of the item they decorate, so they wait for it
        if node.type == "attribute_item":
            pending.append(get_node_text(node))
            continue
        for rust_name, text in _binding_items(node, source):
            if (name := by_rust_name.get(rust_name)) is None:
                raise ValueError(f"Bindgen emitted unrequested item `{rust_name}`!\n{bindings}")
            items.setdefault(name, []).extend([*pending, text])
        pending.clear()

    return {name: CodeRust("\n".join(lines)) for name, lines in items.items() if lines}


def _binding_items(node: Node, source: bytes) -> Iterable[tuple[str, str]]:
    body = node.child_by_field_name("body") if node.type == "foreign_mod_item" else None
    if body is None:
        yield get_node_text(node.child_by_field_name("name")), get_node_text(node)
        return

    inner = [n for n in get_nodes(body) if n.type not in ("{", "}")]
    if len(inner) == 1:
        # The common case: bindgen already gave this item a block of its own
        yield get_node_text(inner[0].child_by_field_name("name")), get_node_text(node)
        return

    # Re-wrap each item so one shared block never lands whole in two symbols' bindings
    header = source[node.start_byte : body.children[0].end_byte].decode()
    for item in inner:
        name = get_node_text(item.child_by_field_name("name"))
        # Only the item's first line lost its indentation; the rest keep the source's
        pad = " " * item.start_point.column
        yield name, f"{header}\n{pad}{get_node_text(item)}\n}}"
