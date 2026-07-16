#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from pathlib import Path
from textwrap import dedent

import pytest

from ideas import wrapper as wrapper_mod


@pytest.mark.parametrize(
    ("source", "symbol", "expected"),
    [
        (
            "int foo = 7;\n",
            "foo",
            dedent(
                """
                unsafe extern "C" {
                    pub static mut foo: ::std::os::raw::c_int;
                }
                """
            ).strip(),
        ),
        (
            "static int sfoo = 7;\n",
            "sfoo",
            dedent(
                """
                unsafe extern "C" {
                    pub static mut sfoo: ::std::os::raw::c_int;
                }
                """
            ).strip(),
        ),
        (
            "int arr[3] = {1,2,3};\n",
            "arr",
            dedent(
                """
                unsafe extern "C" {
                    pub static mut arr: [::std::os::raw::c_int; 3usize];
                }
                """
            ).strip(),
        ),
        (
            "int arr_unsized[] = {1,2,3};\n",
            "arr_unsized",
            dedent(
                """
                unsafe extern "C" {
                    pub static mut arr_unsized: [::std::os::raw::c_int; 0usize];
                }
                """
            ).strip(),
        ),
        (
            "static int sarr[2] = {4,5};\n",
            "sarr",
            dedent(
                """
                unsafe extern "C" {
                    pub static mut sarr: [::std::os::raw::c_int; 2usize];
                }
                """
            ).strip(),
        ),
        (
            "struct Point { int x; int y; };\nstruct Point pt = {1,2};\n",
            "pt",
            dedent(
                """
                #[repr(C)]
                #[derive(Debug, Copy, Clone)]
                pub struct Point {
                    pub x: ::std::os::raw::c_int,
                    pub y: ::std::os::raw::c_int,
                }
                unsafe extern "C" {
                    pub static mut pt: Point;
                }
                """
            ).strip(),
        ),
        (
            "struct Point { int x; int y; };\nstatic struct Point spt = {3,4};\n",
            "spt",
            dedent(
                """
                #[repr(C)]
                #[derive(Debug, Copy, Clone)]
                pub struct Point {
                    pub x: ::std::os::raw::c_int,
                    pub y: ::std::os::raw::c_int,
                }
                unsafe extern "C" {
                    pub static mut spt: Point;
                }
                """
            ).strip(),
        ),
        (
            "const int c = 9;\n",
            "c",
            dedent(
                """
                unsafe extern "C" {
                    pub static c: ::std::os::raw::c_int;
                }
                """
            ).strip(),
        ),
        (
            "int x = 0; int *px = &x;\n",
            "px",
            dedent(
                """
                unsafe extern "C" {
                    pub static mut px: *mut ::std::os::raw::c_int;
                }
                """
            ).strip(),
        ),
        (
            "int match = 1;\n",
            "match",
            dedent(
                """
                unsafe extern "C" {
                    #[link_name = "\\u{1}match"]
                    pub static mut match_: ::std::os::raw::c_int;
                }
                """
            ).strip(),
        ),
    ],
)
def test_bindgen_emits_expected_text_for_global_shapes(
    tmp_path: Path, source: str, symbol: str, expected: str
):
    c_path = tmp_path / "input.c"
    c_path.write_text(source)

    binding = wrapper_mod.bindgen(c_path, symbol)

    assert str(binding).strip() == expected
    assert c_path.read_text() == source


def test_bindgen_restores_source_when_bindgen_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    c_path = tmp_path / "input.c"
    original_src = "static int foo = 7;\n"
    c_path.write_text(original_src)

    monkeypatch.setattr(
        wrapper_mod, "run_subprocess", lambda *_args, **_kwargs: (False, "", "boom", 1)
    )

    with pytest.raises(ValueError, match="Bindgen failed"):
        wrapper_mod.bindgen(c_path, "foo")

    assert c_path.read_text() == original_src


def test_bindgen_raises_for_empty_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    c_path = tmp_path / "input.c"
    c_path.write_text("int foo(void) { return 1; }\n")

    monkeypatch.setattr(
        wrapper_mod, "run_subprocess", lambda *_args, **_kwargs: (True, "   \n", "", 0)
    )

    with pytest.raises(ValueError, match="empty binding"):
        wrapper_mod.bindgen(c_path, "foo")


def test_bindgen_handles_dependent_declarations_for_target_global(tmp_path: Path):
    c_path = tmp_path / "input.c"
    array_decl = "int arr[] = {1,2,3};\n"
    dependent_decl = "static const int num_arr = sizeof(arr) / sizeof(arr[0]);\n"

    c_path.write_text(array_decl)
    baseline_binding = wrapper_mod.bindgen(c_path, "arr")
    assert c_path.read_text() == array_decl

    c_path.write_text(array_decl + dependent_decl)
    dependent_binding = wrapper_mod.bindgen(c_path, "arr")
    assert c_path.read_text() == array_decl + dependent_decl

    assert str(dependent_binding).strip() == str(baseline_binding).strip()


def test_bindgen_handles_dependent_declarations_for_target_function(tmp_path: Path):
    c_path = tmp_path / "input.c"
    baseline_source = "int f(int x) { return x + 1; }\n"
    dependent_source = baseline_source + "int (*pf)(int) = f;\n"

    c_path.write_text(baseline_source)
    baseline_binding = wrapper_mod.bindgen(c_path, "f")
    assert c_path.read_text() == baseline_source

    c_path.write_text(dependent_source)
    dependent_binding = wrapper_mod.bindgen(c_path, "f")
    assert c_path.read_text() == dependent_source

    assert str(dependent_binding).strip() == str(baseline_binding).strip()
