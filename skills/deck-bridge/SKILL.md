---
name: deck-bridge
description: Hand a bounded implementation or analysis task from a Claude Code cloud session to the owner's home machine, where a codex or agy delegate does it under the owner's own logins and verification, and get the result back as a git branch. Use when the user asks to "send this to the Deck", "use codex/agy from here", or to delegate work to a cheaper engine from a cloud session. Not for live actions (deploys, releases, pushes to main).
---

# Deck bridge (cloud side)

Cloud sessions cannot run the codex or agy delegates: their CLIs, logins and
verification gate live on the owner's home machine. This skill sends one task
there and fetches the result. The home machine validates everything you send
as untrusted data, runs the delegate in an isolated worktree with the repo's
registered checks, and pushes the outcome to `bridge/<task_id>` on GitHub.

## 1. Push the base

The delegate starts from a commit that must already be on GitHub. Commit your
work, push your branch, and take the full commit id:

    git push -u origin HEAD
    BASE_SHA=$(git rev-parse HEAD)

## 2. Write the envelope

One JSON object, schema `deck-bridge/v1`. Unknown keys are refused.

| key | value |
|---|---|
| `v` | `1` |
| `task_id` | `^[a-z0-9][a-z0-9-]{2,62}$`, unique per task, e.g. `fix-login-20261010a` |
| `repo` | GitHub `owner/name` of this repository |
| `profile` | optional; home-machine profile id, needed only when the bridge refuses with `repo matches several profiles (...)`; copy one id from that refusal, e.g. `election-night-multi` |
| `base_sha` | the 40-character commit id from step 1 |
| `engine` | `"agy"` (mechanical, breadth) or `"codex"` (depth on a few files) |
| `model` | optional; codex: `gpt-6.1-sol`. agy: omit, or an `agy models` name |
| `effort` | optional; codex `low|medium|high|xhigh`; agy: only when `model` is omitted |
| `category` | optional: `implementation` (default), `documentation`, `testing`, `research`, `review`, `design`, `diagnosis`; research/review/design/diagnosis run read-only and return a report |
| `allowed_paths` | 1-50 repo-relative paths or prefixes the delegate may change (`src/`, `tests/x.py`); omit (or []) for those analysis categories |
| `timeout_s` | optional, 60-3600, default 1800 |
| `spec` | the full task text, up to 60000 characters |

The delegate sees only `spec`, never this conversation. Write it like a
contract: files to change, exact behaviour, numbered tests, acceptance
commands, what is out of scope, and "if this would break an existing test,
stop and start your final message with `BLOCKED:`". Never put secrets in it.

## 3. Open the request issue

Open an issue on the task's own repository with the GitHub MCP tool
`issue_write` (method `create`):

- title: exactly `deck-bridge: <task_id>`
- body: the envelope as JSON inside one ```json fence, nothing else needed

The Deck checks the owner's onboarded repositories every minute. Within about
a minute it comments `bridge: accepted task=...` (or `bridge: refused ...`
with the reason) and closes the issue. Only issues the owner's account opened
count. Do not reopen or edit a picked-up issue; a retry is a new issue with a
new `task_id`.

## 4. Wait for the branch

The result arrives as `bridge/<task_id>`. Poll with a deadline (codex/agy runs
take 5-40 minutes):

    for i in $(seq 1 90); do
      git ls-remote --exit-code origin "refs/heads/bridge/$TASK_ID" >/dev/null && break
      sleep 30
    done

No branch after the deadline: read the issue's comment first. A `refused`
comment gives the reason (repo not onboarded there, agents paused, bad
envelope); an issue still open with no comment means the Deck is off or not
polling. Tell
the user; do not retry with the same `task_id`.

## 5. Read the status and result

    git fetch origin "bridge/$TASK_ID"
    git log -1 --format=%B FETCH_HEAD

The tip commit's message is `bridge-status: <task_id>`, a blank line, then a
JSON status: `status` (`passed|failed|refused`), `reason`, `run_id`,
`delegate_exit`, `category`, `patch_commit`, `changed_paths`, `report_commit`,
`report_paths`, `final_message` (the
delegate's own report, which is a claim, not evidence).

- `passed` for implementation/documentation/testing: `git cherry-pick <patch_commit>`, then read the diff yourself and
  run the tests. The repo's registered checks passed on the home machine;
  your own review is still the gate.
- `failed` / `refused`: read `reason` and `final_message`, fix the spec, and
  open a new issue with a NEW `task_id`. Retry at most once; after a second failure
  do the work yourself or ask the user.

For a passed analysis task, read `bridge-report/<task_id>.md` (and `.json`) on branch `bridge/<task_id>`:
`git fetch origin bridge/<task_id> && git show FETCH_HEAD^:bridge-report/<task_id>.md`.
The report is evidence for the cloud session to judge, not a decision.
The status commit is empty; the session must not merge the bridge branch.
The report files are not meant for main.

## Phone adb jobs

To look at or update the owner's phone, open an issue titled
`deck-adb: <job_id>` on a private repo the Deck knows (poop-schedule or
local-llm), body a json fence:

    {"v":1,"job_id":"<job_id>","op":"devices"}
    {"v":1,"job_id":"<job_id>","op":"logcat","package":"com.noamv.poopschedule","lines":300,"level":"W"}
    {"v":1,"job_id":"<job_id>","op":"dumpsys","package":"com.noamv.localllm","service":"meminfo"}
    {"v":1,"job_id":"<job_id>","op":"install","package":"com.noamv.poopschedule","tag":"v1.7.3"}

`package` is `com.noamv.poopschedule` or `com.noamv.localllm` only. `install`
fetches the APK from that app's public releases repo and checks its SHA-256
first; `tag` is optional (default: latest release). Nothing else can run.
Within about a minute the Deck comments the output (or `adb: refused ...` with
the reason) and closes the issue. Read the comment; do not reopen the issue,
open a new one with a new `job_id`.
