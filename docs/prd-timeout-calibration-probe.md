# PRD: Read-only Codex extraction timeout calibration probe

**Status:** Accepted

**Owner:** AutoScout workflow

**Related work:** Closed issues #283 and #306; `docs/weekly-runtime-budget.md`

## Problem statement

AutoScout currently enforces a 180-second per-attempt Codex extraction limit inside a 1,800-second portfolio budget.

This protects the weekly workflow from unbounded workers, but it does not show how long a source actually needs.

The 2026-08-01 weekly run demonstrates the measurement gap:

- Capture completed for the affected sources.
- Nine sources reached the 180-second extraction ceiling.
- One retry reached a 120.484-second worker-collection timeout because the remaining portfolio budget was smaller.
- The extraction phase consumed 1,461.931 seconds of its 1,800-second budget.
- The timing records therefore contain mostly right-censored observations.

Increasing the global timeout without measuring the underlying distribution could make the weekly workflow slower, exhaust the portfolio budget, or mask worker contention and difficult payloads.

We need a bounded diagnostic probe that measures intrinsic source and model time separately from production-shaped parallel contention and produces recommendations without changing production configuration.

## Goal

Provide a Codex-operated, read-only calibration probe that replays existing captured source pages, measures extraction duration and outcome distributions, and reports evidence-based per-source and portfolio timeout recommendations.

The probe answers:

1. How long does each source take when measured in isolation?
2. How does the same source behave in a production-shaped first-attempt round?
3. Is a timeout caused by source or payload complexity, transient Codex behavior, or worker contention?
4. Can a proposed timeout set fit inside the existing total portfolio budget?

## Non-goals

- Changing the production timeout or portfolio budget automatically.
- Re-running live source capture or checking source availability.
- Repairing extraction quality, evidence validation, or source-specific parsing.
- Replacing the bounded worker and subprocess safeguards from #306.
- Adding Platform API extraction, API keys, hosted services, or new runtime dependencies.
- Reusing stale validated extraction artifacts as fresh timing observations.
- Automatically reactivating disabled sources or changing source lifecycle state.
- Automatically committing, pushing, or modifying canonical weekly state.
- Treating optional run context as a fresh observation or using it in percentile calculations.

## Proposed solution

Add a diagnostic command, tentatively named `calibrate-timeouts`, that operates on a captured Codex worklist and its local capture files.

Example command shape:

```text
python -m autoscout calibrate-timeouts \
  --run-dir <captured-run-dir> \
  --source-name "<source-1>" \
  --source-name "<source-2>" \
  --source-name "<source-3>" \
  --repetitions 3 \
  --workers 1,3 \
  --timeout-ceiling-seconds 600 \
  --output-dir <calibration-output-dir>
```

The exact CLI spelling may follow the repository's existing command naming, but the behavior and artifact contract below are required.

`--source-name` is repeatable.

### Inputs and execution boundary

The probe must:

- Read the exact captured pages and work-item metadata from the selected run.
- Use the existing subscription-backed Codex extraction adapter against local captures only.
- Use a unique temporary or staging artifact for every source and repetition.
- Validate each produced artifact with the existing extraction validator.
- Write only derived calibration artifacts under the explicitly supplied diagnostic output directory.
- Leave canonical extraction artifacts, resume markers, weekly state, Git, and source configuration unchanged.
- Fail closed if a requested capture or work item is missing or does not match its declared identity or digest.
- Never treat an existing canonical extraction artifact as a successful fresh measurement.

Calibration observations use only selected work items and captures.

The report may read `source-runs.json`, `codex-workflow-resume.json`, and sanitized `workflow-log.json` as read-only run context to classify inactive or disabled sources, prior validated artifacts, and prior failure classes.

This context is never a fresh observation and never affects percentile calculations.

Missing optional context must be reported as missing and must never be guessed.

The probe is read-only with respect to AutoScout state, but it intentionally invokes Codex locally to measure the real extraction path.

It must use the existing subscription-backed Codex path and must not introduce an API-backed fallback.

### Measurement modes

The probe must support two execution modes in one report:

Timing and count fields are numeric when their phase occurs and `null` otherwise, with `diagnostic_class` explaining why the phase did not occur.

1. **Intrinsic mode:** Each selected source and repetition runs alone with `worker_count=1`.
2. **Production-shaped mode:** Each repetition is a first-attempt round that submits every selected source together to a bounded pool.

A worker count above 1 requires at least that many selected sources.

The command must fail this check before Codex execution.

Each source gets one independent first attempt per round.

Queue wait is measured from task submission until the Codex subprocess starts.

Subprocess duration is measured from child start through bounded exit or termination.

