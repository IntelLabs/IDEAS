#![allow(dead_code, unused_variables)]

use std::ffi::c_int;

pub fn simple_public() {
    println!("Hello");
}

fn private_function() {
    println!("Hello");
}

pub fn generic_function<T: std::fmt::Display>(value: T) {
    println!("Hello");
}

pub fn lifetime_function<'a>(s: &'a str) -> &'a str {
    println!("Hello");
    s
}

pub unsafe fn unsafe_function(ptr: *const i32) {
    println!("Hello");
}

pub const fn const_function(x: i32) -> i32 {
    println!("Hello");
    x * 2
}

pub async fn async_function() {
    println!("Hello");
}

pub fn where_clause_function<T>(value: T) where T: Clone + std::fmt::Debug {
    println!("Hello");
}

#[unsafe(no_mangle)]
pub extern "C" fn ffi_function(x: c_int) -> c_int {
    println!("Hello");
    x
}

#[inline(always)]
#[must_use]
pub fn attributed_function() -> i32 {
    println!("Hello");
    42
}

pub fn complex_return() -> Result<Vec<Option<String>>, Box<dyn std::error::Error>> {
    println!("Hello");
    Ok(vec![])
}

pub fn multi_lifetime<'a, 'b>(x: &'a mut i32, y: &'b str) -> &'a i32 {
    println!("Hello");
    x
}

pub unsafe extern "system" fn system_abi_function(code: i32) {
    println!("Hello");
}

#[allow(clippy::all)]
pub fn complex_generic<T, U>(first: T, second: U) -> String
where
    T: std::fmt::Display + Clone,
    U: std::fmt::Debug + Send + Sync,
{
    println!("Hello");
    format!("{}", first)
}

// External function declarations (no body) - typically used for FFI
extern "C" {
    pub fn external_c_function(x: c_int) -> c_int;

    unsafe fn unsafe_external_function(ptr: *mut u8, len: usize);

    static EXTERNAL_GLOBAL: c_int;

    fn printf(format: *const u8, ...) -> c_int;
}

// Extern block with different ABI
extern "system" {
    pub fn system_api_call(code: u32) -> i32;
}
