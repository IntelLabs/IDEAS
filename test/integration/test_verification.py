#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from conftest import REPO_ROOT, get_crate_name

import ideas.agents.verifiers as verifiers
from ideas.agents.utils import (
    finalize_tests,
    write_instrumentation_script,
    write_test_templates,
)
from ideas.agents.verifiers import (
    VERIFIER_SOCKET_ENV,
    VerificationError,
    get_fresh_copy,
    verification_service,
    verify_instrumentation,
    verify_tests_are_portable,
)
from ideas.tools import Crate

TESTS = ["collect", "io"]

_PASSING = """\
#[test]
fn passes() {
    assert_eq!(unsafe { add(1, 1) }, 2);
}
"""

_FAILING = """\
#[test]
fn fails() {
    assert_eq!(1 + 1, 3);
}
"""

_NON_PORTABLE = """\
#[test]
fn reads_agent_leftovers() {
    std::fs::read_to_string("agent_scratch.txt").unwrap();
}
"""

_WORKSPACE_SIDE_EFFECT = """\
#[test]
fn writes_outside_the_allowed_artifact_directories() {
    std::fs::write("agent-created.txt", "unexpected").unwrap();
}
"""

_SANITIZER_EXIT_BIN = """\
#include "mini.h"
#include <stdio.h>

int add(int a, int b) {
    return a + b;
}

int sub(int a, int b) {
    return a - b;
}

int main(void) {
    fputs("simulated sanitizer diagnostic\\n", stderr);
    return 86;
}
"""

_PRINT_PROGRAM_PATH_AND_HOME_BIN = """\
#include "mini.h"
#include <stdio.h>
#include <stdlib.h>

int add(int a, int b) {
    return a + b;
}

int sub(int a, int b) {
    return a - b;
}

int main(int argc, char **argv) {
    if (argc > 0) {
        puts(argv[0]);
    }
    puts(getenv("HOME"));
    return 0;
}
"""

_TEMPLATE_TESTS = {
    "bin": r"""#[test]
fn scaffold_builds() {
    let dir = sandbox("scaffold_builds");
    let call = run(&dir, &[], None);
    assert_eq!(call.stdout, format!("<program>\n{}\n", dir.display()));
    assert!(call.changed.is_empty());
    assert!(call.removed.is_empty());
}
""",
    "lib": r"""#[test]
fn scaffold_builds() {
    assert_eq!(unsafe { add(2, 3) }, 5);
    let dir = sandbox("scaffold_builds");
    std::fs::write(dir.join("output.txt"), b"output").unwrap();
    assert_eq!(std::fs::read(dir.join("output.txt")).unwrap(), b"output");
    assert_eq!(
        snapshot(&dir).get("output.txt"),
        Some(&Some(b"output".to_vec())),
    );
}
""",
}


@pytest.fixture
def sys_crate(instrumented_workspace, monkeypatch):
    monkeypatch.setenv("UV_PROJECT", str(REPO_ROOT))
    workspace = instrumented_workspace("lib")

    crate = Crate(workspace / f"{get_crate_name('lib')}-sys" / "Cargo.toml")
    lib_name = crate.lib_name
    assert lib_name is not None
    test_dir = crate.cargo_toml.parent / "tests"
    test_dir.mkdir(parents=True)
    for test in TESTS:
        (test_dir / f"{test}.rs").write_text(f"use {lib_name}::*;\n\n{_PASSING}")
    return crate


@pytest.fixture
def fresh_crate(sys_crate, tmp_path):
    return get_fresh_copy(sys_crate, tmp_path)


def _finalize(crate, fresh_crate):
    finalize_tests(
        crate,
        fresh_crate,
        TESTS,
        ["cc_asan", "cc_ubsan", "cc_coverage"],
    )


def _list_tests(crate, test):
    return crate.cargo_test_list(test)


def _tests_pass(crate, test):
    return crate.cargo_test(test)[0]


