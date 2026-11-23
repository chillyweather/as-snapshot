# Capture, replay and extraction

Part of the [operator workflow](../current-weekly-operator-workflow.md).

## Detailed Command Sequence (Diagnostic Fallback)

This command sequence is the explicit step-by-step fallback for diagnosis,
replay, and Dima handoff. The Russian [one-request path](one-request-path.md) is the normal
editor interface; use these commands when troubleshooting.

For portable in-repo invocation prefer `.venv/bin/python -m autoscout`.
The blessed launcher (`scripts/run-weekly`) resolves the same interpreter
and canonical arguments automatically.

AutoScout discovery uses configured sources only. General web search,
topic search, or agent intuition may produce Source Proposals, but never Scouting Candidates.

### Config Files

- **`configs/current-weekly-pilot-live.json`** — Live-capture config with `readiness_purpose: recurring-weekly`. Source URLs and metadata only (no fixture files). Sources are fetched live.
- **`configs/current-weekly-pilot.json`** — Replay config with committed fixture files for deterministic replay without Codex or network access.
- **`runs/current-weekly-pilot-russian/codex-replay-config.json`** — Generated replay config produced by `finalize-codex-run`.

### Fresh Scout Path (live capture → Codex extraction → ingest)

1. **Git preflight**: inspect branch, status, recent commits, and remote tracking (git preflight). Do not use destructive commands such as `git reset --hard`, `git checkout --`, or force pushes. If unrelated user changes exist, leave them alone.

2. **Pull canonical state**: update from the normal remote/base branch using non-destructive git commands. Stop on conflicts instead of overwriting.

3. **Live capture**:

