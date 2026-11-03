// Portions of the below code are inspired by/taken from Cargo, https://github.com/rust-lang/cargo/
// Copyright (c) 2016-2021 The Cargo Developers

use std::path::{Path, PathBuf};

/// Whether `path` is in a git repo that does not ignore it
pub fn in_git_repo(path: &Path) -> bool {
    if let Some(workdir) = git_toplevel(path) {
        // Don't check if the working directory itself is ignored.
        if workdir == path {
            true
        } else {
            !git_ignores(path)
        }
    } else {
        false
    }
}

/// The root of the git working tree containing `path`, if any
fn git_toplevel(path: &Path) -> Option<PathBuf> {
    let output = super::git(path)
        .args(["rev-parse", "--show-toplevel"])
        .output()
        .ok()?;
    let toplevel = String::from_utf8(output.stdout).ok()?;
    output
        .status
        .success()
        .then(|| PathBuf::from(toplevel.trim_end_matches('\n')))
}

/// Whether the ignore rules match `path`, regardless of whether it is tracked
fn git_ignores(path: &Path) -> bool {
    super::git(path)
        .args(["check-ignore", "-q", "--no-index"])
        .arg(path)
        .status()
        .is_ok_and(|status| status.success())
}
