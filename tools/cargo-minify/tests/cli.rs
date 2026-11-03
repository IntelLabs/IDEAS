//! The command line as users see it: help, errors, diffs and their colors, and `--apply`

mod common;

use std::{fs, process::Command};

use common::{cargo_minify, dead_code_crate, outcome, scratch, LIB_RS, M_RS};

const HELP: &str = "\
Optional arguments:
  -q, --quiet           No output printed to stdout
  -p, --package SPEC    Package to minify
  --workspace           Minify all packages in the workspace
  --exclude SPEC        Exclude packages from the minify
  -f, --file SPEC       File to minify
  -i, --ignore SPEC     Ignore files from the minify
  -k, --kinds < FUNCTION | CONST | STATIC | STRUCT | ENUM | UNION | TYPE_ALIAS | ASSOCIATED_FUNCTION | MACRO_DEFINITION >
                        specify which kinds of diagnostics to apply (all by default)
  --apply               Apply changes instead of outputting a diff
  -h, --help            Print help message
  --color WHEN          Coloring: auto, always, never
  --manifest-path PATH  Path to Cargo.toml
  --allow-dirty         Fix code even if the working directory is dirty
  --allow-staged        Fix code even if there are staged files in the VCS
  --allow-no-vcs        Also operate if no version control system was found
";

const LIB_DIFF: &str = "\
#
#\tshowing diff for \"src/lib.rs\":
#
#\t...
 \t    m::used()
 \t}
 \t
-\tfn dead() {}
 \t
";

const M_DIFF: &str = "\
#
#\tshowing diff for \"src/m.rs\":
#
#\t...
 \t    1
 \t}
 \t
-\tpub fn unused() {}
 \t
";

