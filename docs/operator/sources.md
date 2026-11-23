# Source proposals, probes and source health

Part of the [operator workflow](../current-weekly-operator-workflow.md).

## Source Proposal And Source Research Workflow

A Source Proposal is a researched or editor-suggested source that is not a configured source and cannot create Scouting Candidates until deliberately accepted into configuration. Use `ingest-feedback` only as fallback for a returned CSV or source-ideas CSV; it is not the normal conversational path. Do not add filler rows, topic-only candidates, or unconfigured web discoveries to satisfy the target.

For editor-suggested sources or agent-researched sets such as Scandinavian nonfiction sources, evaluate and record every evaluation criterion: source URL and provenance, signal family, Access status, Stability, Adult-nonfiction fit, Metadata-only safety, Reason and caveats. Use `python -m autoscout source-proposal add` for manual proposals. Adding a proposal is not hidden configuration: until a proposal is accepted into `configs/current-weekly-pilot.json`, `run-mvp` must not collect from it.

When actually onboarding an accepted source into configuration, follow the canonical step-by-step checklist in `docs/source-onboarding-runbook.md` (feasibility evidence → Source Proposal → portfolio/overlay configuration → replay fixtures first → validation → probation entry → commit).

---

## Source-Gap Discovery

When the editor requests source-gap discovery without a URL, run `./scripts/add-source discover`.
Use `./scripts/add-source discover --signal-family '<family>'` when one supported signal family is requested.
The offline command needs no URL.
The Search Optimization Review is optional.
Without `--review`, discovery reads `runs/current-weekly-pilot-russian/search-optimization-review.json` only when it exists.
When absent, configured sources are labelled `not-reviewed` and no allocation recommendations are used.
Use `--review <path>` as an explicit override; that path keeps strict file and schema validation.
It resolves a `weekly_portfolio` reference relative to the supplied `--portfolio` path before validation and analysis, including overlay active state.
It reads the resolved configured weekly portfolio and the optional Source Candidate Ledger.
It reports each canonical signal family with configured and active coverage, source allocation labels, and deterministic weakness reasons.
Unclassified sources are surfaced once in `sources_without_signal_family` with bounded identity fields and a missing or unsupported reason.
A family with no active source, one active source, or a strict majority of `reduce` allocation labels is weak when metadata is sufficient.
The report proposes only URLs grounded in existing repository research or requirements.
It silently omits configured URLs and reports only ledger-known exclusions.
It does not inspect or report country, market, or language gaps.
It has no network, model, configuration, proposal, mention, candidate, history, or weekly-state side effects.
Successful offline reports have top-level status `success` and return exit code 0.
A report with status `insufficient_metadata` returns exit code 0 and suppresses weakness conclusions, candidate proposals, and probing.
Supply canonical signal-family metadata or a resolvable weekly portfolio reference before relying on exact coverage.
Input, configuration, review, and ledger failures have status `input_error` and return exit code 2.
A structured live-probe exit code 2 has status `probe_error`.
Unsupported options such as `--url` are discovery input errors and return exit code 2.
The offline command stops after its JSON report.
Run `./scripts/add-source discover --probe` only when the editor explicitly asks to probe the remaining candidates.
The live mode computes coverage, configured URL filtering, and ledger exclusions offline before invoking the existing `./scripts/probe-source` launcher.
It passes `--keep-captures tmp/source-candidate-captures`, `--ledger data/source-candidate-ledger.json`, and `--feasibility-dir data/source-feasibility-drafts` to that launcher.
Retained captures stay outside Git publication in the ignored `tmp/source-candidate-captures` directory.
Live mode may write only the existing probe ledger and Markdown sidecar, retained accepted captures, and feasibility drafts.
Protected configuration and weekly/editorial state remain unchanged.
Request one tool-level network authorization for the full live invocation with this exact rationale: «Для проверки доступности предложенных источников нужен сетевой доступ. Разрешить запуск?»
The live mode propagates exit code 0 when any candidate accepts, exit code 1 when no candidate accepts and a reject or malformed result exists, exit code 2 for discovery, probe, command, input, or storage errors, and exit code 3 when all candidates defer.
If no candidates remain after filtering, live mode returns exit code 0 with a clear no-candidates status and makes no network call.
The live command stops after its probe.
After either mode, onboarding must wait until the editor explicitly chooses exactly one candidate, and that candidate must be accepted.

## Quick Source Health Check

To answer "which sources are dead, alive, or need fixing right now" without
running the weekly workflow, use the blessed launcher (resolves the project
interpreter automatically, like `scripts/run-weekly`):

```bash
./scripts/check-sources
```

