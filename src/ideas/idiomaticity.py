#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import os
import re
import sys
import json
import shutil
import logging
from typing import Any, cast
from pathlib import Path
from dataclasses import dataclass, field

import hydra
from omegaconf import MISSING, OmegaConf
from hydra.core.config_store import ConfigStore

from ideas.model import ModelConfig, format_usage
from ideas.codex import CodexConfig, run_codex_turn
from ideas.tools import ClippyResult, Crate

logger = logging.getLogger("ideas.idiomaticity")

# Clippy JSON names a lint, never its group, so every other group is silenced. `--force-warn`
# because it is the only level an `#![allow(...)]` in the reviewed code cannot override.
GATE_GROUPS = ["clippy::correctness", "clippy::suspicious"]
# `--cap-lints=warn` so only a target that does not compile fails the run
GATE_LINTS = [
    "-Aclippy::all",
    *(f"--force-warn={group}" for group in GATE_GROUPS),
    "--cap-lints=warn",
]

# UB gates harder than a lint. Leaks are safe Rust, so `cargo_rapx` skips rapx's leak detector.
UB_CAP = 1
LINT_CAP = 2
# D1's rubric starts level 2 at "builds"
BUILD_CAP = 1
# Enough of a build failure for the judge to see what is broken
BUILD_FAILURE_CHARS = 8000

# Anchored at line start so a commented-out attribute does not count
FORBID_UNSAFE = re.compile(r"^\s*#!\[forbid\([^)]*\bunsafe_code\b", re.MULTILINE)

