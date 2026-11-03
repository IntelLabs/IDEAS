use ::x as _;

unsafe extern "C" {
    fn add(a: i32, b: i32) -> i32;
    fn bump() -> i32;
}

fn dead_in_tests() {}

#[test]
fn adds() {
    assert_eq!(unsafe { add(2, -3) }, 5);
}

#[test]
fn bumps() {
    assert!(unsafe { bump() } > 0);
}
