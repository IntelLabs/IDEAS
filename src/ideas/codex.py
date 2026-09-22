#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import os
import json
import shutil
import logging
import tempfile
from typing import Any
from pathlib import Path
from functools import cache
from dataclasses import dataclass, field

import dspy
from litellm import get_model_info
from json_repair import loads as json_repair_loads
from dspy.utils.exceptions import AdapterParseError
from dspy.dsp.utils.settings import settings
from hydra.core.config_store import ConfigStore

from .ast import CodeC, Symbol
from .ast_rust import CodeRust, mangle
from .adapters import Code
from .model import ModelConfig
from .tools import run_subprocess
from .translate_snippet import SnippetTranslator
from .wrapper import WrapperGenerator

logger = logging.getLogger("ideas.codex")


@dataclass
class CodexConfig:
    sandbox: str = os.environ.get("IDEAS_CODEX_SANDBOX", "workspace-write")
    timeout: int = 900
    ignore_user_config: bool = True
    ignore_rules: bool = True
    extra_config: list[str] = field(default_factory=list)
    extra_env: list[str] = field(default_factory=list)


cs = ConfigStore.instance()
cs.store(name="codex", node=CodexConfig)


class _CodexOutput(dspy.Signature):
    reasoning: str = dspy.OutputField()
    output: str = dspy.OutputField()


@dataclass
class CodexResult:
    final_message: str
    usage: dict[str, Any]
    reasoning: str
    raw: str
    empty_output: str = ""

    def rust(self) -> tuple[CodeRust, str]:
        try:
            parsed = json_repair_loads(self.final_message.strip())
            if isinstance(parsed, dict) and isinstance(parsed.get("output"), str):
                summary = parsed.get("reasoning")
                code = CodeRust(parsed["output"])
                return code, summary if isinstance(summary, str) else ""
        except Exception:
            pass

        logger.error(f"Unparseable Codex message: {self.final_message.strip() or '<empty>'}")
        logger.debug(f"Codex event stream:\n{self.raw.strip() or '<empty>'}")
        raise AdapterParseError(
            adapter_name="CodexAdapter",
            signature=_CodexOutput,  # type: ignore[reportArgumentType]
            lm_response=self.final_message,
        )

    def track(self, pred: dspy.Prediction | None, model_name: str) -> None:
        if pred is not None:
            pred.set_lm_usage({model_name: self.usage})
        if (parent := settings.usage_tracker) is not None:
            parent.add_usage(model_name, dict(self.usage))

    def prompt_files(self, prompt: str) -> dict[str, str]:
        files = {"_prompt.md": prompt, "_codex.jsonl": self.raw}
        return {name: text.replace("\x00", "\\0") for name, text in files.items()}

    def as_prediction(self, name: str, prompt: str, model_name: str) -> dspy.Prediction:
        try:
            code, summary = self.rust()
        except AdapterParseError:
            # A turn that failed or returned garbage still burned tokens
            self.track(None, model_name)
            raise

        # The model's own account of the turn, falling back to the raw reasoning items
        pred = dspy.Prediction(reasoning=summary or self.reasoning, **{name: code})
        self.track(pred, model_name)
        pred.was_generated = True
        pred.prompt_files = self.prompt_files(prompt)
        return pred


