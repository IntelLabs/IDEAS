//! `--kinds function` must remove exactly the functions rustc reports as never used: never a live
//! one (the crate still builds and its tests still pass), and every dead one (none is reported
//! after), wherever it is declared and whatever surrounds it

mod common;

use std::{collections::BTreeSet, fs, path::Path};

use cargo_metadata::Message;
use common::{cargo, minify, scratch};

/// The functions rustc reports as never used in either library build
fn unused_functions(root: &Path) -> BTreeSet<String> {
    let mut unused = BTreeSet::new();
    for profile in ["dev", "test"] {
        let args = [
            "check",
            "--lib",
            "--profile",
            profile,
            "--message-format=json",
        ];
        let stdout = cargo(root, &args).stdout;
        for message in Message::parse_stream(stdout.as_slice()) {
            if let Message::CompilerMessage(message) = message.unwrap() {
                let message = message.message.message;
                if let Some(ident) = message.strip_prefix("function `") {
                    unused.insert(ident.strip_suffix("` is never used").unwrap().to_owned());
                }
            }
        }
    }
    unused
}

/// The unit tests of the library, which must all pass
fn passing_tests(root: &Path) -> String {
    cargo(root, &["test", "--lib", "--quiet"]);
    let out = cargo(root, &["test", "--lib", "--quiet", "--", "--list"]);
    String::from_utf8(out.stdout).unwrap()
}

/// Minifies `before` into exactly `after`, leaving only the `kept` functions reported as unused
fn assert_minifies(name: &str, before: &str, after: &str, kept: &[&str]) {
    assert_minifies_files(name, &[("src/lib.rs", before, after)], kept);
}

/// Minifies each file from its `before` into exactly its `after`, where a `Cargo.toml` replaces
/// the default manifest, leaving only the `kept` functions reported as unused
fn assert_minifies_files(name: &str, files: &[(&str, &str, &str)], kept: &[&str]) {
    let root = scratch(&format!("functions-{name}"));
    let manifest = "[package]\nname = \"p\"\nedition = \"2024\"\n";
    fs::write(root.join("Cargo.toml"), manifest).unwrap();
    for (path, before, _) in files {
        let path = root.join(path);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, before).unwrap();
    }
    let tests = passing_tests(&root);

    minify(&root, &["--kinds", "function"]);

    for (path, _, after) in files {
        assert_eq!(
            fs::read_to_string(root.join(path)).unwrap(),
            *after,
            "{path}"
        );
    }
    assert_eq!(passing_tests(&root), tests);
    let kept = kept.iter().map(|&ident| ident.to_owned()).collect();
    assert_eq!(unused_functions(&root), kept);

    let out = minify(&root, &["--kinds", "function"]);
    assert!(String::from_utf8_lossy(&out.stderr).contains("no unused code"));
}

#[test]
fn name_shared_with_a_live_function_in_a_module() {
    assert_minifies(
        "mod",
        r#"pub fn live() -> i32 { helper() + inner::run() }
fn helper() -> i32 { 1 }
mod inner {
    fn helper() -> i32 { 2 }
    pub fn run() -> i32 { 3 }
}
"#,
        r#"pub fn live() -> i32 { helper() + inner::run() }
fn helper() -> i32 { 1 }
mod inner {
    pub fn run() -> i32 { 3 }
}
"#,
        &[],
    );
}

#[test]
fn name_shared_with_a_live_function_declared_first() {
    assert_minifies(
        "order",
        r#"mod inner {
    pub fn helper() -> i32 { 2 }
}
fn helper() -> i32 { 1 }
pub fn live() -> i32 { inner::helper() }
"#,
        r#"mod inner {
    pub fn helper() -> i32 { 2 }
}
pub fn live() -> i32 { inner::helper() }
"#,
        &[],
    );
}

#[test]
fn every_dead_function_sharing_a_name() {
    assert_minifies(
        "same-name",
        r#"mod a {
    mod b {
        fn dead() {}
        pub fn live() -> i32 { 1 }
    }
    pub fn live() -> i32 { b::live() }
    fn dead() {}
}
pub fn live() -> i32 { a::live() }
fn dead() {}
"#,
        r#"mod a {
    mod b {
        pub fn live() -> i32 { 1 }
    }
    pub fn live() -> i32 { b::live() }
}
pub fn live() -> i32 { a::live() }
"#,
        &[],
    );
}