const COLORED_DIFF: &str = "\
\x1B[90m#
#\tshowing diff for \"src/lib.rs\":
#\x1B[0m
\x1B[90m#\t...\x1B[0m
\x1B[39m \t    m::used()\x1B[0m
\x1B[39m \t}\x1B[0m
\x1B[39m \t\x1B[0m
\x1B[91m-\tfn dead() {}\x1B[0m
\x1B[39m \t\x1B[0m
";

// The scratch crates are ignored by the enclosing repository, if any
const APPLY_ANYWAY: [&str; 4] = [
    "--apply",
    "--allow-no-vcs",
    "--allow-dirty",
    "--allow-staged",
];

const RUN_WITH_APPLY: &str = "run with --apply to apply these changes\n";

fn usage_error(message: &str) -> (i32, String, String) {
    let stderr = format!("error: {message}\n\nFor more information, try '--help'\n");
    (1, String::new(), stderr)
}

#[test]
fn help_whether_or_not_invoked_through_cargo() {
    let root = scratch("cli-help");
    let help = (0, HELP.to_owned(), String::new());
    for args in [&["--help"][..], &["-h"], &["-qh"], &["--apply", "--help"]] {
        assert_eq!(outcome(cargo_minify(&root).args(args)), help, "{args:?}");
    }
    let mut direct = Command::new(env!("CARGO_BIN_EXE_cargo-minify"));
    assert_eq!(outcome(direct.arg("--help").current_dir(&root)), help);
}

#[test]
fn malformed_command_lines_exit_with_a_hint() {
    let root = scratch("cli-errors");
    for (args, message) in [
        (&["--bogus"][..], "unrecognized option `--bogus`"),
        (&["-z"], "unrecognized option `-z`"),
        (&["--file"], "missing argument to option `--file`"),
        (&["--help=x"], "option `--help` does not accept an argument"),
        (&["stray"], "unexpected free argument `stray`"),
        (
            &["--color=bad"],
            "invalid argument to option `--color`: unsupported color mode, pick any of: auto, \
             always, never",
        ),
        (
            &["-kbogus"],
            "invalid argument to option `-k`: not an unused-diagnostic",
        ),
        // Parsing fails before help is printed
        (&["--help", "--bogus"], "unrecognized option `--bogus`"),
    ] {
        assert_eq!(
            outcome(cargo_minify(&root).args(args)),
            usage_error(message),
            "{args:?}"
        );
    }
}

#[test]
fn conflicting_options_exit_with_a_hint() {
    let root = scratch("cli-conflicts");
    let targets = "either specify --workspace and optionally \
                   --exclude specific targets, or specify specific targets with --package";
    for (args, message) in [
        (
            &["--exclude", "a"][..],
            "--exclude can only be used in conjunction with --workspace",
        ),
        (&["--workspace", "-p", "a"], targets),
        (&["-p", "a", "--exclude", "b"], targets),
        (
            &["-f", "a", "-i", "b"],
            "either specify --ignore to minify all files except",
        ),
        // Conflicts are reported even when asking for help
        (
            &["-h", "--exclude", "a"],
            "--exclude can only be used in conjunction with --workspace",
        ),
    ] {
        assert_eq!(
            outcome(cargo_minify(&root).args(args)),
            usage_error(message),
            "{args:?}"
        );
    }
}

#[test]
fn diffs_every_file_in_order_without_changing_them() {
    let root = scratch("cli-diff");
    dead_code_crate(&root);
    let stdout = format!("{LIB_DIFF}{M_DIFF}{RUN_WITH_APPLY}");
    let expected = (0, stdout, String::new());
    for _ in 0..3 {
        assert_eq!(outcome(cargo_minify(&root).arg("--color=never")), expected);
    }
    assert_eq!(fs::read_to_string(root.join("src/lib.rs")).unwrap(), LIB_RS);
    assert_eq!(fs::read_to_string(root.join("src/m.rs")).unwrap(), M_RS);
}

#[test]
fn colors_the_diff_unless_told_never_to() {
    let root = scratch("cli-color");
    dead_code_crate(&root);
    let args = ["--file", "src/lib.rs"];
    let colored = (0, format!("{COLORED_DIFF}{RUN_WITH_APPLY}"), String::new());
    // Even when not printing to a terminal
    assert_eq!(outcome(cargo_minify(&root).args(args)), colored);
    assert_eq!(
        outcome(cargo_minify(&root).args(args).arg("--color=auto")),
        colored
    );
    assert_eq!(
        outcome(cargo_minify(&root).args(args).arg("--color=always")),
        colored
    );
    let plain = (0, format!("{LIB_DIFF}{RUN_WITH_APPLY}"), String::new());
    let mut cmd = cargo_minify(&root);
    assert_eq!(
        outcome(
            cmd.args(args)
                .args(["--color", "always", "--color", "never"])
        ),
        plain
    );
}

#[test]
fn files_and_kinds_select_what_is_diffed() {
    let root = scratch("cli-select");
    dead_code_crate(&root);
    let only_m = (0, format!("{M_DIFF}{RUN_WITH_APPLY}"), String::new());
    for args in [
        &["-f", "src/m.rs"][..],
        &["--file=src/m*"],
        &["-i", "*/lib.rs"],
    ] {
        let mut cmd = cargo_minify(&root);
        assert_eq!(
            outcome(cmd.arg("--color=never").args(args)),
            only_m,
            "{args:?}"
        );
    }
    let nothing = (
        0,
        String::new(),
        "no unused code that can be minified\n".to_owned(),
    );
    for args in [&["-f", "src/none.rs"][..], &["-k", "struct", "-kconstant"]] {
        assert_eq!(outcome(cargo_minify(&root).args(args)), nothing, "{args:?}");
    }
}

#[test]
fn quiet_prints_no_diff() {
    let root = scratch("cli-quiet");
    dead_code_crate(&root);
    // Only the hint remains, as upstream
    let hint = (0, RUN_WITH_APPLY.to_owned(), String::new());
    assert_eq!(outcome(cargo_minify(&root).arg("-q")), hint);
    let mut cmd = cargo_minify(&root);
    let applied = outcome(cmd.arg("-q").args(APPLY_ANYWAY));
    assert_eq!(applied, (0, String::new(), String::new()));
    assert_ne!(fs::read_to_string(root.join("src/lib.rs")).unwrap(), LIB_RS);
}

#[test]
fn applies_to_every_file_then_finds_nothing_left() {
    let root = scratch("cli-apply");
    dead_code_crate(&root);
    let mut cmd = cargo_minify(&root);
    let applied = outcome(cmd.arg("--color=never").args(APPLY_ANYWAY));
    assert_eq!(applied, (0, format!("{LIB_DIFF}{M_DIFF}"), String::new()));
    // Deleting the last item of a file keeps the blank line before it, as upstream
    let lib_rs = LIB_RS.replace("fn dead() {}\n", "");
    assert_eq!(fs::read_to_string(root.join("src/lib.rs")).unwrap(), lib_rs);
    let m_rs = M_RS.replace("pub fn unused() {}\n", "");
    assert_eq!(fs::read_to_string(root.join("src/m.rs")).unwrap(), m_rs);

    let nothing = (
        0,
        String::new(),
        "no unused code that can be minified\n".to_owned(),
    );
    assert_eq!(outcome(&mut cargo_minify(&root)), nothing);
}

#[test]
fn kinds_as_spelled_in_the_usage_select_what_is_applied() {
    let root = scratch("cli-const");
    dead_code_crate(&root);
    let lib_rs = format!("{LIB_RS}\nconst DEAD: i32 = 1;\n");
    fs::write(root.join("src/lib.rs"), &lib_rs).unwrap();
    let mut cmd = cargo_minify(&root);
    let applied = outcome(cmd.args(["-q", "-k", "CONST"]).args(APPLY_ANYWAY));
    assert_eq!(applied, (0, String::new(), String::new()));
    let minified = fs::read_to_string(root.join("src/lib.rs")).unwrap();
    assert!(!minified.contains("const DEAD"), "{minified}");
    assert!(minified.contains("fn dead() {}"), "{minified}");
    assert_eq!(fs::read_to_string(root.join("src/m.rs")).unwrap(), M_RS);
}
