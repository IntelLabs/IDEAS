#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ideas import minify as module
from ideas.minify import MinifyConfig, _main, _record, minify
from ideas.tools import VCS

ORIGINAL = 'pub extern "C" fn add() {}\n\nfn dead() {}\n'
MINIFIED = 'pub extern "C" fn add() {}\n\n'


def _run(**outcomes: str) -> tuple[bool, str, str, int]:
    lines = [
        json.dumps({"type": "test", "event": e, "name": f"io${t}"}) for t, e in outcomes.items()
    ]
    return "failed" not in outcomes.values(), "\n".join(lines), "", 0


def _crate(tmp_path: Path, *runs: tuple[bool, str, str, int] | BaseException) -> MagicMock:
    lib = tmp_path / "src" / "lib.rs"
    lib.parent.mkdir(parents=True)
    lib.write_text(ORIGINAL)

    def cargo_minify() -> tuple[bool, str]:
        lib.write_text(MINIFIED)
        return True, ""

    crate = MagicMock()
    crate.name = "libx-ffi"
    crate.cargo_toml = tmp_path / "Cargo.toml"
    crate.cargo_test.side_effect = runs
    crate.cargo_minify.side_effect = cargo_minify
    return crate


def test_minify_keeps_a_minify_with_the_same_outcomes(tmp_path: Path) -> None:
    crate = _crate(tmp_path, _run(a="ok", b="failed"), _run(a="ok", b="failed"))

    changed, _ = minify(crate, "io")

    assert changed == [tmp_path / "src" / "lib.rs"]
    assert (tmp_path / "src" / "lib.rs").read_text() == MINIFIED


@pytest.mark.parametrize(
    "after",
    [
        _run(a="failed", b="failed"),  # regressed
        _run(a="ok", b="ok"),  # newly passes, so the code changed too
        _run(a="ok"),  # vanished
        _run(),  # nothing builds
    ],
)
def test_minify_undoes_a_minify_that_changes_any_outcome(
    tmp_path: Path, after: tuple[bool, str, str, int]
) -> None:
    crate = _crate(tmp_path, _run(a="ok", b="failed"), after)

    with pytest.raises(RuntimeError):
        minify(crate, "io")

    assert (tmp_path / "src" / "lib.rs").read_text() == ORIGINAL
    assert crate.cargo_test.call_count == 2


def test_minify_undoes_a_failed_cargo_minify(tmp_path: Path) -> None:
    crate = _crate(tmp_path, _run(a="ok"))
    lib = tmp_path / "src" / "lib.rs"

    def partial() -> tuple[bool, str]:
        lib.write_text("partially written")
        return False, "boom"

    crate.cargo_minify.side_effect = partial

    with pytest.raises(RuntimeError, match="boom"):
        minify(crate, "io")

    assert lib.read_text() == ORIGINAL


def test_minify_undoes_a_minify_it_could_not_verify(tmp_path: Path) -> None:
    crate = _crate(tmp_path, _run(a="ok"), KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        minify(crate, "io")

    assert (tmp_path / "src" / "lib.rs").read_text() == ORIGINAL


def test_minify_skips_without_a_passing_test(tmp_path: Path) -> None:
    crate = _crate(tmp_path, _run(a="failed"))

    with pytest.raises(RuntimeError):
        minify(crate, "io")

    crate.cargo_minify.assert_not_called()


def test_record_overwrites_with_the_latest_minify(tmp_path: Path) -> None:
    stats = tmp_path / "minify.json"

    _record(stats, 100, 90)
    _record(stats, 90, 85)

    assert json.loads(stats.read_text()) == {
        "lines_before": 90,
        "lines_after": 85,
        "lines_deleted": 5,
    }


def test_main_commits_the_latest_stats_and_reruns_leave_the_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True
        ).stdout

    lib = tmp_path / "src" / "lib.rs"
    lib.parent.mkdir(parents=True)
    lib.write_text(ORIGINAL)
    git("init", "-q", "--initial-branch=main")
    git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")

    crate = MagicMock()
    crate.cargo_toml = tmp_path / "Cargo.toml"
    crate.vcs = VCS(tmp_path, vcs="git")
    hydra = MagicMock()
    hydra.output_subdir = None
    monkeypatch.setattr(module.HydraConfig, "get", lambda: hydra)
    monkeypatch.setattr(module, "Crate", lambda *_, **__: crate)

    def minified(*_: object) -> tuple[list[Path], str]:
        lib.write_text(MINIFIED)
        return [lib], "Minified"

    cfg = MinifyConfig(cargo_toml=crate.cargo_toml, tests="io", vcs="git")
    for outcome in (minified, lambda *_: ([], "Nothing"), lambda *_: ([], "Nothing")):
        monkeypatch.setattr(module, "minify", outcome)
        _main(cfg)

    assert git("log", "--format=%s").split() == ["Nothing", "Minified", "init"]
    assert git("status", "--porcelain") == ""
    assert json.loads((tmp_path / "minify.json").read_text())["lines_deleted"] == 0
