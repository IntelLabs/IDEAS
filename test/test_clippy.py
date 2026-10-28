#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ideas import ast
from ideas.ast_rust import CodeRust
from ideas.codex import CodexConfig
from ideas.consolidate import get_symbols_and_dependencies
from ideas.idiomaticity import DIMENSIONS, apply_gate, build_failure, build_prompt, judge
from ideas.model import Generation, ModelConfig
from ideas.tools import ClippyResult, Crate
from ideas.translate_recurrent import SymbolTranslator, SymbolGroup, SymbolName
from ideas.translate_snippet import SnippetTranslator
from ideas.wrapper import WrapperGenerator

from test_translate_recurrent import _make_translator, _mock_bar_symbol, _run
from test_translate_recurrent import _StubSnippetTranslator, _StubWrapperGenerator
from test_translate_recurrent import _GLOBAL_VARIABLE, _SYNC_FNS, _variable_wrapper
from test_translate_recurrent import _clippy_result

PREFIX = "#![forbid(unsafe_code)]\n\npub fn earlier() {}\n"
APPENDED = CodeRust("pub fn bar(x: &Vec<i32>) -> usize {\n    x.len()\n}")

# Where `_diagnostic` keeps the package cargo was compiling until `_clippy_run` moves it
PACKAGE = "_package_id"


def _diagnostic(
    file_name: str,
    line: int,
    rendered: str,
    level: str = "warning",
    expansion: dict[str, Any] | None = None,
    package: str = "foo-rs",
) -> dict[str, Any]:
    span = {"file_name": file_name, "line_start": line, "is_primary": True}
    if expansion is not None:
        span["expansion"] = {"span": expansion}
    return {"level": level, "rendered": rendered, "spans": [span], PACKAGE: package}


def _crate(tmp_path: Path) -> Crate:
    package_dir = tmp_path / "foo-rs"
    (package_dir / "src").mkdir(parents=True)
    (package_dir / "Cargo.toml").write_text('[package]\nname = "foo-rs"\n')
    (package_dir / "src" / "lib.rs").write_text(str(CodeRust(PREFIX) + APPENDED))
    crate = Crate(package_dir / "Cargo.toml")
    # In place of `cargo metadata`
    crate.__dict__["metadata"] = {
        "workspace_root": str(tmp_path),
        "workspace_members": ["foo-rs"],
        "resolve": {"root": "foo-rs"},
        "packages": [
            {
                "id": "foo-rs",
                "name": "foo-rs",
                "targets": [
                    {
                        "kind": ["lib"],
                        "name": "foo_rs",
                        "src_path": str(package_dir / "src" / "lib.rs"),
                    }
                ],
            }
        ],
    }
    return crate


def _clippy_run(diagnostics: list[dict[str, Any]], stderr: str = "") -> Any:
    def record(diagnostic: dict[str, Any]) -> str:
        message = {k: v for k, v in diagnostic.items() if k != PACKAGE}
        package_id = diagnostic.get(PACKAGE, "foo-rs")
        return json.dumps(
            {"reason": "compiler-message", "package_id": package_id, "message": message}
        )

    output = "\n".join(record(diagnostic) for diagnostic in diagnostics)
    returncode = 101 if stderr else 0
    return patch(
        "ideas.tools.run_subprocess", return_value=(not stderr, output, stderr, returncode)
    )


def _cargo_clippy(
    tmp_path: Path, diagnostics: list[dict[str, Any]], only_in: CodeRust | None = None
) -> list[str]:
    crate = _crate(tmp_path)
    with _clippy_run(diagnostics):
        result = crate.cargo_clippy()
        if only_in is not None:
            result = result.only_in(only_in)
    assert result.success
    return [d["rendered"] for d in result.diagnostics]


def test_cargo_clippy_selects_the_requested_target(tmp_path: Path) -> None:
    crate = _crate(tmp_path)
    with patch("ideas.tools.run_subprocess", return_value=(True, "", "", 0)) as run:
        crate.cargo_clippy()
        assert "--lib" in run.call_args.args[0]
        assert "--all-targets" not in run.call_args.args[0]

        crate.cargo_clippy(targets="--all-targets")
        assert "--all-targets" in run.call_args.args[0]
        assert "--lib" not in run.call_args.args[0]


