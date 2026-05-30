#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import os
import re
import json
import shutil

import tomlkit
import logging
import subprocess
from functools import cached_property
from typing import Any, Literal
from tempfile import TemporaryDirectory
from pathlib import Path


TestCase = dict[str, None | str | int | float | list[int] | list[str] | list[float]]

logger = logging.getLogger("ideas.tools")

DEFAULT_TEST_TIMEOUT = 10.0  # seconds


class VCS:
    def __init__(
        self,
        repo_dir: Path,
        vcs: Literal["none", "git"] = "git",
    ):
        self.repo_dir = repo_dir
        self.vcs = vcs

    def init(self, force_init: bool = False) -> bool:
        if self.vcs == "none":
            return True

        ok, out = False, ""
        if not force_init:
            ok, out = self("rev-parse --abbrev-ref HEAD")
        if not ok:
            ok, out = self("init --initial-branch=main")
        if not ok:
            raise ValueError(f"Failed to initialize git in {self.repo_dir}!\n{out}")
        return ok

    def add(self, *paths: Path) -> bool:
        if self.vcs == "none":
            return True

        ok = True
        for path in paths:
            ok, out = self(f"add {path}")
            if not ok:
                raise ValueError(f"Failed to add {path}!\n{out}")
        return ok

    def rm(self, *paths: Path, force: bool = False) -> bool:
        if self.vcs == "none":
            ret = True
            for path in paths:
                target = path if path.is_absolute() else self.repo_dir / path
                try:
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                except Exception:
                    if not force:
                        ret = False
            return ret

        ret = True
        for path in paths:
            cmd = ["rm"]
            if path.is_dir():
                cmd.append("-r")
            if force:
                cmd.append("-f")

            ok, out = self(" ".join([*cmd, str(path)]))
            if not force:
                ret = ret and ok
        return ret

    def commit(self, message: str = "") -> bool:
        if self.vcs == "none":
            return True

        message = message.replace("\x00", "")
        ok, out = self("commit --allow-empty -F -", input=message)
        if not ok:
            raise ValueError(f"Failed to commit changes to git!\n{out}")
        return ok

    def __call__(self, cmd, *args, **kwargs) -> tuple[bool, str]:
        if self.vcs == "none":
            return True, ""

        success, output, error, _ = run_subprocess(
            [self.vcs, "-C", str(self.repo_dir), *cmd.split(" "), *args], **kwargs
        )
        return success, output + error


class Workspace:
    def __init__(
        self,
        cargo_toml: Path,
        vcs: Literal["none", "git"] = "none",
    ):
        self.cargo_toml = cargo_toml

        workspace_dir = self.cargo_toml.parent
        self.vcs = VCS(repo_dir=workspace_dir, vcs=vcs)

        if not self.cargo_toml.exists():
            # Create a new workspace
            os.makedirs(workspace_dir, exist_ok=True)
            contents = {"workspace": {"resolver": "3"}}
            self.cargo_toml.write_text(tomlkit.dumps(contents))

        # Initialize repository if needed
        self.vcs.init(force_init=True)