SNIPPET_INSTRUCTIONS = """\
Translate exactly `snippet` into idiomatic, memory-safe Rust with the same observable
behavior as the original C. Return only this symbol group and still-missing types it
requires; do not translate unrelated functions. Treat `feedback`, `build_feedback`, and
`scope_feedback` as evidence about the prior attempt and fix their root causes.

The existing Rust file prefix (`reference_code` in the translator) is immutable and was
already accepted for earlier symbols. `prior_translation`, when nonempty, is the complete
rejected candidate for this symbol group: replace it as a whole. Do not treat it as
another immutable prefix, append a second implementation after it, or return a patch.
The new output must contain exactly one complete definition of each requested symbol.

# Establish the program contract

The snippet is not self-contained. Search the supplied C and Rust files before writing.

1. Find its declarations, callers, callees, constructors, mutators, iterators, callback
   sites, and destructors. A type definition must be designed for its entire lifecycle.
2. For each pointer and `void *`, determine its concrete meaning, nullability, owner,
   aliases, identity uses, lifetime, live extent, capacity, and allocator.
3. Reuse the exact representation already established in Rust. Earlier items are
   immutable. If this is the first relevant type, choose one representation that can
   support every related operation you found.
4. Privately map each C state location to its Rust owner. For this operation, account for
   every change to storage, links, root/head/tail, size, capacity, iterator state,
   allocation ownership, out-parameters, and rollback.

# Representation rules

- The owner of per-instance state must be reachable from that Rust instance. Do not use
  a global or thread-local arena, address registry, leaked object, or fabricated
  `'static` lifetime unless the C state itself is global.
- Use one owner plus stable handles for cyclic or aliased graphs. Keep null, sentinel,
  vacant, removed, and live states distinct. Preserve pointer identity and shallow-copy
  aliases; do not clone identity-bearing objects or renumber surviving handles.
- A handle denotes identity, not payload. Compare handles only where C compares
  pointers. A comparator, predicate, lookup, printer, or destructor that dereferences a
  C pointer must resolve the handle and inspect the same pointee fields as C.
- Give `void *` a concrete type whenever its uses establish one. Use type erasure only
  for genuinely heterogeneous data, with one agreed concrete erased representation for
  every producer and consumer.
- Keep logical storage separate from observable custom-allocation tokens. Retain each
  successful token in its owner until the C lifetime ends, then release it exactly once
  through the same allocator family. Preserve allocator call order, count, requested
  byte sizes, failure timing, and rollback. The layout of an idiomatic Rust replacement
  is not the layout of the C struct it represents.
- Callbacks are optional until invoked and may be reentrant. Do not hold a borrow or lock
  across a callback. If C rereads state afterward, observe callback mutations instead of
  continuing from a cached snapshot. A comparator may even change the root, collection,
  or global being traversed, so reacquire all affected state before the next C read.

# Translate the behavior

- Follow C control flow, sequencing, and short-circuit evaluation. Fetch conditional
  operands lazily. Check left and right children independently; one missing child must
  not suppress the other. Distinguish live length from allocated capacity and validate
  both slice bounds immediately before slicing. `Vec::with_capacity` reserves memory but
  creates no initialized, indexable elements; materialize safe storage for every slot C
  may legally read or write without claiming dead slots are live values.
- Preserve all defined return values, mutations, allocation effects, partial failures,
  output parameters, output bytes, and early returns, including strange behavior.
  Updating only a counter is not enough when C also inserts, removes, or relinks data.
- Public state may be externally modified between calls. Do not assume stronger
  invariants than C, and do not panic for an input on which C has a defined path. Avoid
  unchecked indexing, `unwrap`, `expect`, and caller-reachable assertions.
- Preserve integer width, signedness, promotions, truncation, wrapping, comparison
  direction, and operator grouping. Use checked arithmetic only where C checks it.
- Preserve C `sizeof`, alignment, and element-count results. Never apply `size_of` to an
  idiomatic Rust replacement and assume it has the C layout; derive the C quantity from
  the original fields and target primitive layouts when no C-layout type is available.
- C strings end at the first NUL; length-delimited data does not. Match parsing,
  delimiter consumption, stdout/stderr selection, newlines, and exit status exactly.
- Translate `main` as a zero-argument Rust entry point that gathers arguments and exits
  with the C status.

# Required output

- It must compile with the existing `#![forbid(unsafe_code)]`. Emit no `unsafe`, raw
  pointer representation shortcuts, `#[repr(C)]`, derives, `impl` blocks, crate
  attributes, placeholders, stubs, or knowingly partial behavior.
- Every emitted top-level item and struct field must be `pub`. Use existing names and
  types exactly and never duplicate an existing item.
- Emit the exact requested symbol name. Fix current-turn code instead of adding a
  `_translation`, `_safe`, `_impl`, or other renamed substitute.
- If an immutable earlier representation makes fidelity impossible, do not hide the
  mismatch behind shadow state or false success. Leave this translation incomplete and
  identify the incompatible representation in `reasoning`.

Before returning, compare the result statement by statement with C and repeat the
ownership/mutation audit across construction, use, failure, and destruction. Use only
the existing dependencies; `openssl`, `flate2`, and `regex` are the only additional
crates permitted when required.
"""


