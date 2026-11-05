#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

# How IDEAS drives the real cargo-minify; tools/cargo-minify tests cargo-minify itself on -ffi crates

import os
import re
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ideas.minify import minify
from ideas.tools import Crate

from conftest import (
    CRATE,
    REPO_ROOT,
    TRANSLATION_DIR,
    TRANSLATION_TEST,
    build_ok,
    get_shipped_name,
    run_translate,
)
from test_final import _CALLBACK_C, _CALLBACK_H, _callback_codex

DEAD_FN = 'unsafe extern "C" fn dead_scaffolding() {}'

LIB_RS = f"""\
#[unsafe(export_name = "add")]
pub extern "C" fn add(a: i32, b: i32) -> i32 {{
    libx_rs::add(a, b)
}}

{DEAD_FN}
"""

# Ported tests, as `_port_tests` leaves them, with dead code cargo-minify must not reach
IO_RS = """\
use ::x as _;

unsafe extern "C" {
    fn add(a: i32, b: i32) -> i32;
}

fn dead_in_tests() {}

#[test]
fn adds() {
    assert_eq!(unsafe { add(2, 3) }, 5);
}

#[test]
fn fails() {
    assert_eq!(unsafe { add(2, 3) }, 6);
}
"""

RS_LIB_RS = """\
#![forbid(unsafe_code)]

pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

fn dead_rs() {}
"""


def _workspace(root: Path, lib_rs: str, io_rs: str = IO_RS) -> Crate:
    # A -ffi crate as `_init_ffi_crate` lays it out, next to the -rs crate it wraps
    (root / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["libx-rs", "libx-ffi"]\nresolver = "3"\n'
    )
    (root / ".gitignore").write_text("/target\n")
    rs = root / "libx-rs"
    (rs / "src").mkdir(parents=True)
    (rs / "Cargo.toml").write_text(
        '[package]\nname = "libx-rs"\nversion = "0.1.0"\nedition = "2024"\n'
    )
    (rs / "src" / "lib.rs").write_text(RS_LIB_RS)
    ffi = root / "libx-ffi"
    (ffi / "src").mkdir(parents=True)
    (ffi / "tests").mkdir()
    (ffi / "Cargo.toml").write_text(
        '[package]\nname = "libx-ffi"\nversion = "0.1.0"\nedition = "2024"\n\n'
        '[lib]\nname = "x"\ncrate-type = ["lib", "cdylib"]\ntest = false\ndoctest = false\n\n'
        '[dependencies]\nlibx-rs = { path = "../libx-rs" }\n'
    )
    (ffi / "src" / "lib.rs").write_text(lib_rs)
    (ffi / "tests" / "io.rs").write_text(io_rs)
    # Locked like a translated workspace, so that only minifying can change any file
    subprocess.run(
        [
            "cargo",
            "generate-lockfile",
            "--offline",
            "--manifest-path",
            str(root / "Cargo.toml"),
        ],
        check=True,
        capture_output=True,
    )
    return Crate(ffi / "Cargo.toml")


def _files(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and "target" not in path.relative_to(root).parts
    }


def _is_log(path: Path) -> bool:
    return path.suffix == ".log" or ".minify" in path.parts


def _exports(root: Path, crate: str, lib: str) -> set[str]:
    assert build_ok(root, crate), f"{crate} does not build"
    proc = subprocess.run(
        ["nm", "--dynamic", "--defined-only", str(root / "target" / "debug" / f"lib{lib}.so")],
        capture_output=True,
        text=True,
        check=True,
    )
    return {parts[-1] for line in proc.stdout.splitlines() if len(parts := line.split()) > 1}


def test_minify_undoes_a_real_regression(tmp_path: Path) -> None:
    # A test that reads the sources is a way dead code can change an outcome
    crate = _workspace(
        tmp_path,
        LIB_RS,
        io_rs="use ::x as _;\n\n#[test]\nfn keeps_dead_code() {\n"
        '    assert!(include_str!("../src/lib.rs").contains("dead_scaffolding"));\n}\n',
    )
    before = _files(tmp_path)
    crate.cargo_test = MagicMock(wraps=crate.cargo_test)

    with pytest.raises(RuntimeError):
        minify(crate, "io")

    # It minified, then reran the tests before undoing
    assert crate.cargo_test.call_count == 2
    assert _files(tmp_path) == before


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout


def _app(crate_dir: Path, tests: str) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        "-m",
        "ideas.minify",
        f"cargo_toml={crate_dir / 'Cargo.toml'}",
        f"tests={tests}",
        "vcs=git",
        "hydra.output_subdir=.minify",
        "hydra.job.name=minify",
        f"hydra.run.dir={crate_dir}",
    ]
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    return subprocess.run(cmd, env=env, capture_output=True, text=True)


@pytest.fixture
def git_workspace(tmp_path: Path) -> Path:
    _workspace(tmp_path, LIB_RS)
    _git(tmp_path, "init", "--initial-branch=main")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "translated")
    return tmp_path