Extra arguments pass through (`--replay`, `--output-dir <dir>`). Note that
`--replay` only stays offline for configs that declare fixture files; the
launcher pins the live config, so a deterministic offline probe is the raw
command against `configs/current-weekly-pilot.json` with `--replay`. The
equivalent raw command is:

```bash
python -m autoscout check-sources --config configs/current-weekly-pilot-live.json
```

In Codex, the editor phrase «Проверь источники» triggers this launcher via
the `docs/agents/contracts/source-health-check.md` contract (single sandbox-permission request, read-only, Russian
one-line summary plus the technical table).

This runs the same capture pass as `prepare-codex-extraction --live` —
including the wrong-target and expected-page-heading guards — but stops
before any extraction or model call, prints one verdict line per source
(`codex-captured`, `precollected`, `inactive`, `skipped`, `ignored`,
`failed` with the failure reason), and exits non-zero only when a source
failed. Captures go to a throwaway directory unless `--output-dir` is
passed; weekly state is never touched. `--replay` probes the configured
fixture files instead of the live pages (deterministic, offline).

The probe proves access, page identity, and page shape. Extraction-level
health (malformed model output, empty extraction, content drift) still only
shows in a full run; contribution health (sources that are alive but never
produce candidates) is the `source-lifecycle` metrics conversation.

## Probe Proposed Source Candidates

Use the blessed launcher to probe unconfigured Source Candidate URLs in one bounded live batch:

```bash
./scripts/probe-source --url https://example.test/list
```

Repeat `--url` for additional candidates.
Pass `--re-probe` only when an explicit fresh evaluation of durable ledger entries is required.
Use aligned repeatable `--name`, `--signal-family`, and `--proposing-context` values when operator metadata is available.
For a discovery report, pass each candidate's concise reason as its aligned `--proposing-context` value.
The command emits one structured JSON report containing every candidate, including malformed URLs and isolated request failures.

Each valid URL receives one HEAD request.
Only HEAD 200, 405, or 501 permits one plain GET.
The probe never falls back to GET after a denied or other failing HEAD.
GET and retained captures are bounded to 1,000,000 bytes.
The result is `accept`, `defer`, or `reject` with a reason code, HTTP evidence, heading, bounded entry count, and grounded samples when present.
The process exits 0 when any candidate is accepted, 1 when any candidate is rejected or malformed, and 3 when every candidate defers.

Captures are discarded by default.
Pass `--keep-captures <directory>` to retain bounded evidence for accepted candidates only.
Retained HTML uses the canonical capture sanitizer for CSRF, session, and analytics values while preserving book evidence.
The durable JSON ledger defaults to `data/source-candidate-ledger.json` and its readable Markdown sidecar uses the same stem.
Pass `--ledger <path>` to select another ledger location.
The path must be a new `.json` artifact or an existing version-1, version-2, or version-3 Source Candidate Ledger.
Legacy list ledgers, incomplete entries, aliases, invalid outcomes, duplicate identities, malformed history, protected state/config basenames, weekly/config/state directories, and symlink targets are rejected before network access or writes.
The Markdown sidecar is guarded by the same protected-file checks.
Each ledger entry records the requested URL, its ISO probe date, and the structured verdict.
The ledger is outside weekly state, is ordered deterministically, and is consulted before network access.
Without `--re-probe`, an existing URL returns its prior verdict with no network request or JSON rewrite.
With `--re-probe`, one fresh bounded probe replaces the current verdict even when the requested contract family or metadata changes, while preserving a flat chronological history of prior verdicts and current-source outcome counts.
The complete version-3 history and structural-diagnostic privacy contract is defined in `docs/source-onboarding-runbook.md`.

This operation does not modify configuration, weekly state, Candidate History, visibility, Editorial Taste Profile, Source Mentions, or Scouting Candidates.
The launcher performs no network call by itself.

## Monthly Source Discovery Nudge (#341)

The exact Russian editor trigger is: «Запусти ежемесячный поиск источников за YYYY-MM».

The agent runs exactly this blessed offline launcher command, replacing `YYYY-MM` with the explicit requested month:

```bash
./scripts/add-source monthly --operation-month YYYY-MM
```

`--operation-month YYYY-MM` is required and the monthly route does not infer a month from the clock.

The command reads the resolved weekly portfolio and overlays, the optional Search Optimization Review, the canonical Source Candidate Ledger JSON, Source Proposals, and the derived monthly history.

This command is the deterministic schedule target for an external or editor-controlled monthly cadence.

The repository scope intentionally contains no installed or active cron job, hosted scheduler, launchd job, GitHub Action, service, or runtime scheduling dependency.

Merging code alone does not activate a monthly nudge.

