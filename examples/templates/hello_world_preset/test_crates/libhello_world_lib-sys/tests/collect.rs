use libhello_world_lib_sys::*;

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

/// Run `call` while redirecting the process's C stdout into a pipe.
fn capture_stdout<T>(call: impl FnOnce() -> T) -> (T, String) {
    use std::io::Read;
    use std::os::fd::FromRawFd;

    let mut fds = [-1; 2];
    unsafe {
        libc::fflush(std::ptr::null_mut());
        assert_eq!(libc::pipe(fds.as_mut_ptr()), 0);
    }
    let saved = unsafe { libc::dup(libc::STDOUT_FILENO) };
    assert!(saved >= 0);
    unsafe {
        assert_eq!(libc::dup2(fds[1], libc::STDOUT_FILENO), libc::STDOUT_FILENO);
        libc::close(fds[1]);
    }
    // Drain concurrently so output larger than the pipe buffer cannot block.
    let reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        unsafe { std::fs::File::from_raw_fd(fds[0]) }
            .read_to_end(&mut bytes)
            .unwrap();
        bytes
    });
    let value = call();
    unsafe {
        libc::fflush(std::ptr::null_mut());
        assert_eq!(libc::dup2(saved, libc::STDOUT_FILENO), libc::STDOUT_FILENO);
        libc::close(saved);
    }
    let stdout = String::from_utf8_lossy(&reader.join().unwrap()).into_owned();
    (value, stdout)
}

#[derive(Serialize)]
struct HelloPrintCase {
    calls: usize,
    stdout: String,
}

#[test]
fn hello_print_writes_banner() {
    let ((), stdout) = capture_stdout(|| unsafe { hello_print() });
    save_case("hello_print_writes_banner", &HelloPrintCase { calls: 1, stdout });
}

#[test]
fn hello_print_twice_concatenates() {
    let ((), stdout) = capture_stdout(|| unsafe {
        hello_print();
        hello_print();
    });
    save_case("hello_print_twice_concatenates", &HelloPrintCase { calls: 2, stdout });
}
