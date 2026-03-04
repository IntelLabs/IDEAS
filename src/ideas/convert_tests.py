#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#


import json
import logging
from dataclasses import dataclass
from pathlib import Path

import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig

from ideas.tools import Crate, run_subprocess


logger = logging.getLogger("ideas.translate")


@dataclass
class ConvertConfig:
    test_vectors: list[Path] = MISSING
    output: Path = MISSING
    crate_manifest: Path = MISSING
    timeout: int = 600000

    # Library-specific inputs
    runner_manifest: Path | None = None
    template: Path | None = None


cs = ConfigStore.instance()
cs.store(name="convert_tests", node=ConvertConfig)


def rustfmt(path: Path) -> None:
    cmd = ["rustfmt", str(path)]
    run_subprocess(cmd)


def to_rust_str(string):
    return '"' + repr(string)[1:-1] + '"'


def is_bin_test(test_case: Path):
    test_case_json = json.loads(test_case.read_text())
    return "lib_state_in" not in test_case_json and "lib_state_out" not in test_case_json


def convert_tests_for_exec(test_cases: list[Path], crate: Crate, timeout: int = 60000) -> str:
    test_cases = list(filter(is_bin_test, test_cases))
    if len(test_cases) == 0:
        return ""

    # Add test dependencies
    crate.cargo_add(dep="assert_cmd@2.0.17", section="dev")
    crate.cargo_add(dep="ntest@0.9.3", section="dev")
    crate.cargo_add(dep="predicates@3.1.3", section="dev")

    output = ""
    output += "use assert_cmd::Command;\n"
    output += "use ntest::timeout;\n"
    output += "use predicates::prelude::*;\n"
    output += "\n"

    for test_case in test_cases:
        test_case_json = json.loads(test_case.read_text())

        # Skip tests that exercise undefined behavior
        if "has_ub" in test_case_json:
            output += f"// Skipping {test_case} because it exercises undefined C behavior of type {test_case_json['has_ub']}\n"
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

        output += "#[test]\n"
        output += f"#[timeout({timeout})]\n"
        output += f"fn test_case_{test_case.stem}() {{\n"
        output += "    Command::cargo_bin(assert_cmd::crate_name!()).unwrap()"
        if len(args) > 0:
            output += f".args(&[{', '.join([to_rust_str(arg) for arg in args])}])"
        if stdin is not None:
            output += f".write_stdin({to_rust_str(stdin)})"
        output += ".assert()"
        output += (
            f".stdout({to_rust_str(stdout_pattern)})"
            if not is_stdout_regex
            else f".stdout(predicates::str::is_match({to_rust_str(stdout_pattern)}).unwrap())"
        )
        output += (
            f".stderr({to_rust_str(stderr_pattern)})"
            if not is_stderr_regex
            else f".stderr(predicates::str::is_match({to_rust_str(stderr_pattern)}).unwrap())"
        )
        output += f".code({rc});\n"
        output += "}\n"

    return output


def is_lib_test(test_case: Path):
    test_case_json = json.loads(test_case.read_text())
    return "lib_state_in" in test_case_json and "lib_state_out" in test_case_json


def convert_tests_for_lib(
    test_cases: list[Path],
    crate: Crate,
    runner_manifest: Path | None,
    template_path: Path | None,
    timeout: int = 60000,
) -> str:
    if template_path is None:
        return ""

    test_cases = list(filter(is_lib_test, test_cases))
    if len(test_cases) == 0:
        return ""

    # Add test dependencies
    crate.cargo_add(dep="ntest@0.9.3", section="dev")
    crate.cargo_add(dep="once_cell@1.21.3", section="dev")
    crate.cargo_add(dep="test-cdylib@1.1.0", section="dev")

    # Load template
    template = template_path.read_text()
    # Replace the timeout
    template = template.replace("#[timeout(placeholder)]", f"#[timeout({timeout})]")

    # FIXME: This currently assumes that the macro generate_tests! is defined in the template
    # Use the generate_tests! macro to add tests
    lines = ["\n", "generate_tests! {"]
    lines.append(f'    "{runner_manifest}";')
    for test_case in test_cases:
        # Skip tests that exercise undefined behavior
        test_case_json = json.loads(test_case.read_text())
        if "has_ub" in test_case_json:
            continue
        lines.append(f'    test_vector_{test_case.stem} => "{test_case}",')
    lines.append("}")

    template += "\n".join(lines)
    return template


@hydra.main(version_base=None, config_name="convert_tests")
def main(cfg: ConvertConfig) -> None:
    output_dir = Path(HydraConfig.get().runtime.output_dir)
    logger.info(f"Saving results to {output_dir}")

    test_vectors = [Path(path) for path in cfg.test_vectors]
    crate = Crate(cargo_toml=cfg.crate_manifest)

    exec_tests = convert_tests_for_exec(test_vectors, crate, cfg.timeout)
    lib_tests = convert_tests_for_lib(
        test_vectors, crate, cfg.runner_manifest, cfg.template, cfg.timeout
    )
    # Write and format tests
    cfg.output.parent.mkdir(exist_ok=True, parents=True)
    cfg.output.write_text(exec_tests + "\n" + lib_tests)
    rustfmt(cfg.output)

    # Update VCS
    crate.add(cfg.crate_manifest)
    crate.add(cfg.output)
    crate.invalidate_metadata()
    crate.commit("Converted JSON test vectors to Rust tests")


if __name__ == "__main__":
    main()
