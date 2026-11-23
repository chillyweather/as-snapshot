---
status: accepted
---

# Operational Russian-edition verification is separate from editorial status

Issue: #616.

## Context

The Russian-edition status is an editorial-facing candidate fact with the canonical values `found in Russian`, `not found in Russian`, and `unclear`.
Provider execution has operational statuses, such as checked, not run, skipped, blocked, failed, and unimplemented.
A provider outcome is a closed result such as matched, no match, ambiguous, blocked, failed, or unimplemented.
Combining editorial status, provider execution, and provider outcome allowed incomplete provider evidence to look like a verified negative.

## Decision

This accepted ADR and implementation slice corrects reliability with existing providers only.
Labirint and other new provider work, ISBN-first query order, Yandex allowlisting, bounded per-candidate query budgets, caching and replay integration, and rolling backfill prioritization remain deferred.
This ADR does not claim full #616 completion.

Russian-edition Verification is stored as a separate candidate-level operational record.
The record stores Check Coverage, Provider Outcome, Verification Confidence, candidate-scoped provider evidence, and bounded English operational reasons.
A null aggregate Provider Outcome means that no single closed outcome exists because required provider checks did not all complete.
Each provider stores its execution status separately from its outcome.
A provider with execution status not run or skipped has no Provider Outcome.
Russian-only labels and explanations belong to Markdown and HTML review presentation, not internal JSON artifacts.

Only complete configured-provider coverage with a candidate-scoped `no_match` outcome permits the editorial status `not found in Russian`.
An unscoped `no_match` record never binds to a candidate, even when the run has one candidate.
An explicitly declared unknown retailer or provider remains required coverage with the Provider Outcome `unimplemented`.
Russian publisher-site checks remain deferred and do not contribute to required retailer coverage.

An incomplete check preserves a prior verified positive Russian edition instead of replacing it with an unverifiable negative.
A new incomplete check without a prior verified positive produces the editorial status `unclear`.
Operational verification does not change editorial feedback or visibility state.

## Consequences

The visible review shows `unclear` instead of an unverifiable `not found in Russian` result.
This keeps the editor from treating missing, blocked, failed, or unimplemented provider evidence as proof of absence.
The pilot readiness surface reports the resulting unclear statuses as a caveat, so incomplete verification can prevent readiness for editor sharing.
A verified positive remains visible with the URL from its matched provider evidence and that structured evidence while a later incomplete check is recorded separately.
The existing publisher-site deferral remains available without weakening retailer coverage requirements.
