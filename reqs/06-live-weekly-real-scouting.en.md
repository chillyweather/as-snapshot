# AutoScout PRD — Live Weekly Real Scouting Configuration

Status: draft PRD v0.1
Date: 2026-06-29
Source decisions:
- `docs/adr/0005-configured-source-discovery-only.md`
- `docs/adr/0006-agent-extraction-default-collector.md`
- `docs/current-weekly-operator-workflow.md`
- PR #182 real-live extraction path

This PRD defines the next step after the real extraction backend has landed: turning AutoScout's current weekly operation from a replay rehearsal into a real model-backed weekly scout against configured sources.

---

## Problem Statement

AutoScout can now run a real model-backed `agent-extraction-live` path, but the checked-in weekly configuration still uses deterministic source-page fixtures and deterministic extraction-response files. A normal weekly run therefore reports `collection_mode=fixture-live` with source-level `collection_mode=real-live-replay`: useful for rehearsal and debugging, but not a fresh scouting run.

From the operator's perspective, this creates an awkward half-step. The app has the real-live engine, yet the blessed weekly command still produces stale fixture-backed evidence. To run a real weekly scout, the operator must know exactly which fixture fields to remove, which credential and bounds to keep, where captures should go, how to verify that the run was truly live, and how to recover if one source is blocked, malformed, too large, or too costly.

The next product slice should make a real configured-source weekly scout a deliberate, safe, repeatable local workflow.

## Solution

Add an explicit live weekly configuration and supporting operator workflow that fetches configured source URLs, calls the real model-backed extraction transport, captures both fetched pages and model extraction responses, and proves through `run-health` and artifacts that the run was genuinely `real-live`.

The replayable fixture-backed current weekly config should remain available as a deterministic rehearsal/debug path. The new live path should not broaden discovery: it still collects Scouting Candidates only from configured sources and still rejects general web search, topic search, unaccepted Source Proposals, copied source bodies, and unverifiable model output.

The live configuration must be safe by default: bounded input, bounded output, bounded timeout, explicit credential environment variable, stable capture paths, source-health degradation instead of mysterious crashes, and clear docs for when to call a result ready versus when to hand off diagnostics.

## User Stories

1. As Dima, I want a checked-in live weekly config separate from the deterministic rehearsal config, so that the team can intentionally run fresh scouting without destroying the replay baseline.
2. As the editor's local agent, I want one blessed command for the live weekly scout, so that I do not have to infer which config fields to remove or keep.
3. As the editor, I want the weekly report to be based on freshly fetched configured source pages, so that the scouting list reflects current market signals.
4. As Dima, I want replay captures from a live run, so that any surprising model output can be reproduced deterministically.
5. As the editor's local agent, I want `run-health` to say `collection_mode=real-live` for a true live run, so that I can tell fresh scouting apart from replay rehearsal.
6. As Dima, I want `source-runs.json` to record backend name, backend mode, safe failure status, capture paths, input limit, output limit, and timeout, so that source failures can be diagnosed without secrets or raw prompts.
7. As the editor, I want blocked or failed sources to degrade into source health diagnostics, so that one bad source does not make the whole weekly workflow mysterious.
8. As Dima, I want deterministic tests for the live configuration using an injected transport, so that CI does not call a real model.
9. As the editor's local agent, I want missing credentials to produce `real-live-unavailable`, so that I know this is an operator setup problem, not a source scarcity result.
10. As Dima, I want oversized source pages to fail as source health, so that model cost stays bounded.
11. As Dima, I want model output tokens bounded and visible in config, so that output cost and failure modes are reviewable.
12. As the editor, I want the live run to preserve existing Russian-edition checks, internal exclusions, Visible Risk behavior, suppression, and feedback history, so that the scouting workflow does not change under me.
13. As the editor's local agent, I want the docs to say exactly when to use live config versus replay config, so that I do not accidentally present stale fixtures as fresh scouting.
14. As Dima, I want the first live run to write stable source-page and extraction-response captures, so that it can be converted into future replay fixtures after review.
15. As the editor, I want the report to include honest caveats when fewer than the target number of rows are produced, so that source scarcity is not hidden by filler candidates.
16. As Dima, I want source failures classified by source name, status, reason, and safe failure status, so that repairs can be prioritized source by source.
17. As the editor's local agent, I want clear guidance for committing live captures or diagnostic artifacts, so that secrets and unrelated local changes are not accidentally included.
18. As Dima, I want a migration path from today's replay-backed config to a live config, so that the current weekly sources do not need to be rethought from scratch.
19. As the editor, I want no general web search or topic search to create candidates, so that every candidate still has configured-source provenance.
20. As Dima, I want a dry-run or rehearsal path to remain available, so that prompt/config changes can be checked offline before spending model calls.
21. As the editor's local agent, I want clear operator recovery steps for HTTP 401/403/429, CAPTCHA-like pages, malformed model output, empty extraction output, and fully dropped responses, so that I can classify a failed week correctly.
22. As Dima, I want the live workflow to be agent-friendly and file-based, so that it fits the editor-run local agent operating model without a hosted service.
23. As the editor, I want the same review artifacts after a live run as after a replay run, so that my weekly review surface does not change.
24. As Dima, I want acceptance tests to prove that a captured live run can replay through `agent-extraction-replay`, so that live scouting remains inspectable and debuggable.
25. As the editor's local agent, I want the current weekly docs to make the freshness state unmistakable, so that I never call `real-live-replay` a live scout.

