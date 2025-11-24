#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import re
import pytest

from pathlib import Path

from ideas.ast_rust import extract_info_rust
from ideas.ast_rust import ensure_no_mangle_in_module


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "ast_rust"


@pytest.fixture
def no_mangle_code(fixtures_dir: Path) -> str:
    return (fixtures_dir / "no_mangle.rs").read_text()


@pytest.fixture
def test_rust_file(tmp_path: Path, no_mangle_code: str) -> Path:
    """Create a temporary copy of the no_mangle.rs file for testing."""
    test_file = tmp_path / "no_mangle_test.rs"
    test_file.write_text(no_mangle_code, encoding="utf-8")
    return test_file


def extract_function_with_attributes(source_code: str, function_name: str) -> str:
    # Match attributes and function signature for the given function
    pattern = rf"(?:^#\[.*\].*\n)*^fn {re.escape(function_name)}\(\)"
    match = re.search(pattern, source_code, re.MULTILINE)
    if not match:
        raise ValueError(f"Function {function_name} not found")
    return match.group(0)


def test_add_no_mangle_top(test_rust_file: Path):
    original_code = test_rust_file.read_text(encoding="utf-8")
    # Add the no_mangle attribute at the top-level and inside the module
    modified_code = ensure_no_mangle_in_module(original_code, module_name=None, add=True)
    modified_code = ensure_no_mangle_in_module(modified_code, module_name="foo", add=True)
    info = extract_info_rust(modified_code)

    for function_name in [
        "no_attributes",
        "needs_unsafe_single",
        "needs_unsafe_same_line",
        "needs_unsafe_different_lines",
        "needs_unsafe_between",
        "needs_unsafe_irregular_1_2",
        "needs_unsafe_irregular_2_1",
        "already_safe",
        "already_safe_with_others",
        "other_attributes_only",
        "needs_unsafe_three_same_line",
        "extern_c_function",
        "extern_c_with_args",
        "namespaced_function",
    ]:
        attributes = info.symbols[function_name].attributes
        assert function_name in info.symbols
        assert attributes is not None
        assert "#[unsafe(no_mangle)]" in attributes
        assert "#[no_mangle]" not in attributes


def test_patch_no_mangle(no_mangle_code: str):
    # Apply the patching function at the top-level
    modified_code = ensure_no_mangle_in_module(no_mangle_code, module_name=None, add=False)
    info = extract_info_rust(modified_code)

    # Check each expected function
    for function_name in [
        "needs_unsafe_single",
        "needs_unsafe_same_line",
        "needs_unsafe_different_lines",
        "needs_unsafe_between",
        "needs_unsafe_irregular_1_2",
        "needs_unsafe_irregular_2_1",
        "already_safe",
        "already_safe_with_others",
        "needs_unsafe_three_same_line",
        "extern_c_function",
        "extern_c_with_args",
    ]:
        attributes = info.symbols[function_name].attributes
        assert function_name in info.symbols
        assert attributes is not None
        assert "#[unsafe(no_mangle)]" in attributes
        assert "#[no_mangle]" not in attributes

    # Apply it inside the module
    modified_code = ensure_no_mangle_in_module(no_mangle_code, module_name="foo", add=False)
    info = extract_info_rust(modified_code)

    # Check the namespaced function
    function_name = "namespaced_function"
    attributes = info.symbols[function_name].attributes
    assert function_name in info.symbols
    assert attributes is not None
    assert "#[unsafe(no_mangle)]" in attributes
    assert "#[no_mangle]" not in attributes