It calls the canonical Issue #340 `discover_source_gaps(..., probe=False)` analysis and publishes `data/monthly-source-discovery/reports/YYYY-MM/monthly-source-discovery.json` and `data/monthly-source-discovery/reports/YYYY-MM/monthly-source-discovery.md`, plus `data/monthly-source-discovery/monthly-source-discovery-history.json`, by default.

The public offline input and output overrides are `--portfolio`, `--review`, `--ledger`, `--source-proposals`, and `--output-dir`.
`--source-proposals` names a state directory's Source Proposals file, and the proposals are read through that directory's State Store (#1071).

The route always covers the full portfolio and does not accept `--signal-family`, `--history`, `--history-file`, `--proposals`, or `--probe`.

The default output is a local derived report directory ignored by Git, not canonical weekly, editorial, configuration, Source Proposal, or Source Candidate Ledger state.

The report separates new candidate URLs, previously banked candidate URLs, ledger-known rejected or deferred URLs, and current signal-family coverage gaps.

A banked candidate is only an unconfigured Source Candidate Ledger URL with outcome `accept`.

A ledger-accepted banked candidate remains reportable when portfolio metadata is insufficient.

A current eligible Issue #340 candidate that is not configured and is not ledger-known is new.

Monthly history is only a derived publication index.

It is never evidence or classification input.

Currently configured source URLs are never listed as banked candidates.

The rejected-or-deferred bucket contains every normalized Source Candidate Ledger URL with outcome `reject` or `defer`, including URLs outside the Issue #340 static seed catalog and URLs from families that are not currently weak.

The report shows the verified completed count of configured sources with accepted proposals and structured onboarding completion dates in the rolling window from the operation month and preceding 11 calendar months toward the goal of 12.

When canonical evidence is incomplete, the count is an auditable lower bound, `remaining_to_goal` is `null`, and the report names every configured URL without matching structured evidence.

When canonical evidence is complete, the count is exact and the numeric remaining goal is reported.

The report also has one nested accepted-proposal signal object.

It reports configured sources with matching accepted proposals and canonical `proposal_date` values in the rolling window.

This signal is not verified onboarding completion and never changes verified progress or `remaining_to_goal`.

The object separately reports accepted proposals without valid structured completion dates and configured sources without any matching accepted proposal.

Invalid or absent proposal dates are never presented in the in-window signal.

They remain in the accepted-proposal-without-completion group when completion evidence is absent.

The onboarding evidence state is `insufficient_evidence` when any currently configured source lacks an accepted proposal with matching URL identity and structured `onboarding_request.run_date` evidence.

A zero onboarding count is valid and is not an error.

Normal statuses return exit code 0: `new_candidates_available`, `only_banked_candidates_available`, `no_candidates_this_month`, and `insufficient_portfolio_metadata`.

Malformed inputs and invalid or partial derived publications return status `input_error` or `artifact_error` with exit code 2.

A valid retry for an existing operation month first validates that month's complete, canonical JSON and Markdown pair and its one matching history entry.

Malformed, partial, aliased, or tampered target artifacts fail closed with exit code 2 and are never overwritten.

The retry then recomputes the canonical semantic report evidence.

If the evidence is unchanged, it returns the existing identity, digest, and bytes without rewriting history.

If the evidence changes, it atomically replaces that month's JSON, Markdown, and single history entry.

A retry after replacement is byte-identical and idempotent.

The Evidence Digest and operation identity are deterministic from the explicit operation month and canonical semantic report evidence.

Irrelevant input formatting, irrelevant input fields, and local monthly history do not change them.

Normal reports return exit code 0 and input or artifact errors return exit code 2.

Publication uses the shared atomic bundle and lock helpers and fails closed if any per-month report pair is missing, extra, malformed, or not matched by exactly one history entry.

A later month checks that older report directories contain complete JSON and Markdown pairs and have matching history entries, but it does not re-render older Markdown.

A retry fully validates the target month's JSON, Markdown, history agreement, hashes, canonical rendering, operation identity, and Evidence Digest.

The route has no `--probe` capability, requests no network access, invokes no model or subprocess probe, and stops after report publication.

Publication destinations must not overlap any input artifact, including the ledger Markdown sidecar, and symlink or hard-link aliases fail closed.

It never writes the portfolio, overlays, Source Proposals, Source Candidate Ledger or its Markdown sidecar, Source Mentions, Scouting Candidates, Candidate History, Editorial Taste Profile, visibility, feedback, onboarding state, or weekly state.

It never accesses the network, invokes a model or probe subprocess, or runs a real scheduler.

A monthly report only informs a later explicit editor choice and does not accept, reject, add, retire, promote, or otherwise change a source.
