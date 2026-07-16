#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from pathlib import Path

import pytest

from ideas import tools
from ideas.ast import CodeC


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "compile"


@pytest.fixture
def c_paths(fixtures_dir: Path) -> tuple[Path, ...]:
    return fixtures_dir / "hello_world_good.c", fixtures_dir / "hello_world_bad.c"


@pytest.fixture
def rust_paths(fixtures_dir: Path) -> tuple[Path, Path]:
    return fixtures_dir / "hello_world_good.rs", fixtures_dir / "hello_world_bad.rs"


def test_check_c(c_paths: tuple[Path, ...]):
    # Compilation should succeed
    success1, out1 = tools.check_c(CodeC(c_paths[0].read_text()))
    assert success1, out1
    assert out1 == ""

    # Compilation should fail
    success2, out2 = tools.check_c(CodeC(c_paths[1].read_text()))
    assert not success2, out2
    assert out2 != ""


def test_check_rust(rust_paths: tuple[Path, Path]):
    # Compilation should succeed
    success1, out1 = tools.check_rust(rust_paths[0].read_text())
    assert success1
    assert out1 == ""

    # Compilation should fail
    success2, out2 = tools.check_rust(rust_paths[1].read_text())
    assert not success2
    assert out2 != ""


@pytest.fixture
def echo_123(fixtures_dir: Path) -> Path:
    return fixtures_dir / "echo_123"


def test_run_and_check_test_args_in(echo_123):
    assert tools.run_and_check_test(echo_123, {"args": None, "in": None, "out": "1 2 3"})
    assert tools.run_and_check_test(echo_123, {"args": [], "in": [], "out": "1 2 3"})
    assert tools.run_and_check_test(echo_123, {"args": "", "in": "", "out": "1 2 3"})


def test_run_and_check_test_in_only(echo_123):
    assert tools.run_and_check_test(echo_123, {"in": None, "out": "1 2 3"})
    assert tools.run_and_check_test(echo_123, {"in": [], "out": "1 2 3"})
    assert tools.run_and_check_test(echo_123, {"in": "", "out": "1 2 3"})


def test_run_and_check_test_args_only(echo_123):
    assert tools.run_and_check_test(echo_123, {"args": None, "out": "1 2 3"})
    assert tools.run_and_check_test(echo_123, {"args": [], "out": "1 2 3"})
    assert tools.run_and_check_test(echo_123, {"args": "", "out": "1 2 3"})


def test_run_and_check_test(echo_123):
    assert tools.run_and_check_test(echo_123, {"out": "1 2 3"})
    assert tools.run_and_check_test(echo_123, {"out": ["1 2 3"]})


def test_run_and_check_test_echo_args():
    assert tools.run_and_check_test("echo", {"args": ["1", "2", "3"], "out": "1 2 3"})
    assert tools.run_and_check_test("echo", {"args": "1 2 3", "out": "1 2 3"})


def test_run_and_check_test_echo_args_number():
    assert tools.run_and_check_test("echo", {"args": [1, 2, 3], "out": "1 2 3"})
    assert tools.run_and_check_test("echo", {"args": [1.0, 2.0, 3.0], "out": "1.0 2.0 3.0"})


def test_run_and_check_test_missing_out():
    with pytest.raises(Exception):
        tools.run_and_check_test("echo", {"args": "", "in": ""})


@pytest.fixture
def echo_stdin(fixtures_dir: Path) -> Path:
    return fixtures_dir / "echo_stdin"


def test_run_and_check_test_echo_stdin_str(echo_stdin):
    assert tools.run_and_check_test(echo_stdin, {"in": "1 2 3", "out": "1 2 3"})
    assert tools.run_and_check_test(echo_stdin, {"in": "1 2 3\n", "out": "1 2 3"})


def test_run_and_check_test_echo_stdin_str_newlines(echo_stdin):
    assert tools.run_and_check_test(echo_stdin, {"in": "1\n2\n3", "out": "1\n2\n3"})
    assert tools.run_and_check_test(echo_stdin, {"in": "1\n2\n3\n", "out": "1\n2\n3"})
    assert tools.run_and_check_test(echo_stdin, {"in": "1\n2\n3\n", "out": [1, 2, 3]})
    assert tools.run_and_check_test(echo_stdin, {"in": "1\n2\n3\n", "out": ["1", "2", "3"]})


def test_run_and_check_test_echo_stdin_list(echo_stdin):
    assert tools.run_and_check_test(echo_stdin, {"in": ["1 2 3"], "out": "1 2 3"})
    assert tools.run_and_check_test(echo_stdin, {"in": ["1 2 3\n"], "out": "1 2 3"})


def test_run_and_check_test_echo_stdin_list_newlines(echo_stdin):
    assert tools.run_and_check_test(echo_stdin, {"in": ["1", "2", "3"], "out": "1\n2\n3"})
    assert tools.run_and_check_test(echo_stdin, {"in": ["1", "2", "3", "\n"], "out": "1\n2\n3"})
    assert tools.run_and_check_test(echo_stdin, {"in": ["1", "2", "3"], "out": [1, 2, 3]})
    assert tools.run_and_check_test(echo_stdin, {"in": ["1", "2", "3"], "out": ["1", "2", "3"]})
