//! Helpers shared by the integration tests, each of which uses only some
#![allow(dead_code)]

use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
};

pub fn scratch(name: &str) -> PathBuf {
    let root = Path::new(env!("CARGO_TARGET_TMPDIR")).join(name);
    let _ = fs::remove_dir_all(&root);
    fs::create_dir_all(&root).unwrap();
    root
}

pub fn run(cmd: &mut Command) -> Output {
    // Neither the caller's warning levels nor its target directory may leak into the fixtures
    let out = cmd
        .env_remove("RUSTFLAGS")
        .env_remove("CARGO_ENCODED_RUSTFLAGS")
        .env_remove("CARGO_TARGET_DIR")
        .output()
        .unwrap();
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(out.status.success(), "{cmd:?} failed:\n{stderr}");
    assert!(!stderr.contains("panicked"), "{cmd:?} panicked:\n{stderr}");
    out
}

pub fn cargo(root: &Path, args: &[&str]) -> Output {
    run(Command::new("cargo").args(args).current_dir(root))
}

pub fn minify(root: &Path, args: &[&str]) -> Output {
    run(Command::new(env!("CARGO_BIN_EXE_cargo-minify"))
        .args([
            "minify",
            "--apply",
            "--allow-dirty",
            "--allow-staged",
            "--allow-no-vcs",
            "--color=never",
        ])
        .args(args)
        .current_dir(root))
}

/// `cargo-minify minify` in `root`, unaffected by the user's git configuration
pub fn cargo_minify(root: &Path) -> Command {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_cargo-minify"));
    cmd.arg("minify")
        .current_dir(root)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env_remove("RUSTFLAGS")
        .env_remove("CARGO_ENCODED_RUSTFLAGS")
        .env_remove("CARGO_TARGET_DIR");
    cmd
}

/// The exit code, stdout and stderr of `cmd`, which must not panic
pub fn outcome(cmd: &mut Command) -> (i32, String, String) {
    let out = cmd.output().unwrap();
    let stdout = String::from_utf8(out.stdout).unwrap();
    let stderr = String::from_utf8(out.stderr).unwrap();
    assert!(!stderr.contains("panicked"), "{cmd:?} panicked:\n{stderr}");
    (out.status.code().unwrap(), stdout, stderr)
}

pub const LIB_RS: &str = "mod m;\n\npub fn live() -> i32 {\n    m::used()\n}\n\nfn dead() {}\n";
pub const M_RS: &str = "pub fn used() -> i32 {\n    1\n}\n\npub fn unused() {}\n";

/// A library crate in `root` with one dead function in each of its two files
pub fn dead_code_crate(root: &Path) {
    fs::write(
        root.join("Cargo.toml"),
        "[package]\nname = \"p\"\nedition = \"2021\"\n",
    )
    .unwrap();
    fs::create_dir_all(root.join("src")).unwrap();
    fs::write(root.join("src/lib.rs"), LIB_RS).unwrap();
    fs::write(root.join("src/m.rs"), M_RS).unwrap();
    fs::write(root.join(".gitignore"), "/target\n").unwrap();
    cargo(root, &["generate-lockfile", "--offline"]);
}
