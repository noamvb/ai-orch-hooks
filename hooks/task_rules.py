"""Refuse Claude Stop while live background tasks remain unexplained."""
from __future__ import annotations

from datetime import datetime
import json
import os
import re
import time

import _hooklib
import _transcript

BACKGROUND = re.compile(r"Command running in background with ID: ([A-Za-z0-9]+)")
MONITOR = re.compile(r"Monitor started \(task ([A-Za-z0-9]+), expires in (\d+)(s|m|h)")
WAKEUP = re.compile(r"Next wakeup scheduled for [^(]*\(in (\d+)s\)")
TASK_ID = re.compile(r"<task-id>([^<]+)</task-id>")
STATUS = re.compile(r"<status>.*?</status>", re.S)
STOPPED = re.compile(r"Successfully stopped task: ([A-Za-z0-9]+)")
JSON_ID = re.compile(r'"task_id"\s*:\s*"([A-Za-z0-9]+)"')
ACK = re.compile(r"^\s*WAITING ON\s+(\S+?)[:\s]", re.M)
CANDIDATE = re.compile(r'"type"\s*:\s*"(?:user|assistant|queue-operation)"')
MARKERS = ("Command running in background", "Monitor started", "Next wakeup scheduled",
           "<task-id>", "Successfully stopped", "Loop stopped", "queued_command")


def _records(path):
    """Read raw records, retaining notification user lines and queued prompts."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not any(marker in line for marker in MARKERS) and not CANDIDATE.search(line):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or rec.get("isSidechain"):
                    continue
                attachment = rec.get("attachment")
                if rec.get("type") == "attachment" and (
                    not isinstance(attachment, dict) or attachment.get("type") != "queued_command"
                ):
                    continue
                yield rec
    except (OSError, TypeError, ValueError):
        return


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content if isinstance(b, dict)
                         and isinstance(b.get("text"), str))
    return ""


def _timestamp(rec):
    try:
        return datetime.fromisoformat(rec["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def _expiry(started, digits, unit="s"):
    try:
        return started + int(digits) * {"s": 1, "m": 60, "h": 3600}[unit]
    except (ValueError, OverflowError):
        return None


def _scan(path, now):
    tasks, labels, acknowledged = {}, {}, set()
    for rec in _records(path):
        kind = rec.get("type")
        message = rec.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        if kind == "assistant":
            for block in blocks:
                if block.get("type") == "tool_use":
                    data = block.get("input")
                    if isinstance(data, dict) and isinstance(block.get("id"), str):
                        description = data.get("description")
                        command = data.get("command")
                        labels[block["id"]] = (description if isinstance(description, str)
                                               else command[:80] if isinstance(command, str) else "")
                elif block.get("type") == "text":
                    acknowledged.update(ACK.findall(_text(block.get("text"))))
        if kind == "user" and (isinstance(content, str) or any(
            b.get("type") == "text" for b in blocks
        )):
            acknowledged.clear()

        # Only notification strings/queued prompts end tasks via XML status.
        notification = ""
        if kind == "user" and isinstance(content, str):
            notification = content
        elif kind == "attachment":
            notification = _text(rec["attachment"].get("prompt"))
        elif kind == "queue-operation":
            notification = _text(rec.get("content"))
        if STATUS.search(notification):
            for task_id in TASK_ID.findall(notification):
                tasks.pop(task_id, None)

        if kind != "user":
            continue
        for block in blocks:
            if block.get("type") != "tool_result":
                continue
            text = _text(block.get("content"))
            for task_id in STOPPED.findall(text):
                tasks.pop(task_id, None)
            if "Successfully stopped" in text:
                for task_id in JSON_ID.findall(text):
                    tasks.pop(task_id, None)
            if text.startswith("Loop stopped"):
                tasks.pop("wakeup", None)
            label = labels.get(block.get("tool_use_id"), "") if isinstance(block.get("tool_use_id"), str) else ""
            started = _timestamp(rec)
            # The harness writes these at the very start of its own result; the same
            # phrase later in a result is command output quoting it.
            head = text.lstrip()
            bg, monitor, wakeup = BACKGROUND.match(head), MONITOR.match(head), WAKEUP.match(head)
            if bg:
                task_id, task_kind, expiry = bg[1], "background command", None
            elif monitor and started is not None:
                task_id, task_kind = monitor[1], "monitor"
                expiry = _expiry(started, monitor[2], monitor[3])
                if expiry is None:
                    continue
            elif wakeup and started is not None:
                task_id, task_kind, expiry = "wakeup", "wakeup", _expiry(started, wakeup[1])
                if expiry is None:
                    continue
            else:
                continue
            # Replacing a task also moves it to the newer start's position.
            tasks.pop(task_id, None)
            tasks[task_id] = ({"id": task_id, "kind": task_kind, "label": label}, expiry)
    return [task for task, expiry in tasks.values() if expiry is None or expiry > now], acknowledged


def live_tasks(transcript_path: str, now: float) -> list[dict]:
    """Return tasks still live at now, in their start-line order."""
    return _scan(transcript_path, now)[0]


def stop_check(event: dict, now: float | None = None) -> str | None:
    """Require a current-turn WAITING ON line for each retained live task."""
    if not isinstance(event, dict) or _hooklib.depth() >= 1 or event.get("stop_hook_active"):
        return None
    path = event.get("transcript_path")
    if not isinstance(path, str) or not os.path.isfile(path):
        return None
    if _transcript.transcript_format(path) != "claude":
        return None
    tasks, acknowledged = _scan(path, time.time() if now is None else now)
    unexplained = [task for task in tasks if task["id"] not in acknowledged]
    if not unexplained:
        return None
    names = ", ".join(
        f'{task["id"]} ({task["kind"]}: {task["label"]})' if task["label"]
        else f'{task["id"]} ({task["kind"]})' for task in unexplained
    )
    return (
        "BACKGROUND TASKS: still running at the end of this turn: " + names
        + ". Stop each one you no longer need (TaskStop with its id; ScheduleWakeup with stop: true for the wakeup), "
        "or keep it by writing a line `WAITING ON <id>: <what it waits for>` in your reply."
    )
