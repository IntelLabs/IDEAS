#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import subprocess
import textwrap

import pytest
from conftest import get_crate_name, build_ok

from ideas.tools import Crate
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
def test_instrumentation_runs(instrumented_workspace, template):
    ws = instrumented_workspace(template)
    sys_crate = Crate(ws / f"{get_crate_name(template)}-sys" / "Cargo.toml")

    test_collect = sys_crate.cargo_toml.parent / "tests" / "test_collect.rs"
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
        ["bash", script.name],
        cwd=script.parent,
        capture_output=True,
        text=True,
    )
    # The instrumentation script should run successfully
    assert proc.returncode == 0, (
        f"instrumentation script failed (rc={proc.returncode}):\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