#[test]
fn functions_nested_in_function_bodies() {
    assert_minifies(
        "nested",
        r#"fn helper() -> i32 { 1 }
pub fn live() -> i32 {
    fn helper() -> i32 { 2 }
    fn used() -> i32 { 3 }
    const INNER: i32 = 4;
    used()
}
"#,
        r#"pub fn live() -> i32 {
    fn used() -> i32 { 3 }
    const INNER: i32 = 4;
    used()
}
"#,
        &[],
    );
}

#[test]
fn transitively_dead_functions() {
    assert_minifies(
        "transitive",
        r#"fn a() -> i32 { b() }
fn b() -> i32 { c() }
fn c() -> i32 { 1 }
fn d() -> i32 { 2 }
pub fn live() -> i32 { d() }
"#,
        r#"fn d() -> i32 { 2 }
pub fn live() -> i32 { d() }
"#,
        &[],
    );
}

#[test]
fn functions_sharing_a_line() {
    assert_minifies(
        "line",
        "fn a() {} pub fn live() {} fn b() {}\nfn c() {} fn d() {}\n",
        "pub fn live() {} ",
        &[],
    );
}

#[test]
fn attributes_and_comments_go_with_their_function() {
    assert_minifies(
        "attributes",
        r#"pub fn live() {}

/// Documented
#[inline]
#[allow(clippy::all)]
fn dead() {}

// A comment of its own
pub fn live2() {}
"#,
        r#"pub fn live() {}


// A comment of its own
pub fn live2() {}
"#,
        &[],
    );
}

#[test]
fn only_the_dead_one_of_cfg_twins() {
    assert_minifies(
        "cfg",
        r#"#[cfg(unix)]
fn twin() {}
#[cfg(not(unix))]
fn twin() {}
pub fn live() {}
"#,
        r#"#[cfg(not(unix))]
fn twin() {}
pub fn live() {}
"#,
        &[],
    );
}

#[test]
fn not_a_function_only_unit_tests_use() {
    let source = r#"fn helper() -> i32 { 1 }
pub fn live() {}
#[cfg(test)]
mod tests {
    #[test]
    fn t() { assert_eq!(super::helper(), 1); }
}
"#;
    assert_minifies("unit-tests", source, source, &["helper"]);
}

#[test]
fn not_a_function_only_unit_tests_leave_dead() {
    let source = r#"fn helper() -> i32 { 1 }
#[cfg(not(test))]
pub fn live() -> i32 { helper() }
"#;
    assert_minifies("not-unit-tests", source, source, &["helper"]);
}

#[test]
fn non_ascii_text() {
    assert_minifies(
        "unicode",
        "// ü\npub fn live() -> &'static str { \"é😀\" }\nfn dead() -> &'static str { \"日本\" } fn dead2() {}\npub fn live2() {}\n",
        "// ü\npub fn live() -> &'static str { \"é😀\" }\npub fn live2() {}\n",
        &[],
    );
}

#[test]
fn crlf_line_endings() {
    let crlf = |code: &str| code.replace('\n', "\r\n");
    assert_minifies(
        "crlf",
        &crlf(
            "pub fn live() -> i32 { 1 }\nfn dead() {}\n/// ü\nfn dead2() {}\npub fn live2() {}\n",
        ),
        &crlf("pub fn live() -> i32 { 1 }\npub fn live2() {}\n"),
        &[],
    );
}

#[test]
fn foreign_functions_and_their_emptied_block() {
    assert_minifies(
        "foreign",
        r#"unsafe extern "C" {
    fn dead_ext();
}
unsafe extern "C" {
    pub safe fn abs(x: i32) -> i32;
    fn labs(x: i64) -> i64;
}
pub fn live() -> i32 { abs(-1) }
"#,
        r#"unsafe extern "C" {
    pub safe fn abs(x: i32) -> i32;
}
pub fn live() -> i32 { abs(-1) }
"#,
        &[],
    );
}

#[test]
fn every_flavor_of_function() {
    assert_minifies(
        "flavors",
        r#"unsafe extern "C" fn c() {}
extern "C-unwind" fn c_unwind() {}
const fn konst() -> i32 { 1 }
async fn not_awaited() {}
unsafe fn raw(p: *const i32) -> i32 { unsafe { *p } }
fn generic<T: Clone>(t: &T) -> T where T: Default { t.clone() }
fn r#type() {}
pub fn live() {}
"#,
        "pub fn live() {}\n",
        &[],
    );
}

