#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ideas import sandbox
from ideas.sandbox import LandlockPolicy, Mount, Options


def _sandbox(*args: str, **kwargs) -> subprocess.CompletedProcess[str]:
    """Runs sandbox.py as make does: as a standalone script, not part of the package."""
    return subprocess.run(
        [sys.executable, sandbox.__file__, *args], capture_output=True, text=True, **kwargs
    )


@pytest.fixture(scope="session")
def landlock() -> None:
    try:
        sandbox.Landlock()
    except SystemExit:
        pytest.skip("Landlock is unavailable")


@pytest.fixture
def ideas(tmp_path: Path) -> Path:
    """An IDEAS checkout with one example to expose (a) and one to keep hidden (b)."""
    ideas = tmp_path / "ideas"
    (ideas / "src").mkdir(parents=True)
    (ideas / "src" / "m.py").write_text("src\n")
    for example in ("a", "b"):
        (ideas / "examples" / example / "test_case").mkdir(parents=True)
        (ideas / "examples" / example / "test_case" / f"{example}.c").write_text(f"{example}\n")
    (ideas / "examples" / "a" / "out").mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret").write_text("secret\n")
    return ideas.resolve()


# Argument handling


@pytest.mark.parametrize(
    "runner, expected",
    [
        ("native", ("native", "")),
        ("landlock", ("landlock", "")),
        ("docker:img", ("docker", "img")),
    ],
)
def test_runner_is_parsed(runner: str, expected: tuple[str, str]):
    opts = sandbox.parse_args(["--runner", runner, "--", "true"])
    assert (opts.runner, opts.image) == expected


@pytest.mark.parametrize("runner", ["bogus", "docker:", ""])
def test_invalid_runner_is_rejected(runner: str):
    result = _sandbox("--runner", runner, "--", "true")
    assert result.returncode == 2
    assert "unknown runner" in result.stderr


@pytest.mark.parametrize("arg", ["1X=y", "--env=y", "--env"])
def test_invalid_env_is_rejected(arg: str):
    with pytest.raises(SystemExit):
        sandbox.parse_args(
            [arg, "NOEQUALS", "--", "true"] if arg == "--env" else [arg, "--", "x"]
        )


def test_at_prefix_hides_the_command_unless_inspecting():
    assert sandbox.parse_args(["--", "@true"]) == Options(echo=False, command=["true"])
    assert sandbox.parse_args(["--inspect", "--", "@true"]).echo


def test_mounts_resolve_inside_root(ideas: Path):
    mounts = sandbox.resolve_mounts(
        str(ideas),
        [Mount("src", False), Mount(str(ideas / "examples/a/out"), True), Mount("src", False)],
    )
    assert mounts == [
        Mount(str(ideas / "src"), False),
        Mount(str(ideas / "examples/a/out"), True),
    ]


@pytest.mark.parametrize(
    "mounts",
    [
        [Mount("../outside", False)],
        [Mount("src/../..", False)],
        [Mount("missing", False)],
        [Mount("a:b", False)],
        [Mount("src", False), Mount("src", True)],
    ],
)
def test_bad_mounts_are_rejected(ideas: Path, mounts: list[Mount]):
    with pytest.raises(SystemExit):
        sandbox.resolve_mounts(str(ideas), mounts)


def test_hides_resolve_inside_root_and_skip_missing_dirs(ideas: Path):
    hides = sandbox.resolve_hides(str(ideas), ["examples", "missing", str(ideas / "src")])
    assert hides == [str(ideas / "examples"), str(ideas / "src")]


@pytest.mark.parametrize("hide", ["../outside", "src/m.py", ""])
def test_bad_hides_are_rejected(ideas: Path, hide: str):
    with pytest.raises(SystemExit):
        sandbox.resolve_hides(str(ideas), [hide])


# Docker


def test_docker_hides_paths_and_an_unmounted_workdir(ideas: Path):
    opts = Options(runner="docker", image="img", env=["A=b"])
    workdir = str(ideas / "examples/a")
    args = sandbox.docker_args(
        opts,
        str(ideas),
        workdir,
        [f"{ideas}/examples"],
        [Mount(f"{workdir}/out", True)],
        {"no_proxy": "x"},
    )
    assert args[-1] == "img"
    assert f"type=tmpfs,dst={ideas}/examples" in args
    assert f"type=tmpfs,dst={workdir}" in args
    assert f"type=bind,src={workdir}/out,dst={workdir}/out" in args
    assert f"type=bind,src={ideas},dst={ideas},readonly" in args
    assert "no_proxy=x,host.docker.internal" in args
    assert args[args.index("A=b") - 1] == "--env"


def test_docker_shows_a_mounted_workdir(ideas: Path):
    workdir = str(ideas / "examples/a")
    args = sandbox.docker_args(
        Options(image="img"), str(ideas), workdir, [], [Mount(workdir, False)], {}
    )
    assert f"type=tmpfs,dst={workdir}" not in args
    assert f"type=bind,src={workdir},dst={workdir},readonly" in args


# Landlock policy


