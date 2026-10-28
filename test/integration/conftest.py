#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from ideas.codex import CodexResult
from ideas.hybrid import SAFE_CRATE
from ideas.model import ModelConfig
from ideas.translate import TranslateConfig
import ideas.codex as codex_mod
import ideas.translate as translate_mod
import ideas.translate_snippet as snippet_mod
import ideas.wrapper as wrapper_mod

REPO_ROOT = Path(__file__).resolve().parents[2]
IDEAS_MK = REPO_ROOT / "IDEAS.mk"
TRANSLATION_DIR = "translation.test"
TEST_CRATES_DIR = "test_crates"
CRATE = "driver"

BUILD_TARGET = "bear"
INIT_TARGET = "init"
TRANSLATION_TEST = "smoke"

BUILD_ENV = {**os.environ, "CARGO_NET_OFFLINE": "true", "RUSTFLAGS": "-Awarnings"}
# Native, so the suite needs no Docker image; IDEAS_RUNNER=landlock opts into the sandbox
_RUNNER = "landlock" if os.environ.get("IDEAS_RUNNER") == "landlock" else "native"
MAKE_ENV = {**os.environ, "IDEAS_RUNNER": _RUNNER, "UV_PROJECT": str(REPO_ROOT)}

_MINI_H = """\
#ifndef MINI_H
#define MINI_H
int add(int a, int b);
int sub(int a, int b);
#endif
"""

_MINI_C_LIB = """\
#include "mini.h"

int add(int a, int b) {
    return a + b;
}

int sub(int a, int b) {
    return a - b;
}
"""

_MINI_C_BIN = """\
#include "mini.h"

int add(int a, int b) {
    return a + b;
}

int sub(int a, int b) {
    return a - b;
}

int main(void) {
    return add(2, 3) - sub(5, 0);
}
"""

_CMAKE_LIB = """\
cmake_minimum_required(VERSION 3.19)
project({crate} C)
add_library({crate} SHARED src/mini.c)
target_include_directories({crate} PUBLIC ${{CMAKE_CURRENT_SOURCE_DIR}}/include)
"""

_CMAKE_BIN = """\
cmake_minimum_required(VERSION 3.19)
project({crate} C)
add_executable({crate} src/mini.c)
target_include_directories({crate} PUBLIC ${{CMAKE_CURRENT_SOURCE_DIR}}/include)
"""

_SMOKE_TEST = """\
#[test]
fn smoke() {
    assert_eq!(1, 1);
}
"""

_TRANSLATION = {
    "add": "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n",
    "sub": "pub fn sub(a: i32, b: i32) -> i32 {\n    a - b\n}\n",
    "main": "pub fn main() {\n    let _ = add(2, 3) - sub(5, 0);\n}\n",
}


def get_crate_name(template: str) -> str:
    return f"{CRATE}" if template == "bin" else f"lib{CRATE}"


def get_hybrid_name(template: str) -> str:
    return f"{get_crate_name(template)}-hybrid"


def get_hybrid_lib(template: str) -> str:
    return get_hybrid_name(template).replace("-", "_")


def get_shipped_name(template: str) -> str:
    return f"{get_crate_name(template)}-{'ffi' if template == 'lib' else 'rs'}"


@pytest.fixture
def instrumented_workspace(tmp_path):
    """
    test_crates/smoke.rs -> cmake -> init
    """

    def _factory(template: str, header: str | None = None, source: str | None = None) -> Path:
        instrumented = tmp_path / template
        instrumented.mkdir()
        _write_project(instrumented, template, header, source)
        test = (
            instrumented
            / TEST_CRATES_DIR
            / get_crate_name(template)
            / "tests"
            / f"{TRANSLATION_TEST}.rs"
        )
        test.parent.mkdir(parents=True)
        test.write_text(_SMOKE_TEST)
        _make(instrumented, BUILD_TARGET)
        _make(instrumented, INIT_TARGET)
        return instrumented / TRANSLATION_DIR

    return _factory


def _write_project(
    instrumented: Path,
    template: str,
    header: str | None = None,
    source: str | None = None,
) -> None:
    if template == "lib":
        cmake, default_source = _CMAKE_LIB, _MINI_C_LIB
    elif template == "bin":
        cmake, default_source = _CMAKE_BIN, _MINI_C_BIN
    else:
        raise ValueError(template)

    test_case = instrumented / "test_case"
    (test_case / "src").mkdir(parents=True, exist_ok=True)
    (test_case / "include").mkdir(parents=True, exist_ok=True)
    (test_case / "include" / "mini.h").write_text(header or _MINI_H)
    (test_case / "src" / "mini.c").write_text(source or default_source)
    (test_case / "CMakeLists.txt").write_text(cmake.format(crate=CRATE))


