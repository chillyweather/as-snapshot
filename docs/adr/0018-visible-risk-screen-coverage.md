---
status: accepted
---

# Visible Risk Screen Coverage is separate from the Visible Risk status

Issue: #714.

## Context

The editorial Visible Risk status has three canonical values: `no visible risk`, `text check required`, and `clear visible risk`.
The screen could set a status only through fields that the retired bespoke BookPage and NPR collectors produced: `public_data_status`, `risk_signal_scope`, and `risk_evidence_url`.
Agent and Codex extraction, the default collectors since ADR 0006, emit none of them, so every candidate in the weekly portfolio fell through the screen with an empty status.
An empty cell could mean "screened, nothing found" or "never screened", and the review-metadata warnings counted only rows that already carried a status, so an inert screen raised no warning.
The keyword matcher also used substring containment, so `war` matched inside `award`.

## Decision

Visible Risk Screen Coverage is stored as a separate candidate-level operational record, following the precedent of ADR 0015 for Russian-edition Verification.
Its closed coverage values are `screened`, `partial`, `not_run`, and `failed`.
The record lists the evidence fields the screen read (`title`, `short_description`, `content_description`, `evidence_notes`) and one bounded English operational reason.
Russian labels belong to the Markdown and HTML review presentation, not to JSON artifacts.

Coverage is computed by the screening layer from what it was given and is never inherited from a collector field or inferred from an absent one.
`screened` requires the title and a description (`short_description` or Content Description); anything less is `partial`.
Evidence Notes are read for signals, but they say why a book was listed, such as an award or a ranking, rather than what it contains, so they never conclude on their own.
An award-only Evidence Note therefore yields no category and no status, and a generic source note never makes a title-only book `screened`.
The placeholder Evidence Note a mention receives when nothing describes it and the description fallback text are not evidence.
A Content Description is a screening input even though it is Russian prose; the English keyword table can miss a Russian mention of a category, a known limit of public-metadata screening.
The pipeline screens before description enrichment, so the post-enrichment refresh screens every `partial` candidate again and publishes changed Visible Risk fields to the current candidates, Candidate History, and the latest snapshot as one bundle.
A new status there keeps the priority rules it controls: clear book-content risk forces D, and A requires no visible risk.
A status supplied with the Source Mention is kept and the screen records `not_run`; a screen that raises records `failed` and leaves the status empty.

The editorial vocabulary does not change and no fourth status is added.
When a collector declares `public_data_status`, the existing branches keep their meaning.
Otherwise a `screened` candidate without a configured signal reaches `no visible risk`, a keyword hint without credible book-content evidence reaches `text check required` with its category, and a `partial` candidate without a signal keeps an empty status explained by its coverage.

Keywords match whole words and their plural.
A closed list of other forms that substring matching reached and that a keyword still means stays matched: `russian`, `warfare`, `wartime`, `nazism`, `lgbtq`, `lgbtqia`, `transgenderism`, and `drugged`.
The categories and keyword lists keep their meaning.

The run summary carries coverage counts.
A run in which no candidate reached `screened` raises a major warning, including a run in which every status was supplied (`not_run`).
Any `failed` coverage raises its own major warning.
The unreviewed-Visible-Risk warning no longer counts a `no visible risk` status that the screen concluded with `screened` coverage, because it asks nothing of a person; generated `text check required` and `clear visible risk` statuses still count.
The record joins the Candidate History snapshot; a Material Change rule for coverage transitions is out of scope.

## Consequences

Every editor-facing row says whether and how completely it was screened, so a blank risk cell is no longer ambiguous.
ADR 0004 still holds: a candidate with clear Visible Risk stays in the CSV with its category and comment.
Keyword hints that substring matching produced by accident, such as `award`, no longer reach the editor, and genuine whole-word hints now do as `text check required`.
Visible Risk remains public-metadata screening; it is not legal clearance or a full-text review.