Validation duration is measured separately.

Total duration is measured from task submission through final classification and cleanup.

The operator can select either mode, both modes, or explicit worker counts.

The default recommendation is to measure both modes for sources that timed out in a weekly run.

Retries are disabled for the baseline measurement.

Retry behavior is covered only by the separate portfolio retry-envelope simulation.

### Measurement record

Every observation must record:

- Source name and stable work-item identity.
- Worker count, repetition number, and round.
- Timeout ceiling applied to that observation.
- Capture byte size and capture Unicode character count.
- Generated prompt character count.
- Queue wait, subprocess duration, validation duration, and total duration.
- Exit code or sanitized command diagnostic class.
- Output byte size, raw extracted entry count, and validated entry count.
- Final status.

The probe must not claim model token counts that it cannot observe.

`final_status` must be exactly one of:

- `validated` - Codex completed and the artifact passed validation.
- `invalid_output` - Codex completed but output was malformed or failed schema or provenance validation.
- `timeout` - The configured ceiling was reached before a usable result.
- `command_failed` - Codex exited unsuccessfully for a bounded, classified reason.
- `unavailable` - The local Codex executable or required runtime was not available.
- `budget_exhausted` - The probe stopped because its own total budget ended.
- `input_invalid` - The capture or work-item identity check failed before Codex execution.

`diagnostic_class` is separate from `final_status`.

It must distinguish `codex_timeout`, `worker_collection_timeout`, validation failure classes, and sanitized command failure classes.

Timeout observations are right-censored.

Their ceiling is a lower bound on the unknown completion time.

Censored values must never be included in percentiles.

### Repetition and ceilings

The operator must be able to choose:

- Selected sources.
- Repetitions per source and worker mode.
- Worker counts.
- A diagnostic per-observation ceiling greater than the production limit.
- A total calibration budget.

The command must enforce its own total budget and terminate cleanly.

A ceiling that is too small to distinguish the known production timeout must be rejected or explicitly acknowledged as a censored-only run.

An eligible observation for completion-rate policy is any observation that passed input validation and was scheduled before total-budget exhaustion.

Validated completion rate is validated eligible observations divided by all eligible observations.

The initial operator recipe is:

1. Run one broad sweep against all affected sources with a generous bounded ceiling.
2. Repeat sources that complete near the ceiling or show materially different intrinsic and production-shaped durations.
3. Use at least 20 validated observations for a production timeout recommendation.

If a source has fewer than three validated observations, the report must say `insufficient_data`.

Three through 19 validated observations are `preliminary` only.

A p95 calculated from five observations is the maximum completed duration in that sample and is not sufficient for production confidence.

### Statistical summary and recommendations

For each source and worker mode, the report must show:

- Number of observations.
- Validated, invalid, timed-out, failed, unavailable, input-invalid, and budget-exhausted counts.
- Minimum, p50, p90, p95, and maximum for completed validated runs.
- Censored-observation count, minimum censoring threshold, maximum censoring threshold, and strongest known lower bound.
- Variation between repetitions.
- Comparison with the current 180-second limit.
- Comparison between intrinsic and production-shaped timing.

Percentiles use deterministic nearest-rank calculation over sorted completed validated durations.

For percentile `p`, the rank is `max(1, ceil(p * n))`.

The report must state p50, p90, and p95.

Fewer than three validated observations produce `insufficient_data`.

Three through 19 validated observations produce `preliminary` only.

At least 20 validated observations are required for a production timeout recommendation.

Production timeout recommendations use production-shaped observations only.

Intrinsic observations support diagnosis and comparison but cannot by themselves produce a production timeout recommendation.

The recommended ceiling is rounded upward to the nearest 30 seconds using this formula:

`ceil_to_30(p95 + max(30 seconds, 20 percent of p95))`

The formula, sample size, and policy values must be recorded in both JSON and Markdown.

The only outcome vocabulary is `recommended`, `insufficient_data`, `preliminary`, `unstable`, and `structurally_slow_or_stuck`.

For at least 20 validated observations, `unstable` means validated completion rate below 80 percent or `p95 / p50 >= 2.0` when p50 is greater than zero.

If p50 is zero and p95 is greater than zero, classify `unstable`.

If all started observations time out at the highest requested diagnostic ceiling, the outcome is `structurally_slow_or_stuck`.

Outcome precedence is exactly five outcomes: `structurally_slow_or_stuck` first when its condition applies; `insufficient_data` for fewer than 3 validated observations; `preliminary` for 3-19 validated observations; `unstable` for at least 20 validated observations when either stability test applies; and `recommended` for at least 20 validated observations when neither stability test applies.

