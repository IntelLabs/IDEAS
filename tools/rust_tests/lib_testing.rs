use std::{
    ffi::OsStr,
    path::{Path, PathBuf},
    process::Command,
    sync::OnceLock,
};

const DEFAULT_TOOLCHAIN: &str = "1.94.1";

fn candidate_lib() -> &'static Path {
    static LIB: OnceLock<PathBuf> = OnceLock::new();

    LIB.get_or_init(|| {
        const MANIFEST_PATH: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/Cargo.toml");

        // Cargo tells tests nothing about their profile, so recover it from the build layout
        let exe = std::env::current_exe().expect("Couldn't get path of the test executable");
        let profile_dir = exe
            .parent()
            .and_then(Path::parent)
            .and_then(Path::file_name)
            .and_then(OsStr::to_str)
            .expect("Test executable should live in `<target dir>/<profile>/deps`");
        // The `dev` profile builds into a directory named `debug`
        let profile = if profile_dir == "debug" { "dev" } else { profile_dir };

        let mut cmd = Command::new(env!("CARGO"));
        cmd.args(["build", "--manifest-path", MANIFEST_PATH]);
        cmd.args(["--profile", profile]);
        cmd.arg("--message-format=json");

        let output = cmd.output().expect("Failed to execute `cargo build`");
        assert!(
            output.status.success(),
            "`cargo build` failed while locating the candidate library:\n{}",
            String::from_utf8_lossy(&output.stderr)
        );

        let stdout = String::from_utf8(output.stdout).expect("`cargo build` emitted invalid UTF-8");
        stdout
            .lines()
            .filter_map(|line| serde_json::from_str::<serde_json::Value>(line).ok())
            .filter(|msg| msg["reason"] == "compiler-artifact" && msg["manifest_path"] == MANIFEST_PATH)
            .flat_map(|msg| msg["filenames"].as_array().cloned().unwrap_or_default())
            .filter_map(|name| name.as_str().map(PathBuf::from))
            .find(|name| name.extension() == Some(OsStr::new(std::env::consts::DLL_EXTENSION)))
            .expect("`cargo build` reported no cdylib for this crate")
    })
    .as_path()
}

fn run_test_vector(vector_path: &str, runner_manifest_path: &str, test_root_dir: &str) {
    // Run the test vector
    let output = Command::new("cargo")
        .env("RUSTUP_TOOLCHAIN", std::env::var("RUNNER_TOOLCHAIN").unwrap_or_else(|_| DEFAULT_TOOLCHAIN.to_string()))
        .env("CANDO_LIBRARY_PATH", candidate_lib())
        .args(&["run", "--release", "--manifest-path", runner_manifest_path])
        .arg("--")
        .args(["--log-level", "quiet"])
        .args(["--test-root-dir", test_root_dir])
        .args(["--vectors", vector_path])
        .arg("--rust")
        .arg("lib")
        .output()
        .expect("Failed to execute runner");

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);

    // Parse and assert
    let success = output.status.success();
    assert!(success, "Test failed or crashed. Full output:\n{}\n{}", stdout, stderr);
}

// Macro to generate tests from test vector files
macro_rules! generate_tests {
    ($runner_manifest:expr, $test_root_dir:expr; $($name:ident => $path:expr),* $(,)?) => {
        $(
            #[test]
            fn $name() {
                run_test_vector($path, $runner_manifest, $test_root_dir);
            }
        )*
    };
}

// Auto-generated from test vectors
