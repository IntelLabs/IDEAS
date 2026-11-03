#![forbid(
    improper_ctypes,
    improper_ctypes_definitions,
    static_mut_refs,
    unsafe_op_in_unsafe_fn,
    clippy::multiple_unsafe_ops_per_block,
    clippy::not_unsafe_ptr_arg_deref,
    clippy::undocumented_unsafe_blocks
)]

use std::ffi::{c_int, c_long};

use libx_rs as _;

unsafe extern "C" {
    safe fn abs(x: c_int) -> c_int;
}

static mut COUNTER: c_int = 0;

#[repr(C)]
pub struct Point {
    pub x: c_int,
}

unsafe trait Marker {}
unsafe impl Marker for Point {}

unsafe impl Send for Point {}

/// # Safety
///
/// The caller must uphold the contract of the C function this replaces.
#[unsafe(export_name = "add")]
pub unsafe extern "C" fn add(a: c_int, b: c_int) -> c_int {
    // SAFETY: `helper` has no preconditions
    let a = unsafe { helper(a) };
    libx_rs::add(a, abs(b))
}

unsafe extern "C" fn helper(a: c_int) -> c_int {
    a
}

#[unsafe(no_mangle)]
pub extern "C" fn bump() -> c_int {
    // SAFETY: the tests are single-threaded
    unsafe { *&raw mut COUNTER += 1 };
    // SAFETY: as above
    unsafe { *&raw const COUNTER }
}

#[unsafe(export_name = "priv_export")]
extern "C" fn priv_export() -> c_int {
    1
}

#[unsafe(no_mangle)]
extern "C" fn priv_no_mangle() -> c_int {
    2
}

#[unsafe(no_mangle)]
static priv_global: c_int = 3;

#[used]
static KEEP: c_int = 4;

mod inner {
    pub(crate) fn live_in_mod() -> i32 {
        7
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn uses_mod() -> i32 {
    inner::live_in_mod()
}

pub enum LiveEnum {
    A,
    B,
}
