# Next Steps

> **Status note (2026-07):** This note predates ADR 0009. Treat Google-Sheet-centered guidance below as historical unless it is explicitly kept as a fallback. The accepted main path is editor review in an agent session, conversational feedback captured through `record-feedback`, and no persistent Google Sheet as the primary workflow.

Decided 2026-06-12 (grilling session against editor notes in `editor-notes-main-ideas.md`).

## The phase, in order

1. **Close the feedback round trip.** This is the only blocking item — no editor feedback has ever flowed back into the pipeline.
   - ~~Stable candidate ID column in the editor-facing CSV (rightmost; it is the key `apply_editor_feedback` matches on and the CSV currently lacks it).~~ **Resolved:** identity-key state now backs conversational feedback via `record-feedback`; `editor-facing.csv` remains a portable export.
   - ~~`ingest-feedback` command that parses the shared Sheet's CSV export into returned editor rows.~~ **Updated per ADR 0009:** `record-feedback` is the normal editor-chat path; `ingest-feedback` remains as a dormant fallback. **Feedback is sparse by design**: many weeks will have zero comments. A cycle without ingested feedback is a normal, healthy cycle — never a warning.
   - Persistent state: Candidate History read at run start, written at run end.
   - ~~Dima asks the editor to start writing rejection **reasons** into Editor Note — when she comments at all.~~ **Resolved:** rejection reasons are captured conversationally via `record-feedback`. Reasons are the raw material for the future learning milestone.
2. **Automate weekly collection.** Replace the hand-refreshed recorded-mentions file with the agent-extraction collector (ADR 0006) over the current source set. Recorded Source Mentions remain for deterministic trial runs only.
3. **Source Proposal flow** (glossary: Source Proposal). ~~Editor proposes via a "Source ideas" tab in the shared Sheet;~~ **Updated per ADR 0009:** editor proposes conversationally via chat or `source-proposal add`. The agent proposes into persistent `state/source-proposals.json`, surfaced in the run summary Dima already reviews; only `approve-source` run by Dima turns a proposal into a configured source. Rejections carry a reason and are never re-proposed; uncollectable accepted proposals become Skipped Sources.
4. **Widen English sources** through that flow, targeting the missing signal families — awards, reader-interest, audiobook rankings (current data covers only bestseller/ranking and media/curated list).

## Standing decisions

- **Presentation**: ~~one persistent Google Sheet shared by Dima and the editor. No UI, no dashboard. The Sheet is both presentation and feedback surface; weekly runs update it rather than replacing it.~~ **Superseded by ADR 0009:** the editor reviews in an agent session through a local rendered view or chat/canvas rendering. `editor-facing.csv` still exists as a portable export; there is no persistent Google Sheet on the main path.
- **Operator**: Dima stays the manual weekly operator and QA gate. Deployment is deferred until the feedback loop has survived several real cycles.
- **Collection architecture**: agent extraction is the default collector; bespoke parsers are a per-source optimization (ADR 0006). Adding a source is a config entry, not a development task.
- **The source list is a commodity; the add-a-source path is the product.** Three proposers (Dima, editor, agent), one approver (Dima).

## Parked, with re-entry conditions

- **Google Sheets API sync (both directions)** — superseded for the main path by ADR 0009; re-enters only with an explicit fallback/deployment decision, not as the normal editor loop.
- **Deployment / scheduling** — after the feedback loop has gone around at least twice with real data.
- **Learning from feedback** (tuning selection from rejection patterns) — data-gated, not calendar-gated: when enough real editor statuses and reasons have accumulated through conversational `record-feedback`, review them manually for patterns before building anything.
- **German-language stream** — validated demand (the editor's own words), first non-English target. Enters once steps 1–4 prove that adding a source is cheap; should be a config change plus existing Non-English Candidate handling, not a project.
- **Candidate analysis** (why is this book popular, review-pattern evidence, market-fit reasoning) — explicitly next *after* this phase, per both Dima and the editor.
