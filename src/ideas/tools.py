#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from json import loads as js_loads
import re
import logging
import subprocess
from typing import Any
from tempfile import TemporaryDirectory
from pathlib import Path


TestCase = dict[str, None | str | int | float | list[int] | list[str] | list[float]]

logger = logging.getLogger("ideas.tools")

DEFAULT_TEST_TIMEOUT = 10.0  # seconds


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


def extract_rust(response: str) -> str:
    block_pattern = r"```([^\n]*)\n(.*?)\n?```"
    open_ended_pattern = r"```([^\n]*)\n(.*?)$"

    snippets, languages = [], []
    last_closed_end_idx = 0  # Keeps track of where the last codeblock ends

    # Match all closed code blocks
    for match in re.finditer(block_pattern, response, re.DOTALL):
        language = match.group(1).lower().strip() if match.group(1) else None
        content = match.group(2).strip()

        last_closed_end_idx = match.end()
        snippets.append(content)
        languages.append(language)

    # Look for an open-ended code block after the last closed block
    if last_match := re.search(open_ended_pattern, response[last_closed_end_idx:], re.DOTALL):
        language = last_match.group(1).lower().strip() if last_match.group(1) else None
        content = last_match.group(2).strip()

        snippets.append(content)
        languages.append(language)

    # Filter only Rust and generic (non-language-specific) non-empty code blocks
    snippets = [
        content.strip()
        for content, language in zip(snippets, languages)
        if len(content) > 0 and language in ["rust", None]
    ]

    # Accept the entire response as-is if no pattern matches
    if len(snippets) == 0:
        logger.warning(
            "No valid code blocks found in translation! Using the entire response as-is."
        )
        return response

    # Warn if multiple snippets were output by the model
    if len(snippets) > 1:
        logger.warning("Multiple valid code blocks found in translation! Using the last one.")

    return snippets[-1]


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
