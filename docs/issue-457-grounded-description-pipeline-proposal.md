# Issue #457: Grounded Description Pipeline Proposal

> **Status update.**
> Issue #570 records standing editor approval for automatic recurring weekly application to eligible visible candidates.
> The recurring route uses the validated steady-state acceptance payload as its mutation gate and does not require a separate per-run dry-run approval.
> A separately invoked one-time bulk backfill remains subject to explicit dry-run approval.
> This document retains the proposed Part 2 design and data contract; it does not itself implement the pipeline.

## Purpose and boundary

Part 1 only marks displayed valid Codex-generated descriptions so that the editor can see their provenance.
Part 2 defines the recurring weekly route authorized by Issue #570 and the separately invoked one-time bulk dry-run route, whose approval boundaries are different.
Part 2 would replace the current ungrounded description workflow with a bounded, source-grounded research stage.
The existing one-source-family `grounding_contract` must remain byte-for-byte unchanged.
The new outer stage adds discovery, independent-source agreement, and evidence validation before it invokes or complements the existing source-family-specific behavior.
The outer stage does not grant the existing agent permission to fetch extra URLs.
The proposal does not change description wording, Candidate History data semantics, ranking, priority labels, independent signal weighting, runtime budget constants, models, profiles, or reasoning arguments.

This revision makes six original proposal decisions explicit.
The fixed first-15 cap is removed in favor of time-bounded resumable scheduling.
The generic budget outcome is split into distinct attempted and not-attempted outcomes.
Durable carryover and starvation-safe ordering are added.
Subject-bearing sources may use original languages, including German, French, Spanish, Czech, Italian, and English.
A verified published-title route is added without allowing arbitrary translations to bridge identity.
Issue #458 becomes a design and data-contract prerequisite for implementing Part 2 of this proposal.

## Editor decisions - 2026-08-15

Owner independence does not require distinct source families because different owners are the safety boundary, while source family remains useful observed metadata rather than an acceptance condition.

One individually verified source may produce a description when it passes the current title, author, classification, snippet, checked-date, and subject gates, and the editor must see the visibly marked single-source confidence status.

Identity or subject disagreement never falls back to a single source because discarding contradictory evidence would hide a safety failure instead of resolving it.

## Universal grounding contract

The description stage must define one universal description grounding contract for every accepted description.
The contract must require no title-only inference.
The contract must permit no outside knowledge.
The contract must use only URLs that were fetched and accepted by the stage.
A description must not be accepted unless one individually verified subject-bearing source passes the identity and content gates, or two to three independent subject-bearing sources agree on both the title and the author.
The existing one-source-family contract remains byte-for-byte unchanged for its existing sources and checks.
The outer stage adds the source agreement gate rather than changing the existing contract or treating a source family as sufficient evidence.
A source that contains only a title, a search result snippet, a navigation label, or a non-subject-bearing locator cannot satisfy the contract.
The description generator may synthesize only from bounded accepted evidence and must not add facts that the accepted evidence does not support.

## Identity gate

The stage must use the #434 identity normalization seam in `autoscout/identity.py`.
The work title comparison must use the subtitle-insensitive normalized title from `normalize_work_title`.
That normalization takes the text before the first colon, lowercases it, replaces runs of non-word characters with spaces, and strips the result.
Author comparison must use the existing conservative author display normalization and normalized identity text.
The accepted source set must share at least one normalized primary author pairwise with the target and with every other accepted source.
The stage must not use ISBN equality to bypass title-and-author agreement.
An ISBN may be recorded as supporting metadata, but it cannot authorize a description when the title and author agreement gate fails.

The stage must reuse the #443 durable `candidate_id` and alias-resolution seam to select the target record.
The resolver must map the requested work to one durable candidate record before any description is persisted.
An unresolved identity, a missing durable target, or more than one possible target must fail closed with an ambiguity reason code.
Description work must not create aliases.
Alias creation and correction remain separate identity operations with their own approval boundaries.

