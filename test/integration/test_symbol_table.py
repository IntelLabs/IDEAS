#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

"""
Symbol ownership in the hybrid crate over the course of a translation.
"""

import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from conftest import CRATE, TEST_CRATES_DIR, field_value, get_crate_name, run_translate
from dspy.utils.dummies import DummyLM

from ideas import adapters
from ideas.oracle import TestOracle
from ideas.tools import Crate
import ideas.translate as translate_mod
from ideas.translate_recurrent import RecurrentTranslator

TEMPLATE = "lib"
ORDER = ("add", "scale", "square", "magnitude")
C_ARCHIVE = "library"
TESTS = "smoke"

# Refuse any linker oddity
STRICT_LINK_ARGS = (
    "-Wl,--no-undefined",
    "-Wl,--no-allow-shlib-undefined",
    "-Wl,--warn-common",
    "-Wl,--fatal-warnings",
)

_CHAIN_H = """\
#ifndef MINI_H
#define MINI_H
int add(int a, int b);
int scale(int v, int k);
int square(int v);
int magnitude(int v);
#endif
"""


_CHAIN_C = """\
#include <stdio.h>
#include "mini.h"

int add(int a, int b) {
    printf("hello from C add\\n");
    fflush(stdout);
    return a + b;
}

int scale(int v, int k) {
    printf("hello from C scale\\n");
    fflush(stdout);
    return add(v, v) * k;
}

int square(int v) {
    printf("hello from C square\\n");
    fflush(stdout);
    return scale(v, v);
}

int magnitude(int v) {
    printf("hello from C magnitude\\n");
    fflush(stdout);
    return add(square(v), 1);
}
"""

_TEST_RS_TEMPLATE = """\
#![allow(unused_imports)]
use std::env;
use std::process::Command;

use __SYS_CRATE__::*;

const CAPTURE_CHILD: &str = "TRACTOR_SMOKE_CAPTURE_CHILD";
const CAPTURE_BEGIN: &str = "--- smoke stdout begin ---";
const CAPTURE_END: &str = "--- smoke stdout end ---";

fn assert_stdout(test: &str, expected: &str, call: impl FnOnce()) {
    if env::var_os(CAPTURE_CHILD).is_some() {
        println!("{}", CAPTURE_BEGIN);
        call();
        println!("{}", CAPTURE_END);
        return;
    }

    let output = Command::new(env::current_exe().expect("current test executable"))
        .args(["--exact", test, "--nocapture", "--test-threads=1"])
        .env(CAPTURE_CHILD, "1")
        .output()
        .expect("run captured smoke test");
    let stdout = String::from_utf8(output.stdout).expect("smoke stdout is UTF-8");
    let stderr = String::from_utf8(output.stderr).expect("smoke stderr is UTF-8");
    assert!(
        output.status.success(),
        "captured smoke test failed:\\n--- stdout ---\\n{stdout}\\n--- stderr ---\\n{stderr}"
    );

    let (_, after_begin) = stdout
        .split_once(CAPTURE_BEGIN)
        .unwrap_or_else(|| panic!("missing stdout start marker:\\n{stdout}"));
    let (captured, _) = after_begin
        .split_once(CAPTURE_END)
        .unwrap_or_else(|| panic!("missing stdout end marker:\\n{stdout}"));

    assert_eq!(captured.trim(), expected);
}

#[test]
fn smoke_1_add() {
    assert_stdout(
        "smoke_1_add",
        r#"__ADD_STDOUT__"#,
        || unsafe {
            assert_eq!(add(2, 3), 5);
        },
    );
}

#[test]
fn smoke_2_scale() {
    assert_stdout(
        "smoke_2_scale",
        r#"__SCALE_STDOUT__"#,
        || unsafe {
            assert_eq!(scale(2, 3), 12);
        },
    );
}

#[test]
fn smoke_3_square() {
    assert_stdout(
        "smoke_3_square",
        r#"__SQUARE_STDOUT__"#,
        || unsafe {
            assert_eq!(square(2), 8);
        },
    );
}

#[test]
fn smoke_4_magnitude() {
    assert_stdout(
        "smoke_4_magnitude",
        r#"__MAGNITUDE_STDOUT__"#,
        || unsafe {
            assert_eq!(magnitude(2), 9);
        },
    );
}
"""


