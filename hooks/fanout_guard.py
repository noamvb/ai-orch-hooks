"""PreToolUse fan-out / runaway guard for Agent, Task, Workflow tools.

Adapted from rootstock-os hooks/fanout_guard.py (https://github.com/Mazhron/rootstock-os, commit 06c1a08, MIT License, Copyright (c) 2026 Travis Rhoda).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _hooklib
import _transcript

# "delegate" is not a tool: bin/delegate asks this guard before every CLI dispatch.
SPAWN_TOOLS = ("Agent", "Task", "spawn_agent", "delegate")
WORKFLOW_TOOLS = ("Workflow",)
ALL_GUARDED_TOOLS = SPAWN_TOOLS + WORKFLOW_TOOLS

DEFAULTS = {
    "burst_cap": 8,
    "burst_window": 60,
    "flood_cap": 25,
    "flood_window": 600,
    "agents_warn_every": 10,
    "tokens_warn_every": 10_000_000,
    "velocity_window": 120,
    "velocity_warn": 3_000_000,
    "velocity_halt": 10_000_000,
}

WEIGHTS = {
    "input_tokens": 1.0,
    "cache_creation_input_tokens": 1.25,
    "cache_read_input_tokens": 0.1,
    "output_tokens": 5.0,
}


def _config_file() -> Path:
    override = os.environ.get("AIORCH_HOOK_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".config" / "ai-orch" / "hooks.json"


def load_limits() -> Dict[str, int]:
    limits = dict(DEFAULTS)
    cfg = _config_file()
    try:
        if cfg.is_file():
            with open(cfg, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                fanout = data.get("fanout")
                if isinstance(fanout, dict):
                    for k, v in fanout.items():
                        if k in DEFAULTS and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                            limits[k] = int(v)
    except Exception:
        pass
    return limits


def _get_now(event: Dict[str, Any]) -> float:
    env_now = os.environ.get("AIORCH_NOW")
    if env_now:
        try:
            return float(env_now)
        except ValueError:
            pass
    event_now = event.get("now")
    if isinstance(event_now, (int, float)):
        return float(event_now)
    return time.time()


def _fmt(n: float) -> str:
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    return f"{int(n // 1000)}k"


def spawn_log_path() -> Path:
    return _hooklib.state_dir() / "spawns.jsonl"


def _load_session_state(s_key: str) -> Dict[str, Any]:
    path = _hooklib.state_dir() / f"session_{s_key}.json"
    try:
        if path.is_file():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {
        "files": {},
        "raw": 0,
        "weighted": 0.0,
        "samples": [],
        "warned_agents": 0,
        "warned_level": 0,
        "warned_velocity": 0.0,
    }


def _save_session_state(s_key: str, state: Dict[str, Any]) -> None:
    path = _hooklib.state_dir() / f"session_{s_key}.json"
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except Exception:
        pass


def _get_spawn_counts(now: float, burst_window: float, flood_window: float, s_key: str) -> Tuple[int, int, int]:
    path = spawn_log_path()
    if not path.is_file():
        return 0, 0, 0
    burst_count = 0
    flood_count = 0
    total_session = 0
    burst_horizon = now - burst_window
    flood_horizon = now - flood_window
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = float(rec.get("timestamp", 0.0))
                    rec_s_key = rec.get("session_key")
                    if ts >= burst_horizon:
                        burst_count += 1
                    if rec_s_key == s_key:
                        total_session += 1
                        if ts >= flood_horizon:
                            flood_count += 1
                except Exception:
                    continue
    except Exception:
        pass
    return burst_count, flood_count, total_session


def _append_spawn(ts: float, s_key: str, tool_name: str) -> None:
    path = spawn_log_path()
    rec = {"timestamp": ts, "session_key": s_key, "tool_name": tool_name}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    _trim_spawn_log(path, ts)


TRIM_BYTES = 1_000_000
KEEP_SECONDS = 7 * 86400


def _trim_spawn_log(path: str, now: float) -> None:
    """Keep the log bounded: past TRIM_BYTES, drop records older than KEEP_SECONDS."""
    try:
        if os.path.getsize(path) <= TRIM_BYTES:
            return
        horizon = now - KEEP_SECONDS
        kept = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    if float(json.loads(line).get("timestamp", 0.0)) >= horizon:
                        kept.append(line)
                except Exception:
                    continue
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(kept)
        os.replace(tmp, path)
    except Exception:
        _hooklib.log_error("fanout_guard.trim")


def _transcripts(transcript_path: Optional[str], s_key: str) -> List[str]:
    out = []
    if transcript_path and os.path.isfile(transcript_path):
        out.append(transcript_path)
    base = os.path.join(os.path.dirname(transcript_path or ""), s_key or "", "subagents")
    try:
        if os.path.isdir(base):
            for name in os.listdir(base):
                if name.endswith(".jsonl"):
                    out.append(os.path.join(base, name))
    except OSError:
        pass
    return out


def _update_meter(s: Dict[str, Any], transcript_path: Optional[str], s_key: str, now: float, limits: Dict[str, int]) -> float:
    added = 0.0
    files = s.setdefault("files", {})
    for path in _transcripts(transcript_path, s_key):
        backfill = path not in files
        rec = files.setdefault(path, {"off": 0, "last_id": ""})
        try:
            size = os.path.getsize(path)
            if size < rec["off"]:
                rec["off"] = 0
            if size == rec["off"]:
                continue
            with open(path, "rb") as fh:
                fh.seek(rec["off"])
                chunk = fh.read()
        except OSError:
            continue
        if not chunk.endswith(b"\n"):
            cut = chunk.rfind(b"\n")
            if cut < 0:
                continue
            chunk = chunk[: cut + 1]
        rec["off"] += len(chunk)
        fmt = _transcript.transcript_format(path)
        line_idx = rec.get("line", 0)
        for ln in chunk.decode("utf-8", "replace").splitlines():
            line_idx += 1
            for u in _transcript._parse_line(ln, line_idx=line_idx - 1, fmt=fmt):
                if u.get("kind") != "usage":
                    continue
                uid = str(u.get("id") or "")
                if uid and uid == rec["last_id"]:
                    continue
                rec["last_id"] = uid
                raw = int(u.get("context_tokens") or 0)
                w = float(u.get("weighted_tokens") or 0.0)
                s["raw"] = s.get("raw", 0) + raw
                s["weighted"] = s.get("weighted", 0.0) + w
                if not backfill:
                    added += w
        rec["line"] = line_idx
    if added > 0:
        s.setdefault("samples", []).append([now, added])
    horizon = now - limits["velocity_window"]
    s["samples"] = [x for x in s.get("samples", []) if x[0] >= horizon]
    return sum(x[1] for x in s["samples"])


def main(event: Dict[str, Any]) -> None:
    tool_name = event.get("tool_name")
    if tool_name not in ALL_GUARDED_TOOLS:
        return

    now = _get_now(event)
    limits = load_limits()
    s_key = _hooklib.session_key(event)
    session_state = _load_session_state(s_key)

    # Rule 7: Velocity halt
    transcript_path = event.get("transcript_path")
    vel = _update_meter(session_state, transcript_path, s_key, now, limits)
    if vel >= limits["velocity_halt"]:
        _save_session_state(s_key, session_state)
        _hooklib.deny(
            f"FANOUT VELOCITY: {_fmt(vel)} weighted tokens in the last {limits['velocity_window']}s "
            f"(limit {_fmt(limits['velocity_halt'])}). Refused until window drains. Tune with /runaway."
        )
        return

    # Rule 3 (Burst) and Rule 4 (Flood)
    burst_count, flood_count, total_session = _get_spawn_counts(
        now, limits["burst_window"], limits["flood_window"], s_key
    )
    if burst_count >= limits["burst_cap"]:
        _save_session_state(s_key, session_state)
        _hooklib.deny(
            f"FANOUT BURST: {burst_count} sub-agents spawned machine-wide in the last "
            f"{limits['burst_window']}s (limit {limits['burst_cap']}). Tune with /runaway."
        )
        return

    if flood_count >= limits["flood_cap"]:
        _save_session_state(s_key, session_state)
        _hooklib.deny(
            f"FANOUT FLOOD: {flood_count} sub-agents spawned in this session in the last "
            f"{limits['flood_window']}s (limit {limits['flood_cap']}). Tune with /runaway."
        )
        return

    # Rule 2: Each permitted spawn appends one record to JSONL under state_dir()
    _append_spawn(now, s_key, tool_name)
    n = total_session + 1

    # Rule 6 and Rule 5: Warnings
    warnings = []
    if tool_name in WORKFLOW_TOOLS:
        warnings.append("FANOUT WARN: Workflow fans out multiple sub-agents; burst and flood limits still apply.")

    lvl = n // limits["agents_warn_every"]
    if lvl > session_state.get("warned_agents", 0):
        session_state["warned_agents"] = lvl
        warnings.append(f"FANOUT WARN: {n} sub-agents spawned this session")

    spend_lvl = int(session_state.get("weighted", 0.0) // limits["tokens_warn_every"])
    if spend_lvl > session_state.get("warned_level", 0):
        session_state["warned_level"] = spend_lvl
        warnings.append(f"FANOUT WARN: session spend ~{_fmt(session_state['weighted'])} weighted tokens")

    if vel >= limits["velocity_warn"] and now - session_state.get("warned_velocity", 0.0) > limits["velocity_window"]:
        session_state["warned_velocity"] = now
        warnings.append(
            f"FANOUT WARN: token velocity ~{_fmt(vel)} in the last {limits['velocity_window']}s "
            f"(refusal at {_fmt(limits['velocity_halt'])})"
        )

    if warnings:
        _hooklib.system_message(" | ".join(warnings))

    _save_session_state(s_key, session_state)


if __name__ == "__main__":
    _hooklib.run(main, "fanout_guard")
