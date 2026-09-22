#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import json
import logging
from typing import Literal, Protocol
from dataclasses import dataclass, field

from .ast import CodeC, Symbol, SymbolGroup
from .ast_rust import CodeRust
from .tools import Crate

logger = logging.getLogger("ideas.oracle")


class UntrustedBaselineError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    symbol: Symbol
    snippet: CodeC
    translation: CodeRust
    wrapper: CodeRust | None = None
    wrap_rejection: str = ""
    wrapped: bool = True


@dataclass
class Verdict:
    rejected: bool = False
    stage: Literal["wrap", "review"] | None = None
    feedback: str = ""
    wrap_feedback: str = ""
    evidence: str = ""
    message: str = ""
    blamed: set[str] = field(default_factory=set)


class SymbolOracle(Protocol):
    def exercises(self, symbol: Symbol) -> bool: ...

    @property
    def baseline_failures(self) -> frozenset[str]: ...

    @property
    def expected_tests(self) -> frozenset[str]: ...

    @property
    def regressions(self) -> dict[str, SymbolGroup]: ...

    def build(self) -> str: ...

    def baseline(self) -> None: ...

    def audit(self) -> str: ...

    def judge(self, candidate: Candidate) -> Verdict: ...

    def blame(self, group: SymbolGroup, signals: set[str]) -> None: ...


def _wrap_rejection(candidate: Candidate) -> Verdict:
    return Verdict(rejected=True, stage="wrap", feedback=_unwrappable_feedback(candidate))


class NullOracle:
    def exercises(self, symbol: Symbol) -> bool:
        return False

    @property
    def baseline_failures(self) -> frozenset[str]:
        return frozenset()

    @property
    def expected_tests(self) -> frozenset[str]:
        return frozenset()

    @property
    def regressions(self) -> dict[str, SymbolGroup]:
        return {}

    def build(self) -> str:
        return ""

    def baseline(self) -> None:
        pass

    def audit(self) -> str:
        return ""

    def judge(self, candidate: Candidate) -> Verdict:
        # An unwrappable translation is rejected whether or not anything can run the code
        return Verdict() if candidate.wrapped else _wrap_rejection(candidate)

    def blame(self, group: SymbolGroup, signals: set[str]) -> None:
        pass


