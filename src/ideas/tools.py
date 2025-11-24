#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import json
from json import loads as js_loads
from dataclasses import dataclass

import logging
import subprocess
from typing import Any
from tempfile import TemporaryDirectory
from pathlib import Path


TestCase = dict[str, None | str | int | float | list[int] | list[str] | list[float]]

logger = logging.getLogger("ideas.tools")

DEFAULT_TEST_TIMEOUT = 10.0  # seconds


@dataclass
class Crate:
    cargo_toml: Path
    rust_src_path: Path
    root_package: dict[str, Any]
    is_bin: bool


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
    cmd = ["clang"]

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
    cmd = ["clang"]

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


def get_info_from_cargo_toml(cargo_toml: Path) -> Crate:
    # Extract root package metadata from Cargo.toml
    out = subprocess.run(
        ["cargo", "metadata", "--manifest-path", cargo_toml], text=True, capture_output=True
    )
    if out.returncode != 0:
        raise ValueError(f"Failed to get cargo metadata from {cargo_toml}!\n{out.stderr}")
    metadata = json.loads(out.stdout)
    root = metadata["resolve"]["root"]
    if root is None:
        raise ValueError("No root package specified!")
    root_package = next(filter(lambda p: p["id"] == root, metadata["packages"]))

    # Get rust source path for bin or lib
    bin_targets = list(filter(lambda t: "bin" in t["kind"], root_package["targets"]))
    lib_targets = list(filter(lambda t: "lib" in t["kind"], root_package["targets"]))
    if len(bin_targets) == 1 and len(lib_targets) == 0:
        rust_src_path = Path(bin_targets[0]["src_path"])
        is_bin = True
    elif len(bin_targets) == 0 and len(lib_targets) == 1:
        rust_src_path = Path(lib_targets[0]["src_path"])
        is_bin = False
    else:
        raise ValueError(
            f"Unhandled bin/lib targets configuration in Cargo.toml: {bin_targets=} {lib_targets=}"
        )

    return Crate(
        cargo_toml=cargo_toml,
        rust_src_path=rust_src_path,
        root_package=root_package,
        is_bin=is_bin,
    )


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
