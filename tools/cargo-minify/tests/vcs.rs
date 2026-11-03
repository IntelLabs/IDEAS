//! `--apply` only minifies a crate whose changes version control can undo, unless allowed to

mod common;

use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};

use common::{cargo_minify, dead_code_crate, outcome, run, scratch, LIB_RS};

const NO_VCS: &str = "no VCS found for this package and `cargo minify` can potentially perform \
                      destructive changes; if you'd like to suppress this error pass \
                      `--allow-no-vcs`\n";

fn git(root: &Path, args: &[&str]) {
    run(Command::new("git")
        .args([
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "init.defaultBranch=main",
        ])
        .args(args)
        .current_dir(root)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1"));
}

fn commit_all(root: &Path) {
    git(root, &["add", "-A"]);
    git(root, &["commit", "-qm", "commit"]);
}

/// A repository whose root is a crate with dead code, plus a few text files
fn repository(name: &str) -> PathBuf {
    let root = scratch(name);
    dead_code_crate(&root);
    for file in ["a.txt", "b.txt", "d.txt"] {
        fs::write(root.join(file), "original\n").unwrap();
    }
    git(&root, &["init", "-q"]);
    commit_all(&root);
    root
}

fn edit(root: &Path, file: &str) {
    fs::write(root.join(file), "edited\n").unwrap();
}

fn apply(root: &Path, args: &[&str]) -> (i32, String, String) {
    outcome(cargo_minify(root).args(["-q", "--apply"]).args(args))
}

fn minified(root: &Path) -> bool {
    fs::read_to_string(root.join("src/lib.rs")).unwrap() != LIB_RS
}

fn refused(dirty: &[&str], staged: &[&str]) -> (i32, String, String) {
    let mut stderr = "working directory contains dirty/staged files:\n".to_owned();
    for file in dirty {
        stderr += &format!("\t{file} (dirty)\n");
    }
    for file in staged {
        stderr += &format!("\t{file} (staged)\n");
    }
    stderr += "please fix this or ignore this warning with --allow-dirty and/or --allow-staged\n";
    (0, String::new(), stderr)
}

const APPLIED: (i32, String, String) = (0, String::new(), String::new());

#[test]
fn a_clean_repository_is_minified() {
    let root = repository("vcs-clean");
    assert_eq!(apply(&root, &[]), APPLIED);
    assert!(minified(&root));
}

#[test]
fn dirty_and_staged_files_are_listed_in_order_and_block_the_minify() {
    let root = repository("vcs-mixed");
    edit(&root, "a.txt");
    edit(&root, "b.txt");
    git(&root, &["add", "b.txt"]);
    fs::write(root.join("c.txt"), "new\n").unwrap();
    git(&root, &["add", "c.txt"]);
    edit(&root, "d.txt");
    git(&root, &["add", "d.txt"]);
    fs::write(root.join("d.txt"), "edited again\n").unwrap();
    fs::create_dir(root.join("newdir")).unwrap();
    fs::write(root.join("newdir/x.txt"), "").unwrap();
    fs::write(root.join("0.txt"), "").unwrap();

    let expected = refused(&["0.txt", "a.txt", "d.txt", "newdir/"], &["b.txt", "c.txt"]);
    for args in [
        &[][..],
        &["--allow-dirty"],
        &["--allow-staged"],
        &["--allow-no-vcs"],
    ] {
        assert_eq!(apply(&root, args), expected, "{args:?}");
        assert!(!minified(&root));
    }
    assert_eq!(apply(&root, &["--allow-dirty", "--allow-staged"]), APPLIED);
    assert!(minified(&root));
}

#[test]
fn each_kind_of_change_needs_its_own_allowance() {
    let root = repository("vcs-dirty-only");
    edit(&root, "a.txt");
    assert_eq!(apply(&root, &["--allow-staged"]), refused(&["a.txt"], &[]));
    assert_eq!(apply(&root, &["--allow-dirty"]), APPLIED);
    assert!(minified(&root));

    let root = repository("vcs-staged-only");
    edit(&root, "a.txt");
    git(&root, &["add", "a.txt"]);
    assert_eq!(apply(&root, &["--allow-dirty"]), refused(&[], &["a.txt"]));
    assert_eq!(apply(&root, &["--allow-staged"]), APPLIED);
    assert!(minified(&root));
}

#[test]
fn renames_and_deletions() {
    let root = repository("vcs-renames");
    git(&root, &["mv", "a.txt", "z.txt"]);
    git(&root, &["rm", "-q", "b.txt"]);
    fs::remove_file(root.join("d.txt")).unwrap();
    assert_eq!(
        apply(&root, &[]),
        refused(&["d.txt"], &["a.txt", "b.txt", "z.txt"])
    );
    assert!(!minified(&root));
}

#[test]
fn merge_conflicts_are_dirty() {
    let root = repository("vcs-conflict");
    git(&root, &["checkout", "-qb", "other"]);
    edit(&root, "a.txt");
    commit_all(&root);
    git(&root, &["checkout", "-q", "main"]);
    fs::write(root.join("a.txt"), "conflicting\n").unwrap();
    commit_all(&root);
    let merge = Command::new("git")
        .args([
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "merge",
            "-q",
            "other",
        ])
        .current_dir(&root)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .output()
        .unwrap();
    assert!(!merge.status.success());

    assert_eq!(apply(&root, &["--allow-staged"]), refused(&["a.txt"], &[]));
    assert!(!minified(&root));
}

/// A repository with a crate in `ws`, whose root and other files are outside the crate
fn crate_in_repository(name: &str, gitignore: &str) -> PathBuf {
    let root = scratch(name);
    let ws = root.join("ws");
    fs::create_dir(&ws).unwrap();
    dead_code_crate(&ws);
    fs::write(root.join(".gitignore"), gitignore).unwrap();
    git(&root, &["init", "-q"]);
    commit_all(&root);
    ws
}

#[test]
fn the_whole_repository_counts_with_paths_from_its_root() {
    let ws = crate_in_repository("vcs-parent", "");
    let root = ws.parent().unwrap();
    fs::write(root.join("outside.txt"), "").unwrap();
    fs::write(ws.join("src/inside.rs"), "").unwrap();
    assert_eq!(
        apply(&ws, &[]),
        refused(&["outside.txt", "ws/src/inside.rs"], &[])
    );
    assert!(!minified(&ws));
}

#[test]
fn a_crate_ignored_by_its_repository_has_no_vcs() {
    for (name, gitignore, crate_dir) in [
        ("vcs-ignored", "/ws/\n", "ws"),
        ("vcs-ignored-parent", "/outer/\n", "outer/ws"),
        ("vcs-ignored-glob", "w*\n", "ws"),
    ] {
        let root = scratch(name);
        let ws = root.join(crate_dir);
        fs::create_dir_all(&ws).unwrap();
        dead_code_crate(&ws);
        fs::write(root.join(".gitignore"), gitignore).unwrap();
        fs::write(root.join("dirty.txt"), "").unwrap();
        git(&root, &["init", "-q"]);

        assert_eq!(
            apply(&ws, &[]),
            (0, String::new(), NO_VCS.to_owned()),
            "{name}"
        );
        assert!(!minified(&ws), "{name}");
        assert_eq!(apply(&ws, &["--allow-no-vcs"]), APPLIED, "{name}");
        assert!(minified(&ws), "{name}");
    }
}

#[test]
fn a_tracked_crate_that_is_now_ignored_has_no_vcs() {
    let ws = crate_in_repository("vcs-ignored-tracked", "");
    let root = ws.parent().unwrap();
    fs::write(root.join(".gitignore"), "/ws/\n").unwrap();
    commit_all(root);
    assert_eq!(apply(&ws, &[]), (0, String::new(), NO_VCS.to_owned()));
    assert!(!minified(&ws));
}

#[test]
fn git_environment_variables_do_not_redirect_discovery() {
    let other = repository("vcs-env-other");
    let ws = crate_in_repository("vcs-env", "");
    let root = ws.parent().unwrap();
    fs::write(root.join("outside.txt"), "").unwrap();

    let mut cmd = cargo_minify(&ws);
    cmd.args(["-q", "--apply"])
        .env("GIT_DIR", other.join(".git"))
        .env("GIT_WORK_TREE", &other)
        .env("GIT_INDEX_FILE", other.join(".git/index"))
        .env("GIT_CEILING_DIRECTORIES", root)
        .env("GIT_DISCOVERY_ACROSS_FILESYSTEM", "0");
    assert_eq!(outcome(&mut cmd), refused(&["outside.txt"], &[]));
    assert!(!minified(&ws));
}

#[test]
fn a_failing_git_status_is_reported() {
    let root = repository("vcs-corrupt");
    fs::write(root.join(".git/index"), "corrupt").unwrap();
    let (code, stdout, stderr) = apply(&root, &["--allow-dirty", "--allow-staged"]);
    assert_eq!((code, stdout.as_str()), (0, ""));
    assert!(stderr.starts_with("git problem: "), "{stderr}");
    assert!(stderr.contains("index"), "{stderr}");
    assert!(
        stderr.ends_with('\n') && !stderr.ends_with("\n\n"),
        "{stderr:?}"
    );
    assert!(!minified(&root));
}
