#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#


import logging
import shutil
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig

from ideas.agents.printer import ConsoleTee, LoggingConsolePrinter
from ideas.agents.utils import finalize_tests, strip_line_directives, write_test_templates
from ideas.agents.utils import TESTGEN_BOTTOM_UP, TESTGEN_FINISH_EARLY
from ideas.agents.verifiers import get_fresh_copy, verification_service
from ideas.tools import Crate

from kiss.agents.sorcar.useful_tools import UsefulTools
from kiss.core.kiss_agent import KISSAgent
from kiss.core.kiss_error import KISSError, BudgetExceededError

logger = logging.getLogger("ideas.agents.generate_io_tests")


@dataclass
class TestgenConfig:
    model: str = MISSING
    output_path: Path = MISSING
    manifest: Path = MISSING

    coverage: int = 100  # [%] branch coverage

    budget: float = 4.0  # USD
    steps: int = 100

    vcs: str = "none"


class TestgenAgent(KISSAgent):
    """Give the model its step count without token or budget telemetry."""

    def _get_usage_info_string(self) -> str:
        return f"Steps: {self.step_count}/{self.max_steps}"

    def _can_finish(self) -> bool:
        return self.step_count >= self._finish_step_threshold() or TESTGEN_FINISH_EARLY

    def _finish_tool_description(self) -> str:
        # The base description advertises the step gate that TESTGEN_FINISH_EARLY lifts.
        if not TESTGEN_FINISH_EARLY:
            return super()._finish_tool_description()
        return (
            "Finish with the final answer. Call this only once the inventory is exhausted: every "
            "public function or mode is tested and verified, the coverage target is met, and every "
            "remaining branch has been proven unreachable. Difficulty, slow progress, a long "
            "trajectory, and the number of passing tests never justify finishing."
        )


