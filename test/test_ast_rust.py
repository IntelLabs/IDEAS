#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from ideas.ast_rust import (
    CodeRust,
    non_public_items,
    strip_cfg_test,
    strip_exports,
    strip_visibility,
)


def test_strip_cfg_test_drops_only_test_items():
    code = CodeRust("""\
impl CInterop for house_t {
    type Rust = driver_rs::HouseT;
}

#[cfg(test)]
#[allow(non_snake_case)]
mod test_type_house_t {
    #[test]
    fn round_trip_zeroed() {}
}

#[cfg(test)] // tests
mod test_var_counter {
    #[test]
    fn initial_value_matches() { todo!() }
}

#[cfg(not(test))]
pub fn keep() {}
""")

    out = str(strip_cfg_test(code))

    assert "impl CInterop for house_t {" in out
    assert "test_type_house_t" not in out
    assert "#[allow(non_snake_case)]" not in out
    assert "test_var_counter" not in out
    assert "todo!()" not in out
    assert "#[cfg(not(test))]\npub fn keep() {}" in out


def test_strip_exports_keeps_items_but_drops_their_symbol_names():
    code = CodeRust("""\
#[unsafe(export_name = "helper")]
pub extern "C" fn helper() {}

#[no_mangle]
pub extern "C" fn other() {}

#[unsafe(no_mangle)]
#[inline]
pub extern "C" fn third() {}
""")

    out = str(strip_exports(code))

    assert "export_name" not in out
    assert "no_mangle" not in out
    assert 'pub extern "C" fn helper() {}' in out
    assert 'pub extern "C" fn other() {}' in out
    assert '#[inline]\npub extern "C" fn third() {}' in out


def test_strip_visibility_drops_only_top_level_visibility():
    code = CodeRust("""\
#[unsafe(no_mangle)]
pub extern "C" fn f() {}

pub(crate) static S: i32 = 0;

pub struct T { pub x: i32 }
""")

    out = str(strip_visibility(code))

    assert '#[unsafe(no_mangle)]\nextern "C" fn f() {}' in out
    assert "\nstatic S: i32 = 0;" in out
    assert "\nstruct T { pub x: i32 }" in out


def test_non_public_items_lists_private_items_and_fields():
    code = CodeRust("""\
pub struct A { pub x: i32, #[doc = ""] y: u8, pub(crate) z: u8 }
pub struct T(pub i32, #[allow(dead_code)] u8);
struct U;
fn f() {}
pub fn g() { fn inner() {} }
pub enum E { V }
type Alias = i32;
pub const C: i32 = 0;
impl A { fn m(&self) {} }
use std::rc::Rc;
""")

    assert non_public_items(code) == ["A.y", "A.z", "T.1", "U", "f", "Alias"]
