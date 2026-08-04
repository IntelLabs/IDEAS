#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import os
import subprocess
from pathlib import Path

import dspy
import pytest
from dspy.utils.dummies import DummyLM

from ideas import adapters
from ideas.translate import TranslateConfig
import ideas.model as model_mod
import ideas.translate as translate_mod

REPO_ROOT = Path(__file__).resolve().parents[2]
IDEAS_MK = REPO_ROOT / "IDEAS.mk"
TRANSLATION_DIR = "translation.test"
CRATE = "driver"

BUILD_TARGET = "bear"
INIT_TARGET = "init"

BUILD_ENV = {**os.environ, "CARGO_NET_OFFLINE": "true", "RUSTFLAGS": "-Awarnings"}
MAKE_ENV = {**os.environ, "UV_PROJECT": str(REPO_ROOT)}

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

_TRANSLATION = {
    "add": "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n",
    "sub": "pub fn sub(a: i32, b: i32) -> i32 {\n    a - b\n}\n",
    "main": "pub fn main() {\n    let _ = add(2, 3) - sub(5, 0);\n}\n",
}


def get_crate_name(template: str) -> str:
    return f"{CRATE}" if template == "bin" else f"lib{CRATE}"


@pytest.fixture
def instrumented_workspace(tmp_path):
    """
    cmake -> init -> tests/smoke.rs
    """

    def _factory(template: str) -> Path:
        instrumented = tmp_path / template
        instrumented.mkdir()
        _write_project(instrumented, template)
        _make(instrumented, BUILD_TARGET)
        _make(instrumented, INIT_TARGET)
        _make(
            instrumented,
            f"{TRANSLATION_DIR}/{get_crate_name(template)}-sys/tests/smoke.rs",
        )
        return instrumented / TRANSLATION_DIR

    return _factory


def _write_project(instrumented: Path, template: str) -> None:
    test_case = instrumented / "test_case"
    (test_case / "src").mkdir(parents=True, exist_ok=True)
    (test_case / "include").mkdir(parents=True, exist_ok=True)
    (test_case / "include" / "mini.h").write_text(_MINI_H)
    if template == "lib":
        (test_case / "src" / "mini.c").write_text(_MINI_C_LIB)
        (test_case / "CMakeLists.txt").write_text(_CMAKE_LIB.format(crate=CRATE))
    elif template == "bin":
        (test_case / "src" / "mini.c").write_text(_MINI_C_BIN)
        (test_case / "CMakeLists.txt").write_text(_CMAKE_BIN.format(crate=CRATE))
    else:
        raise ValueError(template)


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


def _snippet_field(content: str) -> str:
    # DummyLM keys match anywhere in the prompt, but `dependent_code` also carries the
    # C code of sibling symbols, so every translation prompt contains every definition.
    # Narrow matching to the `snippet` field so each key selects exactly one symbol.
    _, marker, rest = content.partition("[[ ## snippet ## ]]")
    if not marker:
        return content
    return rest.partition("[[ ## ")[0]


class _SnippetKeyedLM(DummyLM):
    def __call__(self, prompt=None, messages=None, **kwargs):
        if messages:
            last = messages[-1]
            messages = [
                *messages[:-1],
                {**last, "content": _snippet_field(last["content"])},
            ]
        return super().__call__(prompt=prompt, messages=messages, **kwargs)


def success_lm() -> DummyLM:
    return _SnippetKeyedLM(
        {
            "return a + b;": {
                "reasoning": "trivial translation",
                "translation": _TRANSLATION["add"],
            },
            "return a - b;": {
                "reasoning": "trivial translation",
                "translation": _TRANSLATION["sub"],
            },
            "int main": {
                "reasoning": "trivial translation",
                "translation": _TRANSLATION["main"],
            },
        },
        adapter=adapters.ChatAdapter(),
    )


def error_lm() -> DummyLM:
    # The empty key matches every prompt, so every call returns a field the signature
    # never declares -> the adapter raises AdapterParseError, which the pipeline treats
    # as an irrecoverable DSPy error.
    return DummyLM(
        {"": {"unexpected_field": "not a translation"}}, adapter=adapters.ChatAdapter()
    )


def make_config(workspace: Path, template: str):
    crate_name = get_crate_name(template)
    return TranslateConfig(
        cargo_toml=workspace / crate_name / "Cargo.toml",
        bindings_cargo_toml=workspace / f"{crate_name}-sys" / "Cargo.toml",
        tests="smoke",
        template=template,
        translator="ChainOfThought",
        translator_max_iters=1,
        wrapper="ChainOfThought",
        wrapper_max_iters=0,
        max_iters=1,
        vcs="none",
    )


def install_mock(monkeypatch, workspace: Path, lm: dspy.LM, template: str) -> None:
    def fake_configure(model, generate):
        dspy.configure(lm=lm, track_usage=True)

    monkeypatch.setattr(model_mod, "configure", fake_configure)
    output_dir = workspace / get_crate_name(template)

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


def run_translate(monkeypatch, workspace: Path, template: str, lm: dspy.LM) -> None:
    install_mock(monkeypatch, workspace, lm, template)
    translate_mod._main(make_config(workspace, template))
