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
from typing import Any, Literal
from pathlib import Path
from functools import cache
from dataclasses import dataclass, field

from json_repair import loads as json_repair_loads
from hydra.core.config_store import ConfigStore

from .ast_rust import CodeRust
from .adapters import Code
from .model import Generation, GenerationParseError, LMUsage, ModelConfig, current_tracker
from .model import model_info
from .tools import run_subprocess

logger = logging.getLogger("ideas.codex")


@dataclass
class CodexConfig:
    sandbox: str = os.environ.get("IDEAS_CODEX_SANDBOX", "workspace-write")
    timeout: int = 900
    subagents: bool = False
    extra_config: list[str] = field(default_factory=list)
    extra_env: list[str] = field(default_factory=list)


cs = ConfigStore.instance()
cs.store(name="codex", node=CodexConfig)


# Reasoning comes first so it is generated before the code it explains
CODE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "output": {"type": "string"},
    },
    "required": ["reasoning", "output"],
    "additionalProperties": False,
}


@dataclass
class CodexResult:
    final_message: str
    usage: dict[str, Any]
    reasoning: str
    raw: str
    empty_output: str = ""

    def json(self) -> dict[str, Any]:
        try:
            parsed = json_repair_loads(self.final_message.strip())
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        logger.error(f"Unparseable Codex message: {self.final_message.strip() or '<empty>'}")
        logger.debug(f"Codex event stream:\n{self.raw.strip() or '<empty>'}")
        raise GenerationParseError(
            f"Codex returned no JSON object: {self.final_message.strip() or '<empty>'}"
        )

    def rust(self) -> tuple[CodeRust | None, str]:
        try:
            parsed = json_repair_loads(self.final_message.strip())
            if isinstance(parsed, dict) and isinstance(out := parsed.get("output"), str):
                summary = parsed.get("reasoning")
                # An empty `output` is a turn that returned no code, not code that is empty
                code = CodeRust(out) if out.strip() else None
                return code, summary if isinstance(summary, str) else ""
        except Exception:
            pass

        logger.error(f"Unparseable Codex message: {self.final_message.strip() or '<empty>'}")
        logger.debug(f"Codex event stream:\n{self.raw.strip() or '<empty>'}")
        raise GenerationParseError(
            "Codex returned no `reasoning`/`output` JSON object: "
            f"{self.final_message.strip() or '<empty>'}"
        )

    def track(self, model_name: str) -> LMUsage:
        if (tracker := current_tracker()) is not None:
            tracker.add(model_name, self.usage)
        return {model_name: dict(self.usage)}

    def prompt_files(self, prompt: str) -> dict[str, str]:
        files = {"_prompt.md": prompt, "_codex.jsonl": self.raw}
        return {name: text.replace("\x00", "\\0") for name, text in files.items()}

    def as_generation(self, prompt: str, model_name: str) -> Generation:
        try:
            code, summary = self.rust()
        except GenerationParseError:
            # A turn that failed or returned garbage still burned tokens
            self.track(model_name)
            raise

        return Generation(
            code=code,
            # The model's own account of the turn, falling back to the raw reasoning items
            reasoning=summary or self.reasoning,
            usage=self.track(model_name),
            was_generated=True,
            prompt_files=self.prompt_files(prompt),
        )


