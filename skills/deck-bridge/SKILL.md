---
name: deck-bridge
description: Hand a bounded implementation task from a Claude Code cloud session to the owner's home machine, where a codex or agy delegate does it under the owner's own logins and verification, and get the result back as a git branch. Use when the user asks to "send this to the Deck", "use codex/agy from here", or to delegate work to a cheaper engine from a cloud session. Not for live actions (deploys, releases, pushes to main).
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
| `category` | optional: `implementation` (default), `documentation`, `testing` |
| `allowed_paths` | 1-50 repo-relative paths or prefixes the delegate may change (`src/`, `tests/x.py`) |
| `timeout_s` | optional, 60-3600, default 1800 |
| `spec` | the full task text, up to 60000 characters |

The delegate sees only `spec`, never this conversation. Write it like a
contract: files to change, exact behaviour, numbered tests, acceptance
commands, what is out of scope, and "if this would break an existing test,
stop and start your final message with `BLOCKED:`". Never put secrets in it.

## 3. Fire the Routine

Find the Routine named `deck-bridge` with `list_triggers` and call
`fire_trigger` on it with `text` set to the envelope as ONE line of compact
JSON (`json.dumps(obj, separators=(",", ":"))`). Nothing else goes in `text`.

## 4. Wait for the branch

The result arrives as `bridge/<task_id>`. Poll with a deadline (codex/agy runs
take 5-40 minutes):

    for i in $(seq 1 90); do
      git ls-remote --exit-code origin "refs/heads/bridge/$TASK_ID" >/dev/null && break
      sleep 30
    done

No branch after the deadline means the home machine refused before it could
push (repo not onboarded there, agents paused, or the machine is off). Tell
the user; do not re-fire with the same `task_id`.

## 5. Read the status and take the patch

    git fetch origin "bridge/$TASK_ID"
    git log -1 --format=%B FETCH_HEAD

The tip commit's message is `bridge-status: <task_id>`, a blank line, then a
JSON status: `status` (`passed|failed|refused`), `reason`, `run_id`,
`delegate_exit`, `patch_commit`, `changed_paths`, `final_message` (the
delegate's own report, which is a claim, not evidence).

- `passed`: `git cherry-pick <patch_commit>`, then read the diff yourself and
  run the tests. The repo's registered checks passed on the home machine;
  your own review is still the gate.
- `failed` / `refused`: read `reason` and `final_message`, fix the spec, and
  fire again with a NEW `task_id`. Retry at most once; after a second failure
  do the work yourself or ask the user.

The status commit is empty; never merge the `bridge/*` branch itself.
