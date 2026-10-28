#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import re
import difflib
from collections import Counter, OrderedDict

from tree_sitter import Language, Parser, Node, Query, QueryCursor
import tree_sitter_rust_orchard

from .adapters import Code

# Initialize the Rust language once
RUST_LANGUAGE = Language(tree_sitter_rust_orchard.language())
RUST_PARSER = Parser(RUST_LANGUAGE)


class CodeRust(Code):
    language = "rust"


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

        # The exact source from the first qualifier up to the body, without attributes
        head = [child for child in node.children if child.type != "attributes"]
        body = node.child_by_field_name("body")
        start = head[0].start_byte if head else node.start_byte
        end = body.start_byte if body else node.end_byte
        self.text: str = (
            (node.text or b"")[start - node.start_byte : end - node.start_byte].decode().strip()
        )
        # Doc comments share the `attributes` node but are prose, not behavior
        self.attributes: list[str] = [
            get_node_text(attribute)
            for child in node.children
            if child.type == "attributes"
            for attribute in child.named_children
            if attribute.type == "attribute_item"
        ]

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

        # The whole head, since dropping `unsafe` or `extern "C"` still compiles and links
        return self.text == other.text


def attribute_feedback(template: RustFnSignature, signature: RustFnSignature) -> str | None:
    missing_attributes = Counter(template.attributes) - Counter(signature.attributes)
    if not missing_attributes:
        return None
    expected = "\n".join(template.attributes)
    return (
        f"The attributes of `{get_node_text(template.name)}` do not match the template. "
        f"Keep every template attribute exactly as written. Additional attributes are allowed.\n"
        f"Required attributes:\n~~~rust\n{expected}\n~~~"
    )


def signature_feedback(template: RustFnSignature, signature: RustFnSignature | None) -> str:
    feedback = (
        f"The signature of `{get_node_text(template.name)}` does not match the template. It "
        "must be copied character-for-character: the same line breaks, spaces, and "
        "punctuation, without adding or removing trailing commas. Only the function body may "
        f"change.\nExpected exactly:\n~~~rust\n{template.text}\n~~~"
    )
    if signature is None:
        return feedback

    diff = difflib.unified_diff(
        template.text.splitlines(),
        signature.text.splitlines(),
        fromfile="template",
        tofile="yours",
        lineterm="",
    )
    return feedback + "\nYour signature differs:\n~~~diff\n" + "\n".join(diff) + "\n~~~"


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


# Only these share the value namespace with an exported `fn` or an extern static
_VALUE_ITEMS = ("function_item", "static_item", "const_item")


def top_level_value_names(code: CodeRust) -> set[str]:
    # FIXME: Misses value names from `extern` blocks, `use` imports, unit/tuple struct
    # constructors and macro-generated items
    return {
        get_node_text(node.child_by_field_name("name"))
        for node in get_root(str(code)).children
        if node.type in _VALUE_ITEMS
    }


_PUBLIC_ITEMS = (
    "function_item",
    "struct_item",
    "union_item",
    "enum_item",
    "type_item",
    "static_item",
    "const_item",
    "trait_item",
)


def _is_pub(node: Node) -> bool:
    vis = next((c for c in node.children if c.type == "visibility_modifier"), None)
    return get_node_text(vis) == "pub"


def non_public_items(code: CodeRust) -> list[str]:
    missing = []
    for item in get_root(str(code)).children:
        if item.type not in _PUBLIC_ITEMS:
            continue
        name = get_node_text(item.child_by_field_name("name"))
        if not _is_pub(item):
            missing.append(name)
        body = item.child_by_field_name("body")
        if body is None or item.type not in ("struct_item", "union_item"):
            continue
        if body.type == "field_declaration_list":
            for field in body.named_children:
                if field.type == "field_declaration" and not _is_pub(field):
                    missing.append(f"{name}.{get_node_text(field.child_by_field_name('name'))}")
        elif body.type == "ordered_field_declaration_list":
            # Tuple fields have no wrapper node: a visibility applies to the type after it
            index, pub = 0, False
            for child in body.named_children:
                if child.type == "visibility_modifier":
                    pub = get_node_text(child) == "pub"
                elif child.type not in ("attributes", "attribute_item"):
                    if not pub:
                        missing.append(f"{name}.{index}")
                    index, pub = index + 1, False
    return missing


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
                    "You must preserve the function signature in `example_wrapper` intact (including **any** formatting) and only modify the function body!"
                )
            else:
                # Compare signatures
                template_signature = RustFnSignature(template_node)
                try:
                    signature = RustFnSignature(node)
                except ValueError:
                    signature = None

                if template_signature != signature:
                    scope_feedback["signature_changes"] = signature_feedback(
                        template_signature, signature
                    )
                if signature is not None and (
                    feedback := attribute_feedback(template_signature, signature)
                ):
                    scope_feedback["attribute_changes"] = feedback

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


def strip_cfg_test(code: CodeRust) -> CodeRust:
    source = str(code).encode()
    kept: list[bytes] = []
    start = 0
    # Outer attributes arrive either as siblings before the item or nested inside it
    attributes: list[Node] = []
    for node in get_root(source).children:
        if node.type == "attribute_item":
            attributes.append(node)
            continue
        nested = [
            attribute
            for child in node.children
            if child.type == "attributes"
            for attribute in child.children
        ]
        if any(
            "".join(get_node_text(a).split()) == "#[cfg(test)]" for a in attributes + nested
        ):
            kept.append(source[start : (attributes or [node])[0].start_byte])
            start = node.end_byte
        attributes.clear()
    kept.append(source[start:])
    return CodeRust(b"".join(kept).decode())


_EXPORT_ATTRIBUTE = re.compile(r"#\[(unsafe\()?(no_mangle|export_name)\b")


def strip_exports(code: CodeRust) -> CodeRust:
    # Rust can still name the items, but the linker no longer exports them
    source = str(code).encode()
    kept: list[bytes] = []
    start = 0
    for node in get_root(source).children:
        attributes = (
            [node]
            if node.type == "attribute_item"
            else [
                attribute
                for child in node.children
                if child.type == "attributes"
                for attribute in child.children
            ]
        )
        for attribute in attributes:
            if _EXPORT_ATTRIBUTE.match("".join(get_node_text(attribute).split())):
                kept.append(source[start : attribute.start_byte])
                # The attribute's own line break goes with it
                end = attribute.end_byte
                while end < len(source) and source[end : end + 1].isspace():
                    end += 1
                start = end
    kept.append(source[start:])
    return CodeRust(b"".join(kept).decode())


def strip_visibility(code: CodeRust) -> CodeRust:
    # Drop the visibility of top-level items, so only their own crate can name them
    source = str(code).encode()
    kept: list[bytes] = []
    start = 0
    for node in get_root(source).children:
        for child in node.children:
            if child.type == "visibility_modifier":
                kept.append(source[start : child.start_byte])
                # The whitespace after it goes with it
                end = child.end_byte
                while end < len(source) and source[end : end + 1].isspace():
                    end += 1
                start = end
    kept.append(source[start:])
    return CodeRust(b"".join(kept).decode())