def build_prompt(
    instructions: str,
    inputs: dict[str, Code | str],
    files: dict[str, str],
    append_to: str,
    *,
    build_cmd: str = "cargo build",
    lint_cmd: str = "cargo clippy",
    task: Literal["translate", "wrap"] | None = None,
) -> str:
    if "--no-run" in build_cmd:
        no_tests = (
            "Never execute the tests: no `cargo test` without `--no-run`, no "
            "`cargo nextest run`, and nothing else that runs them."
        )
    else:
        no_tests = "Never run `cargo test`, `cargo nextest`, or any other test command."

    allow_guidance = {
        None: "Use an allowance only when the warning is genuinely inapplicable or unavoidable.",
        "translate": (
            "Use an allowance only when the warning is genuinely inapplicable or a sound, "
            "idiomatic implementation cannot avoid it."
        ),
        "wrap": (
            "An allowance may be appropriate when a warning follows from intentionally "
            "preserving an external interface."
        ),
    }
    build = (
        f"Before you answer, append your work to `{append_to}` and run `{build_cmd}`. "
        "That exact command and its final exit status are authoritative: a narrower build "
        "than it does not count, and a linker or test-target compilation failure is not "
        "unrelated merely because the appended crate alone builds. After every code change, "
        "rerun the exact command. Do not return code you have not compiled with it: read the "
        "errors, fix them, and rebuild until it builds clean. If you run out of time before "
        "it does, return your latest version anyway and say in your reasoning what is still "
        "broken. "
        f"{no_tests}"
        "\n\n"
        f"Once the code builds clean, run `{lint_cmd}` and act only on its "
        "warnings and errors whose primary source span is in the code you appended "
        "this turn. Ignore diagnostics in untouched code, dependencies, or other "
        "targets. Fix each in-scope warning or error when practical. "
        f"{allow_guidance[task]} "
        "If you suppress a warning, use only its specific lint in a "
        "local `#[allow(lint_name)]` on the smallest applicable item, and explain why "
        "the allowance is warranted in your reasoning. Never use broad suppressions such "
        "as `#[allow(warnings)]` or crate/module-wide allowances, and never suppress "
        "errors. Crate-level `forbid` lints remain fatal and cannot be overridden. After "
        "every change rerun both commands; the build must be clean and every remaining "
        "in-scope warning must either be fixed or covered by a justified local allowance."
        "\n\n"
        "Preserve every item that existed before this invocation and modify no other file "
        "except where these instructions say otherwise. You may revise or remove only text "
        "that you yourself appended during this invocation; use that freedom to fix build "
        "errors instead of adding renamed, duplicate, or shadow implementations. The final "
        "file must consist of the untouched original prefix followed by exactly the final "
        "code returned in `output`. Your edits are discarded once you finish, so the value "
        "you return must stand on its own."
    )

    parts = [
        instructions,
        "# Files\n\n"
        "Consult these rather than waiting to be given the inputs they stand in for:\n\n"
        + "\n".join(f"- `{path}` — {what}" for path, what in files.items()),
        "# Build\n\n" + build,
    ]
    for name, value in inputs.items():
        language = getattr(value, "language", "")
        parts.append(f"# {name}\n\n```{language}\n{str(value).strip()}\n```")

    task_outputs = {
        None: "the Rust source these instructions ask for",
        "translate": "an idiomatic, memory-safe Rust translation of `snippet`",
        "wrap": "the wrapper these instructions describe",
    }
    parts.append(
        '# Output\n\nReturn `{"reasoning": "<why it is right>", "output": "<rust source '
        f'code>"}}` and nothing else.\n\n- `output` is {task_outputs[task]}: exactly the text you appended '
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
        result = run_codex_turn(prompt, cfg, model, workdir)
        if not result.final_message.strip():
            # Work left on disk by a killed turn outranks an empty `output`, which is kept
            # so the caller sees a turn that returned no code rather than a parse failure
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


def run_codex_turn(
    prompt: str,
    cfg: CodexConfig,
    model: ModelConfig,
    workdir: Path,
    output_schema: dict[str, Any] | None = None,
    scratch: Path | None = None,
) -> CodexResult:
    if shutil.which("codex") is None:
        raise RuntimeError(
            "`codex` was not found on PATH; install the Codex CLI or select another "
            "`translator`/`wrapper`"
        )

    output_schema = output_schema or CODE_SCHEMA
    with tempfile.TemporaryDirectory() as tmp:
        schema = None
        if model.enable_output_schema:
            schema = Path(tmp) / "schema.json"
            schema.write_text(json.dumps(output_schema))
        codex_cmd = _codex_cmd(cfg, model, workdir, schema)
        logger.debug(f"Running {' '.join(codex_cmd)}")
        env = _env(cfg, model, workdir, scratch)
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
    return _parse_events(stdout, model.name, _answer_key(output_schema))


def _answer_key(schema: dict[str, Any]) -> str:
    # The last required property is the one the model fills in last, so a mid-turn status
    # update is the message that left it empty
    return str(schema["required"][-1])


def _codex_cmd(
    cfg: CodexConfig, model: ModelConfig, workdir: Path, schema: Path | None = None
) -> list[str]:
    cmd = [
        "codex",
        "exec",
        "-",  # Read the prompt from stdin
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        *("--sandbox", cfg.sandbox),
        *("--cd", str(workdir)),
        *("-c", 'web_search="disabled"'),
        *("-c", "features.view_image=false"),
        *("-c", f"agents.enabled={str(cfg.subagents).lower()}"),
        *("-c", "features.image_generation=false"),
        *("-c", "features.tool_suggest=false"),
        *("-c", "features.skill_search=false"),
        *("-c", "features.plugins=false"),
        *("-c", "features.apps=false"),
        *("-c", "features.browser_use=false"),
        *("-c", "features.browser_use_external=false"),
        *("-c", "features.computer_use=false"),
        *("-c", "features.in_app_browser=false"),
        *("-c", "allow_login_shell=false"),
        *("--model", model.api_name),
        *("-c", f'model_reasoning_effort="{model.reasoning_effort}"'),
        *("-c", 'model_reasoning_summary="none"'),
    ]
    if model.provider != "openai":
        p = f"model_providers.{model.provider}"
        cmd += ("-c", f'model_provider="{model.provider}"')
        cmd += ("-c", f'{p}.name="{model.provider}"')
        cmd += ("-c", f'{p}.base_url="{model.base_url}"')
        # Unlike `env_key`, command auth makes Codex fetch the provider's model catalog
        cmd += ("-c", f'{p}.auth.command="sh"')
        cmd += ("-c", f'{p}.auth.args=["-c", "echo ${model.env_key}"]')
    if schema is not None and model.enable_output_schema:
        cmd += ("--output-schema", str(schema))
    for override in cfg.extra_config:
        cmd += ("-c", override)
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


def _env(
    cfg: CodexConfig, model: ModelConfig, workdir: Path, scratch: Path | None = None
) -> dict[str, str]:
    keep = (
        "PATH",
        "HOME",
        "USER",
        "TERM",
        "LANG",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "RUSTUP_TOOLCHAIN",
        "CARGO_BUILD_JOBS",
        "RUST_TEST_THREADS",
        "NEXTEST_TEST_THREADS",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        *("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"),
        *("http_proxy", "https_proxy", "no_proxy"),
        # The OpenAI login or the third-party auth command reads this variable
        model.env_key,
        *cfg.extra_env,
    )
    prefixes = ("LC_", "XDG_", "CODEX_")
    env = {
        name: value
        for name, value in os.environ.items()
        if name in keep or name.startswith(prefixes)
    }
    env["SHELL"] = "/bin/bash"
    # `workdir` is read-only for some callers, so they redirect everything codex writes.
    # Absolute, since codex runs from `workdir` and would resolve a relative path there.
    codex_dir = ((scratch or workdir / "target") / "codex").absolute()
    tmpdir = codex_dir / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(tmpdir)
    codex_home = codex_dir / "home"
    codex_home.mkdir(parents=True, exist_ok=True)
    env["CODEX_HOME"] = str(codex_home)
    env.setdefault("CARGO_BUILD_JOBS", "1")
    env.setdefault("RUST_TEST_THREADS", "1")
    env.setdefault("NEXTEST_TEST_THREADS", "1")
    env.setdefault("CARGO_TARGET_DIR", str(codex_dir))
    return env


def _parse_events(stdout: str, model_name: str, answer_key: str = "output") -> CodexResult:
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

    final, empty_output = _final_message(messages, answer_key)
    return CodexResult(final, usage, "\n\n".join(reasoning), stdout, empty_output)


def _final_message(messages: list[str], answer_key: str) -> tuple[str, str]:
    # `--output-schema` constrains every agent message, so the status updates the agent
    # emits mid-turn parse too, carrying an empty answer
    empty = ""
    for text in reversed(messages):
        try:
            parsed = json_repair_loads(text.strip())
        except Exception:
            continue
        if isinstance(parsed, dict) and (answer := parsed.get(answer_key)) is not None:
            if not isinstance(answer, str) or answer.strip():
                return text, ""
            empty = empty or text

    # Nothing carried an answer; returning "" lets the caller salvage the work left on disk
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
        info = model_info(model_name) or {}
    except Exception as error:
        logger.warning(f"Could not load model prices: {error}")
        info = {}

    input_cost = info.get("input_cost_per_token") or 0.0
    output_cost = info.get("output_cost_per_token") or 0.0
    if not input_cost and not output_cost:
        logger.warning(f"No pricing for `{model_name}`; spend is unreported")
        return None

    return {
        "input": input_cost,
        "output": output_cost,
        # Providers that do not price these separately bill them as ordinary input
        "cache_read": info.get("cache_read_input_token_cost") or input_cost,
        "cache_write": info.get("cache_creation_input_token_cost") or input_cost,
    }