The matching algorithm must be exact after the existing normalization steps.
It must not add fuzzy similarity, stemming, translation, transliteration, token deletion, or semantic widening to the direct identity route.
A missing title or missing author must fail closed.
A normalized-title collision with different normalized authors must remain separate.
A same-author same-title collision with no available discriminator must remain ambiguous rather than selecting an arbitrary record.
Raw source title and author displays must be preserved as evidence metadata, while the normalized values are used only for the deterministic gate.
Every accepted source must pass the same pairwise title-and-author check against the target identity and against the other accepted sources.

## Multilingual grounding and publication titles

Source language does not need to be Russian.
A German, French, Spanish, Czech, Italian, or English subject-bearing source can ground a Russian description.
The Russian text is a translation or synthesis based only on accepted source evidence.
Source language must be recorded as metadata and must not be used as an identity shortcut.

A fetched bilingual source may explicitly carry both the original title and a translated or published title for the same work and author.
The direct identity gate must use the original title plus the author.
The translated or published title must never substitute for a missing original title in the direct route.
One bilingual page counts as one source rather than two sources.

The design must couple #457 to #458 through a shared, sourced publication-title evidence seam.
The seam must support two safe routes for an accepted subject-bearing source.

- The direct route requires an exact normalized original-title and author match.
- The verified published-title route requires an exact normalized published-title and author match only after #458 supplies provenance-bearing mapping evidence that explicitly links that published title to the candidate's original title and author.

A generated or marked rendering from #458 must never be identity evidence.
A merely similar title, a literal translation, a transliteration, a search snippet, or a model-inferred title must never bridge identity.
The #458 mapping source is identity evidence and is not automatically one of the two subject-bearing research sources.
The mapping source counts as a research source only when its fetched page independently contains sufficient subject-bearing text and has independent ownership.
Accepted subject sources may use the direct route or the verified published-title route.
Every accepted subject source must resolve through the same sourced title mapping to one durable candidate and must still agree on the author.
The independent-owner rule, subject consistency rule, single-source warning, and all fail-closed outcomes remain unchanged.

Without one individually verified subject-bearing source or two independent subject-bearing sources that pass one of these routes, a Czech or Italian candidate receives no grounded description.
After an attempted search, this outcome uses `fewer_than_two_agreeing_sources`.
This outcome must not be presented as broad Czech or Italian source coverage.
If the candidate was not attempted because of the stage budget, the only scheduling reason is `not_attempted_stage_budget` and the report must make no source-availability claim.

The #458 title-evidence and provenance seam is a design and data-contract prerequisite for implementing #457 Part 2.
This prerequisite exists because both features need one authoritative distinction among the original title, a sourced published English title, and a generated rendering.
It does not exist because Russian synthesis needs an English title.
The proposal recommends landing the #458 title-evidence and provenance seam before #457 consumes it.
Discovery and fetch evidence should be shared, while the English-title output and Russian-description output remain separate.
This design is stronger than accepting arbitrary English translations and does not weaken title-and-author matching.

## Source discovery, classification, and preference

Source classification and preference selection must happen before fetching source content.
The stage must deterministically classify each discovery result from its URL host and path together with maintained source policy metadata.
The classification evidence must record the host, relevant path rule, policy metadata version or identifier, source kind, and classification result.
The preferred order must be review page first, author official page second, and publisher page third.
Identity eligibility must be evaluated only after the selected page is fetched and its own title and author metadata are extracted.
A URL cannot pass identity eligibility from its host, path, search result, or discovery label alone.
Unknown or unclassifiable results must not count as sources.
Search result snippets must remain discovery-only and must never count as sources or description evidence.
A search result may identify a page to classify and fetch, but its snippet must never be used as description content.

