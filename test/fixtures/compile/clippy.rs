use std::collections::HashMap;

// Correctness violations
fn correctness_invalid_regex() {
    let _ = regex::Regex::new("[").unwrap(); // invalid regex
}

fn correctness_out_of_bounds_indexing() {
    let v = vec![1, 2, 3];
    let _ = v[10]; // out of bounds
}

fn correctness_float_cmp() -> bool {
    let x = 1.0;
    let y = 2.0;
    x == y // float comparison
}

fn correctness_clone_on_copy() {
    let x = 42i32;
    let _ = x.clone(); // cloning Copy type
}

fn correctness_redundant_clone() {
    let s = String::from("hello");
    let _ = s.clone().len(); // redundant clone
}

// Suspicious violations
fn suspicious_empty_loop() {
    loop {} // empty infinite loop
}

fn suspicious_suspicious_else_formatting() -> i32 {
    let x = 5;
    if x > 0 { 1 }
    else
    { 0 } // suspicious else formatting
}

fn suspicious_assign_op_pattern() {
    let mut x = 5;
    x = x + 1; // should use +=
}

fn suspicious_inefficient_to_string() {
    let s = "hello";
    let _ = s.to_string(); // should use to_owned() for &str
}

fn suspicious_single_char_pattern() {
    let s = "hello world";
    let _ = s.split("l"); // single char should use char literal
}

// Complexity violations
fn complexity_too_many_arguments(a: i32, b: i32, c: i32, d: i32, e: i32, f: i32, g: i32, h: i32) {
    println!("{} {} {} {} {} {} {} {}", a, b, c, d, e, f, g, h);
}

fn complexity_cognitive_complexity() -> i32 {
    let mut result = 0;
    for i in 0..10 {
        if i % 2 == 0 {
            for j in 0..5 {
                if j > 2 {
                    if i > 5 {
                        result += 1;
                        if result > 10 {
                            break;
                        }
                    }
                }
            }
        }
    }
    result
}

fn complexity_type_complexity() -> Result<HashMap<String, Vec<Option<Result<i32, String>>>>, Box<dyn std::error::Error>> {
    Ok(HashMap::new())
}

fn complexity_cyclomatic_complexity(x: i32) -> i32 {
    if x > 0 {
        if x > 10 {
            if x > 20 {
                if x > 30 {
                    if x > 40 {
                        50
                    } else { 40 }
                } else { 30 }
            } else { 20 }
        } else { 10 }
    } else { 0 }
}

// Performance violations
fn perf_unnecessary_to_owned() {
    let s = String::from("hello");
    let _ = s.as_str().to_owned(); // unnecessary
}

fn perf_string_add_assign() {
    let mut s = String::new();
    s = s + "hello"; // inefficient, should use push_str
}

fn perf_vec_init_then_push() {
    let mut v = Vec::new();
    v.push(1);
    v.push(2);
    v.push(3); // should use vec! macro
}

fn perf_iter_nth_zero() {
    let v = vec![1, 2, 3, 4, 5];
    let _ = v.iter().nth(0); // should use .first()
}

fn perf_large_stack_arrays() {
    let _large_array = [0u8; 512 * 1024]; // large stack allocation
}

// Style violations
fn style_needless_return() -> i32 {
    return 42; // needless return
}

fn style_single_match() -> String {
    let x = Some(42);
    match x {
        Some(n) => n.to_string(),
        None => "none".to_string(),
    } // should use if let
}

fn style_redundant_field_names() {
    let name = "test".to_string();
    let value = 42;
    let _s = MyStruct {
        name: name,  // redundant field name
        value: value // redundant field name
    };
}

struct MyStruct {
    name: String,
    value: i32,
}

fn style_unnecessary_mut() {
    let mut x = 5; // unnecessary mut
    println!("{}", x);
}

fn style_collapsible_if() {
    let x = 5;
    if x > 0 {
        if x < 10 { // collapsible
            println!("between 0 and 10");
        }
    }
}

fn style_len_zero() {
    let v = vec![1, 2, 3];
    if v.len() == 0 { // should use is_empty()
        println!("empty");
    }
}

fn style_redundant_closure() {
    let v = vec![1, 2, 3];
    let _: Vec<String> = v.iter().map(|x| x.to_string()).collect(); // redundant closure
}

fn style_manual_map() -> Option<i32> {
    let x = Some(5);
    match x {
        Some(val) => Some(val * 2),
        None => None, // manual map
    }
}

// Additional violations
fn style_unnecessary_wraps() -> Option<i32> {
    Some(42) // always returns Some
}

fn perf_useless_vec() {
    for item in vec![1, 2, 3].iter() { // useless vec
        println!("{}", item);
    }
}
