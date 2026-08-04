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

from ideas import create_translation_unit, extract_info_c
from ideas.agents.printer import ConsoleTee, LoggingConsolePrinter
from ideas.agents.utils import (
    RESTRICT_COVERAGE,
    strip_line_directives,
    write_instrumentation_script,
    write_assert_script,
    write_collect_script,
    write_profile_list,
)
from ideas.consolidate import get_symbols_and_dependencies, is_system_symbol
from ideas.tools import Crate

from kiss.agents.sorcar.useful_tools import UsefulTools
from kiss.core.kiss_agent import KISSAgent
from kiss.core.kiss_error import KISSError

logger = logging.getLogger("ideas.agents.generate_io_tests")


@dataclass
class TestgenConfig:
    model: str = MISSING
    output_path: Path = MISSING
    manifest: Path = MISSING

    coverage: int = 100  # [%] branch coverage

    budget: float = 4.0  # USD
    steps: int = 100


@dataclass
class TestgenInstructions:
    overview: str = textwrap.dedent(
        """
        # Overview
        The working directory is {work_dir}. All paths are relative to the working directory.
        You must work **strictly** inside {work_dir} and never read, write, or list anything outside it.
        This directory is self-contained and holds everything you need.
        Never use absolute paths that leave it, never use `..` to climb above it, and never `cd` out of it.

        {work_dir} contains a Rust crate that links C code through the Rust C FFI.
        Everything you need to know must be derived from the C source in `src/lib.c`.
        The C source is amalgamated and has no `#include` directives.
        Every declaration and definition you need is in the file.
        Do not test or look for the definition of any functions with an `extern` declaration.

        Do not edit the `src/lib.c` file.
        """
    )

    analyze_library: str = textwrap.dedent(
        """
        Public (exported) functions have pre-generated FFI bindings using `bindgen` in `src/lib.rs`.
        The bindings and build files are correct and must not be changed.

        Understand the C source and, for each public function, determine which parameters are input-only,
        which are output-only (written by the callee) and which are modified in place.
        This tells you how to set up inputs and where to capture outputs.

        Note any logic that can loop forever and the conditions that trigger it.
        Identify any input that would cause undefined behavior so you can avoid it.
        The code can only be driven through calling public functions with their arguments set up,
        environment variables (if any are used), and any input files it reads.
        You cannot call private functions or edit the program to reach more code.
        """
    )

    analyze_binary: str = textwrap.dedent(
        """
        The C `main` function (correctly placed in `src/lib.c` and linked in)
        is the entry point of the final executable.
        The build files are correct and must not be changed.

        Understand the C source and determine how the program uses `argc`/`argv`, whether it reads from stdin,
        what it prints to stdout and stderr, and which exit codes it returns.

        Decide whether the program is batch (runs and exits) or interactive (loops on stdin).
        If interactive, find its exit condition.
        Note any input that can loop forever, and any input that would cause undefined behavior so you can avoid it.
        The code can only be driven through the `main` function: command-line arguments, stdin, environment
        variables, and any input files it reads.
        You cannot call any other function directly or edit the program to reach more code.
        """
    )

    analyze_instrumentation: str = textwrap.dedent(
        """
        # Instrumentation
        The `{instrument_path}` script instruments the tests for coverage and sanitizers.
        It should not be modified and should always be executed to get accurate coverage results.
        It takes a single argument selecting which test file to run, either `collect` or `io`:
        ```bash
        {instrument_path} collect
        {instrument_path} io
        ```

        Carefully read and understand this script, it is **critical** for correct measurements.

        Coverage is measured only if every test passes under every sanitizer build; if anything
        fails, the script exits early and none of the coverage outputs below are generated.
        {coverage_scope}
          - `{coverage_dir}/coverage_summary.log`: the aggregate per-file and TOTAL coverage
            summary table. Also printed to stdout under `Coverage summary:`.
          - `{coverage_dir}/coverage_report.log`: the full per-line annotated coverage report,
            including branch coverage details.
          - `{coverage_dir}/uncovered_branches.log`: just the uncovered branches (a branch
            whose True or False count is zero), or `none` if fully covered. Also printed to
            stdout under `Uncovered branches:`.

        Running this script and inspecting its logs is the **only** allowed way to run the tests and get metrics.
        You must never invoke `cargo test`, `cargo nextest`, `cargo llvm-cov`, or any other test runner directly,
        and you must never derive coverage or output data by any other means.
        Every test run must go through `{instrument_path} collect` or `{instrument_path} io`.
        No ad-hoc or manual run can replace this script.

        A sanitizer `FAIL` only means the test failed under that sanitizer build.
        It does not necessarily mean the test exercises undefined behavior.
        The script does not tell the two apart, so read `{sanitizer_dir}/<feature>.log`:
          - A sanitizer diagnostic (`ERROR: AddressSanitizer:`, `runtime error:`,
            `SUMMARY: UndefinedBehaviorSanitizer:`, or a stack trace into the C source) means the
            input drives the C code into UB. Change that input, or drop the case.
          - A Rust panic (`assertion ... failed`, `panicked at tests/...`) with no sanitizer
            diagnostic is a normal test failure. Your expected value is wrong, so fix the test.
          - A test killed for running too long is a hang, usually a program waiting on stdin.
            Give it the input it waits for, or close its stdin.

        Read the whole report, not just the last error: a sanitizer diagnostic often appears inside
        an assertion message. The same test failing under several features is one problem, not many.

        The sanitizers are configured by this script.
        Do not set or override `ASAN_OPTIONS`, `UBSAN_OPTIONS`, `LSAN_OPTIONS`, or any other sanitizer
        variable, whether in a test, in the environment, or in a config file.
        Never mark a test `#[ignore]`, comment it out, or otherwise skip it to get past a failure.
        Silencing a check invalidates the whole result.
        """
    )

    goal_library: str = textwrap.dedent(
        """
        # Goal
        Your goal is to write input/output C FFI tests to `{test_path}` that call public C functions
        directly and verify their outputs with `assert!` or `assert_eq!`.
        """
    )

    goal_binary: str = textwrap.dedent(
        """
        # Goal
        Your goal is to write input/output tests to `{test_path}` that run the binary
        and verify its stdout, stderr, and exit code.
        """
    )

    goal_common: str = textwrap.dedent(
        """
        You must achieve a branch coverage of at least {target_coverage}%.
        You can **never** exercise UB (undefined behavior) in any test.
        If you can no longer improve branch coverage (e.g., unreachable code without UB), you may early stop.

        Reach this goal in three steps:

        1. Collect input/output pairs in `{collect_path}`.
           Use the `{instrument_path} collect` command to instrument the tests for coverage and sanitizers.

        2. Improve branch coverage by appending more collection tests to `{collect_path}`.
           Any data collection attempt that exercises UB will be detected by the instrumentation and rejected.

        3. Write pure assertion I/O tests to `{test_path}` from the collected data.
           Use the `{instrument_path} io` command to run a final verification of the coverage and sanitizers.
           The coverage should exactly match the outcome of `{instrument_path} collect` and all tests should pass without exercising UB.
        """
    )

    collect_common: str = textwrap.dedent(
        """
        ## Step 1: Collect input/output pairs

        Collect input/output pairs of data by writing tests to `{collect_path}`.

        These tests should not assert outputs, but serialize them using `serde` for later conversion
        to expected value tests.

        Build up complexity gradually, in two passes:

        1a. Start with the simplest possible tests: one isolated invocation each, with straightforward inputs.
            Cover the common path of every entry point this way before doing anything more elaborate.
        1b. Only once the simple tests are collected and passing, add chained tests that perform several
            invocations in sequence, where earlier invocations set up the state for later ones.

        Prefer the simplest test that reaches a given behavior. Reach for a chained test only when the
        behavior genuinely cannot be reached by a single invocation.

        Keep tests short and contained, if possible: one behavior per test, named after that behavior.
        Never grow an existing test to cover something new. Many small tests are better than a few large ones.
        """
    )

    collect_library: str = textwrap.dedent(
        """
        The `{collect_path}` file is pre-populated and imports every
        FFI binding for the C library: all functions and all data structures.
        Use them exactly as imported; do not redeclare or wrap them.

        It also provides a helper that must be used and must not be changed:
          - `save_case(name, case)` serializes any `Serialize` value to `{json_dir}/<name>.json`.

        Append each new test below the
        `// ==== Add collection tests below this line ====` marker.

        Call each C function through its FFI binding, set up all of its inputs, and capture the resulting state.
        For pointer parameters, allocate the pointed-to data as a local variable and pass a raw pointer to it;
        never use hard-coded addresses. NUL-terminate any C strings, or the data will be silently corrupted.

        Start with one test per public function, calling that function exactly once. Only after those exist
        should you write chained tests that call several functions in sequence on shared state (for example an
        init/update/finalize sequence, or a function whose output feeds the next function's input).

        In a chained test, capture the intermediate state after every call,
        not just the final one, so each step can be asserted later.

        If a function reads or writes files, run it inside a fresh temporary directory unique to the test.
        Create any input files there first, then capture the files the function creates or modifies
        (their paths and contents) as part of the output state. Never touch shared or absolute system paths,
        so the collection and I/O tests stay isolated and reproducible.

        Each collection test should finish with a single `save_case("<test-name>", &case)`, writing one JSON
        file to `{json_dir}/<test-name>.json` per collection test.
        Write both inputs and outputs using the same data structure across all collection tests.
        This data structure should contain the input and output state of all inputs
        (in case functions modify data in-place) and any return value.
        """
    )

    collect_binary: str = textwrap.dedent(
        """
        The `{collect_path}` file is pre-populated with two helpers that must be used and must not be changed:
          - `run(args, stdin) -> Call` runs the binary once and returns its `stdout`, `stderr`,
            and `exit_code`.
          - `save_case(name, calls)` serializes an ordered slice of calls to `{json_dir}/<name>.json`.

        Push every `Call` into a local `Vec` in the order it was made, and finish each test with a single
        `save_case("<test-name>", &calls)`, passing the test's own function name.

        Write one `#[test]` per collection test.
        Append each new test to the end of the file, below the `// ==== Add collection tests below this line ====`
        marker. Leave the helpers above that marker unchanged.

        Start with tests that call `run` exactly once, covering each subcommand or mode on its own with simple
        arguments and stdin. Only after those exist should you write chained tests that call `run` several times
        in sequence, where earlier invocations set up state (files, configuration) for later ones. In a chained
        test, push every call, so each intermediate invocation can be asserted later.

        For programs with multiple subcommands or modes, cover each one, and cover sequential invocations
        of subcommands in any relevant combination.

        If the program reads or writes files, set up a fresh temporary directory unique to the test.
        Create any input files there first, then capture the files the program creates or modifies
        (their paths and contents) as part of the collected output. Never touch shared or absolute system paths,
        so tests stay isolated and reproducible.

        Each collection test produces one file, `{json_dir}/<test-name>.json`, holding the test `name`
        and its ordered `calls`. Each call has its `args` and `stdin` inputs and its `stdout`, `stderr`,
        and `exit_code` outputs. The script empties `{json_dir}` before every collection run, so the
        files left there always match the tests in `{collect_path}`.
        """
    )

    improvement: str = textwrap.dedent(
        """
        # Step 2: Improve branch coverage

        After initial data collection, focus on adding more tests to increase branch coverage.
        Ensure that additions do not introduce undefined behavior (UB).

        To review the current coverage status, execute:
        ```bash
        cat {coverage_dir}/coverage_summary.log
        ```

        To review a summary of the current uncovered code branches, execute:
        ```bash
        cat {coverage_dir}/uncovered_branches.log
        ```

        To review the current uncovered code branches in detail and the complete report, execute:
        ```bash
        cat {coverage_dir}/coverage_report.log
        ```

        Carefully reason about code paths and behavior to identify program states that may lead to uncovered branches.

        Keep preferring the simplest test that covers a branch: first try new inputs to a single invocation,
        and only chain invocations when a branch depends on state left behind by an earlier one.
        """
    )

    assert_common: str = textwrap.dedent(
        """
        # Step 3: Write pure assertion I/O tests

        Once branch coverage is satisfactory, write the pure assertion I/O tests to `{test_path}`
        using the data collected in the `{json_dir}` JSON files.

        Write one I/O test per collection test, reading the expected values from the matching JSON file.
        These tests must be pure: hard-code the expected values as plain Rust literals and do not depend on
        `serde`, `serde_json`, the `{json_dir}` files, or the `{collect_path}` file in any way. Each test must
        set up its own inputs and assert every recorded output, so it still passes if moved to another crate.

        An I/O test must mirror the structure of the collection test it came from. For a chained collection test,
        replay the same sequence in the same order and assert the recorded intermediate state after every step,
        not only the final result. An intermediate step that is not asserted is a missing assertion.

        Do not skip any assertion, and do not weaken one just to make a test pass.
        """
    )

    assert_library: str = textwrap.dedent(
        """
        The `{test_path}` file is pre-populated with `use {lib_name}::*;`, the same FFI bindings
        `{collect_path}` imports. Append each new test to the end of the file, below the
        `// ==== Add assertion tests below this line ====` marker.

        For each JSON file, reconstruct the recorded input state as Rust literals, call the C function through
        its FFI binding, and assert that every output field matches the recorded output state:
          - use `assert_eq!` for integer and boolean fields;
          - for floating-point fields, compare with a small relative epsilon rather than exact equality;
          - for pointer outputs, dereference the pointer and compare the pointed-to value, not the address;
          - for files the function created or modified, recreate the recorded inputs in a fresh temporary
            directory and assert the resulting file paths and contents.

        For a chained case, call the same functions in the same order and assert the recorded state after each
        call, including any state modified in place by an earlier call.
        """
    )

    assert_binary: str = textwrap.dedent(
        """
        The `{test_path}` file is pre-populated with a helper that must be used and must not be changed:
          - `run(args, stdin) -> Call` runs the binary once and returns its `stdout`, `stderr`,
            and `exit_code`.

        This is the only way you may run the binary. It uses `stdbuf -e0 -o0`, exactly like the
        collection helper, so it sees the same unbuffered output the recorded values came from.
        Never use `Command::cargo_bin` or `std::process::Command` in this file.
        It is the collection helper without the recording, so a collection test body carries over as is:
        drop the `Vec` and the `save_case` call, and assert each `Call` instead.

        Append each new test to the end of the file, below the
        `// ==== Add assertion tests below this line ====` marker, and leave the helper above it unchanged.

        For each JSON file, replay its `calls` in order and assert each one against literals:

        ```rust
        let call = run(&["-E", "-"], Some("int x;\\n"));
        assert_eq!(call.exit_code, 0);
        assert_eq!(call.stdout, "int x;\\n");
        assert_eq!(call.stderr, "");
        /// TODO: File state assertions, if any
        ```

        When a value changes between runs (temporary paths, PIDs), assert the stable part with
        `predicates::str::contains`. For files the program created or changed, recreate the recorded
        inputs in a fresh temporary directory and assert the resulting paths and contents.

        Assert every call in the sequence, not just the last one.
        """
    )

    finish: str = textwrap.dedent(
        """
        Once the target coverage is achieved and all I/O tests are written, exit immediately.
        Do not generate extensive reports or perform redundant sanity checks.
        """
    )

    library: str = (
        overview
        + analyze_library
        + analyze_instrumentation
        + goal_library
        + goal_common
        + collect_common
        + collect_library
        + improvement
        + assert_common
        + assert_library
        + finish
    )
    binary: str = (
        overview
        + analyze_binary
        + analyze_instrumentation
        + goal_binary
        + goal_common
        + collect_common
        + collect_binary
        + improvement
        + assert_common
        + assert_binary
        + finish
    )


