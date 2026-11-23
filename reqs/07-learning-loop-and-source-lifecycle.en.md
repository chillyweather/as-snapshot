# AutoScout PRD — Learning Loop & Source Lifecycle

Status: draft PRD v0.1
Date: 2026-07-06
Source decisions:
- `docs/adr/0010-editorial-taste-profile.md`
- Supporting: `docs/adr/0005-configured-source-discovery-only.md`, `docs/adr/0007-editor-run-agent-on-git-repo.md`, `docs/adr/0008-editor-authority-agent-informed-consent.md`, `docs/adr/0009-results-representation-and-feedback-channel.md`

This PRD defines how AutoScout **gets smarter with use**. Today the editor can give feedback on individual books (PRD `05`), but there is nowhere for generalizable feedback ("don't recommend this author", "more books like that") to live; each weekly report is a fresh snapshot instead of an update; and sources can be added but never retired based on evidence. This PRD adds those three loops without changing the operating model: the editor still does everything by talking to the agent, and the pipeline stays deterministic, stdlib-only, and explainable.

---

## 1. Product summary

Four additions, one theme — the system accumulates the editor's judgment:

1. **Editorial Taste Profile** — a human-readable state file that stores the editor's standing rules (hard) and preferences (soft), applied deterministically on every run. The agent translates natural chat feedback into profile entries and per-book statuses.
2. **Update digest ("new on top")** — every run classifies each row as new / upgraded / changed / unchanged against Candidate History, sorts new findings to the top, and opens the review with a "this week" summary.
3. **Source lifecycle** — per-source contribution metrics computed from run artifacts, plus a periodic source-review conversation where the agent proposes retiring dead weight and promoting probation sources.
4. **Non-English expansion in waves** — French, then Spanish, then Italian, then Scandinavian/Dutch, onboarded through the existing Source Proposal flow with a probation period, reusing the German-source pattern.

---

## 2. Problem statement

1. **Feedback doesn't generalize.** `record-feedback` remembers decisions about *this* book. When the editor says "never show me this author again" or "I want more narrative science like this one," the system has no memory slot for it — the agent would have to re-derive her taste from scratch in every session, or the rule silently evaporates.
2. **Every week looks like the first week.** The editor's real question on week N is "what's new and what changed?", not "here are 100 rows again." The diff exists implicitly in Candidate History but is never rendered.
3. **Sources only accumulate.** There is a flow to add sources but no evidence for removing them. Nobody can answer "how many English sources are enough?" — the honest answer is empirical (when a new source stops contributing unique finds), and nothing measures that today.
4. **Discovery space is capped by language.** English nonfiction lists overlap heavily; genuinely new discovery space comes from other languages, and the editor wants it.

---

## 3. Product goals

- Feedback said once in chat **keeps working forever** (or until revoked) — rules survive across weeks, runs, and agent sessions.
- Everything the profile does is **readable and explainable**: the editor can ask "why is this book hidden / demoted / promoted?" and get a plain answer quoting her own rule.
- The weekly review **opens with the delta**: new findings on top, a one-line summary of what changed.
- Source add/remove decisions are made **from measured contribution**, not vibes; the "how many sources" question answers itself.
- All of the above stays inside the existing operating model: chat-only interface, deterministic pipeline, file/JSON state in the private git remote, zero new runtime dependencies.

## 4. Non-goals

- **No machine learning / opaque scoring.** Preferences are explicit rules with visible effects, consistent with the qualitative Signal Strength philosophy. Derived statistics may *suggest* rules but never silently change ranking.
- **No autonomous rule creation.** The agent proposes; only the editor's explicit confirmation creates or changes a profile entry.
- **No automatic source addition or removal.** Metrics inform a conversation; the accept/retire action still goes through the existing Source Proposal / config flow (ADR 0005 stands: configured sources only).
- **No permanent deletion.** Hidden books, retired sources, and revoked rules are all suppressed-with-history, reversible via state files and git.

---

## 5. Functional requirements

### 5.1 Editorial Taste Profile

A new state file, `editorial-profile.json`, living alongside `candidate-history.json` in the state directory (canonical in the private git remote, like all state — ADR 0007). Machine-validated but written to be read by a human: every entry carries the editor's verbatim words and a plain-language reason.