@dataclass
class TestgenInstructions:
    introduction: str = textwrap.dedent(
        """
        # Goal
        Work **strictly** inside `{work_dir}`; all paths below are relative to it.

        Your goal is to generate input/output tests in `{test_path}` and
        reach at least {target_coverage}%% branch coverage without Undefined Behavior (UB).

        The amalgamated `src/lib.c` is the source of truth for the implementation-under-test.
        Never edit it, its Rust bindings, the build files, `{instrument_path}`, or the supplied helpers.
        Do not test or search for definitions of functions declared `extern`. Do not run `git``;
        repository metadata is not provided, so inspect and compare files directly.

        Identify and avoid all code paths that lead to infinite loops or hang execution waiting for
        interactive inputs or network requests.
        """
    )

    library_top_down: str = textwrap.dedent(
        """
        Start generating tests from the highest-level public functions: use implementation call
        relationships to identify entry points and orchestration APIs that exercise substantial project
        behavior. Give each one its smallest focused real-work success case, then work downward through
        lower-level public primitives without skipping any inventory item or combining unrelated APIs.
        """
    )

    library_bottom_up: str = textwrap.dedent(
        """
        Start generating tests from the lowest-level public functions: use implementation call
        relationships to identify leaf APIs and foundational primitives used throughout the project.
        Give each one its smallest focused real-work success case, then work upward through higher-level
        entry points and orchestration APIs without skipping any inventory item or combining unrelated APIs.
        """
    )

    library_scope: str = textwrap.dedent(
        """
        Do not change `src/lib.rs` or any build file. Inventory every public function by subsystem,
        including inputs, outputs, mutations, return paths, errors, and file or environment effects. Read
        each implementation and avoid inputs that hang or cause UB.

        {library_breadth_order}

        Drive the code only through public FFI bindings, environment variables, and input files. Never
        call private functions or edit the program to reach code. Assert outputs, mutations, return values,
        and file-system effects.
        """
    ).replace(
        "{library_breadth_order}",
        (library_bottom_up if TESTGEN_BOTTOM_UP else library_top_down).strip(),
    )

    binary_scope: str = textwrap.dedent(
        """
        Do not change any build file. Read C `main` completely and follow every dispatch entry and handler.
        Understand every mode or subcommand with its flags, arguments, stdin, stdout, stderr, exit codes,
        and file effects. Derive exit conditions and dangerous inputs from implementations, dispatch
        tables, option parsers, and usage strings.

        Drive the program through `main` using arguments, stdin, environment, and files, and reach what
        `main` cannot by calling public functions directly through the `{lib_name}` FFI bindings already
        imported by the test files. **Never** edit the program to reach code.

        When calling `main`, assert stdout, stderr, exit codes, return values, mutations, and file-system effects.
        """
    )

    portability: str = textwrap.dedent(
        """
        # Linux portability and isolation
        Generated tests must behave identically in any fresh compatible Linux container; assume only the
        supplied harness and project-declared dependencies.
        Documented Linux kernel and `libc` contracts are allowed.

        Every expected value and asserted file must trace to project code, a literal, an explicit test-owned input,
        or such an OS contract; sandbox containment does not make an ambient value portable.
        Generated tests may invoke only the target through supplied FFI or helpers. Neither a test nor its result may
        depend, directly or indirectly, on unrelated executables, installed package data, daemons, network or DNS,
        system or user configuration, caches, templates, account databases, or their presence, version, or contents.

        Assume every environment value not fixed by the harness or test may be absent or arbitrary. Never use inherited
        identity, account details, hostnames, paths, locale, timezone, proxy or tool settings, or other host values
        to select a case, construct input, or form an exact expectation; assert only stable structure if exposed.

        When the target searches defaults or imports external data, supply literal sandbox-owned data through its
        public interface; never copy ambient files or discover host programs for fixtures.
        A portability obstacle never permits skipping an item: replace the ambient input or assert a stable,
        host-independent invariant, verify it, and continue the inventory.
        """
    )

    workflow: str = textwrap.dedent(
        """
        If `src/lib.c` is too large for your context, never sample it or read it front to back. Locate
        what you need with pinpointed searches: `rg` or `grep -n` for text such as strings, dispatch
        tables, call sites, enums, and macros, and `ast-grep` for structure such as definitions,
        signatures, and call patterns. Turn each match's line number into a bounded read with
        `sed -n 'START,ENDp'`, widening the range only until the definition is complete. Record the
        symbols and line ranges you resolve so you can return to them without rescanning.

        Build a complete inventory and track every item as untested, basic, deep, or temporarily blocked.
        Complete a test generation pass across the entire program before spending many attempts on one branch,
        then return item by item.

        **Never** give up early, test only a convenient subset, or leave a
        large subsystem unexamined. Source size, complexity, slow coverage growth, and repeated failures
        never justify narrowing the scope.

        Call a branch unreachable only after reading its guard, tracing its public input path, and trying
        the simplest safe input that satisfies it.

        # Workflow
        Read `{instrument_path}` once before editing so you understand every check, log, and reported
        result; never edit it. Run tests only through:
        ```bash
        {instrument_path} collect
        {instrument_path} io
        ```
        A successful `collect` prints only JSON values that changed in that run; all case files remain
        under `{json_dir}` if you need to inspect one again.

        On failure, read the complete relevant
        logs under `{sanitizer_dir}` and `{coverage_dir}`, classify the cause as UB, a wrong expectation,
        or a hang, and fix the input or test. Count one test failing in several builds as one problem.
        Never set sanitizer environment variables or assert sanitizer failure as expected.
        Keep each test's body, inputs, calls, and expectations identical in every build;
        never branch on Cargo features or sanitizer state.

        Work in batches of generated tests:
        1. Append collection cases to `{collect_path}` and run `{instrument_path} collect`.
        2. Mirror them in `{test_path}` and run `{instrument_path} io`.

        Run both steps before adding another batch. In `{collect_path}`, serialize outputs for assertion
        tests; do not assert them. Append rather than rewriting the file or accumulating an unverified
        batch. Target one public operation or mode that covers as many code branches as possible,
        with only necessary setup and cleanup.

        Add the minimum state needed for behavior direct calls cannot reach;
        then add interactions only when one call must create state for another.
        Help, usage, version, and immediate validation failures do not count as basic
        coverage. Give every test fresh state and one named behavior.
        Prefer one call over a short chain and a short chain over a long one.

        ## Test readability
        **Always** prefer more shorter and simpler tests over fewer, longer, and more complex ones,
        and prefer tests that use short, self-contained files over files containing an amalgamation of
        tested behaviors.
        """
    )

    collection: str = textwrap.dedent(
        """
        In `{collect_path}`, append below the marker and keep `use {lib_name}::*;` exactly as written. Call
        public C functions through FFI. Record return values, output parameters, and mutations. Allocate
        pointed-to data locally by idiomatically populating Rust `struct`s where needed.
        Use NUL-terminated C strings. Do not hard-code addresses.

        Give every file test its own `sandbox("<test-name>")` and keep all paths inside it. Use `snapshot`
        when the complete tree matters; otherwise record the relevant relative paths, existence or removal,
        and stable bytes directly. Do not substitute hashes or sizes for contents. Finish each test with
        one `save_case("<test-name>", &case)` containing every input and observed output.
        """
    )

    binary_collection: str = textwrap.dedent(
        """
        Give every test that calls `main` its own `sandbox("<test-name>")` and invoke it only with
        `run(&dir, ...)`. The helper uses that sandbox for both the working directory and `HOME` and
        records its file effects. Keep every path inside the sandbox and preserve the helper's normalized
        `<program>` placeholder.

        Record every program invocation in order. When file contents matter, read the relevant sandbox
        files after the call and serialize that observation alongside the calls.
        """
    )

    assertion: str = textwrap.dedent(
        """
        In `{test_path}`, create exactly one pure assertion test per collection test. Hard-code expected
        values as Rust literals; never read `{json_dir}` or `{collect_path}` or use `serde` or `serde_json`.
        Replay calls in order and assert every return value, output, mutation, and file effect after each
        step. Assert unrelated values separately and do not omit or weaken an assertion to make a test pass.

        Append below the marker and keep the statement `use {lib_name}::*;` **exactly** as written.
        Reach C only through imported library items; never add raw `extern` declarations.
        Compare integers and booleans exactly, floats with a small relative epsilon,
        and pointed-to values rather than addresses.

        Give file tests their own sandbox. Assert a complete `snapshot` when the whole tree is behaviorally
        relevant; otherwise assert relevant relative paths, existence or removal, and stable bytes directly.
        Assert in-place state after every call in a chain.
        """
    )

    binary_assertion: str = textwrap.dedent(
        """
        After every `main` call, assert stdout, stderr, exit code, changed and removed paths,
        and no disk change where none was recorded.
        """
    )

    completion_gated: str = textwrap.dedent(
        """
        Before the explicit `Only N steps left` warning, you can never call `finish`.
        Difficulty, slow progress, and the number of passing tests do not change this gate.

        Once the warning appears, make `{test_path}` mirror `{collect_path}` exactly,
        run `{instrument_path} io`, and call `finish` with a brief report.
        """
    )

    completion_early: str = textwrap.dedent(
        """
        You may call `finish` only once the inventory is exhausted: every item is tested and verified,
        the coverage target is met, and every branch you left uncovered has been proven unreachable by
        reading its guard and tracing its public input path. Difficulty, slow progress, a long
        trajectory, repeated failures, and the number of passing tests never justify finishing; keep
        working item by item while any untested item or untried input remains.

        Before finishing, make `{test_path}` mirror `{collect_path}` exactly,
        run `{instrument_path} io`, and call `finish` with a brief report.
        If the `Only N steps left` warning arrives first, wrap up as instructed and finish then.
        """
    )

    completion: str = textwrap.dedent(
        """
        # Completion
        A passing instrumentation or pristine verification run proves only that the current batch is
        valid; it never means the task is complete.
        {completion_gate}
        """
    ).replace(
        "{completion_gate}",
        (completion_early if TESTGEN_FINISH_EARLY else completion_gated).strip(),
    )

    library: str = (
        introduction
        + library_scope
        + portability
        + workflow
        + collection
        + assertion
        + completion
    )
    binary: str = (
        introduction
        + binary_scope
        + portability
        + workflow
        + collection
        + binary_collection
        + assertion
        + binary_assertion
        + completion
    )


