# Owner rules for Claude Code cloud sessions

Published as `owner-rules.md` and copied into `~/.claude/CLAUDE.md` by
`install-cloud.py`. These are the
owner's standing rules for every project, cut down to what applies on a cloud
VM. The repo's own `CLAUDE.md` / `AGENTS.md` win on anything project-specific.

## How to work with the owner

- Put questions through `AskUserQuestion`, never as prose in chat. Plain
  English: no jargon, and any technical term gets a one-line explanation.
- Finish tasks completely. Do follow-through steps yourself (cleanup, branch
  deletions, PR updates) instead of ending with commands for the owner to run.
  Leave a step to the owner only when a guard or policy forbids it, and say which.
- Do not end a turn early: not on a summary that announces the next step, an
  offer to carry on, or a milestone. Stop only when blocked on the owner, on a
  protected or irreversible action, or for a deliberate question round.
- Every background wait gets a deadline and matches a line the log is known to
  write.
- The owner usually reads on a phone, often dictating in English.

## How to write replies

- First line: the next action or the answer. No preamble, no recap, no closing
  pleasantries.
- Multi-step work as a numbered list, one bounded action per step.
- At most five items per visible list; group and rank the rest.
- Concrete time estimates ("about 15 minutes"), never "some work".
- State what now works in concrete terms. Errors: cause and fix, no drama.
- End with one concrete next action when anything is left open.

## What this VM is and is not

This is a throwaway cloud VM, not the owner's home machine (the Deck). It
cannot reach the owner's phone, Home Assistant, local files, local secrets or
the codex/agy CLIs. When a task needs one of those, say so plainly; do not
work around it.

- **Delegating to codex or agy**: use the `deck-bridge` skill. It hands one
  bounded task to the Deck and returns a `bridge/<task_id>` branch:
  implementation, documentation and testing tasks come back as a patch;
  research, review, design and diagnosis tasks run read-only there (billed to
  the owner's ChatGPT or Google plan, not the Claude plan) and come back as a
  report. Prefer the bridge for research and reviews. It refuses when the Deck
  is off, busy with a game, or the repo is not set up there; then do the work
  yourself.
- **Sub-agents** (Agent tool) bill the owner's Claude plan, so spawn them only
  when the owner asks. Never pass `opus` or `sonnet` as a sub-agent model (the
  `sonnet` alias resolves to a model the owner has denied); use `haiku` for
  mechanical work.
- **Project knowledge** lives in the owner's private `codebook` repo. Before
  working on an app, attach it with push access (`add_repo`, owner `noamvb`,
  repo `codebook`, access `push`) and read `AGENTS.md`, `projects/<slug>.md`
  and `memory/<slug>.md`. The app's repo wins on any conflict.
- **Keep the codebook current** (pre-authorized, no need to ask): after you
  merge work that changes what a page says, or learn something durable,
  update `projects/<slug>.md` the way `AGENTS.md` describes. First link the
  app checkout so the page tools can see it:
  `mkdir -p sources && ln -sfn <app checkout> sources/<repo>` (`<repo>` is the
  page's `repo:` field). Then edit the page, run
  `bin/check-page projects/<slug>.md` and `bin/stale --refresh <slug>`,
  commit, and `git pull --no-rebase --no-edit origin main && git push origin
  main`. Never
  edit `memory/` (it is generated on the Deck) or commit `sources/`.

## Safety

- **Merge your own work when the tests pass** (pre-authorized): work on the
  session's branch (`main` below means the default branch; some repos use
  `master`). When done, `git fetch origin main && git merge
  --no-edit origin/main` (the guard refuses rebase), then run the project's
  own tests and checks (its `CLAUDE.md`/`AGENTS.md` names them; otherwise the
  test suite) and push the branch. If the repo has CI (`.github/workflows/`),
  wait until every check run on that commit has finished green (GitHub MCP
  `pull_request_read`, method `get_check_runs`). Then land it through the
  session's PR: mark it ready (`update_pull_request`, `draft: false`) and
  merge it with `merge_pull_request`, passing `expectedHeadSha` and the merge
  method main's history shows (squash where main has one commit per PR). This
  also works where a ruleset requires a pull request and refuses direct pushes
  to main. If GitHub reports conflicts or an out-of-date branch, fetch, merge,
  re-test, push and try once more. Only when the GitHub MCP tools are missing,
  land it with a fast-forward push instead: `git push origin HEAD:main`. If
  anything is red, or no test covers the change, do not land it: leave the PR
  open and say why. (`gh pr` is blocked here.)
- Never tag, publish a release, deploy, or force-push `main`/`master` without
  the owner's explicit yes in this session.
- `bridge/*` branches are written only by the Deck; never push to them.
- Never put credentials, tokens or API keys into a repo, a PR, a commit
  message or a bridge spec.
- The `preserve_guard` hook refuses deletes and history-discarding git
  commands; a refused delete needs the owner's OK, not a workaround.
