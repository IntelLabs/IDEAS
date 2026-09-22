#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
import os
import stat
import textwrap
from pathlib import Path

from ideas.agents.verifiers import (
    VERIFIER_COMMAND_ENV,
    VERIFIER_SOCKET_ENV,
    VerificationError,
    verify_tests_are_portable,
)
from ideas.ast_rust import get_root
from ideas.tools import Crate, run_subprocess

logger = logging.getLogger("ideas.agents.utils")

_TEMPLATE_DIR = Path(__file__).with_name("templates") / "tests"
_ASAN_OPTIONS = (
    "detect_leaks=0"  # This is assumed a non-issue for short-lived instrumentation
    ":allocator_may_return_null=1"  # https://github.com/google/sanitizers/wiki/SanitizerCommonFlags
    ":detect_invalid_pointer_pairs=2"  # https://github.com/llvm/llvm-project/blob/03fdf3e62c9c647649168a2edb50b0ebb55cdb47/compiler-rt/lib/asan/asan_flags.inc#L149
    ":exitcode=86"
)
_UBSAN_OPTIONS = (
    "halt_on_error=1"  # UBSan doesn't halt by default
    ":print_stacktrace=1"  # https://releases.llvm.org/21.1.0/tools/clang/docs/UndefinedBehaviorSanitizer.html#stack-traces-and-report-symbolization
    ":exitcode=86"
)
_PLACEHOLDER = "#[test]\nfn placeholder() {\n    assert_eq!(1, 1);\n}\n"


def _cargo_test_with_features(
    crate: Crate,
    test: str,
    features: list[str],
    name: str | None = None,
) -> bool:
    cmd = [
        "cargo",
        "nextest",
        "run",
        "--color=never",
        "--cargo-quiet",
        f"--manifest-path={crate.cargo_toml}",
        "--no-fail-fast",
        "--test",
        test,
        "--test-threads=1",
        "--features",
        ",".join(features),
    ]
    if name is not None:
        cmd.extend(["-E", f"test(={name})"])
    env = {
        **os.environ,
        "ASAN_OPTIONS": _ASAN_OPTIONS,
        "UBSAN_OPTIONS": _UBSAN_OPTIONS,
        "LSAN_OPTIONS": "",
    }
    return run_subprocess(cmd, env=env, cwd=crate.cargo_toml.parent)[0]


def find_failing_tests(crate: Crate, test: str, features: list[str] | None = None) -> list[str]:
    if features:
        passes = _cargo_test_with_features(crate, test, features)
    else:
        passes = crate.cargo_test(test)[0]
    if passes:
        return []

    names = crate.cargo_test_list(test, features)
    return [
        name
        for name in names
        if not (
            _cargo_test_with_features(crate, test, features, name)
            if features
            else crate.cargo_test(test, skip=[other for other in names if other != name])[0]
        )
    ]


def remove_tests(path: Path, names: list[str]) -> list[str]:
    unique_names = set(names)
    if not unique_names:
        return []

    source = path.read_bytes()
    spans: list[tuple[int, int]] = []
    removed: list[str] = []
    for node in get_root(source).children:
        name_node = node.child_by_field_name("name") if node.type == "function_item" else None
        if name_node is None or name_node.text is None:
            continue
        if (name := name_node.text.decode()) not in unique_names:
            continue

        start, end = node.start_byte, node.end_byte
        sibling = node.prev_sibling
        while sibling is not None and sibling.type == "attribute_item":
            start = sibling.start_byte
            sibling = sibling.prev_sibling
        while end < len(source) and source[end : end + 1].isspace():
            end += 1

        spans.append((start, end))
        removed.append(name)

    for start, end in sorted(spans, reverse=True):
        source = source[:start] + source[end:]
    path.write_bytes(source)
    return removed


