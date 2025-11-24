#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from typing import Callable
from tree_sitter import Language, Parser, Node
import tree_sitter_rust as tsrust

from .utils import RustSymbol

logger = logging.getLogger("ideas.ast_rust")


@dataclass
class RustTreeResult:
    symbols: dict[str, RustSymbol] = field(default_factory=dict)
    fn_definitions: dict[str, str | None] = field(
        default_factory=lambda: defaultdict(lambda: None)
    )


def extract_all_attributes(function_node: Node, source_code: str) -> list[str]:
    attributes = []
    prev_sibling = function_node.prev_sibling
    while prev_sibling and prev_sibling.type in [
        "attribute_item",
        "line_comment",
        "block_comment",
    ]:
        if prev_sibling.type == "attribute_item":
            attr_text = source_code[prev_sibling.start_byte : prev_sibling.end_byte]
            attributes.append(attr_text)
        prev_sibling = prev_sibling.prev_sibling

    return list(reversed(attributes))


def has_attribute(function_node: Node, source_code: str, attribute: str) -> bool:
    prev_sibling = function_node.prev_sibling
    while prev_sibling and prev_sibling.type in [
        "attribute_item",
        "line_comment",
        "block_comment",
    ]:
        if prev_sibling.type == "attribute_item":
            attr_text = source_code[prev_sibling.start_byte : prev_sibling.end_byte]
            if attr_text == attribute:
                return True

        prev_sibling = prev_sibling.prev_sibling

    return False


def find_attribute_node(function_node: Node, source_code: str, attribute: str) -> Node | None:
    prev_sibling = function_node.prev_sibling
    while prev_sibling and prev_sibling.type in [
        "attribute_item",
        "line_comment",
        "block_comment",
    ]:
        if prev_sibling.type == "attribute_item":
            attr_text = source_code[prev_sibling.start_byte : prev_sibling.end_byte]
            if attr_text == attribute:
                return prev_sibling
        prev_sibling = prev_sibling.prev_sibling

    return None


def find_function_node(
    root_node: Node, source_code: str, symbol_name: str, module_name: str | None = None
) -> Node | None:
    def find_in_node(node: Node, in_target_module: bool = False) -> Node | None:
        for child in node.children:
            if child.type == "function_item":
                func_name = get_function_name(child, source_code)
                if func_name == symbol_name and (
                    (module_name is None and node == root_node) or in_target_module
                ):
                    return child

            elif child.type == "mod_item" and module_name is not None:
                name_node = child.child_by_field_name("name")
                if (
                    not name_node
                    or source_code[name_node.start_byte : name_node.end_byte] != module_name
                ):
                    continue

                # Search within this module's body
                body = child.child_by_field_name("body")
                if not body:
                    continue

                result = find_in_node(body, in_target_module=True)
                if result:
                    return result
        return None

    if module_name is None:
        # Search only top-level functions
        for node in root_node.children:
            if (
                node.type == "function_item"
                and get_function_name(node, source_code) == symbol_name
            ):
                return node
        return None
    else:
        # Search for function in the specified module
        return find_in_node(root_node)


def get_function_name(function_node: Node, source_code: str) -> str | None:
    for child in function_node.children:
        if child.type == "identifier":
            return source_code[child.start_byte : child.end_byte]

    return None


def get_extern_symbols(node: Node, source_code: str) -> list[RustSymbol]:
    symbols: list[RustSymbol] = []
    for child in node.children:
        # Only look for the "declaration_list" node
        if child.type != "declaration_list":
            continue

        for decl_child in child.children:
            # Only look for "function_signature_item" nodes
            if decl_child.type != "function_signature_item":
                continue

            name_node = decl_child.child_by_field_name("name")
            if not name_node:
                raise ValueError(
                    f"Encountered an extern ABI function signature item without a name in {source_code[decl_child.start_byte:decl_child.end_byte] = }!"
                )
            fn_name = source_code[name_node.start_byte : name_node.end_byte]
            fn_signature = source_code[decl_child.start_byte : decl_child.end_byte]
            attributes = extract_all_attributes(decl_child, source_code)

            symbols.append(RustSymbol(fn_name, decl_child, fn_signature, attributes=attributes))

    return symbols


