#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

"""Codex CLI agent for test generation: the run and its live console."""

import fcntl
import functools
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Concatenate, TextIO, cast

from rich.console import Console, ConsoleDimensions, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from ideas.agents import codex_hooks
from ideas.agents.verifiers import VERIFIER_COMMAND_ENV, VERIFIER_SOCKET_ENV
from ideas.codex import CodexConfig, _codex_cmd, _codex_login, _env, _parse_events, _rates
from ideas.model import ModelConfig

logger = logging.getLogger("ideas.agents.codex_backend")

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "output": {"type": "string"},
    },
    "required": ["reasoning", "output"],
    "additionalProperties": False,
}

_FINAL_RESPONSE = """\
# Final response

After finishing the tests, return
`{"reasoning": "<brief account of the completed work>", "output": "<nonempty summary>"}`.
The files in the working tree, rather than this summary, are the deliverable.
"""

_MAX_RESULT_LENGTH = 3000
_STYLES = {"bash": "yellow", "apply_patch": "green", "todo_list": "magenta", "error": "red"}
_TOOL_NAMES = {"command_execution": "Bash", "file_change": "apply_patch"}
_HIDDEN_FIELDS = {"id", "type", "status", "aggregated_output", "exit_code"}


class CodexAgentError(RuntimeError):
    """A Codex agent run failed or ended without a usable result."""


def run_codex_agent(
    prompt: str,
    model: ModelConfig,
    workdir: Path,
    trajectory: Path,
    *,
    prompt_dir: Path,
    hurry_message: str,
    allow_early_finish: bool,
    max_steps: int,
    max_budget: float,
) -> None:
    """Run Codex on `prompt` in `workdir`, logging to `trajectory` and dumping to `prompt_dir`."""
    workdir = workdir.resolve()
    trajectory = trajectory.resolve()
    hook_trajectory = trajectory.with_suffix(".hooks.jsonl")
    prompt = f"{prompt.rstrip()}\n\n{_FINAL_RESPONSE}"

    # Same layout and NUL escaping as the translator's `.prompt` dumps
    shutil.rmtree(prompt_dir, ignore_errors=True)
    prompt_dir.mkdir(parents=True)
    (prompt_dir / "_prompt.md").write_text(prompt.replace("\x00", "\\0"), encoding="utf-8")

    printer = _Printer(trajectory)
    printer.prompt(prompt)
    try:
        if shutil.which("codex") is None:
            raise RuntimeError("`codex` was not found on PATH; install the Codex CLI")
        rates = _rates(model.name)
        hurry_steps = max_steps // 10
        state_path = _install_hooks(
            workdir,
            hook_trajectory,
            {
                "tool_calls": 0,
                "last_reported": 0,
                "max_steps": max_steps,
                "hurry_steps": hurry_steps,
                "hurry_message": hurry_message,
                "minimum_steps": 0 if allow_early_finish else max_steps - hurry_steps,
                "max_budget": max_budget,
                "rates": rates,
                "cost": None if rates is None else 0.0,
            },
        )
        cfg = CodexConfig(
            extra_config=[
                f'projects={{{json.dumps(str(workdir))}={{trust_level="trusted"}}}}',
                "features.hooks=true",
            ],
            extra_env=[VERIFIER_COMMAND_ENV, VERIFIER_SOCKET_ENV],
        )
        env = _env(cfg, model, workdir)
        _codex_login(env)
        events = _EventPrinter(printer, state_path, hook_trajectory)
        with tempfile.TemporaryDirectory() as temporary:
            schema = Path(temporary) / "schema.json"
            schema.write_text(json.dumps(_OUTPUT_SCHEMA))
            command = _codex_cmd(cfg, model, workdir, schema)
            # Hooks read cumulative usage from the persisted rollout and are trusted as ours
            command[command.index("--ephemeral")] = "--dangerously-bypass-hook-trust"
            logger.debug(f"Running {' '.join(command)}")
            stdout, stderr, exit_code = _run_streaming(
                command, input=prompt, cwd=workdir, env=env, on_line=events.print_line
            )
        (prompt_dir / "_codex.jsonl").write_text(
            stdout.replace("\x00", "\\0"), encoding="utf-8"
        )
        result = _parse_events(stdout, model.name)
    except Exception as error:
        raise CodexAgentError(f"Codex failed: {error}") from error

    printer.text(stderr, style="yellow")
    cost = result.usage.get("cost")
    status = _read_status(state_path)
    status = replace(
        status,
        cost=status.cost if cost is None else cost,
        total_tokens=result.usage["total_tokens"],
    )
    printer.result(result.final_message, status=status)

    if exit_code != 0:
        raise CodexAgentError(f"Codex exited with status {exit_code}")
    if cost is not None and cost >= max_budget:
        raise CodexAgentError(
            f"Codex exhausted its budget, using ${cost:.4f} of ${max_budget:.2f}"
        )
    if not result.final_message.strip():
        raise CodexAgentError("Codex returned no final message")