DIAGNOSTICS = [
    _diagnostic("foo-rs/src/lib.rs", 3, "warning: in the prefix"),
    _diagnostic("foo-rs/src/lib.rs", 5, "warning: in the appended code"),
    _diagnostic("foo-sys/src/lib.rs", 1, "warning: in a dependency", package="foo-sys"),
    _diagnostic(
        "/rustc/0000/library/alloc/src/macros.rs",
        42,
        "warning: expanded from std into the appended code",
        expansion={"file_name": "foo-rs/src/lib.rs", "line_start": 6},
    ),
    _diagnostic("foo-rs/src/lib.rs", 7, "error: denied in the appended code", "error"),
    _diagnostic(
        "target/debug/build/foo-rs-0000/out/bindings.rs",
        1,
        "warning: in code the package generated into OUT_DIR",
    ),
    # Cargo's closing summary, which points nowhere
    {"level": "warning", "rendered": "warning: 2 warnings emitted", "spans": []},
]


def test_cargo_clippy_reports_diagnostics_in_its_package(tmp_path: Path) -> None:
    # Clippy also lints path dependencies, whose diagnostics are not this package's to fix,
    # and closes with a summary that points nowhere
    assert _cargo_clippy(tmp_path, DIAGNOSTICS) == [
        "warning: in the prefix",
        "warning: in the appended code",
        "warning: expanded from std into the appended code",
        "error: denied in the appended code",
        "warning: in code the package generated into OUT_DIR",
    ]


def test_cargo_clippy_answers_only_for_its_own_package(tmp_path: Path) -> None:
    # The path dependency's denied Clippy lint and rustc warning are not this package's to fix
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["foo-rs", "foo-sys"]\nresolver = "2"\n'
    )
    for name, lib in {
        "foo-sys": "#![deny(clippy::ptr_arg)]\n"
        "pub fn len(x: &Vec<i32>) -> usize {\n    let unused = 0;\n    x.len()\n}\n",
        "foo-rs": "pub fn len(x: &Vec<i32>) -> usize {\n    x.len() + foo_sys::len(&Vec::new())\n}\n",
    }.items():
        (tmp_path / name / "src").mkdir(parents=True)
        (tmp_path / name / "src" / "lib.rs").write_text(lib)
        manifest = f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n'
        if name == "foo-rs":
            manifest += '\n[dependencies]\nfoo-sys = { path = "../foo-sys" }\n'
        (tmp_path / name / "Cargo.toml").write_text(manifest)

    result = Crate(tmp_path / "foo-rs" / "Cargo.toml").cargo_clippy()

    assert result.success, result.output
    assert [(d["code"]["code"], d["spans"][0]["file_name"]) for d in result.diagnostics] == [
        ("clippy::ptr_arg", "foo-rs/src/lib.rs")
    ]


def test_cargo_clippy_keeps_only_what_is_in_the_appended_code(tmp_path: Path) -> None:
    # `bar` spans lines 5-7 of lib.rs, after the three-line prefix and the blank line joining them
    assert _cargo_clippy(tmp_path, DIAGNOSTICS, APPENDED) == [
        "warning: in the appended code",
        "warning: expanded from std into the appended code",
        "error: denied in the appended code",
    ]


def test_cargo_clippy_keeps_nothing_when_nothing_was_appended(tmp_path: Path) -> None:
    assert _cargo_clippy(tmp_path, DIAGNOSTICS, CodeRust()) == []


def test_cargo_clippy_keeps_nothing_when_the_appended_code_is_gone(tmp_path: Path) -> None:
    assert _cargo_clippy(tmp_path, DIAGNOSTICS, CodeRust("pub fn gone() {}")) == []