def test_fresh_copy_has_a_random_package_and_target_name(tmp_path):
    source = Crate(tmp_path / "source" / "example-sys" / "Cargo.toml", template="lib")
    fresh = get_fresh_copy(source, tmp_path / "fresh")

    assert fresh.name != source.name
    assert fresh.lib_name != source.lib_name
    assert fresh.cargo_toml.parent.name == fresh.name
    assert fresh.workspace_root == fresh.cargo_toml.parent


def test_reproducible_suite_verifies(sys_crate, fresh_crate):
    assert sys_crate.name != fresh_crate.name
    assert fresh_crate.workspace_root.name != sys_crate.workspace_root.name
    verify_tests_are_portable(sys_crate, fresh_crate, TESTS)
    for test in TESTS:
        source = (fresh_crate.cargo_toml.parent / "tests" / f"{test}.rs").read_text()
        assert f"use {fresh_crate.lib_name}::*;" in source


def test_tests_relying_on_agent_leftovers_do_not_verify(sys_crate, fresh_crate):
    crate_dir = sys_crate.cargo_toml.parent
    (crate_dir / "tests" / "io.rs").write_text(_NON_PORTABLE)

    # The pipeline never generates this, so verification must fail
    (crate_dir / "agent_scratch.txt").write_text("scratch\n")
    assert sys_crate.cargo_test("io")[0]

    with pytest.raises(VerificationError):
        verify_tests_are_portable(sys_crate, fresh_crate, TESTS)


def test_edited_instrumentation_script_does_not_verify(sys_crate, fresh_crate):
    script = sys_crate.cargo_toml.parent / "instrument.sh"
    script.chmod(0o755)
    script.write_text(script.read_text() + "# the agent was here\n")

    with pytest.raises(VerificationError, match="modified and has been reverted"):
        verify_tests_are_portable(sys_crate, fresh_crate, TESTS)
    assert script.read_bytes() == (fresh_crate.cargo_toml.parent / "instrument.sh").read_bytes()
    assert (
        script.stat().st_mode
        == (fresh_crate.cargo_toml.parent / "instrument.sh").stat().st_mode
    )


def test_instrumentation_invokes_portability_verification(sys_crate, fresh_crate):
    crate_dir = sys_crate.cargo_toml.parent
    (crate_dir / "tests" / "io.rs").write_text(_NON_PORTABLE)
    (crate_dir / "agent_scratch.txt").write_text("scratch\n")

    with verification_service(sys_crate, fresh_crate):
        result = subprocess.run(
            ["bash", "instrument.sh", "io"],
            cwd=crate_dir,
            capture_output=True,
            text=True,
        )

    assert result.returncode != 0
    assert "not portable to a fresh crate copy" in result.stderr


def test_instrumentation_verification_compares_the_pristine_workspace(sys_crate, fresh_crate):
    workspace = fresh_crate.workspace_root
    manifest = fresh_crate.cargo_toml.relative_to(workspace)
    verify_instrumentation(sys_crate.cargo_toml, workspace, manifest, TESTS)

    (sys_crate.cargo_toml.parent / "tests" / "io.rs").write_text(_WORKSPACE_SIDE_EFFECT)
    with pytest.raises(VerificationError, match="agent-created.txt"):
        verify_instrumentation(sys_crate.cargo_toml, workspace, manifest, ["io"])


def test_final_verification_restores_latest_successful_pair(
    sys_crate, fresh_crate, monkeypatch
):
    test_dir = sys_crate.cargo_toml.parent / "tests"

    def verify(candidate_manifest, _workspace, _fresh_manifest, tests):
        for test in tests:
            if (candidate_manifest.parent / "tests" / f"{test}.rs").read_text() == "invalid\n":
                raise VerificationError("invalid test")

    monkeypatch.setattr(verifiers, "verify_instrumentation", verify)
    with verification_service(sys_crate, fresh_crate):
        socket_path = os.environ[VERIFIER_SOCKET_ENV]
        for test in TESTS:
            assert verifiers.run_cli([socket_path, test]) == (0, None)

        latest_collect = (test_dir / "collect.rs").read_text() + "// latest collect\n"
        (test_dir / "collect.rs").write_text(latest_collect)
        assert verifiers.run_cli([socket_path, "collect"]) == (0, None)
        verified_io = (test_dir / "io.rs").read_text()

        for test in TESTS:
            (test_dir / f"{test}.rs").write_text("invalid\n")

    assert (test_dir / "collect.rs").read_text() == latest_collect
    assert (test_dir / "io.rs").read_text() == verified_io