```bash
python -m autoscout prepare-codex-extraction \
  --config configs/current-weekly-pilot-live.json \
  --output-dir runs/current-weekly-pilot-russian \
  --live
```

   Each source gets one capture attempt and at most one retry after a timeout or a rate limit (issue #1005); The capture-stage source runs record both attempts under `capture_attempts` in the `source-runs.json` written by `prepare-codex-extraction` and in the `capture` stage detail of `workflow-log.json`. See `docs/weekly-runtime-budget.md`.

4. **Codex extraction**: in a Codex session, feed each work item from `codex-extraction-worklist.json` to Codex. Each work item carries an `extraction_contract` block — obey it exactly; it is the same contract the collector validates against.
Save each extraction artifact under the `expected_extraction_file` filename from the worklist (e.g. `codex-extraction.json`).

5. **Finalize**:

```bash
python -m autoscout finalize-codex-run \
  --output-dir runs/current-weekly-pilot-russian \
  --live-config configs/current-weekly-pilot-live.json
```

6. **Run MVP Pipeline**:

```bash
python -m autoscout run-mvp \
  --config runs/current-weekly-pilot-russian/codex-replay-config.json \
  --output-dir runs/current-weekly-pilot-russian \
  --state-dir runs/current-weekly-pilot-russian
```

7. **Health check**:

```bash
python -m autoscout run-health \
  --output-dir runs/current-weekly-pilot-russian
```

8. **Render review**:

```bash
python -m autoscout render-review \
  --output-dir runs/current-weekly-pilot-russian \
  --state-dir runs/current-weekly-pilot-russian
```

9. **Editor review** from `weekly-review.md` or `weekly-review.html`.
   Both list the books under the date they were added, newest first; the latest run's date carries «последний запуск».
   There is no «Новые» group (#1057).
   The HTML tabs are «Все» (every book), «Без решения» (the same timeline without the books the editor decided), «Обновлённые» and «Сделки и права» (rights leads) (#1063). There is no «С решением» tab: a book with a decision stays in «Все».
   With `editorial_fit_ordering` on and a recorded fit score, the books of one date are ordered by fit score, and only «Без решения» becomes one list by score with no date sections; see [Review format contract](one-request-path.md#review-format-contract-278).
   A book whose last material change is within 14 days of the review date (`UPDATED_WINDOW_DAYS` in `autoscout/review_dates.py`) stays under its date and shows «⬆ обновлено» with its change date in every tab (the «Обновлено» column in Markdown).
   «Обновлённые» lists those books again under the date of the change, newest first.
   In the HTML review the name of a Probation Source is violet in the source column (`autoscout/source_trial.py`); its tooltip gives the week of the trial, or «срок проверки истёк, нужно решение» past the source's window.
   The colour stays until the source is promoted or retired with `source-lifecycle`.

10. **Preview feedback** (zero mutation — return code 3):

```bash
python -m autoscout record-feedback \
  --identity-key "<full Candidate ID>" \
  --status <interested|maybe_watch|reject|already_known|needs_deeper_analysis> \
  --note "<verbatim editor note>" \
  [--appeal <appealing|not_appealing>] \
  [--rights <available|unavailable|unknown>] \
  [--quality-problem <none|description_missing|wrong_category|bad_links|unusable_title|other>] \
  [--info-needed <none|purchase_status|rights_status|russian_edition|description|other>] \
  --state-dir runs/current-weekly-pilot-russian
```

The preview prints the matched title, author, full identity key, intended status, the editor note verbatim, and the exact structured dimensions that would be recorded (Issue #678: `dimension_editorial_appeal=…`, `dimension_rights_availability=…`, `dimension_quality_problem=…`, `dimension_information_needed=…`, `final_disposition=…`). Omitted dimensions stay `unspecified`. No state file is created or modified. The agent then presents a concise **Russian confirmation question** to the editor. A bare refusal such as «Нет.» stays explicitly unspecified: no dimension may be recorded beside it.

11. **Apply confirmed feedback** (after explicit editor confirmation only):

```bash
python -m autoscout record-feedback \
  --identity-key "<full Candidate ID>" \
  --status <interested|maybe_watch|reject|already_known|needs_deeper_analysis> \
  --note "<verbatim editor note>" \
  [--appeal …] [--rights …] [--quality-problem …] [--info-needed …] \
  --state-dir runs/current-weekly-pilot-russian \
  --confirmed-by-editor
```

Only effects the editor explicitly confirmed are applied. The confirmed invocation must carry exactly the dimensions that were previewed. Preserve verbatim notes — do not normalize, translate, or summarize editor wording. See `docs/issue-678-structured-feedback.md` for how the dimensions may and may not be used by future learning or evaluation.

12. **Record visibility** (when needed):

```bash
python -m autoscout record-visibility \
  --identity-key "<full Candidate ID>" \
  --action <hide|remove|restore|show|show-again> \
  --reason "<verbatim reason>" \
  --state-dir runs/current-weekly-pilot-russian
```

13. **Rerender/finalize** with `run-mvp` after confirmed mutations (suppression, history, or memo state). `record-feedback --confirmed-by-editor` stages pending feedback; the subsequent `run-mvp` consumes it into Candidate History, suppression, or `memo-queue.json` behavior. Then `render-review` to refresh the review.
14. Use `ingest-feedback` only as fallback for a returned CSV or source-ideas CSV. It is not the normal conversational path.
15. **Git finalization**: after an authorized scan or repair, routine finalization (commit relevant run state and push) is automatic. Inspect status/diff. Commit only relevant run state, accepted configuration changes, docs, or source-proposal artifacts, and push. open a PR against `main` when the editor requests it. Confirmation boundaries still gate consequential state changes.

### Replay Path (deterministic, no Codex session needed)

```bash
python -m autoscout run-mvp \
  --config configs/current-weekly-pilot.json \
  --output-dir runs/current-weekly-pilot-russian \
  --state-dir runs/current-weekly-pilot-russian
```

### Recurring Weekly Readiness

The `readiness_purpose: recurring-weekly` config key separates normal weekly
operation from the initial seed/calibration run. In recurring mode:

- Readiness is based on artifact validity, source execution, and cumulative
  review generation — **not** a fixed row-count target.
- A healthy week with 0 New books is ready, not a failure.
- One degraded source does not invalidate usable results from other sources.
- Structured readiness facts (health classification, attention requirement,
  delta counts, source-problem counts) are available for the Russian
  presentation layer.

The 100-row target applies only in `seed-calibration` mode (historical default
for backward compatibility with replay fixtures).

---

## Codex Subscription vs Platform API Boundary

AutoScout has two distinct authentication and billing domains:

- **Codex subscription access** (default path): the editor's Codex subscription
  powers Codex-operated extraction. Codex reads captured source pages, produces
  metadata-only Source Mention extraction artifacts, and writes them as local
  files. No Platform API credentials are needed. This is the blessed operator
  path — the editor uses their existing Codex subscription without additional
  API billing. Codex-operated scouting is the blessed default path. It does **not** require `AUTOSCOUT_AGENT_EXTRACTION_API_KEY` or any Platform API credentials.
- **Platform API credentials** (explicit opt-in): requires `AUTOSCOUT_AGENT_EXTRACTION_API_KEY`
  or equivalent Platform API credentials. This path incurs separate API billing
  and is reserved for development, testing, or deterministic replay.

---

## Artifact Checks

Shareable: `editor-facing.csv`, `editor-review-guide.md`, `weekly-review.md`,
`weekly-review.html`

`pilot-status.json` is the readiness starting point.
The current pilot target is 100 editor-facing Weekly Report Rows.
That target is a floor, not a ceiling.
A run produces every eligible candidate, so the row count is higher than the target when the sources supply more.
The ceiling is the configured `pilot_row_max`, which `readiness_basis` reports in every run.

In recurring mode, a shortfall from the historical 100-row target is not a readiness blocker.
If fewer than 100 rows are produced, inspect `readiness_basis.shortfall_reason`, `run-summary.json`, and `source-runs.json` before calling it a problem.
A 100-row shortfall caused by source scarcity, skipped sources, configured-source limits, or hard exclusions is an honest capacity finding, not an app crash.

`run-summary.json` reports `row_cap_dropped_count`, and `pilot-status.json` repeats it as a caveat.
A value above zero means the `pilot_row_max` cap discarded that many eligible editor-facing rows, so the sheet is not the full set of eligible candidates.
Raise `pilot_row_max` to recover them.
The count uses the same hidden-visibility filter as `editor_facing_count`, so the two numbers share one unit.

Internal: `pilot-status.json`, `run-summary.json`, `source-runs.json`,
`source-mentions.json`, `scouting-candidates.json`, `internal-exclusions.json`,
`russian-edition-checks.json`, `russian-review-queue.csv`,
`candidate-history.json`, `proposed-corrections.json`, `memo-queue.json`,
`source-proposals.json`, `suppressed-candidates.json`, `hidden-by-rule.json`,
`workflow-log.json`

`candidate-history.json` is absent when the output directory is the state
directory and that state keeps Candidate History one file per book
(`candidate-history/`, #1073).

`workflow-log.json` contains a sanitized stage-by-stage diagnostic log with
stage status, safe artifact references, and terminal outcome.

---

## Codex Capture and Replay Path

For a fresh weekly scout, the workflow is:

1. `prepare-codex-extraction --config configs/current-weekly-pilot-live.json --live` fetches configured source pages, writes page captures, and emits `codex-extraction-worklist.json`.
2. Codex extracts Source Mentions from each captured page and writes extraction artifacts under the filename given in each worklist item's `expected_extraction_file` field.
   Each artifact carries an `entries` array inside an identity envelope: `source_name`, `source_url`, `operation_id`, and `capture_sha256`, all copied exactly from the work item.
   The envelope is not optional. An artifact that omits any of the four is refused whole as `identity_mismatch`, and every grounded mention inside it is discarded.
   The work item's `extraction_contract` states this shape under `required_output_shape` and `required_top_level_fields`.
3. `finalize-codex-run --output-dir runs/current-weekly-pilot-russian/ --live-config configs/current-weekly-pilot-live.json` scans for capture and extraction artifacts and generates `codex-replay-config.json`.
4. `run-mvp --config runs/current-weekly-pilot-russian/codex-replay-config.json` ingests the Codex-produced extraction artifacts into the normal pipeline.

Sources with both a capture file and an extraction artifact become `codex-extraction-replay` entries. Source runs in the Codex-operated path report `collection_mode=codex-capture`. `run-health` emits `source_codex=` diagnostics for Codex-operated sources.

### Annual and monthly sources are collected on Sundays (#1057)

The weekly run captures and extracts a source whose `check_frequency` is `annual` or `monthly` only when the run date is the collection weekday: Sunday, unless the live config sets `collection_weekday`.
The rule is by run date, so a manual run and a timer run on the same day agree.
Weekly and daily sources are collected on every run.
On the other days the source is not fetched and gets no work item; its source run has status `not-due`, with `next_due_date` and `carried_mention_count`.
It is counted under `not-due` in `capture_source_outcome_counts`, and it is never a failure, a skip, stale evidence, a shortfall or a reason for attention, so the editor sees no source problem for it.
Its books stay as they are: the pipeline carries the source's last collected list forward from `scheduled-source-lists.json` in the state directory, re-dated to the run date, so their signals, priority and update delta are those of an unchanged list, and the next Sunday's collection of the same list is not news.
Only a collection that reported mentions replaces the recorded list: a failed or empty Sunday keeps the last good list, and a shorter list replaces it.
A source with no recorded list yet falls back once to its mentions in the previous run's `source-mentions.json`, which this run then records; a source onboarded during the week has nothing to carry (`carried_mention_count: 0`) until its next Sunday.
A night on which no source is due extracts nothing and still carries the books forward.
The annual source audit runs on every run and reads configuration only.

### Artifact freshness

`finalize-codex-run` does not accept an artifact only because the file exists.
It compares the artifact's `operation_id` and `capture_sha256` against the work item, and records the verdict as `artifact_freshness` on the generated source entry:

- `fresh`: the artifact answers this operation and this capture. Counted in `ready_sources`.
- `reused`: the artifact is bound to an earlier operation or an earlier capture. Counted in `reused_sources`.
  The source still feeds the pipeline, by operator decision, but it reports `status: reused` in `source-runs.json` rather than `collected`.
- `unbound`: the work item claims no identity, which is what a standalone `prepare-codex-extraction` produces. There is no evidence either way, so nothing is claimed. Counted in `ready_sources`.

A run with any reused source reports `codex_workflow_health: stale-evidence`, states the reuse share as its first entry in `major_warnings`, and counts it in `run-summary.json` as `reused_artifact_count` against `freshly_collected_count`.
The Russian editor summary states the reuse share before the candidate counts, because those counts then describe more than one run.

Before this binding existed, an artifact of any age satisfied readiness. On the 2026-08-05 run that let 14 of 22 contributing sources serve last week's evidence while every one of them reported as freshly collected.

### The extraction contract in each work item

Every work item carries an `extraction_contract` block. It is generated from the
source configuration by the same code that builds the agent-extraction model
prompt:

- `required_output_shape.mentions[0]` - the field-by-field shape of one mention,
  including family-specific fields such as `underlying_work_type`,
  `publication_status`, `candidate_category`, `current_rank`, and `weeks_on_list`.
- `required_fields` - the fields an extraction must always supply. Fields outside
  this list stay conditional on what the page shows.
- `rules` - the family-specific extraction rules, plus two capture-binding rules:
  extract only from the named capture file, and confirm that every `evidence_url`
  is either the configured source page URL or a link present in that capture's
  `html` field.

Treat the contract as binding. Most of it is enforced: an invented evidence URL,
a changed `signal_type` or `signal_strength`, and any missing family-specific
field (`underlying_work_type`, `publication_status`, `candidate_category`,
`current_rank`, `weeks_on_list`, `isbn`, `source_week`) all cost the mention, and
a source whose every mention is dropped reports zero valid mentions (Issue #375).

The enforcement is not one-for-one with `required_fields`, so do not read a
passing run as proof the contract was met:

- The collector's raw check accepts any one of `source_url`, `evidence_url`, or
  `item_url` where the contract asks for both URL fields, and it treats `authors`
  as satisfying `primary_authors`.
- Those base fields are checked on the Codex path only. Agent extraction defaults
  a missing `signal_strength` or `evidence_note` instead of dropping the mention.

### Partial lists (#839)

Some checks used to refuse a whole list when one row failed: the artifact gate's provenance and exact-list metadata checks, the BookMarks required-field rule, and the Audible and Amazon chart rule that every rank must be present.
By operator decision on #751 (option C), a list keeps the rows that pass when they are at least 80% of the extracted rows.
The failing rows are dropped with their reasons; below 80% the list fails as before.
Every kept row passed the same checks it would face alone, so no unverified row reaches the editor.

- The gate publishes a partial artifact without the refused rows and records them under `validation_dropped_entries` with `validation_extracted_count`.
  Codex never writes these fields; the gate removes any it finds, and publishes only the list it validated (`entries`, not a second `mentions` alias).
  When the collector drops more rows from such a list, the 80% rule is checked again over all drops (`partial_list_below_threshold`).
- A chart may miss only the ranks its dropped rows claimed. A rank the extraction never returned, or a rank given twice, still fails the chart.
- `source-runs.json` lists the dropped rows in `drop_reasons` and marks the run with `debug_metadata.partial_list` (`kept_rows`, `extracted_rows`).
- The run names the source as a row-count departure with rule `partial_list`: in the run caveats, in the review's source block («Необычное число книг: … — оставлено 19 из 20 найденных»), and in the completion message.
  A partial run is not recorded as the list's usual size in `source-row-history.json`.

Envelope failures such as `identity_mismatch`, and an ambiguous helper-metadata sidecar match, still refuse the whole artifact.

## API-Backed Real Extraction (Explicit Opt-In)

API-backed real extraction remains available as an explicit opt-in for development,
testing, and live model verification. It is **not** the default operator path.

To use the API-backed path, configure a source with `extraction_method: agent-extraction-live`,
`enable_live: true`, `extraction_backend: real`, `enable_real_backend: true`,
and `credential_env: AUTOSCOUT_AGENT_EXTRACTION_API_KEY`.

---