class CodexSnippetTranslator(SnippetTranslator):
    def __init__(
        self,
        cache: Path | None = None,
        *,
        codex: CodexConfig,
        model: ModelConfig,
        workdir: Path,
        rust_src: Path,
        c_src: Path,
        max_iters: int = 1,
    ):
        super().__init__(dspy.Predict, max_iters, cache)
        self._codex = codex
        self._model = model
        self._workdir = workdir
        self._rust_src = rust_src
        self._c_src = c_src

    def translate(
        self,
        reference_code: CodeRust,
        snippet: CodeC,
        dependent_code: CodeC,
        prior_translation: CodeRust | None,
        feedback: str,
        build_feedback: str,
        scope_feedback: str,
        translation: CodeRust | None,
    ) -> dspy.Prediction:
        if translation is not None:
            pred = dspy.Prediction(translation=translation, reasoning="")
            pred.set_lm_usage({})
            pred.was_generated = False
            return pred

        inputs = {
            "snippet": snippet,
            "prior_translation": prior_translation or CodeRust(),
            "feedback": feedback,
            "build_feedback": build_feedback,
            "scope_feedback": scope_feedback,
        }
        # Codex runs with `--cd workdir`, so the prompt names files relative to it
        rust_src = str(self._rust_src.relative_to(self._workdir))
        c_src = str(self._c_src.relative_to(self._workdir))
        files = {
            rust_src: (
                "the Rust crate translated so far. Search it for the items your "
                "translation references; your output is appended to this file and must "
                "compile against it."
            ),
            c_src: (
                "the original C. Search it for the callers of the snippet and for any "
                "definition the snippet references. A definition already translated "
                "appears here as an extern declaration; read that one from the Rust crate."
            ),
        }
        prompt = build_prompt(
            SNIPPET_INSTRUCTIONS,
            inputs,
            files,
            rust_src,
            output="an idiomatic, memory-safe Rust translation of `snippet`",
        )
        restore = [self._rust_src, self._c_src]
        result = run_codex(
            prompt, self._codex, self._model, self._workdir, restore, self._rust_src
        )
        return result.as_prediction("translation", prompt, self._model.name)


def build_prompt(
    instructions: str,
    inputs: dict[str, Code | str],
    files: dict[str, str],
    append_to: str,
    build_cmd: str = "cargo build",
    output: str = "the Rust source these instructions ask for",
) -> str:
    if "--no-run" in build_cmd:
        no_tests = (
            "Never execute the tests: no `cargo test` without `--no-run`, no "
            "`cargo nextest run`, and nothing else that runs them. "
        )
    else:
        no_tests = "Never run `cargo test`, `cargo nextest`, or any other test command. "

    parts = [
        instructions,
        "# Files\n\n"
        "Consult these rather than waiting to be given the inputs they stand in for:\n\n"
        + "\n".join(f"- `{path}` — {what}" for path, what in files.items())
        + f"\n\nBefore you answer, append your work to `{append_to}` and run `{build_cmd}`. "
        "That exact command and its final exit status are authoritative: a narrower package "
        "build does not count, and a linker or test-target compilation failure is not "
        "unrelated merely because the appended crate alone builds. After every code change, "
        "rerun the exact command. Do not return code you have not compiled with it: read the "
        "errors, fix them, and rebuild until it builds clean. If you run out of time before "
        "it does, return your latest "
        "version anyway and say in your reasoning what is still broken. Preserve every "
        "item that existed before this invocation and modify no other file except where "
        "these instructions say otherwise. You may revise or remove only text that you "
        "yourself appended during this invocation; use that freedom to fix build errors "
        "instead of adding renamed, duplicate, or shadow implementations. The final file "
        "must consist of the untouched original prefix followed by exactly the final code "
        f"returned in `output`. {no_tests}Your edits are discarded once you finish, "
        "so the value you return must stand on its own.",
    ]
    for name, value in inputs.items():
        language = getattr(value, "language", "")
        parts.append(f"# {name}\n\n```{language}\n{str(value).strip()}\n```")

    parts.append(
        '# Output\n\nReturn `{"reasoning": "<why it is right>", "output": "<rust source '
        f'code>"}}` and nothing else.\n\n- `output` is {output}: exactly the text you appended '
        f"for your last `{build_cmd}`, never a variant you did not build, and never a "
        "diff, a summary, or a path to a file.\n"
        "- `reasoning` argues that `output` is that, and not merely something that "
        "compiled: name the choices that make it so, the places where the C forced your "
        "hand, and anything still wrong or unfinished."
    )
    return "\n\n".join(parts)


def run_codex(
    prompt: str,
    cfg: CodexConfig,
    model: ModelConfig,
    workdir: Path,
    restore: list[Path],
    append_to: Path,
) -> CodexResult:
    manifests = [workdir / "Cargo.toml", workdir / "Cargo.lock"]
    manifests += [path.parent.parent / "Cargo.toml" for path in restore]
    saved = [
        (path, path.read_bytes() if path.exists() else None)
        for path in dict.fromkeys(restore + manifests)
    ]
    try:
        result = _run_codex(prompt, cfg, model, workdir)
        if not result.final_message.strip():
            # Work left on disk by a killed turn outranks a deliberate empty `output`,
            # which is the real answer only when the turn appended nothing
            salvaged = _salvage(append_to, dict(saved).get(append_to))
            result.final_message = salvaged or result.empty_output
        return result
    finally:
        for path, data in saved:
            _restore(path, data)


