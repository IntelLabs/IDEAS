#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

"""
The two terminal states of a translation run:

* exit after a whole translation with no irrecoverable error, and
* exit after an irrecoverable parse error (the C it never ported is stranded).
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import (
    BUILD_ENV,
    CRATE,
    Responder,
    build_ok,
    error_codex,
    field_value,
    get_crate_name,
    get_hybrid_lib,
    get_hybrid_name,
    get_shipped_name,
    run_translate,
    success_codex,
    wrapping_codex,
)

from ideas.hybrid import SAFE_CRATE


def _dynamic_exports(workspace: Path, lib_name: str) -> set[str]:
    cdylib = workspace / "target" / "debug" / f"lib{lib_name}.so"
    proc = subprocess.run(
        ["nm", "--dynamic", "--defined-only", str(cdylib)],
        capture_output=True,
        text=True,
        check=True,
    )
    return {parts[-1] for line in proc.stdout.splitlines() if len(parts := line.split()) > 1}


def _run_bin(workspace: Path, name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(workspace / "target" / "debug" / name)], capture_output=True, text=True
    )


@pytest.mark.parametrize("template", ["lib", "bin"])
def test_crates_buildable_on_exit_after_success(instrumented_workspace, monkeypatch, template):
    ws = instrumented_workspace(template)

    run_translate(monkeypatch, ws, template, success_codex())

    crate_name = get_crate_name(template)
    assert build_ok(ws, get_hybrid_name(template))
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

    run_translate(monkeypatch, ws, template, error_codex())

    crate_name = get_crate_name(template)
    assert build_ok(ws, get_hybrid_name(template))
    assert build_ok(ws, f"{crate_name}-rs")
    if template == "bin":
        # `main` was never translated, so the binary -rs ships refuses to run
        assert "was never translated" in _run_bin(ws, crate_name).stderr
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
        wrapping_codex(fail=frozenset({"sub", "main"})),
        wrapper_max_iters=1,
    )

    crate_name = get_crate_name(template)
    # `add` lost its body to its wrapper, `sub` to the partial-translation cleanup
    c_src = (ws / f"{crate_name}-sys" / "src" / "lib.c").read_text()
    assert "return a + b;" not in c_src
    assert "return a - b;" not in c_src

    # The one symbol Rust did take over keeps its wrapper
    hybrid = get_hybrid_name(template)
    assert 'export_name = "add"' in (ws / hybrid / "src" / "lib.rs").read_text()

    assert build_ok(ws, hybrid)
    if template == "bin":
        # C still owns the program logic but has no entrypoint left to reach it
        assert "was never translated" in _run_bin(ws, hybrid).stderr
    else:
        # `add` resolves to the Rust translation and `sub` resolves to nothing at all
        exports = _dynamic_exports(ws, get_hybrid_lib(template))
        assert "add" in exports
        assert "sub" not in exports


def test_ffi_crate_exports_c_interface_without_c_functions(instrumented_workspace, monkeypatch):
    ws = instrumented_workspace("lib")

    run_translate(monkeypatch, ws, "lib", wrapping_codex(), wrapper_max_iters=1)

    crate_name = get_crate_name("lib")
    assert build_ok(ws, f"{crate_name}-ffi")
    assert {"add", "sub"} <= _dynamic_exports(ws, CRATE)
    # -ffi links -sys only for the storage of C globals, so every export above is Rust
    archives = list((ws / "target" / "debug").glob(f"build/{crate_name}-sys-*/out/*.a"))
    assert archives
    for archive in archives:
        proc = subprocess.run(
            ["nm", "--defined-only", str(archive)], capture_output=True, text=True, check=True
        )
        functions = [
            p[-1]
            for line in proc.stdout.splitlines()
            if len(p := line.split()) == 3 and p[1] in "Tt"
        ]
        assert functions == [], f"{archive} defines C functions: {functions}"


_CALLBACK_H = """\
int add(int a, int b);
int sub(int a, int b);
int (*default_op(void))(int);
"""

_CALLBACK_C = """\
#include "mini.h"

static int twice(int x) {
    return 2 * x;
}

int add(int a, int b) {
    return a + b;
}

int sub(int a, int b) {
    return a - b;
}