@pytest.mark.parametrize("abi, allowed", [(1, False), (2, False), (3, True), (6, True)])
def test_landlock_needs_abi_3_to_block_truncate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], abi: int, allowed: bool
):
    class FakeLibc:
        def syscall(self, *_args: object) -> int:
            return abi

    monkeypatch.setattr(sandbox.ctypes, "CDLL", lambda *_args, **_kwargs: FakeLibc())
    if allowed:
        assert sandbox.Landlock().access & sandbox.TRUNCATE
    else:
        with pytest.raises(SystemExit):
            sandbox.Landlock()
        assert "Linux 6.2+ is required" in capsys.readouterr().err


def test_hidden_paths_are_carved_out_of_grants(tmp_path: Path):
    for d in ("keep", "hide/inner", "hide/other"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "hide")
    policy = LandlockPolicy()
    policy.grant(str(tmp_path))
    policy.hide(str(tmp_path / "hide"))
    policy.grant(str(tmp_path / "hide/inner"), writable=True)
    assert policy.rules() == {str(tmp_path / "keep"): False, str(tmp_path / "hide/inner"): True}


def test_policy_mirrors_docker(ideas: Path, tmp_path: Path):
    home, prefix = tmp_path / "home", tmp_path / "tools"
    (prefix / "bin").mkdir(parents=True)
    workdir = str(ideas / "examples/a")
    environ = {"HOME": str(home), "PATH": f"{prefix}/bin:relative:/usr/bin"}
    hides = [f"{ideas}/examples"]
    policy, env = sandbox.landlock_policy(str(ideas), workdir, hides, [], "/scratch", environ)
    assert policy.hides == {str(home), f"{ideas}/examples", workdir}
    assert policy.grants[str(ideas)] is False
    assert policy.grants[f"{prefix}/libexec"] is False
    assert policy.grants["/scratch"] is True
    assert env["HOME"] == "/scratch/home"
    assert env["CARGO_HOME"] == f"{home}/.cargo"


def test_read_only_mount_inside_writable_mount_is_rejected(ideas: Path, tmp_path: Path):
    out = ideas / "examples/a/out"
    (out / "cache").mkdir()
    environ = {"HOME": str(tmp_path / "home")}
    nested = [Mount(str(out), True), Mount(str(out / "cache"), False)]
    with pytest.raises(SystemExit):
        sandbox.landlock_policy(str(ideas), str(out), [], nested, "/scratch", environ)
    reversed_nesting = [Mount(str(out), False), Mount(str(out / "cache"), True)]
    sandbox.landlock_policy(str(ideas), str(out), [], reversed_nesting, "/scratch", environ)


# Landlock in action

EXPOSE_A = (
    *("--readable", "examples/a/test_case"),
    *("--writable", "examples/a/out"),
)


