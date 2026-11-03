//! Which crates get minified, depending on where it runs and how packages are selected

mod common;

use std::{collections::BTreeSet, fs, path::Path};

use common::{cargo, cargo_minify, minify, outcome, scratch};

/// A library crate `name` in `dir` with one dead function, using the crates it depends on
fn library(dir: &Path, name: &str, dependencies: &[(&str, &str)]) {
    let mut manifest =
        format!("[package]\nname = \"{name}\"\nedition = \"2021\"\n\n[dependencies]\n");
    let mut uses = String::new();
    for (dependency, path) in dependencies {
        manifest += &format!("{dependency} = {{ path = \"{path}\" }}\n");
        uses += &format!("    {dependency}::live();\n");
    }
    fs::create_dir_all(dir.join("src")).unwrap();
    fs::write(dir.join("Cargo.toml"), manifest).unwrap();
    let lib_rs = format!("pub fn live() {{\n{uses}}}\n\nfn dead() {{}}\n");
    fs::write(dir.join("src/lib.rs"), lib_rs).unwrap();
}

/// A virtual workspace where `a` depends on `b`, and both on `ext` from outside of it
fn workspace(name: &str) -> std::path::PathBuf {
    let root = scratch(&format!("workspace-{name}"));
    library(&root.join("ext"), "ext", &[]);
    let ws = root.join("ws");
    library(&ws.join("a"), "a", &[("b", "../b"), ("ext", "../../ext")]);
    library(&ws.join("b"), "b", &[("ext", "../../ext")]);
    let manifest = "[workspace]\nmembers = [\"a\", \"b\"]\nresolver = \"2\"\n";
    fs::write(ws.join("Cargo.toml"), manifest).unwrap();
    cargo(&ws, &["generate-lockfile", "--offline"]);
    ws
}

/// The crates of the workspace whose dead function is gone
fn minified(ws: &Path) -> BTreeSet<&'static str> {
    [("a", "a"), ("b", "b"), ("ext", "../ext")]
        .into_iter()
        .filter(|(_, dir)| {
            let lib_rs = fs::read_to_string(ws.join(dir).join("src/lib.rs")).unwrap();
            !lib_rs.contains("fn dead")
        })
        .map(|(name, _)| name)
        .collect()
}

fn assert_minifies(name: &str, cwd: &str, args: &[&str], expected: &[&str]) {
    let ws = workspace(name);
    let out = minify(&ws.join(cwd), args);
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert_eq!(
        minified(&ws),
        expected.iter().copied().collect(),
        "{stdout}"
    );
}

#[test]
fn at_the_root_of_a_workspace_every_member() {
    assert_minifies("root", "", &[], &["a", "b"]);
}

#[test]
fn inside_a_member_none_of_the_workspace() {
    // Upstream resolves rustc's paths, which are relative to the workspace root, from the current
    // directory instead; only crates from outside of the workspace have absolute paths
    assert_minifies("member", "a", &[], &[]);
    assert_minifies("member-package", "a", &["-p", "a"], &[]);
    assert_minifies("member-workspace", "b", &["--workspace"], &["ext"]);
}

#[test]
fn with_a_manifest_path_only_its_package() {
    assert_minifies(
        "manifest-path",
        "",
        &["--manifest-path", "b/Cargo.toml"],
        &["b"],
    );
}

#[test]
fn the_workspace_along_with_its_path_dependencies_from_outside_of_it() {
    assert_minifies("workspace", "", &["--workspace"], &["a", "b", "ext"]);
}

#[test]
fn the_workspace_except_excluded_packages() {
    assert_minifies(
        "exclude",
        "",
        &["--workspace", "--exclude", "b"],
        &["a", "ext"],
    );
    assert_minifies(
        "exclude-glob",
        "",
        &["--workspace", "--exclude", "[ab]"],
        &[],
    );
    assert_minifies(
        "exclude-ext",
        "",
        &["--workspace", "--exclude", "e*"],
        &["a", "b"],
    );
}

#[test]
fn only_the_selected_packages() {
    assert_minifies("package", "", &["-p", "b"], &["b"]);
    assert_minifies("packages", "", &["-p", "a", "--package", "b"], &["a", "b"]);
}

#[test]
fn rejects_packages_outside_of_the_workspace() {
    let ws = workspace("not-a-member");
    let (code, stdout, stderr) = outcome(cargo_minify(&ws).args(["-p", "ext", "-p", "a"]));
    assert_eq!((code, stdout.as_str()), (3, ""));
    assert_eq!(
        stderr,
        "IO error: package `ext` is not a member of the workspace\n"
    );
    assert!(minified(&ws).is_empty());
}

#[test]
fn a_manifest_path_to_a_virtual_workspace_finds_nothing() {
    // Upstream compares the workspace root with the manifest file rather than its directory
    let ws = workspace("virtual-manifest");
    let args = ["--manifest-path", "Cargo.toml", "--color=never"];
    let (code, stdout, stderr) = outcome(cargo_minify(&ws).args(args));
    assert_eq!(code, 0);
    assert_eq!(stdout, "");
    assert_eq!(
        stderr,
        "crate resolution found no targets\nno unused code that can be minified\n"
    );
}

#[test]
fn fails_without_a_manifest() {
    let root = scratch("workspace-no-manifest");
    let args = ["--manifest-path", "nowhere/Cargo.toml"];
    let (code, stdout, stderr) = outcome(cargo_minify(&root).args(args));
    assert_eq!((code, stdout.as_str()), (3, ""));
    let error = "IO error: `cargo metadata` exited with an error: ";
    assert!(stderr.starts_with(error), "{stderr}");
}
