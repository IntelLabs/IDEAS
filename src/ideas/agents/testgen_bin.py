#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#


import sys
import os
import logging
import tempfile
import textwrap
import time
import shutil
from pathlib import Path
from dataclasses import dataclass

import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig

from ideas.tools import Crate
from ideas.agents.printer import ConsoleTee, LoggingConsolePrinter
from ideas.agents.build import strip_instrumentation
from ideas.agents.utils import (
    NEXTEST_DUMMY_TEST,
    nextest_config,
    write_coverage_script,
    write_collect_script,
    write_extract_json_script,
)

from kiss.agents.sorcar.useful_tools import UsefulTools
from kiss.core.relentless_agent import RelentlessAgent
from kiss.core.kiss_error import KISSError

logger = logging.getLogger("ideas.agents.testgen_bin")


@dataclass
class TestgenConfig:
    cargo_toml: Path = MISSING
    model: str = MISSING
    c_code: Path = MISSING
    project_name: str = MISSING
    test_crate_out: Path = MISSING

    guarantee_assert_tests: bool = False
    collect_to_assert: bool = False
    target_coverage: int = 90

    def __post_init__(self):
        if not self.c_code.exists():
            raise ValueError(f"c_code must be a single C file, got: {self.c_code}")


@dataclass
class TestgenInstructions:
    analyze_file: str = textwrap.dedent(
        """
        ## Step 1 – Analyze the standalone C file ##
        Carefully read and understand the single C source file at
        `{c_proj_path}/main.c`.

        This is a **standalone** C file that should **never** be edited.
        If the file is too large to analyze in one go, focus on its `main` function.

        Analyze how the program uses `argc`/`argv`, whether it reads from `stdin`,
        what it prints to `stdout`/`stderr`, which exit codes it returns, and which
        system libraries it links against.

        **Interactive program handling:** Determine whether the program is **batch**
        (runs and exits) or **interactive** (loops on `stdin`, e.g. `while(1)` +
        `fgets`/`scanf`). If interactive, identify the exit condition (menu
        choice, special command, or EOF only).

        **Infinite looping:** If the program loops infinitely, identify all relevant paths
        and their trigger conditions.

        **Undefined behavior:** Carefully analyze the C code for possible undefined behavior (UB).
        """
    )

    build_rs: str = textwrap.dedent(
        """
        ## Step 2 – Analyze test crate ##
        The directory at {rs_crate_path} contains a Rust crate that links the C code
        and has **no** code of its own.

        Analyze the `Cargo.toml` file and the `build.rs` files, and understand how they link the C code.
        The crate is a **binary** crate, so the C `main` function is the real entry point of the final executable.

        ### Working directory ###
        Execute `cd {rs_crate_path}` to enter the crate directory before any `cargo` command.
        Build using `cargo build` to confirm the C code compiles and links.

        ### Test framework ###
        The crate uses `cargo nextest` as the test framework exclusively.
        This **guarantees** that all tests are run in parallel and that no test can rely on side effects from another test.
        You **must** use `cargo nextest` to run tests, and you **must not** write any test that relies on shared state or side effects.
        """
    )

    coverage_script: str = textwrap.dedent(
        """
        ### Measuring coverage ###
        The crate is set up to measure source-based coverage of the C code with LLVM's sanitizers and coverage tools.
        Understand how this is done by analyzing the `build.rs` file and the `Cargo.toml`.

        The script `{rs_crate_path}/measure_coverage.sh` is used to run tests and measure C code coverage of the tests that will be written.
        This script must be run at any time to get an updated coverage report and identify untested code paths.
        This script is **complete and correct** as-is, and the task is to write tests that can be measured with it.

        This is the **only** way to measure coverage, so do not attempt to use other tools or methods.
        Instead write any relevant experiments as collection tests, as indicated in Step 3 below.
        """
    )

    write_collect_tests: str = textwrap.dedent(
        """
        ## Step 3 – Generate a data-collection test harness ##
        Based on the C program analysis, design input tests that achieve high coverage of the program.

        Each test must be **independent** and **self-contained**: it must set up its own input data,
        call the function under test, and capture all relevant output data without
        relying on any shared state or side effects from other tests.
        Because of `cargo nextest`'s parallel execution, clean-up on exit is **not** required.

        The test cases **cannot exercise undefined behavior (UB) or infinite loops**!
        If they do, instrumentation will make `cargo nextest run` output a
        failed test and return an error, and they should be re-attempted.

        Include at least:
        - a default / no-argument invocation (if the program supports it)
        - a typical invocation with representative arguments
        - an edge-case or boundary invocation (empty input, very long input,
          special characters, etc.)
        - an error path that triggers a non-zero exit code or stderr output
          (if the program has any such path)

        The `{rs_crate_path}/tests/test_collect.rs` file begins with the `collect_and_print`
        function that **must** be used to collect all test cases.
        This file can be considered **complete and correct** as-is, and the task is to write test cases that call `collect_and_print`.
        Note the `stdbuf` approach is **required** to ensure proper `libc` output buffering.

        For **each** test case, write a
        `#[test]` function named `collect_<NAME>` that calls `collect_and_print`
        with the test case's name, args, and stdin.  Example:
        ```rust
        #[test]
        fn collect_<NAME>() {{
            collect_and_print("<NAME>", &["arg1", "arg2"], Some("stdin data"));
        }}
        ```
        Pass `None` for stdin when the test case has no input.

        The tests generated in this step are not meant to **assert** outputs, but only collect them
        and they should **not** assume any state in the test file.
        They are only meant to be a harness to collect input/output data and coverage information.

        ### NUL-terminated strings in C ###
        If the C function takes string inputs, remember that they must be NUL-terminated.
        Not respecting this will cause silent memory corruption and make it impossible to collect meaningful data!
        To create a NUL-terminated string in Rust, you can create a `Vec<u8>` with the string bytes and a trailing `0`,
        and then pass a pointer to its first element.

        ### Non-persistence ###
        **All** collection tests must be designed to be run repeatedly without any clean-up,
        and they must not rely on any side effects or shared state.

        ### Portable, self-contained tests ###
        If the C code relies on pre-existing files on disk (e.g., through hardcoded paths),
        you must ensure that all tests **locally** create any required files with the expected content before calling the function under test,
        and that they do not rely on any pre-existing state on disk.
        If multiple tests reference **exactly** the same file, place a safe Lock around all accesses to that file to
        prevent race conditions; `cargo nextest` handles parallel execution by default otherwise.
        You **cannot** rely on files on-disk: the goal is for the test file to be moved to some other crate and still work.

        If the C code relies on network access, you must ensure that all tests mock the network interactions locally
        and do not rely on any external network state or connectivity.
    """
    )

    coverage_improvement: str = textwrap.dedent(
        """
        ### The coverage metric ###
        Use **branch coverage** to identify and exercise untested code paths.

        To improve branch coverage, generate interesting combinations of input arguments
        and the `stdin` stream, with special attention to edge cases and boundary conditions.
        Pay special attention to `libc` functions that may be used in the C code,
        and generate inputs that trigger different code paths in them
        (e.g. `strlen` with short vs long strings, `fgets` with input shorter vs longer than the buffer size, etc.).

        Ensure the new input values exercise **well-defined** code paths that improve branch coverage.
        Exercising UB will be caught and rejected by the sanitizers!

        Aim to achieve branch coverage of at least {target_coverage}%%.
        After **three** consecutive attempts where branch coverage has not improved by at least 1 percentage point,
        you may stop trying to improve coverage and proceed to the next step.

        Verify that all tests pass and JSON is correctly printed for **all** of them,
        including tests on new symbols added for improving coverage.
        """
    )

    analyze_data_collection_tests: str = textwrap.dedent(
        """
        ## Step 3 – Analyze the data-collection tests ##
        The crate already contains some data-collection tests in `tests/test_collect.rs`
        designed to print JSON outputs by running them and capturing their stdout.

        Carefully analyze the `tests/test_collect.rs` file and understand how it imports the FFI symbols,
        how it defines the `LibState` struct and the `collect_vector_<N>` tests, and how it prints the JSON output.

        Execute `cargo nextest run --test test_collect --nocapture 2>/dev/null`
        to run the tests and see the JSON output they print on the `stdout` channel.
        Validate **all** collection tests run successfully and print valid JSON with the expected structure.

        If tests pass **do NOT** modify them in any way at this stage.
        If tests exercise UB or trip sanitizers, you must remove them.
        If tests fail functionally, attempt to fix them until they pass and print the expected JSON.
        """
    )

    write_test_vectors: str = textwrap.dedent(
        """
        ## Step 4 – Save outputs as JSON files ##
        Create the directory `{test_vectors_path}`.

        Run each data-collection test **individually** and capture its stdout.
        Write the JSON output of each `collect_<NAME>` test to
        `{test_vectors_path}/<N>.json`, where `<N>` is the 1-based index.

        Use the provided `extract_json.py` script to extract the JSON reliably – do NOT rely on grep/sed:
        ```bash
        cargo nextest run --nocapture -- collect_vector_<N> --exact 2>/dev/null | \
            uv run extract_json.py > {test_vectors_path}/<N>.json
        ```

        Verify each file is valid JSON by running
        `uv run python -m json.tool {test_vectors_path}/<N>.json`.
        """
    )

    write_assert_tests: str = textwrap.dedent(
        """
        ## Step 5 – Write assert-style Rust tests ##
        Create `{rs_crate_path}/tests/test_assert.rs`.

        Hardcode all expected values as Rust string literals taken from the
        JSON files saved in Step 4.

        The file **must** use these imports and the structure below:
        ```rust
        use assert_cmd::Command;
        use predicates::prelude::*;
        ```

        IMPORTANT: You **must** write an assertion test for each collection test, no matter
        how many collection tests are there!
        Write them one-by-one if there are too many.

        For **each** test case from the JSON, write a `#[test]` function named
        `test_case_<NAME>` following this exact pattern:
        ```rust
        #[test]
        fn test_case_<NAME>() {{
            let pkg_name_path = assert_cmd::cargo::cargo_bin(assert_cmd::pkg_name!());
            let pkg_name_path_str = pkg_name_path.to_str().unwrap();

            Command::new("stdbuf")
                .args(&["-e0", "-o0", pkg_name_path_str])
                // .args(&["a1", "a2"])         // only if args non-empty
                // .write_stdin("data")         // only if stdin non-empty
                .assert()
                .stdout("<EXPECTED_STDOUT>")
                .stderr("<EXPECTED_STDERR>")
                .code(<EXPECTED_EXIT_CODE>);
        }}
        ```

        Rules:
        - Use the **exact** stdout/stderr strings from the JSON, properly escaped
          in Rust string literals.
        - If expected stderr is empty use `.stderr("")`.
        - If stderr contains variable content (PIDs, paths) use
          `predicates::str::contains(...)` to capture **path-invariant** contents.

        Once done, run:
        ```bash
        cargo nextest run --test test_assert --cargo-quiet
        ```
        All tests **must** pass and not exercise any undefined behavior.
        """
    )

    simple_exit: str = textwrap.dedent(
        """
        Once the task is complete, exit immediately.
        Do not over-verify or generate extensive reports.
        """
    )

    @classmethod
    def coverage_based(cls) -> str:
        return (
            cls.analyze_file
            + cls.build_rs
            + cls.coverage_script
            + cls.write_collect_tests
            + cls.coverage_improvement
            + cls.write_test_vectors
            + cls.write_assert_tests
            + cls.simple_exit
        )

    @classmethod
    def collect_to_assert(cls) -> str:
        return (
            cls.analyze_file
            + cls.build_rs
            + cls.analyze_data_collection_tests
            + cls.write_test_vectors
            + cls.write_assert_tests
            + cls.simple_exit
        )