#[test]
fn not_exported_or_otherwise_used_functions() {
    let source = r#"#[unsafe(no_mangle)]
extern "C" fn no_mangle() {}
#[unsafe(export_name = "exported")]
extern "C" fn export_name() {}
fn as_pointer(x: i32) -> i32 { x }
pub fn live() -> fn(i32) -> i32 { as_pointer }
#[unsafe(no_mangle)]
pub static CALLBACK: extern "C" fn() = no_mangle;
"#;
    assert_minifies("used", source, source, &[]);
}

#[test]
fn not_other_kinds_of_unused_code() {
    let source = r#"pub trait T { fn method(&self) {} }
struct S;
impl S { fn associated() {} }
const C: i32 = 1;
static S2: i32 = 2;
macro_rules! generate { () => { fn generated() {} } }
generate!();
pub fn live() {}
"#;
    assert_minifies("kinds", source, source, &["generated"]);
}

#[test]
fn live_functions_keep_their_behavior() {
    assert_minifies(
        "behavior",
        r#"fn dead() -> i32 { 0 }
fn helper(x: i32) -> i32 { x * 2 }
pub fn live(x: i32) -> i32 { helper(x) + 1 }
#[cfg(test)]
mod tests {
    #[test]
    fn live() { assert_eq!(super::live(20), 41); }
}
"#,
        r#"fn helper(x: i32) -> i32 { x * 2 }
pub fn live(x: i32) -> i32 { helper(x) + 1 }
#[cfg(test)]
mod tests {
    #[test]
    fn live() { assert_eq!(super::live(20), 41); }
}
"#,
        &[],
    );
}

#[test]
fn nothing_outside_the_library() {
    let root = scratch("functions-library");
    let manifest = "[package]\nname = \"p\"\nedition = \"2024\"\n";
    fs::write(root.join("Cargo.toml"), manifest).unwrap();
    for dir in ["src", "tests", "examples"] {
        fs::create_dir(root.join(dir)).unwrap();
    }
    let files = [
        ("src/lib.rs", "pub fn live() {}\nfn dead() {}\n"),
        ("src/main.rs", "fn main() {}\nfn dead_in_bin() {}\n"),
        ("tests/t.rs", "fn dead_in_test() {}\n"),
        ("examples/e.rs", "fn main() {}\nfn dead_in_example() {}\n"),
    ];
    for (path, code) in files {
        fs::write(root.join(path), code).unwrap();
    }

    minify(&root, &["--kinds", "function"]);

    for (path, code) in files {
        let expected = if path == "src/lib.rs" {
            "pub fn live() {}\n"
        } else {
            code
        };
        assert_eq!(
            fs::read_to_string(root.join(path)).unwrap(),
            expected,
            "{path}"
        );
    }
    cargo(&root, &["build", "--all-targets", "--quiet"]);
}

#[test]
fn in_every_kind_of_module_file() {
    assert_minifies_files(
        "module-files",
        &[
            (
                "src/lib.rs",
                "mod a;\nmod gone;\n#[path = \"elsewhere/c.rs\"]\nmod c;\npub fn live() -> i32 { a::live() + c::live() }\n",
                "mod a;\nmod gone;\n#[path = \"elsewhere/c.rs\"]\nmod c;\npub fn live() -> i32 { a::live() + c::live() }\n",
            ),
            (
                "src/a/mod.rs",
                "mod b;\npub fn live() -> i32 { b::live() }\nfn dead_a() {}\n",
                "mod b;\npub fn live() -> i32 { b::live() }\n",
            ),
            (
                "src/a/b.rs",
                "fn dead_b() {}\npub fn live() -> i32 { 1 }\n",
                "pub fn live() -> i32 { 1 }\n",
            ),
            (
                "src/elsewhere/c.rs",
                "pub fn live() -> i32 { 2 }\nfn dead_c() {}\n",
                "pub fn live() -> i32 { 2 }\n",
            ),
            ("src/gone.rs", "fn dead() {}\npub fn dead_too() {}\n", ""),
        ],
        &[],
    );
}

