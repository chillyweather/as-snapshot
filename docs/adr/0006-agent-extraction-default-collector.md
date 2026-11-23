# Agent extraction is the default collection mode

AutoScout collects Source Mentions from a configured source by fetching the source page and using an LLM to extract mentions into the same schema as Recorded Source Mentions, rather than hand-writing an HTML parser per source. Adding a source becomes a configuration entry (URL, signal family, check frequency, notes) instead of a development task, and the weekly run is one command with no manual recorded-mentions refresh. This was adopted because the largest recurring operating cost was hand-refreshing recorded mentions, and because bespoke parsers made every new source a maintenance liability that broke on markup changes.

Hard provenance guardrails make extracted mentions meet the same evidence standard as recorded ones: every mention must carry a real evidence URL present in the fetched page, pass the same schema/vocabulary validation, and reject copied bodies/reviews/blurbs (metadata-only, consistent with [ADR 0002](0002-mvp-stores-evidence-notes.md)). A mention whose evidence URL cannot be verified against the fetched page is **dropped and logged with a reason**, never invented; a fully-dropped extraction is a failed source run, not an empty success. Source runs record extraction health counts (extracted / validated / dropped-with-reasons).

[ADR 0005](0005-configured-source-discovery-only.md) still holds: agent extraction operates only on configured sources — it cannot follow links to new sources or create candidates from general search.

## Considered Options

- **Hand-written bespoke parsers per source** (status quo: NPR, BookPage). Rejected as the default because each source is then development + ongoing breakage risk. **Kept** as a per-source optimization where a source is high-volume and stable enough to earn one; existing bespoke parsers run unchanged behind the shared collection interface.
- **Hand-refreshed Recorded Source Mentions** as the live input. Rejected as the steady-state mode (manual refresh every run); **retained** as deterministic trial/test fixtures.

## Consequences

- The LLM call is isolated behind an extraction-backend interface with a recorded-replay mode, so collector behavior is testable deterministically without network or model access. Live fetching/extraction stays opt-in per run; live responses can be captured and replayed.
- The collector is language-agnostic and feeds existing Non-English Candidate handling, so a future German source is a config change, not a new collection project.
- Model choice, prompt wording, and token budgets are implementation details, but extraction must request structured output conforming to the mention schema and instruct metadata-only extraction.

_Backfilled after the fact: this decision was implemented under issues #104 and #112–#116 and referenced by number before the ADR file existed._