`input_invalid` and `unavailable` are observation statuses, not recommendation outcomes.

The report must never modify `CODEX_TIMEOUT_SECONDS` or any configuration file.

### Portfolio simulation

The simulation uses active Codex work items from the captured run in worklist order.

It uses each source's production-shaped recommended ceiling and the current 180 seconds for sources without a recommendation.

It simulates bounded list scheduling in first-attempt rounds.

It also simulates a conservative retry-envelope round in which every source can consume a second full attempt.

The simulation uses the configured worker count and maximum attempts.

The budget is 1,800 seconds.

Required headroom is 180 seconds, or 10 percent.

`feasible_with_headroom` means makespan is less than or equal to 1,620 seconds.

`feasible_without_required_headroom` means makespan is greater than 1,620 seconds and less than or equal to 1,800 seconds.

`budget_conflict` means makespan is greater than 1,800 seconds.

The report must include first-round and retry-envelope makespans, assumptions, headroom seconds, and headroom percent.

The simulation must not change configuration.

## Artifacts

The probe must produce two derived artifacts in the supplied output directory:

1. `timeout-calibration.json` - Machine-readable observations, summaries, percentile calculations, censoring thresholds, portfolio simulation, invocation parameters, and schema version.
2. `timeout-calibration.md` - Concise operator-facing explanation of the result, including source tables, timing distributions, uncertainty, and the recommended next action.

The JSON must preserve raw observation records needed to reproduce the summary without re-running Codex.

It must not include prompts, captured page bodies, reviews, blurbs, long excerpts, secrets, or unredacted subprocess output.

The Markdown report must explicitly state:

- That the probe used replayed captures and did not perform live source collection.
- Which observations were censored by the diagnostic ceiling.
- The minimum and maximum censoring thresholds and strongest known lower bound.
- Which sources completed with valid output.
- Whether the result is sufficient to change a production limit.
- Whether the proposed limits fit the portfolio budget.
- Missing optional run context, if any.
- One recommended next action, if the data is inconclusive or unsafe.

## User stories

1. As the operator, I want to measure real extraction duration without another live fetch, so that network health does not confuse timeout diagnosis.
2. As the operator, I want intrinsic and production-shaped timings side by side, so that I can distinguish a slow payload from worker contention.
3. As the operator, I want timeout observations treated as right-censored lower bounds, so that the report does not pretend the configured limit is the source's true runtime.
4. As the operator, I want a portfolio-budget simulation, so that increasing one source's limit cannot silently make the whole weekly run exceed its safety envelope.
5. As the operator, I want recommendations to remain derived evidence, so that changing production limits remains an explicit engineering decision.
6. As Dima, I want the probe to reuse the existing extraction seam and public artifact contracts, so that it can be tested without exposing internal helpers or creating a second Codex workflow.

## Acceptance criteria

### Safety and boundary

- Running the probe against a real captured run performs no network capture.
- The probe does not modify canonical extraction artifacts, resume markers, candidate history, editorial state, source configuration, or Git.
- Every fresh observation uses a unique staging path and cannot overwrite a canonical artifact, including when a late worker completes.
- Missing, stale, or digest-mismatched captures fail before Codex execution.
- A worker count above 1 with fewer selected sources than workers fails before Codex execution.
- A stuck subprocess or worker is terminated through the existing bounded cleanup path and cannot keep the probe alive beyond its total calibration budget.
- The probe does not use an API key or silently switch extraction backends.

### Measurement correctness

- A deterministic fixture with known completion durations produces the expected nearest-rank p50, p90, and p95 summaries.
- The fixture uses sorted completed validated durations and `max(1, ceil(p * n))` for percentile rank.
- A deterministic fixture with right-censored observations does not count a timeout ceiling as a completed duration or include it in percentile calculations.
- The report identifies the minimum and maximum censoring thresholds and strongest known lower bound.
- The report distinguishes final status from `codex_timeout`, `worker_collection_timeout`, validation failure classes, and sanitized command failure classes.
- The report records intrinsic and production-shaped observations and exposes their duration difference.
- The report records queue wait, subprocess duration, validation duration, and total duration with their specified boundaries.
- A source with fewer than three validated observations receives `insufficient_data`.
- A source with three through 19 validated observations receives `preliminary` and cannot produce a production timeout recommendation.
- A p95 from five observations is identified as the sample maximum and is not treated as production-confidence evidence.
- A source with at least 20 validated observations receives a recommendation only when the completion-rate and stability policy checks pass.
- Production timeout recommendations use production-shaped observations only, and intrinsic observations cannot by themselves produce a production timeout recommendation.
- A source for which all started observations time out at the highest diagnostic ceiling receives `structurally_slow_or_stuck`.