DIMENSIONS: dict[str, str] = {
    "d1_project_structure_and_cargo_hygiene": """\
## D1 Project Structure and Cargo Hygiene

Does the translation look like a normal Rust project a maintainer would write?

- **1 No hygiene, nothing pinned:** no `Cargo.toml`; `cargo fmt --check` fails; \
`clippy::correctness`/`clippy::suspicious` fire; monolithic file structure with no \
delineated boundaries; unused dependencies present; no tests or trivial tests; every item \
`pub`.
- **2 Ad hoc:** builds but layout is ad hoc; formatting inconsistent; clippy warnings \
unaddressed; tests exist but are all in `main.rs` or the top of a single file; some \
`todo!`/`dbg!` in shipped code.
- **3 Standard layout:** standard cargo layout (`src/lib.rs` and/or `src/main.rs`, `tests/` for \
integration, `#[cfg(test)] mod tests` for unit); `Cargo.toml` has `name` and `version`; \
`cargo fmt --check` and `cargo clippy -D warnings` clean; `README.md` present with \
build/run instructions.
- **4 Deliberate structure:** lib + bin split where both apply (logic in `lib.rs`, thin \
`main.rs`); `[dev-dependencies]` correctly separated; no unused deps; module style \
(`mod.rs` vs `foo.rs` + `foo/`) consistent; `pub(crate)`/`pub(super)` narrow visibility; \
no residual `dbg!`/`todo!`/`unimplemented!` in shipped code; `Cargo.lock` handled per \
official guidance (included for bins, not for libs); integration tests use fixtures \
rather than duplicating setup.
- **5 Template-quality:** `[features]` used purposefully with an explicit `default` and `dep:` \
syntax for optional deps; project layout and tooling could serve as a template for other \
Rust projects within the domain.

**Anti-idioms:** `todo!()`/`unimplemented!()`/`dbg!()` in shipped code; every item `pub`; \
unused dependencies.

**Idiomatic signals:** modules mirror the domain, not the file listing; `pub(crate)` and \
`pub(super)` used to narrow visibility; unit tests live next to the code they test; \
integration tests are their own crate under `tests/`; `[features]` used to gate optional \
deps, with a documented `default`.""",
    "d2_ownership_and_data_flow": """\
## D2 Ownership and Data Flow

Does the code work *with* ownership rather than around it?

- **1 Fighting the borrow checker:** pervasive `.clone()` to appease the borrow checker; \
`String`/`Vec<T>`/`&Vec<T>`/`&String` parameters where `&str`/`&[T]` would work; \
`Arc<Mutex<..>>` or `RefCell` reached for on first sign of sharing; `let mut` on every \
binding; explicit `'a` on every reference; occasional `unsafe` used to escape lifetime \
issues.
- **2 Cloning less, but still reflexively:** some cloning is justified but reflexive clones \
remain; some `&Vec<T>`/`&String` in signatures; interior-mutability primitives introduced \
without a stated invariant; unnecessary `mut` scattered but not universal.
- **3 Borrows preferred, lifetimes parsimonious:** borrows preferred over clones; parameter \
types use `&str` and `&[T]` idiomatically; interior mutability appears only where \
justified with a comment; lifetime annotations parsimonious; occasional lapse (e.g. one \
`.clone()` where a borrow would have worked).
- **4 Ownership expresses intent:** move-by-value builders, `Cow<'_, str>` where the data flow \
is mixed borrowed/owned; no defensive cloning; interior mutability is a documented \
deliberate choice; lifetime bounds precise and elided where the compiler allows.
- **5 Ownership restructured to fit the problem:** ownership restructured to fit the problem \
rather than reaching for `Arc<Mutex<..>>` first; shared state minimised architecturally.

**Anti-idioms:** defensive `.clone()` to appease borrowck (the anti-idiom is pervasive or \
reflexive cloning, not any clone); `let mut` on values never mutated; `Arc<Mutex<..>>`/\
`RefCell` as first reach for shared state; `&Vec<T>` or `&String` in parameter position; \
explicit lifetimes where elision applies.

**Idiomatic signals:** signatures use `&str`, `&[T]`, and `impl AsRef<...>` where \
appropriate; move-by-value builders (`self` not `&mut self`); `Cow<'_, str>` at API \
boundaries with mixed ownership; interior mutability restricted to modules that document \
the invariant.""",
    "d3_error_handling_and_unsafe": """\
## D3 Error Handling and Unsafe

Does the code fail well and treat `unsafe` with the respect it deserves?

- **1 Panics and undocumented unsafe:** `.unwrap()`/`.expect()`/`panic!` in normal execution \
paths; magic sentinels (`-1`, `usize::MAX`) instead of `Option`/`Result`; errors \
swallowed via `.ok()` or `let _ = ...`; `unsafe` present without `// SAFETY:` comments or \
beyond clear FFI need; raw pointers used where references would work.
- **2 Stringly-typed errors, unexplained unsafe:** `Result<_, String>` in signatures; some `?` \
propagation but mixed with `.unwrap()` in the same function; unsafe blocks have brief \
comments but do not state the invariant being upheld; interior-unsafe (unsafe hidden in a \
safe function) without a documented boundary.
- **3 Propagation with typed errors, unsafe justified:** `?` used throughout for propagation; \
typed error enum or `thiserror` in library code; `.expect("...")` only where an \
invariant holds and the message states it; no `.unwrap()` outside `#[cfg(test)]`; \
`unsafe` blocks scoped narrowly and preceded by a `// SAFETY:` comment.
- **4 Errors compose, unsafe abstracted safely:** errors compose (`From` impls for \
conversion); diagnostic messages carry enough context to debug from a log line; \
`unreachable!()` reserved for truly-unreachable branches with prose justification; \
`unsafe` minimised and abstracted behind a safe API with invariants documented in the \
style of `std`; `# Errors`/`# Panics` doc sections cover every fallible or panicking \
public function.
- **5 Failure modes self-evident, unsafe audit-grade:** `unsafe` either absent or forensically \
documented, with preconditions, postconditions, and the safe-API boundary stated at the \
level of an external audit; error types educate the reader; the taxonomy of failure \
modes is self-evident from the public API.

**Anti-idioms:** `.unwrap()`/`.expect()` in production paths; `Result<_, String>` in library \
signatures; magic sentinels instead of `Option`/`Result`; `unsafe` without \
`// SAFETY:`; interior-unsafe with no marked boundary; raw pointers where references work.

**Idiomatic signals:** `?` chains with a project-wide error enum; `// SAFETY:` blocks that \
reference specific documented invariants; `# Errors` and `# Panics` sections on every \
public fallible or panicking fn; custom error types implementing `std::error::Error` \
(`thiserror` is fine).

**Gate rule:** any `clippy::correctness` or `clippy::suspicious` finding, or any rapx \
UB-adjacent finding, gates D3 to at most 2 (or 1 for UB) until resolved.""",
    "d4_types_and_abstractions": """\
## D4 Types and Abstractions

Does the type system carry the invariants, and are the abstractions earned?

- **1 The type system is invisible:** `bool` and `Option<bool>` parameters where a \
purpose-built enum would read better; magic numbers; missing `Debug`/`Clone` on public \
types; ad-hoc `new_from_x`/`to_y` methods duplicating what `From`/`Into` would give; \
stringly-typed APIs (`&str` where an enum or newtype belongs); C-style typedefs or \
integer flags where an enum belongs.
- **2 Types attempted but not enforced:** some enums replace booleans; some `From` impls \
present but manual conversion methods still dominate; newtypes attempted but their \
invariants are not enforced by construction; generics used defensively ("in case I need \
it later").
- **3 Types carry meaning:** enums replace flag booleans; common derives present where they \
make sense; `From` and `Into` for cheap conversions; newtypes wrap values that carry \
useful invariants; generics used only where actually generic.
- **4 Misuse tends toward a compile error:** types encode invariants such that misuse is a \
compile error (typestate or phantom types where warranted); `TryFrom` for fallible \
conversions; abstractions justified by at least 2 uses, with no premature generics, no \
`dyn Trait` for a single-implementor trait, and no builder pattern on a two-field \
struct; `bitflags` for flag sets.
- **5 Type-driven design is the point:** the API is essentially incapable of being misused; \
generics are elegant and their bounds are minimal; a reader learns the domain by reading \
the types.

**Anti-idioms:** over-parameterised builders (`FileBuilder<T: AsRef<str>, 'a>`); \
`Box<dyn Iterator>` where `impl Iterator` fits; single-implementor traits introduced \
"just in case"; stringly-typed APIs; magic numbers and `bool` parameters; unsound generic \
bounds on unsafe wrappers.

**Idiomatic signals:** newtypes enforce at construction time the invariant the type name \
claims; `From`/`Into` (or `TryFrom` for fallible) rather than named conversion methods; \
trait bounds are tight and associated types are purposeful; no `dyn Trait` unless \
dispatch actually varies.""",
    "d5_expression_and_control_flow": """\
## D5 Expression and Control Flow

Does the code read the way experienced Rust reads?

- **1 C control flow in Rust:** `for i in 0..v.len()` indexed loops with `v[i]` inside; \
imperative accumulation where `.map().sum()` or `.collect()` would fit; nested \
`match`/`if let` pyramids; `if x { true } else { false }`; `.unwrap()` inside `.map()`/\
`.and_then()` closures; wildcard `_ =>` arms in matches on the crate's own enums; `while` \
used where `for` or an iterator adapter would express intent.
- **2 Idioms appear but don't dominate:** some iterator adapters used but for-index loops \
persist; matches mostly exhaustive but wildcards silence variants the code should \
handle; occasional needless `.collect()` between adapter stages; functions large but \
decomposable.
- **3 Idiomatic expression is the default:** iterator adapters used where they simplify; `?` \
chains where they express intent; pattern matching leveraged well (`if let`, \
`let ... else`, exhaustive `match`); wildcards used only where the enum is open; \
cognitive complexity around 0.7 per method.
- **4 Construct choice is deliberate:** plain `for` loops where they read better than a \
functional chain; no needless `.collect()` between iterator stages; destructuring in \
bindings and function arguments; `matches!`/`let ... else` used exactly where they \
improve clarity, not because they exist; macros used only for meta uses.
- **5 Every construct legible as a choice:** reading the code is faster than reading its \
comments; complex operations pipeline cleanly; the choice between imperative and \
functional shape is deliberate every time.

**Anti-idioms:** indexed loops; `needless_bool`, `needless_collect`, \
`redundant_pattern_matching` clippy warnings; `.unwrap()` in closures; catch-all `_ =>` \
arms in matches on the crate's own enums; functions over 30 statements without \
decomposition.

**Idiomatic signals:** `.iter().map(...).filter(...).collect()` chains where they compose; \
`for x in ...` when a chain would obscure the shape; `let Some(x) = ... else { return \
... };` in place of a `match` that only unwraps; cognitive complexity at most 1.0 per \
method.""",
    "d6_api_naming_and_documentation": """\
## D6 API, Naming, and Documentation

Would someone else be able to use this crate without reading its source?

- **1 Opaque without the source:** `get_foo()`-style getters; `///` docs absent on `pub` \
items; examples that use `.unwrap()` or that don't compile; missing `# Errors`/\
`# Panics` sections; every item is `pub`; `Debug` missing on public types.
- **2 Documented in places:** some public items documented but examples don't run; naming \
mostly follows RFC 430 but some abbreviations; `# Errors` sections missing on half of \
fallible functions; ad-hoc conversion methods with unclear semantics.
- **3 Conventional and navigable:** collections expose `iter`/`iter_mut`/`into_iter`; \
`as_`/`to_`/`into_` used per their cost conventions; `///` docs on public items; \
`# Errors`/`# Panics` where applicable (`# Panics` when a panic is reachable under some \
input, not whenever a panic-macro token appears in the body; a `panic!()` or \
`unreachable!()` in a branch the surrounding control flow proves unreachable needs none); \
examples use `?`; `pub(crate)` used to narrow scope.
- **4 Guided use, runnable docs:** constructors are `new`; `From`/`AsRef` over inherent \
conversion methods; `#[must_use]` where a return is easy to drop by accident; rustdoc \
examples runnable and present on the fallible or panicking public surface (`cargo test \
--doc` passes and covers more than a single trivial function); `Debug` on public types; \
struct fields private with accessors where invariants exist.
- **5 The API teaches its own use:** the docs are the mental model; every export is \
intentional and its cost is stated; the crate could be released to crates.io without \
significant editing.

**Anti-idioms:** `///` docs absent on `pub` items; `# Errors`/`# Panics` missing on fallible \
or panicking public functions; `get_foo()` getters (Rust uses `foo()` and `foo_mut()`); \
ad-hoc `new_from_x`/`to_y` methods where `From`/`Into`/`AsRef` fit; everything `pub`; \
public types without `Debug`.

**Idiomatic signals:** doctest examples that use `?` and are runnable; `#[must_use]` on \
returns that carry meaning (e.g. `Result`, builders); public structs with private fields \
and constructor invariants; module-level `//!` doc that states the invariant the module \
protects.""",
}