#[test]
fn in_included_files() {
    assert_minifies_files(
        "include",
        &[
            (
                "src/lib.rs",
                "include!(\"included.rs\");\npub fn live() -> i32 { used() }\n",
                "include!(\"included.rs\");\npub fn live() -> i32 { used() }\n",
            ),
            (
                "src/included.rs",
                "fn used() -> i32 { 1 }\nfn dead() {}\n",
                "fn used() -> i32 { 1 }\n",
            ),
        ],
        &[],
    );
}

#[test]
fn recursive_and_mutually_recursive_functions() {
    assert_minifies(
        "recursion",
        r#"fn even(n: u32) -> bool { n == 0 || odd(n - 1) }
fn odd(n: u32) -> bool { n != 0 && even(n - 1) }
fn countdown(n: u32) -> u32 { if n == 0 { 0 } else { countdown(n - 1) } }
fn fact(n: u64) -> u64 { if n == 0 { 1 } else { n * fact(n - 1) } }
pub fn live() -> u64 { fact(5) }
"#,
        r#"fn fact(n: u64) -> u64 { if n == 0 { 1 } else { n * fact(n - 1) } }
pub fn live() -> u64 { fact(5) }
"#,
        &[],
    );
}

#[test]
fn not_what_allowed_or_expected_dead_code_calls() {
    let source = r#"#[allow(dead_code)]
fn allowed() { helper() }
fn helper() {}
#[expect(dead_code)]
fn expected() { helper2() }
fn helper2() {}
pub fn live() {}
"#;
    assert_minifies("allow", source, source, &[]);
}

#[test]
fn public_functions_unreachable_from_outside() {
    assert_minifies(
        "visibility",
        r#"mod private {
    pub fn unreachable() {}
    pub fn reexported() {}
    pub(crate) fn crate_only() {}
    pub(super) fn parent_only() {}
    pub(in crate::private) fn self_only() {}
}
pub use private::reexported;
pub mod public {
    pub fn api() {}
    pub(crate) fn internal() {}
}
"#,
        r#"mod private {
    pub fn reexported() {}
}
pub use private::reexported;
pub mod public {
    pub fn api() {}
}
"#,
        &[],
    );
}

#[test]
fn not_what_closures_trait_impls_or_live_constants_call() {
    let source = r#"fn by_closure() -> i32 { 1 }
fn by_unused_closure() -> i32 { 2 }
fn by_trait_impl() -> i32 { 3 }
const fn by_const() -> i32 { 4 }
fn by_static() {}
pub struct S(pub i32);
impl Default for S { fn default() -> Self { S(by_trait_impl()) } }
pub const C: i32 = by_const();
pub static CALLBACK: fn() = by_static;
pub fn live() -> i32 { let f = || by_closure(); let _g = || by_unused_closure(); f() }
"#;
    assert_minifies("roots", source, source, &[]);
}

#[test]
fn what_only_a_macro_never_invoked_calls() {
    assert_minifies(
        "macro-body",
        r#"fn by_used_macro() -> i32 { 1 }
fn by_unused_macro() -> i32 { 2 }
macro_rules! used { () => { by_used_macro() } }
#[allow(unused_macros)]
macro_rules! unused { () => { by_unused_macro() } }
pub fn live() -> i32 { used!() }
"#,
        r#"fn by_used_macro() -> i32 { 1 }
macro_rules! used { () => { by_used_macro() } }
#[allow(unused_macros)]
macro_rules! unused { () => { by_unused_macro() } }
pub fn live() -> i32 { used!() }
"#,
        &[],
    );
}

#[test]
fn what_only_disabled_features_call() {
    // rustc only sees the enabled features, so the crate no longer builds with the others
    let manifest = "[package]\nname = \"p\"\nedition = \"2024\"\n\n[features]\nextra = []\n";
    assert_minifies_files(
        "features",
        &[
            ("Cargo.toml", manifest, manifest),
            (
                "src/lib.rs",
                "fn helper() -> i32 { 1 }\n#[cfg(feature = \"extra\")]\npub fn extra() -> i32 { helper() }\npub fn live() {}\n",
                "#[cfg(feature = \"extra\")]\npub fn extra() -> i32 { helper() }\npub fn live() {}\n",
            ),
        ],
        &[],
    );
    let root = Path::new(env!("CARGO_TARGET_TMPDIR")).join("functions-features");
    let check = std::process::Command::new("cargo")
        .args(["check", "--quiet", "--features", "extra"])
        .current_dir(root)
        .env_remove("RUSTFLAGS")
        .env_remove("CARGO_TARGET_DIR")
        .output()
        .unwrap();
    assert!(!check.status.success());
    assert!(String::from_utf8_lossy(&check.stderr).contains("cannot find function `helper`"));
}

