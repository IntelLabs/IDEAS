#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from collections import OrderedDict

from tree_sitter import Language, Parser, Node, Query, QueryCursor
import tree_sitter_rust

# Initialize the Rust language once
RUST_LANGUAGE = Language(tree_sitter_rust.language())
RUST_PARSER = Parser(RUST_LANGUAGE)


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


def get_root(code: str) -> Node:
    tree = RUST_PARSER.parse(code.encode())
    return tree.root_node


def get_nodes(node: Node, node_type: str | None = None) -> list[Node]:
    nodes = []
    for child in node.children:
        if not node_type or child.type == node_type:
            nodes.append(child)
    return nodes


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


def validate_changes(code: str, template: str) -> OrderedDict[str, str]:
    code_root = get_root(code)
    template_root = get_root(template)

    nodes = get_nodes(code_root)
    template_nodes = get_nodes(template_root)
    allowed_change_nodes = get_macro_nodes(template_root, "unimplemented")

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
