"""Transcript reader supporting Claude Code and OpenAI Codex JSONL formats.

Exposes:
    transcript_format(path: str) -> str
    iter_records(path: str, start_line: int = 0) -> Iterator[dict]
    last_context(path: str) -> Tuple[Optional[int], Optional[int]]
    last_exchange(path: str) -> Tuple[str, str]
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterator, Optional, Tuple

CODEX_TYPES = {
    "session_meta",
    "event_msg",
    "response_item",
    "turn_context",
    "world_state",
    "token_usage_record",
}
CLAUDE_TYPES = {
    "user",
    "assistant",
    "system",
    "summary",
}

WEIGHTS = {
    "input_tokens": 1.0,
    "cache_creation_input_tokens": 1.25,
    "cache_read_input_tokens": 0.1,
    "output_tokens": 5.0,
}

ERROR_RE = re.compile(
    r"(traceback|error:|\bFAIL\b|not found|no such file|command not found|"
    r"is not recognized|refused|denied|exit code [1-9]|still failing)",
    re.I,
)
EXIT_CODE_LINE_RE = re.compile(r"^Exit code: [1-9]", re.M)
PROCESS_EXITED_RE = re.compile(r"Process exited with code [1-9]")


def transcript_format(path: str) -> str:
    """Detect whether a transcript is 'claude', 'codex', or 'unknown'."""
    if not path or not os.path.isfile(path):
        return "unknown"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                top_type = data.get("type")
                if top_type in CODEX_TYPES:
                    return "codex"
                if top_type in CLAUDE_TYPES:
                    return "claude"
                if "payload" in data:
                    return "codex"
                if "message" in data:
                    return "claude"
                # Unrecognised bookkeeping lines (the desktop app opens with
                # "queue-operation") say nothing; decide on the next line.
                continue
    except OSError:
        return "unknown"
    return "unknown"


def _is_codex_error(text: str) -> bool:
    if bool(ERROR_RE.search(text[:4000])):
        return True
    if any(lit in text for lit in ("Script failed", "Script error", "Command blocked by PreToolUse hook")):
        return True
    if bool(EXIT_CODE_LINE_RE.search(text)):
        return True
    if bool(PROCESS_EXITED_RE.search(text)):
        return True
    return False


def _parse_codex_record(rec: Dict[str, Any], line_idx: int) -> Iterator[Dict[str, Any]]:
    top_type = rec.get("type")
    payload = rec.get("payload") or {}
    if not isinstance(payload, dict):
        return

    ptype = payload.get("type")

    # 1. user_text:
    # event_msg whose payload.type == "item_completed" and payload.item.type == "UserMessage" -> join payload.item.content[].text;
    # also event_msg with payload.type == "user_message" -> payload.message.
    # NEVER take user text from response_item messages with role user or developer.
    if top_type == "event_msg":
        if ptype == "item_completed":
            item = payload.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "UserMessage":
                content = item.get("content") or []
                if isinstance(content, list):
                    text = "".join(c.get("text", "") for c in content if isinstance(c, dict) and "text" in c)
                elif isinstance(content, str):
                    text = content
                else:
                    text = ""
                yield {"kind": "user_text", "line": line_idx, "text": text}
        elif ptype == "user_message":
            text = str(payload.get("message") or "")
            yield {"kind": "user_text", "line": line_idx, "text": text}

    # 2. assistant_text:
    # response_item with payload.type == "message" and payload.role == "assistant" -> join content[].text where content[].type == "output_text".
    if top_type == "response_item" and ptype == "message" and payload.get("role") == "assistant":
        content = payload.get("content") or []
        if isinstance(content, list):
            text = "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "output_text")
        elif isinstance(content, str):
            text = content
        else:
            text = ""
        yield {"kind": "assistant_text", "line": line_idx, "text": text}

    # 3. tool_call:
    # response_item with payload.type in {"custom_tool_call", "function_call"};
    # id = payload.call_id; name = payload.name; command = payload.input (custom) or payload.arguments (function), as a string.
    if top_type == "response_item" and ptype in ("custom_tool_call", "function_call"):
        call_id = str(payload.get("call_id") or "")
        name = str(payload.get("name") or "")
        if ptype == "custom_tool_call":
            raw_cmd = payload.get("input")
        else:
            raw_cmd = payload.get("arguments")
        command = raw_cmd if isinstance(raw_cmd, str) else json.dumps(raw_cmd)
        yield {"kind": "tool_call", "line": line_idx, "id": call_id, "name": name, "command": command}

    # 4. tool_result:
    # response_item with payload.type in {"custom_tool_call_output", "function_call_output"};
    # id = call_id; text = output if a string, else join output[].text;
    # is_error = the existing error regex from bin/lesson-log OR any of these literals in the text:
    # Script failed, Script error, Command blocked by PreToolUse hook, or a line matching ^Exit code: [1-9] or Process exited with code [1-9].
    if top_type == "response_item" and ptype in ("custom_tool_call_output", "function_call_output"):
        call_id = str(payload.get("call_id") or "")
        out = payload.get("output")
        if isinstance(out, str):
            text = out
        elif isinstance(out, list):
            text = "".join(item.get("text", "") for item in out if isinstance(item, dict))
        else:
            text = str(out or "")
        is_error = _is_codex_error(text)
        yield {"kind": "tool_result", "line": line_idx, "id": call_id, "text": text, "is_error": is_error}

    # 5. usage:
    # event_msg with payload.type == "token_count" and non-null payload.info;
    # u = info.last_token_usage; context_tokens = u.input_tokens; window = info.model_context_window;
    # weighted_tokens = (input_tokens - cached_input_tokens - cache_write_input_tokens) * 1.0 +
    # cached_input_tokens * 0.1 + cache_write_input_tokens * 1.25 + output_tokens * 5.0 (missing keys = 0);
    # id = "L<line>".
    if top_type == "event_msg" and ptype == "token_count" and payload.get("info"):
        info = payload.get("info") or {}
        if isinstance(info, dict):
            u = info.get("last_token_usage") or {}
            if isinstance(u, dict):
                in_tok = int(u.get("input_tokens") or 0)
                cached_tok = int(u.get("cached_input_tokens") or 0)
                cache_write_tok = int(u.get("cache_write_input_tokens") or 0)
                out_tok = int(u.get("output_tokens") or 0)
                non_cached = max(0, in_tok - cached_tok - cache_write_tok)
                weighted = (
                    non_cached * 1.0
                    + cached_tok * 0.1
                    + cache_write_tok * 1.25
                    + out_tok * 5.0
                )
                window = info.get("model_context_window")
                win_val = int(window) if window is not None else None
                yield {
                    "kind": "usage",
                    "line": line_idx,
                    "id": f"L{line_idx}",
                    "context_tokens": in_tok,
                    "window": win_val,
                    "weighted_tokens": weighted,
                }


def _parse_claude_record(rec: Dict[str, Any], line_idx: int) -> Iterator[Dict[str, Any]]:
    if rec.get("isSidechain") or rec.get("isMeta"):
        return

    top_type = rec.get("type")
    msg = rec.get("message")
    if not isinstance(msg, dict):
        msg = {}

    content = msg.get("content")
    if content is None and "content" in rec:
        content = rec.get("content")

    # If top_type == "user":
    if top_type == "user":
        if isinstance(content, str):
            s = content.strip()
            if s and not (s.startswith("<") or s.startswith("Caveat:")):
                yield {"kind": "user_text", "line": line_idx, "text": s}
        elif isinstance(content, list):
            blocks = [b for b in content if isinstance(b, dict)]
            tool_results = [b for b in blocks if b.get("type") == "tool_result"]
            if tool_results:
                for b in tool_results:
                    call_id = str(b.get("tool_use_id") or b.get("id") or "")
                    bc = b.get("content")
                    if isinstance(bc, str):
                        txt = bc
                    elif isinstance(bc, list):
                        txt = "\n".join(x.get("text", "") for x in bc if isinstance(x, dict))
                    else:
                        txt = str(bc or "")
                    is_err = bool(b.get("is_error")) or bool(ERROR_RE.search(txt[:4000]))
                    yield {"kind": "tool_result", "line": line_idx, "id": call_id, "text": txt, "is_error": is_err}
            else:
                txt = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
                if txt and not (txt.startswith("<") or txt.startswith("Caveat:")):
                    yield {"kind": "user_text", "line": line_idx, "text": txt}

    # If top_type == "assistant" or top_type is None and "message" in rec:
    if top_type == "assistant" or (top_type is None and ("message" in rec or "usage" in rec)):
        if isinstance(content, str):
            s = content.strip()
            if s:
                yield {"kind": "assistant_text", "line": line_idx, "text": s}
        elif isinstance(content, list):
            blocks = [b for b in content if isinstance(b, dict)]
            txt = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
            if txt:
                yield {"kind": "assistant_text", "line": line_idx, "text": txt}
            for b in blocks:
                if b.get("type") == "tool_use":
                    call_id = str(b.get("id") or "")
                    name = str(b.get("name") or "")
                    inp = b.get("input")
                    if isinstance(inp, dict):
                        if "command" in inp:
                            cmd = str(inp["command"])
                        elif "file_path" in inp:
                            cmd = str(inp["file_path"])
                        elif "path" in inp:
                            cmd = str(inp["path"])
                        elif name in ("Agent", "Task") and ("description" in inp or "prompt" in inp):
                            cmd = str(inp.get("description") or inp.get("prompt") or "")
                        else:
                            cmd = json.dumps(inp)
                    elif isinstance(inp, str):
                        cmd = inp
                    else:
                        cmd = str(inp or "")
                    yield {"kind": "tool_call", "line": line_idx, "id": call_id, "name": name, "command": cmd}

        # Usage
        u = msg.get("usage") or rec.get("usage")
        if isinstance(u, dict):
            in_tok = int(u.get("input_tokens") or 0)
            cr_tok = int(u.get("cache_read_input_tokens") or u.get("cache_read_tokens") or 0)
            cw_tok = int(u.get("cache_creation_input_tokens") or u.get("cache_creation_tokens") or 0)
            out_tok = int(u.get("output_tokens") or 0)
            ctx_tokens = in_tok + cr_tok + cw_tok
            model = str(msg.get("model") or rec.get("model") or "")
            rmult = 0.025 if "fable" in model else WEIGHTS["cache_read_input_tokens"]
            weighted = (
                in_tok * 1.0
                + cw_tok * 1.25
                + cr_tok * rmult
                + out_tok * 5.0
            )
            uid = str(msg.get("id") or rec.get("id") or f"L{line_idx}")
            yield {
                "kind": "usage",
                "line": line_idx,
                "id": uid,
                "context_tokens": ctx_tokens,
                "window": None,
                "weighted_tokens": weighted,
            }


def _parse_line(raw: str, line_idx: int = 0, fmt: Optional[str] = None) -> Iterator[Dict[str, Any]]:
    raw = raw.strip()
    if not raw:
        return
    try:
        rec = json.loads(raw)
    except ValueError:
        return
    if not isinstance(rec, dict):
        return

    target_fmt = fmt
    if target_fmt not in ("codex", "claude"):
        top_type = rec.get("type")
        if top_type in CODEX_TYPES or "payload" in rec:
            target_fmt = "codex"
        else:
            target_fmt = "claude"

    if target_fmt == "codex":
        yield from _parse_codex_record(rec, line_idx)
    else:
        yield from _parse_claude_record(rec, line_idx)


def iter_records(path: str, start_line: int = 0) -> Iterator[Dict[str, Any]]:
    """Yield normalized records from transcript, starting at 0-based line index start_line."""
    if not path or not os.path.isfile(path):
        return
    fmt = transcript_format(path)
    if fmt == "unknown":
        return
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line_idx, raw in enumerate(fh):
                if line_idx < start_line:
                    continue
                yield from _parse_line(raw, line_idx=line_idx, fmt=fmt)
    except OSError:
        return


def last_context(path: str) -> Tuple[Optional[int], Optional[int]]:
    """Return (context_tokens, window) for the last usage record in path."""
    if not path or not os.path.isfile(path):
        return None, None
    last_ctx: Optional[int] = None
    last_win: Optional[int] = None
    for r in iter_records(path):
        if r.get("kind") == "usage":
            last_ctx = r.get("context_tokens")
            last_win = r.get("window")
    return last_ctx, last_win


def last_exchange(path: str) -> Tuple[str, str]:
    """Return (last_user_text, last_assistant_text) from path."""
    if not path or not os.path.isfile(path):
        return "", ""
    last_user = ""
    last_asst = ""
    for r in iter_records(path):
        kind = r.get("kind")
        if kind == "user_text":
            last_user = r.get("text", "")
        elif kind == "assistant_text":
            last_asst = r.get("text", "")
    return last_user, last_asst