def _install_hooks(workdir: Path, hook_trajectory: Path, state: dict[str, Any]) -> Path:
    """Run `codex_hooks.py` on every hook event, sharing `state` between invocations."""
    hooks_directory = workdir / ".codex"
    hooks_directory.mkdir(parents=True, exist_ok=True)
    state_path = hooks_directory / "state.json"
    state_path.write_text(json.dumps(state))
    hook_trajectory.write_text("")

    script = codex_hooks.__file__
    command = shlex.join([sys.executable, script, str(state_path), str(hook_trajectory)])
    handler = {"hooks": [{"type": "command", "command": command}]}
    hooks = {"hooks": {event: [handler] for event in codex_hooks.HOOK_EVENTS}}
    (hooks_directory / "hooks.json").write_text(json.dumps(hooks))
    return state_path


def _run_streaming(
    command: list[str],
    *,
    input: str,
    cwd: Path,
    env: dict[str, str],
    on_line: Callable[[str], None],
) -> tuple[str, str, int]:
    """Run `command`, passing each stdout line to `on_line`; return stdout, stderr, and status."""
    stdout: list[str] = []
    stderr: list[str] = []
    with subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        cwd=cwd,
        env=env,
    ) as proc:
        assert proc.stdin and proc.stdout and proc.stderr
        drain = threading.Thread(target=stderr.extend, args=(proc.stderr,), daemon=True)
        drain.start()
        try:
            proc.stdin.write(input)
            proc.stdin.close()
            for line in proc.stdout:
                stdout.append(line)
                on_line(line)
            exit_code = proc.wait()
        except BaseException:
            _kill_group(proc)
            raise
        finally:
            drain.join()
    return "".join(stdout), "".join(stderr), exit_code


