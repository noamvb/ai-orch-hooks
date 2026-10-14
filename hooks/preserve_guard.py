"""PreToolUse guard implementing the preservation law for ai-orch.

Adapted from rootstock-os hooks/preserve_guard.py (https://github.com/Mazhron/rootstock-os, commit 06c1a08, MIT License, Copyright (c) 2026 Travis Rhoda).
"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _hooklib

# Deck-only rule modules (absent from the public cloud copy): each exposes
# check_bash(command, cwd, event) -> refusal or None and fails open itself.
_RULE_MODULES = []
for _name in ("phone_rules", "build_rules", "notes_rules"):
    try:
        _RULE_MODULES.append(__import__(_name))
    except ImportError:
        pass

import fnmatch
import json
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SEPARATORS = {";", "&&", "||", "|", "&", "\n", "(", ")"}

# Settings file writes pattern (Rule 4 safeguard)
_T = (r"(?:\"[^\"|;]*settings(?:\.local)?\.json\"|'[^'|;]*settings(?:\.local)?\.json'|"
      r"[\w.:~-]*[/\\][\w./\\:~-]*settings(?:\.local)?\.json\b)")
_TB = r"(?:" + _T + r"|settings(?:\.local)?\.json\b)"
SETTINGS_WRITE_RE = re.compile(
    r"(>{1,2}\s*" + _TB + r")"
    r"|(\b(cp|copy|mv|move|tee|copy-item|move-item)\s+(-\S+\s+)*(\S+\s+)?" + _T + r")"
    r"|(\b(set-content|out-file|add-content)\b[^|;\n]*" + _T + r")"
    r"|(\bsed\s+-i\S*\s+(\S+\s+)?" + _T + r")"
    r"|(open\([^)]*settings(?:\.local)?\.json[^)]*[\"'][wa])",
    re.I,
)

_C = (r"(?:\"[^\"|;]*config\.toml\"|'[^'|;]*config\.toml'|"
      r"[\w.:~-]*[/\\]\.codex[/\\]config\.toml\b)")
_CB = r"(?:" + _C + r"|\.codex[/\\]config\.toml\b)"
CODEX_SETTINGS_WRITE_RE = re.compile(
    r"(>{1,2}\s*" + _CB + r")"
    r"|(\b(cp|copy|mv|move|tee|copy-item|move-item)\s+(-\S+\s+)*(\S+\s+)?" + _C + r")"
    r"|(\b(set-content|out-file|add-content)\b[^|;\n]*" + _C + r")"
    r"|(\bsed\s+-i\S*\s+(\S+\s+)?" + _C + r")"
    r"|(open\([^)]*\.codex[/\\]config\.toml[^)]*[\"'][wa])",
    re.I,
)

CODE_PATTERNS = [
    r"\bos\s*\.\s*(remove|unlink|rmdir|removedirs)\s*\(",
    r"\bshutil\s*\.\s*rmtree\s*\(",
    r"\bpathlib\s*\.\s*Path\([^)]*\)\s*\.\s*unlink\s*\(",
    r"\.\s*unlink\s*\(",
    r"\.\s*rmdir\s*\(",
    r"\bsend2trash\s*\(",
    r"\bDirAccess\s*\.\s*remove",
    r"\bfs\s*\.\s*(rm|rmSync|unlink|unlinkSync|rmdir|rmdirSync)\s*\(",
    r"\b(File|Directory)\s*\.\s*Delete\s*\(",
    r"\[System\.IO\.(File|Directory)\]\s*::\s*Delete\s*\(",
    r"\bos\s*\.\s*system\s*\([^)]*\b(rm|rd|del|rmdir|erase)\b",
    r"\bsubprocess\s*\.\s*\w+\s*\([^)]*['\"](rm|rd|del|rmdir|erase|unlink|shred)['\"]",
]

PROSE_EXTS = (".md", ".txt", ".rst", ".csv", ".html")


_CURRENT_SESSION = "unknown"
_PENDING_CREATED: List[str] = []


def created_ledger() -> Optional[List[str]]:
    """Read only this session's bounded creation ledger, failing open on errors."""
    try:
        path = _hooklib.state_dir() / f"created_{_CURRENT_SESSION}.json"
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not all(isinstance(p, str) and os.path.isabs(p) for p in data):
            return None
        return data[-500:]
    except Exception:
        return None


def save_created(paths: List[str]) -> None:
    """Persist at most 500 entries, dropping the oldest."""
    try:
        path = _hooklib.state_dir() / f"created_{_CURRENT_SESSION}.json"
        path.write_text(json.dumps(paths[-500:]), encoding="utf-8")
    except Exception:
        return None


def record_created(value: str, cwd: str, parents: bool = False,
                   cwd_known: bool = True) -> None:
    """Stage a literal nonexistent path before execution; never claim existing work."""
    try:
        if not value or not literal_path(value) or (not cwd_known and not os.path.isabs(value)):
            return None
        path = os.path.abspath(os.path.join(cwd, os.path.expanduser(value)))
        if path.startswith("/dev/") or os.path.lexists(path):
            return None
        if parents:
            while not os.path.lexists(os.path.dirname(path)):
                path = os.path.dirname(path)
        real = os.path.realpath(path)
        if real not in _PENDING_CREATED:
            _PENDING_CREATED.append(real)
    except Exception:
        return None


def record_bash_created(cmd: str, args: List[str], cwd: str, cwd_known: bool) -> None:
    """Recognise creation operands and output redirects in an already parsed segment."""
    try:
        for i, arg in enumerate(args[:-1]):
            if arg in (">", ">>"):
                record_created(args[i + 1], cwd, cwd_known=cwd_known)
        operands = [a for a in without_redirections(args) if not a.startswith("-")]
        if cmd in ("mkdir", "touch"):
            for value in operands:
                record_created(value, cwd, parents=cmd == "mkdir" and "-p" in args,
                               cwd_known=cwd_known)
        elif cmd in ("cp", "mv") and len(operands) >= 2:
            record_created(operands[-1], cwd, cwd_known=cwd_known)
        elif cmd == "git" and args and args[0] == "clone":
            clone_args = args[1:]
            # Options with values cannot be mistaken for the URL or destination.
            values: List[str] = []
            i = 0
            while i < len(clone_args):
                arg = clone_args[i]
                if arg in ("-b", "--branch", "-o", "--origin", "--depth", "--reference",
                           "--reference-if-able", "--separate-git-dir", "--template", "-c", "--config"):
                    i += 2
                    continue
                if not arg.startswith("-"):
                    values.append(arg)
                i += 1
            if values:
                dest = values[-1] if len(values) > 1 else values[0].rstrip("/").rsplit("/", 1)[-1]
                if len(values) == 1 and dest.endswith(".git"):
                    dest = dest[:-4]
                if literal_path(values[0]):
                    record_created(dest, cwd, cwd_known=cwd_known)
        elif cmd == "python3" and args[:2] == ["-m", "venv"] and len(args) > 2:
            record_created(args[2], cwd, cwd_known=cwd_known)
        elif cmd == "uv" and args[:1] == ["venv"] and len(args) > 1:
            record_created(args[1], cwd, cwd_known=cwd_known)
        elif cmd == "virtualenv" and operands:
            record_created(operands[0], cwd, cwd_known=cwd_known)
    except Exception:
        return None


