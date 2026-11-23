# The editor's one-request path

Part of the [operator workflow](../current-weekly-operator-workflow.md).

## Russian One-Request Path (Primary)

The blessed editor workflow for normal weekly operation:

**Editor says** (any of these or semantically equivalent Russian phrases):

- «Запусти еженедельный поиск книг»
- «Запусти поиск»
- «Запускай поиск»
- «Ищи книги»

**Agent does**:

```
./scripts/run-weekly
```

The launcher (`scripts/run-weekly`) resolves a usable project Python
interpreter automatically — it prefers the project-local `.venv/bin/python`
when executable and falls back to the host `python3`. It execs the
canonical `autoscout run-weekly` command with the correct config, output,
and state directories relative to the repository root. The agent does not
need to spell `python` or know which interpreter the project uses.

The agent then presents concise Russian progress, warnings, and a completion summary, and automatically opens the generated local HTML review.

The enforced capture and extraction timing limits for this path are documented
in [Weekly source-processing runtime budget](../weekly-runtime-budget.md).

### Auto-completion requirements

After the scan, the agent MUST automatically complete all routine safe follow-up
in that same request:
- Validation/health check
- Rerender the review
- Canonical persistence (state files committed and pushed)
- Opening/refreshing the local review HTML

The agent must NOT stop merely because canonical changes are unsaved or because
a safe routine step remains.

If continuing requires a consequential editorial/state decision or is unsafe,
the agent gives exactly one recommended next action in plain Russian and asks
one concise question for the minimum confirmation. It must NOT end only with
"data not changed" or diagnostics.

### Validated same-operation retry (issue #315)

When a weekly workflow fails after capture (e.g. during Codex extraction),
the output directory already contains validated workflow-owned artifacts:
capture files, extraction worklist, source runs, resume marker, and
sanitized diagnostics.

A safe retry of the same current operation recognises these artifacts:

- **Before repository preflight**, the workflow validates that the
  output directory holds a complete current operation for the effective
  config and date (source partition, capture hashes, worklist binding,
  precollected evidence).
- Only validated workflow-owned files — exactly the capture,
  extraction, precollected, and fixed artifacts from the worklist — are
  exempted from Git dirty checks during preflight and update.
- **Unrelated files, even inside the output directory, still block**
  the retry.  Stale, mismatched, or incomplete operations also block.
- **Prior diagnostics are preserved**: the original failure evidence
  (capture, Codex, source-processing entries) survives through
  subsequent preflight blocks, with each new failure appending its own
  sanitized stage entry and exactly one terminal record.
- The editor-facing Russian message distinguishes a blocked
  retry ("Запуск поиска заблокирован …") from a failed result
  persistence ("Не удалось завершить сохранение результатов …").
- No destructive recovery (reset, stash, clean) is ever performed.
  The exemption is read-only and exact-path; it only affects the
  dirty-check in preflight/update.

### Normal editor output scope

Normal Russian completion output contains editorial facts only:
- New books, Updated books counts
- Description coverage (validated / total, requiring verification)
- Source problems and caveats (named skipped or failed sources)
- Review availability
- Whether the editor must act

Developer diagnostics are **hidden from normal editor output**:
- Full automated test counts
- Commit hashes and identifiers
- Branch and remote names
- Push details and working-tree cleanliness

Workflow-generated diagnostics belong in sanitized `workflow-log.json`.
Such facts may be shown only when the editor explicitly requests them.

### Review format contract (#278)