def _kill_group(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


# Live console


@dataclass(frozen=True, slots=True)
class _Status:
    """Hook telemetry displayed alongside a Codex tool call."""

    turn_count: int = 0
    max_turns: int | None = None
    cost: float | None = None
    max_budget: float | None = None
    total_tokens: int | None = None

    def subtitle(self) -> str:
        turns = f"{self.turn_count:,}"
        if self.max_turns is not None:
            turns += f"/{self.max_turns:,}"
        cost = "unavailable" if self.cost is None else f"${self.cost:.4f}"
        if self.max_budget is not None:
            cost += f"/${self.max_budget:.2f}"
        subtitle = f"turn={turns}  cost={cost}"
        if self.total_tokens is not None:
            subtitle = f"tokens={self.total_tokens:,}  {subtitle}"
        return subtitle


def _read_status(state_path: Path) -> _Status:
    try:
        with state_path.open(encoding="utf-8") as state_file:
            fcntl.flock(state_file, fcntl.LOCK_SH)
            state = json.load(state_file)
    except (OSError, ValueError):
        logger.debug("Could not read Codex hook state", exc_info=True)
        return _Status()
    return _Status(
        turn_count=state["tool_calls"],
        max_turns=state["max_steps"],
        cost=state.get("cost"),
        max_budget=state["max_budget"],
    )


class _PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return Text.from_ansi(super().format(record)).plain


class _Tee:
    """Write console output to a stream and each complete line to a logger."""

    def __init__(self, stream: TextIO, logger: logging.Logger):
        self.stream = stream
        self.logger = logger
        self.buffer = ""
        self.encoding = getattr(stream, "encoding", "utf-8")

    def write(self, text: str) -> int:
        self.stream.write(text)
        *lines, self.buffer = (self.buffer + text).split("\n")
        for line in filter(None, lines):
            self.logger.info(line)
        return len(text)

    def flush(self) -> None:
        self.stream.flush()

    def fileno(self) -> int:
        return self.stream.fileno()

    def isatty(self) -> bool:
        return self.stream.isatty()


class _Console(Console):
    """A Rich console sized from its real output stream that never exits on a closed pipe."""

    def __init__(self, file: _Tee) -> None:
        self._stream = file
        environ = {k: v for k, v in os.environ.items() if k not in ("COLUMNS", "LINES")}
        super().__init__(file=cast(TextIO, file), highlight=False, _environ=environ)

    @property
    def size(self) -> ConsoleDimensions:
        try:
            columns, lines = os.get_terminal_size(self._stream.fileno())
        except (OSError, ValueError):
            columns = lines = 0
        if not columns or not lines:
            # Redirected output is wider than Rich's default so logs stay readable
            columns, lines = shutil.get_terminal_size(fallback=(120, 25))
        return ConsoleDimensions(columns, lines)

    @size.setter
    def size(self, new_size: tuple[int, int]) -> None:
        self._width, self._height = new_size

    def on_broken_pipe(self) -> None:
        self.quiet = True
        raise BrokenPipeError("Codex console output stream is closed")


def _best_effort[S: "_Printer", **P](
    method: Callable[Concatenate[S, P], None],
) -> Callable[Concatenate[S, P], None]:
    """Disable rendering after a failure, since the console must never stop a Codex run."""

    @functools.wraps(method)
    def render(self: S, *args: P.args, **kwargs: P.kwargs) -> None:
        if not self._enabled:
            return
        try:
            method(self, *args, **kwargs)
        except Exception:
            self._enabled = False
            logger.warning(
                "Disabling Codex console output after a rendering failure", exc_info=True
            )

    return render


class _Printer:
    """Render Codex events as Rich panels, mirroring them as plain text to a log."""

    def __init__(self, log_path: Path) -> None:
        trajectory = logger.getChild("trajectory")
        trajectory.propagate = False
        handler = logging.FileHandler(log_path)
        handler.setFormatter(_PlainFormatter("%(asctime)s %(message)s"))
        trajectory.addHandler(handler)
        self._console = _Console(_Tee(sys.__stdout__ or sys.stdout, trajectory))
        self._enabled = True

    @_best_effort
    def prompt(self, content: str) -> None:
        panel = Panel(
            Markdown(content), title="[bold]Prompt[/bold]", border_style="cyan", padding=(1, 2)
        )
        self._console.print(panel)

    @_best_effort
    def text(self, content: str, *, style: str | None = None) -> None:
        if content.strip():
            self._console.print(content, style=style, markup=False)

    @_best_effort
    def tool_call(self, name: str, tool_input: dict[str, Any], *, status: _Status) -> None:
        parts: list[Any] = []
        for change in tool_input.get("changes") or []:
            parts.append(Text(str(change.get("path") or ""), style="bold cyan"))
            parts.append(Text(str(change.get("kind") or "change"), style="italic"))
        if command := tool_input.get("command"):
            parts.append(Syntax(str(command), "bash", theme="monokai", word_wrap=True))
        if patch := tool_input.get("patch"):
            parts.append(Syntax(str(patch), "diff", theme="monokai", word_wrap=True))
        for key, value in tool_input.items():
            if key not in ("changes", "command", "patch"):
                shown = (
                    value
                    if isinstance(value, str)
                    else json.dumps(value, indent=2, default=str)
                )
                parts.append(Text(f"{key}: {shown}", style="dim"))

        style = _STYLES.get(name.casefold(), "blue")
        self._console.print(
            Panel(
                Group(*parts) if parts else Text("(no arguments)"),
                title=Text(name, style=f"bold {style}"),
                subtitle=status.subtitle(),
                border_style=style,
                padding=(0, 1),
            )
        )

    @_best_effort
    def tool_result(self, content: str, *, is_error: bool, tool_name: str) -> None:
        style = "red" if is_error else _STYLES.get(tool_name.casefold(), "blue")
        if len(content) > _MAX_RESULT_LENGTH:
            half = _MAX_RESULT_LENGTH // 2
            content = f"{content[:half]}\n... (truncated) ...\n{content[-half:]}"
        self._console.rule("FAILED" if is_error else "RESULT", style=style)
        self._console.print(content, markup=False)
        self._console.rule(style=style)

    @_best_effort
    def reasoning(self, content: str) -> None:
        self._console.rule("Thinking", style="dim cyan")
        self._console.print(content, style="dim cyan italic", markup=False)
        self._console.rule(style="dim cyan")
        self._console.print()

    @_best_effort
    def result(self, content: str, *, status: _Status) -> None:
        self._console.print(
            Panel(
                Markdown(content) if content else Text("(no result)"),
                title="Result",
                subtitle=status.subtitle(),
                border_style="bold green",
                padding=(1, 2),
            )
        )


class _EventPrinter:
    """Render tool items from a `codex exec --json` stream as they arrive."""

    def __init__(self, printer: _Printer, state_path: Path, hook_trajectory: Path):
        self.printer = printer
        self.state_path = state_path
        self.hook_trajectory = hook_trajectory
        self.hook_offset = 0
        self.patches: deque[str] = deque()
        self.started: set[str | None] = set()

    def print_line(self, line: str) -> None:
        # Display is best-effort and must never stop the Codex subprocess
        try:
            self._print_event(json.loads(line))
        except Exception:
            logger.warning(f"Could not render Codex event: {line.rstrip()}", exc_info=True)

    def _print_event(self, event: dict[str, Any]) -> None:
        item = event.get("item") or {}
        match event.get("type"), item.get("type"):
            # Retries and failures arrive outside tool items; hiding them makes a run look stuck
            case "error", _:
                self.printer.text(f"Codex: {event.get('message')}", style="yellow")
            case "turn.failed", _:
                error = (event.get("error") or {}).get("message")
                self.printer.text(f"Codex turn failed: {error}", style="bold red")
            case "item.completed", "error":
                self.printer.text(f"Codex: {item.get('message')}", style="yellow")
            case "item.completed", "reasoning":
                if text := item.get("text"):
                    self.printer.reasoning(text)
            case _, None | "agent_message" | "reasoning" | "error":
                pass
            # A file change is shown on completion, once its hook has audited the patch
            case "item.started", "file_change":
                pass
            case "item.started", _:
                self.started.add(item.get("id"))
                self._print_call(item)
            case "item.completed", _:
                item_id = item.get("id")
                if item_id not in self.started:
                    self._print_call(item)
                self.started.discard(item_id)
                self.printer.tool_result(
                    _tool_result(item), is_error=_tool_failed(item), tool_name=_tool_name(item)
                )

    def _print_call(self, item: dict[str, Any]) -> None:
        tool_input = {key: value for key, value in item.items() if key not in _HIDDEN_FIELDS}
        if item["type"] == "file_change" and (patch := self._next_patch()):
            tool_input["patch"] = patch
        status = _read_status(self.state_path)
        self.printer.tool_call(_tool_name(item), tool_input, status=status)

    def _next_patch(self) -> str | None:
        """Recover the next applied patch, which public JSON events omit, from the hook audit."""
        with suppress(OSError), self.hook_trajectory.open("rb") as audit:
            audit.seek(self.hook_offset)
            for line in audit:
                if not line.endswith(b"\n"):
                    break
                self.hook_offset += len(line)
                with suppress(ValueError):
                    event = json.loads(line)["event"]
                    if (
                        event.get("hook_event_name") == "PostToolUse"
                        and event.get("tool_name") == "apply_patch"
                        and (patch := (event.get("tool_input") or {}).get("command"))
                    ):
                        self.patches.append(patch)
        return self.patches.popleft() if self.patches else None


def _tool_name(item: dict[str, Any]) -> str:
    return _TOOL_NAMES.get(item["type"], item["type"])


def _tool_result(item: dict[str, Any]) -> str:
    match item["type"]:
        case "command_execution":
            if output := item.get("aggregated_output"):
                return output
            exit_code = item.get("exit_code")
            return "(no output)" if exit_code is None else f"Exit code: {exit_code}"
        case "file_change" if item.get("changes"):
            return "\n".join(
                f"{change.get('kind') or 'change'}: {change.get('path') or ''}"
                for change in item["changes"]
            )
    return f"Status: {item.get('status') or 'completed'}"


def _tool_failed(item: dict[str, Any]) -> bool:
    return item.get("status") in {"failed", "error", "cancelled"} or bool(item.get("exit_code"))
