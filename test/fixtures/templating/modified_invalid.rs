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
