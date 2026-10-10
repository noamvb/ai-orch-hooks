"""Shared helpers for ai-orch's Claude Code hooks.

Adapted from rootstock-os hooks/_hooklib.py
(https://github.com/Mazhron/rootstock-os, commit 06c1a08, MIT License,
Copyright (c) 2026 Travis Rhoda). Rewritten for ai-orch: hooks are wired
globally in ~/.claude/settings.json, so they run in every Claude Code session
on this machine, including delegated `claude -p` runs launched by bin/delegate.

Rules every hook in this directory follows:
- Fail open. A hook that crashes must never block the user: wrap the entry
  point in run(main) and any exception exits 0 silently (logged to state_dir).
- Standard library only. Python 3.9+.
- Delegate awareness: bin/delegate exports AIORCH_DEPTH=1 to its engines, so
  depth() >= 1 means "this session is a delegate". in_delegate_worktree()
  says whether a path lies inside a delegate's throwaway worktree.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Optional

WORKTREE_MARKER = "/.aiorch-worktrees/"
_OUTCOMES = ("deny", "block", "context", "message", "silent", "error")
_run_outcome: Optional[str] = None


def read_event() -> Dict[str, Any]:
    """The hook's stdin JSON payload; {} when stdin is empty or not JSON."""
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def depth() -> int:
    """Delegation depth: 0 in an orchestrator session, >= 1 inside a delegate."""
    try:
        return max(0, int(os.environ.get("AIORCH_DEPTH", "0")))
    except ValueError:
        return 0


def is_delegate() -> bool:
    return depth() >= 1


def in_delegate_worktree(path: Optional[str]) -> bool:
    """True when path (or the cwd, when path is None) is inside .aiorch-worktrees/."""
    target = path if path else os.getcwd()
    try:
        resolved = os.path.realpath(target)
    except OSError:
        resolved = target
    return WORKTREE_MARKER in resolved + "/"


def state_dir() -> Path:
    """Private per-user state directory for hooks, created 0700 on first use.

    ${XDG_STATE_HOME:-~/.local/state}/ai-orch/hooks. Tests override it with
    AIORCH_HOOK_STATE_DIR so they never touch the real one.
    """
    override = os.environ.get("AIORCH_HOOK_STATE_DIR")
    if override:
        path = Path(override)
    else:
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        path = Path(base) / "ai-orch" / "hooks"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def session_key(event: Dict[str, Any]) -> str:
    """A filesystem-safe key for per-session state files."""
    raw = str(event.get("session_id") or "unknown")
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in raw)[:80]


def engine(event: Dict[str, Any]) -> str:
    """Identify Codex from its transcript or session environment."""
    if ("/.codex/" in str(event.get("transcript_path") or "")
            or os.environ.get("CODEX_SESSION_ID") or os.environ.get("CODEX_THREAD_ID")):
        return "codex"
    return "claude"


def _track_outcome(outcome: str) -> None:
    global _run_outcome
    if _run_outcome is not None and _OUTCOMES.index(outcome) < _OUTCOMES.index(_run_outcome):
        _run_outcome = outcome


def _log_run(event: Dict[str, Any], name: str, outcome: str, ms: int) -> None:
    """Best-effort bounded ledger; never include tool input or command text."""
    try:
        path = state_dir() / "hook-runs.jsonl"
        if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            path.write_text("".join(lines[len(lines) // 2:]), encoding="utf-8")
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "hook": name, "event": event.get("hook_event_name", ""),
               "engine": engine(event), "depth": depth(), "session": session_key(event),
               "outcome": outcome, "ms": ms}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        pass


def emit(obj: Dict[str, Any]) -> None:
    specific = obj.get("hookSpecificOutput") or {}
    if specific.get("permissionDecision") == "deny":
        _track_outcome("deny")
    if obj.get("decision") == "block":
        _track_outcome("block")
    if "additionalContext" in specific:
        _track_outcome("context")
    if "systemMessage" in obj:
        _track_outcome("message")
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def deny(reason: str) -> None:
    """PreToolUse: refuse the tool call. The reason is shown to the model."""
    emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }})


def context(event_name: str, text: str) -> None:
    """SessionStart / UserPromptSubmit: add text to the model's context."""
    emit({"hookSpecificOutput": {
        "hookEventName": event_name,
        "additionalContext": text,
    }})


def block_stop(reason: str) -> None:
    """Stop: refuse to end the turn once. The reason is shown to the model.

    Callers must check event.get("stop_hook_active") first and never block
    when it is true, or the turn can loop forever.
    """
    emit({"decision": "block", "reason": reason})


def system_message(text: str) -> None:
    """Any event: show a one-line note to the user without blocking."""
    emit({"systemMessage": text})


def log_error(name: str) -> None:
    try:
        with open(state_dir() / "errors.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {name}\n")
            fh.write(traceback.format_exc())
    except Exception:
        pass


def log_line(name: str, text: str) -> None:
    """Append one caller-supplied line to errors.log, without raising."""
    try:
        with open(state_dir() / "errors.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {name} {text}\n")
    except Exception:
        pass


def run(main: Callable[[Dict[str, Any]], None], name: str) -> None:
    """Entry point wrapper: read the event, call main(event), fail open."""
    global _run_outcome
    event = read_event()
    previous_outcome = _run_outcome
    _run_outcome = "error"
    started = time.perf_counter()
    try:
        main(event)
        _track_outcome("silent")
    except SystemExit:
        _track_outcome("silent")
        raise
    except Exception:
        log_error(name)
    finally:
        ms = int((time.perf_counter() - started) * 1000)
        _log_run(event, name, _run_outcome, ms)
        _run_outcome = previous_outcome
    sys.exit(0)