def _run(
    ideas: Path, script: str, *mounts: str, workdir: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return _sandbox(
        *("--runner", "landlock"),
        *("--root", str(ideas)),
        *("--workdir", str(workdir or ideas / "examples" / "a")),
        *("--hide", "examples"),
        *mounts,
        "--",
        *("sh", "-c", script),
    )


def _allowed(ideas: Path, script: str, *mounts: str, **kwargs) -> bool:
    return _run(ideas, script, *mounts, **kwargs).returncode == 0


@pytest.mark.usefixtures("landlock")
@pytest.mark.parametrize(
    "script, allowed",
    [
        ("cat ../../src/m.py", True),
        ("echo x > ../../src/new", False),
        ("cat test_case/a.c", True),
        ("ls test_case", True),
        ("echo x > test_case/new", False),
        ("echo x > out/r.rs && mkdir out/sub && echo x > out/sub/f", True),
        ("cat ../b/test_case/b.c", False),
        ("ls ..", False),
        ("ls .", False),  # an unmounted workdir is hidden, as with Docker's tmpfs
        ("touch ../new", False),
        ("cat ../../../outside/secret", False),
        ('touch "$HOME/f" && touch "$TMPDIR/f"', True),
    ],
)
def test_mounts_shadow_like_docker(ideas: Path, script: str, allowed: bool):
    assert _allowed(ideas, script, *EXPOSE_A) is allowed


@pytest.mark.usefixtures("landlock")
def test_writable_workdir_is_visible(ideas: Path):
    workdir = ideas / "examples" / "a"
    assert _allowed(ideas, "ls . && touch new", "--writable", str(workdir), workdir=workdir)
    assert not _allowed(ideas, "cat ../b/test_case/b.c", "--writable", str(workdir))


@pytest.mark.usefixtures("landlock")
def test_hidden_files_cannot_be_linked_or_moved_into_a_writable_dir(ideas: Path):
    assert not _allowed(ideas, "ln ../b/test_case/b.c out/b.c", *EXPOSE_A)
    assert not _allowed(ideas, "mv ../b/test_case/b.c out/b.c", *EXPOSE_A)
    assert (ideas / "examples" / "b" / "test_case" / "b.c").exists()


@pytest.mark.usefixtures("landlock")
def test_only_hidden_paths_are_hidden(ideas: Path):
    def ls(path: str, *args: str) -> bool:
        result = _sandbox("--runner", "landlock", "--root", str(ideas), *args, "--", "ls", path)
        return result.returncode == 0

    assert ls("examples/a")
    assert not ls("examples/a", "--hide", "examples")
    assert not ls("examples/a", "--hide", "examples", "--readable", "examples/b")
    assert ls("examples/b", "--hide", "examples", "--readable", "examples/b")


@pytest.mark.usefixtures("landlock")
def test_nested_read_only_mount_fails_closed(ideas: Path):
    (ideas / "examples/a/out/cache").mkdir()
    result = _run(
        ideas,
        "touch out/cache/f",
        "--writable",
        "examples/a/out",
        "--readable",
        "examples/a/out/cache",
    )
    assert result.returncode == 2
    assert "cannot be enforced with Landlock" in result.stderr
    assert not (ideas / "examples/a/out/cache/f").exists()


@pytest.mark.usefixtures("landlock")
def test_inherited_descriptors_are_closed(ideas: Path):
    # A descriptor opened before the sandbox would otherwise bypass Landlock
    hidden = str(ideas / "examples/b/test_case/b.c")
    result = subprocess.run(
        ["sh", "-c", 'exec "$@" 7<"$0"', hidden, sys.executable, sandbox.__file__]
        + ["--runner", "landlock", "--root", str(ideas), "--hide", "examples"]
        + ["--", "sh", "-c", "cat <&7"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert result.stdout == ""


@pytest.mark.usefixtures("landlock")
@pytest.mark.parametrize(
    "lock",
    ['chmod a-w "$HOME"', 'chmod 000 "$HOME/sub"', 'chmod 000 "$HOME/.."'],
)
def test_home_is_private_and_removed(ideas: Path, lock: str):
    script = f'mkdir "$HOME/sub" && touch "$HOME/sub/f" && echo "$HOME" && {lock}'
    result = _run(ideas, script, *EXPOSE_A)
    assert result.returncode == 0, result.stderr
    home = Path(result.stdout.strip())
    assert home != Path.home()
    assert not home.parent.exists()
    assert "could not remove" not in result.stderr


def _running(pid: int, timeout: float = 5.0) -> bool:
    """Whether the process is still alive (not merely a zombie) after up to timeout seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return False
        except FileNotFoundError:
            return False
        time.sleep(0.05)
    return True


def _landlock_popen(ideas: Path, script: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, sandbox.__file__, "--runner", "landlock", "--root", str(ideas)]
        + ["--", "sh", "-c", script],
        stdout=subprocess.PIPE,
        text=True,
    )


@pytest.mark.usefixtures("landlock")
def test_leftover_descendants_are_terminated(ideas: Path):
    # Plain, detached (setsid) and SIGTERM-ignoring background processes
    script = (
        "sleep 300 & echo $!; (setsid sleep 300 & echo $!); "
        "(trap '' TERM; exec sleep 300) & echo $!"
    )
    proc = _landlock_popen(ideas, script)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0
    pids = [int(pid) for pid in out.split()]
    assert len(pids) == 3
    assert not any(_running(pid, timeout=0.1) for pid in pids)


@pytest.mark.usefixtures("landlock")
def test_sigterm_reaches_the_step_and_its_descendants(ideas: Path):
    proc = _landlock_popen(ideas, "sleep 300 & echo $!; sleep 300 & echo $!; wait")
    assert proc.stdout is not None
    pids = [int(proc.stdout.readline()) for _ in range(2)]
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 128 + signal.SIGTERM
    assert not any(_running(pid, timeout=0.1) for pid in pids)


@pytest.mark.usefixtures("landlock")
def test_step_dies_with_a_killed_sandbox(ideas: Path):
    proc = _landlock_popen(ideas, "echo $$; exec sleep 300")
    assert proc.stdout is not None
    step = int(proc.stdout.readline())
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=30)
    assert not _running(step)


@pytest.mark.usefixtures("landlock")
def test_exit_status_and_stdin_pass_through(ideas: Path):
    assert _run(ideas, "exit 3", *EXPOSE_A).returncode == 3
    assert _run(ideas, "nosuchcommand", *EXPOSE_A).returncode == 127
    result = _sandbox("--runner", "landlock", "--root", str(ideas), "--", "cat", input="hi")
    assert result.stdout == "hi"


def test_native_runner_runs_unconfined(ideas: Path, tmp_path: Path):
    secret = str(tmp_path / "outside" / "secret")
    result = _sandbox("--runner", "native", "--root", str(ideas), "--", "cat", secret)
    assert result.stdout == "secret\n"


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is unavailable")
def test_runs_as_a_uv_script():
    result = subprocess.run(
        ["uv", "run", "--script", sandbox.__file__, "A=b", "--", "@sh", "-c", 'echo "$A"'],
        capture_output=True,
        text=True,
    )
    assert (result.returncode, result.stdout) == (0, "b\n"), result.stderr
