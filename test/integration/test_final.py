#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

"""
The two terminal states of a translation run:

* exit after a whole translation with no irrecoverable error, and
* exit after an irrecoverable DSPy error (the C it never ported is stranded).
"""

import subprocess
from pathlib import Path

import pytest
from conftest import (
    build_ok,
    error_lm,
    get_crate_name,
    run_translate,
    success_lm,
    wrapping_lm,
)


def _dynamic_exports(workspace: Path, crate_name: str) -> set[str]:
    cdylib = workspace / "target" / "debug" / f"{crate_name}.so"
    proc = subprocess.run(
        ["nm", "--dynamic", "--defined-only", str(cdylib)],
        capture_output=True,
        text=True,
        check=True,
    )
    return {parts[-1] for line in proc.stdout.splitlines() if len(parts := line.split()) > 1}


def _run_hybrid_bin(workspace: Path, crate_name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(workspace / "target" / "debug" / crate_name)], capture_output=True, text=True
    )


@pytest.mark.parametrize("template", ["lib", "bin"])
def test_crates_buildable_on_exit_after_success(instrumented_workspace, monkeypatch, template):
    ws = instrumented_workspace(template)

    run_translate(monkeypatch, ws, template, success_lm())

    crate_name = get_crate_name(template)
    assert build_ok(ws, crate_name)
    assert build_ok(ws, f"{crate_name}-rs")
    if template == "bin":
        # Translating a binary migrates the C `main` to Rust
        # so the -sys reference binary (`#![no_main]`) can no longer link
        assert not build_ok(ws, f"{crate_name}-sys")
    else:
        assert build_ok(ws, f"{crate_name}-sys")
        assert build_ok(
            ws, f"{crate_name}-sys", "--no-default-features", "--features", "cc_ubsan"
        )
        assert build_ok(
            ws, f"{crate_name}-sys", "--no-default-features", "--features", "cc_asan"
        )
        assert build_ok(
            ws, f"{crate_name}-sys", "--no-default-features", "--features", "cc_coverage"
        )


@pytest.mark.parametrize("template", ["lib", "bin"])
def test_crates_buildable_on_exit_after_error(instrumented_workspace, monkeypatch, template):
    ws = instrumented_workspace(template)

    run_translate(monkeypatch, ws, template, error_lm())

    crate_name = get_crate_name(template)
    assert build_ok(ws, crate_name)
    assert build_ok(ws, f"{crate_name}-rs")
    if template == "bin":
        # C no longer has an entrypoint to reach, so the binary refuses to run
        assert "was never translated" in _run_hybrid_bin(ws, crate_name).stderr
        # and the -sys reference binary (`#![no_main]`) can no longer link
        assert not build_ok(ws, f"{crate_name}-sys")
    else:
        assert build_ok(ws, f"{crate_name}-sys")
        assert build_ok(
            ws, f"{crate_name}-sys", "--no-default-features", "--features", "cc_ubsan"
        )
        assert build_ok(
            ws, f"{crate_name}-sys", "--no-default-features", "--features", "cc_asan"
        )
        assert build_ok(
            ws, f"{crate_name}-sys", "--no-default-features", "--features", "cc_coverage"
        )


@pytest.mark.parametrize("template", ["lib", "bin"])
def test_no_c_functions_linkable_after_partial_translation(
    instrumented_workspace, monkeypatch, template
):
    ws = instrumented_workspace(template)

    # `add` translates and is wrapped, `sub` raises and ends the run
    run_translate(
        monkeypatch,
        ws,
        template,
        wrapping_lm(template, fail=frozenset({"sub", "main"})),
        wrapper_max_iters=1,
    )

    crate_name = get_crate_name(template)
    # `add` lost its body to its wrapper, `sub` to the partial-translation cleanup
    c_src = (ws / f"{crate_name}-sys" / "src" / "lib.c").read_text()
    assert "return a + b;" not in c_src
    assert "return a - b;" not in c_src

    # The one symbol Rust did take over keeps its wrapper
    assert 'export_name = "add"' in (ws / crate_name / "src" / "lib.rs").read_text()

    assert build_ok(ws, crate_name)
    if template == "bin":
        # C still owns the program logic but has no entrypoint left to reach it
        assert "was never translated" in _run_hybrid_bin(ws, crate_name).stderr
    else:
        # `add` resolves to the Rust translation and `sub` resolves to nothing at all
        exports = _dynamic_exports(ws, crate_name)
        assert "add" in exports
        assert "sub" not in exports