def _salvage(path: Path, original: bytes | None) -> str:
    # A killed or failed turn still left its work on disk; the appended tail is the answer
    # it never got to return
    try:
        current = path.read_bytes()
    except OSError:
        return ""

    prefix = original or b""
    if not current.startswith(prefix) or len(current) == len(prefix):
        return ""

    tail = current[len(prefix) :].decode("utf-8", "replace").strip()
    logger.warning(
        f"Recovered {len(tail):,} characters appended to {path} by an unfinished run"
    )
    return json.dumps(
        {
            "reasoning": "SALVAGED FROM AN UNFINISHED RUN",
            "output": tail,
        }
    )


def _restore(path: Path, data: bytes | None) -> None:
    try:
        if (path.read_bytes() if path.exists() else None) == data:
            return
        if data is None:
            path.unlink()
        else:
            path.write_bytes(data)
    except OSError as e:
        # Never mask the outcome of the run itself
        logger.error(f"Failed to restore {path}: {e}")


def _run_codex(prompt: str, cfg: CodexConfig, model: ModelConfig, workdir: Path) -> CodexResult:
    if shutil.which("codex") is None:
        raise RuntimeError(
            "`codex` was not found on PATH; install the Codex CLI or select another "
            "`translator`/`wrapper`"
        )

    with tempfile.TemporaryDirectory() as tmp:
        schema = Path(tmp) / "schema.json"
        schema.write_text(
            json.dumps(
                {
                    "type": "object",
                    # Reasoning comes first so it is generated before the code it explains
                    "properties": {
                        "reasoning": {"type": "string"},
                        "output": {"type": "string"},
                    },
                    "required": ["reasoning", "output"],
                    "additionalProperties": False,
                }
            )
        )
        codex_cmd = _codex_cmd(cfg, model, workdir, schema)
        logger.debug(f"Running {' '.join(codex_cmd)}")
        env = _env(cfg, workdir)
        _codex_login(env)
        try:
            ok, stdout, stderr, code = run_subprocess(
                codex_cmd,
                input=prompt,
                timeout=cfg.timeout,
                env=env,
            )
        except OSError as e:
            raise RuntimeError(f"Failed to run `{' '.join(codex_cmd)}`: {e}") from e

    if not ok:
        logger.error(f"`codex exec` exited with {code}: {stderr.strip()}")
    return _parse_events(stdout, model.name)


def _codex_cmd(cfg: CodexConfig, model: ModelConfig, workdir: Path, schema: Path) -> list[str]:
    model_name = model.name.split("/", 1)[-1]
    provider_config: list[str] = []
    output_schema = ("--output-schema", str(schema))
    if model.name.startswith("openrouter/"):
        model_name = model.name.removeprefix("openrouter/")
        provider_config = [
            'model_provider="openrouter"',
            'model_providers.openrouter.name="OpenRouter"',
            'model_providers.openrouter.base_url="https://openrouter.ai/api/v1"',
            'model_providers.openrouter.env_key="OPENROUTER_API_KEY"',
            'model_providers.openrouter.wire_api="responses"',
        ]
    elif model.name.startswith("hosted_vllm/"):
        model_name = model.name.removeprefix("hosted_vllm/")
        provider_config = [
            'model_provider="vllm"',
            'model_providers.vllm.name="vLLM"',
            f'model_providers.vllm.base_url="{model.base_url}"',
            'model_providers.vllm.wire_api="responses"',
        ]
        if os.environ.get("VLLM_API_KEY"):
            provider_config.append('model_providers.vllm.env_key="VLLM_API_KEY"')
        # vLLM's schema is applied to every message, not just the final one like openai
        output_schema = ()
    cmd = [
        "codex",
        "exec",
        "-",  # Read the prompt from stdin
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        *("--sandbox", cfg.sandbox),
        *output_schema,
        *("--model", model_name),
        *("-c", f'model_reasoning_effort="{model.reasoning_effort}"'),
        *("--cd", str(workdir)),
        *("-c", 'web_search="disabled"'),
        *("-c", "features.view_image=false"),
        *("-c", "agents.enabled=false"),
    ]
    if cfg.ignore_user_config:
        cmd.append("--ignore-user-config")
    if cfg.ignore_rules:
        cmd.append("--ignore-rules")
    for override in provider_config:
        cmd += ["-c", override]
    for override in cfg.extra_config:
        cmd += ["-c", override]
    return cmd