def _expected_stdout(wrapped: tuple[str, ...], *calls: str) -> str:
    return "\n".join(
        f"hello from {'Rust' if name in wrapped else 'C'} {name}" for name in calls
    )


def _smoke_test(wrapped: tuple[str, ...]) -> str:
    return (
        _TEST_RS_TEMPLATE.replace("__ADD_STDOUT__", _expected_stdout(wrapped, "add"))
        .replace("__SCALE_STDOUT__", _expected_stdout(wrapped, "scale", "add"))
        .replace(
            "__SQUARE_STDOUT__",
            _expected_stdout(wrapped, "square", "scale", "add"),
        )
        .replace(
            "__MAGNITUDE_STDOUT__",
            _expected_stdout(wrapped, "magnitude", "square", "scale", "add", "add"),
        )
        .replace("__SYS_CRATE__", f"{get_crate_name(TEMPLATE)}_sys")
    )


_TRANSLATIONS = {
    "return magnitude(2) - 9;": "pub fn main() { std::process::exit(magnitude(2).wrapping_sub(9)); }\n",
    "return a + b;": """\
pub fn add(a: i32, b: i32) -> i32 {
    println!("hello from Rust add");
    a.wrapping_add(b)
}
""",
    "return add(v, v) * k;": """\
pub fn scale(v: i32, k: i32) -> i32 {
    println!("hello from Rust scale");
    add(v, v).wrapping_mul(k)
}
""",
    "return scale(v, v);": """\
pub fn square(v: i32) -> i32 {
    println!("hello from Rust square");
    scale(v, v)
}
""",
    "return add(square(v), 1);": """\
pub fn magnitude(v: i32) -> i32 {
    println!("hello from Rust magnitude");
    add(square(v), 1)
}
""",
}

_WRAPPERS = {
    "add": """\
#[unsafe(export_name = "add")]
pub extern "C" fn add(a: ::std::os::raw::c_int, b: ::std::os::raw::c_int) -> ::std::os::raw::c_int {
    __RS_CRATE__::add(a, b)
}
""",
    "scale": """\
#[unsafe(export_name = "scale")]
pub extern "C" fn scale(
    v: ::std::os::raw::c_int,
    k: ::std::os::raw::c_int,
) -> ::std::os::raw::c_int {
    __RS_CRATE__::scale(v, k)
}
""",
    "square": """\
#[unsafe(export_name = "square")]
pub extern "C" fn square(v: ::std::os::raw::c_int) -> ::std::os::raw::c_int {
    __RS_CRATE__::square(v)
}
""",
    "magnitude": """\
#[unsafe(export_name = "magnitude")]
pub extern "C" fn magnitude(v: ::std::os::raw::c_int) -> ::std::os::raw::c_int {
    __RS_CRATE__::magnitude(v)
}
""",
}

_EXPORT_NAME = re.compile(r'export_name = "(?P<name>\w+)"')


def _translation_for(snippet: str) -> str:
    hits = [rust for c_line, rust in _TRANSLATIONS.items() if c_line in snippet]
    assert len(hits) == 1, f"expected exactly one translation for:\n{snippet}"
    return f"```rust\n{hits[0]}```"


def _wrapper_for(template: str) -> str:
    match = _EXPORT_NAME.search(template)
    assert match is not None, f"unrecognized wrapper template:\n{template}"
    return f"```rust\n{_WRAPPERS[match['name']].replace('__RS_CRATE__', f'{get_crate_name(TEMPLATE)}_rs')}```"


