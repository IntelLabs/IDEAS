//! Minifies Rust 2024 crates that export a C interface, as C-to-Rust translations ship them: full
//! of `unsafe(...)` attributes, `unsafe extern` blocks and private but exported items

mod common;

use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
    process::Command,
};

use common::{cargo, minify, run, scratch};

const FIXTURES: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures");

const DEAD_FN: &str = "unsafe extern \"C\" fn dead_scaffolding() {}";

fn copy_dir(from: &Path, to: &Path) {
    for entry in fs::read_dir(from).unwrap() {
        let entry = entry.unwrap();
        let target = to.join(entry.file_name());
        if entry.file_type().unwrap().is_dir() {
            fs::create_dir_all(&target).unwrap();
            copy_dir(&entry.path(), &target);
        } else {
            fs::copy(entry.path(), target).unwrap();
        }
    }
}

fn files(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
    fn walk(root: &Path, dir: &Path, out: &mut BTreeMap<PathBuf, Vec<u8>>) {
        for entry in fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                if path != root.join("target") {
                    walk(root, &path, out);
                }
            } else {
                let content = fs::read(&path).unwrap();
                out.insert(path.strip_prefix(root).unwrap().to_path_buf(), content);
            }
        }
    }
    let mut out = BTreeMap::new();
    walk(root, root, &mut out);
    out
}