Accepted URLs must be distinct after canonical URL normalization.
Distinct URLs must also have independent owners.
Two URLs controlled by the same publisher, syndication network, author domain, or clearly copied owner must not be counted as two independent sources.
The stage must record the resolved owner identity and the exact independence reason `distinct_canonical_urls_owner_ids` for each accepted set.
If owner independence cannot be established, the source must be excluded or the result must fail closed.
The stage must record source kind, classification evidence, canonical URL, owner evidence, source language, title-evidence route, and the bounded fetch outcome.

### BookMarks handling

The description pipeline should fetch and follow the per-book BookMarks review URL when discovery exposes that URL.
The per-book review page is the useful subject-bearing source, while the BookMarks index is only a discovery surface.
Changing the BookMarks index collector to preserve or extract the per-book outbound URL from the already captured index is a separate focused collector repair.
That collector repair must not fetch per-book pages or define per-book collector fan-out.
Fetching the per-book review is solely the description stage's job.
The BookMarks collector must not fan out beyond its current fixed listing capture and extraction budget.
Unbounded per-entry crawling would change collection behavior, extraction cost, failure isolation, and reproducibility.
The collector repair must remain limited to preserving or extracting an outbound per-book URL from the existing index capture.
Until that repair is approved and implemented, the description stage may use only a per-book URL already present in accepted discovery metadata.

## Description and prominence separation

Prominence must be a separate structured field and a separate HTML and CSV line.
Prominence must never be blended into `content_description`.
Prominence must not be used for ranking in this proposal.
The field should store typed claims such as `bestseller`, `debut`, `prize`, and `mentor_or_backing`.
Each claim must include exact evidence URLs, source kind, checked date, and bounded display text.
Each claim must pass the same title-and-author identity gate as a description source.
Bestseller and prize claims should prefer official lists, official award pages, or equivalent authoritative list evidence.
Debut and mentor or backing claims should require direct attributable evidence from the author, publisher, official organization, or another clearly attributable source.
The HTML line should identify the claim type and link the exact evidence URL.
The CSV line should contain the bounded display text and must keep the evidence URL and claim type separately available.
A claim with conflicting title, author, date, market, or scope evidence must fail closed.
A conflict must not be resolved by choosing the more prominent source or by model judgment.
A claim with unclear time or market scope must be recorded as unresolved rather than presented as universal prominence.

## Time-bounded resumable scheduling and budget

The stage must use time-bounded, resumable scheduling rather than a fixed number of new candidates.
Evidence from 11 measured operations shows that only 2 operations had 15 or fewer new candidates.
The median measured operation had about 37 new candidates.
The maximum measured operation had 155 new candidates.
Therefore, 15 is not a valid steady-state cap.
The proposal must not replace 15 with a new fixed cap of 155.

Each operation must build a worklist from all eligible new candidates plus durable carryover that remains eligible for retry.
The scheduler may admit work only when the remaining monotonic stage budget can hold the complete per-candidate envelope.
A candidate that cannot be safely admitted before the stage budget ends must not be started merely to improve the count.
The stage must retain all non-admitted work as durable carryover for the next operation.
Candidate 16 and every later eligible candidate must therefore remain visible in the operation outcome rather than being silently omitted.

The stage must have its own independent monotonic deadline of 900 seconds.
The stage must not borrow time from extraction.
The stage must never extend the extraction segment.
The stage must preserve finalization even when the description stage is incomplete.
Each admitted candidate has one complete 90-second envelope covering discovery, source fetching, challenge retries, and synthesis.
Each eligible candidate receives at most one discovery HTTP request.
Each candidate receives at most three source-fetch HTTP requests in total, including challenge retries across all discovered pages.
Every actual HTTP request checks the same monotonic candidate deadline immediately before it starts.
Each request uses the smaller of the 10-second inactivity timeout and the remaining candidate or stage budget.
Host-rate-limit waits and challenge retries must not start after the candidate deadline expires.
The stage may use at most three workers.
The scheduler must check the remaining stage budget before admission and reserve enough time for the complete 90-second candidate envelope.
Budget exhaustion must record a bounded reason code and continue finalization.
The recurring weekly route applies automatically after the Issue #570 standing approval, while the separately invoked one-time bulk route continues to require its exact dry-run approval.
Budget exhaustion must not create a partial accepted description.

