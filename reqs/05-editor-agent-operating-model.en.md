# AutoScout PRD — Editor + Agent Operating Model

Status: draft PRD v0.1
Date: 2026-06-13
Source decisions:
- `docs/adr/0007-editor-run-agent-on-git-repo.md`
- `docs/adr/0008-editor-authority-agent-informed-consent.md`
- `docs/adr/0009-results-representation-and-feedback-channel.md`
- Supporting: `docs/adr/0006-agent-extraction-default-collector.md`, `docs/adr/0005-configured-source-discovery-only.md`, `docs/adr/0004-clear-visible-risk-stays-in-csv.md`

This PRD defines how AutoScout is **operated after the development period ends**. It does not change what the pipeline computes (candidate discovery, dedup, Russian-edition checks, risk screening, the weekly report); it defines who runs it, through what surface, and with what authority. It supersedes the earlier assumption (PRD `03`, §3.1 and §10) that Dima stays in the loop every week and that the editor works through a Google Sheet round-trip.

---

## 1. Product summary

After handoff, AutoScout is operated by **the editor plus a coding agent** — not by a developer, and not by a hosted web application. The editor runs a coding agent (e.g. Claude Code / Codex / Gemini desktop) locally; the agent is both the **user interface** and the **orchestrator** that drives AutoScout's existing stdlib CLI. Canonical cross-week state lives in a **private remote git repo**. The editor reviews the weekly list and gives feedback **by talking to the agent**.

The design goal is **survivability without a developer**: the smallest possible operating surface, zero hosting, and no new runtime dependencies, so the weekly cycle keeps working when Dima is only occasionally available.

---

## 2. Problem statement

The MVP feedback loop assumes a developer: Dima launches the run, imports the CSV into Google Sheets, the editor edits the sheet, Dima converts it back to CSV and re-ingests it. That loop has three problems for the post-handoff world:

1. **No permanent developer.** Long-term the operators are the editor and the agent; the manual launch/import/convert steps have no owner.
2. **Feedback friction.** The edited-CSV round-trip is clunky and error-prone, and only 2 of the report's ~24 columns actually carry feedback.
3. **Wrong tool reflex.** "Build a web app / dashboard" was the instinct, but a hosted service with no developer to maintain it is a liability, and the editor already finds conversational chat an acceptable, even preferred, interface.

---

## 3. Target users

### 3.1 Primary user — the editor
Russian publishing editor / acquisition scout. **The decision authority** for AutoScout. Comfortable with conversational AI tools (already uses ChatGPT). Runs a coding agent locally and interacts with AutoScout entirely through it.

### 3.2 The agent
A coding agent acting as UI + orchestrator. Presents candidates, captures feedback, runs the pipeline, and applies the editor's requested changes. Operates with broad autonomy bounded by informed consent (see §6).

### 3.3 Occasional user — Dima (developer)
Implementer and **async escape hatch** for actual breakage. Pulls the exact broken state from the remote repo, fixes, and pushes. **Not** an approver of editorial or configuration decisions; no routine change waits on him.

---

## 4. Product goals

- The weekly cycle is **one agent conversation**: run, review, give feedback — no manual file shuffling, no CSV import/export.
- **No hosted service and zero new runtime dependencies**; AutoScout stays stdlib-only and file-based. *(Since 2026-11-10 the scheduled run uses a home server that Dima maintains — ADR 0021.)*
- Canonical state is **durable, backed up, and remotely inspectable** (private git remote).
- When something breaks, the editor+agent can **recover or hand off async** without Dima being physically present.
- The editor can **shape the system** (sources, config, screening) through the agent, with consequences made explicit before risky changes.

## 5. Non-goals

