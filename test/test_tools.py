#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from contextlib import nullcontext as does_not_raise
from pathlib import Path

import pytest

from ideas import tools


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "compile"


@pytest.fixture
def c_paths(fixtures_dir: Path) -> tuple[Path, ...]:
    return fixtures_dir / "hello_world_good.c", fixtures_dir / "hello_world_bad.c"


@pytest.fixture
def c_files(c_paths: tuple[Path, Path]) -> tuple[str, ...]:
    return str(c_paths[0]), str(c_paths[1])


@pytest.fixture
def rust_paths(fixtures_dir: Path) -> tuple[Path, Path]:
    return fixtures_dir / "hello_world_good.rs", fixtures_dir / "hello_world_bad.rs"


@pytest.fixture
def rust_files(rust_paths: tuple[Path, Path]) -> tuple[str, str]:
    return str(rust_paths[0]), str(rust_paths[1])


@pytest.fixture
def clippy_files(fixtures_dir: Path) -> str:
    return str(fixtures_dir / "clippy.rs")


def test_compile_c(c_files: tuple[str, str], tmpdir: Path):
    # Compilation should succeed
    success1, out1 = tools.compile_c(c_files[0], str(tmpdir / "out"))
    assert success1
    assert out1 == ""

    # Compilation should fail
    success2, out2 = tools.compile_c(c_files[1], str(tmpdir / "out"))
    assert not success2
    assert out2 != ""


def test_check_c(c_paths: tuple[Path, ...]):
    # Compilation should succeed
    success1, out1 = tools.check_c(c_paths[0].read_text())
    assert success1, out1
    assert out1 == ""

    # Compilation should fail
    success2, out2 = tools.check_c(c_paths[1].read_text())
    assert not success2, out2
    assert out2 != ""


def test_compile_rust(rust_files: tuple[str, str], tmpdir: Path):
    # Compilation should succeed
    success1, out1 = tools.compile_rust(Path(rust_files[0]).read_text(), tmpdir / "out")
    assert success1
    assert out1 == ""

    # Compilation should fail
    success2, out2 = tools.compile_rust(Path(rust_files[1]).read_text(), tmpdir / "out")
    assert not success2
    assert out2 != ""


def test_clippy(clippy_files: str):
    # All clippy calls should trigger
    all_out = tools.run_clippy(clippy_files)
    successes, outputs = zip(*all_out)
    assert not any(successes)
    assert not any(map(lambda out: out == "", outputs))


def test_structured(rust_files: tuple[str, str], clippy_files: str, tmpdir: Path):
    # JSON dict construction should succeed
    _, structured_output = tools.compile_rust(
        Path(rust_files[1]).read_text(), tmpdir / "out", structured_output=True
    )

    with does_not_raise():
        as_json = tools.tool_output_to_js_dict(structured_output)

    # Message rendered from the JSON dict should be identical to the original render
    _, rendered_og_all = tools.compile_rust(Path(rust_files[1]).read_text(), tmpdir / "out")
    rendered_reconstructed = tools.structured_to_rendered(as_json)
    # FIXME: strip at the call sites vs somewhere in tools.py
    assert rendered_reconstructed.rstrip() == rendered_og_all.rstrip()

    # JSON dict construction should succeed
    all_out = tools.run_clippy(clippy_files, structured_output=True)
    _, structured_outputs = zip(*all_out)
    structured_outputs = list(structured_outputs)

    with does_not_raise():
        as_json_all = tools.tool_output_to_js_dict(structured_outputs)
        as_json_individual = [tools.tool_output_to_js_dict(so) for so in structured_outputs]

    # Messages rendered from the JSON dict should be identical to the original render
    all_out = tools.run_clippy(clippy_files)
    _, outputs = zip(*all_out)
    rendered_og_all = list(outputs)

    rendered_reconstructed_all = tools.structured_to_rendered(as_json_all)
    assert rendered_reconstructed_all == "".join(rendered_og_all)

    rendered_reconstructed_individual = [
        tools.structured_to_rendered(so) for so in as_json_individual
    ]
    the_same = map(
        lambda og, recon: og == recon, rendered_og_all, rendered_reconstructed_individual
    )
    assert all(the_same)


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