def get_function_names_in_module(
    root_node: Node, source_code: str, module_name: str | None = None
) -> list[str]:
    function_names = []

    if module_name is None:
        # Search only top-level functions
        for node in root_node.children:
            if node.type == "function_item":
                func_name = get_function_name(node, source_code)
                if func_name:
                    function_names.append(func_name)
            elif node.type == "foreign_mod_item":
                extern_symbols = get_extern_symbols(node, source_code)
                function_names.extend([symbol.name for symbol in extern_symbols])
    else:
        # Find the module body
        module_node = find_module_body(root_node, source_code, module_name)
        if not module_node:
            raise ValueError(f"Module {module_name} not found!")

        # Extract all functions from the module
        for child in module_node.children:
            if child.type == "function_item":
                func_name = get_function_name(child, source_code)
                if func_name:
                    function_names.append(func_name)
            elif child.type == "foreign_mod_item":
                extern_symbols = get_extern_symbols(child, source_code)
                function_names.extend([symbol.name for symbol in extern_symbols])

    return function_names


def find_module_body(node: Node, source_code: str, module_name: str) -> Node | None:
    for child in node.children:
        if child.type == "mod_item":
            name_node = child.child_by_field_name("name")
            if (
                name_node
                and source_code[name_node.start_byte : name_node.end_byte] == module_name
            ):
                return child.child_by_field_name("body")

            # Check nested modules recursively
            body = child.child_by_field_name("body")
            if body:
                result = find_module_body(body, source_code, module_name)
                if result:
                    return result
    return None


def traverse_rust(source_code: str, node: Node) -> RustTreeResult:
    result = RustTreeResult()

    if node.type == "function_item":
        # Extract function name from the function_item node
        name_node = node.child_by_field_name("name")
        if name_node:
            fn_name = source_code[name_node.start_byte : name_node.end_byte]
            fn_definition = source_code[node.start_byte : node.end_byte]
            # Extract just the signature (everything before the body)
            body_node = node.child_by_field_name("body")
            if body_node:
                fn_signature = source_code[node.start_byte : body_node.start_byte]
            else:
                fn_signature = fn_definition
            attributes = extract_all_attributes(node, source_code)

            result.symbols[fn_name] = RustSymbol(
                fn_name, node, fn_signature, attributes=attributes
            )
            result.fn_definitions[fn_name] = fn_definition

    # Non-ABI nested function signatures (e.g., in trait definitions)
    elif node.type == "function_signature_item":
        name_node = node.child_by_field_name("name")
        if name_node:
            fn_name = source_code[name_node.start_byte : name_node.end_byte]
            fn_signature = source_code[node.start_byte : node.end_byte]
            attributes = extract_all_attributes(node, source_code)

            result.symbols[fn_name] = RustSymbol(
                fn_name, node, fn_signature, attributes=attributes
            )
            result.fn_definitions[fn_name] = fn_signature

    # Handle extern blocks with FFI function declarations
    elif node.type == "foreign_mod_item":
        # The ABI is the "string_literal" child of the "extern_modifier" child
        abi = None
        for child in node.children:
            if child.type != "extern_modifier":
                continue

            if abi:
                raise ValueError(
                    f"Multiple extern modifiers found in extern block {source_code[node.start_byte:node.end_byte] = }!"
                )

            for modifier_child in child.children:
                if modifier_child.type == "string_literal":
                    abi = source_code[modifier_child.start_byte : modifier_child.end_byte]
                    break

        if abi is None:
            raise ValueError(
                f"Unable to determine ABI for extern block {source_code[node.start_byte:node.end_byte] = }!"
            )

        # Extract all symbols in this extern block
        symbols: list[RustSymbol] = get_extern_symbols(node, source_code)
        for symbol in symbols:
            fn_name = symbol.name
            decl = symbol.decl

            # Construct standalone declaration with ABI specifier
            complete_decl = f"extern {abi} {{ {decl} }}"
            result.fn_definitions[fn_name] = complete_decl
            result.symbols[fn_name] = RustSymbol(
                fn_name, symbol.node, complete_decl, symbol.attributes
            )

        # Don't recursively traverse extern block children since we handled them with the ABI identifier
        return result

    # Recursively traverse all child nodes
    for child in node.children:
        child_result = traverse_rust(source_code, child)
        result.symbols.update(child_result.symbols)
        result.fn_definitions.update(child_result.fn_definitions)

    return result