def prune_failing_tests(
    crate: Crate, test: str, features: list[str] | None = None
) -> list[str]:
    label = f"`{test}`" + (f" with {'+'.join(features)}" if features else "")
    failures = find_failing_tests(crate, test, features)
    if not failures:
        return []

    logger.warning(f"{len(failures)} test(s) of {label} fail: {', '.join(failures)}")
    removed = remove_tests(crate.cargo_toml.parent / "tests" / f"{test}.rs", failures)
    if missing := sorted(set(failures) - set(removed)):
        logger.warning(f"Failing test(s) {missing} of {label} are not defined in {test}.rs!")
    try:
        listed = crate.cargo_test_list(test)
        if survivors := sorted(set(removed) & set(listed)):
            logger.warning(f"Removed test(s) {survivors} of {label} are still listed!")
    except RuntimeError as e:
        logger.warning(f"Cannot list the tests of `{test}` after removing {removed}: {e}")
    return removed


def clear_generated_test_artifacts(crate: Crate) -> None:
    for pattern in ("json/*.json", "coverage_logs/*.log", "sanitizer_logs/*.log"):
        for artifact in crate.cargo_toml.parent.glob(pattern):
            artifact.unlink()


def write_placeholder_tests(crate: Crate, fresh: Crate, tests: list[str], message: str) -> None:
    test_dir = crate.cargo_toml.parent / "tests"
    for test in tests:
        (test_dir / f"{test}.rs").write_text(_PLACEHOLDER)
    crate.vcs.add(test_dir)
    crate.vcs.commit(message)
    try:
        verify_tests_are_portable(crate, fresh, tests)
    except VerificationError as error:
        # Critical failure
        raise VerificationError("A placeholder integration test does not build!") from error


def finalize_tests(crate: Crate, fresh: Crate, tests: list[str], features: list[str]) -> None:
    crate.vcs.init(force_init=True)
    crate_dir = crate.cargo_toml.parent
    test_dir = crate_dir / "tests"
    clear_generated_test_artifacts(crate)

    for test in tests:
        if not (test_dir / f"{test}.rs").exists():
            logger.warning(f"{test}.rs was not generated by the agent!")
            (test_dir / f"{test}.rs").write_text(_PLACEHOLDER)

    # Prune uninstrumented first, then once per sanitizer feature
    pruned: dict[str, list[str]] = {}
    unbuildable: set[str] = set()
    for test in tests:
        removed: set[str] = set()
        try:
            for feature in [None, *features]:
                removed.update(prune_failing_tests(crate, test, [feature] if feature else None))
        except RuntimeError:
            logger.warning(
                f"Cannot list the tests of `{test}`; replacing the suites with placeholders"
            )
            unbuildable.add(test)
        pruned[test] = sorted(removed)

    if any(pruned.values()):
        clear_generated_test_artifacts(crate)

    # A test file that lists nothing either holds no test or does not build
    empty = list(unbuildable)
    for test in tests:
        if test in unbuildable:
            continue
        try:
            listed = crate.cargo_test_list(test)
        except RuntimeError as e:
            logger.warning(f"Cannot list the tests of `{test}`: {e}")
            listed = []
        if not listed:
            empty.append(test)

    if empty:
        message = "No test survived. Only placeholders left"
        logger.warning(message)
        write_placeholder_tests(crate, fresh, tests, message)
        return

    # Recollect data if any test was pruned
    if pruned["collect"]:
        for stale in (crate_dir / "json").glob("*.json"):
            stale.unlink()
        crate.cargo_test("collect")

    crate.vcs.add(test_dir)
    crate.vcs.commit(f"Removed the failing tests: {pruned}")

    # Portability verification after pruning
    try:
        verify_tests_are_portable(crate, fresh, tests)
    except VerificationError as e:
        message = f"The pruned tests did not pass verification: {e}"
        logger.warning(message)
        write_placeholder_tests(crate, fresh, tests, message)


def strip_line_directives(path: Path) -> None:
    success, output, error, _ = run_subprocess(
        ["clang", "--preprocess", "--no-line-commands", str(path), "-o", "-"]
    )
    if not success:
        raise RuntimeError(f"Failed to strip line directives from {path}!{output + error}")
    path.write_text(output)