class TestOracle:
    def __init__(self, crate: Crate, tests: str, *, share_evidence: bool = False):
        self._crate = crate
        self._tests = tests
        self._share_evidence = share_evidence
        self._baseline_failures: frozenset[str] = frozenset()
        self._expected_tests: frozenset[str] = frozenset()
        self._regressions: dict[str, SymbolGroup] = {}

    def exercises(self, symbol: Symbol) -> bool:
        return True

    @property
    def baseline_failures(self) -> frozenset[str]:
        return self._baseline_failures

    @property
    def expected_tests(self) -> frozenset[str]:
        return self._expected_tests

    @property
    def regressions(self) -> dict[str, SymbolGroup]:
        return dict(self._regressions)

    def build(self) -> str:
        # cargo build skips test targets, which can fail to compile on their own
        return self._run(self._tests, build_only=True)[0]

    def _run(
        self, name: str, *, skip: list[str] | None = None, build_only: bool = False
    ) -> tuple[str, dict[str, str]]:
        passes, output, error, _ = self._crate.cargo_test(
            name,
            skip=skip,
            build_only=build_only,
            # nextest warns and ignores the format under --no-run, so don't ask for it
            message_format=None if build_only else "libtest-json",
        )
        # A --no-run invocation reports no test outcomes, so there is nothing worth recording
        if not build_only:
            crate_dir = self._crate.cargo_toml.parent
            run_events = crate_dir / f"cargo_{name}.jsonl"
            run_events.write_text(output, encoding="utf-8")
            run_log = crate_dir / f"cargo_{name}.log"
            run_log.write_text(error, encoding="utf-8")
            # Staged, not committed: the run belongs to the caller's next commit, and forced
            # because the workspace .gitignore excludes *.log
            self._crate.vcs.add(run_events, run_log, force=True)
        # Feedback is empty when the tests pass, so callers can treat it as the failure signal
        events = _extract_test_events(output)
        if passes:
            return "", events

        action = f"Building test `{name}`" if build_only else f"Running `cargo test {name}`"
        return f"{action} fails!\n{error}", events

    def blame(self, group: SymbolGroup, signals: set[str]):
        self._regressions.update(dict.fromkeys(signals, group))

    def baseline(self):
        self._baseline_failures = frozenset()
        self._expected_tests = frozenset()
        self._regressions = {}

        logger.info(f"Verifying test `{self._tests}` against the untranslated crate ...")

        # A baseline nothing can build says nothing about the tests, so don't trust one
        builds, output = self._crate.cargo_build()
        feedback = f"Running `cargo build` fails!\n{output}" if not builds else self.build()
        if feedback:
            raise UntrustedBaselineError(
                f"Crate `{self._crate.name}` does not build before translation!\n{feedback}"
            )

        # Then run the tests to see which pass and which fail against the untranslated C
        feedback, events = self._run(self._tests)
        passing = {name for name, event in events.items() if event == "ok"}
        broken = {name for name, event in events.items() if event == "failed"}
        # A later run missing one of these was truncated, not green
        self._expected_tests = frozenset(events)
        # An ignored test never ran, so it is no evidence that anything can judge a translation
        if not passing:
            raise UntrustedBaselineError(
                f"No test in `{self._tests}` passes against the untranslated C in "
                f"`{self._crate.name}`, so the tests are broken and cannot judge a "
                f"translation!\n{feedback}"
            )

        if broken:
            logger.warning(
                "Excluding test(s) that already fail against the untranslated C: "
                f"{', '.join(sorted(broken))}"
            )
        logger.info(f"Verified {len(passing)}/{len(events)} of `{self._tests}` passed!")
        self._baseline_failures = frozenset(broken)

    def audit(self) -> str:
        error, events = self._run(self._tests, skip=sorted(self._baseline_failures))
        # A test the baseline ran but this run never mentioned did not pass, it disappeared
        vanished = self._expected_tests - self._baseline_failures - set(events)
        if vanished:
            missing = ", ".join(sorted(vanished))
            return f"Test(s) did not run against the stranded crate: {missing}\n\n{error}"
        return error

    def judge(self, candidate: Candidate) -> Verdict:
        if not candidate.wrapped:
            return _wrap_rejection(candidate)

        symbol = candidate.symbol
        logger.info(f"Testing symbol `{symbol.name}` ...")

        # Skip only what the C already fails; an earlier regression still runs, just unblamed
        error, events = self._run(self._tests, skip=sorted(self._baseline_failures))

        # The baseline and earlier symbol groups already own their failures
        prior_failures = self._baseline_failures | set(self._regressions)
        # Only a test nobody else owns can say anything about this symbol
        judged = set(events) - prior_failures
        regressed = {n for n in judged if events[n] == "failed"}
        # A test the baseline ran but this run never mentioned did not pass, it disappeared
        vanished = self._expected_tests - prior_failures - set(events)

        if not events and error:  # cargo test failed
            regressed, vanished = set(), set()  # nothing ran, so blame no test
            evidence = error
            message = f"Error testing symbol `{symbol.name}`"
            logger.error(message)
        elif regressed or vanished:
            blamed = sorted(regressed) + [f"{n} (did not run)" for n in sorted(vanished)]
            evidence = f"Regressed test(s): {', '.join(blamed)}\n\n{error}"
            message = f"Failed to test symbol `{symbol.name}`"
            logger.error(message)
        elif not judged:  # every test was already blamed elsewhere
            evidence = ""
            message = f"Skipped testing symbol `{symbol.name}`"
            logger.info(message)
        else:
            evidence = ""
            message = f"Tested symbol `{symbol.name}`"
            logger.info(message)

        feedback = _diverged_feedback(symbol) if evidence else ""
        wrap_feedback = _diverged_wrap_feedback(symbol) if evidence else ""
        if evidence and self._share_evidence:
            feedback += _evidence_feedback(evidence)
            wrap_feedback += _evidence_feedback(evidence)

        return Verdict(
            rejected=bool(evidence),
            stage="review" if evidence else None,
            feedback=feedback,
            wrap_feedback=wrap_feedback,
            evidence=evidence,
            message=message,
            blamed=regressed | vanished,
        )


def _unwrappable_feedback(candidate: Candidate) -> str:
    symbol = candidate.symbol
    name = symbol.spelling
    preamble = (
        f"The attempt to give Rust `{name}` in `prior_translation` a working C-compatible FFI "
        "wrapper was rejected. That alone does not prove the safe translation is wrong: the "
        "attached reason may instead be a wrapper scope, syntax, build, or test-fixture defect. "
        "Use its exact reason rather than guessing. If it is wrapper-only, preserve "
        "`prior_translation` exactly. Only where the rejection demonstrates an incompatible safe "
        "interface or representation, regenerate the complete `prior_translation` so the exact "
        "Rust symbol exists once and a wrapper can bridge every valid C state without changing "
        "its meaning. Do not append a renamed replacement or make the Rust behavior less faithful "
        "merely to ease conversion. "
    )
    coda = (
        f"The guidance above is about `{name}`: change the other items in `prior_translation` "
        f"only where `{name}` requires it, and leave the rest as they are. "
        "Items in `prior_translation` are the rejected group and may be revised; previously "
        "accepted reference code is not. Keep the translation fully memory-safe and free of "
        "`unsafe` constructs, and keep its observable behavior identical to the C `snippet`."
    )

    if symbol.is_variable:
        guidance = (
            f"Keep `{name}` a `static` or `const` item, initialized exactly as the C `snippet` "
            "initializes it. It must not become a function, a lazily-initialized cell, or "
            "anything that hands out a fresh value per call, and a field that points at "
            "another global in C must borrow that same translated global rather than a "
            "private duplicate. If a field's type makes that impossible in safe Rust, change "
            "that field's type instead of the item's shape. "
        )
    elif symbol.is_type:
        guidance = (
            f"Choose one canonical representation for `{name}` that a wrapper can convert "
            "field by field while preserving live contents, null and sentinel states, aliases, "
            "pointer identity, callbacks, allocation ownership, and logical length versus "
            "capacity. The Rust fields need not copy the C layout, but every C state accepted by "
            "the program needs a total safe representation. Keep per-instance graph ownership "
            "reachable from the instance. Avoid required `'static` or exclusive borrows that a "
            "call-scoped C value cannot supply, and avoid an erased or unit representation that "
            "cannot be converted back. "
        )
    else:
        guidance = (
            f"Emit `{name}` under that exact name with explicit, concrete argument and return "
            "types that a C caller can convert for the duration of one call. Preserve ownership "
            "and aliases without requiring a borrow to outlive its owner, hiding required state "
            "inside a closure, or returning a reference whose backing owner is unavailable at "
            "the boundary. If `{name}` belongs to a mutually recursive candidate, include and "
            "correct the whole required group rather than adding a differently named entry point. "
        )

    feedback = preamble + guidance + coda
    # Compiler and wrapper-template output, not ported-suite evidence, so it is safe to
    # hand the translator verbatim
    if candidate.wrap_rejection:
        feedback += (
            f"\n\nThe last attempt at a wrapper was rejected with:\n{candidate.wrap_rejection}"
        )
    return feedback