def extract_info(code: str, traverse_fn: Callable, parser: Parser) -> RustTreeResult:
    tree = parser.parse(bytes(code, "utf8"))
    return traverse_fn(code, tree.root_node)


def extract_info_rust(code: str) -> RustTreeResult:
    lang = Language(tsrust.language())
    parser = Parser(lang)
    return extract_info(code, traverse_fn=traverse_rust, parser=parser)


def remove_attribute_from_fn(
    source_code: str,
    symbol_name: str,
    attr: str,
    module_name: str | None = None,
) -> str:
    # Parse the code
    parser = Parser(Language(tsrust.language()))
    source_bytes = source_code.encode("utf-8")
    tree = parser.parse(source_bytes)
    root_node = tree.root_node

    # Find the target function in the target module (or top-level if module_name is None)
    target_function = find_function_node(root_node, source_code, symbol_name, module_name)
    if target_function is None:
        logger.warning(
            f"Function {symbol_name} not found in {module_name or 'top-level'} module!"
        )
        return source_code

    # Find the attribute node
    attr_node = find_attribute_node(target_function, source_code, attr)
    if attr_node is None:
        logger.debug(f"Function {symbol_name} does not have attribute {attr}, no changes made.")
        return source_code

    # Remove the attribute from the source code
    modified_source = source_code[: attr_node.start_byte] + source_code[attr_node.end_byte :]

    return modified_source


def ensure_attribute_for_fn(
    source_code: str,
    symbol_name: str,
    attr: str,
    module_name: str | None = None,
) -> str:
    # 'main' should not have #[unsafe(no_mangle)] in Rust
    if symbol_name == "main" and attr == "#[unsafe(no_mangle)]":
        logger.warning("Skipping addition of #[unsafe(no_mangle)] to 'main' function!")
        return source_code

    # Parse the code
    parser = Parser(Language(tsrust.language()))
    source_bytes = source_code.encode("utf-8")
    tree = parser.parse(source_bytes)
    root_node = tree.root_node

    # Find the target function in the target module (or top-level if module_name is None)
    target_function = find_function_node(root_node, source_code, symbol_name, module_name)
    if target_function is None:
        logger.warning(
            f"Function {symbol_name} not found in {module_name or 'top-level'} module!"
        )
        return source_code

    # If function already has attribute, do nothing
    if has_attribute(target_function, source_code, attr):
        logger.debug(f"Function {symbol_name} already has attribute {attr}, no changes made.")
        return source_code

    # Add as the last attribute
    func_line_start = source_code.rfind("\n", 0, target_function.start_byte) + 1
    modified_source = (
        source_code[:func_line_start] + f"{attr}\n" + source_code[func_line_start:]
    )

    return modified_source


def ensure_no_mangle_in_module(
    source_code: str, module_name: str | None = None, add: bool = False
) -> str:
    """Ensure that all functions in the specified module have the #[unsafe(no_mangle)] attribute.

    Args:
        source_code (str): The Rust source code.
        module_name (str | None): The module name to target, or None for top-level
        add (bool): Whether to also add the attribute to all functions or only replace existing invalid occurrences.

    Returns:
        str: The modified source code.
    """
    # Parse the code
    parser = Parser(Language(tsrust.language()))
    source_bytes = source_code.encode("utf-8")
    tree = parser.parse(source_bytes)
    root_node = tree.root_node
    function_names = get_function_names_in_module(
        root_node=root_node, source_code=source_code, module_name=module_name
    )

    # Patch each function if needed
    modified_source = source_code
    for func_name in function_names:
        fn_node = find_function_node(root_node, source_code, func_name, module_name)
        if fn_node is None:
            raise ValueError(f"Function {func_name} not found in {module_name = }!")

        attributes = extract_all_attributes(fn_node, source_code)
        # Remove invalid use
        if "#[no_mangle]" in attributes:
            modified_source = remove_attribute_from_fn(
                modified_source, func_name, "#[no_mangle]", module_name=module_name
            )
        # Add the correct attribute
        if add or "#[no_mangle]" in attributes:
            modified_source = ensure_attribute_for_fn(
                modified_source, func_name, "#[unsafe(no_mangle)]", module_name=module_name
            )

    return modified_source