def consume_created(targets: List[str], cwd: str) -> Optional[bool]:
    """Authorise only targets within this session's ledger and consume deleted entries."""
    try:
        paths = created_ledger()
        if paths is None:
            return None
        if not targets or any(not literal_path(t) for t in targets):
            return False
        resolved = [os.path.realpath(os.path.join(cwd, os.path.expanduser(t))) for t in targets]
        if not all(any(t == p or t.startswith(p + os.sep) for p in paths) for t in resolved):
            return False
        save_created([p for p in paths
                      if not any(p == t or p.startswith(t + os.sep) for t in resolved)])
        return True
    except Exception:
        return None


def log_refusal(event: Dict[str, Any], reason: str) -> None:
    """Append one refusal record to state_dir()/preserve.jsonl."""
    try:
        p = _hooklib.state_dir() / "preserve.jsonl"
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session_id": event.get("session_id"),
            "cwd": event.get("cwd"),
            "tool_name": event.get("tool_name"),
            "tool_input": event.get("tool_input"),
            "reason": reason,
        }
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass


def fallback_scan(clean_cmd: str) -> Optional[str]:
    """Conservatively find destructive operations when shell parsing fails."""
    segment = r"(?:^|&&|\|\||[;&|()\n])\s*(?:(?:sudo|command|xargs)\s+)*"
    patterns = [
        re.compile(segment + r"(?:rm|rmdir|unlink|shred)\b[^;&|()\n]*", re.I),
        re.compile(r"\bfind\b[^;&|()\n]*?(?:-delete\b|-exec\s+rm\b)[^;&|()\n]*", re.I),
        re.compile(r"\bgit\b[^;&|()\n]*?\breset\s+--(?:hard|merge)\b[^;&|()\n]*", re.I),
        re.compile(r"\bgit\b[^;&|()\n]*?\bclean\s+(?:-[a-z]*f[a-z]*|--force)\b[^;&|()\n]*", re.I),
        re.compile(r"\bgit\b[^;&|()\n]*?\bcheckout\s+(?:--|\.)[^;&|()\n]*", re.I),
        re.compile(r"\bgit\b[^;&|()\n]*?\brestore\b(?![^;&|()\n]*--staged)[^;&|()\n]*", re.I),
        re.compile(r"\bgit\b[^;&|()\n]*?\bstash\s+(?:drop|clear)\b[^;&|()\n]*", re.I),
        re.compile(r"\bgit\b[^;&|()\n]*?\bbranch\s+-D\b[^;&|()\n]*", re.I),
        re.compile(r"\bgit\b[^;&|()\n]*?\b(?:rebase|filter-branch|filter-repo)\b[^;&|()\n]*", re.I),
        re.compile(r"\bgit\b[^;&|()\n]*?\bcommit\b[^;&|()\n]*--amend\b[^;&|()\n]*", re.I),
        re.compile(r"\bgit\b[^;&|()\n]*?\bpush\b[^;&|()\n]*(?:--force(?:-with-lease)?\b|(?:^|\s)-f\b|(?:^|\s)\+[^\s;&|()]+)", re.I),
        re.compile(r"--no-verify\b", re.I),
    ] + ([SETTINGS_WRITE_RE, CODEX_SETTINGS_WRITE_RE] if settings_guarded() else []) + [
        re.compile(pat, re.I) for pat in CODE_PATTERNS]
    for pattern in patterns:
        match = pattern.search(clean_cmd)
        if match:
            found = match.group(0).strip()[:60]
            return ("PRESERVE: this command could not be parsed (unbalanced quotes?) and "
                    f"contains '{found}'; rewrite it so it parses - for example put the inline "
                    "script in a file - and retry.")
    return None


def check_never_delete(path_str: str, cwd: Optional[str] = None) -> Optional[str]:
    """Return refusal reason if target is on never-delete list, else None."""
    clean_p = path_str.strip().strip("'\"`")
    if not clean_p:
        return "empty target"

    expanded = os.path.expanduser(os.path.expandvars(clean_p))
    if not os.path.isabs(expanded):
        base = cwd if cwd else os.getcwd()
        abs_p = os.path.abspath(os.path.join(base, expanded))
    else:
        abs_p = os.path.abspath(expanded)

    try:
        real_p = os.path.realpath(abs_p)
    except OSError:
        real_p = abs_p

    home_abs = os.path.abspath(os.path.expanduser("~"))
    try:
        home_real = os.path.realpath(home_abs)
    except OSError:
        home_real = home_abs

    # 1. $HOME itself
    if abs_p == home_abs or real_p == home_real or clean_p in ("~", "$HOME", "${HOME}"):
        return "$HOME itself"

    # Filesystem root
    if abs_p == "/" or real_p == "/" or clean_p in ("/", "/*"):
        return "filesystem root"

    # 2. any .git directory
    norm_parts = abs_p.split(os.sep)
    raw_parts = clean_p.replace("\\", "/").split("/")
    if ".git" in norm_parts or ".git" in raw_parts or os.path.basename(abs_p) == ".git":
        return "any .git directory"

    # 3. *.result.json under any logs/
    basename = os.path.basename(abs_p)
    if fnmatch.fnmatch(basename, "*.result.json"):
        if "logs" in norm_parts or "logs" in raw_parts:
            return "*.result.json under logs/"

    # 4. ~/.ai-orch
    aiorch_abs = os.path.abspath(os.path.expanduser("~/.ai-orch"))
    try:
        aiorch_real = os.path.realpath(aiorch_abs)
    except OSError:
        aiorch_real = aiorch_abs
    # The checkout root itself only; files inside it are covered by the
    # receipt, .git and CLAUDE.md rules and are otherwise grantable.
    if abs_p == aiorch_abs or real_p == aiorch_real:
        return "~/.ai-orch"

    # 5. CLAUDE.md
    if os.path.basename(abs_p) == "CLAUDE.md" or os.path.basename(clean_p) == "CLAUDE.md":
        return "CLAUDE.md"

    # 6. AGENTS.md
    if os.path.basename(abs_p) == "AGENTS.md" or os.path.basename(clean_p) == "AGENTS.md":
        return "AGENTS.md"

    return None


