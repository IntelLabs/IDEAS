#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from ideas import extract_rust


def test_extract_complete_rust_block():
    snippet = """Here's some code:
```rust
fn main() {
    println!("Hello, world!");
}
```
That's it."""

    result = extract_rust(snippet)
    expected = 'fn main() {\n    println!("Hello, world!");\n}'
    assert result == expected


def test_extract_open_rust_block_to_end():
    test = """Here's some code:
```rust
enum Color {
    Red,
    Green,
    Blue,
}"""

    result = extract_rust(test)
    expected = "enum Color {\n    Red,\n    Green,\n    Blue,\n}"
    assert result == expected


def test_extract_complete_generic_block():
    test = """Here's some code:
```
let x = 5;
let y = 10;
println!("{}", x + y);
```
That's it."""

    result = extract_rust(test)
    expected = 'let x = 5;\nlet y = 10;\nprintln!("{}", x + y);'
    assert result == expected


def test_extract_open_generic_block_to_end():
    test = """Here's some code:
```
fn fibonacci(n: u32) -> u32 {
    if n <= 1 {
        n
    } else {
        fibonacci(n - 1) + fibonacci(n - 2)
    }
}"""

    result = extract_rust(test)
    expected = "fn fibonacci(n: u32) -> u32 {\n    if n <= 1 {\n        n\n    } else {\n        fibonacci(n - 1) + fibonacci(n - 2)\n    }\n}"
    assert result == expected


def test_extract_no_code_blocks():
    test = """This is just regular text with no code blocks at all."""

    result = extract_rust(test)
    expected = test
    assert result == expected


def test_extract_multiple_code_blocks():
    test = """First block:
```rust
fn first() {
    println!("First");
}
```

Second block:
```rust
fn second() {
    println!("Second");
}
```"""

    result = extract_rust(test)
    expected = 'fn second() {\n    println!("Second");\n}'
    assert result == expected


def test_extract_multiple_different_code_blocks():
    test = """First block:
```rust
fn first() {
    println!("First");
}
```

Second block:
```c++
#include <stdio.h>
#include <stdlib.h>
```

Third block:
```
fn third() {
    println!("Third");
}
```"""

    result = extract_rust(test)
    expected = 'fn third() {\n    println!("Third");\n}'
    assert result == expected


def test_extract_backticks_within_code():
    test = """Here's some code:
```rust
let template = "Use `backticks` for code";
println!("{}", template);
```"""

    result = extract_rust(test)
    expected = 'let template = "Use `backticks` for code";\nprintln!("{}", template);'
    assert result == expected


def test_extract_empty_code_block():
    test = """Empty block:
```rust
```
Text after."""

    result = extract_rust(test)
    expected = test  # Empty codeblocks are ignored
    assert result == expected


def test_extract_sameline_end_code_block():
    test = """Ending backticks on the same row:
```rust
let template = "This is a test";```
Text after."""

    result = extract_rust(test)
    expected = 'let template = "This is a test";'
    assert result == expected