int (*default_op(void))(int) {
    return twice;
}
"""

_CALLBACK_TRANSLATION = {
    "return 2 * x;": "pub fn twice(x: i32) -> i32 {\n    2 * x\n}\n",
    "return a + b;": "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n",
    "return a - b;": "pub fn sub(a: i32, b: i32) -> i32 {\n    a - b\n}\n",
    "return twice;": "pub fn default_op() -> fn(i32) -> i32 {\n    twice\n}\n",
}

_C_INT_FN = 'unsafe extern "C" fn(::std::os::raw::c_int) -> ::std::os::raw::c_int'

# The prompt asks for `rust_path`; `extern_decl` is the redeclaration it forbids
_INSTALL_TWICE = {
    "rust_path": f"let op: {_C_INT_FN} = twice;\n    Some(op)",
    "extern_decl": (
        'unsafe extern "C" {\n'
        "        fn twice(x: ::std::os::raw::c_int) -> ::std::os::raw::c_int;\n"
        "    }\n"
        f"    let op: {_C_INT_FN} = twice;\n    Some(op)"
    ),
}


def _callback_codex(reference: str) -> Responder:
    rs_crate = SAFE_CRATE
    bodies = {
        "twice": f"{rs_crate}::twice(x)",
        "add": f"{rs_crate}::add(a, b)",
        "sub": f"{rs_crate}::sub(a, b)",
        "default_op": _INSTALL_TWICE[reference],
    }

    def respond(prompt: str) -> dict[str, str]:
        if example := field_value(prompt, "example_wrapper").strip():
            match = re.search(r'export_name = "(\w+)"', example)
            assert match is not None, f"unrecognized wrapper template:\n{example}"
            wrapper = example.replace("unimplemented!()", bodies[match[1]])
            return {"reasoning": "trivial wrapper", "output": f"```rust\n{wrapper}\n```"}

        snippet = field_value(prompt, "snippet")
        [translation] = [t for key, t in _CALLBACK_TRANSLATION.items() if key in snippet]
        return {"reasoning": "trivial translation", "output": translation}

    return respond


@pytest.mark.parametrize(
    "reference",
    [
        "rust_path",
        pytest.param(
            "extern_decl",
            marks=pytest.mark.xfail(
                strict=True, reason="-ffi does not export the static wrapper it redeclares"
            ),
        ),
    ],
)
def test_ffi_crate_keeps_static_wrapper_installed_as_callback(
    instrumented_workspace, monkeypatch, reference
):
    ws = instrumented_workspace("lib", header=_CALLBACK_H, source=_CALLBACK_C)

    run_translate(monkeypatch, ws, "lib", _callback_codex(reference), wrapper_max_iters=1)

    assert build_ok(ws, f"{get_crate_name('lib')}-ffi")
    exports = _dynamic_exports(ws, CRATE)
    assert {"add", "sub", "default_op"} <= exports
    # `twice` is static in C, so it is not part of the library's interface
    assert "twice" not in exports
    # The callback `default_op` hands out must still resolve to the Rust `twice`
    cdylib = ws / "target" / "debug" / f"lib{CRATE}.so"
    script = (
        "import ctypes, sys\n"
        "lib = ctypes.CDLL(sys.argv[1])\n"
        "lib.default_op.restype = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)\n"
        "print(lib.default_op()(21))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(cdylib)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "42"


@pytest.mark.parametrize("template", ["lib", "bin"])
def test_bare_workspace_build_builds_shipped_crate(
    instrumented_workspace, monkeypatch, template
):
    ws = instrumented_workspace(template)

    run_translate(monkeypatch, ws, template, wrapping_codex(), wrapper_max_iters=1)

    # Evaluation runs a bare `cargo build`, which must not trip over the unlinkable -sys bin
    proc = subprocess.run(
        ["cargo", "build", "--quiet"], cwd=ws, env=BUILD_ENV, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    artifact = f"lib{CRATE}.so" if template == "lib" else CRATE
    assert (ws / "target" / "debug" / artifact).exists()


@pytest.mark.parametrize("template", ["lib", "bin"])
def test_hybrid_crate_removed_but_kept_in_history(
    instrumented_workspace, monkeypatch, template
):
    ws = instrumented_workspace(template)

    run_translate(monkeypatch, ws, template, wrapping_codex(), wrapper_max_iters=1, vcs="git")

    hybrid = get_hybrid_name(template)
    assert not (ws / hybrid).exists()
    assert hybrid not in (ws / "Cargo.toml").read_text()
    assert hybrid not in (ws / "Cargo.lock").read_text()
    # The final commit leaves nothing for a later cargo run to rewrite
    proc = subprocess.run(
        ["git", "-C", str(ws), "status", "--porcelain", "Cargo.lock"],
        capture_output=True,
        text=True,
    )
    assert proc.stdout == ""
    assert build_ok(ws, get_shipped_name(template))
    # The commit before the final one still has it
    proc = subprocess.run(
        ["git", "-C", str(ws), "show", f"HEAD~1:{hybrid}/Cargo.toml"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