The stage must expose four distinct editor-facing outcome classes.

- `grounded` means that the candidate was admitted, the universal grounding contract passed, and a description was accepted from two to three independent subject-bearing sources.
- `single_source` means that one individually verified source passed every current identity and content gate, and the description is visibly marked as single-source.
- `attempted_no_description` means that the candidate was admitted and the attempt ended without an accepted grounded description.
- `not_attempted` means that the candidate was never admitted because the remaining stage budget could not safely hold its complete envelope.

A `not_attempted` candidate must have reason code `not_attempted_stage_budget`.
It must remain queued for a later operation.
It must have no identity verdict and no source verdict from this operation.
An `attempted_no_description` candidate whose own envelope is exhausted must have reason code `candidate_budget_exhausted`.
An attempted candidate that completes its checks must retain the existing source and identity refusal reason codes such as `no_results`, `title_mismatch`, or `fewer_than_two_agreeing_sources`.
The scheduler must not replace a completed attempt's concrete refusal reason with a generic budget reason.

The HTML and CSV review must show every non-admitted candidate as `Поиск источников еще не запускался`.
The review summary must count `not_attempted` candidates separately from attempted refusals.
The non-admitted candidates must be carried into the next operation.
A single-source result must be shown separately as `⚠ Описание по одному источнику`.
An attempted refusal must be shown separately as `Источники проверены - описание не подтверждено` together with its concrete human-readable reason.
The two messages must never be rendered as equivalent or merged into one count.

Durable scheduling metadata must be stored outside Candidate History.
The queue entry must contain the durable candidate ID.
It must contain the queue entry operation and date.
It must contain the first seen date.
It must contain the latest scheduling Priority Label.
It must contain the attempt status.
It must contain the reason code.
The queue metadata must distinguish a waiting `not_attempted` entry from an attempted refusal and must support deterministic resume without recapturing already accepted evidence.
For the separately invoked one-time bulk backfill, the dry-run approval boundary must remain in force before any Candidate History mutation.
The recurring Issue #570 route may mutate Candidate History automatically after its validated acceptance payload passes the durable identity, evidence, and prose gates.

Scheduling order must be deterministic and starvation-safe.
The primary key is the oldest queue-entry operation and date.
The next key is the current Priority Label from A through D.
The next key is the earliest first-seen date.
The final key is the durable candidate ID.
Priority Label is the correct scarce-budget choice within the same waiting cohort.
Queue age must outrank Priority Label so that an older low-priority candidate cannot starve behind newer high-priority arrivals.
This scheduling order must not change the candidate's actual Priority Label or ranking.
It only chooses which eligible work receives the available description-stage budget.

The 439 Candidate History rows are a separate resumable one-time backfill operation.
The 344 rows with invented descriptions must be processed first.
The 95 rows with missing descriptions must be processed second.
The counts are proposal planning numbers and are not implemented by this issue.
Each backfill batch may contain at most 25 rows and may run for at most 1800 seconds.
The fixed 25-row batch size is an approval and report-size boundary, not a claim that every batch item will execute.
The batch worklist must apply the same admission rule as the steady-state scheduler.
A non-admitted batch item must remain pending with `not_attempted` status and `not_attempted_stage_budget` reason code.
An admitted batch item that exhausts its own envelope must use `attempted_no_description` status and `candidate_budget_exhausted` reason code.
Each batch must be resumable without recapturing or duplicating accepted evidence.
Each batch must first emit a dry-run report outside Candidate History.
Candidate History updates must happen only after Dima approves that exact dry-run report.
Approval must identify the batch, candidate IDs, proposed accepted URLs, provenance changes, claim changes, and failure outcomes.
A later report must not reuse an earlier approval token when its candidate set or evidence changes.
Production timing evidence must be reviewed after the initial batches before the batch size or limits are reconsidered.
No implementation may claim that the 439 rows, the 344 rows, or the 95 rows have already been processed.

