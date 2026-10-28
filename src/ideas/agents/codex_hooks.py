#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

"""Native Codex hooks for test generation, run as `python codex_hooks.py STATE_PATH AUDIT_PATH`.

Codex executes this file for every hook event, once per tool call, so it imports only the
standard library; `ideas` takes seconds to import.
"""

import fcntl
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

HOOK_EVENTS = ("PreToolUse", "PostToolUse", "PreCompact", "PostCompact", "Stop")

_TOKEN_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def _usage_cost(usage: dict[str, int], rates: dict[str, float] | None) -> float | None:
    if rates is None:
        return None
    cached = usage["cached_input_tokens"]
    written = usage["cache_write_input_tokens"]
    fresh = max(usage["input_tokens"] - cached - written, 0)
    return (
        fresh * rates["input"]
        + cached * rates["cache_read"]
        + written * rates["cache_write"]
        + usage["output_tokens"] * rates["output"]
    )


def _transcript(event: dict[str, Any], state: dict[str, Any]) -> Path | None:
    if supplied := event.get("transcript_path") or state.get("transcript_path"):
        state["transcript_path"] = supplied
        return Path(supplied)

    session_id = event.get("session_id") or state.get("session_id")
    if not session_id:
        return None
    state["session_id"] = session_id
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    for path in (codex_home / "sessions").rglob(f"*{session_id}.jsonl"):
        state["transcript_path"] = str(path)
        return path
    return None


def _refresh_usage(event: dict[str, Any], state: dict[str, Any]) -> None:
    """Copy the latest cumulative rollout usage into the shared hook state."""
    try:
        path = _transcript(event, state)
        if path is None:
            return
        offset = int(state.get("transcript_offset") or 0)
        if path.stat().st_size < offset:
            offset = 0
        with path.open("rb") as transcript:
            transcript.seek(offset)
            while (line := transcript.readline()).endswith(b"\n"):
                try:
                    record = json.loads(line)
                except ValueError:
                    transcript.seek(-len(line), os.SEEK_CUR)
                    break
                payload = record.get("payload") or {}
                if record.get("type") != "event_msg" or payload.get("type") != "token_count":
                    continue
                total = (payload.get("info") or {}).get("total_token_usage") or {}
                usage = {key: max(int(total.get(key) or 0), 0) for key in _TOKEN_KEYS}
                state["usage"] = usage
                state["cost"] = _usage_cost(usage, state.get("rates"))
            else:
                # Leave a line Codex is still writing for the next hook
                transcript.seek(-len(line), os.SEEK_CUR)
            state["transcript_offset"] = transcript.tell()
    except (OSError, TypeError, ValueError):
        return


def _hook_response(event: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    count = int(state["tool_calls"])
    maximum = int(state["max_steps"])
    match event.get("hook_event_name"):
        case "PreToolUse":
            cost, budget = state.get("cost"), state["max_budget"]
            if cost is not None and cost >= budget:
                reason = (
                    f"Budget of ${budget:.2f} exhausted after ${cost:.4f}. "
                    "Return the best final result without making another tool call."
                )
            elif count >= maximum:
                reason = f"Only 0 steps left. {state['hurry_message']}".strip()
            else:
                state["tool_calls"] = count + 1
                return {}
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        case "PostToolUse" if count > int(state["last_reported"]):
            state["last_reported"] = count
            remaining = max(maximum - count, 0)
            hurry_steps = int(state["hurry_steps"])
            message = str(state.get("hurry_message") or "")
            if hurry_steps > 0 and message and remaining <= hurry_steps:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": f"Only {remaining} steps left. {message}",
                    }
                }
        case "Stop" if not event.get("stop_hook_active"):
            minimum = int(state["minimum_steps"])
            if count < minimum:
                return {
                    "decision": "block",
                    "reason": f"This tool can only be called after {minimum} steps are exhausted",
                }
    return {}


def main(state_path: Path, audit_path: Path) -> None:
    event = json.load(sys.stdin)
    with state_path.open("r+", encoding="utf-8") as state_file:
        fcntl.flock(state_file, fcntl.LOCK_EX)
        state = json.load(state_file)
        state_before = dict(state)
        _refresh_usage(event, state)
        response = _hook_response(event, state)
        state_file.seek(0)
        json.dump(state, state_file, separators=(",", ":"))
        state_file.truncate()
        state_file.flush()
        os.fsync(state_file.fileno())
        audit = {
            "timestamp_ns": time.time_ns(),
            "event": event,
            "state_before": state_before,
            "response": response,
            "state_after": state,
        }
        try:
            with audit_path.open("a", encoding="utf-8") as audit_file:
                fcntl.flock(audit_file, fcntl.LOCK_EX)
                audit_file.write(json.dumps(audit, separators=(",", ":")) + "\n")
                audit_file.flush()
                os.fsync(audit_file.fileno())
        except OSError as error:
            print(f"Could not write Codex hook audit: {error}", file=sys.stderr)
    print(json.dumps(response, separators=(",", ":")))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(f"usage: {sys.argv[0]} STATE_PATH AUDIT_PATH")
    main(Path(sys.argv[1]), Path(sys.argv[2]))