def _codex_login(env: dict[str, str]) -> None:
    key = env.get("OPENAI_API_KEY")
    if not key:
        return

    logged_in, *_ = run_subprocess(["codex", "login", "status"], timeout=60, env=env)
    if logged_in:
        return

    ok, _, stderr, _ = run_subprocess(
        ["codex", "login", "--with-api-key"], input=key, timeout=60, env=env
    )
    if not ok:
        raise RuntimeError(f"`codex login --with-api-key` failed: {stderr.strip()}")


def _env(cfg: CodexConfig, workdir: Path) -> dict[str, str]:
    keep = (
        "PATH",
        "HOME",
        "USER",
        "TERM",
        "LANG",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "RUSTUP_TOOLCHAIN",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENROUTER_API_KEY",
        "VLLM_API_KEY",
        *("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"),
        *("http_proxy", "https_proxy", "no_proxy"),
        *cfg.extra_env,
    )
    prefixes = ("LC_", "XDG_", "CODEX_")
    env = {
        name: value
        for name, value in os.environ.items()
        if name in keep or name.startswith(prefixes)
    }
    env["SHELL"] = "/bin/bash"
    tmpdir = workdir / "target" / "codex" / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(tmpdir)
    codex_home = workdir / "target" / "codex" / "home"
    codex_home.mkdir(parents=True, exist_ok=True)
    env["CODEX_HOME"] = str(codex_home)
    env.setdefault("RUSTFLAGS", "-Awarnings")
    env.setdefault("CARGO_TARGET_DIR", str(workdir / "target" / "codex"))
    return env


def _parse_events(stdout: str, model_name: str) -> CodexResult:
    messages: list[str] = []
    reasoning: list[str] = []
    tokens: dict[str, int] = dict.fromkeys(
        (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        ),
        0,
    )

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        match event.get("type"):
            case "item.completed":
                item = event.get("item") or {}
                match item.get("type"):
                    case "agent_message":
                        if text := item.get("text"):
                            messages.append(text)
                    case "reasoning":
                        if text := item.get("text"):
                            reasoning.append(text)
            case "turn.completed":
                turn_usage = event.get("usage") or {}
                for key in tokens:
                    tokens[key] += turn_usage.get(key) or 0
            case "turn.failed":
                error = (event.get("error") or {}).get("message", "")
                logger.error(f"Codex turn failed: {error}")
            case "error":
                logger.error(f"Codex error: {event.get('message', '')}")

    usage: dict[str, Any] = {
        "prompt_tokens": tokens["input_tokens"],
        "completion_tokens": tokens["output_tokens"],
        "total_tokens": tokens["input_tokens"] + tokens["output_tokens"],
        "cached_input_tokens": tokens["cached_input_tokens"],
        "cache_write_input_tokens": tokens["cache_write_input_tokens"],
        "reasoning_output_tokens": tokens["reasoning_output_tokens"],
    }
    # `codex exec` reports no per-call cost, so derive one from the token counts
    if (cost := _cost(model_name, tokens)) is None:
        usage["cost_missing"] = 1
    else:
        usage["cost"] = cost

    final, empty_output = _final_message(messages)
    return CodexResult(final, usage, "\n\n".join(reasoning), stdout, empty_output)


def _final_message(messages: list[str]) -> tuple[str, str]:
    # `--output-schema` constrains every agent message, so the status updates the agent
    # emits mid-turn parse too, carrying an empty `output`
    empty = ""
    for text in reversed(messages):
        try:
            parsed = json_repair_loads(text.strip())
        except Exception:
            continue
        if isinstance(parsed, dict) and isinstance(out := parsed.get("output"), str):
            if out.strip():
                return text, ""
            empty = empty or text

    # Nothing carried code; returning "" lets the caller salvage the work left on disk
    return "", empty


def _cost(model_name: str, tokens: dict[str, int]) -> float | None:
    if (rates := _rates(model_name)) is None:
        return None

    # Codex reports cached and cache-write tokens as subsets of `input_tokens`, and a
    # multi-turn session resends its whole context, so the cached share dominates
    cached = tokens["cached_input_tokens"]
    written = tokens["cache_write_input_tokens"]
    fresh = max(tokens["input_tokens"] - cached - written, 0)
    return (
        fresh * rates["input"]
        + cached * rates["cache_read"]
        + written * rates["cache_write"]
        + tokens["output_tokens"] * rates["output"]
    )