cs = ConfigStore.instance()
cs.store(name="generate_io_tests", node=TestgenConfig)


def get_tools():
    useful_tools = UsefulTools()
    return [useful_tools.Bash, useful_tools.Read, useful_tools.Edit, useful_tools.Write]


def _wrapup_notice(collect_path: Path, test_path: Path) -> str:
    return (
        "Stop starting new work and consolidate what you already have: finish any "
        f"test you left half-written, and make `{test_path}` mirror `{collect_path}` "
        "exactly, one assertion test per collected case. Running out of steps is not "
        "a reason to cut corners: keep every assertion, do not weaken or drop one to "
        "make a test pass, do not mark tests `#[ignore]`, and do not touch the "
        "sanitizer configuration. If a case cannot be finished properly, remove it "
        "from both files rather than leaving a broken version of it behind."
    )


def _main(cfg: TestgenConfig) -> None:
    output_dir = Path(HydraConfig.get().runtime.output_dir)
    logger.info(f"Saving results to {output_dir}")

    # Separately log the complete trajectory
    logger_trajectory = logging.getLogger("ideas.agents.generate_io_tests.trajectory")
    logger_trajectory.propagate = False
    trajectory_log = output_dir / f"testgen-{int(time.time())}.log"
    fh = logging.FileHandler(trajectory_log)
    fh.setFormatter(ConsoleTee.StripANSIFormatter("%(asctime)s %(message)s"))
    logger_trajectory.addHandler(fh)
    # Simultaneous print and log to file
    printer = LoggingConsolePrinter(logger=logger_trajectory)
    agent = KISSAgent(name="C code reviewer")

    # -sys crate setup
    sys_crate = Crate(cfg.manifest)
    sys_root = sys_crate.cargo_toml.parent
    sys_test_dir = sys_root / "tests"
    sys_json_dir = sys_root / "json"
    sys_test_dir.mkdir(parents=True, exist_ok=True)
    sys_json_dir.mkdir(parents=True, exist_ok=True)

    # Analyze consolidated code to find all reachable symbols
    template = "bin" if len(sys_crate.bin_targets) > 0 else "lib"
    assert sys_crate.lib_src_path is not None, "Expected lib.rs to exist in -sys crate!"
    c_src_path = sys_crate.lib_src_path.with_suffix(".c")
    tu = create_translation_unit(c_src_path)
    asts = [extract_info_c(tu)]
    symbols, _ = get_symbols_and_dependencies(
        asts, external_symbol_names=["c:@F@main"] if template == "bin" else None
    )
    profile_list = None
    if RESTRICT_COVERAGE:
        profile_functions = [
            s.spelling
            for s in symbols.values()
            if s.is_function and s.is_definition and not is_system_symbol(s)
        ]
        if profile_functions:
            logger.info(f"Restricting coverage to {len(profile_functions)} functions")
            profile_list = write_profile_list(sys_root / "profile.lst", profile_functions)
        else:
            logger.warning("No instrumentable functions found, coverage stays unrestricted!")

    # Add testing dependencies
    if template == "bin":
        sys_crate.cargo_add(dep="assert_cmd@2.0.17", section="dev")
        sys_crate.cargo_add(dep="predicates@3.1.3", section="dev")
        sys_crate.cargo_add(dep="libc@0.2", section="dev")
    sys_crate.cargo_add(dep="serde@1", section="dev", features=["derive"])
    sys_crate.cargo_add(dep="serde_json@1", section="dev")
    sys_crate.invalidate_metadata()

    # Write instrumentation and test scripts
    write_collect_script(
        sys_test_dir / "collect.rs",
        template=template,
        lib_name=sys_crate.lib_name if template == "lib" else None,
    )
    write_assert_script(
        sys_test_dir / "io.rs",
        template=template,
        lib_name=sys_crate.lib_name if template == "lib" else None,
    )
    write_instrumentation_script(
        sys_root / "instrument.sh", features=["cc_asan", "cc_ubsan"], profile_list=profile_list
    )

    # Isolate the crate
    work_dir = Path(tempfile.mkdtemp()) / sys_root.name
    shutil.copytree(
        sys_root, work_dir, dirs_exist_ok=True, ignore=shutil.ignore_patterns("*.log", ".*")
    )

    # Strip line directives
    for c_file in (work_dir / "src").glob("*.c"):
        strip_line_directives(c_file)

    # If this is a binary, remove the `lib.rs` file and simplify `main.rs`
    if template == "bin":
        lib_rs = work_dir / "src" / "lib.rs"
        assert lib_rs.exists(), "Expected to find lib.rs in -sys crate"
        lib_rs.unlink()
        main_rs = work_dir / "src" / "main.rs"
        main_rs.write_text("#![no_main]\n")

    # Build the task prompt
    task_description = (
        TestgenInstructions.library if template == "lib" else TestgenInstructions.binary
    ).strip()
    collect_path = (sys_test_dir / "collect.rs").relative_to(sys_root)
    test_path = (sys_test_dir / "io.rs").relative_to(sys_root)
    coverage_scope = (
        f"Coverage is restricted to the program's own functions through `{profile_list.name}`;"
        " libc and system code is never counted, so do not try to cover it, and never modify"
        " that file:"
        if profile_list is not None
        else "The coverage outputs are:"
    )
    task_description = task_description.format(
        work_dir=work_dir,
        instrument_path="instrument.sh",
        target_coverage=cfg.coverage,
        collect_path=collect_path,
        test_path=test_path,
        json_dir=sys_json_dir.relative_to(sys_root),
        sanitizer_dir="sanitizer_logs",
        coverage_dir="coverage_logs",
        coverage_scope=coverage_scope,
        lib_name=sys_crate.lib_name or "",
    )

    # Run the agent
    os.chdir(work_dir)
    agent.wrapup_steps = 10
    agent.wrapup_notice = _wrapup_notice(collect_path, test_path)
    try:
        agent.run(
            model_name=cfg.model,
            prompt_template=task_description,
            max_steps=cfg.steps,
            max_budget=cfg.budget,
            tools=get_tools(),
            printer=printer,
            verbose=True,
        )
    except KISSError as e:
        logger.warning(f"Agent claims it failed with error: {e}")

    # Copy the generated tests back
    work_test_dir = work_dir / "tests"
    sys_test_dir.mkdir(parents=True, exist_ok=True)
    for name in ("collect.rs", "io.rs"):
        src = work_test_dir / name
        if src.exists():
            shutil.copy2(src, sys_test_dir / name)

    # Copy the collected JSON data back
    work_json_dir = work_dir / "json"
    if work_json_dir.is_dir():
        shutil.copytree(work_json_dir, sys_json_dir, dirs_exist_ok=True)
    else:
        logger.warning("The agent removed the JSON directory!")

    # Guarantee tests are generated
    placeholder = "#[test]\nfn placeholder() {\n    assert_eq!(1, 1);\n}\n"
    for name in ("collect.rs", "io.rs"):
        test_file = sys_test_dir / name
        if not test_file.exists():
            test_file.write_text(placeholder)
            logger.warning(
                f"{name} not generated by the agent, writing always-pass placeholder!"
            )
    sys_crate.vcs.add(sys_test_dir / "collect.rs", sys_test_dir / "io.rs", sys_json_dir)
    sys_crate.vcs.commit("Generated I/O equivalence tests")


@hydra.main(version_base=None, config_name="generate_io_tests")
def main(cfg: TestgenConfig) -> None:
    try:
        _main(cfg)
    except Exception as e:
        logger.exception(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
