#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import subprocess
import textwrap

import pytest
from conftest import get_crate_name, build_ok, make_config

from ideas.tools import Crate
from ideas.translate import _init_crates
from ideas.agents.utils import write_instrumentation_script


@pytest.mark.parametrize("template", ["lib", "bin"])
def test_sys_crate_buildable_after_init(instrumented_workspace, template):
    ws = instrumented_workspace(template)

    crate_name = get_crate_name(template)
    assert build_ok(ws, f"{crate_name}-sys")
    assert build_ok(ws, f"{crate_name}-sys", "--no-default-features", "--features", "cc_ubsan")
    assert build_ok(ws, f"{crate_name}-sys", "--no-default-features", "--features", "cc_asan")
    assert build_ok(
        ws, f"{crate_name}-sys", "--no-default-features", "--features", "cc_coverage"
    )


def test_bin_stripped_sys_crate_links_with_sanitizer(instrumented_workspace):
    ws = instrumented_workspace("bin")
    crate_name = get_crate_name("bin")
    src = (ws / f"{crate_name}-sys" / "Cargo.toml").parent / "src"

    # Strip out the library target completely and link to C `main`
    (src / "lib.rs").unlink()
    (src / "main.rs").write_text("#![no_main]\n")

    assert build_ok(ws, f"{crate_name}-sys")
    assert build_ok(ws, f"{crate_name}-sys", "--no-default-features", "--features", "cc_ubsan")
    assert build_ok(ws, f"{crate_name}-sys", "--no-default-features", "--features", "cc_asan")
    assert build_ok(
        ws, f"{crate_name}-sys", "--no-default-features", "--features", "cc_coverage"
    )


@pytest.mark.parametrize("template", ["lib", "bin"])
def test_c_main_weak_only_for_bin(instrumented_workspace, template):
    ws = instrumented_workspace(template)

    c_src = (ws / f"{get_crate_name(template)}-sys" / "src" / "lib.c").read_text()

    if template == "bin":
        assert "__attribute__((weak)) int main(" in c_src
    else:
        assert "__attribute__((weak))" not in c_src


def test_ported_test_builds_when_binding_matches_crate_name(instrumented_workspace):
    ws = instrumented_workspace(
        "lib",
        header="typedef struct driver { int value; } driver;\nint driver_value(driver);\n",
        source='#include "mini.h"\nint driver_value(driver value) { return value.value; }\n',
    )
    cfg = make_config(ws, "lib")
    assert cfg.tests is not None
    cfg.tests.write_text(
        "use libdriver_sys::*;\n\n"
        "#[test]\n"
        "fn binding_can_match_crate_name() {\n"
        "    assert_ne!(std::mem::size_of::<driver>(), 0);\n"
        "}\n"
    )

    _init_crates(cfg)

    assert build_ok(ws, "libdriver", "--test", "smoke")


# Referencing a C symbol makes the linker pull in the object defining `main`, which must
# not outrank the libtest harness `main`
def _add_c_referencing_test(ws, crate_name):
    lib_rs = ws / f"{crate_name}-sys" / "src" / "lib.rs"
    lib_rs.write_text(
        lib_rs.read_text()
        + textwrap.dedent(
            """
            #[cfg(test)]
            mod tests {
                #[test]
                fn references_c() {
                    assert!(!(super::add as *const ()).is_null());
                }
            }
            """
        )
    )


def _cargo_test(ws, crate_name, *extra):
    cmd = [
        "cargo",
        "test",
        "--quiet",
        "--lib",
        "--manifest-path",
        str(ws / "Cargo.toml"),
        "-p",
        f"{crate_name}-sys",
        *extra,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def test_bin_sys_crate_lib_tests_link(instrumented_workspace):
    ws = instrumented_workspace("bin")
    crate_name = get_crate_name("bin")
    _add_c_referencing_test(ws, crate_name)

    result = _cargo_test(ws, crate_name, "--no-run")
    assert result.returncode == 0, result.stderr


def test_bin_sys_crate_lib_tests_are_discoverable(instrumented_workspace):
    ws = instrumented_workspace("bin")
    crate_name = get_crate_name("bin")
    _add_c_referencing_test(ws, crate_name)

    # `--list` runs the harness, so it fails if the C `main` took over the entry point
    result = _cargo_test(ws, crate_name, "--", "--list")
    assert result.returncode == 0, result.stderr
    assert "tests::references_c: test" in result.stdout, result.stdout


@pytest.mark.parametrize("template", ["lib", "bin"])
def test_instrumentation_runs(instrumented_workspace, template):
    ws = instrumented_workspace(template)
    sys_crate = Crate(ws / f"{get_crate_name(template)}-sys" / "Cargo.toml")

    test_collect = sys_crate.cargo_toml.parent / "tests" / "collect.rs"
    test_collect.parent.mkdir(parents=True, exist_ok=True)
    test_collect.write_text(
        textwrap.dedent(
            """
            #[test]
            fn always_pass() {
                assert_eq!(1 + 1, 2);
            }
            """
        ).strip()
    )

    script = write_instrumentation_script(
        sys_crate.cargo_toml.parent / "instrument.sh", features=["cc_asan", "cc_ubsan"]
    )

    proc = subprocess.run(
        ["bash", script.name, "collect"],
        cwd=script.parent,
        capture_output=True,
        text=True,
    )
    # The instrumentation script should run successfully
    assert proc.returncode == 0, (
        f"instrumentation script failed (rc={proc.returncode}):\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
