---
status: accepted
---

# Artifact freshness and source-run status use separate seams

Issue: #467.

## Context

Issue #384 introduced the serialized artifact freshness values `fresh`, `reused`, and `unbound` for Codex extraction artifacts.
`reused` means that an artifact is stale or cannot prove that it belongs to the current operation, and the operator-facing health, warning, and review surfaces report it as a fault.
A future validated cache hit will also use an artifact from an earlier operation, but it will be a successful and deliberate result rather than a stale-artifact fault.

## Decision

Artifact Freshness is a closed domain with the serialized values `fresh`, `reused`, and `unbound`.
Source Run Status is a separate closed domain with the serialized values `inactive`, `skipped`, `ignored`, `failed`, `collected`, `reused`, `precollected`, and `codex-captured`.
The two domains have separate predicates and separate meanings, even though both currently contain the serialized value `reused`.

The artifact predicate `is_stale_artifact_freshness` is used at artifact decisions, such as deciding whether an extraction artifact is a stale fault.
The source-run predicate `is_stale_source_run` is used at operator fault surfaces, such as run health, major warnings, Russian completion output, Markdown review rendering, and HTML review rendering.
Source-run accounting is centralized in one summary interface that normalizes raw statuses once and derives stale, collecting, and freshly-collected counts through the source-run predicate and collecting policy.
Presentation functions treat supplied source statuses as authoritative and use persisted aggregate counts only for legacy inputs that have no statuses.

The collection boundary has an explicit exhaustive adapter from Artifact Freshness to Source Run Status.
`reused` converts to `reused` before zero-contribution checks because its stale-artifact fault must remain visible.
`fresh` and `unbound` convert to `collected` only after the existing zero-contribution checks, so those checks retain their existing outcomes.
A future fourth artifact state must add an explicit adapter branch and explicit artifact-predicate behavior before it can be serialized as a source-run status.

Neutral source accounting keeps `reused` among the collecting statuses because a stale artifact still produces mentions for the current pipeline.
A future validated cache-hit state needs explicit non-fault branches in both predicates and explicit conversion at the collection boundary.

## Consequences

Existing artifacts, configuration values, source statuses, counts, warnings, and review output remain byte-identical for consistent current data.
Inconsistent legacy aggregate counts no longer suppress a stale-evidence warning when the actual source statuses report `reused`.
The fault surfaces remain aligned because they depend on the shared source-run predicate instead of repeating the stale-evidence judgment.
Adding cache behavior requires a new source state only after its non-fault policy, conversion, and presentation behavior are explicit.