def settings_guarded() -> bool:
    """Settings writes are refused only inside a delegate (AIORCH_DEPTH >= 1).

    The owner edits ~/.claude/settings.json and ~/.codex/config.toml through the
    interactive session they are talking to (owner decision, 4 Oct 2026); an
    unattended delegate must not, since those files hold the hooks guarding it.
    """
    return _hooklib.is_delegate()


def classify_settings_path(path_str: str, cwd: Optional[str] = None) -> Optional[str]:
    """Return 'claude', 'codex', or 'instructions' for protected paths, else None."""
    clean_p = path_str.strip().strip("'\"`")
    if not clean_p:
        return None
    expanded = os.path.expanduser(os.path.expandvars(clean_p))
    if not os.path.isabs(expanded):
        base = cwd if cwd else os.getcwd()
        abs_p = os.path.abspath(os.path.join(base, expanded))
    else:
        abs_p = os.path.abspath(expanded)
    try:
        real_p = os.path.realpath(abs_p)
    except OSError:
        real_p = abs_p

    home_abs = os.path.abspath(os.path.expanduser("~"))
    try:
        home_real = os.path.realpath(home_abs)
    except OSError:
        home_real = home_abs

    s1_abs = os.path.abspath(os.path.join(home_abs, ".claude", "settings.json"))
    s2_abs = os.path.abspath(os.path.join(home_abs, ".claude", "settings.local.json"))
    c_abs = os.path.abspath(os.path.join(home_abs, ".codex", "config.toml"))

    s1_real = os.path.realpath(s1_abs)
    s2_real = os.path.realpath(s2_abs)
    c_real = os.path.realpath(c_abs)

    if abs_p in (s1_abs, s2_abs) or real_p in (s1_real, s2_real):
        return "claude"
    if abs_p == c_abs or real_p == c_real:
        return "codex"

    instruction_paths = [
        os.path.join(home_abs, relative)
        for relative in (
            ".claude/CLAUDE.md", ".codex/AGENTS.md", ".ai-orch/CLAUDE.md",
            ".ai-orch/AGENTS.md", ".ai-orch/docs/owner.md",
        )
    ]
    if abs_p in instruction_paths or real_p in map(os.path.realpath, instruction_paths):
        return "instructions"

    projects_abs = os.path.join(home_abs, ".claude", "projects")
    projects_real = os.path.realpath(projects_abs)
    for candidate, projects in ((abs_p, projects_abs), (real_p, projects_real)):
        prefix = projects + os.sep
        if candidate.startswith(prefix):
            parts = candidate[len(prefix):].split(os.sep)
            if len(parts) >= 3 and parts[1] == "memory":
                return "instructions"
    return None


def is_settings_path(path_str: str, cwd: Optional[str] = None) -> bool:
    """True for protected 'claude', 'codex', or 'instructions' paths."""
    return classify_settings_path(path_str, cwd) is not None


def is_temp_path(path_str: str, cwd: Optional[str] = None) -> bool:
    """True if path resolves under $TMPDIR, /tmp, /private/tmp, or /private/var/folders/."""
    clean_p = path_str.strip().strip("'\"`")
    if not clean_p:
        return False
    expanded = os.path.expanduser(os.path.expandvars(clean_p))
    if not os.path.isabs(expanded):
        base = cwd if cwd else os.getcwd()
        abs_p = os.path.abspath(os.path.join(base, expanded))
    else:
        abs_p = os.path.abspath(expanded)
    try:
        real_p = os.path.realpath(abs_p)
    except OSError:
        real_p = abs_p

    temp_roots = ["/tmp", "/private/tmp", "/private/var/folders"]
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        temp_roots.append(tmpdir)
        temp_roots.append(os.path.realpath(tmpdir))

    for root in temp_roots:
        r_abs = os.path.abspath(root).rstrip("/")
        try:
            r_real = os.path.realpath(r_abs).rstrip("/")
        except OSError:
            r_real = r_abs
        if abs_p == r_abs or abs_p.startswith(r_abs + "/") or \
           real_p == r_real or real_p.startswith(r_real + "/"):
            return True
    return False


def inside_a_worktree(path_str: str, cwd: Optional[str] = None) -> bool:
    """True when path resolves strictly below a .aiorch-worktrees/ root.

    The root itself is excluded: deleting it would take every delegate's
    worktree, including runs still in flight.
    """
    clean_p = path_str.strip().strip("'\"`")
    if not clean_p:
        return False
    expanded = os.path.expanduser(os.path.expandvars(clean_p))
    base = cwd if cwd else os.getcwd()
    abs_p = os.path.abspath(os.path.join(base, expanded))
    try:
        real_p = os.path.realpath(abs_p)
    except OSError:
        real_p = abs_p
    marker = _hooklib.WORKTREE_MARKER
    idx = (real_p + "/").find(marker)
    if idx < 0:
        return False
    rest = (real_p + "/")[idx + len(marker):].strip("/")
    return bool(rest)