def test_cargo_clippy_scopes_macro_expansions_by_where_they_were_invoked(
    tmp_path: Path,
) -> None:
    # Macros defined in files that exist, outside the package, still expand into it
    registry_lib = tmp_path / "cargo-home" / "registry" / "src" / "dep" / "src" / "lib.rs"
    sibling_lib = tmp_path / "foo-macros" / "src" / "lib.rs"
    for path in (registry_lib, sibling_lib):
        path.parent.mkdir(parents=True)
        path.write_text("macro_rules! m { () => {} }\n")
    into_appended = {"file_name": "foo-rs/src/lib.rs", "line_start": 6}
    diagnostics = [
        _diagnostic(
            str(registry_lib), 1, "warning: from a dependency", expansion=into_appended
        ),
        _diagnostic(
            "foo-macros/src/lib.rs", 1, "warning: from a sibling", expansion=into_appended
        ),
        _diagnostic(
            "/rustc/0000/library/core/src/macros/mod.rs",
            9,
            "warning: from std through a sibling",
            expansion={
                "file_name": "foo-macros/src/lib.rs",
                "line_start": 2,
                "expansion": {"span": into_appended},
            },
        ),
        _diagnostic(
            str(registry_lib),
            1,
            "warning: from a dependency into a dependency",
            expansion={"file_name": "foo-sys/src/lib.rs", "line_start": 1},
            package="foo-sys",
        ),
    ]
    crate = _crate(tmp_path)
    # The attempt's lines, and the default of the whole package
    for only_in in (APPENDED, None):
        with _clippy_run(diagnostics):
            result = crate.cargo_clippy()
        found = result.diagnostics if only_in is None else result.only_in(only_in).diagnostics
        assert [d["rendered"] for d in found] == [
            "warning: from a dependency",
            "warning: from a sibling",
            "warning: from std through a sibling",
        ]


def test_cargo_clippy_explains_its_failure(tmp_path: Path) -> None:
    crate = _crate(tmp_path)
    in_dependency = _diagnostic(
        "foo-sys/src/lib.rs", 1, "error: in a dependency", "error", package="foo-sys"
    )
    diagnostics = [
        _diagnostic("foo-rs/src/lib.rs", 5, "error: in the appended code", "error"),
        in_dependency,
        in_dependency,
        _diagnostic("foo-sys/src/lib.rs", 2, "warning: in a dependency", package="foo-sys"),
    ]
    stderr = "error: could not compile `foo-sys` (lib) due to 1 previous error\n"
    with _clippy_run(diagnostics, stderr):
        result = crate.cargo_clippy()

    assert not result.success
    assert [d["rendered"] for d in result.diagnostics] == ["error: in the appended code"]
    # The errors left out of scope, once each, and then what cargo said
    assert result.output == f"error: in a dependency\n\n{stderr.strip()}"


def test_cargo_clippy_scoped_run_ignores_out_of_scope_errors(tmp_path: Path) -> None:
    crate = _crate(tmp_path)
    in_prefix = _diagnostic("foo-rs/src/lib.rs", 1, "error: in the prefix", "error")
    stderr = "error: could not compile `foo-rs` (lib) due to 1 previous error\n"
    with _clippy_run([in_prefix], stderr):
        result = crate.cargo_clippy().only_in(APPENDED)

    assert result.success
    assert result.diagnostics == []
    assert "error: in the prefix" in result.output
    assert stderr.strip() in result.output


def test_judge_gates_only_on_the_judged_package(tmp_path: Path) -> None:
    crate = _crate(tmp_path)
    diagnostics = [
        {
            **_diagnostic(file, 1, f"warning: in {file}", package=file.split("/")[0]),
            "code": {"code": "clippy::eq_op"},
        }
        for file in ("foo-rs/src/lib.rs", "foo-sys/src/lib.rs")
    ]
    cfg = SimpleNamespace(
        cargo_toml=crate.cargo_toml,
        vcs="none",
        output=tmp_path / "out.json",
        gate=True,
        ub=False,
    )

    class Judged(Exception):
        pass

    def build_prompt(workdir: Path, findings: list[dict[str, Any]], failure: str) -> str:
        assert [finding["file"] for finding in findings] == ["foo-rs/src/lib.rs"]
        assert failure == ""
        raise Judged

    with (
        patch("ideas.idiomaticity.Crate", return_value=crate),
        _clippy_run(diagnostics) as run,
        patch("ideas.idiomaticity.build_prompt", side_effect=build_prompt),
        pytest.raises(Judged),
    ):
        judge(cfg)  # type: ignore[arg-type]
    assert "--all-targets" in run.call_args_list[0].args[0]


def _scores(level: int = 4) -> dict[str, Any]:
    return {"dimensions": {name: {"level": level} for name in DIMENSIONS}}