class _ScriptedLM(DummyLM):
    def __init__(self):
        super().__init__({}, adapter=adapters.ChatAdapter())

    def __call__(self, prompt=None, messages=None, **kwargs):
        content = messages[-1]["content"] if messages else (prompt or "")
        if template := field_value(content, "example_wrapper").strip():
            answer = {"wrapper": _wrapper_for(template)}
        else:
            answer = {"translation": _translation_for(field_value(content, "snippet"))}
        self.answers = {"": answer}
        return super().__call__(prompt=prompt, messages=messages, **kwargs)


@dataclass(frozen=True)
class _Step:
    wrapped: tuple[str, ...] = ()
    c_archive: tuple[str, ...] = ()
    rust_wrappers: tuple[str, ...] = ()
    linked: tuple[str, ...] = ()
    undefined: tuple[str, ...] = ()
    test_events: tuple[tuple[str, str], ...] = ()
    test_feedback: str = ""
    link_log: str = ""


def _nm(path: Path, *flags: str, names: tuple[str, ...] | None = ORDER) -> tuple[str, ...]:
    """The symbols of interest an artifact carries, sorted and with any duplicate kept."""
    proc = subprocess.run(["nm", *flags, str(path)], capture_output=True, text=True, check=True)
    # Archive listings interleave `member.o:` headers, which are not symbols
    found = [parts[-1] for line in proc.stdout.splitlines() if len(parts := line.split()) > 1]
    return tuple(sorted(name for name in found if names is None or name in names))


def _c_archive(workspace: Path) -> Path:
    target = workspace / "target" / "debug"
    sys_crate = f"{get_crate_name(TEMPLATE)}-sys"
    archives = sorted(target.glob(f"build/{sys_crate}-*/out/lib{C_ARCHIVE}.a"))
    assert archives, f"no C archive under {target}"
    return max(archives, key=lambda path: path.stat().st_mtime)


def _link_command(workspace: Path) -> list[str]:
    return [
        "cargo",
        "rustc",
        "--manifest-path",
        str(workspace / "Cargo.toml"),
        "-p",
        get_crate_name(TEMPLATE),
        "--lib",
        "--crate-type",
        "cdylib",
        "--",
        # `+whole-archive` drags in every archive member, so a definition C and Rust both
        # own is a link error instead of a member the linker never bothered to extract
        "-l",
        f"static:+whole-archive={C_ARCHIVE}",
        *(f"-Clink-arg={arg}" for arg in STRICT_LINK_ARGS),
    ]


def _strict_link(workspace: Path) -> subprocess.CompletedProcess:
    return subprocess.run(_link_command(workspace), capture_output=True, text=True)


# Inspect using `nm` at every step
def _inspect(
    workspace: Path,
    wrapped: tuple[str, ...],
    test_feedback: str,
    test_events: dict[str, str],
) -> _Step:
    target = workspace / "target" / "debug"
    print(f"wrappers {list(wrapped)}: {shlex.join(_link_command(workspace))}")
    link = _strict_link(workspace)
    cdylib = target / f"lib{CRATE}.so"
    return _Step(
        wrapped=wrapped,
        c_archive=_nm(_c_archive(workspace), "--defined-only", "--extern-only"),
        rust_wrappers=_nm(target / f"lib{CRATE}.rlib", "--defined-only", "--extern-only"),
        linked=_nm(cdylib, "--defined-only", "--extern-only"),
        undefined=_nm(cdylib, "--undefined-only"),
        test_events=tuple(sorted(test_events.items())),
        test_feedback=test_feedback,
        link_log=link.stdout + link.stderr if link.returncode else "",
    )