## Accepted artifacts and persistence

A staging artifact may be accepted only when it contains research metadata and a description that passed the universal grounding contract.
Accepted staging artifacts must carry `research_sources` with exactly one URL for `single_source` or exactly two to three accepted URLs for `grounded`.
The URLs must be ordered deterministically, for example by source preference followed by canonical URL.
The artifact must include source kind, source language, owner classification, identity comparison results, title-evidence route, checked date, and bounded attempt outcomes.
For the verified published-title route, the artifact must include the provenance-bearing #458 mapping reference that links the sourced published title to the candidate's original title and author.
The artifact must not include rejected source prose.
It may include bounded rejection reason codes and safe URL or metadata identifiers.
A mapping reference without independently sufficient subject-bearing text must remain identity evidence only and must not be included in `research_sources`.

Canonical provenance must persist the same ordered URLs through the existing `description_provenance` and `evidence_basis` seam.
The persisted evidence basis must not silently reorder, truncate, or replace the accepted URL list.
The recurring stage must publish its stage artifact, queue, state Candidate History, output Candidate History copy, and current candidates as one rollback-capable bundle under a write lock.
Staging or replacement failure must restore every pre-existing destination byte and remove newly created stage and queue files.
Bundle recovery must use a trusted replacement path and verify every destination's original existence and bytes.
If recovery cannot complete, the operation fails closed with a distinct safe-artifact recovery error and retains recovery backups.
A publication failure that rolls back successfully must return a distinct `persistence_failed` status with reason `description_persistence_failure` without exposing exception text or filesystem paths.
A recovery failure must return a distinct `recovery_failed` status with reason `description_persistence_recovery_failure`, retain recovery backups, and expose no exception text or filesystem paths.
The weekly workflow must stop before review, health, or repository finalization after either failure, using `description-persistence` for a successful rollback and `description-recovery` for a failed recovery.
The recovery-failure intervention must recommend Dima diagnosis instead of repeating the weekly search.
The canonical provenance must identify the provenance class and the exact stage date.
Accepted source bodies must not be stored in Candidate History.
The description text must remain separate from evidence references.
Prominence claims must persist in their own typed field and must not be copied into the description provenance payload as prose.

The steady-state stage artifact must retain an outcome class for every eligible new or carryover candidate.
The artifact must record all four outcome counts and deterministic counts for each non-empty raw reason code.
A grounded record may contain accepted source records and the accepted description payload.
An attempted refusal may contain bounded attempt metadata and its concrete reason code.
A not-attempted record must contain scheduling metadata and `not_attempted_stage_budget`, but no identity or source verdict.
The artifact must retain enough data to render the distinct HTML and CSV messages without retaining rejected source prose.
Queue metadata must remain outside Candidate History and must contain the durable ID, queue entry operation/date, first seen date, latest Priority Label, attempt status, and reason code.

Attempt outcomes must be bounded and deterministic.
A candidate record should retain the attempt status, stage date, source kind counts, and reason codes needed to explain why a description was or was not accepted.
The record must not retain rejected source prose, search snippets, or unbounded page captures.
The attempt artifact must distinguish discovery, classification, identity, fetch, agreement, scheduling, and persistence failures.

## Failure outcomes and fail-closed rules

The stage must define stable reason codes for at least the following completed-attempt outcomes.

- `no_results` means discovery returned no usable result.
- `search_or_fetch_failure` means discovery or an allowed fetch failed without enough accepted evidence.
- `fewer_than_two_agreeing_sources` means fewer than two independent sources passed the agreement gate.
- `title_mismatch` means a source title failed normalized title agreement.
- `author_mismatch` means a source author failed normalized author agreement.
- `source_identity_disagreement` means a fetched source disagreed with the target or accepted sources on the identity pair and was excluded.
- `subject_content_disagreement` means a fetched source disagreed with the subject evidence and was excluded or left fewer than two consistent sources.
- `insufficient_subject_text` means the accepted page lacks enough subject-bearing text.
- `ambiguity` means more than one candidate or work identity remains possible.
- `not_attempted_stage_budget` means the candidate was not admitted because the remaining stage budget could not hold the complete per-candidate envelope.
- `candidate_budget_exhausted` means an admitted candidate exhausted its own envelope before producing an accepted grounded description.