@cache
def _rates(model_name: str) -> dict[str, float] | None:
    try:
        info = get_model_info(model_name)
    except Exception:
        info = {}

    input_cost = info.get("input_cost_per_token") or 0.0
    output_cost = info.get("output_cost_per_token") or 0.0
    if not input_cost and not output_cost:
        logger.warning(f"litellm has no pricing for `{model_name}`; spend is unreported")
        return None

    return {
        "input": input_cost,
        "output": output_cost,
        # Providers that do not price these separately bill them as ordinary input
        "cache_read": info.get("cache_read_input_token_cost") or input_cost,
        "cache_write": info.get("cache_creation_input_token_cost") or input_cost,
    }


FUNCTION_WRAPPER_INSTRUCTIONS = """\
Implement only the C ABI adapter for `{wrapped_crate}::{symbol_name}` by completing
`example_wrapper`. Preserve its attributes, exported name, signature, types, and module
structure exactly. Use feedback about prior attempts to fix the conversion rather than
the C algorithm.

For a function template, replace only the existing `unimplemented!()` body. Emit the
same single top-level function as `example_wrapper`: add no top-level `use`, helper,
static, thread-local, module, attribute, or second export. Any helper function or scoped
callback context must be declared inside that body. `prior_wrapper` is a rejected
candidate to replace, not text to combine with the template.

# Boundary contract

Search the C definition, safe Rust function, relevant types, and existing conversions.
Privately map every C argument, reachable mutable object/global, result, and
out-parameter to its Rust counterpart and back. Record null/sentinel meaning, ownership,
aliases, pointer identity, live length versus capacity, callback ABI, allocator family,
and the point at which C reads or writes each value. Reuse a correct existing `CInterop`
policy; do not invent a competing conversion for the same value.

# Delegate, do not translate again

- The semantic operation must be one fully qualified call to
  `{wrapped_crate}::{symbol_name}`. Convert inputs, make that call, then synchronize all
  C-visible effects. An early return before it is valid only where C takes the same early
  return before the operation.
- Do not implement searching, parsing, sorting, balancing, insertion, deletion, or
  business logic in the wrapper. Traversal is allowed only to map an object graph or
  synchronize the safe result, and must not independently decide the result.
- If the exact safe function is absent, do not call a renamed helper, another hybrid
  export, or a wrapper-side reimplementation, and do not edit the safe crate. Return an
  empty `output` and identify the missing safe symbol in `reasoning`; deliberate wrapper
  rejection is required so the translation can be regenerated with the exact entry
  point.

# Convert safely and faithfully

- Respect C sequencing: check null and bounds only when C does, never after an invalid
  dereference or eager slice. Read live elements unless C actually reads initialized
  capacity storage; do not touch unrelated dead slots on write-back.
- Inbound C strings end at their first NUL. Any pointer returned to C for string use must
  refer to storage with an explicit trailing NUL and a lifetime covering every C use;
  `str::as_ptr()` and an unterminated byte slice are not C strings.
- For a graph, build one address-to-handle map and reuse it for every edge. Preserve
  cycles, aliases, null, sentinels, removed nodes, and addresses of surviving objects.
  Update existing C objects in place rather than replacing equal contents at new
  addresses.
- Never use `Box::from_raw`, `Vec::from_raw_parts`, or an owning slice merely to borrow
  C-owned memory. Transfer ownership only where C transfers it. Preserve opaque `void *`
  identity without dereferencing unless its concrete pointee is proven.
- Bridge C callbacks with an explicit scoped trampoline; never transmute ABIs. Support
  nesting and restore prior context. A callback may reenter and mutate the current graph
  or globals: expose current state before it, import its mutations afterward, hold no
  borrow or lock across it, and never overwrite it from an older snapshot.
- Use the object's configured allocator for the same calls, sizes, ordering, failures,
  and rollback as C. Carry logical data and allocation tokens separately. Do not expose
  Rust allocation storage as C-owned or substitute `libc` for a callback allocator.
- A new or resized C buffer is committed only after it is allocated, filled with every
  live translated element, and ready to publish. Then update its pointer and metadata
  together and release the old allocation only where C does. Never report success after
  discarding a Rust mutation.

# Synchronize and audit

Treat mutable pointers as in/out. Write back every changed scalar, buffer element, link,
root/head/tail, size, capacity, iterator cursor, global, and out-parameter. Preserve C
string termination and actual writable bounds. Use global synchronization helpers
without recursively reacquiring their locks. Return the safe function's exact
C-equivalent result; do not mask panics with invented statuses.

For the temporary build of a nonempty wrapper, change the definition of `{symbol_name}`
in `{c_src}` into an extern declaration with the same prototype; change no other C
definition. This edit must not appear in the returned Rust. If the exact safe symbol is
missing and `output` is empty, leave the C definition intact so the unchanged hybrid can
still be built before the wrapper is deliberately rejected.

Before returning, search the final body and verify the exact safe call is present, every
boundary-map entry is synchronized, all allocation ownership is balanced, and no branch
implements or discards part of the semantic operation.
"""


