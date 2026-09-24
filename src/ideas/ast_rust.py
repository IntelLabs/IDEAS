#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from textwrap import indent
from collections import OrderedDict

from tree_sitter import Language, Parser, Node, Query, QueryCursor
import tree_sitter_rust_orchard

from .adapters import Code

# Initialize the Rust language once
RUST_LANGUAGE = Language(tree_sitter_rust_orchard.language())
RUST_PARSER = Parser(RUST_LANGUAGE)
CodeRust = Code["rust"]


# The name bindgen gives an item, already mangled into a usable Rust identifier
class BindgenName(str):
    __slots__ = ()

    def __new__(cls, value: str) -> "BindgenName":
        # An unnamed item reads as "", which bindgen would take as a regex matching everything
        if not value:
            raise ValueError("A bindgen name cannot be empty!")
        return super().__new__(cls, value)


class RustFnSignature:
    def __init__(self, node: Node):
        if not node.type == "function_item":
            raise ValueError(
                f"Node {node} is not a function_item, so cannot extract a signature!"
            )

        name = node.child_by_field_name("name")
        if not name:
            raise ValueError(f"Function name not found in {node}!")

        self.name: Node = name
        self.params: Node | None = node.child_by_field_name("parameters")
        self.return_type: Node | None = node.child_by_field_name("return_type")

    def __repr__(self) -> str:
        text = ""
        if _text := self.name.text:
            text += _text.decode()

        if self.params and (_text := self.params.text):
            text += _text.decode()

        if self.return_type and (_text := self.return_type.text):
            text += _text.decode()
        return text

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RustFnSignature):
            return NotImplemented

        return self.__repr__() == other.__repr__()


def get_root(code: str | bytes) -> Node:
    if isinstance(code, str):
        code = code.encode()
    tree = RUST_PARSER.parse(code)
    return tree.root_node


def get_nodes(node: Node, node_type: str | None = None) -> list[Node]:
    nodes = []
    for child in node.children:
        if not node_type or child.type == node_type:
            nodes.append(child)
    return nodes


def get_node_text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode()


_TYPE_IDENTIFIER_QUERY = Query(RUST_LANGUAGE, "(type_identifier) @name")


def get_referenced_type_names(node: Node) -> set[BindgenName]:
    # Every type named anywhere under `node`, e.g. the field types of a struct item
    captures = QueryCursor(_TYPE_IDENTIFIER_QUERY).captures(node)
    return {BindgenName(text) for n in captures.get("name", []) if (text := get_node_text(n))}


def get_ancestor_nodes(node: Node, node_type: str | None = None) -> list[Node]:
    ancestors = []
    # Excluding self
    current = node.parent
    while current:
        if not node_type or current.type == node_type:
            ancestors.append(current)
        current = current.parent

    # Remove root node from ancestors
    return ancestors[:-1]


def get_macro_nodes(root: Node, placeholder: str) -> list[Node]:
    # Query for all nodes containing macro invocation
    source = f"""
    (macro_invocation
      macro: (identifier) @macro_name
      (#eq? @macro_name "{placeholder}")) @macro
    """

    query = Query(RUST_LANGUAGE, source)
    cursor = QueryCursor(query)
    captures = cursor.captures(root)

    # Collect all unique ancestors by walking up from each macro invocation
    ancestors = set()
    for macro_node in captures.get("macro", []):
        ancestors.update(get_ancestor_nodes(macro_node))

    return list(ancestors)


_CATCH_UNWIND_QUERY = Query(
    RUST_LANGUAGE,
    """
    (scoped_identifier
      path: (_) @path
      name: (identifier) @name
      (#eq? @name "catch_unwind"))

    (scoped_use_list
      path: (_) @path
      list: (use_list (identifier) @name)
      (#eq? @name "catch_unwind"))
    """,
)


def _path_tail(node: Node) -> str:
    name = node.child_by_field_name("name") if node.type == "scoped_identifier" else node
    return get_node_text(name)


def uses_catch_unwind(code: CodeRust) -> bool:
    matches = QueryCursor(_CATCH_UNWIND_QUERY).matches(get_root(str(code)))
    return any(_path_tail(caps["path"][0]) == "panic" for _, caps in matches)


_UNIMPLEMENTED_QUERY = Query(
    RUST_LANGUAGE,
    """
    (macro_invocation
      macro: [
        (identifier) @name
        (scoped_identifier name: (identifier) @name)
      ]
      (#eq? @name "unimplemented"))
    """,
)


def is_unimplemented(code: CodeRust) -> bool:
    captures = QueryCursor(_UNIMPLEMENTED_QUERY).captures(get_root(str(code)))
    return bool(captures.get("name"))


