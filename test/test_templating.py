#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import re

import pytest

from ideas.ast_rust import CodeRust, validate_changes
from ideas.codex import build_prompt

TEMPLATE = """\
struct Context {
    other_var: i32,
}

fn function(var: i32) -> i32 {
    unimplemented!()
}

fn other_function(other_var: i32) -> i32 {
    unimplemented!()
}

fn immutable_function(var: i32) -> i32 {
    var
}

fn main() -> Result<()> {
    Ok(())
}
"""

MODIFIED_VALID = """\
struct Context {
    other_var: i32,
}

fn function(var: i32) -> i32 {
    fn inner_function(param: i32) {
        println!("Inner function received: {}", param);
    }

    let closure_func = || {
        println!("Closure accessed var directly: {}", var);
    };

    var
}

fn other_function(other_var: i32) -> i32 {
unimplemented!()
}

fn immutable_function(var: i32) -> i32 {
    var
}

fn main() -> Result<()> {
    Ok(())
}
"""

MODIFIED_INVALID = """\
struct Context {
    other_var: i32,
    more_var: u32,
}

fn function(var: i32) -> i32 {
    fn inner_function(param: i32) {
        println!("Inner function received: {}", param);
    }

    let closure_func = || {
        println!("Closure accessed var directly: {}", var);
    };

    var
}

fn other_function(other_var: i32) -> i32 {
    unimplemented!()
}

fn immutable_function(var: i32) -> i32 {
    var
}

fn immutable_function2(var: i32) -> i32 {
    var
}

fn main() -> Result<()> {
    Ok(())
}
"""

SIGNATURE_TEMPLATE = """\
#[unsafe(export_name = "buffer_append")]
pub unsafe extern "C" fn buffer_append(buffer: *mut buffer_t, len: usize) -> c_int {
    unimplemented!()
}
"""

SIGNATURE_IDENTICAL = """\
#[unsafe(export_name = "buffer_append")]
pub unsafe extern "C" fn buffer_append(buffer: *mut buffer_t, len: usize) -> c_int {
    0
}
"""

SIGNATURE_REFORMATTED = """\
#[unsafe(export_name = "buffer_append")]
pub unsafe extern "C" fn buffer_append(
    buffer: *mut buffer_t,
    len: usize,
) -> c_int {
    0
}
"""

SIGNATURE = 'pub unsafe extern "C" fn buffer_append(buffer: *mut buffer_t, len: usize) -> c_int'


def _reformatted_feedback() -> str:
    feedback = validate_changes(
        CodeRust(code=SIGNATURE_REFORMATTED), CodeRust(code=SIGNATURE_TEMPLATE)
    )
    return feedback["signature_changes"]


def test_modified_valid():
    feedback = validate_changes(CodeRust(code=MODIFIED_VALID), CodeRust(code=TEMPLATE))
    assert not feedback


def test_modified_invalid():
    feedback = validate_changes(CodeRust(code=MODIFIED_INVALID), CodeRust(code=TEMPLATE))
    assert list(feedback.keys()) == ["top_level_changes", "signature_changes"]


def test_identical_signature_is_accepted():
    feedback = validate_changes(
        CodeRust(code=SIGNATURE_IDENTICAL), CodeRust(code=SIGNATURE_TEMPLATE)
    )
    assert not feedback


def test_reformatted_signature_feedback():
    feedback = _reformatted_feedback()

    assert f"~~~rust\n{SIGNATURE}\n~~~" in feedback
    diffs = [
        block.splitlines() for block in re.findall(r"~~~diff\n(.*?)\n~~~", feedback, re.DOTALL)
    ]
    assert any(
        f"-{SIGNATURE}" in lines
        and any(line.startswith("+") and not line.startswith("+++") for line in lines)
        for lines in diffs
    )


def test_feedback_stays_inside_prompt_fence():
    feedback = _reformatted_feedback()
    prompt = build_prompt("", {"scope_feedback": feedback}, {}, "lib.rs")
    # A backtick fence in the feedback would close the one the prompt wraps it in
    assert "```" not in feedback
    assert f"# scope_feedback\n\n```\n{feedback}\n```" in prompt


def test_build_prompt_mentions_lint_command():
    lint_cmd = "cargo clippy --lib -p example"
    prompt = build_prompt("", {}, {}, "lib.rs", lint_cmd=lint_cmd)
    assert f"`{lint_cmd}`" in prompt


@pytest.mark.parametrize(
    "old, new",
    [
        ("pub unsafe extern", "pub extern"),
        ('unsafe extern "C" fn', "unsafe fn"),
    ],
    ids=["drop-unsafe", "drop-extern-c"],
)
def test_changed_qualifiers_are_rejected(old: str, new: str):
    wrapper = SIGNATURE_IDENTICAL.replace(old, new)
    feedback = validate_changes(CodeRust(code=wrapper), CodeRust(code=SIGNATURE_TEMPLATE))
    assert list(feedback.keys()) == ["signature_changes"]


@pytest.mark.parametrize(
    "old, new",
    [
        ('export_name = "buffer_append"', 'export_name = "buffer_append2"'),
        ('#[unsafe(export_name = "buffer_append")]', "#[unsafe(no_mangle)]"),
        ('#[unsafe(export_name = "buffer_append")]\n', ""),
    ],
    ids=["rename-export", "swap-no-mangle", "drop-attribute"],
)
def test_changed_attributes_are_rejected(old: str, new: str):
    wrapper = SIGNATURE_IDENTICAL.replace(old, new)
    feedback = validate_changes(CodeRust(code=wrapper), CodeRust(code=SIGNATURE_TEMPLATE))
    assert list(feedback.keys()) == ["attribute_changes"]
    assert '#[unsafe(export_name = "buffer_append")]' in feedback["attribute_changes"]


@pytest.mark.parametrize(
    "prefix, suffix",
    [
        ("/// # Safety\n///\n/// Reworded.\n", ""),
        ("", "#[allow(non_snake_case)]\n"),
        ("", "#[allow(clippy::too_many_arguments)]\n"),
    ],
    ids=["doc-comment", "allow-non-snake-case", "additional-lint-attribute"],
)
def test_permitted_additions_are_accepted(prefix: str, suffix: str):
    wrapper = prefix + SIGNATURE_IDENTICAL.replace(
        "pub unsafe extern", suffix + "pub unsafe extern"
    )
    feedback = validate_changes(CodeRust(code=wrapper), CodeRust(code=SIGNATURE_TEMPLATE))
    assert not feedback


def test_disallowed_name_allowance_preserves_template_signature():
    template = CodeRust("pub fn convert(foo: i32) { unimplemented!() }")
    wrapper = CodeRust(
        "#[allow(clippy::disallowed_names)]\npub fn convert(foo: i32) { let _ = foo; }"
    )

    assert not validate_changes(wrapper, template)