// Removed items leave blank lines behind, which rustfmt is there for
fn without_blank_lines(code: &str) -> String {
    code.lines()
        .filter(|line| !line.trim().is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

fn ffi_workspace(name: &str) -> PathBuf {
    let root = scratch(name);
    copy_dir(&Path::new(FIXTURES).join("ffi"), &root);
    cargo(&root, &["generate-lockfile", "--offline"]);
    root
}

// Only what translations ship: the -ffi crate's own sources, not its tests nor its dependencies
const FFI_ARGS: &[&str] = &[
    "--manifest-path=libx-ffi/Cargo.toml",
    "--package",
    "libx-ffi",
    "--file",
    "libx-ffi/src/**",
];

#[cfg(target_os = "linux")]
fn exports(root: &Path) -> BTreeSet<String> {
    cargo(root, &["build", "--quiet", "--package", "libx-ffi"]);
    let nm = run(Command::new("nm")
        .args(["--dynamic", "--defined-only"])
        .arg(root.join("target/debug/libx.so")));
    String::from_utf8(nm.stdout)
        .unwrap()
        .lines()
        .filter_map(|line| line.split_whitespace().nth(2))
        .map(str::to_owned)
        .collect()
}

#[test]
fn ffi_crate_loses_exactly_its_dead_code() {
    let root = ffi_workspace("ffi");
    #[cfg(target_os = "linux")]
    let exported = {
        let exported = exports(&root);
        for name in [
            "add",
            "bump",
            "priv_export",
            "priv_no_mangle",
            "priv_global",
            "uses_mod",
        ] {
            assert!(
                exported.contains(name),
                "`{name}` is not exported: {exported:?}"
            );
        }
        exported
    };
    let before = files(&root);

    minify(&root, FFI_ARGS);

    let lib_rs = Path::new("libx-ffi/src/lib.rs");
    let after = files(&root);
    let changed: BTreeSet<_> = before
        .keys()
        .chain(after.keys())
        .filter(|path| before.get(*path) != after.get(*path))
        .collect();
    assert_eq!(changed, BTreeSet::from([&lib_rs.to_path_buf()]));
    let expected = fs::read_to_string(Path::new(FIXTURES).join("ffi.minified.rs")).unwrap();
    let minified = fs::read_to_string(root.join(lib_rs)).unwrap();
    assert_eq!(
        without_blank_lines(&minified),
        without_blank_lines(&expected)
    );

    #[cfg(target_os = "linux")]
    assert_eq!(exports(&root), exported);
    cargo(&root, &["test", "--quiet", "--package", "libx-ffi"]);
}

#[test]
fn minifying_twice_changes_nothing_more() {
    let root = ffi_workspace("ffi-twice");
    minify(&root, FFI_ARGS);
    let before = files(&root);

    let out = minify(&root, FFI_ARGS);

    assert!(String::from_utf8_lossy(&out.stderr).contains("no unused code"));
    assert!(files(&root) == before, "the second minify changed files");
}

fn assert_keeps(name: &str, form: &str) {
    let root = scratch(&format!("form-{name}"));
    fs::create_dir(root.join("src")).unwrap();
    fs::write(
        root.join("Cargo.toml"),
        "[package]\nname = \"form\"\nversion = \"0.1.0\"\nedition = \"2024\"\n",
    )
    .unwrap();
    let lib_rs = root.join("src/lib.rs");
    fs::write(&lib_rs, format!("{form}\n\n{DEAD_FN}\n")).unwrap();

    minify(&root, &[]);

    assert_eq!(fs::read_to_string(lib_rs).unwrap().trim(), form);
}

// Every Rust 2024 `unsafe` form an exported C interface may hold, each next to a dead function
macro_rules! unsafe_forms {
    ($($name:ident: $form:expr,)*) => {
        mod unsafe_form {
            $(
                #[test]
                fn $name() {
                    super::assert_keeps(stringify!($name), $form);
                }
            )*
        }
    };
}

unsafe_forms! {
    export_name: "#[unsafe(export_name = \"e\")]\npub unsafe extern \"C\" fn e() {}",
    no_mangle: "#[unsafe(no_mangle)]\npub extern \"C\" fn n() {}",
    no_mangle_static_mut: "#[unsafe(no_mangle)]\npub static mut G: i32 = 0;",
    export_name_static: "#[unsafe(export_name = \"s\")]\nstatic S: i32 = 0;",
    link_section: "#[unsafe(link_section = \".data.minify\")]\n#[unsafe(no_mangle)]\n\
        pub static L: i32 = 0;",
    extern_block: "unsafe extern \"C\" {\n    pub safe fn abs(x: i32) -> i32;\n    \
        pub unsafe fn labs(x: i64) -> i64;\n    pub static errno: i32;\n}",
    link_name: "unsafe extern \"C\" {\n    #[link_name = \"abs\"]\n    \
        pub safe fn c_abs(x: i32) -> i32;\n}",
    unsafe_trait: "pub unsafe trait T {}\nunsafe impl T for u8 {}",
    unsafe_impl_send_sync: "pub struct P(*mut u8);\n\
        unsafe impl Send for P {}\nunsafe impl Sync for P {}",
    unsafe_fn: "/// # Safety\n///\n/// `p` must be valid.\n\
        pub unsafe fn f(p: *const i32) -> i32 {\n    // SAFETY: as the caller promised\n    \
        unsafe { *p }\n}",
    raw_ref: "#[unsafe(no_mangle)]\npub extern \"C\" fn raw() -> i32 {\n    \
        static mut C: i32 = 0;\n    // SAFETY: single-threaded\n    \
        unsafe { *&raw mut C += 1 };\n    // SAFETY: as above\n    unsafe { *&raw const C }\n}",
    union: "#[repr(C)]\npub union U {\n    pub a: i32,\n    pub b: f32,\n}\n\
        pub fn read(u: U) -> i32 {\n    // SAFETY: any bits are a valid i32\n    \
        unsafe { u.a }\n}",
    c_unwind: "#[unsafe(no_mangle)]\npub unsafe extern \"C-unwind\" fn unwind() {}",
    fn_pointer: "pub type Callback = Option<unsafe extern \"C\" fn(*mut core::ffi::c_void) -> i32>;",
    unsafe_const: "pub const Z: i32 = unsafe { core::mem::transmute::<u32, i32>(1) };",
    naked: "#[cfg(target_arch = \"x86_64\")]\n#[unsafe(naked)]\n#[unsafe(no_mangle)]\n\
        pub extern \"C\" fn naked() {\n    core::arch::naked_asm!(\"ret\")\n}",
}