def _make(instrumented: Path, goal: str) -> None:
    cmd = [
        "make",
        "-C",
        str(instrumented),
        "-f",
        str(IDEAS_MK),
        f"TRANSLATION_DIR={TRANSLATION_DIR}",
        goal,
    ]
    proc = subprocess.run(cmd, env=MAKE_ENV, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"make target {goal!r} failed (rc={proc.returncode}):\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )


def build_ok(workspace: Path, crate: str, *extra: str) -> bool:
    cmd = [
        "cargo",
        "build",
        "--quiet",
        "--manifest-path",
        str(workspace / "Cargo.toml"),
        "-p",
        crate,
        *extra,
    ]
    return subprocess.run(cmd, env=BUILD_ENV, capture_output=True, text=True).returncode == 0


# The JSON object the real CLI hands back as its `final_message`, keyed off the prompt
type Responder = Callable[[str], dict[str, str]]

# `build_prompt` renders every `inputs` entry as a fenced block under its own `# <field>`
# heading; the instructions above them only ever use prose headings.
_FIELD = "^# {field}\n\n```[a-z]*\n(.*?)\n?```$"


def field_value(content: str, field: str) -> str:
    """Slice one rendered `# <field>` block out of a prompt."""
    pattern = _FIELD.format(field=re.escape(field))
    match = re.search(pattern, content, re.DOTALL | re.MULTILINE)
    return match.group(1) if match else ""


def _result(answer: dict[str, str]) -> CodexResult:
    return CodexResult(final_message=json.dumps(answer), usage={}, reasoning="", raw="")


_SYMBOLS = {"return a + b;": "add", "return a - b;": "sub", "int main": "main"}

# A turn without an `output` field is a GenerationParseError, which the pipeline treats as fatal
_BAD_ANSWER = {"unexpected_field": "not a translation"}

_EXPORT_NAME = re.compile(r'export_name = "(?P<name>\w+)"')


def _symbol(snippet: str) -> str:
    hits = [name for key, name in _SYMBOLS.items() if key in snippet]
    assert len(hits) == 1, f"expected exactly one symbol in:\n{snippet}"
    return hits[0]


def success_codex() -> Responder:
    def respond(prompt: str) -> dict[str, str]:
        name = _symbol(field_value(prompt, "snippet"))
        return {"reasoning": "trivial translation", "output": _TRANSLATION[name]}

    return respond


def error_codex() -> Responder:
    return lambda _prompt: _BAD_ANSWER


def wrapping_codex(*, fail: frozenset[str] = frozenset()) -> Responder:
    # Answers both translation and wrapper prompts so a run reaches the export table
    def respond(prompt: str) -> dict[str, str]:
        if example := field_value(prompt, "example_wrapper").strip():
            match = _EXPORT_NAME.search(example)
            assert match is not None, f"unrecognized wrapper template:\n{example}"
            # Filling in the template keeps its signature and attributes exactly as validated
            body = f"{SAFE_CRATE}::{match['name']}(a, b)"
            wrapper = example.replace("unimplemented!()", body)
            return {"reasoning": "trivial wrapper", "output": f"```rust\n{wrapper}\n```"}

        name = _symbol(field_value(prompt, "snippet"))
        if name in fail:
            return _BAD_ANSWER
        return {"reasoning": "trivial translation", "output": _TRANSLATION[name]}

    return respond


def make_config(workspace: Path, template: str, **overrides):
    crate_name = get_crate_name(template)
    defaults = dict(
        model=ModelConfig(name="fake/model", reasoning_effort="low"),
        cargo_toml=workspace / get_shipped_name(template) / "Cargo.toml",
        bindings_cargo_toml=workspace / f"{crate_name}-sys" / "Cargo.toml",
        tests=(
            workspace.parent / TEST_CRATES_DIR / crate_name / "tests" / f"{TRANSLATION_TEST}.rs"
        ),
        template=template,
        translator_max_iters=1,
        wrapper_max_iters=0,
        max_iters=1,
        vcs="none",
    )
    return TranslateConfig(**{**defaults, **overrides})


def _no_codex(*_args, **_kwargs):
    raise AssertionError("the run reached the real Codex CLI")


def install_mock(monkeypatch, workspace: Path, respond: Responder, template: str) -> None:
    output_dir = workspace / get_shipped_name(template)

    def run_codex(prompt: str, *_args, **_kwargs) -> CodexResult:
        return _result(respond(prompt))

    # Both modules bound `run_codex` at import, so patching `ideas.codex` alone would not take
    monkeypatch.setattr(snippet_mod, "run_codex", run_codex)
    monkeypatch.setattr(wrapper_mod, "run_codex", run_codex)
    # Whatever slipped past those must fail here rather than spawn the real CLI
    monkeypatch.setattr(codex_mod, "run_codex_turn", _no_codex)

    class _Runtime:
        def __init__(self, path: Path):
            self.output_dir = str(path)

    class _HydraCfg:
        def __init__(self, path: Path):
            self.runtime = _Runtime(path)
            self.output_subdir = None

    class _HydraConfig:
        @staticmethod
        def get():
            return _HydraCfg(output_dir)

    monkeypatch.setattr(translate_mod, "HydraConfig", _HydraConfig)


def run_translate(
    monkeypatch, workspace: Path, template: str, respond: Responder, **overrides
) -> None:
    install_mock(monkeypatch, workspace, respond, template)
    translate_mod._main(make_config(workspace, template, **overrides))