def validate_changes(code: CodeRust, template: CodeRust) -> OrderedDict[str, str]:
    code_root = get_root(str(code))
    template_root = get_root(str(template))

    nodes = get_nodes(code_root)
    template_nodes = get_nodes(template_root)
    allowed_change_nodes = get_macro_nodes(template_root, "unimplemented")

    # If the template has no unimplemented!() markers there are no scope constraints
    if not allowed_change_nodes:
        return OrderedDict()

    scope_feedback = OrderedDict()

    # Check for top-level changes
    if len(nodes) != len(template_nodes):
        scope_feedback["top_level_changes"] = (
            "The generated code modifies parts outside the function body.\n"
            "You must **only** modify the `unimplemented!()` function body and leave everything else **unchanged**!"
        )

    # Check for allowed changes
    for template_node, node in zip(template_nodes, nodes):
        if not template_node.text == node.text:
            if (
                template_node not in allowed_change_nodes
                or not template_node.type == "function_item"
            ):
                scope_feedback["top_level_changes"] = (
                    "The generated code modifies parts outside the function body.\n"
                    "You must **only** modify the `unimplemented!()` function body and leave everything else **unchanged**!"
                )

            if not node.type == "function_item" or (node.type != template_node.type):
                scope_feedback["signature_changes"] = (
                    "You must preserve the function signature in the template intact and **not modify it**!"
                )
            else:
                # Compare signatures
                template_signature = RustFnSignature(template_node)
                try:
                    signature = RustFnSignature(node)
                except ValueError:
                    signature = None

                if template_signature != signature:
                    scope_feedback["signature_changes"] = (
                        "You must preserve the function signature in the template intact and **not modify it**!"
                    )

    return scope_feedback


def mangle(name: str) -> str:
    # FIXME: It would be much nicer to let bindgen mangle names but need to feed the mangled name to --allowlist-function.
    # See: https://github.com/rust-lang/rust-bindgen/blob/b7b501feb2642b6ac3796f8c5f2a1461640a2a67/bindgen/ir/context.rs#L859-L887
    if (
        "@" in name
        or "?" in name
        or "$" in name
        or name in ("abstract", "alignof", "as", "async", "await", "become", "box", "break")
        or name in ("const", "continue", "crate", "do", "dyn", "else", "enum", "extern")
        or name in ("false", "final", "fn", "for", "gen", "if", "impl", "in")
        or name in ("let", "loop", "macro", "match", "mod", "move", "mut", "offsetof")
        or name in ("override", "priv", "proc", "pub", "pure", "ref", "return", "Self")
        or name in ("self", "sizeof", "static", "struct", "super", "trait", "true", "try")
        or name in ("type", "typeof", "unsafe", "unsized", "use", "virtual", "where", "while")
        or name in ("yield", "str", "bool", "f32", "f64", "usize", "isize", "u128")
        or name in ("i128", "u64", "i64", "u32", "i32", "u16", "i16", "u8", "i8", "_")
    ):
        name = name.replace("@", "_")
        name = name.replace("?", "_")
        name = name.replace("$", "_")
        name += "_"
    return name


def _rust_node_signature(node: Node, source: bytes, delete: bool = False) -> str | None:
    ntype = node.type
    if ntype in ("function_item", "function_signature_item"):
        if delete:
            return None

        # Find the block body and remove it
        body = node.child_by_field_name("body")
        if body:
            # Everything before the body is the signature
            sig = source[node.start_byte : body.start_byte].rstrip()
            return sig.decode() + ";"

    # Recurse into impl/trait/mod bodies so nested functions are also stripped/deleted
    if ntype in ("impl_item", "trait_item", "mod_item"):
        body = node.child_by_field_name("body")
        if body is not None:
            header = source[node.start_byte : body.start_byte].decode()
            parts = [
                sig
                for child in body.named_children
                if (sig := _rust_node_signature(child, source, delete=delete)) is not None
            ]
            if not parts:
                return header + "{}"
            return header + "{\n" + indent("\n".join(parts), " " * 4) + "\n}"

    # Keep everything else as-is
    return source[node.start_byte : node.end_byte].decode()


def strip_fns(code: CodeRust, delete: bool = False) -> CodeRust:
    if not str(code).strip():
        return code

    source = str(code).encode()
    root = get_root(source)
    parts: list[str] = []

    for node in root.children:
        sig = _rust_node_signature(node, source, delete=delete)
        if sig is not None:
            parts.append(sig)

    return CodeRust("\n".join(parts)) if parts else CodeRust("")
