---
status: accepted
---

# Results representation and feedback channel

How the editor *sees* the weekly candidate list and how she *gives feedback* are a single coupled decision, because the operating model (ADR 0007) has no always-on server and no persistent editable surface unless we add one. The editor confirmed her preference: **she reviews and gives feedback by talking to the agent.**

## Decision

The editor reviews the weekly list **in an agent session** and gives feedback **through chat**:

- **Representation:** the agent renders a **local view** of the candidate list on demand (a generated HTML page it opens, or an in-chat/canvas rendering). There is no persistent web app and no pushed Google Sheet on the main path. `editor-facing.csv` still exists and can be opened/downloaded.
- **Feedback:** the agent captures decisions conversationally (reject / interested / maybe-watch / already-known / needs-deeper-analysis, plus notes) and records them via a small **new programmatic feedback command** that writes into the existing identity-key state (`title|authors`), feeding the same Material Change / candidate-history machinery as before.
- Because feedback identity is inferred from conversation, the agent **confirms the matched candidate** before recording (e.g. "Behave by Robert Sapolsky → Reject, correct?") to avoid mis-keying.
- This **retires the manual edited-CSV round-trip** (`ingest-feedback` on a hand-edited sheet) as the editor's normal loop — the original friction in `docs/notes/ui-and-presentation.md`.
- **No new dependency** is introduced; the zero-dependency, stdlib-only design (ADR 0007) is preserved.

## Considered Options

- **Local agent-rendered view + chat feedback** (programmatic feedback command, no round-trip). **Chosen** — matches the editor's stated preference and keeps zero dependencies.
- **Push-only Google Sheet view + chat feedback** (glanceable any-device, read-only). Rejected: editor does not need to view the list independently of a session; would add the first non-stdlib dependency for no benefit she asked for.
- **Persistent Google Sheet with read-back round-trip** (editor edits feedback cells). Rejected: keeps the round-trip friction this whole effort set out to remove.

## Consequences

- The feedback command is two-phase by default: `record-feedback --identity-key … --status … --note …` validates and prints a preview (return code 3, zero mutation); the agent presents a Russian confirmation question. Only after the editor confirms does the agent re-run with `--confirmed-by-editor` to apply. Funnels into the same state files and identity model that `ingest-feedback` writes today, so nothing downstream changes.
- `ingest-feedback` and the CSV round-trip are **kept as a dormant fallback** (useful for the Nanode fallback path and the Dima escape hatch), but are no longer the editor's primary loop.
- The reliability of feedback now depends on the agent correctly resolving "which candidate" from conversation; the confirm-before-record step is the safeguard. This is an acceptable single-editor trade-off.
- No new glossary term is required: in-chat feedback maps onto the existing editor-status terms already defined in GLOSSARY.md.
