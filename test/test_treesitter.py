#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import pytest
from pathlib import Path

from ideas import treesitter


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "ast"


@pytest.fixture
def rust_code(fixtures_dir: Path) -> str:
    return (fixtures_dir / "signatures.rs").read_text()


def test_info_rust(rust_code: str):
    expected = [
        "fn add(a: i32, b: i32) -> i32",
        "pub fn multiply(x: f64, y: f64) -> f64",
        "fn identity<T>(value: T) -> T",
        "fn calculate(&self, a: i32, b: i32) -> i32",
        "fn reset(&mut self)",
        "fn printf(format: *const i8, ...) -> c_int",
        "fn malloc(size: usize) -> *mut u8",
    ]

    result = treesitter.extract_info_rust(rust_code)
    signatures = result.fn_definitions.keys()
    for sig, exp in zip(signatures, expected):
        assert sig == exp
