"""Adapted from rootstock-os hooks/stop_tick.py (https://github.com/Mazhron/rootstock-os, commit 06c1a08, MIT License, Copyright (c) 2026 Travis Rhoda).

Stop hook: the checkpoint task counter + live context gauge.
Ticks a task only when git HEAD moved or working tree status changed.
Refuses to stop every 8 tasks until the agent addresses the checkpoint prompt.
Warns at <80% context remaining and refuses once at <30% context remaining.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _hooklib
import _transcript

try:  # Deck-only phone rules; absent from the public cloud copy.
    import phone_rules
except ImportError:
    phone_rules = None


def is_git_repo(cwd: str) -> bool:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:
        return False


def work_fingerprint(cwd: str) -> str:
    head = ""
    status = ""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            head = r.stdout.strip()
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            status = r.stdout
    except Exception:
        pass
    return hashlib.sha1((head + "\n" + status).encode("utf-8", "replace")).hexdigest()


def load_state(path: Any) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Any, state: Dict[str, Any]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
    except OSError:
        pass


def get_context_window() -> int:
    config_path = Path.home() / ".config" / "ai-orch" / "hooks.json"
    try:
        if config_path.is_file():
            with open(config_path, encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict) and "context_window_tokens" in data:
                    return int(data["context_window_tokens"])
    except Exception:
        pass
    # rootstock reference tools/checkpoint.py: ~925k auto-compact budget on a 1M window.
    return 925000


def get_transcript_usage(path: Optional[str]) -> Optional[int]:
    if not path or not os.path.isfile(path):
        return None
    tokens, _ = _transcript.last_context(path)
    return tokens


def main(event: Dict[str, Any]) -> None:
    # The phone note rides along with whatever this hook says, or alone.
    note = phone_rules.stop_restore(event) if phone_rules else None
    if not _tick(event, note) and note:
        _hooklib.system_message(note)


def _tick(event: Dict[str, Any], note: Optional[str]) -> bool:
    """The task/context logic; True when it emitted output (which carries note)."""
    extra = [note] if note else []
    cwd = event.get("cwd") or os.getcwd()
    key = _hooklib.session_key(event)
    state_file = _hooklib.state_dir() / f"stop_tick_{key}.json"
    state = load_state(state_file)

    in_repo = is_git_repo(cwd)
    if in_repo:
        fp = work_fingerprint(cwd)
        prev_fp = state.get("fp")
        ticked = fp != prev_fp
        if ticked:
            task_count = int(state.get("task_count", 0)) + 1
            state["task_count"] = task_count
            state["fp"] = fp
        else:
            task_count = int(state.get("task_count", 0))
    else:
        ticked = False
        task_count = int(state.get("task_count", 0))

    checkpoint_at = int(state.get("checkpoint_at", 0))
    n = task_count - checkpoint_at

    transcript_path = event.get("transcript_path")
    tokens, tr_window = _transcript.last_context(transcript_path) if transcript_path else (None, None)
    if tr_window is not None:
        window = tr_window
    else:
        window = get_context_window()

    remaining: Optional[float] = None
    pct = 100
    if tokens is not None:
        remaining = max(0.0, 1.0 - tokens / float(window))
        pct = round(remaining * 100)

    task_checkpoint_due = n >= 8

    ctx_dire = remaining is not None and remaining < 0.30
    ctx_advised = remaining is not None and 0.30 <= remaining < 0.80

    # A lower band gets one advisory; a two-band rise indicates compaction.
    band = int(remaining * 100) // 10 if remaining is not None else None
    warned_band = state.get("ctx_warned_band")
    if band is not None and warned_band is not None and band >= warned_band + 2:
        state.pop("ctx_warned_band", None)
        warned_band = None
    ctx_advised = ctx_advised and (warned_band is None or band < warned_band)

    stop_hook_active = bool(event.get("stop_hook_active"))
    delegate = _hooklib.depth() >= 1

    ctx_refusal_due = ctx_dire and not state.get("ctx_blocked", False)

    if not delegate and not stop_hook_active and (task_checkpoint_due or ctx_refusal_due):
        reasons = []
        if task_checkpoint_due:
            try:
                top_result = subprocess.run(
                    ["git", "rev-parse", "--show-toplevel"],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                toplevel = top_result.stdout.strip() if top_result.returncode == 0 else cwd
            except Exception:
                toplevel = cwd
            reasons.append(
                f'CHECKPOINT: {n} tasks since the last checkpoint. Address this in your reply before stopping. '
                f'If the arc is closed: record where things stand with '
                f'`~/.ai-orch/bin/checkpoint --repo {shlex.quote(toplevel)} "<done / open / next step>"`, '
                f'then tell the user this is a good point for a fresh session or /compact. '
                f'If the arc is still open: tell the user in one line what remains and why you are continuing. '
                f'The counter restarts now.'
            )
            state["checkpoint_at"] = task_count
        if ctx_refusal_due:
            state["ctx_blocked"] = True
            reasons.append(
                f"CONTEXT: {pct}% context remaining (~{tokens // 1000}k / {window // 1000}k tokens used). "
                f"Suggest /compact or write a handoff now."
            )
        save_state(state_file, state)
        _hooklib.block_stop(" ".join(reasons + extra))
        return True

    if delegate:
        msgs = []
        if task_checkpoint_due:
            msgs.append(f"CHECKPOINT ADVISED: {n} tasks since last checkpoint.")
        if ctx_dire:
            msgs.append(f"CONTEXT: {pct}% context remaining (~{tokens // 1000}k used).")
        elif ctx_advised:
            state["ctx_warned_band"] = band
            msgs.append(f"CONTEXT ADVISED: {pct}% context remaining (~{tokens // 1000}k used).")
        if msgs:
            save_state(state_file, state)
            _hooklib.system_message(" ".join(msgs + extra))
            return True

    # Non-delegate advisory
    adv_msgs = []
    if ctx_advised:
        state["ctx_warned_band"] = band
        adv_msgs.append(f"CONTEXT ADVISED: {pct}% context remaining (~{tokens // 1000}k used).")
    if adv_msgs:
        save_state(state_file, state)
        _hooklib.system_message(" ".join(adv_msgs + extra))
        return True

    save_state(state_file, state)
    return False


if __name__ == "__main__":
    _hooklib.run(main, "stop_tick")