def test_judge_gates_a_crate_that_does_not_build(tmp_path: Path) -> None:
    crate = _crate(tmp_path)
    crate.cargo_build = MagicMock(return_value=(True, ""))  # type: ignore[method-assign]
    error = "error[E0308]: mismatched types in a test"
    diagnostics = [
        _diagnostic("foo-rs/tests/bar.rs", 3, error, "error"),
        {
            **_diagnostic("foo-rs/src/lib.rs", 1, "warning: eq_op"),
            "code": {"code": "clippy::eq_op"},
        },
    ]
    cfg = SimpleNamespace(
        cargo_toml=crate.cargo_toml,
        vcs="none",
        output=tmp_path / "out" / "out.json",
        gate=True,
        ub=False,
        codex=None,
        model=SimpleNamespace(name="model"),
    )
    result = MagicMock()
    result.json.return_value = _scores()
    result.prompt_files.return_value = {}

    with (
        patch("ideas.idiomaticity.Crate", return_value=crate),
        _clippy_run(diagnostics, 'error: could not compile `foo-rs` (test "bar")') as run,
        patch("ideas.idiomaticity.run_codex_turn", return_value=result) as run_codex_turn,
        patch("ideas.idiomaticity.format_usage", return_value=""),
    ):
        scores = judge(cfg)  # type: ignore[arg-type]

    # The judge is shown why, and the rubric's caps are applied whatever it scored
    prompt = run_codex_turn.call_args.args[0]
    assert "# Build" in prompt and error in prompt
    assert scores["build"] == "failed"
    assert scores["lint_scan"] == "failed"
    assert scores["ub_scan"] == "skipped"
    dimensions = scores["dimensions"]
    assert dimensions["d1_project_structure_and_cargo_hygiene"]["level"] == 1
    assert dimensions["d3_error_handling_and_unsafe"]["level"] == 2
    assert "--all-targets" in run.call_args_list[0].args[0]


def test_gate_caps_d1_only_on_a_crate_that_does_not_build() -> None:
    d1 = "d1_project_structure_and_cargo_hygiene"
    assert "ungated_level" not in apply_gate(_scores(), [])["dimensions"][d1]
    dimension = apply_gate(_scores(), [], builds=False)["dimensions"][d1]
    assert dimension == {"level": 1, "ungated_level": 4}
    assert (
        "ungated_level"
        not in apply_gate(_scores(), [], builds=False)["dimensions"][
            "d3_error_handling_and_unsafe"
        ]
    )


def test_build_failure_comes_from_cargo_build_or_else_clippy() -> None:
    crate = MagicMock()
    crate.cargo_build.return_value = (False, "error: linking with `cc` failed\n")
    assert build_failure(crate, _clippy_result()) == "error: linking with `cc` failed"

    crate.cargo_build.return_value = (True, "")
    assert (
        build_failure(
            crate, _clippy_result(diagnostics=[_diagnostic("lib.rs", 1, "warning: w")])
        )
        == ""
    )
    diagnostics = [
        _diagnostic("lib.rs", 1, "warning: not why it fails"),
        _diagnostic("tests/t.rs", 2, "error: why it fails", "error"),
    ]
    assert build_failure(
        crate, _clippy_result(False, diagnostics, "error: could not compile")
    ) == ("error: why it fails\n\nerror: could not compile")


def test_prompt_shows_a_build_failure_truncated() -> None:
    assert "# Build" not in build_prompt(Path("crate"), [])
    prompt = build_prompt(Path("crate"), [], "error: x" + "y" * 100_000)
    assert "# Build" in prompt and "error: x" in prompt
    assert "[... truncated]" in prompt and "y" * 100_000 not in prompt


def test_findings_are_the_warnings_and_errors() -> None:
    diagnostics = [
        _diagnostic("lib.rs", 1, "warning: a warning\n"),
        _diagnostic("lib.rs", 2, "error: an error", "error"),
        _diagnostic("lib.rs", 2, "note: not a finding of its own", "note"),
    ]
    assert _clippy_result(diagnostics=diagnostics).render() == (
        "warning: a warning\n\nerror: an error"
    )
    assert _clippy_result(diagnostics=diagnostics[2:]).render() == ""
    # A failure is reported even when no diagnostic explains it
    failed = _clippy_result(False, diagnostics[2:], "error: failed\n")
    assert failed.render() == "error: failed"
    assert failed.render(failure=False) == ""
    assert _clippy_result(False, diagnostics[:1], "error: failed").render() == (
        "warning: a warning\n\nerror: failed"
    )
    assert _clippy_result(False, diagnostics, "error: failed").render(("error",)) == (
        "error: an error\n\nerror: failed"
    )