TYPE_WRAPPER_INSTRUCTIONS = """\
Implement only the `CInterop` conversion between C-layout `{type_name}` and its existing
safe representation in `{wrapped_crate}`. Complete the exact `example_wrapper`, including
its tests. Conversion maps representation; it never performs the data structure's
semantic algorithms.

Preserve every top-level item, signature, test-module name, and test-function name from
`example_wrapper`. Replace `type Rust = ()` before implementing anything; a unit type,
no-op conversion, or tests that merely prove the C object was never touched are not a
conversion. If no translated safe type can represent the valid C states, report that
representation mismatch rather than fabricating one in the wrapper.

# One conversion contract

Search all C and Rust uses of the type. For every field, identify whether it is scalar,
owner, alias, opaque identity, callback, allocator, live length, capacity, union tag,
null, sentinel, vacant slot, or stable handle. Then use one policy in both directions:

- `to_rust` reads only C state that is live and defined and produces the canonical safe
  value.
- `sync_to_c` updates the caller's existing C objects and live storage without changing
  unrelated bytes, aliases, or identity.

# Safety, ownership, and identity

- Never dereference null or form a slice before validating both bounds. Zero live length
  with a null pointer is empty/absent. Capacity, dead slots, removed nodes, and inactive
  union members are not readable unless C explicitly reads them.
- Use one graph map for cycles and aliases. Distinguish null, sentinel, vacant, removed,
  and live handles. Write surviving objects in place; do not clone, free, or recreate
  them merely to simplify conversion.
- Per-instance state must remain in or be reachable from that instance. Do not use
  global/thread-local arenas, permanent address registries, leaks, or fabricated
  `'static` references.
- Borrowed C storage remains borrowed. Do not use `Box::from_raw`, owning slices, or
  `Vec::from_raw_parts` unless ownership really transfers. Preserve an opaque pointer as
  identity rather than casting it to a dummy pointee type.
- A Rust string written back for C string use needs explicit NUL-terminated storage. A
  Rust vector's capacity is not initialized C storage, and publishing a new or resized C
  buffer requires copying every live Rust element before pointer and capacity metadata
  are committed.
- Keep logical contents separate from custom-allocation tokens. Preserve allocator and
  callback nullability and identity; never transmute callback ABIs or treat C storage as
  Rust-owned. Converting a borrow must not invent an owning token, and synchronizing must
  copy live contents rather than drop or forget a token.

# Non-vacuous tests

For any valid C value, `to_rust` followed immediately by `sync_to_c` must preserve every
C-visible field, live pointee, and retained address unless C semantics require
reallocation.

- `round_trip_zeroed` must use the simplest valid empty value, not zero bytes if those
  are invalid.
- `round_trip_nontrivial` must include relevant nonzero lengths, pointers, links,
  aliases, callbacks, and sentinel state. Include poisoned dead storage when a field
  controls liveness and prove it was neither read nor changed.
- Compare fields, pointees, and addresses directly; do not normalize expected and actual
  through the same conversion. Exercise two independent owning instances to detect
  accidental shared global storage.

Use no placeholders, `catch_unwind`, or vacuous self-round-trip assertions. Conversion
code must not use `unwrap`, `expect`, or assertions for states accepted by C.
"""


