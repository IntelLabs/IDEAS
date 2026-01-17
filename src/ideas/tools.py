#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import os
import json
from json import loads as js_loads

import logging
import subprocess
from functools import cached_property
from typing import Any, Literal
from tempfile import TemporaryDirectory
from pathlib import Path


TestCase = dict[str, None | str | int | float | list[int] | list[str] | list[float]]

logger = logging.getLogger("ideas.tools")

DEFAULT_TEST_TIMEOUT = 10.0  # seconds


class Crate:
    def __init__(
        self,
        cargo_toml: Path,
        vcs: Literal["none", "git"] = "none",
        type: Literal["bin", "lib"] | None = None,
    ):
        self.cargo_toml: Path = cargo_toml
        self.vcs = vcs

        if not self.cargo_toml.exists():
            # Create a new crate with specified type, but without VCS
            crate_dir = self.cargo_toml.parent
            if not type:
                raise ValueError(
                    f"Crate at {crate_dir} does not exist; type must be specified!"
                )
            os.makedirs(crate_dir, exist_ok=True)
            success, output = run_subprocess(
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
                    f"Failed to create new crate at {crate_dir} with error:\n\n{output}"
                )

        # Initialize repository
        if self.vcs == "git":
            ok, out = self.git("rev-parse --abbrev-ref HEAD")
            if not ok:
                ok, out = self.git("init --initial-branch=main")
            if not ok:
                raise ValueError(
                    f"Failed to initialize git in {self.cargo_toml.parent}!\n{out}"
                )

    @cached_property
    def metadata(self) -> dict[str, Any]:
        success, out = run_subprocess(
            ["cargo", "metadata", "--manifest-path", str(self.cargo_toml)],
        )
        if not success:
            raise ValueError(f"Failed to get cargo metadata from {self.cargo_toml}!\n{out}")
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

    def cargo_add(self, dep: str, section: str | None = None) -> str:
        cmd = [
            "cargo",
            "add",
            "--quiet",
            f"--manifest-path={self.cargo_toml}",
        ]
        if section:
            cmd.append(f"--{section}")
        cmd.append(dep)

        success, output = run_subprocess(cmd)
        if not success:
            raise RuntimeError(
                f"Failed to add dependency {dep} to {self.cargo_toml} with error:\n\n{output}"
            )

        # Invalidate cached metadata
        self.invalidate_metadata()
        return output

    def cargo_build(self, allow_unsafe: bool = False) -> tuple[bool, str]:
        env = os.environ.copy()
        # Disallow unsafe by default; allow when explicitly requested
        if not allow_unsafe:
            env["RUSTFLAGS"] = (env.get("RUSTFLAGS", "") + " -D unsafe-code").strip()
        return run_subprocess(
            [
                "cargo",
                "build",
                "--quiet",
                "--color=never",
                f"--manifest-path={self.cargo_toml}",
            ],
            env=env,
        )

    def add(self, *paths: Path) -> bool:
        if self.vcs != "git":
            return True

        ok = True
        for path in paths:
            ok, out = self.git(f"add {path}")
            if not ok:
                raise ValueError(f"Failed to add {path}!\n{out}")
        return ok

    def commit(self, message: str = "") -> bool:
        if self.vcs != "git":
            return True

        ok, out = self.git("commit --allow-empty -F -", input=message)
        if not ok:
            raise ValueError(f"Failed to commit changes to git!\n{out}")
        return ok

    def git(self, cmd, *args, **kwargs) -> tuple[bool, str]:
        if self.vcs != "git":
            return True, ""

        repo_dir = self.cargo_toml.parent
        return run_subprocess(["git", "-C", str(repo_dir), *cmd.split(" "), *args], **kwargs)

    def write(self, path: Path, data, **kwargs):
        if path.is_absolute():
            raise ValueError("path must not be absolute")
        path = self.cargo_toml.parent / path
        return path.write_text(data, **kwargs)


def run_subprocess(
    cmd: list[str],
    input: str | None = None,
    timeout: float | None = None,
    **kwargs,
) -> tuple[bool, str]:
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
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stderr


def compile_c(
    source_file: str, output_file: str, flags: list[str] | None = None
) -> tuple[bool, str]:
    cmd = ["clang-21"]

    if flags:
        cmd.extend(flags)
    else:
        cmd.append("-Wall")

    cmd.append(source_file)
    cmd.extend(["-o", output_file])

    return run_subprocess(cmd)


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

    cmd.extend(["-x", "c"])
    cmd.append("-")
    cmd.extend(["-o", "/dev/null"])

    return run_subprocess(cmd, input=code)


def compile_rust(
    code: str,
    output_file: Path,
    *,
    flags: list[str] | None = None,
    structured_output: bool = False,
) -> tuple[bool, str]:
    cmd = ["rustc"]

    if flags:
        cmd.extend(flags)

    if structured_output:
        cmd.append("--error-format=json")

    cmd.append("-")
    cmd.extend(["-o", str(output_file)])

    return run_subprocess(cmd, input=code)


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

    return run_subprocess(cmd, input=code)


def run_clippy(
    source_file: str, flags: list[list[str]] | None = None, structured_output: bool = False
) -> list[tuple[bool, str]]:
    base_cmd = ["clippy-driver"]

    if structured_output:
        base_cmd.append("--error-format=json")

    if not flags:
        flags = [
            ["-D", "correctness"],
            ["-W", "suspicious"],
            ["-W", "complexity"],
            ["-W", "perf"],
            ["-W", "style"],
        ]

    res = []
    for opt in flags:
        cmd = base_cmd.copy()
        cmd.extend(opt)
        cmd.append(source_file)
        res.append(run_subprocess(cmd))

    return res


def tool_output_to_js_dict(out: str | list[str]) -> list[dict[str, Any]]:
    if isinstance(out, str):
        out = [out]

    def map_single_str(s: str) -> list[dict[str, Any]]:
        js_list = []
        # rustc outputs multiple lines, each representing a json object
        for line in s.split("\n"):
            stripped = line.strip()
            if stripped:
                js_list.append(js_loads(stripped))

        return js_list

    # clippy tool call is several individual calls; we can process them together as a list
    js_list = []
    for s in out:
        js_list.extend(map_single_str(s))
    return js_list


def structured_to_rendered(js_dict: list[dict[str, Any]]) -> str:
    rendered = ""
    for single_msg in js_dict:
        if r := single_msg["rendered"]:
            rendered += r
    return rendered


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
    return run_subprocess([str(executable), *args], stdin, timeout=timeout)


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


def clang_rename_(source: Path, renames: dict[str, str], compile_commands: Path | None = None):
    for name, new_name in renames.items():
        logger.info(f"{source}: renaming `{name}` to `{new_name}`")
        cmd = ["clang-refactor-21", "local-rename"]
        if compile_commands is not None:
            cmd.append(f"-p={str(compile_commands.absolute())}")
        cmd.append(f"--old-qualified-name={name}")
        cmd.append(f"--new-qualified-name={new_name}")
        cmd.append("-i")
        cmd.append(str(source))
        success, output = run_subprocess(cmd)
        if not success:
            raise ValueError(f"`{' '.join(cmd)}` failed!\n{output}")