### Portfolio decision support

- The report simulates active captured work items in worklist order with bounded list scheduling.
- The simulation includes first-attempt and conservative second-full-attempt retry-envelope makespans.
- The simulation uses each recommended ceiling, 180 seconds for sources without one, configured workers, and maximum attempts.
- The report records the 1,800-second budget and 180-second, 10 percent required headroom.
- The report uses the exact outcomes `feasible_with_headroom`, `feasible_without_required_headroom`, and `budget_conflict` with their specified thresholds.
- No recommendation changes production configuration automatically.

### Artifact and workflow contract

- `timeout-calibration.json` validates against a versioned schema and contains all observations needed to regenerate the Markdown report.
- The JSON and Markdown record the recommendation formula, sample size, and policy values.
- The JSON records capture bytes, capture Unicode characters, generated prompt characters, output bytes, raw extracted entries, and validated entries.
- The artifacts do not claim unavailable model token counts.
- `timeout-calibration.md` names the selected sources, modes, repetitions, ceilings, censored observations, sample sizes, and outcome.
- Identical fixture inputs and a deterministic fake extraction agent produce equivalent summaries apart from explicitly marked runtime metadata.
- The existing weekly pipeline and canonical artifacts remain unchanged when the probe is not invoked.
- The full test suite uses fake agents and bounded test clocks and does not invoke Codex, sleep for real timeout durations, fetch the network, or depend on a live source.

### Current-run calibration check

As a manual acceptance check, run the probe against the 2026-08-01 captured worklist.

The check must use only selected work items and captures for observations.

It may read the three optional run-context files as read-only context.

It must report missing optional context rather than guess it.

The result must separate actual extraction timeouts, validation failures, inactive or disabled sources, and sources whose prior validated artifacts were preserved.

Context classifications must not enter percentile calculations.

## Implementation notes

- Keep the production `CODEX_TIMEOUT_SECONDS`, portfolio budget, worker count, retry semantics, and source lifecycle unchanged in this slice.
- Reuse `ProductionCodexAdapter` and `CodexCliExtractionAgent` through a diagnostic seam that accepts explicit timeout, worker, and budget values.
- Do not reuse the production run's resume marker for calibration.
- Add source-local diagnostics for queue wait and subprocess completion using the specified timing boundaries.
- Keep derived artifacts outside editor-facing weekly output and canonical state.
- Preserve the existing subscription-backed Codex operating model.
- Captured pages remain the only input to extraction during calibration.
- The eventual production timeout or configuration change is a separate follow-up issue and is not part of this PRD or its delivery tickets.

## Delivery plan

Issue #347 remains the parent PRD.

A production timeout change is not part of these tickets.

- **A.** [#348 - Create a reusable observed Codex extraction seam](https://github.com/chillyweather/auto-scout/issues/348) Expose the reusable read-only seam, observation boundaries, validation classification, and sanitized diagnostics.
- **B.** [#349 - Add the intrinsic read-only timeout calibration probe](https://github.com/chillyweather/auto-scout/issues/349) Implement selected-capture replay, intrinsic execution, raw versioned observation records, and CLI validation.
- **C.** [#350 - Add production-shaped timeout calibration and contention timing](https://github.com/chillyweather/auto-scout/issues/350) Implement first-attempt rounds, bounded list scheduling, worker contention timing, and pre-execution worker-count checks.
- **D.** [#351 - Add deterministic timeout summaries and recommendations](https://github.com/chillyweather/auto-scout/issues/351) Implement censoring, nearest-rank statistics, outcome precedence, policy formulas, and both artifacts.
- **E.** [#352 - Add timeout portfolio simulation and complete calibration acceptance](https://github.com/chillyweather/auto-scout/issues/352) Implement first-round and retry-envelope simulation, integration coverage, and the manual acceptance check.

Dependencies are A before B and C, B and C before D, and D before E.

The tickets must not modify production timeout or configuration.

## Follow-up decision gate

After calibration, choose exactly one engineering outcome:

1. **Keep the current limit** when the evidence is sufficient and the recommendation does not justify a change.
2. **Raise a source-specific or global limit** only in a separate follow-up issue when completed validated observations support it and the portfolio simulation retains headroom.
3. **Reduce payload or add a source-specific extraction strategy** when a source is `structurally_slow_or_stuck` or `unstable`.
4. **Investigate Codex or worker contention** when intrinsic timing is healthy but production-shaped timing is materially worse.

The calibration probe itself must stop at this evidence boundary.

It must not apply the chosen outcome automatically.
