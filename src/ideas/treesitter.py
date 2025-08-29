#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from typing import Callable

import tree_sitter_rust as ts_rs
from tree_sitter import Language, Parser, Node

from ideas import TreeResult


def traverse_rust(code: str, node: Node) -> TreeResult:
    result = TreeResult()

    if node.type == "function_item":
        def_text = code[node.start_byte : node.end_byte]
        brace_pos = def_text.find("{")
        if brace_pos != -1:
            signature = def_text[:brace_pos].strip()
            result.fn_definitions[signature] = result.fn_definitions.get(signature)

    # NOTE: need this for signatures in traits
    elif node.type == "function_signature_item":
        sig_text = code[node.start_byte : node.end_byte].strip()
        if sig_text.endswith(";"):
            sig_text = sig_text[:-1].strip()
        result.fn_definitions[sig_text] = result.fn_definitions.get(sig_text)

    # NOTE: need this for FFI signatures
    elif (
        node.type == "function_signature" and node.parent and node.parent.type == "extern_block"
    ):
        sig_text = code[node.start_byte : node.end_byte].strip()
        if sig_text.endswith(";"):
            sig_text = sig_text[:-1].strip()
        result.fn_definitions[sig_text] = result.fn_definitions.get(sig_text)

    for child in node.children:
        child_result = traverse_rust(code, child)
        result.fn_definitions.update(child_result.fn_definitions)

    return result


def extract_info(code: str, traverse_fn: Callable, parser: Parser) -> TreeResult:
    tree = parser.parse(bytes(code, "utf8"))
    return traverse_fn(code, tree.root_node)


def extract_info_rust(code: str) -> TreeResult:
    lang = Language(ts_rs.language())
    parser = Parser(lang)
    return extract_info(code, traverse_fn=traverse_rust, parser=parser)
