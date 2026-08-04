#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

"""
The two terminal states of a translation run:

* exit after a whole translation with no irrecoverable error, and
* exit after an irrecoverable DSPy error (the hybrid crate is stubbed out).
"""

import pytest
from conftest import build_ok, error_lm, get_crate_name, run_translate, success_lm


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
    if template == "bin":
        # Any error leaves the hybrid crate without a buildable binary
        # because the app overwrites `main.rs` with an empty file
        assert not build_ok(ws, crate_name)
    else:
        assert build_ok(ws, crate_name)
    assert build_ok(ws, f"{crate_name}-rs")
    assert build_ok(ws, f"{crate_name}-sys")
    assert build_ok(ws, f"{crate_name}-sys", "--no-default-features", "--features", "cc_ubsan")
    assert build_ok(ws, f"{crate_name}-sys", "--no-default-features", "--features", "cc_asan")
    assert build_ok(
        ws, f"{crate_name}-sys", "--no-default-features", "--features", "cc_coverage"
    )
