fn no_attributes() {
    println!("No attributes");
}

#[no_mangle]
fn needs_unsafe_single() {
    println!("Single no_mangle");
}

#[no_mangle] #[inline]
fn needs_unsafe_same_line() {
    println!("Multiple on same line");
}

#[inline]
#[no_mangle]
fn needs_unsafe_different_lines() {
    println!("Different lines");
}

#[inline]
#[no_mangle]
#[must_use]
fn needs_unsafe_between() {
    println!("In between lines");
}

#[inline]
#[no_mangle] #[must_use]
fn needs_unsafe_irregular_1_2() {
    println!("Irregular pattern");
}

#[inline] #[no_mangle]
#[must_use]
fn needs_unsafe_irregular_2_1() {
    println!("Irregular pattern");
}

#[unsafe(no_mangle)]
fn already_safe() {
    println!("Already has unsafe");
}

#[inline] #[unsafe(no_mangle)]
fn already_safe_with_others() {
    println!("Already safe with others");
}

#[inline] #[must_use]
fn other_attributes_only() {
    println!("Has others only");
}

#[inline] #[no_mangle] #[must_use]
fn needs_unsafe_three_same_line() {
    println!("Middle of line");
}

#[no_mangle]
pub extern "C" fn extern_c_function() {
    println!("Extern C function");
}

#[no_mangle]
extern "C" fn extern_c_with_args(arg1: i32, arg2: f64) -> i32 {
    println!("Extern C with args: {}, {}", arg1, arg2);
    arg1 + arg2 as i32
}

pub mod foo {
    #[no_mangle]
    pub fn namespaced_function() {
        println!("Namespaced function");
    }
}