- A stateful web application or hosted dashboard on the main path (explicitly rejected — ADR 0009).
- Pushing results into Google Sheets or any external service on the main path (rejected — ADR 0009).
- Autonomous scheduled runs on the Nanode or on any server without a maintainer (ADR 0016, #550). *(Narrowed on 2026-11-10: the weekly scan also runs from a timer on a home server that Dima maintains — ADR 0021. Since #1057 that timer runs every night — ADR 0022. An editor-initiated run remains available.)*
- A developer-approval gate on editorial or configuration changes (rejected — ADR 0008).
- Active "search for new sources" automation (deferred; source curation stays a Dima/agent task).
- Multi-user accounts, roles, or permissions (single editor).

---

## 6. Functional requirements

### 6.1 Run + state home (ADR 0007)
- The pipeline can run locally under the editor's agent; such a run is **editor-initiated**.
- Since 2026-11-10 the weekly scan also runs **from a timer on a home server** that Dima maintains (systemd user timers, ADR 0022): **every night at 02:30 Asia/Jerusalem**, with the Kirkus pass at 01:30. It runs the same `./scripts/run-weekly`, commits and pushes state the same way, and is followed by `./scripts/doctor`. The server is the only machine that runs the scan and pushes state, and a manual run is started on the server. A scheduled start that meets a live run in progress skips itself; the run lock is the only coordination, so nothing is paused or resumed by hand. Every scheduled run sends the operator one short morning report by email, whatever its outcome (successful, needs attention, stopped, skipped), including the night's Kirkus pass (decided 2026-11-14).
- Canonical cross-week state (candidate history, feedback, suppression, source proposals) lives in a **private remote git repo**. The agent pulls before a run and commits/pushes after.
- The remote doubles as backup and as Dima's window into the exact state for async support.
- **The Nanode hosts the grounded search adapter and nothing else** (ADR 0016, #537). It is not a runner or a host for the pipeline, and the fallback runner/host option is closed. The adapter holds the search credential, so no operator machine has to. *(Narrowed by ADR 0021: the scheduled run uses an adapter on the home server itself, and the Nanode adapter is the fallback and the laptop path.)*

### 6.2 Results representation (ADR 0009)
- The editor reviews the weekly list **within an agent session**; there is no requirement to view it independently of a session.
- The agent renders a **local view** on demand (generated HTML it opens, or in-chat/canvas rendering).
- `editor-facing.csv` continues to exist and can be opened or downloaded.

### 6.3 Feedback capture (ADR 0009)
- The editor gives feedback **conversationally**; the agent records it via a small **new programmatic feedback command** (e.g. `record-feedback --identity-key … --status … --note …`) that writes into the **existing identity-key state** (`title|authors`) and feeds the same Material Change / candidate-history machinery.
- Editor statuses are unchanged: interested / maybe-watch / reject / already-known / needs-deeper-analysis, plus free-text notes preserved verbatim.
- The agent **confirms the matched candidate before recording** (e.g. "Behave by Robert Sapolsky → Reject, correct?") to avoid mis-keying.
- The manual edited-CSV round-trip (`ingest-feedback` on a hand-edited sheet) is **retired as the editor's normal loop** but **kept as a dormant fallback** for the Dima escape hatch.

### 6.4 Agent authority (ADR 0008)
- The editor is the authority; the agent applies her requests with **broad autonomy** (per-candidate feedback, configuration, source curation, etc.).
- For changes with **risky or non-obvious consequences** (e.g. disabling a risk-screening category per ADR 0004, removing a source), the agent **first explains consequences and offers concrete options**, then acts on her choice (informed consent — not silent action, not a developer gate).
- The agent's **only hard refusal** is an action that would destroy the weekly workflow or break the app entirely.
- The blessed agent command/skill set is **broad**, not a narrow whitelist; the safety mechanism is consequence-explanation plus reversibility via git history.

### 6.5 Resilience + escape hatch (ADR 0007)
- Data/source failures must **degrade gracefully** (replay mode, skipped-/failed-source health counts in `source-runs.json` / run summary) rather than block a run, because the first responder is a non-developer.
- On a failure the agent cannot self-resolve, it **emits a diagnostic** and the editor pings Dima, who fixes async via the remote repo.

### 6.6 Source proposals (unchanged scope)
- The editor may casually propose a source she notices; the agent records it as a Source Proposal for later curation. Active source-search automation remains out of scope (deferred).

---

## 7. Build items implied

1. **`record-feedback` CLI command** — implemented; writes the same state files and identity model as `ingest-feedback`, invoked by the agent from a confirmed conversational decision.
2. **Local results-view rendering** — implemented as `render-review`, which renders `weekly-review.md` from run artifacts; `editor-facing.csv` remains portable.
3. **Blessed agent commands/runbook** — implemented in `docs/current-weekly-operator-workflow.md`: run, render, record feedback, propose/accept/reject sources, inspect health, and hand off diagnostics.
4. **Remote-git operating runbook** — implemented in `docs/current-weekly-operator-workflow.md`: git preflight, pull, run, inspect, commit, push, and Dima async-fix handoff.
5. **Consequence-explanation behavior** — implemented as operator guidance in `docs/current-weekly-operator-workflow.md`, encoding "explain + offer options on risky changes; hard-refuse only workflow/app-breaking actions" from ADR 0008.

---

## 8. Acceptance criteria

- [x] A weekly cycle (run → review → feedback) completes entirely through the agent, with no manual CSV import/export.
- [x] Feedback given conversationally lands in the same identity-key state as the legacy `ingest-feedback` path and survives across weeks (history, Material Change).
- [x] The agent confirms the matched candidate before recording feedback.
- [x] Canonical state round-trips through the private git remote (pull before, push after a run).
- [x] A risky change (e.g. disabling a risk-screening category) triggers the agent to explain consequences and offer options before acting.
- [x] A simulated source failure produces a graceful degraded run (health counts / diagnostic), not a hard stop.
- [x] No new runtime dependency is introduced; AutoScout remains stdlib-only.
- [x] `ingest-feedback` and the CSV round-trip still work as a fallback.

---

## 9. Open items

- The scheduled run uses the grounded search adapter on the home server (ADR 0021); the Nanode adapter remains as the fallback, see `docs/nanode-search-adapter-runbook.md` (ADR 0016, #537). No Nanode work is outstanding.
- The model has only ever been operated from Dima's machines: his laptop, and since 2026-11-10 the home server. Provisioning the editor's own machine is #630, and it is the last item between this PRD and its own premise.