**Layer 1 — Hard Rules.** "Never recommend X": blocked authors, blocked topics/patterns. Each rule stores:
- scope (`author` / `topic` / `publisher`),
- the matching value(s),
- the editor's verbatim quote that created it,
- date created and a note of the conversation it came from,
- status (`active` / `revoked`, with revocation date and reason).

Effect: matching Scouting Candidates are **hidden from the editor list but counted**. Every run reports "N titles hidden by your rules" in the review summary; hidden items go to a run artifact (like the existing exclusions file) with the rule that hid each one. The agent can list them and revert (revoke the rule or add a per-book exception) on request. Hidden candidates still enter Candidate History, so revoking a rule resurfaces them naturally.

**Layer 2 — Soft Preferences.** "+ more narrative science like *Atomic Habits*", "− celebrity memoirs". Each preference stores direction (+/−), a plain-language description of what it matches, optional example books, verbatim quote, date, and status.

Effect: a matching candidate's priority label is **nudged exactly one step** (B→A or B→C), never more, never past the hard floors that already exist (clear visible risk still forces D). The editor-facing row gains a visible explanation: "matches your preference: narrative science". Preference nudges never hide anything.

**Layer 3 — Derived Evidence (computed, never hand-edited).** Acceptance/reject rates by topic, source family, and source, computed from Candidate History. Stored/regenerated as a run artifact, not inside the profile file. Surfaced to the editor periodically as *suggestions*: "you've rejected 9 of 10 business-leadership titles — want a rule?" A suggestion becomes a rule only when the editor says yes; nothing auto-applies.

**Determinism.** Profile application is a pure function of (candidates, profile file): same inputs → byte-identical outputs, preserving the CI determinism guarantee.

### 5.2 Agent-as-translator protocol

