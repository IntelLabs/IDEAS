#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import pytest
import subprocess
from pathlib import Path

from ideas import convert_tests


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "text_processor"


@pytest.fixture
def cargo_toml(fixtures_dir: Path) -> Path:
    return fixtures_dir / "Cargo.toml"


@pytest.fixture
def rust_tests_harness(fixtures_dir: Path) -> Path:
    return fixtures_dir / "tests" / "test_cases.rs"


@pytest.fixture
def json_test_cases(fixtures_dir: Path) -> list[Path]:
    test_case_files = sorted((fixtures_dir / "json_test_cases").glob("test*.json"))
    return test_case_files


def test_convert_to_cargo_test(
    json_test_cases: list[Path], cargo_toml: Path, rust_tests_harness: Path
):
    # Write tests to tests/test_cases.rs
    test_cases = convert_tests.convert_tests_for_exec(json_test_cases)
    original_harness = rust_tests_harness.read_text()
    with open(rust_tests_harness, "w") as f:
        f.write(test_cases)

    # Execute cargo test --test test_cases
    result = subprocess.run(
        ["cargo", "test", "--manifest-path", cargo_toml, "--test", "test_cases"],
        capture_output=True,
        text=True,
    )
    assert (
        "test result: ok. 5 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out;"
        in result.stdout
    )

    # Restore the original tests/test_cases.rs
    with open(rust_tests_harness, "w") as f:
        f.write(original_harness)