cs = ConfigStore.instance()
cs.store(name="generate_io_tests", node=TestgenConfig)


def get_tools(work_dir: Path):
    useful_tools = UsefulTools(work_dir=str(work_dir))
    return [useful_tools.Bash, useful_tools.Read, useful_tools.Edit, useful_tools.Write]


def _wrapup_notice(collect_path: Path, test_path: Path) -> str:
    return (
        "Stop adding cases. Finish the current batch and make "
        f"`{test_path}` mirror `{collect_path}` exactly, with one assertion test per "
        "collected case and every output and file effect asserted. Never weaken, "
        "ignore, or change sanitizer behavior to pass. Remove any unfinished case "
        "from both files, run `./instrument.sh io`, and exit."
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
    agent = TestgenAgent(name="C code reviewer")

    # -sys crate setup
    sys_crate = Crate(cfg.manifest, vcs=cfg.vcs)  # type: ignore[reportArgumentType]
    sys_root = sys_crate.cargo_toml.parent
    sys_test_dir = sys_root / "tests"
    sys_test_dir.mkdir(parents=True, exist_ok=True)

    # Write instrumentation and test scripts
    template = "bin" if len(sys_crate.bin_targets) > 0 else "lib"
    lib_name = sys_crate.lib_name
    write_test_templates(sys_test_dir, template=template, lib_name=lib_name)

    # Isolate the crate
    work_dir = Path(tempfile.mkdtemp()) / sys_root.name
    shutil.copytree(
        sys_root,
        work_dir,
        dirs_exist_ok=True,
        ignore=lambda _directory, entries: {
            entry
            for entry in entries
            if entry.endswith(".log") or (entry.startswith(".") and entry != ".config")
        },
    )
    work_crate = Crate(work_dir / "Cargo.toml")

    # Copy the crate pre-agent for verification under a random package and target name
    fresh_copy_parent = Path(tempfile.mkdtemp())
    fresh_crate = get_fresh_copy(sys_crate, fresh_copy_parent)

    # Strip line directives
    for c_file in (work_dir / "src").glob("*.c"):
        strip_line_directives(c_file)

    # Build the task prompt
    task_description = (
        TestgenInstructions.binary if template == "bin" else TestgenInstructions.library
    ).strip()
    collect_path = (sys_test_dir / "collect.rs").relative_to(sys_root)
    test_path = (sys_test_dir / "io.rs").relative_to(sys_root)
    task_description = task_description.format(
        work_dir=work_dir,
        instrument_path="./instrument.sh",
        target_coverage=cfg.coverage,
        collect_path=collect_path,
        test_path=test_path,
        json_dir="json",
        sanitizer_dir="sanitizer_logs",
        coverage_dir="coverage_logs",
        lib_name=lib_name or "",
    )

    # Expose the direct pristine verifier only while the agent is running.
    agent.wrapup_steps = cfg.steps // 10
    agent.wrapup_notice = _wrapup_notice(collect_path, test_path)
    with verification_service(work_crate, fresh_crate):
        try:
            agent.run(
                model_name=cfg.model,
                prompt_template=task_description,
                max_steps=cfg.steps,
                max_budget=cfg.budget,
                tools=get_tools(work_dir),
                printer=printer,
                verbose=True,
            )
        except BudgetExceededError as error:
            message = f"Budget of ${cfg.budget} exhausted, keeping tests so far: {error}"
            logger.warning(message)
        except KISSError as error:
            message = (
                f"Agent {agent.name} completed {cfg.steps} steps without finishing: {error}"
            )
            logger.warning(message)

    # Copy the generated tests back
    work_test_dir = work_dir / "tests"
    sys_test_dir.mkdir(parents=True, exist_ok=True)
    for name in ("collect.rs", "io.rs"):
        src = work_test_dir / name
        if src.exists():
            shutil.copy2(src, sys_test_dir / name)

    sys_crate.vcs.init(force_init=True)
    sys_crate.vcs.add(sys_test_dir / "collect.rs", sys_test_dir / "io.rs")
    sys_crate.vcs.commit("Generated I/O equivalence tests")

    # Run post-agent verification on the copied tests
    finalize_tests(
        sys_crate, fresh_crate, ["collect", "io"], ["cc_asan", "cc_ubsan", "cc_coverage"]
    )
    shutil.rmtree(fresh_copy_parent)


@hydra.main(version_base=None, config_name="generate_io_tests")
def main(cfg: TestgenConfig) -> None:
    _main(cfg)


if __name__ == "__main__":
    main()
