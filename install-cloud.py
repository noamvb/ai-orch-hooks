#!/usr/bin/env python3
"""Install the ai-orch safety hooks into a Claude Code cloud session's VM.

Run from a cloud environment's setup script, after cloning this repo to
~/.ai-orch-hooks. It points ~/.ai-orch at the clone (the hook commands use
$HOME/.ai-orch/... exactly as on the owner's machines) and merges the hook
block into ~/.claude/settings.json. Re-running replaces its own entries and
keeps everything else.

Only the hooks that make sense on a throwaway VM are wired: the delete and
force-push guard, the spawn limiter, the elapsed-time line and the context
advisory. A SessionStart entry pulls this repo, so hook updates arrive even
when the environment's setup snapshot is days old.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
LINK = os.path.join(HOME, ".ai-orch")
SETTINGS = os.path.join(HOME, ".claude", "settings.json")
MARK = ".ai-orch/hooks/"


def h(name, timeout):
    return {"type": "command", "timeout": timeout,
            "command": f'f="$HOME/.ai-orch/hooks/{name}.py"; [ -f "$f" ] || exit 0; exec python3 "$f"'}


OURS = {
    "PreToolUse": [{"matcher": "Agent|Task|Workflow", "hooks": [h("fanout_guard", 10)]},
                   {"matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit", "hooks": [h("preserve_guard", 10)]}],
    "UserPromptSubmit": [{"hooks": [h("time_budget", 5)]}],
    "PostToolUse": [{"hooks": [h("time_budget", 5)]}],
    "Stop": [{"hooks": [h("stop_tick", 20)]}],
    "SessionStart": [{"hooks": [{"type": "command", "timeout": 20, "command":
        f'git -C "{HERE}" pull -q --ff-only >/dev/null 2>&1 || true  # {MARK}update'}]}],
}


def link_home():
    if os.path.islink(LINK):
        if os.path.realpath(LINK) == os.path.realpath(HERE):
            return
        os.unlink(LINK)
    elif os.path.exists(LINK):
        print(f"install-cloud: {LINK} exists and is not a link; leaving it", file=sys.stderr)
        return
    os.symlink(HERE, LINK)


def merge_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    hooks = data.setdefault("hooks", {})
    for event, groups in OURS.items():
        kept = []
        for group in hooks.get(event, []):
            group = dict(group)
            group["hooks"] = [x for x in group.get("hooks", []) if MARK not in x.get("command", "")]
            if group["hooks"]:
                kept.append(group)
        hooks[event] = kept + groups
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def main():
    link_home()
    merge_settings()
    print(f"install-cloud: hooks wired in {SETTINGS} from {HERE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