def _diverged_feedback(symbol: Symbol) -> str:
    name = symbol.spelling
    return (
        f"Switching `{name}` from C to the routed Rust candidate changed the program's "
        "observable behavior. This signal covers the translation together with its FFI "
        "conversion, so it does not identify which layer is wrong. Re-audit `prior_translation` "
        "against the C `snippet`; if any Rust step differs, it does not match the behavior of "
        "the C `snippet` and must be corrected, but do not distort a correct safe operation to "
        "compensate for a wrapper defect. "
        f"Start with `{name}` and every translated helper first reached through it. Re-derive "
        "their control flow statement by statement and make a mutation ledger for storage, "
        "links, root/head/tail, size, capacity, iterator state, out-parameters, allocation "
        "tokens, rollback, and destruction. Check that handles are resolved before inspecting "
        "pointee values; independent conditional reads stay lazy; live length is not capacity; "
        "and no idiomatic Rust layout is used as a C size. "
        "Also check null and sentinel states, pointer identity and shallow aliases, callback "
        "reentrancy and post-callback rereads, configured allocator call order and failure, C "
        "string termination, integer conversion and wrapping, every early return, and states "
        "the public C representation lets callers mutate. Do not assume inputs are normalized "
        "or well formed where C has defined behavior. "
        "If this audit shows the safe candidate is correct, return it unchanged and leave the "
        "boundary correction to the wrapper retry. Otherwise return one complete replacement for "
        "`prior_translation`, preserving exact symbol names. Do not retain a broken definition "
        "and add a renamed substitute. Make minimal, targeted changes and treat C as the source "
        "of truth even where it looks buggy."
    )


def _diverged_wrap_feedback(symbol: Symbol) -> str:
    name = symbol.spelling
    return (
        f"The wrapper in `prior_wrapper` passed its local build and any wrapper-specific tests, "
        f"but routing `{name}` through it changed the program's observable behavior. The oracle "
        "cannot distinguish a safe-operation defect from a boundary defect, so change the "
        "wrapper only where a comparison with C and `wrapped_crate_code` demonstrates it is "
        "wrong. Preserve exactly "
        "the top-level items and signatures in `example_wrapper`; for a function wrapper, edit "
        "only its existing body and keep any helper or callback context local to that body. "
        f"Verify that every path on which C reaches the operation makes exactly one call to the "
        f"exact safe `{name}` entry point and that the wrapper performs conversion and "
        "synchronization only. Recheck "
        "both directions for null and sentinel states, pointer identity and graph aliases, live "
        "length versus capacity, initialized versus dead storage, integer conversions, C string "
        "NUL termination, callback ABI and reentrant mutation, globals, and out-parameters. A "
        "new or resized C buffer must contain all live Rust results before it is published, and "
        "old storage must be released through the configured allocator only when C would do so. "
        f"Do not compensate for a suspect translation by reimplementing `{name}`'s logic, "
        "calling a renamed substitute, discarding a safe mutation, or stepping outside what "
        "`example_wrapper` allows. If the exact safe symbol or a convertible representation is "
        "missing, let wrapper generation fail so the translation can be regenerated."
    )


def _evidence_feedback(evidence: str) -> str:
    return (
        "\n\nThe test suite that caught this reported:\n"
        f"{evidence}\n\n"
        "Use this only to locate the divergence in the translation. Do not edit, disable, or "
        "special-case anything to satisfy a specific test: the suite is the check, not the "
        "specification, and the C `snippet` remains the source of truth."
    )


def _extract_test_events(output: str) -> dict[str, str]:
    events: dict[str, str] = {}
    for line in output.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "test":
            continue
        event = obj.get("event")
        if event not in {"ok", "failed", "ignored"}:
            continue
        name = str(obj.get("name", "")).rsplit("$", 1)[-1].strip()
        if name:
            events[name] = event

    return events