LEVELS = """\
- **1 Anti-idiomatic:** multiple documented Rust anti-idioms in this dimension. Would be \
out-right rejected in code review.
- **2 Novice:** common Rust idioms attempted but misapplied at a structural level. A reviewer \
would ask for substantial rework rather than touch-ups.
- **3 Developing:** correct application of the common Rust idioms. Occasional lapses that a \
code review would catch.
- **4 Proficient:** fluent, consistent Rust with small, but not meaningful, lapses. Any \
deviations are justified in code or docs.
- **5 Idiomatic:** could serve as a teaching exemplar. A reader learns the Rust idioms by \
reading the code.

Levels 1, 3, and 5 are anchors. When a crate partially satisfies the descriptors of two \
adjacent anchors, assign the level between them rather than forcing the nearer anchor."""

INSTRUCTIONS = f"""\
You are reviewing a Rust crate for idiomaticity: the extent to which it follows \
established Rust conventions and uses the language's intended abstractions, such as \
ownership and borrowing, pattern matching, and standard library constructs.

# Procedure

1. Read the crate as a reviewer would: start at `Cargo.toml` and `lib.rs`/`main.rs`, then \
follow the module tree.
2. Score each of the six dimensions below on the scale that follows.

# Scale

{LEVELS}"""

RULES = """\
# Rules

- Score the crate as it stands. Read whatever you need, but do not modify, create, \
delete, or format any of its files; a repair you make yourself is not a repair the crate \
earned. Beyond reading files, the only commands to run are `cargo fmt --check` and \
`cargo clippy -- -D warnings` in the crate root, which only report; cite their results \
for D1.
- Cite evidence. Every anti-idiom and every idiomatic signal you report must name a real \
file and line you actually read. Do not invent findings, and do not report a finding you \
cannot point at.
- Score only what is in front of you. When the crate gives a dimension little to exercise \
(no fallible operation for D3, no shared data for D2, no domain types for D4), a crate \
that introduces none of that dimension's anti-idioms is idiomatic there: score it as such \
rather than as missing evidence, and say in the rationale how little the dimension was \
exercised.
- Nothing requires this crate to expose a C ABI. `extern "C"`, `#[no_mangle]`, raw \
pointers, C type aliases such as `size_t`, and C naming are choices the crate made, so \
score them as the anti-idioms they are rather than excusing them as dictated by an FFI \
boundary. Code that is mostly such a boundary is not code that leaves a dimension \
unexercised. Data passed or held through raw pointers where a reference, slice, or owned \
value would do is ownership worked around rather than with, and scores low on D2 as well \
as D3.
- Count each anti-idiom a dimension above lists only against the dimension(s) that list \
it. This holds for the D1 checks too: when `cargo clippy` or `cargo fmt` fails only on \
a lint for an anti-idiom another dimension lists, count it there and not against D1.
- Judge idiomaticity, not correctness. A wrong program written idiomatically still scores \
well here.
- A `#[allow(...)]` with a comment justifying it is an accepted override. A crate- or \
file-wide `#![allow(...)]` that silences lints wholesale is not, and hides what it \
silences rather than excusing it.
- Ignore vendored third-party code, `target/`, dot-directories, and generated bindings. \
Other members of the crate's Cargo workspace are out of scope even when you can read them. \
When the crate is a workspace member, Cargo keeps its `Cargo.lock` at the workspace root; \
judge the lock file there, not by its absence from the crate.
- Do not read any `idiomaticity.*` file. They are other judgements, and this score must be \
your own.
- Write the `verdict` last, after every dimension is scored."""