def test_successful_final_verification_keeps_unverified_tail(
    sys_crate, fresh_crate, monkeypatch
):
    monkeypatch.setattr(verifiers, "verify_instrumentation", lambda *_args: None)
    io_path = sys_crate.cargo_toml.parent / "tests" / "io.rs"

    with verification_service(sys_crate, fresh_crate):
        socket_path = os.environ[VERIFIER_SOCKET_ENV]
        for test in TESTS:
            assert verifiers.run_cli([socket_path, test]) == (0, None)
        final_contents = io_path.read_text() + "// valid unverified tail\n"
        io_path.write_text(final_contents)

    assert io_path.read_text() == final_contents


def test_failing_tests_are_dropped(sys_crate, fresh_crate):
    test_path = sys_crate.cargo_toml.parent / "tests" / "io.rs"
    test_path.write_text(test_path.read_text() + _FAILING)

    _finalize(sys_crate, fresh_crate)

    assert _list_tests(sys_crate, "io") == ["passes"]
    assert _tests_pass(sys_crate, "io")


def test_suite_that_fails_falls_back_to_placeholders(sys_crate, fresh_crate):
    (sys_crate.cargo_toml.parent / "tests" / "io.rs").write_text(_FAILING)

    _finalize(sys_crate, fresh_crate)

    for test in TESTS:
        assert _list_tests(sys_crate, test)
        assert _tests_pass(sys_crate, test)
    assert "fails" not in _list_tests(sys_crate, "io")


def test_suite_that_does_not_compile_falls_back_to_placeholders(sys_crate, fresh_crate):
    (sys_crate.cargo_toml.parent / "tests" / "io.rs").write_text("this is not Rust")

    _finalize(sys_crate, fresh_crate)

    for test in TESTS:
        assert _list_tests(sys_crate, test)
        assert _tests_pass(sys_crate, test)


def test_missing_test_file_falls_back_to_placeholders(sys_crate, fresh_crate):
    test_path = sys_crate.cargo_toml.parent / "tests" / "collect.rs"
    test_path.unlink()

    _finalize(sys_crate, fresh_crate)

    assert test_path.exists()
    assert _tests_pass(sys_crate, "collect")


def test_pipeline_cannot_reproduce_falls_back_to_placeholders(sys_crate, fresh_crate):
    crate_dir = sys_crate.cargo_toml.parent
    (crate_dir / "tests" / "io.rs").write_text(_NON_PORTABLE)
    (crate_dir / "agent_scratch.txt").write_text("scratch\n")

    _finalize(sys_crate, fresh_crate)

    assert "reads_agent_leftovers" not in _list_tests(sys_crate, "io")
    for test in TESTS:
        assert _tests_pass(sys_crate, test)