The stage must not use a generic `budget_exhausted` reason where either `not_attempted_stage_budget` or `candidate_budget_exhausted` applies.
A `not_attempted` outcome must use only `not_attempted_stage_budget` for its scheduling decision and must not claim source availability or identity failure.
An admitted candidate that reaches a completed check must use the applicable concrete source or identity reason code.
An admitted candidate whose own envelope ends before the attempt completes must use `candidate_budget_exhausted` rather than inventing a source verdict.

The stage must never widen matching to fill the description field.
No results must leave the existing description unchanged or use the existing safe fallback.
A discovery or fetch failure must leave the description unchanged or use the existing safe fallback.
A title or author mismatch must exclude that fetched source and must not be repaired by model inference.
A source-to-source identity disagreement must exclude the disagreeing source.
The candidate must fail when no individually verified source remains, or when fewer than two independent pairwise identity-consistent and subject-consistent sources remain after an attempted search.
Subject-content disagreement must fail closed when fewer than two such sources remain.
Insufficient subject text must fail closed when fewer than two such sources remain.
Ambiguity must fail closed.
A stage-budget non-admission must leave the candidate queued without an identity or source verdict.
Candidate-budget exhaustion must fail closed for the candidate while allowing finalization.

With exactly two conflicting sources, the candidate has no two agreeing sources and must receive no description.
If a third source disagrees while two independent sources agree, the third source must be excluded.
The stage may proceed with the two remaining sources only when they are pairwise identity-consistent and subject-consistent.
The third-source disagreement and its exclusion reason must be recorded without retaining rejected prose.

An existing valid description whose provenance is exactly `codex-generated` is eligible for recurring weekly grounding because its visible provenance warning remains unconfirmed.
Valid `grounded`, valid `backfilled`, and `editor-corrected` descriptions remain ineligible for recurring grounding.
For an existing valid `codex-generated` description, a failed recurring attempt must preserve that description and its visible provenance warning byte-for-byte.
The failed attempt must retain a bounded attempt record and reason without replacing the description or provenance with fallback or inferred text.
For a missing description, a failed backfill must preserve the safe fallback.
A failed backfill must never delete or replace either value with inferred text.

## Known blind spots

Nominally distinct sites may copy the same publisher copy, so URL and owner independence do not prove independent reporting.
Two works by the same author may have the same normalized main title, so subtitle-insensitive normalization can still collide.
Source metadata may be stale, wrong, or attached to the wrong work.
Sourced publication-title mappings may be wrong, incomplete, stale, or attached to the wrong original title.
Copied cross-language metadata may make unrelated pages appear to share a published title and author.
A bilingual page may state a translated title without providing enough evidence that it is the published title for the same work.
Translations and transliterations that lack an accepted direct or provenance-bearing published-title route must fail the exact gate and remain unfilled.
Sparse Czech or Italian evidence may leave fewer than two independent subject-bearing sources even when the work is legitimate.
JavaScript-only pages, paywalls, robots restrictions, and unavailable content may prevent acceptable evidence.
Prominence claims may have hidden time, geography, language, market, or edition scope.
Search and classifier coverage may omit suitable sources or classify a suitable source as unknown.
A model can still synthesize an incorrect description even from accepted evidence.

This design can still attach the wrong book when two apparently independent sources repeat the same wrong title-author mapping.
It can also attach the wrong work when same-author same-title works have no available discriminator.
The implementation must include fail-closed ambiguity checks wherever those cases are detectable.
The implementation must report the residual blind spot rather than claiming that two accepted URLs prove truth.
Human review remains necessary for ambiguous identity, conflicting subject content, material publication-title mapping concerns, and material prominence claims.