def _record_steps(monkeypatch, workspace: Path, source_test: Path, test: Path) -> list[_Step]:
    steps: list[_Step] = []

    class _Recorder(RecurrentTranslator):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # The oracle only ever runs the smoke test, so every call is worth recording
            oracle = cast(TestOracle, self._oracle)
            oracle_run = oracle._run

            def _run(name, *, skip=None, build_only=False):
                wrapped = tuple(symbol.split("@")[-1] for symbol in self._hybrid.wrappers)
                source = _smoke_test(wrapped)
                if source_test.read_text() != source:
                    source_test.write_text(source)
                translate_mod.copy_and_port_tests(
                    source_test, test, f"{get_crate_name(TEMPLATE)}_sys", "driver"
                )
                result = oracle_run(name, skip=skip, build_only=build_only)
                if not build_only:
                    steps.append(_inspect(workspace, wrapped, *result))
                return result

            oracle._run = _run  # type: ignore[method-assign]

    monkeypatch.setattr(translate_mod, "RecurrentTranslator", _Recorder)
    return steps


@pytest.fixture(params=["lib", "bin"])
def chain_translation(instrumented_workspace, monkeypatch, request):
    monkeypatch.setitem(globals(), "TEMPLATE", request.param)
    source = _CHAIN_C + (
        "\nint main(void) { return magnitude(2) - 9; }\n" if TEMPLATE == "bin" else ""
    )
    workspace = instrumented_workspace(TEMPLATE, header=_CHAIN_H, source=source)
    source_test = (
        workspace.parent / TEST_CRATES_DIR / get_crate_name(TEMPLATE) / "tests" / f"{TESTS}.rs"
    )
    test = workspace / get_crate_name(TEMPLATE) / "tests" / f"{TESTS}.rs"
    source_test.write_text(_smoke_test(()))

    # Hold the pipeline's own builds to the same standard as the relink below
    monkeypatch.setenv("CARGO_NET_OFFLINE", "true")
    monkeypatch.setenv("RUSTFLAGS", " ".join(f"-Clink-arg={arg}" for arg in STRICT_LINK_ARGS))
    monkeypatch.setenv("CFLAGS", "-fno-common")

    # The pipeline cleans the workspace when it finishes, but these tests inspect the artifacts
    # it leaves behind
    monkeypatch.setattr(Crate, "cargo_clean", lambda self, workspace=False: None)

    # Snapshot at every step using a wrapped pipeline
    steps = _record_steps(monkeypatch, workspace, source_test, test)
    run_translate(
        monkeypatch,
        workspace,
        TEMPLATE,
        _ScriptedLM(),
        tests=source_test,
        translator="Predict",
        wrapper="Predict",
        wrapper_max_iters=1,
    )
    return workspace, steps


def test_symbol_ownership_moves_from_c_to_rust(chain_translation):
    _, steps = chain_translation

    assert steps[0].wrapped == ()
    assert steps[0].c_archive == ("add", "magnitude", "scale", "square")
    assert steps[0].rust_wrappers == ()
    assert steps[0].linked == ()
    assert steps[0].undefined == ()
    # Empty link log means no errors
    assert steps[0].link_log == ""

    assert steps[1].wrapped == ("add",)
    assert steps[1].c_archive == ("magnitude", "scale", "square")
    assert steps[1].rust_wrappers == ("add",)
    assert steps[1].linked == ("add",)
    assert steps[1].undefined == ()
    assert steps[1].link_log == ""

    assert steps[2].wrapped == ("add", "scale")
    assert steps[2].c_archive == ("magnitude", "square")
    assert steps[2].rust_wrappers == ("add", "scale")
    assert steps[2].linked == ("add", "scale")
    assert steps[2].undefined == ()
    assert steps[2].link_log == ""

    assert steps[3].wrapped == ("add", "scale", "square")
    assert steps[3].c_archive == ("magnitude",)
    assert steps[3].rust_wrappers == ("add", "scale", "square")
    assert steps[3].linked == ("add", "scale", "square")
    assert steps[3].undefined == ()
    assert steps[3].link_log == ""

    assert steps[4].wrapped == ("add", "scale", "square", "magnitude")
    assert steps[4].c_archive == ()
    assert steps[4].rust_wrappers == ("add", "magnitude", "scale", "square")
    assert steps[4].linked == ("add", "magnitude", "scale", "square")
    assert steps[4].undefined == ()
    assert steps[4].link_log == ""