def test_clippy_rejects_a_translation_and_feeds_its_output_back(tmp_path: Path) -> None:
    rs_lib = tmp_path / "foo-rs" / "src" / "lib.rs"
    rs_lib.parent.mkdir(parents=True)
    rs_lib.write_text(PREFIX)

    rs_crate = MagicMock()
    rs_crate.name = "foo-rs"
    rs_crate.lib_src_path = rs_lib
    rs_crate.main_src_path = None
    rs_crate.workspace.root = tmp_path
    rs_crate.cargo_build.return_value = (True, "")
    warning = "warning: usage of `&Vec<T>` for a function parameter"

    def clippy(only_in: CodeRust) -> ClippyResult:
        assert str(only_in) in rs_lib.read_text()
        # Only the first translation takes `&Vec<i32>`
        line = next(
            (i for i, text in enumerate(rs_lib.read_text().splitlines(), 1) if "Vec" in text), 0
        )
        in_attempt = "Vec" in str(only_in)
        return _clippy_result(
            diagnostics=[_diagnostic("foo-rs/src/lib.rs", line, warning)] if in_attempt else []
        )

    rs_crate.cargo_clippy.return_value.only_in.side_effect = clippy
    responses = iter([APPENDED, CodeRust("pub fn bar(x: &[i32]) -> usize {\n    x.len()\n}")])
    respond = MagicMock(side_effect=lambda **_: Generation(code=next(responses)))

    translator = SymbolTranslator(rs_crate, _StubSnippetTranslator(respond, max_iters=2))
    result = translator.translate("c:@F@bar", CodeRust(PREFIX), MagicMock())

    assert result.success
    assert respond.call_count == 2
    # The Clippy finding is passed through the distinct lint channel.
    assert respond.call_args_list[0].kwargs["build_feedback"] == ""
    lint_feedback = respond.call_args_list[1].kwargs["lint_feedback"]
    assert "Clippy feedback:" in lint_feedback
    assert warning in lint_feedback
    assert "&[i32]" in rs_lib.read_text()


def test_a_failing_clippy_rejects_a_translation_with_nothing_in_scope(tmp_path: Path) -> None:
    rs_lib = tmp_path / "foo-rs" / "src" / "lib.rs"
    rs_lib.parent.mkdir(parents=True)
    rs_lib.write_text(PREFIX)

    rs_crate = MagicMock()
    rs_crate.name = "foo-rs"
    rs_crate.lib_src_path = rs_lib
    rs_crate.main_src_path = None
    rs_crate.workspace.root = tmp_path
    rs_crate.cargo_build.return_value = (True, "")
    error = "error[E0308]: mismatched types\n --> tests/bar.rs:3:20"
    # A Clippy failure still rejects an attempt even when no diagnostic is in its scope.
    rs_crate.cargo_clippy.return_value.only_in.side_effect = [
        _clippy_result(False, output=error),
        _clippy_result(),
    ]
    responses = iter([APPENDED, CodeRust("pub fn bar(x: &[i32]) -> usize {\n    x.len()\n}")])
    respond = MagicMock(side_effect=lambda **_: Generation(code=next(responses)))

    translator = SymbolTranslator(rs_crate, _StubSnippetTranslator(respond, max_iters=2))
    result = translator.translate("c:@F@bar", CodeRust(PREFIX), MagicMock())

    assert result.success
    assert respond.call_count == 2
    lint_feedback = respond.call_args_list[1].kwargs["lint_feedback"]
    assert "Clippy feedback:" in lint_feedback
    assert error in lint_feedback


