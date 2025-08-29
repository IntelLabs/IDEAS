fn add(a: i32, b: i32) -> i32 {
    a + b
}

pub fn multiply(x: f64, y: f64) -> f64 {
    x * y
}

fn identity<T>(value: T) -> T {
    value
}

trait Calculator {
    fn calculate(&self, a: i32, b: i32) -> i32;
    fn reset(&mut self);
}

extern "C" {
    fn printf(format: *const i8, ...) -> c_int;
    fn malloc(size: usize) -> *mut u8;
}