VARIABLE_WRAPPER_INSTRUCTIONS = """\
Implement the exact `example_wrapper` template that synchronizes C global
`{variable_name}` with translated global `{wrapped_crate}::{rust_variable_name}`. They
are two representations of one logical object.

Preserve the template's complete top-level shape and names. `prior_wrapper` is a rejected
candidate to replace, not another definition to retain or extend.

# Establish the contract

Search every C and Rust initializer, reader, writer, and wrapper for this global. Record
its initial value and mutability, which direction must be synchronized around each call,
whether pointers refer to other globals by identity, and which Rust lock or
interior-mutability mechanism is authoritative.

# Synchronize the real globals

- Preserve the template's names and module structure and implement only its requested
  synchronization functions.
- C-to-Rust synchronization must update the existing Rust global in place. Do not
  replace an identity-bearing object with a clone, initializer call, leaked value,
  shadow global, or private registry entry.
- Rust-to-C synchronization must update the actual C symbol and every live pointee or
  buffer C can observe.
- Reuse the type's `CInterop` conversion. Preserve null and sentinel states, pointer
  identity and aliases, logical length versus capacity, callbacks, and allocator
  ownership; do not invent a second representation policy here.
- Hold each Rust lock once for the complete local operation, then release it before any
  helper or callback that could reacquire it. A callback mutation must remain
  authoritative rather than being overwritten by a stale pre-call snapshot.
- Never transmute callbacks, fabricate ownership of C memory, or reinterpret C storage
  as a Rust-owned container.

# Prove both directions

`initial_value_matches` must independently compare the C compiler's initializer with
the translated initializer before synchronization runs; read `{wrapped_crate}` directly
and do not compare the C value with itself.

For round trips, write a nontrivial value on one side, synchronize once, and inspect the
other side before reversing direction. Check live pointees and observable addresses, not
just scalar metadata. If callbacks can mutate the global, test that a later Rust read
sees the callback's change.

Remove every placeholder. Do not use `catch_unwind`, self-comparisons, or expected and
actual values produced by the same conversion path.
"""


class CodexWrapperGenerator(WrapperGenerator):
    def __init__(
        self,
        cache: Path | None = None,
        *,
        codex: CodexConfig,
        model: ModelConfig,
        workdir: Path,
        rust_src: Path,
        c_src: Path,
        hybrid_src: Path,
        max_iters: int = 1,
    ) -> None:
        super().__init__(dspy.Predict, max_iters, cache)
        self._codex = codex
        self._model = model
        self._workdir = workdir
        self._rust_src = rust_src
        self._c_src = c_src
        self._hybrid_src = hybrid_src

    def generate(
        self,
        generate_wrapper: dspy.Module,
        crate: CodeRust,
        wrapped_crate_code: CodeRust,
        support_code: CodeC | None,
        example_wrapper: CodeRust,
        prior_wrapper: CodeRust | None,
        feedback: str,
        build_feedback: str,
        scope_feedback: str,
        wrapper: CodeRust | None,
        symbol: Symbol,
        wrapped_crate: str,
    ) -> dspy.Prediction:
        if wrapper is not None:
            pred = dspy.Prediction(wrapper=wrapper, reasoning="")
            pred.set_lm_usage({})
            pred.was_generated = False
            return pred

        inputs = {
            "example_wrapper": example_wrapper,
            "prior_wrapper": prior_wrapper or CodeRust(),
            "feedback": feedback,
            "build_feedback": build_feedback,
            "scope_feedback": scope_feedback,
        }
        if symbol.is_type:
            template = TYPE_WRAPPER_INSTRUCTIONS
        elif symbol.is_function:
            template = FUNCTION_WRAPPER_INSTRUCTIONS
        elif symbol.is_variable:
            template = VARIABLE_WRAPPER_INSTRUCTIONS
        else:
            raise ValueError(f"No Codex wrapper prompt for `{symbol.kind}` `{symbol.name}`")

        # Codex runs with `--cd workdir`, so the prompt names files relative to it
        rust_src = str(self._rust_src.relative_to(self._workdir))
        c_src = str(self._c_src.relative_to(self._workdir))
        hybrid_src = str(self._hybrid_src.relative_to(self._workdir))
        instructions = template.format(
            symbol_name=symbol.spelling,
            type_name=symbol.bindgen_name,
            variable_name=symbol.spelling,
            rust_variable_name=mangle(symbol.spelling),
            wrapped_crate=wrapped_crate,
            c_src=c_src,
        )
        files = {
            hybrid_src: (
                "the hybrid crate: the bindgen C-layout types, the `CInterop` trait, the "
                "`__c_globals` externs, and the wrappers written so far. It can be very "
                "large, so search it for the specific declarations you need rather than "
                "reading it end to end; your output is appended to this file and must "
                "compile against it."
            ),
            rust_src: "the safe Rust crate being wrapped.",
            c_src: (
                "the C that goes into the build. It still defines the symbol you are "
                "wrapping; a symbol wrapped earlier appears as an extern declaration, and "
                "its translation is in the Rust crate above."
            ),
        }
        prompt = build_prompt(
            instructions,
            inputs,
            files,
            hybrid_src,
            "cargo test --no-run --lib",
            output="the wrapper these instructions describe",
        )
        restore = [self._hybrid_src, self._rust_src, self._c_src]
        result = run_codex(
            prompt, self._codex, self._model, self._workdir, restore, self._hybrid_src
        )
        return result.as_prediction("wrapper", prompt, self._model.name)
