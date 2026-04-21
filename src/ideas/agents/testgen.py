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
import shutil
from pathlib import Path
from dataclasses import dataclass

import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig

from ideas.agents.printer import ConsoleTee, LoggingConsolePrinter
from ideas.tools import run_subprocess

from kiss.agents.sorcar.useful_tools import UsefulTools
from kiss.core.relentless_agent import RelentlessAgent

logger = logging.getLogger("ideas.agents.testgen")


@dataclass
class TestgenConfig:
    model: str = MISSING
    c_code: Path = MISSING
    project_name: str = MISSING
    test_vectors_out: Path = MISSING
    test_crate_out: Path = MISSING

    num_vectors: int = 3
    desired_symbols: int = 3

    def __post_init__(self):
        if not self.c_code.exists():
            raise ValueError(
                f"c_code must be a directory containing a CMake C project or a single C file, got: {self.c_code}"
            )


@dataclass
class TestgenInstructions:
    analyze_dir: str = textwrap.dedent(
        """
        ## Step 1 – Analyze the C project ##
        Carefully read and understand the C project rooted at `{c_proj_path}`.

        Inspect the CMakeLists.txt to learn:
        - the project / library name
        - all source files and include directories
        - any required link libraries (e.g. `-lm`)

        List **all** top-level (exported / non-static) library functions declared in the
        public header(s) under `{c_proj_path}/include`.
        Write only their function names (no declaration or body) to `{c_proj_path}/functions.lst`,
        newline separated.
        """
    )

    analyze_file: str = textwrap.dedent(
        """
        ## Step 1 – Analyze the standalone C file ##
        Carefully read and understand the single C source file at
        `{c_proj_path}/{c_filename}`.

        This is a **standalone** C file (no CMake project, no separate headers).
        All declarations and definitions live in this one file.

        List **all** non-static functions defined in the file.
        Write only their function names (no declaration or body) to `{c_proj_path}/functions.lst`,
        newline separated.
        """
    )

    analyze_select: str = textwrap.dedent(
        """
        From that list, select **up to {desired_symbols}** functions that are the best
        candidates for black-box testing.

        The selected functions **must** have all their dependencies defined in the project.
        If they reference functions that are **only declared**, they **cannot** be tested.

        Only select fewer than {desired_symbols} if there are not
        that many functions. Prefer functions that:
        - are **high-level entry points** (i.e. they orchestrate significant portions
          of the code's logic rather than being small utility helpers)
        - accept rich input (structs, arrays, multiple parameters) so that a single
          call exercises many internal code-paths
        - together give broad coverage of the public API
        Write only their function names (no declaration or body) to `{c_proj_path}/selected.lst`,
        newline separated.

        For each of the selected functions analyze: which parameters are **input-only**,
        which are **output-only** (written by the callee), and which are **in/out** to understand
        how to set up its test data and collect its outputs.
        """
    )

    build_rs: str = textwrap.dedent(
        """
        ## Step 2 – Create a Rust crate with a `build.rs` that compiles and links the C project ##
        Initialize a new Rust **library** crate at `{rs_crate_path}`:
        ```bash
        cargo init --lib --edition=2024 --vcs none --name=<c-project-name> {rs_crate_path}
        ```

        Add the `cc` build dependency and the following dev-dependencies to `Cargo.toml`:
        ```toml
        [build-dependencies]
        cc = "1.2.59"

        [dev-dependencies]
        serde = {{ version = "1", features = ["derive"] }}
        serde_json = "1"
        ```

        Write a `{rs_crate_path}/build.rs` that:
        1. Uses `cc::Build::new()` with `.compiler("clang")` to compile **all** C source files discovered in Step 1.
        2. Adds the correct include directories so the C headers are found.
        3. Uses `.warnings(false)` to suppress warnings.
        4. Uses `.std("c99")` to specify the C standard.
        5. Links any extra system libraries the C project requires (e.g. `println!("cargo::rustc-link-lib=m");`).
        """
    )

    bindgen_dir: str = textwrap.dedent(
        """
        ### Obtain the exact FFI API with `bindgen` ###
        Before populating the crate sources, use `bindgen` on the shell to generate the
        correct Rust FFI declarations for the selected functions from Step 1.

        Run a **separate** `bindgen` invocation for each function and **redirect each
        output directly** into its own binding module file:
        ```bash
        mkdir -p {rs_crate_path}/src/binding
        BINDGEN_EXTRA_CLANG_ARGS="-I<path-to-include-dir>" bindgen \
            --disable-header-comment --no-doc-comments --no-layout-tests \
            <c-header-file> \
            --allowlist-function <function_name> \
            > {rs_crate_path}/src/binding/<function_name>.rs
        ```

        Where you must properly identify:
        - `<path-to-include-dir>` – one or more `-I<dir>` arguments pointing to the
          C include directories discovered in Step 1.  If multiple header directories
          are needed, list them all as space-separated `-I<dir>` arguments inside
          `BINDGEN_EXTRA_CLANG_ARGS`.
        - `<c-header-file>` – the public header that declares the function.
        - `<function_name>` – the exact C function name (one per invocation).
        """
    )

    bindgen_file: str = textwrap.dedent(
        """
        ### Obtain the exact FFI API with `bindgen` ###
        Before populating the crate sources, use `bindgen` on the shell to generate the
        correct Rust FFI declarations for the selected functions from Step 1.

        Since this is a standalone C file with no separate headers, run `bindgen`
        directly on the source file.  Run a **separate** invocation for each function
        and redirect each output directly into its own binding module file:
        ```bash
        mkdir -p {rs_crate_path}/src/binding
        bindgen \
            --disable-header-comment --no-doc-comments --no-layout-tests \
            {c_proj_path}/{c_filename} \
            --allowlist-function <function_name> \
            > {rs_crate_path}/src/binding/<function_name>.rs
        ```

        Where `<function_name>` is the exact C function name (one per invocation).
        """
    )

    build_rs_librs: str = textwrap.dedent(
        """
        ### Critical: crate module layout ###
        The crate **must** use a modular layout that keeps each symbol's bindgen output
        in its own file.  Create the following structure:

        1. **`{rs_crate_path}/src/lib.rs`** – contains **only**:
           ```rust
           pub mod binding;
           ```

        2. **`{rs_crate_path}/src/binding.rs`** – contains one `pub mod <function_name>;`
           line for **each** selected function.  Example (if the selected functions are
           `foo` and `bar`):
           ```rust
           pub mod foo;
           pub mod bar;
           ```

        3. **`{rs_crate_path}/src/binding/<function_name>.rs`** – each file is the
           **exact, unmodified** output of the corresponding `bindgen` invocation from
           the previous step (already written there by the shell redirects above).
           Do **not** hand-edit these files.

        Build using `cargo build` to confirm the C code compiles and links.
        """
    )

    gen_data_collection_tests: str = textwrap.dedent(
        """
        ## Step 3 – Generate a data-collection test harness ##
        Create `{rs_crate_path}/tests/test_collect.rs`.

        ### 3a – FFI linkage (critical!) ###
        **Do NOT** declare `unsafe extern "C"` blocks in the test file.
        Instead, import the FFI functions through the binding modules using
        **absolute crate paths**.  The crate name is derived from the `name` field in
        `Cargo.toml` (with hyphens replaced by underscores).  Import like this:
        ```rust
        use <crate_name>::binding::<function_name>::<function_name>;
        ```
        This is **mandatory** because the C static library is attached to the
        library crate by `build.rs`.  If the test declares its own `extern "C"`
        block the linker will NOT find the C symbols and you will get
        `undefined symbol` errors.

        ### 3b – `#[repr(C)]` struct mirrors ###
        Import the `#[repr(C)]` struct types through the binding modules (they were
        generated by `bindgen` and placed in `src/binding/<function_name>.rs`):
        ```rust
        use <crate_name>::binding::<function_name>::<StructName>;
        ```
        Then add `#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]` to
        **local** wrapper types or re-definitions of those structs that you need for
        JSON serialization.  Because `serde` derives cannot be added to a type imported
        from another crate, you may need to define local copies of the structs in the
        test file with the exact same layout and field names, adding the serde derives.
        Make sure the field names, types, and order match the `bindgen`-generated
        definitions exactly.

        ### 3c – Helper: JSON state container ###
        Define a `#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]` struct
        called `LibState` whose field names match the parameters of the C function
        (one field per parameter, using the parameter name from the C header).
        **Important – pointer-typed fields**: any field in the C signature that is a
        pointer (e.g. `*mut T`, `*const T`) must NOT be hard-coded as a numeric
        address.  Instead:
        - Instantiate the pointed-to data structure as a local `let mut` variable
            with values populating all its fields.
        - Store the underlying data structure in the `LibState` struct.
        - Obtain a raw pointer to it (e.g. `&mut local_var as *mut T`) and pass that pointer
            to the function-under-test.
        This ensures the pointer is valid for the duration of the call and that the
        test does not rely on hard-coded addresses.
        If the function returns a value, add a `returns` field.
        Nested C structs must be represented by the mirrored Rust struct – never flatten
        them.

        Define a wrapper:
        ```rust
        #[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
        struct TestVector {{
            lib_state_in: LibState,
            lib_state_out: LibState,
        }}
        ```

        ### 3d – Data-collection test functions ###
        For each set of representative input values you choose (at least {num_vectors}
        distinct sets), write a `#[test]` function named `collect_vector_<N>` that:
        1. Constructs a `LibState` with the chosen inputs (and outputs / return field zeroed).
        2. Clones it into `lib_state_in`.
        3. Calls the C function through `unsafe`, using the symbol imported from the
           binding module, passing (and receiving) mutable references where needed.
        4. Captures the post-call state into `lib_state_out`.
        5. Asserts the call did not obviously fail (e.g. no null-pointer dereference – a
           simple `assert!` that pointers are non-null or that expected invariants hold).
        6. Serializes a `TestVector {{ lib_state_in, lib_state_out }}` to pretty JSON and
           prints it to stdout with:
           ```rust
           println!("{{}}",  serde_json::to_string_pretty(&vector).unwrap());
           ```

        The chosen inputs should exercise a variety of code-paths in the C function:
        - a zeroed / default / neutral input
        - a "normal" input with representative non-trivial values
        - an edge-case or boundary input

        Build and run tests in the crate with:
        ```bash
        cargo test --manifest-path {rs_crate_path}/Cargo.toml --quiet -- --nocapture
        ```
        Verify that all tests pass and JSON is printed.
        """
    )

    write_test_vectors: str = textwrap.dedent(
        """
        ## Step 4 – Save test vectors as JSON files ##
        Create the directory `{test_vectors_path}`.

        Run each data-collection test **individually** and capture its stdout.
        Write the JSON output of each `collect_vector_<N>` test to
        `{test_vectors_path}/<N>.json`, where `<N>` is the 1-based index.

        Use `uv` to extract the JSON reliably – do NOT rely on grep/sed:
        ```bash
        cargo test collect_vector_<N> -- --nocapture 2>/dev/null | \
            uv run python -c "
        import sys, json
        buf = sys.stdin.read()
        start = buf.index('{{')
        end = buf.rindex('}}') + 1
        obj = json.loads(buf[start:end])
        print(json.dumps(obj, indent=2))
        " > {test_vectors_path}/<N>.json
        ```

        Verify each file is valid JSON with the expected `lib_state_in` / `lib_state_out`
        structure by running `uv run python -m json.tool {test_vectors_path}/<N>.json`.
        """
    )

    write_assert_tests: str = textwrap.dedent(
        """
        ## Step 5 – Write assert-style Rust tests ##
        Create `{rs_crate_path}/tests/test_assert.rs`.

        Import the FFI symbols through the binding modules, the same way
        `test_collect.rs` does:
        ```rust
        use <crate_name>::binding::<function_name>::<function_name>;
        ```
        If the function call requires `#[repr(C)]` structs, import them too:
        ```rust
        use <crate_name>::binding::<function_name>::<StructName>;
        ```

        **Important**: `test_assert.rs` must **not** depend on `serde` or `serde_json`.
        Because the crate's types are exact `bindgen` output (no serde derives), the
        assert tests reconstruct all values as **plain Rust literals** taken from the
        JSON files saved in Step 4.  Do **not** `#[derive(Serialize, Deserialize)]` on
        any type in this file and do **not** add `use serde*` or `use serde_json*`.

        For **each** JSON test vector saved in Step 4, write a `#[test]` function named
        `test_vector_<N>` that:
        1. Reconstructs the `lib_state_in` values from the JSON file as Rust literals.
        2. Calls the C function through `unsafe` using the imported symbol.
        3. Asserts **every** field of the output state matches `lib_state_out` from the
           JSON file.
           - For floating-point fields use an epsilon comparison:
             ```rust
             assert!((actual - expected).abs() / expected.abs() < 1e-3,
                     "field `<name>`: expected {{expected}}, got {{actual}}");
             ```
           - For integer / bool fields use `assert_eq!`.
           - For pointer-typed output fields, dereference the pointer (inside `unsafe`)
             and compare the pointed-to value rather than the pointer address itself.

        Once done, run:
        ```bash
        cargo test --manifest-path {rs_crate_path}/Cargo.toml --quiet --test test_assert
        ```
        All tests **must** pass.
        """
    )

    deny_dependencies: str = textwrap.dedent(
        """
        ## External dependencies ##
        Apart from `cc` (build-dependency), `serde`, and `serde_json` (dev-dependencies),
        do not add any other dependencies to the Cargo.toml file.
        """
    )

    simple_exit: str = textwrap.dedent(
        """
        Once all assert tests pass and JSON files are written, finish the task and exit.
        Do not over-verify or generate extensive reports.
        """
    )

    @classmethod
    def dir_task_description(cls) -> str:
        return (
            cls.analyze_dir
            + cls.analyze_select
            + cls.build_rs
            + cls.bindgen_dir
            + cls.build_rs_librs
            + cls.gen_data_collection_tests
            + cls.write_test_vectors
            + cls.write_assert_tests
            + cls.deny_dependencies
            + cls.simple_exit
        )

    @classmethod
    def file_task_description(cls) -> str:
        return (
            cls.analyze_file
            + cls.analyze_select
            + cls.build_rs
            + cls.bindgen_file
            + cls.build_rs_librs
            + cls.gen_data_collection_tests
            + cls.write_test_vectors
            + cls.write_assert_tests
            + cls.deny_dependencies
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
    logger.info(f"Saving results to {output_dir}")

    # Separately log the complete trajectory
    logger_trajectory = logging.getLogger("ideas.testgen.trajectory")
    logger_trajectory.propagate = False
    fh = logging.FileHandler(output_dir / "testgen_trajectory.log")
    fh.setFormatter(ConsoleTee.StripANSIFormatter("%(asctime)s %(message)s"))
    logger_trajectory.addHandler(fh)
    # Simultaneous print and log to file
    printer = LoggingConsolePrinter(logger=logger_trajectory)
    agent = RelentlessAgent(name="C library test vector generator")

    project_name = cfg.project_name
    work_dir = Path(tempfile.mkdtemp()) / project_name
    os.makedirs(work_dir)

    # Copy the C project into the working directory
    c_proj_path = work_dir / "test_case"
    is_single_file = Path(cfg.c_code).is_file()
    if is_single_file:
        # Coherent /tmp and on-disk paths
        c_proj_path = work_dir / cfg.c_code.parent
        os.makedirs(c_proj_path)
        shutil.copy(cfg.c_code, c_proj_path / cfg.c_code.name)
    else:
        shutil.copytree(cfg.c_code, c_proj_path, dirs_exist_ok=True)

    # Paths the agent will populate
    rs_crate_path = work_dir / (cfg.test_crate_out if is_single_file else "testgen_crate")
    test_vectors_path = work_dir / "test_vectors"

    # Build the task prompt
    task_description = (
        TestgenInstructions.file_task_description()
        if is_single_file
        else TestgenInstructions.dir_task_description()
    )
    arguments = {
        "c_proj_path": c_proj_path.relative_to(work_dir),
        "rs_crate_path": rs_crate_path.relative_to(work_dir),
        "test_vectors_path": test_vectors_path.relative_to(work_dir),
        "num_vectors": cfg.num_vectors,
        "desired_symbols": cfg.desired_symbols,
    }
    if is_single_file:
        arguments["c_filename"] = cfg.c_code.name
    task_description = task_description.format(**arguments)

    # Run agent in the work directory
    original_dir = os.getcwd()
    os.chdir(work_dir)

    agent.run(
        model_name=cfg.model,
        system_instructions="",
        prompt_template=task_description,
        max_steps=100,
        max_budget=4,
        max_sub_sessions=1,
        work_dir=str(work_dir),
        tools=get_tools(),
        printer=printer,
        verbose=True,
    )
    # Verify that assertion tests pass
    cargo_toml = work_dir / cfg.test_crate_out / "Cargo.toml"
    ok, output, error, returncode = run_subprocess(
        ["cargo", "test", "--manifest-path", str(cargo_toml), "--test", "test_assert"],
        timeout=60,
    )
    if not ok:
        raise RuntimeError(
            f"Assert tests failed for target {project_name}: {error}! Tests will not be used during hybrid build!"
        )
    os.chdir(original_dir)

    # Copy test vectors
    shutil.copytree(test_vectors_path, cfg.test_vectors_out, dirs_exist_ok=True)
    # Copy test crate
    shutil.copytree(rs_crate_path, cfg.test_crate_out, dirs_exist_ok=True)
    # Copy C analysis results
    shutil.copy(c_proj_path / "functions.lst", cfg.test_crate_out / "functions.lst")
    shutil.copy(c_proj_path / "selected.lst", cfg.test_crate_out / "selected.lst")


if __name__ == "__main__":
    main()
