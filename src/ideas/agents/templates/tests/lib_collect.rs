use __LIB_NAME__::*;

use serde::Serialize;
use std::collections::BTreeMap;
use std::hash::{Hash, Hasher};
use std::path::{Path, PathBuf};
use walkdir::WalkDir;

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

/// Capture relative paths and exact file contents below a sandbox.
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

fn save_case<T: Serialize>(name: &str, case: &T) {
    std::fs::create_dir_all("json").unwrap();
    std::fs::write(
        format!("json/{name}.json"),
        serde_json::to_string_pretty(case).unwrap(),
    )
    .unwrap();
}

// ==== Add collection tests below this line ====