## Steady state and backlog artifacts

Steady state should produce one bounded per-run description-stage artifact outside canonical Candidate History.
The artifact should contain the operation identity, stage version, monotonic timing summary, complete scheduled worklist summary, candidate outcome records, accepted source records, queue metadata, reason codes, and a digest of the accepted payload.
It should distinguish `grounded`, `single_source`, `attempted_no_description`, and `not_attempted` candidates.
It should preserve `not_attempted_stage_budget` entries as carryover candidates.
It should preserve `candidate_budget_exhausted` as an attempted refusal rather than as a skipped candidate.
It should be safe to inspect without exposing rejected source prose.
The weekly review should consume only canonical accepted results and the render-neutral provenance status.
The HTML and CSV renderer must use `Поиск источников еще не запускался` for not-attempted candidates.
The HTML and CSV renderer must use `Источники проверены - описание не подтверждено` for attempted refusals and must display their concrete reason separately.
The review summary must report these classes separately.

The durable queue should be a separate scheduling artifact outside Candidate History.
It should record the durable candidate ID, queue entry operation/date, first seen date, latest scheduling Priority Label, attempt status, and reason code.
It should support atomic resume and deterministic ordering without changing Candidate History or candidate ranking.
A queue entry awaiting work must not be mistaken for an identity or source failure.

The backlog should have a separate batch manifest for the one-time 439-row operation.
The manifest should record the batch number, exact candidate IDs, batch priority, report digest, approval status, and completion status.
Each dry-run report should be immutable once presented for approval.
A completed batch should record the accepted evidence digest and bounded reason-code summary.
A failed or deferred batch should remain resumable without silently changing its candidate set.
A batch item that is not admitted must remain pending with `not_attempted_stage_budget`.
Candidate History must remain unchanged while a dry-run report is awaiting approval.

The operation should define reason-code counts for discovery, classification, identity, source agreement, persistence, scheduling, and budget stages.
A later report must be able to show whether a candidate was skipped because it was already grounded, already approved, ambiguous, failed, not attempted because of stage budget, or attempted without a description because its candidate budget was exhausted.
These artifacts must not become ranking signals.
They must support operator review, deterministic retry, and evidence-based timing analysis.

## Approval boundary

No description or prominence mutation may occur before the dry-run or steady-state acceptance payload passes validation.
For the recurring weekly route, Issue #570 standing approval authorizes Candidate History mutation after the validated acceptance payload passes.
For the separately invoked one-time bulk backfill, Dima must approve the exact dry-run report before Candidate History changes.
The approval must not authorize later batches, changed URLs, changed identities, or changed generated text.
A changed report requires a new approval.
Queue metadata may preserve scheduling state outside Candidate History, but it must not be used to bypass the approval boundary for canonical description or provenance mutation.
A conflict, ambiguous target, identity alias conflict, persistence mismatch, or rejected push must stop before further mutation.
The pipeline must report one concrete next action and ask for the minimum required confirmation when it cannot proceed safely.

## Proposed Part 2 validation tests

