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
when the environment's setup snapshot is days old. Skills under skills/ (the
deck-bridge hand-off) are linked into ~/.claude/skills, and owner-rules.md is
copied into ~/.claude/CLAUDE.md between marker comments. Last,
install-android.py adds the Android SDK and Flutter (skipped and reported on
any failure), and ANDROID_HOME goes into the settings' env block when the SDK
is there.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
LINK = os.path.join(HOME, ".ai-orch")
SETTINGS = os.path.join(HOME, ".claude", "settings.json")
SKILLS = os.path.join(HOME, ".claude", "skills")
RULES = os.path.join(HOME, ".claude", "CLAUDE.md")
BEGIN, END = "<!-- ai-orch-cloud-rules:begin -->", "<!-- ai-orch-cloud-rules:end -->"
MARK = ".ai-orch/hooks/"
ANDROID_ROOT = os.environ.get("AIORCH_ANDROID_ROOT", "/opt/android-sdk")


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


def link_skills():
    src_root = os.path.join(HERE, "skills")
    if not os.path.isdir(src_root):
        return
    os.makedirs(SKILLS, exist_ok=True)
    for name in sorted(os.listdir(src_root)):
        src, dst = os.path.join(src_root, name), os.path.join(SKILLS, name)
        if os.path.lexists(dst):
            if os.path.realpath(dst) != os.path.realpath(src):
                print(f"install-cloud: {dst} exists; leaving it", file=sys.stderr)
            continue
        os.symlink(src, dst)


def install_rules():
    """Copy owner-rules.md into ~/.claude/CLAUDE.md between markers, keeping any other content."""
    src = os.path.join(HERE, "owner-rules.md")
    if not os.path.isfile(src):
        return
    with open(src, encoding="utf-8") as fh:
        block = f"{BEGIN}\n{fh.read().strip()}\n{END}\n"
    try:
        with open(RULES, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        text = ""
    start, stop = text.find(BEGIN), text.find(END)
    if start != -1 and stop > start:
        text = text[:start] + block + text[stop + len(END):].lstrip("\n")
    else:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
    os.makedirs(os.path.dirname(RULES), exist_ok=True)
    with open(RULES, "w", encoding="utf-8") as fh:
        fh.write(text)


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
    if os.path.isdir(os.path.join(ANDROID_ROOT, "platform-tools")):
        data.setdefault("env", {}).update(ANDROID_HOME=ANDROID_ROOT, ANDROID_SDK_ROOT=ANDROID_ROOT)
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def main():
    link_home()
    link_skills()
    install_rules()
    android = os.path.join(HERE, "install-android.py")
    if os.path.isfile(android):
        subprocess.run([sys.executable, android], check=False)
    merge_settings()
    print(f"install-cloud: hooks wired in {SETTINGS} from {HERE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