def test_minify_app_commits_only_the_minified_ffi_sources(
    git_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pipeline silences warnings, which would leave cargo-minify nothing to remove
    monkeypatch.setenv("RUSTFLAGS", "-Awarnings")
    crate_dir = git_workspace / "libx-ffi"

    proc = _app(crate_dir, "io")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _git(git_workspace, "rev-list", "--count", "HEAD").strip() == "2"
    # Neither the ported tests nor -rs, despite their dead code
    committed = _git(git_workspace, "show", "--name-only", "--format=", "HEAD").split()
    assert [p for p in committed if not p.startswith("libx-ffi/.minify/")] == [
        "libx-ffi/minify.json",
        "libx-ffi/src/lib.rs",
    ]
    assert any(p.startswith("libx-ffi/.minify/") for p in committed)
    minified = _git(git_workspace, "show", "HEAD:libx-ffi/src/lib.rs")
    assert "dead_scaffolding" not in minified
    assert minified.startswith('#[unsafe(export_name = "add")]')
    before, after = len(LIB_RS.splitlines()), len(minified.splitlines())
    assert json.loads((crate_dir / "minify.json").read_text()) == {
        "lines_before": before,
        "lines_after": after,
        "lines_deleted": before - after,
    }
    # Only Hydra's log stays uncommitted, as for every other app
    assert _git(git_workspace, "status", "--porcelain").split() == ["??", "libx-ffi/minify.log"]


def test_minify_app_commits_a_rerun_once_then_leaves_the_history(
    git_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUSTFLAGS", "-Awarnings")
    crate_dir = git_workspace / "libx-ffi"
    assert _app(crate_dir, "io").returncode == 0
    minified = _git(git_workspace, "show", "HEAD:libx-ffi/src/lib.rs")

    proc = _app(crate_dir, "io")

    # The stats describe the last run, which deleted nothing
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _git(git_workspace, "rev-list", "--count", "HEAD").strip() == "3"
    committed = _git(git_workspace, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == ["libx-ffi/minify.json"]
    assert _git(git_workspace, "show", "HEAD:libx-ffi/src/lib.rs") == minified
    lines = len(minified.splitlines())
    assert json.loads((crate_dir / "minify.json").read_text()) == {
        "lines_before": lines,
        "lines_after": lines,
        "lines_deleted": 0,
    }
    head = _git(git_workspace, "rev-parse", "HEAD")
    status = _git(git_workspace, "status", "--porcelain")

    proc = _app(crate_dir, "io")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _git(git_workspace, "rev-parse", "HEAD") == head
    assert _git(git_workspace, "status", "--porcelain") == status


def test_minify_app_skips_without_tests(git_workspace: Path) -> None:
    crate_dir = git_workspace / "libx-ffi"
    head = _git(git_workspace, "rev-parse", "HEAD")

    proc = _app(crate_dir, "null")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (crate_dir / "minify.json").exists()
    assert _git(git_workspace, "rev-parse", "HEAD") == head
    assert _git(git_workspace, "diff", "--name-only", "HEAD") == ""


def test_make_minify_strips_only_dead_scaffolding_from_a_translated_ffi_crate(
    instrumented_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = instrumented_workspace("lib", header=_CALLBACK_H, source=_CALLBACK_C)
    run_translate(monkeypatch, ws, "lib", _callback_codex("rust_path"), wrapper_max_iters=1)
    shipped = get_shipped_name("lib")
    lib_rs = ws / shipped / "src" / "lib.rs"
    # `twice` is static in C, so -ffi holds its wrapper as private scaffolding that only the
    # callback `default_op` hands out keeps alive
    translated = lib_rs.read_text()
    assert re.search(r'^unsafe extern "C" fn twice\(', translated, re.M), translated
    assert 'export_name = "twice"' not in translated
    # And scaffolding nothing uses, as `strip_visibility(strip_exports(...))` renders it
    lib_rs.write_text(f"{translated}\n{DEAD_FN}\n")
    exports = _exports(ws, shipped, CRATE)
    assert {"add", "sub", "default_op"} <= exports
    before = _files(ws)

    def make_minify(*, force: bool = False) -> None:
        command = ["make"]
        if force:
            command.append("-B")
        command.extend(
            [
                "-C",
                str(ws.parent),
                "-f",
                str(REPO_ROOT / "IDEAS.mk"),
                f"TRANSLATION_DIR={TRANSLATION_DIR}",
                f"TRANSLATION_TEST={TRANSLATION_TEST}",
                "VCS=none",
                "minify",
            ]
        )
        proc = subprocess.run(
            command,
            env={**os.environ, "IDEAS_RUNNER": "native", "UV_PROJECT": str(REPO_ROOT)},
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

    make_minify()

    assert lib_rs.read_text().rstrip() == translated.rstrip()
    after = _files(ws)
    changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
    # Only the -ffi sources and their stats changed; we don't care about logs
    assert {p for p in changed if not _is_log(p)} == {
        Path(shipped) / "src" / "lib.rs",
        Path(shipped) / "minify.json",
    }
    lib = Path(shipped) / "src" / "lib.rs"
    lines = len(before[lib].splitlines()), len(after[lib].splitlines())
    assert json.loads((ws / shipped / "minify.json").read_text()) == {
        "lines_before": lines[0],
        "lines_after": lines[1],
        "lines_deleted": lines[0] - lines[1],
    }
    assert _exports(ws, shipped, CRATE) == exports
    # The callback `default_op` hands out still resolves to the Rust `twice`
    script = (
        "import ctypes, sys\n"
        "lib = ctypes.CDLL(sys.argv[1])\n"
        "lib.default_op.restype = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)\n"
        "print(lib.default_op()(21))\n"
    )
    cdylib = ws / "target" / "debug" / f"lib{CRATE}.so"
    proc = subprocess.run(
        [sys.executable, "-c", script, str(cdylib)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "42"

    # Despite the log being there, make runs it again, which finds nothing left to remove
    def sources() -> dict[Path, bytes]:
        return {
            p: c for p, c in _files(ws).items() if not _is_log(p) and p.name != "minify.json"
        }

    minified = sources()
    stats_before = (ws / shipped / "minify.json").read_bytes()
    make_minify()
    assert (ws / shipped / "minify.json").read_bytes() == stats_before
    make_minify(force=True)
    assert sources() == minified
    # The stats describe the last run, which deleted nothing
    assert json.loads((ws / shipped / "minify.json").read_text()) == {
        "lines_before": lines[1],
        "lines_after": lines[1],
        "lines_deleted": 0,
    }
