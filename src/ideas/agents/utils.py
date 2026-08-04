#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import os
import textwrap
import stat
from pathlib import Path

from ideas.tools import run_subprocess


def strip_line_directives(path: Path) -> None:
    success, output, error, _ = run_subprocess(
        ["clang", "--preprocess", "--no-line-commands", str(path)]
    )
    if not success:
        raise RuntimeError(f"Failed to strip line directives from {path}!{output + error}")


def write_profile_list(path: Path, functions: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"fun:{name}\n" for name in sorted(set(functions))))
    return path


def write_instrumentation_script(
    path: Path, features: list[str], profile_list: Path | None = None
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Resolve the list relative to the script so the crate stays relocatable.
    profile_list_export = (
        "# Restrict C instrumentation to the program's own functions.\n        "
        'script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)\n        '
        f'export CFLAGS="${{CFLAGS:-}} -fprofile-list=$script_dir/'
        f'{profile_list.resolve().relative_to(path.parent.resolve())}"\n        '
        if profile_list is not None
        else ""
    )
    contents = textwrap.dedent(
        f"""
        #!/usr/bin/env bash
        ## This script is generated automatically and should not be modified! ##
        set -euo pipefail

        export ASAN_OPTIONS="detect_leaks=0"
        export UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1"
        export LSAN_OPTIONS=

        # Discard raw profiles from previous runs so coverage reflects only this run's tests.
        cargo llvm-cov clean --profraw-only

        test_name="${{1:-}}"
        if [ "$test_name" != "collect" ] && [ "$test_name" != "io" ]; then
            echo "Usage: $0 <collect|io>" >&2
            exit 2
        fi

        log_dir="coverage_logs"
        mkdir -p "$log_dir"
        sanitizer_dir="sanitizer_logs"
        features="{" ".join(features)}"
        sanitizer_status=0
        if [ "$test_name" = "collect" ]; then
            rm -f json/*.json
        fi

        # Run each sanitizer: print basic diagnostics to stdout and, only on
        # failure, save a detailed per-sanitizer log (including stderr diagnostics).
        for feature in $features; do
            err_file=$(mktemp)
            if ! output=$(NEXTEST_EXPERIMENTAL_LIBTEST_JSON=1 cargo nextest run --features "$feature" --test "$test_name" --no-fail-fast --test-threads 1 --message-format libtest-json 2>"$err_file"); then
                sanitizer_status=1
                mkdir -p "$sanitizer_dir"
                log_file="$sanitizer_dir/$feature.log"

                # Basic diagnostics -> stdout
                echo "=== $feature ==="
                printf '%s\\n' "$output" | jq -r 'select(.type == "suite" and .event != "started")
                        | "\\(.passed + .failed) tests run: \\(.passed) passed, \\(.failed) failed"'
                printf '%s\\n' "$output" | jq -r 'select(.type == "test" and .event == "failed")
                        | "FAIL \\(.name)"'
                echo "  detailed log: $log_file"

                # Detailed diagnostics -> per-sanitizer log file
                {{
                    echo "=== $feature ==="
                    printf '%s\\n' "$output" | jq -r 'select(.type == "test" and .event == "failed")
                            | "FAIL \\(.name)\\n\\(.stdout // "")"'
                    echo "--- stderr (sanitizer diagnostics) ---"
                    cat "$err_file"
                }} > "$log_file"
            fi
            rm -f "$err_file"
        done

        if [ "$sanitizer_status" -ne 0 ]; then
            exit 1
        fi

        echo "All sanitizer checks passed"
        {profile_list_export}
        cargo llvm-cov nextest --features cc_coverage --include-ffi --no-report --test "$test_name" --no-fail-fast --test-threads 1 > /dev/null 2>&1
        cargo llvm-cov report --include-ffi --text > "$log_dir/coverage_report.log"

        # Coverage summary table -> stdout and log file.
        echo "Coverage summary:"
        cargo llvm-cov report --include-ffi --summary-only | tee "$log_dir/coverage_summary.log"

        # Emit only uncovered branches (a branch whose True or False count is zero).
        echo "Uncovered branches:"
        {{ grep -E 'Branch \\(.*(True: 0,|False: 0\\])' "$log_dir/coverage_report.log" || echo "  none"; }} | tee "$log_dir/uncovered_branches.log"
        """
    ).strip()
    path.unlink(missing_ok=True)
    path.write_text(contents + "\n")
    # Read + execute for the current user
    path.chmod(stat.S_IRUSR | stat.S_IXUSR)
    return path


def write_collect_script(path: Path, template: str, lib_name: str | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if template == "bin":
        collect_stub = textwrap.dedent(
            """
            #![allow(unused_imports, dead_code)]
            use std::os::unix::process::ExitStatusExt;
            use assert_cmd::Command;
            use serde::Serialize;
            use serde_json;

            /// A single invocation of the binary and everything it produced.
            #[derive(Serialize)]
            struct Call {
                args: Vec<String>,
                stdin: Option<String>,
                stdout: String,
                stderr: String,
                exit_code: i32,
            }

            /// One collection test and the calls it made, in order.
            #[derive(Serialize)]
            struct Case<'a> {
                name: &'a str,
                calls: &'a [Call],
            }

            fn run(args: &[&str], stdin: Option<&str>) -> Call {
                let bin_path = assert_cmd::cargo::cargo_bin(assert_cmd::pkg_name!());
                let bin_path_str = bin_path.to_str().unwrap();

                let mut cmd = Command::new("stdbuf");
                cmd.args(&["-e0", "-o0", bin_path_str]);
                if !args.is_empty() {
                    cmd.args(args);
                }
                if let Some(input) = stdin {
                    cmd.write_stdin(input);
                }
                let output = cmd.output().expect("failed to execute process");
                if matches!(output.status.signal(), Some(libc::SIGILL) | Some(libc::SIGABRT)) {
                    panic!("Sanitizer detected an error during collection!");
                }
                Call {
                    args: args.iter().map(|s| s.to_string()).collect(),
                    stdin: stdin.map(|s| s.to_string()),
                    stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
                    stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
                    exit_code: output.status.code().unwrap_or(-1),
                }
            }

            /// Serialize one collection test's ordered calls to `json/<name>.json`.
            fn save_case(name: &str, calls: &[Call]) {
                std::fs::create_dir_all("json").unwrap();
                let case = Case { name, calls };
                std::fs::write(
                    format!("json/{name}.json"),
                    serde_json::to_string_pretty(&case).unwrap(),
                )
                .unwrap();
            }

            // ==== Add collection tests below this line ====
            """
        ).strip()
    else:
        if lib_name is None:
            raise ValueError("lib_name is required for the library collect script")

        collect_stub = textwrap.dedent(
            f"""
            #![allow(unused_imports, dead_code)]
            use {lib_name}::*;
            use serde::Serialize;
            use serde_json;

            /// Serialize one collection test's input/output state to `json/<name>.json`.
            fn save_case<T: Serialize>(name: &str, case: &T) {{
                std::fs::create_dir_all("json").unwrap();
                let path = format!("json/{{name}}.json");
                std::fs::write(path, serde_json::to_string_pretty(case).unwrap()).unwrap();
            }}

            // ==== Add collection tests below this line ====
            """
        ).strip()
    path.write_text(collect_stub + "\n")


def write_assert_script(path: Path, template: str, lib_name: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if template == "bin":
        assert_stub = textwrap.dedent(
            """
            #![allow(unused_imports, dead_code)]
            use std::os::unix::process::ExitStatusExt;
            use assert_cmd::Command;
            use predicates::prelude::*;

            /// What a single invocation of the binary produced.
            struct Call {
                stdout: String,
                stderr: String,
                exit_code: i32,
            }

            fn run(args: &[&str], stdin: Option<&str>) -> Call {
                let bin_path = assert_cmd::cargo::cargo_bin(assert_cmd::pkg_name!());
                let bin_path_str = bin_path.to_str().unwrap();

                let mut cmd = Command::new("stdbuf");
                cmd.args(&["-e0", "-o0", bin_path_str]);
                if !args.is_empty() {
                    cmd.args(args);
                }
                if let Some(input) = stdin {
                    cmd.write_stdin(input);
                }
                let output = cmd.output().expect("failed to execute process");
                if matches!(output.status.signal(), Some(libc::SIGILL) | Some(libc::SIGABRT)) {
                    panic!("Sanitizer detected an error while running the binary!");
                }
                Call {
                    stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
                    stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
                    exit_code: output.status.code().unwrap_or(-1),
                }
            }

            // ==== Add assertion tests below this line ====
            """
        ).strip()
    else:
        if lib_name is None:
            raise ValueError("lib_name is required for the library assert script")

        assert_stub = textwrap.dedent(
            f"""
            #![allow(unused_imports, dead_code)]
            use {lib_name}::*;

            // ==== Add assertion tests below this line ====
            """
        ).strip()
    path.write_text(assert_stub + "\n")
    return path


RESTRICT_COVERAGE = os.environ.get("RESTRICT_COVERAGE", "0") not in ("0", "", "false", "False")