def _dimension_schema() -> dict[str, Any]:
    finding = {
        "type": "object",
        "properties": {
            "file": {"type": "string"},
            "line": {"type": "integer"},
            "finding": {"type": "string"},
        },
        "required": ["file", "line", "finding"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        # Evidence comes first so the score is reached through it rather than justified
        # after the fact
        "properties": {
            "anti_idioms": {"type": "array", "items": finding},
            "idiomatic_signals": {"type": "array", "items": finding},
            "rationale": {"type": "string"},
            "level": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        },
        "required": ["anti_idioms", "idiomatic_signals", "rationale", "level"],
        "additionalProperties": False,
    }


# `verdict` is last so `run_codex_turn` can tell a finished judgement from a mid-turn
# status update, which the schema forces into the same shape but leaves empty
JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dimensions": {
            "type": "object",
            "properties": {name: _dimension_schema() for name in DIMENSIONS},
            "required": list(DIMENSIONS),
            "additionalProperties": False,
        },
        "verdict": {"type": "string"},
    },
    "required": ["dimensions", "verdict"],
    "additionalProperties": False,
}


@dataclass
class IdiomaticityConfig:
    # _self_ first so the selected provider preset wins over this schema's own defaults
    defaults: list[Any] = field(default_factory=lambda: ["_self_", {"model": "openrouter"}])

    model: ModelConfig = MISSING
    codex: CodexConfig = field(
        # The judge must not edit what it scores. Codex's sandbox cannot nest in the container,
        # so there `IDEAS_CODEX_SANDBOX` overrides it and the `RUN_ARGS` mounts contain it.
        default_factory=lambda: CodexConfig(
            sandbox=os.environ.get("IDEAS_CODEX_SANDBOX", "read-only")
        )
    )

    cargo_toml: Path = MISSING
    output: Path = MISSING

    gate: bool = True
    ub: bool = True
    ub_toolchain: str = MISSING
    ub_timeout: int | None = 600
    vcs: str = "none"


