"""Claude time budget context: one line a minute, only while a budget is set.

Wired for both engines (tests/tools/test_codex_plugin.py keeps the hook sets
identical), but the elapsed/budget signal is an Opus 5.5 behaviour
(docs/policy/archive/prompting-opus-5-5-and-gpt-6.md), so a Codex session
gets nothing.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _hooklib
import _transcript


EMIT_EVERY_S = 60


def _integer(value: Any, positive: bool = False) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    if str(result) != str(value).strip() or (positive and result <= 0):
        return None
    return result


def _state(path: Any) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _now() -> int:
    value = os.environ.get("AIORCH_TIME_BUDGET_NOW")
    parsed = _integer(value) if value is not None else None
    return parsed if parsed is not None else int(time.time())


def main(event: Dict[str, Any]) -> None:
    name = event.get("hook_event_name") or event.get("event")
    if name == "UserPromptSubmit":
        if _hooklib.depth() == 0:
            path = _hooklib.state_dir() / ("time_budget_turn_%s.json" % _hooklib.session_key(event))
            try:
                path.write_text(json.dumps({"turn_started": _now()}), encoding="utf-8")
            except Exception:
                pass
        return
    if name != "PostToolUse":
        return
    if _transcript.transcript_format(str(event.get("transcript_path") or "")) == "codex":
        return

    start = budget = None
    run_start = _integer(os.environ.get("AIORCH_RUN_STARTED_EPOCH"))
    if run_start is not None:
        start = run_start
        budget = _integer(os.environ.get("AIORCH_BUDGET_S"), positive=True)
    else:
        key = _hooklib.session_key(event)
        root = _hooklib.state_dir()
        session = _state(root / ("time_budget_%s.json" % key))
        candidate_budget = session.get("budget_s")
        candidate_start = session.get("set_at")
        if isinstance(candidate_budget, int) and not isinstance(candidate_budget, bool) and candidate_budget > 0 and isinstance(candidate_start, int) and not isinstance(candidate_start, bool):
            start, budget = candidate_start, candidate_budget
        else:
            turn = _state(root / ("time_budget_turn_%s.json" % key))
            candidate = turn.get("turn_started")
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                start = candidate
    # No budget, no line: a bare "elapsed Ns" after every tool call cost context
    # and told the model nothing it could pace against (owner, 11 Nov 2026).
    if start is None or budget is None:
        return
    now = _now()
    emit_path = _hooklib.state_dir() / ("time_budget_emit_%s.json" % _hooklib.session_key(event))
    last = _state(emit_path).get("emitted_at")
    if isinstance(last, int) and not isinstance(last, bool) and 0 <= now - last < EMIT_EVERY_S:
        return
    try:
        emit_path.parent.mkdir(parents=True, exist_ok=True)
        emit_path.write_text(json.dumps({"emitted_at": now}), encoding="utf-8")
    except Exception:
        pass
    elapsed = max(0, now - start)
    line = "elapsed %ss / %ss (pace the work; verification steps are not optional)" % (elapsed, budget)
    _hooklib.emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": line}})


if __name__ == "__main__":
    _hooklib.run(main, "time_budget")