def check_and_consume_grant(target_path: str, cwd: Optional[str] = None) -> bool:
    """If a valid live grant covers target_path, delete the grant file and return True."""
    clean_p = target_path.strip().strip("'\"`")
    if not clean_p:
        return False
    expanded = os.path.expanduser(os.path.expandvars(clean_p))
    if not os.path.isabs(expanded):
        base = cwd if cwd else os.getcwd()
        abs_target = os.path.abspath(os.path.join(base, expanded))
    else:
        abs_target = os.path.abspath(expanded)
    try:
        real_target = os.path.realpath(abs_target)
    except OSError:
        real_target = abs_target

    grants_dir = _hooklib.state_dir() / "grants"
    if not grants_dir.is_dir():
        return False

    now = time.time()
    for gpath in sorted(grants_dir.glob("*.json")):
        try:
            with open(gpath, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                continue
            exp = float(data.get("expires_at", 0))
            if exp <= now:
                try:
                    gpath.unlink()
                except OSError:
                    pass
                continue
            if data.get("kind") == "branch":
                continue  # a branch grant never authorises a path delete
            gt = data.get("target")
            if not gt:
                continue
            gt_abs = os.path.abspath(os.path.expanduser(gt))
            try:
                gt_real = os.path.realpath(gt_abs)
            except OSError:
                gt_real = gt_abs
            if abs_target == gt_abs or real_target == gt_real:
                try:
                    gpath.unlink()
                except OSError:
                    pass
                return True
        except Exception:
            continue
    return False


def repo_identity(path: str) -> str:
    """The repository a path belongs to, identical for every worktree of it.

    Kept in step with repo_identity() in bin/delete-grant, which records it.
    """
    abs_p = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    try:
        out = subprocess.run(
            ["git", "-C", abs_p, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return os.path.dirname(os.path.realpath(out.stdout.strip()))
    except (OSError, subprocess.SubprocessError):
        pass
    return abs_p


def check_and_consume_branch_grants(repo_path: str, branches: List[str]) -> bool:
    """If every branch has a live grant for this repository, spend them all and return True.

    All or nothing: a command naming one ungranted branch is refused and spends
    no grant, so the user's permission for the others survives the refusal.
    """
    if not branches:
        return False
    repo_id = repo_identity(repo_path)
    grants_dir = _hooklib.state_dir() / "grants"
    if not grants_dir.is_dir():
        return False
    now = time.time()
    found: Dict[str, Path] = {}
    for gpath in sorted(grants_dir.glob("*.json")):
        try:
            with open(gpath, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict) or data.get("kind") != "branch":
                continue
            if float(data.get("expires_at", 0)) <= now:
                continue
            b = data.get("branch")
            if data.get("repo") == repo_id and b in branches and b not in found:
                found[b] = gpath
        except Exception:
            continue
    if set(found) != set(branches):
        return False
    for gpath in found.values():
        try:
            gpath.unlink()
        except OSError:
            pass
    return True


SHELL_KEYWORDS = {"do", "then", "else", "elif", "{", "!", "time"}
# Wrapper -> its options that take a separate value.
COMMAND_WRAPPERS = {
    "sudo": {"-u", "-g", "-U", "-C", "-D", "-h", "-p", "-r", "-t", "-T"},
    "doas": {"-u", "-C"},
    "command": set(),
    "env": {"-u", "-C", "-S"},
    "nice": {"-n"},
    "nohup": set(),
    "exec": {"-a"},
}

REDIRECT_OP_RE = re.compile(r"^[&\d]*[<>][<>&]*$")


def without_redirections(args: List[str]) -> List[str]:
    """Remove shell redirection operators, descriptors and their operands."""
    result: List[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if REDIRECT_OP_RE.match(arg):
            if result and result[-1].isdigit():
                result.pop()
            skip_next = True
            continue
        if re.match(r"^[&\d]*[<>]", arg):
            continue  # fused operator and operand
        result.append(arg)
    return result


def literal_path(value: str) -> bool:
    """A shell path we can resolve without evaluating shell expressions."""
    return not any(c in value for c in "$`*?[")


def substitute_targets(args: List[str], assignments: Dict[str, str], cwd_known: bool) -> List[str]:
    """Resolve only earlier literal assignments; retain uncertainty explicitly."""
    result: List[str] = []
    for arg in args:
        value = re.sub(r"\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)",
                       lambda m: assignments.get(m.group(1) or m.group(2),
                           os.environ.get(m.group(1) or m.group(2), m.group(0))), arg)
        if not literal_path(value) or (not cwd_known and not os.path.isabs(value)):
            value = "/$UNRESOLVED/" + value.lstrip("/")
        result.append(value)
    return result


def check_restore_redirect(subcmd: str, subargs: List[str], following: List[str],
                           cwd: str, cwd_known: bool,
                           assignments: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Refuse git object output that overwrites existing work or an unknown target."""
    operands = without_redirections(subargs)
    if subcmd not in ("show", "cat-file") or not any(":" in a for a in operands):
        return None
    if subcmd == "cat-file" and "-p" not in operands:
        return None
    targets = [subargs[i + 1] for i, a in enumerate(subargs[:-1])
               if a == ">" and (i == 0 or not subargs[i - 1].isdigit() or subargs[i - 1] == "1")]
    if following and following[0] == "tee" and not any(a in ("-a", "--append") for a in following):
        targets.extend(a for a in without_redirections(following[1:]) if not a.startswith("-"))
    for target in substitute_targets(targets, assignments or {}, cwd_known):
        unresolved = not literal_path(target) or (not cwd_known and not os.path.isabs(target))
        prohibited = unresolved
        if not unresolved and not is_temp_path(target, cwd):
            abs_target = os.path.abspath(os.path.join(cwd, os.path.expanduser(target)))
            if os.path.isfile(abs_target):
                try:
                    git = subprocess.run(["git", "-C", os.path.dirname(abs_target),
                                          "rev-parse", "--is-inside-work-tree"],
                                         capture_output=True, text=True, timeout=5)
                    prohibited = git.returncode == 0 and git.stdout.strip() == "true"
                except (OSError, subprocess.SubprocessError):
                    pass
        if prohibited:
            return (f"PRESERVE: git verb '{subcmd} overwrite <path>' that discards work or rewrites history is prohibited. "
                    "Rewording a refused command does not make it allowed; ask the user.")
    return None


def branch_operands(subargs: List[str]) -> List[str]:
    """Branch names from `git branch` arguments, skipping options and redirections.

    The shell lexer splits `2>&1` into `2`, `>&`, `1` and `> /dev/null` into `>`,
    `/dev/null`; none of them is a branch name. Counting them made a delete of
    delegate/* branches look like a delete of branches `2` and `1`.
    """
    names: List[str] = []
    skip_next = False
    for a in subargs:
        if skip_next:
            skip_next = False
            continue
        if REDIRECT_OP_RE.match(a):
            if names and names[-1].isdigit():
                names.pop()  # the fd number written before the operator
            if not re.search(r"[<>]&?\d+$|[<>]&-$", a):
                skip_next = True  # the operator's file or fd follows as its own token
            continue
        if a.startswith("-") or a == "branch":
            continue
        names.append(a)
    return names


def strip_prose_heredocs(cmd: str) -> str:
    """Drop heredocs that merely write prose."""
    def repl(m: re.Match) -> str:
        head, tag = m.group(1), m.group(3)
        if re.search(r"\b(cat|tee|set-content|out-file|add-content)\b", head, re.I):
            tgt = re.search(r"(>{1,2}|tee|-path|-filepath)\s*\"?'?([^\s\"'|;&]+)", head, re.I)
            name = (tgt.group(2) if tgt else "").lower()
            if name.endswith(PROSE_EXTS):
                return head + "\n<HEREDOC BODY>\n" + tag
        return m.group(0)

    cmd = re.sub(
        r"([^\n]*<<-?\s*['\"]?(\w+)['\"]?[^\n]*)\n.*?\n(\2)[ \t]*(?=\n|$)",
        repl,
        cmd,
        flags=re.S,
    )
    return cmd


HEREDOC_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "ssh"}


def extract_heredocs(cmd: str) -> Tuple[str, List[Tuple[str, str, bool]]]:
    """Replace closed heredoc bodies, retaining their consumer and quoting."""
    bodies: List[Tuple[str, str, bool]] = []
    lines = cmd.splitlines(keepends=True)
    result: List[str] = []
    opener = re.compile(r"(?<!<)<<(-?)[ \t]*(?:(['\"])(\w+)\2|(\w+))")
    i = 0
    while i < len(lines):
        head = lines[i]
        matches = list(opener.finditer(head))
        result.append(head)
        i += 1
        for match in matches:
            tag = match.group(3) or match.group(4)
            end = i
            while end < len(lines):
                closing = lines[end].rstrip("\r\n")
                if match.group(1):
                    closing = closing.lstrip("\t")
                if closing == tag:
                    break
                end += 1
            if end == len(lines):
                # An incomplete body retains the existing parsing/fallback path.
                result.extend(lines[i:])
                return "".join(result), bodies
            prefix = head[:match.start()]
            lexer = shlex.shlex(prefix, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            words: List[str] = []
            for word in lexer:
                if word in SEPARATORS:
                    words = []
                else:
                    words.append(word)
            # Any shell word in the segment makes the body shell: `sudo -u x bash`,
            # `env bash`, `/bin/sh`, `docker exec -i c sh`.
            is_shell = any(os.path.basename(w) in HEREDOC_SHELLS for w in words)
            bodies.append(("sh" if is_shell else "", "".join(lines[i:end]), bool(match.group(2))))
            # shlex treats newlines as whitespace: keep following commands separate.
            result.extend(("HEREDOC_BODY\n", lines[end].rstrip("\r\n") + ";\n"))
            i = end + 1
    return "".join(result), bodies


def check_code_deletions(cmd: str, cwd: str, body: bool = False) -> Optional[str]:
    """Apply the code deletion rule; body targets follow the deletion call."""
    for pat in CODE_PATTERNS:
        match = re.search(pat, cmd)
        if match:
            call_start = cmd.find("(", match.start())
            target_text = cmd[call_start + 1:] if call_start >= 0 else ""
            m_target = re.match(r"\s*(['\"])(.*?)\1", target_text)
            targets = [m_target.group(2)] if m_target else []
            res = handle_delete_verb("deletion call", targets, cwd)
            if res:
                return res
            break
    return None


def evaluate_write_edit(event: Dict[str, Any]) -> Optional[str]:
    """Rule 11: Write/Edit tool calls are refused only when file_path is a protected settings file."""
    tool_input = event.get("tool_input") or {}
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not file_path:
        return None

    if not settings_guarded():
        return None
    cwd = event.get("cwd") or os.getcwd()
    kind = classify_settings_path(file_path, cwd)
    if kind == "claude":
        return "PRESERVE: modifications to ~/.claude/settings.json are prohibited."
    elif kind == "codex":
        return "PRESERVE: modifications to ~/.codex/config.toml are prohibited."
    elif kind == "instructions":
        return "PRESERVE: delegates may not modify the owner's instruction or memory files."
    return None


def evaluate_apply_patch(event: Dict[str, Any]) -> Optional[str]:
    """Codex apply_patch tool evaluation."""
    tool_input = event.get("tool_input") or {}
    cmd = tool_input.get("command") or ""
    if not isinstance(cmd, str) or "*** Begin Patch" not in cmd:
        _hooklib.log_error("preserve_guard")
        return None

    cwd = event.get("cwd") or os.getcwd()

    add_update_move_paths: List[str] = []
    delete_paths: List[str] = []

    for raw_line in cmd.splitlines():
        line = raw_line.strip()
        if line.startswith("*** Add File:"):
            p = line[len("*** Add File:"):].strip()
            if p:
                add_update_move_paths.append(p)
                record_created(p, cwd)
        elif line.startswith("*** Update File:"):
            p = line[len("*** Update File:"):].strip()
            if p:
                add_update_move_paths.append(p)
        elif line.startswith("*** Move to:"):
            p = line[len("*** Move to:"):].strip()
            if p:
                add_update_move_paths.append(p)
        elif line.startswith("*** Delete File:"):
            p = line[len("*** Delete File:"):].strip()
            if p:
                delete_paths.append(p)

    # Rule 2: apply_patch whose Add/Update/Move-to path is a protected settings file
    for p in (add_update_move_paths if settings_guarded() else []):
        kind = classify_settings_path(p, cwd)
        if kind == "claude":
            return "PRESERVE: modifications to ~/.claude settings files are prohibited."
        elif kind == "codex":
            return "PRESERVE: modifications to ~/.codex/config.toml are prohibited."
        elif kind == "instructions":
            return "PRESERVE: delegates may not modify the owner's instruction or memory files."

    # Rule 1: apply_patch with *** Delete File: P runs P through handle_delete_verb
    if delete_paths:
        res = handle_delete_verb("apply_patch delete", delete_paths, cwd)
        if res:
            return res

    return None


def evaluate_bash(event: Dict[str, Any]) -> Optional[str]:
    """Rules 1-10 on Bash tool calls."""
    tool_input = event.get("tool_input") or {}
    cmd = tool_input.get("command") or ""
    if not cmd.strip():
        return None

    cwd = event.get("cwd") or os.getcwd()

    for module in _RULE_MODULES:
        try:
            reason = module.check_bash(cmd, cwd, event)
        except Exception:
            reason = None
        if reason:
            return reason

    # Rule 10: Parse failures use a conservative raw-string scan.
    try:
        clean_cmd = strip_prose_heredocs(cmd)
        clean_cmd, heredocs = extract_heredocs(clean_cmd)
    except Exception as exc:
        _hooklib.log_line("preserve_guard", "parse-fallback: " + str(exc)[:120])
        return fallback_scan(cmd)
    try:
        lexer = shlex.shlex(clean_cmd, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.wordchars += "$~+*.:"
        tokens = [part for tok in lexer
                  for part in (list(tok) if re.fullmatch(r"[();]+", tok) else [tok])]
    except Exception as exc:
        _hooklib.log_line("preserve_guard", "parse-fallback: " + str(exc)[:120])
        return fallback_scan(clean_cmd)

    # Rule 4: Check shell writes to ~/.claude/settings.json or ~/.claude/settings.local.json or ~/.codex/config.toml
    guard_settings = settings_guarded()
    for i, tok in enumerate(tokens if guard_settings else []):
        if tok in (">", ">>") and i + 1 < len(tokens):
            target_tok = tokens[i + 1]
            kind = classify_settings_path(target_tok, cwd)
            if kind == "claude":
                return "PRESERVE: writing to ~/.claude settings files from shell is prohibited."
            elif kind == "codex":
                return "PRESERVE: writing to ~/.codex/config.toml from shell is prohibited."
            elif kind == "instructions":
                return "PRESERVE: delegates may not modify the owner's instruction or memory files."

    if guard_settings and SETTINGS_WRITE_RE.search(clean_cmd):
        return "PRESERVE: writing to ~/.claude settings files from shell is prohibited."
    if guard_settings and CODEX_SETTINGS_WRITE_RE.search(clean_cmd):
        return "PRESERVE: writing to ~/.codex/config.toml from shell is prohibited."

    # Group tokens into command segments
    segments: List[List[str]] = []
    links: List[str] = []
    curr: List[str] = []
    for tok in tokens:
        if tok in SEPARATORS:
            if curr:
                segments.append(curr)
                links.append(tok)
                curr = []
        else:
            curr.append(tok)
    if curr:
        segments.append(curr)
        links.append("")

    # Process each segment
    assignments: Dict[str, str] = {}
    cwd_known = True
    for seg_index, seg in enumerate(segments):
        if not seg:
            continue
        # Compound-command keywords start a segment without being its command.
        while seg and seg[0] in SHELL_KEYWORDS:
            seg = seg[1:]
        if not seg:
            continue
        assignment = re.fullmatch(r"([A-Za-z_]\w*)=(.*)", seg[0]) if len(seg) == 1 else None
        if assignment:
            name, value = assignment.groups()
            assignments[name] = value if literal_path(value) else "/$UNRESOLVED/" + value.lstrip("/")
            continue
        if seg[0] == "cd" and len(seg) == 2:
            cwd_known = literal_path(seg[1]) and (cwd_known or os.path.isabs(seg[1]))
            if cwd_known:
                cwd = os.path.abspath(os.path.join(cwd, os.path.expanduser(seg[1])))
            continue

        # Rule 4 check on commands like tee, cp, mv, sed
        first_tok = seg[0] if guard_settings else ""
        if first_tok == "tee":
            for a in seg[1:]:
                if not a.startswith("-"):
                    kind = classify_settings_path(a, cwd)
                    if kind == "claude":
                        return "PRESERVE: writing to ~/.claude settings files from shell is prohibited."
                    elif kind == "codex":
                        return "PRESERVE: writing to ~/.codex/config.toml from shell is prohibited."
                    elif kind == "instructions":
                        return "PRESERVE: delegates may not modify the owner's instruction or memory files."
        elif first_tok in ("cp", "copy", "mv", "move"):
            dest_candidates = [a for a in seg[1:] if not a.startswith("-")]
            if dest_candidates:
                kind = classify_settings_path(dest_candidates[-1], cwd)
                if kind == "claude":
                    return "PRESERVE: writing to ~/.claude settings files from shell is prohibited."
                elif kind == "codex":
                    return "PRESERVE: writing to ~/.codex/config.toml from shell is prohibited."
                elif kind == "instructions":
                    return "PRESERVE: delegates may not modify the owner's instruction or memory files."
        elif first_tok == "sed" and any(a.startswith("-i") for a in seg):
            for a in seg[1:]:
                if not a.startswith("-"):
                    kind = classify_settings_path(a, cwd)
                    if kind == "claude":
                        return "PRESERVE: writing to ~/.claude settings files from shell is prohibited."
                    elif kind == "codex":
                        return "PRESERVE: writing to ~/.codex/config.toml from shell is prohibited."
                    elif kind == "instructions":
                        return "PRESERVE: delegates may not modify the owner's instruction or memory files."

        # Find command name (skip leading env assignments like VAR=1)
        idx = 0
        while idx < len(seg) and "=" in seg[idx] and not seg[idx].startswith("-"):
            idx += 1
        # Wrappers run the command that follows them: `sudo -u x rm`, `env A=1 rm`.
        while idx < len(seg) and os.path.basename(seg[idx]) in COMMAND_WRAPPERS:
            wrapper = os.path.basename(seg[idx])
            idx += 1
            while idx < len(seg) and (seg[idx].startswith("-") or (wrapper == "env" and "=" in seg[idx])):
                takes_value = seg[idx] in COMMAND_WRAPPERS[wrapper]
                idx += 2 if takes_value else 1
        if idx >= len(seg):
            continue
        cmd_name = seg[idx]
        seg_args = seg[idx + 1:]
        record_bash_created(cmd_name, seg_args, cwd, cwd_known)

        # Handle git commands
        if cmd_name == "git":
            # Rule 3: ALWAYS refuse --no-verify on any git command
            if "--no-verify" in seg_args:
                return "PRESERVE: always: --no-verify on git commands is prohibited."

            # Find git subcommand
            g_idx = 0
            git_cwd = cwd
            while g_idx < len(seg_args):
                if seg_args[g_idx] == "-C" and g_idx + 1 < len(seg_args):
                    c_dir = os.path.expanduser(os.path.expandvars(seg_args[g_idx + 1].strip("'\"`")))
                    git_cwd = os.path.abspath(os.path.join(git_cwd, c_dir))
                    g_idx += 2
                    continue
                if seg_args[g_idx] in ("-C", "--git-dir", "--work-tree"):
                    g_idx += 2
                    continue
                if seg_args[g_idx].startswith("-"):
                    g_idx += 1
                    continue
                break
            subcmd = seg_args[g_idx] if g_idx < len(seg_args) else ""
            subargs = seg_args[g_idx + 1:] if g_idx < len(seg_args) else []
            following = segments[seg_index + 1] if links[seg_index] == "|" and seg_index + 1 < len(segments) else []
            res = check_restore_redirect(subcmd, subargs, following, cwd, cwd_known, assignments)
            if res:
                return res
            subargs = substitute_targets(without_redirections(subargs), assignments, cwd_known)

            # Rule 3: ALWAYS refuse force push
            if subcmd == "push":
                if re.search(r"\bgit\s+push\b[^|;&\n]*\s(--force(-with-lease|-if-includes)?\b|-f\b|\+[a-zA-Z0-9_:/]+)", clean_cmd):
                    return "PRESERVE: always: force push is prohibited."
                for k, a in enumerate(subargs):
                    if a in ("-f", "--force") or a.startswith("--force-with-lease") or \
                       a.startswith("--force-if-includes") or a.startswith("+") or \
                       (a == "+" and k + 1 < len(subargs)):
                        return "PRESERVE: always: force push is prohibited."

            # Rule 7: Orchestrator cleanup allowed anywhere
            if subcmd == "worktree" and subargs and subargs[0] == "remove":
                wt_paths = [a for a in subargs[1:] if not a.startswith("-")]
                if wt_paths:
                    wt_target = wt_paths[0]
                    expanded = os.path.expanduser(os.path.expandvars(wt_target))
                    abs_wt = os.path.abspath(os.path.join(git_cwd, expanded)) if not os.path.isabs(expanded) else os.path.abspath(expanded)
                    if inside_a_worktree(abs_wt):
                        continue  # Allowed by Rule 7
            if subcmd == "worktree" and subargs and subargs[0] == "prune":
                continue  # Only drops records of worktrees whose folders are gone

            is_forced_branch = subcmd == "branch" and (
                "-D" in subargs
                or (
                    any(a in ("-d", "--delete") for a in subargs)
                    and any(a in ("-f", "--force") for a in subargs)
                )
            )

            if is_forced_branch:
                branches = branch_operands(subargs)
                if branches and all(b.startswith("delegate/") for b in branches):
                    continue  # Allowed by Rule 7
                # Rule 8 for branches: one live grant per named branch, spent together
                if check_and_consume_branch_grants(git_cwd, branches):
                    continue

            # Rule 2: Git verbs that discard work or rewrite history
            is_rule_2 = False
            rule_2_verb = ""
            rule_2_paths: List[str] = []

            if subcmd == "reset" and any(a in ("--hard", "--merge") for a in subargs):
                is_rule_2 = True
                rule_2_verb = "reset --hard"
            elif subcmd == "checkout":
                if "--" in subargs:
                    is_rule_2 = True
                    rule_2_verb = "checkout -- <path>"
                    dash_idx = subargs.index("--")
                    rule_2_paths = subargs[dash_idx + 1:]
                elif "." in subargs or any(a in ("-f", "--force") for a in subargs):
                    is_rule_2 = True
                    rule_2_verb = "checkout ."
                    rule_2_paths = ["."]
            elif subcmd == "restore":
                if "--staged" not in subargs:
                    is_rule_2 = True
                    rule_2_verb = "restore"
                    rule_2_paths = [a for a in subargs if not a.startswith("-")]
            elif subcmd == "clean" and any(a.startswith("-f") or a == "--force" for a in subargs):
                is_rule_2 = True
                rule_2_verb = "clean -f*"
                rule_2_paths = [a for a in subargs if not a.startswith("-")]
            elif subcmd == "stash" and any(a in ("drop", "clear") for a in subargs):
                is_rule_2 = True
                rule_2_verb = "stash drop|clear"
            elif is_forced_branch:
                is_rule_2 = True
                rule_2_verb = "branch -D"
            elif subcmd == "rebase":
                is_rule_2 = True
                rule_2_verb = "rebase"
            elif subcmd == "commit" and "--amend" in subargs:
                is_rule_2 = True
                rule_2_verb = "commit --amend"
            elif subcmd in ("filter-branch", "filter-repo"):
                is_rule_2 = True
                rule_2_verb = "filter-branch"

            if is_rule_2:
                # Rule 5: Worktree relaxation for Rule 2
                if inside_a_worktree(git_cwd):
                    all_in_wt = True
                    for p in rule_2_paths:
                        expanded = os.path.expanduser(os.path.expandvars(p))
                        abs_p = os.path.abspath(os.path.join(git_cwd, expanded)) if not os.path.isabs(expanded) else os.path.abspath(expanded)
                        if not _hooklib.in_delegate_worktree(abs_p):
                            all_in_wt = False
                            break
                    if all_in_wt:
                        continue  # Waived by Rule 5
                if rule_2_verb == "branch -D":
                    names = " ".join(branch_operands(subargs)) or "<branch>"
                    return ("PRESERVE: git verb 'branch -D' that discards work or rewrites history is prohibited. "
                            "Ask the user first; only with their explicit OK in chat, grant ONE forced delete per branch "
                            f"(10 min, single use): ~/.ai-orch/bin/delete-grant --branch <name> --repo {repo_identity(git_cwd)} "
                            f"(branches named: {names})")
                return f"PRESERVE: git verb '{rule_2_verb}' that discards work or rewrites history is prohibited."

            # Check if git command is a delete verb (e.g. git rm)
            if subcmd == "rm":
                targets = [a for a in subargs if not a.startswith("-")]
                res = handle_delete_verb("git rm", targets, git_cwd)
                if res:
                    return res
                continue

            if subcmd == "worktree" and subargs and subargs[0] in ("remove", "prune"):
                targets = [a for a in subargs[1:] if not a.startswith("-")]
                res = handle_delete_verb("git worktree remove", targets, git_cwd)
                if res:
                    return res
                continue

            if subcmd == "tag" and any(a in ("-d", "--delete") for a in subargs):
                targets = [a for a in subargs if not a.startswith("-")]
                res = handle_delete_verb("git tag -d", targets, git_cwd)
                if res:
                    return res
                continue

            if subcmd == "reflog" and any(a in ("expire", "delete") for a in subargs):
                targets = []
                res = handle_delete_verb("git reflog expire", targets, cwd)
                if res:
                    return res
                continue

            if subcmd == "push" and any(a == "--delete" or (a.startswith(":") and len(a) > 1) for a in subargs):
                targets = [a for a in subargs if not a.startswith("-")]
                res = handle_delete_verb("git push --delete", targets, cwd)
                if res:
                    return res
                continue

        # Check non-git shell delete verbs
        seg_args = substitute_targets(without_redirections(seg_args), assignments, cwd_known)
        if cmd_name in ("rm", "rmdir", "unlink", "shred", "del", "erase", "rd",
                        "remove-item", "ri", "clear-content", "clc", "truncate"):
            # Skip options; paths in Unix can start with / so only skip single-letter /x flags or -flags
            targets = [
                a for a in seg_args
                if not a.startswith("-") and not re.match(r"^/[a-zA-Z](?::.*)?$", a)
            ]
            if cmd_name == "rm" and not targets and any(a in ("--help", "-h", "--version", "-v") for a in seg_args):
                continue
            res = handle_delete_verb(cmd_name, targets, cwd)
            if res:
                return res
            continue

        if cmd_name == "find":
            if "-delete" in seg_args:
                targets = [a for a in seg_args[:seg_args.index("-delete")] if not a.startswith("-")]
                if not targets:
                    targets = ["."]
                res = handle_delete_verb("find -delete", targets, cwd)
                if res:
                    return res
                continue
            if "-exec" in seg_args:
                exec_idx = seg_args.index("-exec")
                if exec_idx + 1 < len(seg_args) and seg_args[exec_idx + 1] in ("rm", "rmdir", "unlink", "shred", "del"):
                    targets = [a for a in seg_args[:exec_idx] if not a.startswith("-")]
                    if not targets:
                        targets = ["."]
                    res = handle_delete_verb("find -exec rm", targets, cwd)
                    if res:
                        return res
                    continue

        if cmd_name == "xargs":
            if any(a in ("rm", "rmdir", "unlink", "shred", "del", "rd", "remove-item") for a in seg_args):
                res = handle_delete_verb("xargs rm", [], cwd)
                if res:
                    return res
                continue

        if cmd_name in ("mv", "move", "move-item"):
            non_flags = [a for a in seg_args if not a.startswith("-")]
            if non_flags and non_flags[-1].lower() in ("/dev/null", "nul"):
                res = handle_delete_verb("mv /dev/null", non_flags[:-1], cwd)
                if res:
                    return res
                continue

        if cmd_name == "rsync" and any(a.startswith("--delete") for a in seg_args):
            non_flags = [a for a in seg_args if not a.startswith("-")]
            targets = [non_flags[-1]] if non_flags else []
            res = handle_delete_verb("rsync --delete", targets, cwd)
            if res:
                return res
            continue

        if cmd_name == "robocopy" and any(a.lower() in ("/mir", "/purge") for a in seg_args):
            non_flags = [a for a in seg_args if not a.startswith("/") and not a.startswith("-")]
            targets = [non_flags[1]] if len(non_flags) > 1 else []
            res = handle_delete_verb("robocopy /mir", targets, cwd)
            if res:
                return res
            continue

    # Code deletion patterns in command string (e.g. python -c "os.remove(...)")
    res = check_code_deletions(clean_cmd, cwd)
    if res:
        return res

    for consumer, body, quoted in heredocs:
        if consumer == "sh":
            res = evaluate_bash({"tool_input": {"command": body}, "cwd": cwd})
        else:
            res = check_code_deletions(body, cwd, body=True)
        if res:
            return res
        if not quoted and ("$(" in body or "`" in body):
            res = fallback_scan(body)
            if res:
                return res

    return None


def handle_delete_verb(verb: str, targets: List[str], cwd: str) -> Optional[str]:
    """Check Rule 9 (never-delete), Rule 5 (worktree), Rule 6 (temp), Rule 8 (grant), or deny."""
    # Rule 9: Never-delete list check
    for t in targets:
        nv = check_never_delete(t, cwd)
        if nv:
            return f"PRESERVE: target '{t}' is on the never-delete list ({nv})."

    # Only a readable ledger that covers every target allows; an error refuses.
    if consume_created(targets, cwd) is True:
        return None

    # Rule 5: Worktree relaxation. Judged by where the targets are, not by
    # the session's cwd, so an orchestrator in the source repo can clean up
    # delegate worktrees it owns.
    if targets and all(inside_a_worktree(t, cwd) for t in targets):
        return None  # Waived by Rule 5
    if not targets and _hooklib.in_delegate_worktree(cwd):
        return None  # No explicit targets, cwd in worktree

    # Rule 6: Temp relaxation
    if targets and all(is_temp_path(t, cwd) for t in targets):
        return None  # Waived by Rule 6

    # Rule 8: Grant check (exactly one path covered by live grant)
    if len(targets) == 1:
        if check_and_consume_grant(targets[0], cwd):
            return None  # Allowed and grant consumed

    # Refusal under Rule 1
    if len(targets) == 1:
        raw = os.path.expanduser(os.path.expandvars(targets[0].strip().strip("'\"`")))
        target_hint = shlex.quote(os.path.abspath(os.path.join(cwd or os.getcwd(), raw)))
    else:
        target_hint = "<one absolute path per grant>"
    return (
        f"PRESERVE: shell delete verb '{verb}' is refused. "
        "Ask the user first; only with their explicit OK in chat, grant ONE deletion "
        f"(10 min, single use): ~/.ai-orch/bin/delete-grant {target_hint}"
    )


def main(event: Dict[str, Any]) -> None:
    global _CURRENT_SESSION, _PENDING_CREATED
    _CURRENT_SESSION = _hooklib.session_key(event)
    _PENDING_CREATED = []
    tool = event.get("tool_name")
    if tool not in ("Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch"):
        return

    reason: Optional[str] = None
    if tool == "Bash":
        reason = evaluate_bash(event)
    elif tool == "apply_patch":
        reason = evaluate_apply_patch(event)
    else:
        reason = evaluate_write_edit(event)
        if tool == "Write":
            record_created((event.get("tool_input") or {}).get("file_path") or "",
                           event.get("cwd") or os.getcwd())

    if reason:
        log_refusal(event, reason)
        _hooklib.deny(reason)
    elif _PENDING_CREATED:
        paths = created_ledger()
        if paths is not None:
            save_created([p for p in paths if p not in _PENDING_CREATED] + _PENDING_CREATED)


if __name__ == "__main__":
    _hooklib.run(main, "preserve_guard")