def test_all_smoke_tests_pass_at_every_hybrid_state(chain_translation):
    _, steps = chain_translation

    assert steps[0].wrapped == ()
    assert steps[0].test_feedback == ""
    assert steps[0].test_events == (
        ("smoke_1_add", "ok"),
        ("smoke_2_scale", "ok"),
        ("smoke_3_square", "ok"),
        ("smoke_4_magnitude", "ok"),
    )

    assert steps[1].wrapped == ("add",)
    assert steps[1].test_feedback == ""
    assert steps[1].test_events == (
        ("smoke_1_add", "ok"),
        ("smoke_2_scale", "ok"),
        ("smoke_3_square", "ok"),
        ("smoke_4_magnitude", "ok"),
    )

    assert steps[2].wrapped == ("add", "scale")
    assert steps[2].test_feedback == ""
    assert steps[2].test_events == (
        ("smoke_1_add", "ok"),
        ("smoke_2_scale", "ok"),
        ("smoke_3_square", "ok"),
        ("smoke_4_magnitude", "ok"),
    )

    assert steps[3].wrapped == ("add", "scale", "square")
    assert steps[3].test_feedback == ""
    assert steps[3].test_events == (
        ("smoke_1_add", "ok"),
        ("smoke_2_scale", "ok"),
        ("smoke_3_square", "ok"),
        ("smoke_4_magnitude", "ok"),
    )

    assert steps[4].wrapped == ("add", "scale", "square", "magnitude")
    assert steps[4].test_feedback == ""
    assert steps[4].test_events == (
        ("smoke_1_add", "ok"),
        ("smoke_2_scale", "ok"),
        ("smoke_3_square", "ok"),
        ("smoke_4_magnitude", "ok"),
    )


def test_duplicate_definition_collides_and_is_fatal(chain_translation):
    """The strict flags must reject a name C and Rust both own instead of picking a winner."""
    workspace, _ = chain_translation

    # Undo the extern-ification of `add` so C reclaims a name the Rust wrapper exports
    c_src = workspace / f"{get_crate_name(TEMPLATE)}-sys" / "src" / "lib.c"
    c_src.write_text(
        c_src.read_text().replace(
            "extern int add(int a, int b);", "int add(int a, int b) { return a + b; }"
        )
    )

    link = _strict_link(workspace)

    c_defines = _nm(_c_archive(workspace), "--defined-only", "--extern-only")
    rust_defines = _nm(
        workspace / "target" / "debug" / f"lib{CRATE}.rlib", "--defined-only", "--extern-only"
    )
    assert c_defines == ("add",)
    assert rust_defines == ("add", "magnitude", "scale", "square")
    assert link.returncode != 0, link.stdout + link.stderr
    assert "multiple definition of `add'" in link.stderr, link.stderr


def test_undefined_reference_is_visible_and_fatal(chain_translation):
    """A C body calling a name nobody defines must break the link, not the process later."""
    workspace, _ = chain_translation

    # `retain` keeps the linker from garbage collecting the reference before it checks it
    c_src = workspace / f"{get_crate_name(TEMPLATE)}-sys" / "src" / "lib.c"
    c_src.write_text(
        c_src.read_text()
        + "\nextern int missing(int v);\n"
        + "__attribute__((used, retain)) int probe(int v) { return missing(v); }\n"
    )

    link = _strict_link(workspace)

    assert _nm(_c_archive(workspace), "--undefined-only", names=None) == ("missing",)
    assert link.returncode != 0, link.stdout + link.stderr
    assert "undefined reference to `missing'" in link.stderr, link.stderr
