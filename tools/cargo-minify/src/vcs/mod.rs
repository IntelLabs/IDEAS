use std::{path::Path, process::Command};

mod check_vcs;

pub fn status(path: impl AsRef<Path>) -> Status {
    check_version_control(path.as_ref())
}

pub enum Status {
    Clean,
    Unclean {
        dirty: Vec<String>,
        staged: Vec<String>,
    },
    NoVCS,
    Error(String),
}

// Portions of the below code are inspired by/taken from Cargo, https://github.com/rust-lang/cargo/
// Copyright (c) 2016-2021 The Cargo Developers

fn check_version_control(path: &Path) -> Status {
    if !check_vcs::in_git_repo(path) {
        return Status::NoVCS;
    }

    let output = match git(path)
        .args(["--no-optional-locks", "status", "--porcelain", "-z"])
        .args(["--no-renames", "--untracked-files=normal"])
        .output()
    {
        Ok(output) if output.status.success() => output.stdout,
        Ok(output) => {
            let stderr = String::from_utf8_lossy(&output.stderr);
            return Status::Error(stderr.trim_end().to_owned());
        }
        Err(error) => return Status::Error(error.to_string()),
    };
    let (dirty, staged) = classify(&output);

    if dirty.is_empty() && staged.is_empty() {
        Status::Clean
    } else {
        Status::Unclean { dirty, staged }
    }
}

/// `git -C path`, discovering the repository the way `git2::Repository::discover` does: across
/// filesystems and regardless of the `GIT_*` variables that override it, e.g. in a git hook
fn git(path: &Path) -> Command {
    let mut git = Command::new("git");
    git.arg("-C").arg(path);
    for var in [
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
    ] {
        git.env_remove(var);
    }
    git.env("GIT_DISCOVERY_ACROSS_FILESYSTEM", "1");
    git
}

/// Splits `git status --porcelain -z --no-renames` output into dirty and staged paths, sorted
/// by path as libgit2 lists them: a path is staged iff its only change is in the index
fn classify(porcelain: &[u8]) -> (Vec<String>, Vec<String>) {
    // Each entry is `XY <path>`, where X is the index and Y the worktree status
    let mut entries: Vec<_> = porcelain
        .split(|&b| b == 0)
        .filter(|entry| entry.len() > 3)
        .filter_map(|entry| Some((std::str::from_utf8(&entry[3..]).ok()?, &entry[..2])))
        .collect();
    entries.sort_unstable();
    let (mut dirty, mut staged) = (Vec::new(), Vec::new());
    for (path, xy) in entries {
        match xy {
            [b'A' | b'M' | b'D' | b'T', b' '] => staged.push(path.to_owned()),
            _ => dirty.push(path.to_owned()),
        }
    }
    (dirty, staged)
}

#[cfg(test)]
mod tests {
    use super::classify;

    fn strings(paths: &[&str]) -> Vec<String> {
        paths.iter().map(|&path| path.to_owned()).collect()
    }

    #[test]
    fn nothing_to_report() {
        assert_eq!(classify(b""), (vec![], vec![]));
    }

    #[test]
    fn staged_only_when_the_index_alone_changed() {
        let porcelain = b"A  added\0M  modified\0D  deleted\0T  typechange\0";
        let staged = strings(&["added", "deleted", "modified", "typechange"]);
        assert_eq!(classify(porcelain), (vec![], staged));
    }

    #[test]
    fn dirty_when_the_worktree_changed_or_merging() {
        let entries = [
            " M", " D", " T", " A", "MM", "AM", "AD", "MD", "RM", "UU", "AA", "DD", "AU", "UD",
            "??",
        ];
        let porcelain: Vec<u8> = entries
            .iter()
            .enumerate()
            .flat_map(|(i, xy)| format!("{xy} f{i:02}\0").into_bytes())
            .collect();
        let dirty: Vec<_> = (0..entries.len()).map(|i| format!("f{i:02}")).collect();
        assert_eq!(classify(&porcelain), (dirty, vec![]));
    }

    #[test]
    fn sorted_by_path_with_untracked_interleaved() {
        let porcelain = b" M b.rs\0M  d.rs\0?? a/\0?? c.rs\0A  a.rs\0";
        let (dirty, staged) = (strings(&["a/", "b.rs", "c.rs"]), strings(&["a.rs", "d.rs"]));
        assert_eq!(classify(porcelain), (dirty, staged));
    }

    #[test]
    fn keeps_paths_with_spaces_and_skips_non_utf8_ones() {
        let porcelain = b"?? with space.rs\0?? bad\xff.rs\0 M sub/dir/x.rs\0";
        let dirty = strings(&["sub/dir/x.rs", "with space.rs"]);
        assert_eq!(classify(porcelain), (dirty, vec![]));
    }
}