class Crate:
    def __init__(
        self,
        cargo_toml: Path,
        vcs: Literal["none", "git"] = "none",
        type: Literal["bin", "lib"] | None = None,
    ):
        self.cargo_toml = cargo_toml

        crate_dir = self.cargo_toml.parent
        self.vcs = VCS(repo_dir=crate_dir, vcs=vcs)

        if not self.cargo_toml.exists():
            # Create a new crate with specified type, but without VCS
            if not type:
                raise ValueError(
                    f"Crate at {crate_dir} does not exist; type must be specified!"
                )
            os.makedirs(crate_dir, exist_ok=True)
            success, output, error, _ = run_subprocess(
                [
                    "cargo",
                    "init",
                    "--quiet",
                    f"--{type}",
                    "--vcs=none",
                    str(crate_dir),
                ]
            )
            if not success:
                raise RuntimeError(
                    f"Failed to create new crate at {crate_dir} with error:\n\n{output + error}"
                )

        # Initialize repository if needed
        self.vcs.init()

    @cached_property
    def metadata(self) -> dict[str, Any]:
        success, out, error, _ = run_subprocess(
            ["cargo", "metadata", "--manifest-path", str(self.cargo_toml)],
        )
        if not success:
            raise ValueError(
                f"Failed to get cargo metadata from {self.cargo_toml}!\n{out + error}"
            )
        metadata = json.loads(out)
        return metadata

    def invalidate_metadata(self) -> None:
        if "metadata" in self.__dict__:
            del self.metadata

    @property
    def root_package(self) -> dict[str, Any]:
        root = self.metadata["resolve"]["root"]
        if root is None:
            if len(self.metadata["workspace_members"]) != 1:
                raise ValueError("No root package specified!")
            root = self.metadata["workspace_members"][0]

        root_package = next(filter(lambda p: p["id"] == root, self.metadata["packages"]))
        return root_package

    @property
    def workspace_root(self) -> Path:
        workspace_root = self.metadata.get("workspace_root", None)
        if workspace_root is None:
            # Standalone crates without workspace metadata fall back to crate directory.
            return self.cargo_toml.parent
        return Path(workspace_root)

    @property
    def bin_targets(self) -> list[dict[str, Any]]:
        return list(filter(lambda t: "bin" in t["kind"], self.root_package["targets"]))

    @property
    def lib_targets(self) -> list[dict[str, Any]]:
        return list(filter(lambda t: "lib" in t["kind"], self.root_package["targets"]))

    @property
    def is_bin(self) -> bool:
        if len(self.bin_targets) == 1 and len(self.lib_targets) == 0:
            is_bin = True
        elif len(self.bin_targets) == 0 and len(self.lib_targets) == 1:
            is_bin = False
        else:
            raise ValueError(
                f"Unhandled bin/lib targets configuration in Cargo.toml: {self.bin_targets=} {self.lib_targets=}"
            )
        return is_bin

    @property
    def rust_src_path(self) -> Path:
        if len(self.bin_targets) == 1 and len(self.lib_targets) == 0:
            rust_src_path = Path(self.bin_targets[0]["src_path"])
        elif len(self.bin_targets) == 0 and len(self.lib_targets) == 1:
            rust_src_path = Path(self.lib_targets[0]["src_path"])
        else:
            raise ValueError(
                f"Unhandled bin/lib targets configuration in Cargo.toml: {self.bin_targets=} {self.lib_targets=}"
            )
        return rust_src_path

    @property
    def c_src_path(self) -> Path:
        return self.rust_src_path.with_suffix(".c")

    def cargo_add(
        self, dep: str, section: str | None = None, features: list[str] | None = None
    ) -> str:
        cmd = [
            "cargo",
            "add",
            "--quiet",
            f"--manifest-path={self.cargo_toml}",
        ]
        if section:
            cmd.append(f"--{section}")
        if features:
            cmd.append(f"--features={','.join(features)}")
        cmd.append(dep)

        success, output, error, _ = run_subprocess(cmd)
        if not success:
            raise RuntimeError(
                f"Failed to add dependency {dep} to {self.cargo_toml} with error:\n\n{output + error}"
            )

        # Invalidate cached metadata
        self.invalidate_metadata()
        return output

    def cargo_feature(self, **features: list[str]) -> None:
        # Set/overwrite new features
        cargo_toml = tomlkit.loads(self.cargo_toml.read_text())
        cargo_features = cargo_toml.get("features", {})
        for name, deps in features.items():
            cargo_features[name] = deps
        cargo_toml["features"] = cargo_features
        self.cargo_toml.write_text(tomlkit.dumps(cargo_toml))

        # Invalidate cached metadata
        self.invalidate_metadata()

    def cargo_clean(self) -> None:
        cmd = [
            "cargo",
            "clean",
            "--quiet",
            f"--manifest-path={self.cargo_toml}",
        ]
        success, output, error, _ = run_subprocess(cmd)
        if not success:
            raise RuntimeError(
                f"Failed to clean crate at {self.cargo_toml} with error:\n\n{output + error}"
            )

    def cargo_build(self, fix_E0601: bool = True) -> tuple[bool, str]:
        cmd = [
            "cargo",
            "build",
            "--quiet",
            "--color=never",
            f"--manifest-path={self.cargo_toml}",
        ]
        builds, output, error, _ = run_subprocess(cmd)

        # Work around E0601 error "No main function was found in a binary crate."
        if fix_E0601 and "error[E0601]" in error:
            rust_src = self.rust_src_path.read_text()
            with self.rust_src_path.open("a") as f:
                f.write('\n\nfn main() {\n    println!("Hello, world!");\n}\n')
            builds, output, error, _ = run_subprocess(cmd)
            self.rust_src_path.write_text(rust_src)

        return builds, output + error

    def cargo_test(
        self,
        name: str,
        test_harness: Literal["nextest run", "test"] = "nextest run",
        quiet: bool = True,
        fail_fast: bool = False,
        build_only: bool = False,
        skip: list[str] | None = None,
        message_format: str | None = None,
    ) -> tuple[bool, str, str, int | Literal["timeout"]]:
        cmd = [
            "cargo",
            *test_harness.split(),
            "--color=never",
            f"--manifest-path={self.cargo_toml}",
        ]
        if not fail_fast:
            cmd.append("--no-fail-fast")
        if quiet:
            if test_harness == "nextest run":
                cmd.append("--cargo-quiet")
            elif test_harness == "test":
                cmd.append("--quiet")
            else:
                raise ValueError(f"Unsupported test harness: {test_harness}")
        if name:
            cmd.extend(["--test", name])
        if build_only:
            cmd.append("--no-run")

        env = os.environ.copy()
        if message_format is not None:
            cmd.extend(["--message-format", message_format])
            if message_format == "libtest-json" and test_harness == "nextest run":
                # https://nexte.st/docs/machine-readable/libtest-json/
                env["NEXTEST_EXPERIMENTAL_LIBTEST_JSON"] = "1"
        if skip:
            if test_harness == "nextest run":
                excluded_tests = [f"test(/^{re.escape(test_name)}$/)" for test_name in skip]
                expr = " and ".join(f"not {test_expr}" for test_expr in excluded_tests)
                cmd.extend(["-E", expr])
            else:
                cmd.append("--")
                cmd.append("--exact")
                for test_name in skip:
                    cmd.extend(["--skip", test_name])

        return run_subprocess(cmd, env=env)

    def cargo_nextest_config(self, slow: int = 30, terminate_after: int = 4) -> None:
        nextest_config_path = self.workspace_root / ".config" / "nextest.toml"
        nextest_config_path.parent.mkdir(exist_ok=True)
        nextest_config = {
            "profile": {
                "default": {
                    "slow-timeout": {"period": f"{slow}s", "terminate-after": terminate_after},
                    "final-status-level": "none",
                    "fail-fast": False,
                    "failure-output": "never",
                    "test-threads": 1,
                }
            }
        }
        nextest_config_path.write_text(tomlkit.dumps(nextest_config))
        self.invalidate_metadata()

    def write(self, path: Path, data, **kwargs):
        if path.is_absolute():
            raise ValueError("path must not be absolute")
        path = self.cargo_toml.parent / path
        return path.write_text(data, **kwargs)