def test_lint_logs_what_is_outside_the_attempt(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rs_lib = tmp_path / "foo-rs" / "src" / "lib.rs"
    rs_lib.parent.mkdir(parents=True)
    rs_lib.write_text(PREFIX)

    rs_crate = MagicMock()
    rs_crate.lib_src_path = rs_lib
    rs_crate.main_src_path = None
    rs_crate.cargo_build.return_value = (True, "")
    unexplained = "error: could not compile `foo-sys`"
    explained = "error: could not compile `foo-rs`"
    in_prefix = _diagnostic(str(rs_lib), 1, "error: in the prefix", "error")
    rs_crate.cargo_clippy.side_effect = [
        # Nothing in the package explains this failure, so it may be the environment's
        ClippyResult(False, [], [], unexplained, tmp_path, rs_lib),
        # The package's own error explains this one, though it is outside the attempt
        ClippyResult(False, [in_prefix], [], explained, tmp_path, rs_lib),
    ]
    responses = iter([APPENDED, CodeRust("pub fn bar(x: &[i32]) -> usize {\n    x.len()\n}")])
    respond = MagicMock(side_effect=lambda **_: Generation(code=next(responses)))

    translator = SymbolTranslator(rs_crate, _StubSnippetTranslator(respond, max_iters=2))
    with caplog.at_level("WARNING", logger="ideas.translate_recurrent"):
        result = translator.translate("c:@F@bar", CodeRust(PREFIX), MagicMock())

    assert result.success
    assert respond.call_count == 2
    logged = [
        r.getMessage() for r in caplog.records if "Ignored Clippy findings" in r.getMessage()
    ]
    assert len(logged) == 2
    assert unexplained in logged[0]
    assert "error: in the prefix" in logged[1]
    assert explained not in logged[1]


def test_clippy_warning_and_scope_error_reject_and_feed_back(tmp_path: Path) -> None:
    attempt = 0

    def wrap(**kwargs) -> Generation:
        nonlocal attempt
        attempt += 1
        catch_unwind = "std::panic::catch_unwind(|| {});" if attempt == 1 else ""
        return Generation(
            code=CodeRust(f"pub fn bar() {{\n    let _ = {attempt};\n    {catch_unwind}\n}}")
        )

    respond = MagicMock(side_effect=wrap)
    harness, hybrid_lib, _, sys_c = _make_translator(
        tmp_path,
        symbol_translator=_StubSnippetTranslator(
            MagicMock(return_value=Generation(code=CodeRust("pub fn bar() {}")))
        ),
        symbol_wrapper=_StubWrapperGenerator(respond, max_iters=2),
        tests="dummy_test",
    )
    sys_c.write_text("int bar(void) { return 0; }")
    warning = "warning: this `let _ = 1` binds nothing"

    def clippy(only_in: CodeRust) -> ClippyResult:
        assert str(only_in) in hybrid_lib.read_text()
        lines = hybrid_lib.read_text().splitlines()
        line = next((i for i, text in enumerate(lines, 1) if "let _ = 1;" in text), 0)
        in_attempt = "let _ = 1;" in str(only_in)
        return _clippy_result(
            diagnostics=[_diagnostic(str(hybrid_lib), line, warning)] if in_attempt else []
        )

    harness.hybrid.crate.cargo_clippy.return_value.only_in.side_effect = clippy  # type: ignore[attr-defined]

    sym: SymbolName = "c:@F@bar"
    group: SymbolGroup = (sym,)
    with patch(
        "ideas.translate_recurrent.generate_unimplemented_function_wrapper",
        return_value=CodeRust("pub fn bar() { unimplemented!() }"),
    ):
        pred = _run(harness, symbols={sym: _mock_bar_symbol(sym)}, dependencies={group: []})

    assert pred.complete
    assert respond.call_count == 2
    retry_feedback = respond.call_args_list[1].kwargs
    assert "catch_unwind" in retry_feedback["scope_feedback"]
    lint_feedback = retry_feedback["lint_feedback"]
    assert "Clippy warnings:" in lint_feedback
    assert warning in lint_feedback
    assert "let _ = 2;" in hybrid_lib.read_text()


def test_exhausted_warning_candidate_is_accepted(tmp_path: Path) -> None:
    wrapper = CodeRust("pub fn bar() {}")
    respond = MagicMock(return_value=Generation(code=wrapper))
    harness, hybrid_lib, _, sys_c = _make_translator(
        tmp_path,
        symbol_translator=_StubSnippetTranslator(
            MagicMock(return_value=Generation(code=CodeRust("pub fn bar() {}")))
        ),
        symbol_wrapper=_StubWrapperGenerator(respond, max_iters=1),
        tests="dummy_test",
        max_iters=1,
    )
    sys_c.write_text("int bar(void) { return 0; }")
    warning = "warning: this `let _ = 1` binds nothing"
    harness.hybrid.crate.cargo_clippy.return_value.only_in.return_value = _clippy_result(  # type: ignore[attr-defined]
        diagnostics=[_diagnostic(str(hybrid_lib), 1, warning)]
    )

    sym: SymbolName = "c:@F@bar"
    group: SymbolGroup = (sym,)
    with patch(
        "ideas.translate_recurrent.generate_unimplemented_function_wrapper",
        return_value=CodeRust("pub fn bar() { unimplemented!() }"),
    ):
        _run(harness, symbols={sym: _mock_bar_symbol(sym)}, dependencies={group: []})

    assert respond.call_count == 1
    assert sym in harness.hybrid.wrappers
    assert "pub fn bar() {}" in hybrid_lib.read_text()


def test_warning_candidate_is_discarded_when_next_attempt_is_rejected(tmp_path: Path) -> None:
    wrapper = CodeRust("pub fn bar() {}")
    invalid_wrapper = CodeRust("pub fn unrelated() {}")
    respond = MagicMock(
        side_effect=[Generation(code=wrapper), Generation(code=invalid_wrapper)]
    )
    harness, hybrid_lib, _, sys_c = _make_translator(
        tmp_path,
        symbol_translator=_StubSnippetTranslator(
            MagicMock(return_value=Generation(code=CodeRust("pub fn bar() {}")))
        ),
        symbol_wrapper=_StubWrapperGenerator(respond, max_iters=2),
        tests="dummy_test",
        max_iters=1,
    )
    sys_c.write_text("int bar(void) { return 0; }")
    warning = "warning: this `let _ = 1` binds nothing"

    def clippy(only_in: CodeRust) -> ClippyResult:
        if only_in == wrapper:
            return _clippy_result(diagnostics=[_diagnostic(str(hybrid_lib), 1, warning)])
        return _clippy_result()

    harness.hybrid.crate.cargo_clippy.return_value.only_in.side_effect = clippy  # type: ignore[attr-defined]

    sym: SymbolName = "c:@F@bar"
    group: SymbolGroup = (sym,)
    with patch(
        "ideas.translate_recurrent.generate_unimplemented_function_wrapper",
        return_value=CodeRust("pub fn bar() { unimplemented!() }"),
    ):
        _run(harness, symbols={sym: _mock_bar_symbol(sym)}, dependencies={group: []})

    assert respond.call_count == 2
    assert warning in respond.call_args_list[1].kwargs["lint_feedback"]
    assert sym not in harness.hybrid.wrappers
    assert "pub fn bar() {}" not in hybrid_lib.read_text()
    assert "pub fn unrelated() {}" not in hybrid_lib.read_text()
    assert "{ return 0; }" in sys_c.read_text()


def test_clippy_command_failure_still_rejects_final_candidate(tmp_path: Path) -> None:
    wrapper = CodeRust("pub fn bar() {}")
    respond = MagicMock(return_value=Generation(code=wrapper))
    harness, hybrid_lib, _, sys_c = _make_translator(
        tmp_path,
        symbol_translator=_StubSnippetTranslator(
            MagicMock(return_value=Generation(code=CodeRust("pub fn bar() {}")))
        ),
        symbol_wrapper=_StubWrapperGenerator(respond, max_iters=1),
        tests="dummy_test",
        max_iters=1,
    )
    sys_c.write_text("int bar(void) { return 0; }")
    harness.hybrid.crate.cargo_clippy.return_value.only_in.return_value = _clippy_result(  # type: ignore[attr-defined]
        False, [_diagnostic(str(hybrid_lib), 1, "warning: a lint")], "cargo clippy failed"
    )

    sym: SymbolName = "c:@F@bar"
    group: SymbolGroup = (sym,)
    with patch(
        "ideas.translate_recurrent.generate_unimplemented_function_wrapper",
        return_value=CodeRust("pub fn bar() { unimplemented!() }"),
    ):
        _run(harness, symbols={sym: _mock_bar_symbol(sym)}, dependencies={group: []})

    assert sym not in harness.hybrid.wrappers
    assert "{ return 0; }" in sys_c.read_text()


def test_wrapper_hears_from_clippy_and_its_tests_at_once(tmp_path: Path) -> None:
    tests_mod = "test_var_counter"
    # Both compare the C global against the translation, so either passes validation
    first = _variable_wrapper("assert_eq!(unsafe { counter }, safe_crate::COUNTER);")
    second = _variable_wrapper("assert_eq!(safe_crate::COUNTER, unsafe { counter });")
    respond = MagicMock(side_effect=[Generation(code=first), Generation(code=second)])
    harness, hybrid_lib, _, sys_c = _make_translator(
        tmp_path,
        symbol_translator=_StubSnippetTranslator(
            MagicMock(return_value=Generation(code=CodeRust("pub static COUNTER: i32 = 0;")))
        ),
        symbol_wrapper=_StubWrapperGenerator(respond, max_iters=2),
        tests="dummy_test",
    )
    sys_c.write_text(str(_GLOBAL_VARIABLE))
    warning = "warning: a lint in the first wrapper"
    failure = "test test_var_counter::initial_value_matches ... FAILED"

    def first_on_disk() -> bool:
        return str(first) in hybrid_lib.read_text()

    def clippy(only_in: CodeRust) -> ClippyResult:
        return _clippy_result(
            diagnostics=[_diagnostic(str(hybrid_lib), 1, warning)] if first_on_disk() else []
        )

    def cargo_test(name: str, **kwargs: Any) -> tuple[bool, str, str, int]:
        fails = name == tests_mod and first_on_disk()
        return (not fails, "", failure if fails else "", int(fails))

    crate: Any = harness.hybrid.crate
    crate.cargo_clippy.return_value.only_in.side_effect = clippy
    crate.cargo_test.side_effect = cargo_test

    symbols, dependencies = get_symbols_and_dependencies(
        [ast.extract_info_c(ast.create_translation_unit(_GLOBAL_VARIABLE))]
    )
    binding = CodeRust('extern "C" {\n    pub static mut counter: ::std::os::raw::c_int;\n}')
    with (
        patch("ideas.hybrid.bindgen_bindings", return_value={"counter": binding}),
        patch(
            "ideas.translate_recurrent.generate_unimplemented_variable_wrapper",
            return_value=(first, tests_mod, _SYNC_FNS),
        ),
    ):
        pred = _run(harness, symbols=symbols, dependencies=dependencies)

    assert pred.complete
    assert respond.call_count == 2
    lint_feedback = respond.call_args_list[1].kwargs["lint_feedback"]
    assert f"Clippy warnings:\n{warning}" in lint_feedback
    build_feedback = respond.call_args_list[1].kwargs["build_feedback"]
    assert f"Running `cargo test {tests_mod}` fails!\n{failure}" in build_feedback


def test_translation_and_wrapper_prompts_include_their_lint_command(tmp_path: Path) -> None:
    rust_src = tmp_path / "foo-rs" / "src" / "lib.rs"
    hybrid_src = tmp_path / "foo-hybrid" / "src" / "lib.rs"
    c_src = tmp_path / "foo-sys" / "src" / "lib.c"
    common: dict[str, Any] = dict(
        codex=CodexConfig(),
        model=ModelConfig(name="test"),
        workdir=tmp_path,
        rust_src=rust_src,
        c_src=c_src,
    )
    feedback = dict(feedback="", build_feedback="", lint_feedback="", scope_feedback="")

    with patch("ideas.translate_snippet.run_codex") as run_codex:
        SnippetTranslator(**common).translate(
            snippet=MagicMock(), prior_translation=None, translation=None, **feedback
        )
    snippet_prompt = run_codex.call_args.args[0]

    symbol = MagicMock(is_type=False, is_function=True, spelling="bar", bindgen_name="bar")
    with patch("ideas.wrapper.run_codex") as run_codex:
        WrapperGenerator(**common).generate(
            example_wrapper=CodeRust("pub fn bar(foo: i32) { unimplemented!() }"),
            prior_wrapper=None,
            wrapper=None,
            symbol=symbol,
            wrapped_crate="safe",
            hybrid_src=hybrid_src,
            **feedback,
        )
    wrapper_prompt = run_codex.call_args.args[0]

    assert "`cargo clippy --lib -p foo-rs`" in snippet_prompt
    assert "`cargo clippy --lib -p foo-hybrid`" in wrapper_prompt
    assert "A warning caused by unchanged template content" in wrapper_prompt
    assert "specific" in wrapper_prompt
    assert "`#[allow(lint_name)]`" in wrapper_prompt
    assert "generated body" in wrapper_prompt