@pytest.mark.parametrize("template", ["lib", "bin"])
def test_collect_reports_only_changed_json(instrumented_workspace, template):
    ws = instrumented_workspace(template)
    crate = Crate(ws / f"{get_crate_name(template)}-sys" / "Cargo.toml")
    test_path = crate.cargo_toml.parent / "tests" / "collect.rs"
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(
        textwrap.dedent(
            """
            #[test]
            fn always_pass() {
                assert_eq!(1 + 1, 2);
                std::fs::create_dir_all("json").unwrap();
                std::fs::write("json/stable.json", r#"{"value":1}"#).unwrap();
                std::fs::write("json/new.json", r#"{"value":2}"#).unwrap();
            }
            """
        ).strip()
    )
    json_dir = crate.cargo_toml.parent / "json"
    json_dir.mkdir()
    (json_dir / "stable.json").write_text('{"value":1}')
    script = write_instrumentation_script(
        crate.cargo_toml.parent / "instrument.sh", features=["cc_asan", "cc_ubsan"]
    )

    result = subprocess.run(
        ["bash", script.name, "collect"],
        cwd=script.parent,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Coverage summary for collect:" in result.stdout
    assert "JSON values changed by this collect run:" in result.stdout
    assert "--- json/new.json" in result.stdout
    assert "--- json/stable.json" not in result.stdout
    assert (json_dir / "stable.json").read_text() == '{"value":1}'
    assert re.fullmatch(
        r"Branch coverage: (?:\d+(?:\.\d+)?%|-)", result.stdout.rstrip().splitlines()[-1]
    )


def test_failed_collect_preserves_previous_json(instrumented_workspace):
    ws = instrumented_workspace("lib")
    crate = Crate(ws / f"{get_crate_name('lib')}-sys" / "Cargo.toml")
    test_path = crate.cargo_toml.parent / "tests" / "collect.rs"
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text("this is not Rust")

    json_dir = crate.cargo_toml.parent / "json"
    json_dir.mkdir()
    expected = '{"still":"valid"}'
    (json_dir / "previous.json").write_text(expected)
    script = write_instrumentation_script(
        crate.cargo_toml.parent / "instrument.sh", features=["cc_asan", "cc_ubsan"]
    )

    result = subprocess.run(
        ["bash", script.name, "collect"],
        cwd=script.parent,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert (json_dir / "previous.json").read_text() == expected


def test_standalone_template_crate_builds(tmp_path):
    source = Path(__file__).parents[2] / "src" / "ideas" / "agents" / "templates"
    destination = tmp_path / "templates"
    shutil.copytree(source, destination)

    result = subprocess.run(
        ["cargo", "test", "--quiet", "--manifest-path", destination / "Cargo.toml"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("template", ["lib", "bin"])
def test_generated_test_templates_build(instrumented_workspace, template):
    source = _PRINT_PROGRAM_PATH_AND_HOME_BIN if template == "bin" else None
    ws = instrumented_workspace(template, source=source)
    crate = Crate(ws / f"{get_crate_name(template)}-sys" / "Cargo.toml")
    test_dir = crate.cargo_toml.parent / "tests"
    test_dir.mkdir(parents=True, exist_ok=True)

    write_test_templates(test_dir, template, crate.lib_name)
    for test in TESTS:
        path = test_dir / f"{test}.rs"
        contents = path.read_text()
        assert "__LIB_NAME__" not in contents
        path.write_text(contents + "\n" + _TEMPLATE_TESTS[template])

    for test in TESTS:
        passed, output, error, _ = crate.cargo_test(test)
        assert passed, output + error


def test_child_sanitizer_failure_is_reported(instrumented_workspace):
    ws = instrumented_workspace("bin", source=_SANITIZER_EXIT_BIN)
    crate = Crate(ws / f"{get_crate_name('bin')}-sys" / "Cargo.toml")
    test_path = crate.cargo_toml.parent / "tests" / "io.rs"
    write_test_templates(test_path.parent, template="bin", lib_name=crate.lib_name)
    test_path.write_text(
        test_path.read_text()
        + textwrap.dedent(
            """

            #[test]
            fn child_sanitizer_failure_is_reported() {
                let dir = sandbox("child_sanitizer_failure_is_reported");
                let _ = run(&dir, &[], None);
            }
            """
        )
    )

    result = subprocess.run(
        ["bash", "instrument.sh", "io"],
        cwd=crate.cargo_toml.parent,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "simulated sanitizer diagnostic" in result.stdout
    sanitizer_log = (crate.cargo_toml.parent / "sanitizer_logs" / "cc_asan.log").read_text()
    assert "simulated sanitizer diagnostic" in sanitizer_log