#[test]
fn not_what_dead_code_of_other_kinds_uses() {
    assert_minifies(
        "other-kinds",
        r#"fn first() -> i32 { helper() }
fn second() -> i32 { 2 }
fn helper() -> i32 { 1 }
const fn konst() -> i32 { 3 }
fn r#static() {}
fn alone() {}
static TABLE: [fn() -> i32; 2] = [first, second];
const C: i32 = konst();
static CALLBACK: fn() = r#static;
pub fn live() {}
"#,
        r#"fn first() -> i32 { helper() }
fn second() -> i32 { 2 }
fn helper() -> i32 { 1 }
const fn konst() -> i32 { 3 }
fn r#static() {}
static TABLE: [fn() -> i32; 2] = [first, second];
const C: i32 = konst();
static CALLBACK: fn() = r#static;
pub fn live() {}
"#,
        &["first", "helper", "konst", "second", "static"],
    );
}

#[test]
fn not_what_dead_code_in_ignored_files_uses() {
    let root = scratch("functions-ignored");
    let manifest = "[package]\nname = \"p\"\nedition = \"2024\"\n";
    fs::write(root.join("Cargo.toml"), manifest).unwrap();
    fs::create_dir(root.join("src")).unwrap();
    let files = [
        (
            "src/lib.rs",
            "mod ignored;\nmod minified;\npub fn live() {}\n",
        ),
        ("src/ignored.rs", "fn dead() { super::minified::used() }\n"),
        ("src/minified.rs", "pub fn used() {}\npub fn alone() {}\n"),
    ];
    for (path, code) in files {
        fs::write(root.join(path), code).unwrap();
    }

    minify(
        &root,
        &["--kinds", "function", "--ignore", "src/ignored.rs"],
    );

    let minified = fs::read_to_string(root.join("src/minified.rs")).unwrap();
    assert_eq!(minified, "pub fn used() {}\n");
    assert_eq!(
        fs::read_to_string(root.join("src/ignored.rs")).unwrap(),
        files[1].1
    );
    cargo(&root, &["build", "--quiet"]);
}

/// Minifies `source` with `args`, after which the crate no longer builds with `error`
fn assert_breaks(name: &str, args: &[&str], source: &str, after: &str, error: &str) {
    let root = scratch(&format!("functions-{name}"));
    let manifest = "[package]\nname = \"p\"\nedition = \"2024\"\n";
    fs::write(root.join("Cargo.toml"), manifest).unwrap();
    fs::create_dir(root.join("src")).unwrap();
    fs::write(root.join("src/lib.rs"), source).unwrap();

    minify(&root, args);

    assert_eq!(fs::read_to_string(root.join("src/lib.rs")).unwrap(), after);
    let check = std::process::Command::new("cargo")
        .args(["check", "--quiet"])
        .current_dir(&root)
        .env_remove("RUSTFLAGS")
        .env_remove("CARGO_TARGET_DIR")
        .output()
        .unwrap();
    assert!(String::from_utf8_lossy(&check.stderr).contains(error));
}

// Upstream removes a dead type and the functions only its impls call, but not the impls, which
// rustc never reports; with only functions, it still removes those the impls call
const DEAD_TYPE: &str = r#"struct Dead;
impl Clone for Dead { fn clone(&self) -> Self { helper(); Dead } }
fn helper() {}
pub fn live() {}
"#;

#[test]
fn breaks_the_impls_of_a_dead_type() {
    let after =
        "impl Clone for Dead { fn clone(&self) -> Self { helper(); Dead } }\npub fn live() {}\n";
    assert_breaks(
        "dead-type",
        &[],
        DEAD_TYPE,
        after,
        "cannot find type `Dead`",
    );
}

#[test]
fn breaks_the_impls_of_a_dead_type_calling_dead_functions() {
    let after = "struct Dead;\nimpl Clone for Dead { fn clone(&self) -> Self { helper(); Dead } }\npub fn live() {}\n";
    let args = ["--kinds", "function"];
    assert_breaks(
        "dead-type-calls",
        &args,
        DEAD_TYPE,
        after,
        "cannot find function `helper`",
    );
}
