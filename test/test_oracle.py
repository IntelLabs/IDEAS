#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import json
from unittest.mock import MagicMock

import pytest

# Module-qualified so pytest does not try to collect `TestOracle` as a test class
from ideas import oracle as oracle_mod
from ideas.ast import CodeC
from ideas.ast_rust import CodeRust
from ideas.oracle import UntrustedBaselineError

_TESTS = "smoke"


def _libtest_json(**events: str) -> str:
    return "\n".join(
        json.dumps({"type": "test", "name": f"{_TESTS}${name}", "event": event})
        for name, event in events.items()
    )


# The crate comes back too so a test can stub or inspect the cargo calls
def _oracle(*runs: tuple, share: bool = False) -> tuple[oracle_mod.TestOracle, MagicMock]:
    crate = MagicMock()
    crate.name = "foo"
    crate.cargo_build.return_value = (True, "")
    crate.cargo_test.side_effect = [
        # baseline's build_only pre-pass: crate and test harness compile
        (True, "", "", 0),
        *runs,
    ]
    return oracle_mod.TestOracle(crate, _TESTS, share_evidence=share), crate


def _symbol(name: str = "c:@F@bar") -> MagicMock:
    symbol = MagicMock()
    symbol.name = name
    return symbol


def _candidate(name: str = "c:@F@bar", *, wrapped: bool = True) -> oracle_mod.Candidate:
    return oracle_mod.Candidate(
        symbol=_symbol(name),
        snippet=CodeC("int bar(void) { return 0; }"),
        translation=CodeRust("pub fn bar() {}"),
        wrapper=CodeRust("// wrapper") if wrapped else None,
        wrapped=wrapped,
    )


def test_baseline_excludes_only_the_broken_ones() -> None:
    # One test passes, one already fails against the untranslated C
    oracle, _ = _oracle((False, _libtest_json(works="ok", broken="failed"), "", 101))

    oracle.baseline()
    assert oracle.baseline_failures == {"broken"}


def test_baseline_does_not_disable_ignored_tests() -> None:
    # An ignored test never ran, so it is not a failure to exclude
    oracle, _ = _oracle((True, _libtest_json(works="ok", skipped="ignored"), "", 0))

    oracle.baseline()
    assert oracle.baseline_failures == set()


def test_a_run_records_its_output_for_the_next_commit(tmp_path) -> None:
    output = _libtest_json(works="ok")
    oracle, crate = _oracle((True, output, "Summary [0.1s] 1 test run: 1 passed", 0))
    crate.cargo_toml = tmp_path / "Cargo.toml"

    oracle.baseline()

    events = tmp_path / f"cargo_{_TESTS}.jsonl"
    log = tmp_path / f"cargo_{_TESTS}.log"
    assert events.read_text() == output
    assert log.read_text() == "Summary [0.1s] 1 test run: 1 passed"
    # Staged only: the caller's commit is what puts the run in the history
    crate.vcs.add.assert_called_once_with(events, log, force=True)
    crate.vcs.commit.assert_not_called()


@pytest.mark.parametrize(
    "output",
    [
        _libtest_json(broken="failed"),
        _libtest_json(skipped="ignored"),
        _libtest_json(broken="failed", skipped="ignored"),
        "",
    ],
    ids=["all_broken", "all_ignored", "broken_and_ignored", "never_ran"],
)
def test_baseline_aborts_when_nothing_can_judge(output: str) -> None:
    oracle, _ = _oracle((False, output, "", 101))

    with pytest.raises(UntrustedBaselineError, match="tests are broken"):
        oracle.baseline()


def test_baseline_aborts_when_the_crate_does_not_build() -> None:
    oracle, crate = _oracle()
    crate.cargo_build.return_value = (False, "undefined reference")

    with pytest.raises(UntrustedBaselineError, match="does not build before translation"):
        oracle.baseline()


def test_baseline_failure_alone_does_not_blame_the_symbol() -> None:
    oracle, crate = _oracle(
        (False, _libtest_json(works="ok", broken="failed"), "", 101),
        # `broken` is excluded, so the run is red only for what this symbol did
        (True, _libtest_json(works="ok"), "", 0),
    )
    oracle.baseline()

    verdict = oracle.judge(_candidate())

    assert not verdict.rejected
    assert verdict.blamed == set()
    assert crate.cargo_test.call_args.kwargs["skip"] == ["broken"]


def test_audit_reports_a_suite_the_stranded_crate_cannot_carry() -> None:
    oracle, crate = _oracle(
        (False, _libtest_json(works="ok", broken="failed"), "", 101),
        (False, "", "undefined reference to `bar'", 101),
    )
    oracle.baseline()

    findings = oracle.audit()

    assert "undefined reference" in findings
    # The audit judges the translation, so it excludes only what the C already failed
    assert crate.cargo_test.call_args.kwargs["skip"] == ["broken"]


def test_audit_of_a_green_suite_finds_nothing() -> None:
    oracle, _ = _oracle(
        (True, _libtest_json(works="ok"), "", 0),
        (True, _libtest_json(works="ok"), "", 0),
    )
    oracle.baseline()

    assert oracle.audit() == ""


