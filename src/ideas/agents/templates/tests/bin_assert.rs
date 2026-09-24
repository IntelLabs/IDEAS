use __LIB_NAME__::*;

use assert_cmd::Command;
use std::collections::BTreeMap;
use std::hash::{Hash, Hasher};
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use walkdir::WalkDir;

const PROGRAM_PATH_PLACEHOLDER: &str = "<program>";
const SANITIZER_EXIT_CODE: i32 = 86;

/// What one binary invocation produced.
struct Call {
    stdout: String,
    stderr: String,
    exit_code: i32,
    changed: Vec<String>,
    removed: Vec<String>,
}

/// Create a fresh directory reserved for one test.
fn sandbox(name: &str) -> PathBuf {
    let mut components = Path::new(name).components();
    assert!(
        matches!(components.next(), Some(std::path::Component::Normal(_)))
            && components.next().is_none(),
        "sandbox name must be one ordinary path component",
    );
    let mut id = std::collections::hash_map::DefaultHasher::new();
    env!("CARGO_MANIFEST_DIR").hash(&mut id);
    let dir = std::env::temp_dir()
        .join("ideas-testgen")
        .join(format!("{:016x}", id.finish()))
        .join(name);
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

type FileTree = BTreeMap<String, Option<Vec<u8>>>;

fn snapshot(dir: &Path) -> FileTree {
    WalkDir::new(dir)
        .min_depth(1)
        .sort_by_file_name()
        .into_iter()
        .map(|entry| {
            let entry = entry.unwrap();
            let path = entry
                .path()
                .strip_prefix(dir)
                .unwrap()
                .to_string_lossy()
                .into_owned();
            let contents = entry
                .file_type()
                .is_file()
                .then(|| std::fs::read(entry.path()).unwrap());
            (path, contents)
        })
        .collect()
}

fn diff(before: &FileTree, after: &FileTree) -> (Vec<String>, Vec<String>) {
    let changed = after
        .iter()
        .filter_map(|(path, state)| (before.get(path) != Some(state)).then(|| path.clone()))
        .collect();
    let removed = before
        .keys()
        .filter_map(|path| (!after.contains_key(path)).then(|| path.clone()))
        .collect();
    (changed, removed)
}

fn output_text(bytes: &[u8], bin_path: &str) -> String {
    String::from_utf8_lossy(bytes).replace(bin_path, PROGRAM_PATH_PLACEHOLDER)
}

/// Run once and record changed and removed paths below dir.
fn run(dir: &Path, args: &[&str], stdin: Option<&str>) -> Call {
    let bin_path = assert_cmd::cargo::cargo_bin(assert_cmd::pkg_name!());
    let bin_path_str = bin_path.to_str().unwrap();
    let before = snapshot(dir);

    let mut cmd = Command::new("stdbuf");
    cmd.args(["-e0", "-o0", bin_path_str])
        .args(args)
        .current_dir(dir)
        .env("HOME", dir)
        .env("TZ", "UTC")
        .env("LC_ALL", "C");
    if let Some(input) = stdin {
        cmd.write_stdin(input);
    }
    let output = cmd.output().expect("failed to execute process");
    let signal = output.status.signal();
    let sanitizer_failed = cfg!(any(feature = "cc_asan", feature = "cc_ubsan"))
        && (output.status.code() == Some(SANITIZER_EXIT_CODE) || signal.is_some());
    if sanitizer_failed || matches!(signal, Some(libc::SIGILL) | Some(libc::SIGABRT)) {
        panic!(
            "Sanitizer detected an error while running the binary ({}).\n{}",
            output.status,
            output_text(&output.stderr, bin_path_str),
        );
    }

    let after = snapshot(dir);
    let (changed, removed) = diff(&before, &after);
    Call {
        stdout: output_text(&output.stdout, bin_path_str),
        stderr: output_text(&output.stderr, bin_path_str),
        exit_code: output.status.code().unwrap_or(-1),
        changed,
        removed,
    }
}

// ==== Add assertion tests below this line ====
