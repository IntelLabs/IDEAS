#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from pathlib import Path

import pytest

from ideas import tools


@pytest.fixture
def tmp_crate(tmp_path: Path):
    """Create a minimal lib crate with passing and failing integration tests."""
    crate_dir = tmp_path / "test_crate"
    crate = tools.Crate(cargo_toml=crate_dir / "Cargo.toml", vcs="none", template="lib")

    (crate_dir / "src" / "lib.rs").write_text("pub fn add(a: i32, b: i32) -> i32 { a + b }\n")

    tests_dir = crate_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_pass.rs").write_text(
        "use test_crate::add;\n"
        "#[test] fn pass_one() { assert_eq!(add(1, 2), 3); }\n"
        "#[test] fn pass_two() { assert_eq!(add(0, 0), 0); }\n"
    )
    (tests_dir / "test_mixed.rs").write_text(
        "use test_crate::add;\n"
        "#[test] fn mixed_pass() { assert_eq!(add(1, 1), 2); }\n"
        "#[test] fn mixed_fail() { assert_eq!(add(1, 1), 99); }\n"
    )
    (tests_dir / "test_ignored.rs").write_text(
        "#[test] fn runs() { assert!(true); }\n"
        '#[test] #[ignore] fn skipped() { panic!("should not run"); }\n'
    )
    (tests_dir / "test_stdout.rs").write_text(
        '#[test] fn noisy() { println!("hello from test"); assert!(true); }\n'
    )

    return crate


def _sorted_output(output: str) -> str:
    """Sort test result lines for deterministic comparison (nextest runs in parallel)."""
    lines = output.splitlines()
    test_lines = sorted(line for line in lines if line.startswith("test ") and "..." in line)
    rest = [line for line in lines if not (line.startswith("test ") and "..." in line)]
    return "\n".join(test_lines + rest) + "\n"


# --- cargo nextest run harness ---


def test_passing_json(tmp_crate):
    success, stdout, _, rc = tmp_crate.cargo_test(
        name="test_pass", message_format="libtest-json"
    )
    assert success is True
    assert rc == 0
    assert _sorted_output(tools.nextest_json_to_libtest(stdout)) == (
        "test pass_one ... ok\n"
        "test pass_two ... ok\n"
        "test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
    )


def test_failing_json(tmp_crate):
    success, stdout, _, rc = tmp_crate.cargo_test(
        name="test_mixed", message_format="libtest-json"
    )
    assert success is False
    assert rc == 100
    assert _sorted_output(tools.nextest_json_to_libtest(stdout)) == (
        "test mixed_fail ... FAILED\n"
        "test mixed_pass ... ok\n"
        "test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out\n"
    )


def test_ignored_json(tmp_crate):
    success, stdout, _, rc = tmp_crate.cargo_test(
        name="test_ignored", message_format="libtest-json"
    )
    assert success is True
    assert rc == 0
    assert _sorted_output(tools.nextest_json_to_libtest(stdout)) == (
        "test runs ... ok\n"
        "test result: ok. 1 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out\n"
    )


def test_stdout_not_in_output(tmp_crate):
    success, stdout, _, rc = tmp_crate.cargo_test(
        name="test_stdout", message_format="libtest-json"
    )
    assert success is True
    assert rc == 0
    assert _sorted_output(tools.nextest_json_to_libtest(stdout)) == (
        "test noisy ... ok\n"
        "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
    )


# --- cargo test harness ---


def test_cargo_test_passing(tmp_crate):
    success, stdout, _, rc = tmp_crate.cargo_test(
        name="test_pass", test_harness="test", quiet=False
    )
    assert success is True
    assert rc == 0
    assert "test pass_one ... ok" in stdout
    assert "test pass_two ... ok" in stdout
    assert "2 passed; 0 failed" in stdout


def test_cargo_test_failing(tmp_crate):
    success, stdout, _, rc = tmp_crate.cargo_test(
        name="test_mixed", test_harness="test", quiet=False
    )
    assert success is False
    assert rc == 101
    assert "test mixed_pass ... ok" in stdout
    assert "test mixed_fail ... FAILED" in stdout
    assert "1 passed; 1 failed" in stdout


def test_cargo_test_ignored(tmp_crate):
    success, stdout, _, rc = tmp_crate.cargo_test(
        name="test_ignored", test_harness="test", quiet=False
    )
    assert success is True
    assert rc == 0
    assert "test runs ... ok" in stdout
    assert "1 passed; 0 failed; 1 ignored" in stdout
