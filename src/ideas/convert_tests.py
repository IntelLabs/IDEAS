#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

"""
Use JSON test vectors like:

```json
{
    "argv": ["--flag", "value"],                         // optional; appended after the driver path
    "stdin": "input string\n",                           // optional
    "rc": 0,                                             // optional; default 0
    "stdout": { "pattern": "ok\n", "is_regex": false },  // exact by default
    "stderr": { "pattern": "", "is_regex": false },      // exact by default
    "has_ub": "overflow"                                 // optional; if present, test is skipped
}
```

to generate tests for binary targets:

```rust
use assert_cmd::Command;
use ntest::timeout;
use predicates::prelude::*;

#[test]
#[timeout(some_timeout)]
fn test1() {
    Command::cargo_bin(assert_cmd::crate_name!()).unwrap()
        .args(&["--flag", "value"])
        .write_stdin("input string\n")
        .assert()
        .stdout("ok\n") # if not regex
        .stdout(predicates::str::is_match("ok\n").unwrap()) # if regex
        .stderr("") # if not regex
        .stderr(predicates::str::is_match("").unwrap()) # if regex
        .code(rc);
}
```

TODO: Use a template to generate tests for library targets.
"""

import json
import argparse
from pathlib import Path


def to_rust_str(string):
    return '"' + repr(string)[1:-1] + '"'


def is_bin_test(test_case: Path):
    test_case_json = json.loads(test_case.read_text())
    return "lib_state_in" not in test_case_json and "lib_state_out" not in test_case_json


def convert_tests_for_exec(test_cases: list[Path], timeout: int = 60000):
    test_cases = list(filter(is_bin_test, test_cases))
    if len(test_cases) == 0:
        return

    print("use assert_cmd::Command;")
    print("use ntest::timeout;")
    print("use predicates::prelude::*;")
    print("")

    for test_case in test_cases:
        test_case_json = json.loads(test_case.read_text())

        # Skip tests that exercise undefined behavior
        if "has_ub" in test_case_json:
            print(
                f"// Skipping {test_case} because it exercises undefined C behavior of type {test_case_json['has_ub']}"
            )
            continue

        # Return code
        rc = test_case_json.get("rc", 0)
        if not isinstance(rc, int):
            raise ValueError(f"rc must be an integer, got {type(rc)}")

        # argv
        args = test_case_json.get("argv", []) or []
        if not isinstance(args, list):
            raise ValueError(f"argv must be a list, got {type(args)}")
        args = [str(arg) for arg in args]

        # stdin
        stdin = test_case_json.get("stdin", None)
        if (stdin is not None) and not isinstance(stdin, str):
            raise ValueError(f"stdin must be a string or None, got {type(stdin)}")

        # Parse expected stdout
        stdout = test_case_json.get("stdout", {"pattern": "", "is_regex": False})
        stdout_pattern = stdout["pattern"]
        if not isinstance(stdout_pattern, str):
            raise ValueError(f"stdout.pattern must be a string, got {type(stdout_pattern)}")
        is_stdout_regex = stdout.get("is_regex", False)
        if not isinstance(is_stdout_regex, bool):
            raise ValueError(f"stdout.is_regex must be a boolean, got {type(is_stdout_regex)}")

        # Parse expected stderr
        stderr = test_case_json.get("stderr", {"pattern": "", "is_regex": False})
        stderr_pattern = stderr["pattern"]
        if not isinstance(stderr_pattern, str):
            raise ValueError(f"stderr.pattern must be a string, got {type(stderr_pattern)}")
        is_stderr_regex = stderr.get("is_regex", False)
        if not isinstance(is_stderr_regex, bool):
            raise ValueError(f"stderr.is_regex must be a boolean, got {type(is_stderr_regex)}")

        print("#[test]")
        print(f"#[timeout({timeout})]")
        print(f"fn test_case_{test_case.stem}() {{")
        print("    Command::cargo_bin(assert_cmd::crate_name!()).unwrap()")
        if len(args) > 0:
            print(f"        .args(&[{', '.join([to_rust_str(arg) for arg in args])}])")
        if stdin is not None:
            print(f"        .write_stdin({to_rust_str(stdin)})")
        print("        .assert()")
        print(
            f"        .stdout({to_rust_str(stdout_pattern)})"
            if not is_stdout_regex
            else f"        .stdout(predicates::str::is_match({to_rust_str(stdout_pattern)}).unwrap())"
        )
        print(
            f"        .stderr({to_rust_str(stderr_pattern)})"
            if not is_stderr_regex
            else f"        .stderr(predicates::str::is_match({to_rust_str(stderr_pattern)}).unwrap())"
        )
        print(f"        .code({rc});")
        print("}")
        print("")


def is_lib_test(test_case: Path):
    test_case_json = json.loads(test_case.read_text())
    return "lib_state_in" in test_case_json and "lib_state_out" in test_case_json


def convert_tests_for_lib(
    test_cases: list[Path], template_path: Path | None, timeout: int = 60000
):
    raise ValueError("Library test conversion not implemented yet!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert JSON test vectors to cargo tests")
    parser.add_argument(
        "test_vectors", type=Path, nargs="+", help="Path(s) to JSON test vector(s)"
    )
    parser.add_argument(
        "--template", type=Path, help="Path to Rust test template", required=False
    )
    parser.add_argument(
        "--timeout", type=int, help="Timeout for each test in milliseconds", default=60000
    )
    args = parser.parse_args()

    test_vectors = [Path(path) for path in args.test_vectors]
    convert_tests_for_exec(test_vectors, args.timeout)
    convert_tests_for_lib(test_vectors, args.template, args.timeout)