The Markdown review is the tabular report and includes the localized Visible Risk status.
The HTML review intentionally omits a duplicate risk-status detail row.
When an editor expands an HTML candidate, it keeps the compact risk details for category, rationale, comment, and evidence.
This difference is intentional and does not remove the risk status from the Markdown report.
With `editorial_fit_ordering` on in the weekly config, the books of one date are listed by the «Оценка» column (fit score, 0 to 4), and «Без решения» is one list by that score with no date sections (#1105, #1106); priority-D books come last in both, whatever their score.
The order changes only when a book of the review has a recorded fit score; with no recorded score the review keeps its usual order and «Без решения» stays grouped by date, even with the flag on.

### Earlier edition decisions

The review line «Другое издание» shows decisions and notes on linked historical records. See [candidate matching](../candidate-matching.md) for the matching rules.

An undecided card is left out of the review when every decision on its linked records is «Уже известно» or «Не подходит» (the operator's decision of 2026-11-16).
The link must name the same book firmly: the same author by given name and surname, and titles that are equal or tied by title evidence. A link through a missing author, a shared surname alone, an initial, one title containing the other, or the same title before the colon with different subtitles (the volumes of a series) stays a hint and hides nothing.
The run's own counts of new and updated books still include a card left out this way, as they do a card the editor hid herself.
One linked record she marked «Интересно», «Наблюдать» or «Нужен анализ» keeps the card, with the line above: a new edition of a book she wants is news.
The card she decided on names the editions left out under «Другие издания», the review header counts them, and `hidden-from-review.json` lists each with `hidden_by: decided_other_edition`.
Nothing is written to Candidate History and no decision is copied, so dismissing a wrong link (`probable-duplicates --dismiss`) or changing the decision brings the card back on the next render.

### Identity correction as a confirmation boundary

Identity correction (merging duplicate Candidate identities) is a confirmation
boundary. The correction payload (exact source identity keys and canonical
identity key) is prepared outside canonical state. The agent must:
1. Run `merge-candidate-identity --dry-run` (zero mutation — no state, Git,
   network, or model side effects before confirmation).
2. Present one Russian confirmation question that includes:
   - **Every** exact source identity key (do not truncate).
   - The **canonical identity key**.
   - The **canonical title and author display** (editor-facing book identity).
   - How many references will be rewritten and what is preserved (feedback,
     visibility, history).
   - The chosen canonical description with its retained provenance and evidence
     whenever the merged records disagree about the description.
3. Only after confirmation, apply the same correction and automatically
   rerender/finalize.

This does NOT relax the existing two-phase feedback/profile protocol.

**Conflicting generated descriptions (#559)**: two duplicate records can each carry an independently generated Russian description and its own `description_provenance`.
The merge never chooses between them on its own.
The correction artifact resolves the conflict explicitly with a `description_resolution` block:

```json
"description_resolution": {
  "canonical_source_identity_key": "<one of the merged identity keys>",
  "observed_descriptions": {
    "<identity key>": "<the exact content_description the editor was shown>"
  }
}
```

The named record's description and its provenance become canonical verbatim.
The provenance is only retargeted to the canonical identity key, so it keeps describing the exact text it was generated for.
`observed_descriptions` must name every merged record that currently has a description, with the text repeated verbatim.
Only surrounding whitespace is ignored.
`observed_description_provenance` must name every merged record that currently has a `description_provenance`, repeated field for field, because the provenance is half of what the editor approved.
A missing, extra, stale, or incompatible entry fails the whole correction while the plan is still being computed, so no state file is written.
A chosen provenance whose own `content_description` names different text is refused.
Without the block the merge still refuses conflicting durable content.

The merged record keeps the durable Candidate ID of the canonical identity, and every Candidate ID the merge retires is redirected onto that survivor in `candidate-identity-aliases.json`.
Stored aliases are never rewritten, so a later capture under an old title still reaches the surviving record and its editor decision.
Every surviving reference to the canonical identity is projected onto the merged history record, so no retained row keeps an old title, durable ID, or description under the new identity key.
The description-attempt logs of both records are combined.
Two different attempts claiming one `operation_id` follow the `description_resolution`: the chosen record's attempt is kept and the others are preserved in `description_merge_resolution.discarded_description_attempts`, and the dry run names each one (#1023).
Snapshot rows show the merged history record's attempt log.
Without the block, or when the chosen record has no attempt for that operation or carries two different ones, the merge refuses instead of merging them away.
The block must choose a record with a description, so two records that both lack one and conflict only in their attempts still cannot be merged.
A retained reference never keeps provenance for text it no longer shows, so a chosen description without provenance leaves the reference without one either.
One lock on the state directory covers planning and publication together, so two corrections on the same weekly state run one at a time and neither plans from state the other is replacing.
Every planned file is published as one bundle, so a failed write restores all of them and the same correction can simply be run again.

**Description enrichment**: after the pipeline and before the final review, the workflow runs the bounded grounded-description stage for every visible eligible candidate whose Russian Content Description is blank, the standard fallback, invalid, or otherwise unacceptable.
Sources whose captures contain only bibliographic and signal metadata declare `metadata_only: true` in the weekly portfolio (#868).
This optional boolean defaults to false and survives capture/finalize into replay configuration; it is not inferred from extraction notes, source names, or list structure.
The flag is set only on chart and ranking sources whose captures carry no per-book text (bestseller and audiobook rankings, the Independent Press CSV); award, review, and curated-list sources stay unflagged, because their captured pages can carry book prose that legitimately grounds an extraction description.
At Source Mention intake, any extraction-time Content Description from such a source is cleared with a bounded reason, leaving the candidate at the standard placeholder for description enrichment.
A substantive description from another source or an already saved description is preserved.
The shared deterministic validator also rejects descriptions consisting entirely of title-restatement or chart-position templates; mentioning a position within substantive Russian prose remains valid.
The stage also admits valid-looking visible candidates whose provenance is exactly `codex-generated` because that provenance remains visibly unconfirmed.
Valid `grounded` and `backfilled` descriptions are skipped.
Every `editor-corrected` description is skipped and is never overwritten.
The stage also admits eligible historical visible candidates without changing their weekly update delta.
Accepted descriptions preserve the exact durable Candidate ID, canonical identity key, provenance, applied date, and ordered evidence URLs in Candidate History and the current report.
A failed attempt preserves a valid prior `codex-generated` description and its provenance byte-for-byte while recording a bounded reason.
Ambiguous identity, unsupported evidence, copied prose, and budget non-admission fail closed.
Blank, fallback, and invalid descriptions may remain or become the standard fallback under the existing rules.
For issue #617, terminally shortened durable titles are repaired only for discovery and grounding comparison at read time.
Candidate History and review display remain unchanged because the missing full text cannot be reconstructed safely.
An unusable recovered title term is omitted rather than sent as a weak exact phrase.
The stage reports applied, skipped, failed, and ambiguous classifications plus a separate still-fallback final-state count with bounded candidate IDs and reason codes.
The stage marks attention required only for current-run failed or ambiguous events, not for a retained still-fallback backlog with no such events.
Structurally invalid enrichment input returns `input_failed` with a bounded reason code, while an explicitly disabled enrichment stage remains a quiet skip.
Structural enrichment-input failure stops the workflow before post-enrichment refresh, review rendering, health validation, and repository finalization and requires diagnosis.
The weekly grounded budget is four seconds per eligible new or updated candidate, clamped to 900–2700 seconds; previously seen candidates do not raise it. Adjudication admits that same eligible count, clamped to 120–360 candidates, with its separate 5400-second time budget unchanged. Standalone and forced description generation keep their fixed budgets. Budget and candidate-limit deferrals are reported separately from failures.
Each admitted candidate has a two-part bound: up to 10 seconds for discovery, followed by a post-discovery 90-second candidate work envelope for source fetching, challenge retries, and synthesis.
Stage admission reserves both the 10-second discovery bound and the complete 90-second candidate work envelope.
A candidate that cannot reserve both parts remains waiting with reason `not_attempted_stage_budget`.
Each eligible candidate receives at most one discovery HTTP request, and at most three source-fetch HTTP requests in total, including challenge retries across all discovered pages.
Every actual request checks the monotonic deadline for its current phase immediately before it starts.
A discovery request uses the smaller of the 10-second inactivity timeout and the remaining discovery budget.
A source request uses the smaller of the 10-second inactivity timeout and the remaining post-discovery candidate envelope.
Host-rate-limit waits and challenge retries do not start after the relevant deadline expires.
Executor queue wait before a worker starts a candidate is outside that candidate's timing and work envelope, but still consumes the overall stage wall-clock budget.
The explicit client-side vendor-rate-limit wait is also outside candidate timing and the candidate envelope, but still consumes the overall stage wall-clock budget.
The client/stage limiter keeps candidate timing accurate, while the loopback adapter applies an independent defense-in-depth floor for manual, concurrent, or other callers and may add a second wait when needed.
The separate small discovery cap prevents search or query latency and timeout stacking from starving source fetching, synthesis, and later candidates.
The trade-off is that unusually slow discovery is refused earlier instead of receiving the full candidate envelope.
This trade-off is intentional because protecting later candidate admission gives the bounded stage a more predictable and useful result.
Ordinary description-stage failures are reported separately from source collection and repository persistence failures, while review rendering, health validation, and finalization still run when canonical report state is coherent.

Author ranks and honorifics (#869) are stripped only for grounded-description author matching, with at least two name tokens retained. Durable Candidate identity keys, IDs, stored author displays, and Candidate History are unchanged.
When every fetched page lacks matching author evidence and an ISBN-proven page names primary authors, the candidate outcome in `grounded-description-stage.json` carries `likely_author_typos`.
Each entry records `stored_authors`, `printed_authors`, and the ISBN-matching page's `url`; the parent outcome carries `candidate_id` and `identity_key`.
This advisory appears for accepted and refused outcomes, does not change the gate verdict, and is not copied into canonical state or the editor review. Any correction still requires the identity-correction confirmation protocol in `AGENTS.md` and `docs/agents/contracts/identity-correction.md`.

Description latency and caveats (#865, #759) are refreshed by the same post-enrichment pass as description coverage, after both description stages finish.
`grounded-description-stage.json` (also its `report`) and `description-adjudication-stage.json` carry `call_latency`: `median_seconds`, `max_seconds`, `call_count`, and `timeout_count`.
Synthesis latency uses the existing per-candidate synthesis timing; candidates that never reach synthesis have no latency sample.
The grounded timeout count includes every `candidate_budget_exhausted`, including exhaustion before synthesis, because the whole description attempt was unfinished.
Adjudication latency measures each adapter call, excluding citation verification, and includes first calls, deeper calls, and timeout retries.
The run summary retains both aggregates in `description_call_latency.synthesis` and `.adjudication`; an empty latency sample has null median/max and zero calls.
The slow-Codex caveat appears when grounded budget exhaustions plus adjudication timeouts exceed 20% (`SLOW_CODEX_TIMEOUT_SHARE`) of grounded attempted candidates plus adjudication calls, including retries.
Grounded unchanged-evidence and unattempted rows are excluded from that denominator.
This threshold allows isolated slow calls while explaining a run that loses more than one in five attempts, even if a retry later succeeds.
The source caveat names each source when more than 50% (`SOURCE_PLACEHOLDER_SHARE`) of its new books end with a placeholder: a majority loss rather than an occasional missing description.
Only `update_delta=new` books count; each book counts once under every source that cites it, using the existing `fallback_by_source` rules.
Books awaiting publication (#791) count toward neither side of that share, since a book that is not out yet is no source's loss; the headline «Описаний на русском: X из N» leaves them out of `N` too (`coverage_denominator`), and they are reported on their own line «Ещё не опубликованы: N» (#944).
These Russian lines are stored in `description_caveats`, included in pilot caveats, and shown in completion and review output; latency remains operator diagnostics.
Refreshing replaces these derived caveats, so recovering the descriptions removes a source caveat.

Each timed-out adjudication call gets one exact retry after all first-pass candidates finish, only while a complete 240-second call still fits the stage budget.
A timeout in the deeper search retries that deeper work item, preserving the first call's verdict unless the retry improves it.
A second timeout is not retried again and writes no refusal or description artifact to Candidate History.
Fresh weekly candidates already remain eligible after grounded budget exhaustion or an adjudication adapter failure; standalone grounded generation now also bypasses its ordinary failure cooldown for budget exhaustion.
Depth-repair accounting is unchanged: a timed-out repair attempt counts as an ordinary failed attempt, which keeps stub rotation under `--limit` fair.

A description-results bundle publication failure that rolls back successfully returns `persistence_failed` with reason `description_persistence_failure` and stops the workflow with intervention required at failed stage `description-persistence` before review rendering.
A recovery failure returns `recovery_failed` with reason `description_persistence_recovery_failure`, retains recovery backups, and stops the workflow with intervention required at failed stage `description-recovery` before review rendering, health validation, or finalization.
The recovery-failure intervention recommends Dima diagnosis instead of repeating the weekly search.
The completion summary reports description coverage: validated and total counts, and how many still require verification.
Description gaps are enrichment warnings, not collection failures - the review remains available when canonical description results were published.

**Completion summary** always reports New books, Updated books, Russian
description coverage (validated / total, and any requiring verification),
source problems, and whether editor attention is required. A healthy
zero-New-book week is described as successful.

**Automatically opens or refreshes the review.** Normal editor chat presents a friendly open/refresh action or link. If opening fails, it offers that friendly action without exposing a local path.

### Clean editor-chat output (#284)

The clean-output boundary applies to weekly completion and confirmed feedback,
profile-add, and profile-revocation responses. They contain no memory citations
or internal memory filenames, tool-control markup or raw Git directives (such
as `::git-stage`, `::git-commit`, or `::git-push`), and no absolute local
filesystem paths. Confirmed Level 1 learning still uses the exact heading
`Что AutoScout узнал`.

When the structured workflow log establishes successful or no-op canonical
repository finalization, the agent says plainly in Russian that results are
saved, without exposing Git mechanics. It does not claim remote persistence
without that evidence. If persistence fails, existing Level 3 handling applies:
a plain Russian explanation, exactly one recommended next action, and exactly
one concise minimum-confirmation question, with no raw directives or paths.

---