def nextest_json_to_libtest(stdout: str) -> str:
    """Convert nextest libtest-json output to vanilla `cargo test` text format."""
    lines = []
    summary = {}
    for raw in stdout.splitlines():
        obj = json.loads(raw)

        if obj.get("type") == "test":
            event = obj.get("event")
            if event not in {"ok", "failed", "ignored"}:
                continue

            # nextest uses "$" to join binary::suite$test_name
            name = obj["name"].rsplit("$", 1)[-1]
            status = "FAILED" if event == "failed" else event
            lines.append(f"test {name} ... {status}")

        elif obj.get("type") == "suite" and obj.get("event") != "started":
            summary = obj

    # Append summary from the suite event (or zeros if missing)
    p, f = summary.get("passed", 0), summary.get("failed", 0)
    ig, m = summary.get("ignored", 0), summary.get("measured", 0)
    fo = summary.get("filtered_out", 0)
    result = "FAILED" if f else "ok"
    lines.append(
        f"test result: {result}. {p} passed; {f} failed; "
        f"{ig} ignored; {m} measured; {fo} filtered out"
    )
    return "\n".join(lines) + "\n"


def run_subprocess(
    cmd: list[str],
    input: str | None = None,
    timeout: float | None = None,
    **kwargs,
) -> tuple[bool, str, str, int | Literal["timeout"]]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            input=input,
            timeout=timeout,
            **kwargs,
        )
        return True, result.stdout, result.stderr, result.returncode
    except subprocess.CalledProcessError as e:
        return False, e.stdout, e.stderr, e.returncode
    except subprocess.TimeoutExpired as e:
        return (
            False,
            e.stdout.decode() if e.stdout else "",
            e.stderr.decode() if e.stderr else "",
            "timeout",
        )