cs = ConfigStore.instance()
cs.store(name="testgen", node=TestgenConfig)


def get_tools():
    useful_tools = UsefulTools()
    return [useful_tools.Bash, useful_tools.Read, useful_tools.Edit, useful_tools.Write]


@hydra.main(version_base=None, config_name="testgen")
def main(cfg: TestgenConfig) -> None:
    try:
        _main(cfg)
    except Exception as e:
        logger.exception(e)
        sys.exit(1)


def _main(cfg: TestgenConfig) -> None:
    output_dir = Path(HydraConfig.get().runtime.output_dir)

    # Separately log the complete trajectory
    logger_trajectory = logging.getLogger("ideas.testgen.trajectory")
    logger_trajectory.propagate = False
    fh = logging.FileHandler(output_dir / f"testgen_trajectory-{int(time.time())}.log")
    fh.setFormatter(ConsoleTee.StripANSIFormatter("%(asctime)s %(message)s"))
    logger_trajectory.addHandler(fh)
    # Simultaneous print and log to file
    printer = LoggingConsolePrinter(logger=logger_trajectory)
    agent = RelentlessAgent(name="C executable test generator")

    # Generate helper scripts and files in the crate
    crate = Crate(output_dir / "Cargo.toml")
    nextest_config(crate)
    if not cfg.collect_to_assert:
        write_coverage_script(crate)
        write_collect_script(crate)
    write_extract_json_script(crate)

    # Workspace
    work_dir = Path(tempfile.mkdtemp())
    workspace_dir = work_dir / "test_crates"
    shutil.copytree("test_crates", workspace_dir)

    # Remove all log files
    for log_file in workspace_dir.glob("**/*.log"):
        log_file.unlink()

    # Paths the agent will populate
    rs_crate_path = work_dir / cfg.test_crate_out
    test_vectors_path = rs_crate_path / "json"

    # If assertion tests already exist, they must be correct
    if (rs_crate_path / "tests" / "test_assert.rs").is_file():
        crate = Crate(rs_crate_path / "Cargo.toml")
        ok, output, error, _ = crate.cargo_test("test_assert", quiet=True)
        if not ok:
            raise RuntimeError(
                "Existing assertion tests failed to pass, previous agent did not clean them up!"
            )
        logger.info(
            f"Assertion tests already exist at {rs_crate_path / 'tests/test_assert.rs'}, skipping agent!"
        )
        return

    # Hide instrumentation from conversion agent
    if cfg.collect_to_assert:
        strip_instrumentation(crate)

    # Build the task prompt
    task_description = (
        TestgenInstructions.collect_to_assert()
        if cfg.collect_to_assert
        else TestgenInstructions.coverage_based()
    )
    arguments = {
        "c_proj_path": cfg.c_code.parent,
        "rs_crate_path": rs_crate_path.relative_to(work_dir),
        "test_vectors_path": test_vectors_path.relative_to(work_dir),
        "target_coverage": cfg.target_coverage,
    }
    task_description = task_description.format(**arguments)

    # Run agent in the work directory
    os.chdir(work_dir)
    try:
        agent.run(
            model_name=cfg.model,
            prompt_template=task_description,
            max_steps=100,
            max_budget=4,
            max_sub_sessions=1,
            work_dir=str(work_dir),
            tools=get_tools(),
            printer=printer,
            verbose=True,
        )
    except KISSError as e:
        logger.warning(f"Agent claims it failed with error: {e}. Clean-up will continue.")

    # Verify that collection tests exist
    if not (rs_crate_path / "tests" / "test_collect.rs").is_file():
        raise RuntimeError(
            f"Data collection tests were not found at {rs_crate_path / 'tests/test_collect.rs'}!"
        )

    # Strip instrumentation to ensure tests are correct and do not rely on it
    crate = Crate(rs_crate_path / "Cargo.toml")
    strip_instrumentation(crate)
    ok, output, error, _ = crate.cargo_test("test_collect", quiet=True)
    if not ok:
        raise RuntimeError(
            f"Data collection tests failed to pass without instrumentation! Output:\n{output}\nError:\n{error}"
        )

    ok, output, error, _ = crate.cargo_test("test_assert", quiet=True)
    if not ok:
        logger.error(f"Assertion tests failed to pass! Output:\n{output}\nError:\n{error}")
        # Remove incomplete assertion tests, if any
        if (rs_crate_path / "tests" / "test_assert.rs").is_file():
            (rs_crate_path / "tests" / "test_assert.rs").unlink()

            # And replace with an always-passing test (nextest does not allow empty test files)
            if cfg.guarantee_assert_tests:
                logger.warning("Writing dummy test_assert.rs that always passes")
                (rs_crate_path / "tests" / "test_assert.rs").write_text(NEXTEST_DUMMY_TEST)

    # Clean the crate and copy it back to the project directory
    crate.cargo_clean()
    shutil.copytree(rs_crate_path, output_dir, dirs_exist_ok=True)


if __name__ == "__main__":
    main()
