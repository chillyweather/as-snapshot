# Default implementation workflow

This is a vendor-specific development instruction for Claude Code running in T3 Code.
It applies when the operator asks to implement an issue or another code change, including "using the default implementation workflow", "our workflow" or "our latest workflow".
It does not apply to editor workflows (weekly scan, feedback, doctor, source onboarding), which keep their own contracts in `AGENTS.md` and `docs/agents/contracts/`.
`CODING_STANDARDS.md` and `AGENTS.md` apply throughout.
When Codex is invoked as the reviewer in step 6, it only reviews and must not run this workflow or edit files.

## Roles

| Role | Model | How it runs |
|---|---|---|
| Lead: planning, investigation, briefs, verification, Git, PR, merge | Fable 5.2 | the main Claude Code thread |
| Implementer: all code and tests, including review fixes | Opus 5.5, `claude-opus-5-5` | `delegate_task` to the Claude provider |
| Live Codex extraction, when a criterion needs it | the model and effort in `configs/codex-model-pin.json` | the project's pinned extraction path |
| Reviewer, rounds 1 to 3 | Codex `gpt-6.2-sol`, effort `high` | `codex review --base main -c model="gpt-6.2-sol" -c model_reasoning_effort="high"` from the worktree |
| Reviewer, rounds after 3 | Fable 5.2 | the lead reads the whole diff in the main thread |

The lead writes no code, tests or docs changes itself, including one-line review fixes: every change goes through the implementer.

## Steps

1. **Investigate (lead, read-only).**
   Read the issue, its acceptance criteria, linked issues, and the relevant code, tests, docs and kept run artifacts.
   Done when a bug has a confirmed root cause, or a feature has a settled design, and every acceptance criterion maps to a planned change or check.

2. **Plan and wait for go.**
   Present the plan to the operator: root cause or design, files to change, tests to add, and how each acceptance criterion will be verified.
   Start step 3 only after the operator says "go".
   From then on the whole sequence is authorized, including the merge; stop early only for the cases under "When to stop and ask".

3. **Brief and implement.**
   The lead writes a self-contained brief: goal, root cause or design, files to change, tests to add, acceptance criteria, the `CODING_STANDARDS.md` rules including the #658 product-rule test policy, and "make `./scripts/check` pass".
   The implementer works in a new isolated worktree (`delegate_task` with `isolation: "worktree"`) cut from `origin/main`, on branch `task/<issue>-<short-slug>`, with its own `.venv` (`.venv/bin/python -m pip install -e ".[dev]"`); `./scripts/check` refuses to run against an environment installed from another checkout.
   When `delegate_task` cannot create the worktree, the lead creates it with `git worktree add` from `origin/main` and points the implementer at it.
   Never implement in the main checkout, and never reuse a worktree left by another task.
   A change under `autoscout/state_store/` also passes `./scripts/check --state-backend sqlite` (see "The suite on both State Store backends" in `CODING_STANDARDS.md`).
   Done when the implementer reports `./scripts/check` green.

4. **Verify (lead).**
   Read the whole diff against the brief and every acceptance criterion, and run `./scripts/check` yourself.
   When a criterion needs live behaviour, such as a Codex extraction replay, run it with the pinned model.
   A gap goes back to the implementer as a fix brief; repeat until every criterion is met.

5. **Open the PR (lead).**
   Push the branch and open a PR against `main` with `Closes #<issue>`.

6. **Review loop.**
   Rounds 1 to 3: run the Codex reviewer and show the operator its findings verbatim.
   Always pass the model and the effort explicitly; the operator's global Codex config has different defaults.
   If the model is rejected, run `codex debug models` to read the current catalog and report the mismatch instead of silently choosing another model.
   Verify each finding against the code: real findings go to the implementer as one fix brief; each rejected finding gets a one-line reason.
   After the fixes, run `./scripts/check`, push, and run the reviewer again.
   Done when a round reports nothing new that is real.
   When the third Codex round still has real findings, the lead takes over as reviewer: after each fix it reads the whole diff, reports its own findings to the operator, and sends the real ones to the implementer, until a round finds nothing new that is real.

7. **Merge and clean up (lead).**
   The gate is green checks on the PR, or a green local `./scripts/check` while GitHub CI is disabled.
   When a check fails, read its log and send the cause to the implementer; never retry blindly and never bypass a check.
   If `main` moved and the branch conflicts or is out of date, bring it up to date and run `./scripts/check` again.
   Merge with `gh pr merge --merge --delete-branch`, confirm the commit is on `origin/main`, then remove the worktree and the local branch.
   Finish with a short report: what was implemented, which model implemented it, what each reviewer found and how each finding was resolved, and the merged PR link.

## No fallback implementer

When Opus fails mid-task with a limit, quota, rate or availability error, cancel its task with `task_cancel`, then stop and ask.

## When to stop and ask

State what was found, give one recommended next action, and leave the PR open when:

- a product-rule test conflicts with the change (#658);
- a finding needs a product or scope decision rather than a fix;
- the lead's review rounds stop converging: a round finds as many real problems as the one before it;
- something outside the PR breaks: CI, credentials, a broken `main`, or the implementer unavailable;
- the merge is blocked by branch protection or by a conflict that cannot be resolved without changing behaviour.