## Implementation Decisions

- Add a separate live weekly configuration rather than mutating the deterministic rehearsal config in place. The live config should omit deterministic source-page and extraction-response fields and keep explicit configured source URLs, `agent-extraction-live`, `enable_live: true`, `extraction_backend: real`, `enable_real_backend: true`, credential env name, input bounds, output bounds, timeout, and stable capture paths.
- Keep the existing current weekly replay/rehearsal configuration as a safe baseline for deterministic checks and debugging.
- Use the existing real extraction backend and transport seam. Do not add another model abstraction unless the current transport seam proves insufficient.
- The live configuration should write captures into the run output directory using stable names. Captures must be replayable by switching to `agent-extraction-replay` with the captured page and extraction files.
- `run-health` is the operator gate. A real live run must report `collection_mode=real-live` when every source is truly live, or `mixed` when some sources are live and others are replay/skipped/failed. It must not silently aggregate a real live run into `fixture-live`.
- The source-level artifact remains the source of truth for diagnostics. It should include safe backend metadata and never include secrets, raw prompts, API keys, copied source bodies in metadata, or unbounded model text.
- Preserve the configured-source-only boundary from ADR 0005. The live model can extract only from the already-fetched configured source page. It cannot browse, search, follow links to create candidates, or turn Source Proposals into Scouting Candidates.
- Preserve the metadata-only guardrails from ADR 0006. Model output remains untrusted input and must pass evidence URL verification, forbidden copied-text rejection, schema normalization, candidate filtering, Russian-edition checks, and exclusion/suppression logic.
- Treat live-source failures as source health, not app failure, when the pipeline can still complete and produce honest artifacts. Missing credentials, invalid bounds, blocked pages, transport errors, malformed output, empty output, and all-dropped output must be explainable in artifacts.
- The operator workflow should explicitly define what to commit after a live run. Normal shareable reports may be committed when requested; source-page/model-response captures require care and should be committed only when they are intended as fixtures or diagnostic evidence and contain no secrets or forbidden copied long text.
- If the live run produces fewer than the editor row target, the system should report source scarcity or source health honestly. It must not add filler rows or broaden discovery.
- The live weekly path should remain local CLI + files. No server, scheduler, queue, hosted dashboard, or new runtime dependency is part of this PRD.

## Testing Decisions

- The highest-value test seam is `run-mvp` followed by `run-health` over a live-config fixture that uses the existing injected real extraction transport. The test should prove external behavior: artifact creation, collection mode, source health, and replayability.
- Unit-level transport tests may verify that model input/output/timeout controls are passed into the transport, but they should not replace end-to-end CLI artifact assertions.
- Tests must not call a real model or live network by default. Use the existing deterministic transport injection and fake page fetching for automated tests.
- Add a test proving that a config without deterministic source-page/extraction fields reaches the live transport path and reports `real-live`.
- Add a test proving that live captures are tagged `collection_mode=real-live` and can replay through `agent-extraction-replay` to produce the same Source Mentions.
- Add tests for source-health degradation: missing credential environment variable, invalid output limit, oversized input, timeout or transport failure, malformed model JSON, and all mentions dropped by provenance guardrails.
- Add a docs/config assertion that the blessed live config includes explicit `model_max_output_tokens`, `max_model_input_chars`, `model_timeout_seconds`, credential env name, and capture paths.
- Reuse existing source collection and CLI smoke test patterns rather than adding a new test harness.
- Run the existing full unittest suite as regression coverage to prove Russian-edition checks, internal exclusions, feedback history, suppression, report generation, and health summaries remain compatible.

## Out of Scope

- General web search, topic search, or autonomous internet discovery of candidates.
- Accepting Source Proposals automatically into configured sources.
- A hosted application, scheduler, queue, dashboard, browser automation surface, or background worker.
- Changing the editor review surface, feedback statuses, record-feedback semantics, Candidate History, Material Change behavior, Russian-edition logic, or Visible Risk rules.
- Replacing all source configuration or curating a large new source set.
- Guaranteeing that every configured source is accessible every week; blocked and failed sources are expected source-health outcomes.
- Committing live captures blindly as permanent fixtures before human review.
- Proving production model quality beyond the first operational live run and artifact inspection.

## Further Notes

The current state after PR #182 is an important threshold: the real-live backend exists and is bounded, but the default checked-in current weekly config still runs as `real-live-replay`. This PRD should therefore be implemented as an operationalization slice, not as another backend architecture slice.

The first successful live run should be treated as a calibration run. The team should inspect `run-health`, `source-runs.json`, captures, dropped mention reasons, editor-facing rows, and source scarcity before calling the weekly live path ready for normal use. If the first live run exposes source-specific issues, those should become targeted source-health follow-up issues rather than broad changes to the discovery boundary.