cs = ConfigStore.instance()
cs.store(name="idiomaticity", node=IdiomaticityConfig)


def gate_findings(diagnostics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: dict[tuple[str, str, int], dict[str, Any]] = {}
    for diagnostic in diagnostics:
        code = ((diagnostic.get("code") or {}).get("code") or "") if diagnostic else ""
        # Every other clippy group was allowed, so a `clippy::` code here is a gate lint.
        # rustc's own lints still warn and are not ours to gate on.
        if not code.startswith("clippy::"):
            continue
        spans = diagnostic.get("spans") or []
        primary = next((s for s in spans if s.get("is_primary")), spans[0] if spans else {})
        file_name = primary.get("file_name", "")
        line = primary.get("line_start", 0)
        # `--all-targets` compiles the same source once per target, repeating its lints
        findings.setdefault(
            (code, file_name, line),
            {
                "tool": "clippy",
                "lint": code,
                "file": file_name,
                "line": line,
                "message": diagnostic.get("message", ""),
            },
        )
    return list(findings.values())


def ub_findings(bugs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # rapx reports each bug once per analysed function body, so the same location can
    # surface several times across a crate
    findings = {
        (bug["bug"], bug["file"], bug["line"]): {
            "tool": "rapx",
            "lint": bug["bug"],
            "file": bug["file"],
            "line": bug["line"],
            "message": f"{bug['bug']} detected",
        }
        for bug in bugs
    }
    return list(findings.values())


def log_findings(findings: list[dict[str, Any]]) -> None:
    for finding in findings:
        logger.info(f"  {finding['file']}:{finding['line']}: {finding['lint']}")


def forbids_unsafe(crate: Crate) -> bool:
    roots = [crate.lib_src_path, *(Path(t["src_path"]) for t in crate.bin_targets)]
    roots = [root for root in roots if root is not None]
    return bool(roots) and all(FORBID_UNSAFE.search(root.read_text()) for root in roots)


def ub_scan(
    crate: Crate, toolchain: str, timeout: int | None
) -> tuple[str, list[dict[str, Any]]]:
    # Safe code cannot cause UB, so any rapx finding here is a false positive
    if forbids_unsafe(crate):
        logger.info("UB scan skipped: every crate root is `#![forbid(unsafe_code)]`")
        return "skipped", []

    try:
        findings = ub_findings(crate.cargo_rapx(toolchain, timeout=timeout))
    except RuntimeError as e:
        # rapx can ICE on real translations; an unknown UB result is not clean, but must not
        # cost the whole score
        logger.warning(f"UB scan failed; D3 is gated on clippy alone: {e}")
        return "failed", []

    logger.info(f"rapx reported {len(findings)} UB-adjacent finding(s)")
    log_findings(findings)
    return "clean" if not findings else "findings", findings


def build_failure(crate: Crate, clippy: ClippyResult) -> str:
    # `cargo build` links but skips test targets; clippy compiles every target but never links
    builds, output = crate.cargo_build()
    if not builds:
        return output.strip() or "`cargo build` failed"
    return "" if clippy.success else clippy.render(("error",))


def build_prompt(workdir: Path, findings: list[dict[str, Any]], failure: str = "") -> str:
    parts = [
        INSTRUCTIONS,
        "\n\n".join(["# Dimensions", *DIMENSIONS.values()]),
        RULES,
        f"# Crate under review\n\nThe Rust crate under review is rooted at `{workdir}`.",
    ]

    if failure:
        if len(failure) > BUILD_FAILURE_CHARS:
            failure = failure[:BUILD_FAILURE_CHARS] + "\n[... truncated]"
        parts.append(
            "# Build\n\n"
            "The crate does not build: `cargo build` or `cargo clippy --all-targets` fails. "
            "This is already counted against D1 and you cannot run cargo yourself, but read "
            f"the errors for what they say about the rest of the code:\n\n```\n{failure}\n```"
        )

    if findings:
        cited = "\n".join(
            f"- `{f['lint']}` at {f['file']}:{f['line']} - {f['message']}" for f in findings
        )
        parts.append(
            "# Static analysis\n\n"
            f"Static analysis reports the following {', '.join(GATE_GROUPS)} and "
            f"UB-adjacent findings. They are already counted against D3 and you do not "
            f"need to rediscover them, but read them for what they say about the rest of "
            f"the code:\n{cited}"
        )

    parts.append(
        "# Output\n\n"
        "Return one JSON object and no other text. It must match this JSON schema:\n\n"
        f"```json\n{json.dumps(JUDGE_SCHEMA, indent=2)}\n```"
    )
    return "\n\n".join(parts)


def apply_gate(
    scores: dict[str, Any], findings: list[dict[str, Any]], builds: bool = True
) -> dict[str, Any]:
    # The rubric's caps are enforced here since the model applies them inconsistently
    if not builds:
        _cap(
            scores,
            "d1_project_structure_and_cargo_hygiene",
            BUILD_CAP,
            "the crate does not build",
        )
    if findings:
        ub = [f for f in findings if f["tool"] == "rapx"]
        cause = f"{len(ub)} UB" if ub else f"{len(findings)} {'/'.join(GATE_GROUPS)}"
        cap = UB_CAP if ub else LINT_CAP
        _cap(scores, "d3_error_handling_and_unsafe", cap, f"{cause} finding(s)")
    return scores


def _cap(scores: dict[str, Any], name: str, cap: int, cause: str) -> None:
    dimension = scores["dimensions"][name]
    capped = min(int(dimension["level"]), cap)
    if capped != dimension["level"]:
        label = name.upper().split("_")[0]
        logger.warning(f"Gating {label} from {dimension['level']} to {capped}: {cause}")
    dimension["ungated_level"] = dimension["level"]
    dimension["level"] = capped


def judge(cfg: IdiomaticityConfig) -> dict[str, Any]:
    crate = Crate(cfg.cargo_toml, vcs=cfg.vcs)  # type: ignore[reportArgumentType]
    workdir = crate.cargo_toml.parent

    # Dropped before the turn, not overwritten after it: until this run replaces them the
    # previous score and transcript are just more files the model can read
    transcript = cfg.output.parent / f".{cfg.output.stem}"
    shutil.rmtree(transcript, ignore_errors=True)
    cfg.output.unlink(missing_ok=True)

    findings: list[dict[str, Any]] = []
    failure = ""
    build = lint_scan = scan = "skipped"
    if cfg.gate:
        clippy = crate.cargo_clippy(*GATE_LINTS, targets="--all-targets")
        findings = gate_findings(clippy.diagnostics)
        # Lints are capped at warnings, so a failure is a target that does not compile and
        # whose lints clippy may not have reached
        lint_scan = "failed" if not clippy.success else "findings" if findings else "clean"
        logger.info(f"clippy reported {len(findings)} gating finding(s)")
        log_findings(findings)
        failure = build_failure(crate, clippy)
        build = "failed" if failure else "passed"
        if failure:
            logger.warning(f"`{crate.name}` does not build; D1 is gated:\n{failure}")
        if cfg.ub:
            scan, ub = ub_scan(crate, cfg.ub_toolchain, cfg.ub_timeout)
            findings += ub

    prompt = build_prompt(workdir, findings, failure)
    result = run_codex_turn(
        prompt, cfg.codex, cfg.model, workdir, JUDGE_SCHEMA, scratch=cfg.output.parent
    )

    # Written before the scores are validated, since a rejected judgement is exactly when
    # the transcript is worth reading
    transcript.mkdir(parents=True, exist_ok=True)
    for name, text in result.prompt_files(prompt).items():
        (transcript / name).write_text(text, encoding="utf-8")

    scores = validate(result.json())

    if cfg.gate:
        scores = apply_gate(scores, findings, builds=not failure)

    levels = {name: scores["dimensions"][name]["level"] for name in DIMENSIONS}
    scores["crate"] = crate.name
    scores["mean_level"] = sum(levels.values()) / len(levels)
    scores["gate_findings"] = findings
    scores["build"] = build
    scores["lint_scan"] = lint_scan
    scores["ub_scan"] = scan
    scores["usage"] = result.usage

    logger.info(
        f"Judged `{crate.name}`: "
        + ", ".join(f"{name.upper().split('_')[0]}={level}" for name, level in levels.items())
        + f", mean={scores['mean_level']:.2f} "
        + f"[{format_usage({cfg.model.name: result.usage})}]"
    )
    return scores


def validate(scores: dict[str, Any]) -> dict[str, Any]:
    # `--output-schema` is dropped for non-OpenAI models, so the shape is not guaranteed
    dimensions = scores.get("dimensions")
    if not isinstance(dimensions, dict):
        raise ValueError("Judge returned no `dimensions` object")

    for name in DIMENSIONS:
        dimension = dimensions.get(name)
        if not isinstance(dimension, dict):
            raise ValueError(f"Judge returned no scores for `{name}`")
        try:
            level = int(dimension["level"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"Judge returned no usable level for `{name}`") from e
        if not 1 <= level <= 5:
            raise ValueError(f"Judge scored `{name}` outside 1-5: {level}")
        dimension["level"] = level

    return scores


def _main(cfg: IdiomaticityConfig) -> None:
    if not cfg.model.enable_output_schema:
        # `_codex_cmd` drops `--output-schema` for these, leaving the rubric unenforced
        logger.warning(
            f"`{cfg.model.name}` does not support Codex structured output; the judge may "
            f"return an unusable shape"
        )

    scores = judge(cfg)
    cfg.output.parent.mkdir(parents=True, exist_ok=True)
    cfg.output.write_text(json.dumps(scores, indent=2) + "\n")
    logger.info(f"Wrote {cfg.output}")


@hydra.main(version_base=None, config_name="idiomaticity")
def main(cfg: IdiomaticityConfig) -> None:
    try:
        _main(cast(IdiomaticityConfig, OmegaConf.to_object(cfg)))
    except Exception as e:
        logger.exception(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
