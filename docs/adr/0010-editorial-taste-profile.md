---
status: accepted
---

# Editorial taste profile: readable rules, not learned weights

Per-book feedback (ADR 0009) remembers decisions about individual candidates, but the editor's generalizable feedback — "don't recommend this author," "more books like that" — has no memory slot. For AutoScout to "get smarter with use," that judgment must persist across weeks and sessions, and it must do so without breaking the pipeline's core properties: deterministic, explainable, stdlib-only, operated by a non-developer through an agent.

## Decision

Standing editorial judgment lives in an **Editorial Taste Profile**: a human-readable state file (`editorial-profile.json`, alongside `candidate-history.json`, canonical in the private git remote per ADR 0007) of explicit rules, applied deterministically on every run.

- **Hard Rules** ("never X" — authors, topics) **hide but count**: matching candidates are removed from the editor list, but every run reports how many titles its rules hid, the hidden items land in a run artifact naming the responsible rule, and the agent can list and revert them on request. Nothing is silently or permanently deleted.
- **Soft Preferences** ("+ narrative science", "− celebrity memoirs") nudge a matching candidate's priority label by **exactly one step**, with the reason shown in the editor-facing row. Preferences never hide anything and never override existing hard floors (clear visible risk still forces D).
- Every entry stores the **editor's verbatim words**, its creation date, and a revocable status — the profile reads as a transcript of things she actually said.
- **Derived statistics** (acceptance/reject rates by topic, family, source — computed from candidate history) are surfaced only as *suggestions* the editor may promote into rules. Nothing statistical changes ranking on its own.
- The **agent is the translator** (extending ADR 0009's confirm-before-record): it classifies chat feedback as per-book (`record-feedback`, unchanged) or generalizable (profile entry), asks at most one clarifying scope question, and echoes back exactly what it recorded and what it will do.

## Considered Options

- **Explicit readable rules with fixed, visible effects.** **Chosen** — deterministic, auditable ("why is this hidden?" → quote the rule), editable by the editor through the agent or by hand, consistent with the qualitative Signal Strength philosophy (no fake-precise scores).
- **Stats-driven reweighting** (learn from accept/reject history, adjust ranking automatically). Rejected as the primary mechanism: drifts, can't be explained row-by-row, and a mis-learned weight is invisible until trust is already damaged. Kept only in demoted form as the suggestions layer.
- **Hard rules demote instead of hide** (rule matches stay on the list at priority D). Rejected by the editor: keeps the list noisy and makes rules feel ignored. Hide-but-count gives the same reversibility with a cleaner list.
- **No profile; re-state preferences each session.** Rejected: this is exactly the "system never gets smarter" problem.

## Consequences

- Profile application is a pure function of (candidates, profile), inserted after signal aggregation and before final labeling; the byte-identical determinism guarantee is preserved and CI-checkable.
- Hidden candidates still enter candidate history, so revoking a rule resurfaces them without special machinery (the existing Material Change logic is untouched).
- Since #1025 the cumulative review is judged against the active Hard Rules at every render, so a rule also hides the matching cards already in candidate history, and a new, changed or revoked rule takes effect at the next render. The render writes no hide marks into candidate history; it lists the withheld cards in `hidden-from-review.json`.
- The failure mode shifts from "opaque ranking drift" to "over-broad rule" — mitigated by hide-counts in every review summary, verbatim quotes in every entry, and one-command revocation.
- The agent gains a small new command surface (`editorial-profile add` / `revoke` / `list`) that is two-phase by default, like `record-feedback`: preview with zero mutation (return code 3), then apply only after explicit editor confirmation with `--confirmed-by-editor`. No new runtime dependencies.
- New glossary terms are required (Editorial Taste Profile, Hard Rule, Soft Preference) — added to GLOSSARY.md.
- Requirements are elaborated in `reqs/07-learning-loop-and-source-lifecycle.en.md`.
