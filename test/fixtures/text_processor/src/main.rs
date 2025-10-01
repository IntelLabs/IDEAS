use std::env;
use std::io::{self, Read};
use std::process;

fn main() {
    let args: Vec<String> = env::args().collect();

    // Skip the program name, we need exactly 1 or 2 arguments
    let cmd_args = &args[1..];

    if cmd_args.is_empty() {
        eprintln!("Error: Missing required arguments");
        eprintln!("Modes: upper, lower, reverse, count");
        process::exit(1);
    }

    if cmd_args.len() > 2 {
        eprintln!("Error: Too many arguments (expected 1-2, got {})", cmd_args.len());
        process::exit(1);
    }

    let mode = &cmd_args[0];
    let separator = if cmd_args.len() == 2 {
        &cmd_args[1]
    } else {
        "\n"
    };

    // Read from stdin
    let mut input = String::new();
    match io::stdin().read_to_string(&mut input) {
        Ok(_) => {},
        Err(e) => {
            eprintln!("Error reading from stdin: {}", e);
            process::exit(1);
        }
    }

    // Remove trailing newline if present
    if input.ends_with('\n') {
        input.pop();
    }

    // Process based on mode
    let result = match mode.as_str() {
        "upper" => input.to_uppercase(),
        "lower" => input.to_lowercase(),
        "reverse" => input.chars().rev().collect(),
        "count" => {
            let word_count = input.split_whitespace().count();
            let char_count = input.chars().count();
            format!("Words: {}, Characters: {}", word_count, char_count)
        },
        _ => {
            eprintln!("Error: Unknown mode '{}'", mode);
            eprintln!("Available modes: upper, lower, reverse, count");
            process::exit(1);
        }
    };

    print!("{}{}", result, separator);
}