def check_c(
    code: str,
    *,
    flags: list[str] | None = None,
) -> tuple[bool, str]:
    cmd = ["clang-21"]

    if flags:
        cmd.extend(flags)
    else:
        cmd.append("-Wall")

    cmd.extend(["-march=native", "-x", "c"])
    cmd.append("-")
    cmd.extend(["-o", "/dev/null"])

    success, output, error, _ = run_subprocess(cmd, input=code)
    return success, output + error


def check_rust(
    code: str,
    *,
    flags: list[str] | None = None,
    structured_output: bool = False,
) -> tuple[bool, str]:
    cmd = ["rustc"]

    if flags:
        cmd.extend(flags)

    if structured_output:
        cmd.append("--error-format=json")

    with TemporaryDirectory() as dirname:
        cmd.extend(["-", "--out-dir", dirname])

    success, output, error, _ = run_subprocess(cmd, input=code)
    return success, output + error


def rustfmt(path: Path) -> None:
    cmd = ["rustfmt", str(path)]
    run_subprocess(cmd)


def run_test(
    executable: Path | str,
    test_case: TestCase,
    timeout: float | None = DEFAULT_TEST_TIMEOUT,
) -> tuple[bool, str]:
    # Turn args into list[str]
    args = test_case.get("args", []) or []
    if not isinstance(args, list):
        args = [args]
    args = [str(arg) for arg in args]

    # Turn stdin into list[str] then join on newlines
    stdin = test_case.get("in", []) or []
    if not isinstance(stdin, list):
        stdin = [stdin]
    stdin = [str(s) for s in stdin]
    stdin = "\n".join(stdin)

    # Run test and right-strip output of whitespace
    success, output, error, _ = run_subprocess([str(executable), *args], stdin, timeout=timeout)
    return success, output + error


def check_test(
    test_case: TestCase,
    stdout: str,
) -> bool:
    # Turn out into list[str] then join on newlines
    out = test_case["out"]
    if not isinstance(out, list):
        out = [out]
    out = [str(o) for o in out]
    if isinstance(out, list):
        out = "\n".join(out)

    # Make sure test returned and matches
    return out.rstrip() == stdout.rstrip()


def run_and_check_test(
    executable: Path | str,
    test_case: TestCase,
    timeout: float | None = DEFAULT_TEST_TIMEOUT,
):
    _, stdout = run_test(executable, test_case, timeout=timeout)
    return check_test(test_case, stdout)


def run_and_check_tests(
    executable: Path | str,
    test_cases: list[TestCase],
    timeout: float | None = DEFAULT_TEST_TIMEOUT,
) -> int:
    success = 0
    for test_case in test_cases:
        success += 1 if run_and_check_test(executable, test_case, timeout=timeout) else 0
    return success


def _in_env(var_name: str, default: bool = True) -> bool:
    value = os.getenv(var_name, str(default))
    return value.strip().lower() in {"1", "true", "yes", "on"}


LARGE_PROJECT = _in_env("LARGE_PROJECT", default=False)