def test_audit_rejects_a_zero_exit_run_that_dropped_a_test() -> None:
    oracle, _ = _oracle(
        (True, _libtest_json(works="ok", also="ok"), "", 0),
        # The stranded crate ran only half the suite and still exited clean
        (True, _libtest_json(works="ok"), "0 tests skipped", 0),
    )
    oracle.baseline()

    assert "did not run against the stranded crate: also" in oracle.audit()


def test_new_failure_beside_a_baseline_failure_is_blamed() -> None:
    oracle, _ = _oracle(
        (False, _libtest_json(works="ok", fresh="ok", broken="failed"), "", 101),
        (False, _libtest_json(works="ok", fresh="failed"), "1 test failed", 101),
    )
    oracle.baseline()

    verdict = oracle.judge(_candidate())

    assert verdict.rejected
    assert verdict.blamed == {"fresh"}
    assert "Regressed test(s): fresh" in verdict.evidence


def test_feedback_never_carries_ported_suite_evidence() -> None:
    # The ported suite is the ground-truth signal, so a model that can read which test broke
    # and how can special-case it instead of fixing the translation
    oracle, _ = _oracle(
        (True, _libtest_json(alpha="ok", checks_utf8_boundary="ok"), "", 0),
        (
            False,
            _libtest_json(alpha="ok", checks_utf8_boundary="failed"),
            "assertion `left == right` failed\n  left: 4\n right: 3",
            101,
        ),
    )
    oracle.baseline()

    verdict = oracle.judge(_candidate())

    assert verdict.rejected
    assert "checks_utf8_boundary" in verdict.evidence
    assert "left: 4" in verdict.evidence
    # Neither the name of the broken test nor what it asserted may reach the model
    assert verdict.feedback and verdict.wrap_feedback
    for text in (verdict.feedback, verdict.wrap_feedback):
        assert "checks_utf8_boundary" not in text
        assert "assertion" not in text
        assert "left: 4" not in text


def test_share_evidence_opens_the_firewall() -> None:
    # Opt-in experiment: measure what the model does when it can see the failure
    oracle, _ = _oracle(
        (True, _libtest_json(alpha="ok", checks_utf8_boundary="ok"), "", 0),
        (
            False,
            _libtest_json(alpha="ok", checks_utf8_boundary="failed"),
            "assertion `left == right` failed\n  left: 4\n right: 3",
            101,
        ),
        share=True,
    )
    oracle.baseline()

    verdict = oracle.judge(_candidate())

    assert verdict.rejected
    for text in (verdict.feedback, verdict.wrap_feedback):
        assert "checks_utf8_boundary" in text
        assert "left: 4" in text


def test_share_evidence_stays_quiet_when_nothing_regressed() -> None:
    oracle, _ = _oracle(
        (True, _libtest_json(alpha="ok"), "", 0),
        (True, _libtest_json(alpha="ok"), "", 0),
        share=True,
    )
    oracle.baseline()

    verdict = oracle.judge(_candidate())

    assert not verdict.rejected
    assert not verdict.feedback and not verdict.wrap_feedback


def test_truncated_test_run_is_not_a_pass() -> None:
    # cargo reports a pass, but a test the baseline ran never reported at all
    oracle, _ = _oracle(
        (True, _libtest_json(alpha="ok", beta="ok"), "", 0),
        (True, _libtest_json(alpha="ok"), "", 0),
    )
    oracle.baseline()

    verdict = oracle.judge(_candidate())

    assert verdict.rejected
    assert verdict.blamed == {"beta"}
    assert "beta (did not run)" in verdict.evidence


def test_a_regression_does_not_join_the_baseline() -> None:
    oracle, _ = _oracle(
        (False, _libtest_json(works="ok", fresh="ok", broken="failed"), "", 101),
        (False, _libtest_json(works="ok", fresh="failed"), "1 test failed", 101),
    )
    oracle.baseline()
    verdict = oracle.judge(_candidate())
    oracle.blame(("c:@F@bar",), verdict.blamed)

    # The baseline is what the C fails; a regression is accounted for via regressions instead
    assert oracle.baseline_failures == {"broken"}
    assert oracle.regressions == {"fresh": ("c:@F@bar",)}


def test_a_blamed_test_does_not_judge_a_later_symbol() -> None:
    oracle, _ = _oracle(
        (False, _libtest_json(works="ok", broken="failed"), "", 101),
        (False, _libtest_json(works="failed"), "1 test failed", 101),
        (False, _libtest_json(works="failed"), "1 test failed", 101),
    )
    oracle.baseline()
    oracle.blame(("c:@F@bar",), oracle.judge(_candidate("c:@F@bar")).blamed)

    verdict = oracle.judge(_candidate("c:@F@baz"))

    assert not verdict.rejected
    assert verdict.blamed == set()
    # Every remaining test is owned elsewhere, so none of them says anything about this symbol
    assert "Skipped testing symbol" in verdict.message
