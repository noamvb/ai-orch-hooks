# ai-orch-hooks

Safety hooks for Claude Code cloud sessions, published read-only from a
private orchestration repo. Synced automatically; do not edit here.

What they do on a cloud VM:

- `preserve_guard`: refuses deletes, work-discarding git commands (reset
  --hard, checkout --, restore by redirect), force pushes and `--no-verify`;
  a session may delete files it created itself.
- `fanout_guard`: refuses sub-agent spawn bursts and floods.
- `time_budget`: prints elapsed time after each tool call.
- `stop_tick`: context-remaining advisory.

The owner's working rules, `owner-rules.md`, are copied into
`~/.claude/CLAUDE.md` between marker comments (other content is kept).

`install-android.py` then installs the Android SDK (platforms 36 and 36.1,
build-tools 36, platform-tools) under `/opt/android-sdk` and Flutter under
`/opt/flutter`, and sets `ANDROID_HOME`; the NDK and CMake download on a
project's first build. It fails open and keeps the setup script under the
environment cache's five-minute limit; `AIORCH_CLOUD_ANDROID=0` skips it.

And one skill, linked into `~/.claude/skills`:

- `deck-bridge`: hand a bounded implementation, research or review task to
  the owner's home machine (codex or agy under its own logins and checks) and
  fetch the patch or report as a `bridge/<task_id>` branch. See `skills/deck-bridge/SKILL.md`.

## Use

In a Claude Code cloud environment's setup script:

    #!/bin/bash
    git clone -q --depth 1 https://github.com/noamvb/ai-orch-hooks.git "$HOME/.ai-orch-hooks" \
      || git -C "$HOME/.ai-orch-hooks" pull -q --ff-only
    python3 "$HOME/.ai-orch-hooks/install-cloud.py"

## License

Parts are adapted from rootstock-os (https://github.com/Mazhron/rootstock-os,
commit 06c1a08), MIT License, Copyright (c) 2026 Travis Rhoda; each adapted
file names its source. See LICENSE.