Extends the feedback workflow in `docs/operator/review-and-feedback.md` (ADR 0009's confirm-before-record safeguard applies throughout):

1. The editor speaks naturally: "this one isn't suitable", "enough of this author", "more like this, please".
2. The agent classifies the feedback: **(a) per-book** → existing `record-feedback` path, unchanged; **(b) generalizable** → a profile entry.
3. If scope is ambiguous, the agent asks **one** clarifying question ("just this book, or the whole author?"). It does not interrogate.
4. The agent writes the entry and **echoes back exactly what it recorded**, including the effect ("From now on, books by X are hidden. 2 titles on the current list match — hidden. Say the word to undo.").
5. Fact corrections keep the existing proposed-corrections path; they never become profile entries.

### 5.3 Update digest ("new on top")

- Each run diffs its candidates against Candidate History and the previous run state and labels every Weekly Report Row: **new** / **upgraded** (priority label improved or new Independent Signal family) / **changed** (Russian-edition or Visible Risk status changed) / **unchanged**.
- Both `render-review` and `editor-facing.csv` sort: new first, then upgraded, then changed, then unchanged — each group internally by priority label.
- The review opens with a one-line "this week" summary: e.g. "7 new, 3 upgraded, 2 changed, 2 hidden by your rules."
- The diff is derived from state files only (no timestamps), preserving determinism.
- A new run therefore *prepends* discoveries to the editor's mental list instead of reshuffling it — the "new findings at the top" experience, without maintaining a separate cumulative document.

### 5.4 Source lifecycle

**Metrics** (computed per source over a trailing window, e.g. 8 weeks, from existing run artifacts + Candidate History):
- **Unique Contribution Rate** — share of its mentions that no other configured source surfaced.
- **Corroboration Rate** — share of its mentions that raised an existing candidate's Independent Signal count.
- **Editor Yield** — share of candidates it (co-)surfaced that the editor later marked Interested or Needs Deeper Analysis.

**Source review conversation** (periodic, e.g. monthly, agent-initiated as part of the weekly routine): the agent renders the metrics table and proposes, per source: keep / retire (`active: false` with a recorded reason — soft and reversible) / promote from probation. Retirement follows the ADR 0008 informed-consent pattern: consequences explained, editor decides.

**Probation.** A newly accepted source (via the existing Source Proposal flow) is marked as a Probation Source in config notes and judged on the same metrics after N weeks (default 4–6) before it is considered established.

**The "how many English sources" answer.** Guidance, not a hard cap: English adult-nonfiction discovery saturates around **~20–25 sources (~3–4 strong per family)** because the major lists, awards, and aggregators overlap heavily. Beyond that, prefer aggregators (which cheaply raise corroboration) over new raw sources, and spend the expansion budget on non-English waves. The real ceiling is detected empirically: when new sources' Unique Contribution Rate stays near zero for several weeks, discovery is saturated. Note that low-unique-contribution sources are not automatically worthless — corroboration raises signal counts — but they are the first candidates for retirement when the portfolio needs pruning.

### 5.5 Non-English source waves

Wave order (editor-confirmed, all wanted): **French → Spanish → Italian → Scandinavian/Dutch**. Per wave: 3–5 sources spanning families (bestseller/ranking, award, media/curated list), onboarded through the Source Proposal flow with probation, reusing the German pattern (Spiegel Bestseller Sachbuch / Die Zeit Bestenliste / Deutscher Sachbuchpreis). Candidate starting points:

- **FR**: Livres Hebdo/GfK essais bestsellers, Prix Femina Essai / Prix Renaudot Essai, Le Monde des livres.
- **ES**: Casa del Libro no-ficción rankings, Premio Nacional de Ensayo, Babelia (El País).
- **IT**: Classifica saggistica (la Lettura / Corriere), Premio Strega Saggistica, Il Sole 24 Ore Domenica.
- **Scandinavian/NL**: national bestseller lists (Boktugg SE / Bogmarkedet DK / NO), De Bestseller 60 (NL), Brageprisen sakprosa (NO).

These are proposals to research at onboarding time, not pre-accepted sources (ADR 0005). Existing Non-English Candidate helper fields (English title/author/annotation aids, Search Translation, Author Transliteration Note) already cover rendering and Russian-edition checking; no new mechanics needed.

---

## 6. Build items implied

1. **`editorial-profile.json` state schema + loader** in `autoscout/state.py` patterns; profile application step in `autoscout/pipeline.py` after signal aggregation / before final labeling; hidden-by-rule run artifact + review-summary count.
2. **Profile CLI commands** for the agent (e.g. `profile add-rule` / `add-preference` / `revoke` / `list`), mirroring the `record-feedback` / `source-proposal` command style.
3. **Row-delta classification + sorting** in `autoscout/scoring.py` / `reporting.py` / `review_rendering.py`; "this week" summary line in `render-review` and readiness output.
4. **Source metrics report** (new run artifact + a `source-metrics` render for the review conversation), computed from `source-runs.json` history and Candidate History.
5. **Runbook updates** in `docs/current-weekly-operator-workflow.md`: translator protocol, rule echo-back wording, monthly source review, probation handling.
6. **Wave onboarding** — per-wave Source Proposal research and acceptance (operational work, not code).

Items 1–3 are the first implementable slice; 4–5 second; 6 is ongoing operations.

---

## 7. Acceptance criteria

- [ ] "Don't recommend this author" said once in chat hides matching books in all future runs, with the count visible in each review and a one-command undo.
- [ ] "More books like this" said once nudges matching candidates one priority step up, with the reason visible in the editor-facing row.
- [ ] The editor can read `editorial-profile.json` (or ask the agent to render it) and recognize every rule as something she actually said.
- [ ] Two runs with identical inputs and an identical profile produce byte-identical outputs.
- [ ] The weekly review opens with new findings on top and a "this week: N new / M upgraded / K hidden" summary.
- [ ] Rejected-topic statistics are shown as suggestions only; no rule exists that the editor didn't confirm.
- [ ] The agent can render a per-source contribution table and a retire/keep/promote proposal on request.
- [ ] A retired source stays in config as `active: false` with a reason, and can be re-activated.
- [ ] First non-English wave (French) onboarded via Source Proposals with probation status recorded.

---

## 8. Open items

- Exact matching semantics for topic-scoped rules (keyword list vs. category tags) — decide during implementation; start with the simplest thing the profile can explain.
- Trailing-window length for source metrics and probation duration — defaults proposed (8 weeks / 4–6 weeks), tune with real data.
- Whether Derived Evidence suggestions surface weekly or monthly — start monthly alongside the source review to keep the weekly session short.
