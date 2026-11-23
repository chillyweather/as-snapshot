# Operating model: editor-run coding agent over a git repo

After Dima's development period ends, AutoScout is operated by the **editor plus a coding agent** (e.g. Claude Code / Codex / Gemini desktop), not by a developer and not by a hosted service. The editor runs the agent locally; the agent acts as both the user interface and the orchestrator that drives the existing stdlib CLI. Canonical cross-week state (candidate history, feedback, suppression) lives in a **private remote git repo**, which the agent pulls before a run and pushes after. This keeps the zero-dependency, file-based design intact, requires no hosting or maintenance, and gives a remote that doubles as backup and as Dima's window into the exact state when help is needed. *(Narrowed by ADR 0023: canonical state becomes a database file behind the State Store, and Git keeps a deterministic JSON export as the remote record, with Candidate History sharded one file per candidate.)*

## Considered Options

- **Nanode hub (scheduled).** A small server holds state and runs the pipeline on cron. Rejected as the *primary* path because it is a hosted service with no developer to maintain it; a broken unattended server is worse than a missed manual run. Kept as a **secondary/fallback** path. *(Narrowed by ADR 0016: the secondary path is closed. The Nanode hosts the grounded search adapter and nothing else.)*
- **Stateful web app.** Rejected for the same maintenance reason and because it conflicts with the no-server, file-based model.

## Consequences

- The weekly run only happens when the editor (with the agent) initiates it; there is no autonomous scheduling on the main path. *(Narrowed by ADR 0021: since 2026-11-10 the weekly scan also runs from a timer on a home server that the operator maintains.)*
- Resilience matters more than features: data/source failures must degrade gracefully (replay mode, skipped-source logging) rather than block a run, because the first responder is a non-developer.
- The escape hatch is **async**: on a failure the agent can't self-resolve, it emits a diagnostic and Dima pulls the remote state, fixes, and pushes — no physical presence required.
- A blessed, constrained set of agent commands/skills is needed so the editor's agent has a safe on-rails workflow rather than free-roaming the repo.