The implementation should add synthetic tests for the universal one-to-three-source agreement contract.
Tests should verify that one source is accepted only as visibly marked `single_source`, while title-only evidence, outside knowledge, and search snippets cannot produce a grounded description.
Tests should verify exact normalized title and author agreement, including subtitle differences that should match.
Tests should verify that different authors, missing authors, same-author same-title collisions, and ambiguous durable candidate resolution fail closed.
Tests should verify that ISBN equality never bypasses title-and-author agreement.
Tests should verify pairwise agreement across all accepted sources.
Tests should verify deterministic source classification, preference order, distinct URL enforcement, and independent owner enforcement.
Tests should verify that unknown sources and unclassifiable results do not count.
Tests should verify the BookMarks per-book URL behavior without expanding the collector's fixed listing budget.
Tests should verify typed prominence claims, exact evidence URLs, identity gating, and conflicting-claim failure.
Tests should verify original-language subject sources in German, French, Spanish, Czech, Italian, and English.
Tests should verify a bilingual source that includes an original title and a translated or published title for the same work and author.
Tests should verify that one bilingual page contributes one source rather than two.
Tests should verify the direct original-title route and the verified published-title route.
Tests should verify that a generated rendering, a marked rendering, a literal translation, a transliteration, a search snippet, and a model-inferred title cannot bridge identity.
Tests should verify that a #458 mapping source is not counted as a research source unless its page independently has sufficient subject text and independent ownership.
Tests should verify that accepted subject sources resolve through one sourced title mapping and still agree on author.
Tests should verify the #458 prerequisite behavior before #457 consumes publication-title evidence.
Tests should verify that Czech or Italian candidates without two agreeing subject-bearing sources receive no grounded description and use `fewer_than_two_agreeing_sources` after an attempt.
Tests should verify that a budget-not-attempted Czech or Italian candidate uses only `not_attempted_stage_budget` and makes no source-availability claim.
Tests should verify the 900-second stage ceiling, complete 90-second candidate envelope from Issue #563, at most one discovery request for an eligible candidate, 10-second inactivity timeout capped by remaining budget, three-worker limit, and three total fetch requests including retries with monotonic fake clocks.
Tests should verify that the scheduler has no fixed first-15 cap and no replacement cap of 155.
Tests should verify a worklist containing 16, about 37, and 155 eligible candidates, with only safely admitted work started and all remaining work carried over.
Tests should verify that the remaining stage budget is checked before admission of each complete candidate envelope.
Tests should verify `grounded`, `single_source`, `attempted_no_description`, and `not_attempted` as distinct outcome classes.
Tests should verify `not_attempted_stage_budget` has no identity or source verdict and remains queued.
Tests should verify `candidate_budget_exhausted` is distinct from completed source and identity refusal reasons.
Tests should verify the exact Russian HTML and CSV messages and separate review counts for non-admitted candidates and attempted refusals.
Tests should verify durable queue metadata, atomic resume, and the order of queue-entry operation/date, Priority Label A through D, first seen date, and durable candidate ID.
Tests should verify that an older low-priority candidate outranks a newer high-priority candidate without changing either candidate's actual Priority Label or ranking.
Tests should verify that description-stage budget exhaustion does not extend extraction and still permits finalization.
Tests should verify accepted staging artifacts with exactly one, exactly two, and exactly three ordered URLs.
Tests should verify rejection of four URLs and reordered canonical evidence, single-source handling of one URL, duplicate URLs, and same-owner URLs, and grounded handling of same-family URLs with different owners.
Tests should verify bounded reason codes and absence of rejected source prose.
Tests should verify the separately invoked one-time bulk backfill's resumable 25-row batches, the report-size boundary, non-admitted pending items, exact dry-run report digests, approval gating, idempotent retry, and no Candidate History mutation before approval.
Tests should verify that production timing evidence is recorded for review after initial batches.

## Out of scope

This proposal does not implement source fetching, discovery, classification, or model changes.
This proposal does not implement the #458 publication-title evidence and provenance seam.
The #458 seam must land before #457 Part 2 consumes it.
This proposal does not change the existing extraction stage or its budget.
This proposal does not repair the BookMarks index collector.
This proposal does not alter Candidate History, identity aliases, or candidate identity outside the approved recurring or one-time description operation.
This proposal does not change descriptions in Part 1.
This proposal does not change ranking, priority, signal aggregation, independent signal weighting, profiles, runtime constants, or reasoning arguments.
This proposal does not use prominence for ranking.
This proposal does not claim that the 439-row backlog has been measured, processed, or validated in production.
This proposal does not remove the editor's responsibility to review warnings, ambiguity, conflicts, multilingual evidence, and provenance.
