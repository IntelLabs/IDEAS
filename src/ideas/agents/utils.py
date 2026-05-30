#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import textwrap
import tomlkit
from pathlib import Path

from ideas.tools import Crate
from ideas.convert_tests import rustfmt


NEXTEST_DUMMY_TEST = textwrap.dedent(
    """
    #[test]
    fn dummy_ideas_placeholder() {
        assert_eq!(1 + 1, 2);
    }
    """
).strip()


def nextest_config(crate: Crate):
    nextest_config_path = crate.cargo_toml.parent / ".config" / "nextest.toml"
    nextest_config_path.parent.mkdir(parents=True, exist_ok=True)
    nextest_config_contents = {
        "profile": {
            "default": {
                "fail-fast": False,
                "slow-timeout": {"period": "30s", "terminate-after": 2},
            }
        }
    }
    nextest_config_path.write_text(tomlkit.dumps(nextest_config_contents))


def write_coverage_script(crate: Crate) -> Path:
    coverage_script_path = crate.cargo_toml.parent / "measure_coverage.sh"
    coverage_script_contents = textwrap.dedent(
        """
        cargo llvm-cov nextest --include-ffi --no-report --test test_collect --no-fail-fast 2>/dev/null
        cargo llvm-cov report --include-ffi
        cargo llvm-cov report --include-ffi --text
        """
    ).strip()
    coverage_script_path.write_text(coverage_script_contents)
    return coverage_script_path


def write_collect_script(crate: Crate) -> Path:
    collect_path = crate.cargo_toml.parent / "tests" / "test_collect.rs"
    collect_path.parent.mkdir(parents=True, exist_ok=True)

    if crate.is_bin:
        collect_stub = textwrap.dedent(
            """
            use std::os::unix::process::ExitStatusExt;
            use assert_cmd::Command;
            use serde_json;

            fn collect_and_print(name: &str, args: &[&str], stdin: Option<&str>) {
                let pkg_name_path = assert_cmd::cargo::cargo_bin(assert_cmd::pkg_name!());
                let pkg_name_path_str = pkg_name_path.to_str().unwrap();

                let mut cmd = Command::new("stdbuf");
                cmd.args(&["-e0", "-o0", pkg_name_path_str]);
                if !args.is_empty() {
                    cmd.args(args);
                }
                if let Some(input) = stdin {
                    cmd.write_stdin(input);
                }
                let output = cmd.output().expect("failed to execute process");
                let stdout = String::from_utf8_lossy(&output.stdout);
                let stderr = String::from_utf8_lossy(&output.stderr);
                let code = output.status.code().unwrap_or(-1);
                if output.status.signal() == Some(libc::SIGILL) {
                    panic!("UBSAN detected during collection!");
                }
                println!("{{");
                println!("  \"name\": \"{}\",", name);
                println!("  \"stdout\": {},", serde_json::to_string(&*stdout).unwrap());
                println!("  \"stderr\": {},", serde_json::to_string(&*stderr).unwrap());
                println!("  \"exit_code\": {}", code);
                println!("}}");
            }
            """
        ).strip()
    else:
        collect_stub = ""

    with collect_path.open("a+", encoding="utf-8") as f:
        f.write(collect_stub)
        rustfmt(collect_path)
    return collect_path


def write_extract_json_script(crate: Crate) -> Path:
    extract_json_path = crate.cargo_toml.parent / "extract_json.py"
    extract_json_contents = textwrap.dedent(
        """
        import sys, json
        buf = sys.stdin.read()
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(buf, buf.index('{'))
        print(json.dumps(obj, indent=2))
        """
    ).strip()
    extract_json_path.write_text(extract_json_contents)
    return extract_json_path
