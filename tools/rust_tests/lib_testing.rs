use std::process::Command;

use once_cell::sync::Lazy;
use std::path::PathBuf;
static ARTIFACT_DIR: Lazy<PathBuf> = Lazy::new(|| {
    test_cdylib::build_current_project()
        .parent()
        .expect("Failed to get parent directory of built library")
        .to_path_buf()
});

fn parse_test_output(output: &str, vector_path: &str) -> bool {
    // Check if the output contains "<vector_path>: true" anywhere
    output.contains(&format!("{}: true", vector_path))
}

fn run_test_vector(vector_path: &str, runner_manifest_path: &str) {
    // Run the test vector
    let output = Command::new("cargo")
        .args(&["run", "--release", "--manifest-path", runner_manifest_path])
        .arg("--")
        .arg("-b")
        .arg(&*ARTIFACT_DIR)
        .arg("lib")
        .args(&["-c", vector_path])
        .arg("-d")
        .output()
        .expect("Failed to execute runner");

    let stdout = String::from_utf8_lossy(&output.stdout);

    // Parse and assert
    let success = parse_test_output(&stdout, &vector_path);
    assert!(success, "Test failed or crashed. Full output:\n{}", stdout);
}

// Macro to generate tests from test vector files
macro_rules! generate_tests {
    ($runner_manifest:expr; $($name:ident => $path:expr),* $(,)?) => {
        $(
            #[test]
            fn $name() {
                run_test_vector($path, $runner_manifest);
            }
        )*
    };
}

// Auto-generated from test vectors