def write_instrumentation_script(path: Path, features: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    contents = textwrap.dedent(
        f"""
        #!/usr/bin/env bash
        ## This script is generated automatically and should not be modified! ##
        set -euo pipefail

        test_name="${{1:-}}"
        if [ "$test_name" != "collect" ] && [ "$test_name" != "io" ]; then
            echo "Usage: $0 <collect|io>" >&2
            exit 2
        fi

        # Keep the harness callback out of the environment observed by generated tests.
        test_verifier="${{{VERIFIER_COMMAND_ENV}:-}}"
        verifier_socket="${{{VERIFIER_SOCKET_ENV}:-}}"
        unset {VERIFIER_COMMAND_ENV} {VERIFIER_SOCKET_ENV}

        log_dir="coverage_logs"
        mkdir -p "$log_dir"
        coverage_report="$log_dir/coverage_report.log"
        coverage_summary="$log_dir/coverage_summary.log"
        coverage_log="$log_dir/cc_coverage.log"
        uncovered_branches="$log_dir/uncovered_branches.log"
        rm -f "$coverage_report" "$coverage_summary" "$coverage_log" "$uncovered_branches"
        sanitizer_dir="sanitizer_logs"
        mkdir -p "$sanitizer_dir"
        rm -f "$sanitizer_dir"/*.log
        features="{" ".join(features)}"
        sanitizer_status=0

        json_snapshot=""
        previous_json=""
        restore_json() {{
            local snapshot="$json_snapshot"
            json_snapshot=""
            if [ -n "$snapshot" ]; then
                mkdir -p json
                rm -f json/*.json
                cp -a "$snapshot"/. json/
                rm -rf "$snapshot"
            fi
        }}
        cleanup_json() {{
            restore_json
            if [ -n "$previous_json" ]; then
                rm -rf "$previous_json"
                previous_json=""
            fi
        }}
        trap cleanup_json EXIT

        if [ "$test_name" = "collect" ]; then
            json_snapshot=$(mktemp -d)
            previous_json=$(mktemp -d)
            if [ -d json ]; then
                find json -maxdepth 1 -type f -name '*.json' -exec cp -a -t "$json_snapshot" {{}} +
                find json -maxdepth 1 -type f -name '*.json' -exec cp -a -t "$previous_json" {{}} +
            fi
            rm -f json/*.json
        fi

        # Capture expected values from the ordinary, uninstrumented crate
        cargo nextest run --cargo-quiet --status-level fail --final-status-level fail \\
            --test "$test_name" --no-fail-fast --test-threads 1
        if [ "$test_name" = "collect" ]; then
            rm -f "$json_snapshot"/*.json
            if [ -d json ]; then
                find json -maxdepth 1 -type f -name '*.json' -exec cp -a -t "$json_snapshot" {{}} +
            fi
        fi

        export ASAN_OPTIONS="{_ASAN_OPTIONS}"
        export UBSAN_OPTIONS="{_UBSAN_OPTIONS}"
        export LSAN_OPTIONS=
        export DEBUGINFOD_URLS=

        # Run each sanitizer: print basic diagnostics to stdout and, only on
        # failure, save a detailed per-sanitizer log (including stderr diagnostics).
        for feature in $features; do
            err_file=$(mktemp)
            if ! output=$(NEXTEST_EXPERIMENTAL_LIBTEST_JSON=1 cargo nextest run \\
                --cargo-quiet --status-level fail --final-status-level fail \\
                --features "$feature" --test "$test_name" --no-fail-fast \\
                --test-threads 1 --message-format libtest-json 2>"$err_file"); then
                sanitizer_status=1
                mkdir -p "$sanitizer_dir"
                log_file="$sanitizer_dir/$feature.log"

                # Basic diagnostics -> stdout
                echo "=== $feature ==="
                printf '%s\\n' "$output" | jq -r 'select(.type == "suite" and .event != "started")
                        | "\\(.passed + .failed) tests run: \\(.passed) passed, \\(.failed) failed"'
                printf '%s\\n' "$output" | jq -r 'select(.type == "test" and .event == "failed")
                        | "FAIL \\(.name)"'
                # Detailed diagnostics -> per-sanitizer log file
                {{
                    echo "=== $feature ==="
                    printf '%s\\n' "$output" | jq -r 'select(.type == "test" and .event == "failed")
                            | "FAIL \\(.name)\\n\\(.stdout // "")"'
                    echo "--- stderr (sanitizer diagnostics) ---"
                    cat "$err_file"
                }} > "$log_file"
                sed -n '1,80p' "$log_file"
                echo "  detailed log: $log_file"
            fi
            rm -f "$err_file"
        done

        if [ "$sanitizer_status" -ne 0 ]; then
            exit 1
        fi

        echo "All sanitizer checks passed"
        coverage_failed() {{
            local stage="$1"
            local status="$2"
            echo "=== cc_coverage ==="
            echo "Coverage $stage failed with exit code $status"
            tail -n 40 "$coverage_log"
            echo "  detailed log: $coverage_log"
            exit "$status"
        }}

        if cargo llvm-cov clean --profraw-only >> "$coverage_log" 2>&1; then
            :
        else
            status=$?
            coverage_failed "cleanup" "$status"
        fi
        if cargo llvm-cov nextest --features cc_coverage --include-ffi --no-report --test "$test_name" --no-fail-fast --test-threads 1 >> "$coverage_log" 2>&1; then
            :
        else
            status=$?
            coverage_failed "test run" "$status"
        fi
        if cargo llvm-cov report --include-ffi --text > "$coverage_report" 2>> "$coverage_log"; then
            :
        else
            status=$?
            coverage_failed "text report" "$status"
        fi

        # Coverage summary table -> stdout and log file.
        echo "Coverage summary for $test_name:"
        if cargo llvm-cov report --include-ffi --summary-only 2>> "$coverage_log" | tee "$coverage_summary" | tee -a "$coverage_log"; then
            :
        else
            status=$?
            coverage_failed "summary report" "$status"
        fi

        # Emit only uncovered branches (a branch whose True or False count is zero).
        echo "Uncovered branches:"
        {{ grep -E 'Branch \\(.*(True: 0,|False: 0\\])' "$coverage_report" || echo "  none"; }} | tee "$uncovered_branches"

        # The test-generation harness verifies the generated test against a pristine crate.
        if [ -n "$test_verifier" ]; then
            "$test_verifier" -m ideas.agents.verifiers "$verifier_socket" "$test_name"
            echo "Verified tests/$test_name.rs against a pristine crate."
        fi

        if [ "$test_name" = "collect" ]; then
            restore_json
            echo "JSON values changed by this collect run:"
            changed_json=0
            total_json=0
            for json_file in json/*.json; do
                [ -e "$json_file" ] || continue
                total_json=$((total_json + 1))
                prior="$previous_json/${{json_file#json/}}"
                if [ -e "$prior" ] && cmp -s "$prior" "$json_file"; then
                    continue
                fi
                changed_json=$((changed_json + 1))
                echo "--- $json_file"
                jq -c . "$json_file" 2>/dev/null || cat "$json_file"
            done
            if [ "$changed_json" -eq 0 ]; then
                echo "  none ($total_json JSON files remain under json/)"
            fi
        fi

        branch_coverage=$(awk '$1 == "TOTAL" {{ print $NF; exit }}' "$coverage_summary")
        if [ -z "$branch_coverage" ]; then
            coverage_failed "branch coverage extraction" 1
        fi
        echo "Branch coverage: $branch_coverage"
        """
    ).strip()
    path.unlink(missing_ok=True)
    path.write_text(contents + "\n")

    # Read + execute for the current user
    path.chmod(stat.S_IRUSR | stat.S_IXUSR)
    return path


def _write_test_template(path: Path, template: str, kind: str, lib_name: str | None) -> Path:
    if template not in {"bin", "lib"}:
        raise ValueError(f"Unknown test template: {template}")
    if lib_name is None:
        raise ValueError("lib_name is required for a test template")

    path.parent.mkdir(parents=True, exist_ok=True)
    source = _TEMPLATE_DIR / f"{template}_{kind}.rs"
    path.write_text(source.read_text().replace("__LIB_NAME__", lib_name))
    return path


def write_test_templates(path: Path, template: str, lib_name: str | None = None) -> None:
    _write_test_template(path / "collect.rs", template, "collect", lib_name)
    _write_test_template(path / "io.rs", template, "assert", lib_name)


TESTGEN_BOTTOM_UP = os.environ.get("TESTGEN_BOTTOM_UP", "0") not in ("0", "", "false", "False")
TESTGEN_FINISH_EARLY = os.environ.get("TESTGEN_FINISH_EARLY", "1") not in (
    "0",
    "",
    "false",
    "False",
)
