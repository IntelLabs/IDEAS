#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#


import pytest
from pathlib import Path

from tree_sitter import Language, Parser
import tree_sitter_rust as tsrust

from ideas.ast_rust import get_function_names_in_module, extract_info_rust


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "ast_rust"


@pytest.fixture
def functions(fixtures_dir: Path) -> str:
    return (fixtures_dir / "functions.rs").read_text()


def test_extract_functions_rust(functions: str):
    # Parse the code
    parser = Parser(Language(tsrust.language()))
    source_bytes = functions.encode("utf-8")
    tree = parser.parse(source_bytes)
    root_node = tree.root_node
    function_names = get_function_names_in_module(
        root_node=root_node, source_code=functions, module_name=None
    )

    # Check that all expected functions are extracted
    expected_functions = [
        "simple_public",
        "private_function",
        "unsafe_function",
        "const_function",
        "async_function",
        "generic_function",
        "lifetime_function",
        "where_clause_function",
        "ffi_function",
        "attributed_function",
        "complex_return",
        "multi_lifetime",
        "system_abi_function",
        "complex_generic",
        # FFI signature declarations
        "external_c_function",
        "unsafe_external_function",
        "printf",
        "system_api_call",
    ]

    for func in expected_functions:
        assert func in function_names


def test_extract_info_rust(functions: str):
    result = extract_info_rust(functions)

    assert "simple_public" in result.symbols
    assert (
        result.fn_definitions["simple_public"]
        == """
pub fn simple_public() {
    println!("Hello");
}""".strip()
    )

    assert "private_function" in result.symbols
    assert (
        result.fn_definitions["private_function"]
        == """
fn private_function() {
    println!("Hello");
}""".strip()
    )

    assert "unsafe_function" in result.symbols
    assert (
        result.fn_definitions["unsafe_function"]
        == """
pub unsafe fn unsafe_function(ptr: *const i32) {
    println!("Hello");
}""".strip()
    )

    assert "const_function" in result.symbols
    assert (
        result.fn_definitions["const_function"]
        == """
pub const fn const_function(x: i32) -> i32 {
    println!("Hello");
    x * 2
}""".strip()
    )

    assert "async_function" in result.symbols
    assert (
        result.fn_definitions["async_function"]
        == """
pub async fn async_function() {
    println!("Hello");
}""".strip()
    )

    assert "generic_function" in result.symbols
    assert (
        result.fn_definitions["generic_function"]
        == """
pub fn generic_function<T: std::fmt::Display>(value: T) {
    println!("Hello");
}""".strip()
    )

    assert "lifetime_function" in result.symbols
    assert (
        result.fn_definitions["lifetime_function"]
        == """
pub fn lifetime_function<'a>(s: &'a str) -> &'a str {
    println!("Hello");
    s
}""".strip()
    )

    assert "where_clause_function" in result.symbols
    assert (
        result.fn_definitions["where_clause_function"]
        == """
pub fn where_clause_function<T>(value: T) where T: Clone + std::fmt::Debug {
    println!("Hello");
}""".strip()
    )

    assert "ffi_function" in result.symbols
    assert (
        result.fn_definitions["ffi_function"]
        == """
pub extern "C" fn ffi_function(x: c_int) -> c_int {
    println!("Hello");
    x
}""".strip()
    )

    assert "attributed_function" in result.symbols
    assert (
        result.fn_definitions["attributed_function"]
        == """
pub fn attributed_function() -> i32 {
    println!("Hello");
    42
}""".strip()
    )

    assert "complex_return" in result.symbols
    assert (
        result.fn_definitions["complex_return"]
        == """
pub fn complex_return() -> Result<Vec<Option<String>>, Box<dyn std::error::Error>> {
    println!("Hello");
    Ok(vec![])
}""".strip()
    )

    assert "multi_lifetime" in result.symbols
    assert (
        result.fn_definitions["multi_lifetime"]
        == """
pub fn multi_lifetime<'a, 'b>(x: &'a mut i32, y: &'b str) -> &'a i32 {
    println!("Hello");
    x
}""".strip()
    )

    assert "system_abi_function" in result.symbols
    assert (
        result.fn_definitions["system_abi_function"]
        == """
pub unsafe extern "system" fn system_abi_function(code: i32) {
    println!("Hello");
}""".strip()
    )

    assert "complex_generic" in result.symbols
    assert (
        result.fn_definitions["complex_generic"]
        == """
pub fn complex_generic<T, U>(first: T, second: U) -> String
where
    T: std::fmt::Display + Clone,
    U: std::fmt::Debug + Send + Sync,
{
    println!("Hello");
    format!("{}", first)
}""".strip()
    )

    # Check FFI function signatures
    assert "external_c_function" in result.symbols
    assert (
        result.fn_definitions["external_c_function"]
        == 'extern "C" { pub fn external_c_function(x: c_int) -> c_int; }'
    )

    assert "unsafe_external_function" in result.symbols
    assert (
        result.fn_definitions["unsafe_external_function"]
        == 'extern "C" { unsafe fn unsafe_external_function(ptr: *mut u8, len: usize); }'
    )

    assert "printf" in result.symbols
    assert (
        result.fn_definitions["printf"]
        == 'extern "C" { fn printf(format: *const u8, ...) -> c_int; }'
    )
    assert "system_api_call" in result.symbols
    assert (
        result.fn_definitions["system_api_call"]
        == 'extern "system" { pub fn system_api_call(code: u32) -> i32; }'
    )

    # Check FFI variables for not being captured
    assert "EXTERNAL_GLOBAL" not in result.symbols
