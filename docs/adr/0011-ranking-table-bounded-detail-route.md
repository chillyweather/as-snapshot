# Ranking tables may use a bounded declarative detail route

## Status

Accepted for issue #585.

## Decision

The existing `ranking-table` contract is extended with an optional flat detail route.

The route declares the row link selector, an accepted detail URL pattern, one bounded detail scope, the requested detail fields, field declarations, an exact raw category allowlist when needed, and a positive per-run page cap.
The hard maximum for `ranking_detail_max_pages` is 200 pages.

The closed detail-field vocabulary is `isbn` and `category`.

A detail route is valid only when its declarations are complete and internally consistent.

A category-supplying route must declare a nonblank `eligible_candidate_category`.

The raw category captured from a detail page is evidence only and is checked against `ranking_detail_category_allowlist`.

The mention `candidate_category` remains the configured canonical `eligible_candidate_category`.

The canonical category must never be copied from or replaced by the raw detail category.
A category detail route uses the exact raw detail-category allowlist as its eligibility gate and does not use title-term exclusions.

The existing `ranking_evidence_url` value is `source_page` for single-page rankings and `detail_page` for rankings with a detail route.

## Evidence URL semantics

`source_page` remains the configured ranking-list URL on the top-level capture.

`source_url` on every mention remains that configured ranking-list URL.

For a two-page mention, `evidence_url` identifies the requested same-domain detail URL linked from that mention's list row.

The linked URL must pass the configured same-domain and route-pattern checks.

URL comparisons use the repository's canonical URL identity policy, so equivalent URL spellings are accepted only when their canonical identities match.

Capture producers write the requested row-linked URL consistently as each detail item's `source_page`, `detail_url`, and `resolved_url`.

Those three fields must remain canonically equivalent to the requested URL, and a redirect to a different canonical URL is rejected.

The strict configured list `source_page` identity check remains enforced on the top-level capture.

The list row grounds rank, title, ordered authors, detail link, and source week.

The linked detail page grounds the requested ISBN and the allowlisted raw category.

Every ranking mention still carries a nonblank ISBN.

No detail page can be substituted for the configured list page.

## Fetch and replay policy

Configured live collection fetches and validates the list page first.

The required order is list-page raw-row bounds, row shape, detail links, URL policy, and title exclusions, followed by detail requests and then detail-only category filtering.
A category detail route cannot filter by category before the detail request because the raw category exists only on the detail page.
The route still applies the cap and preserves deterministic list-row order.
A category detail route does not apply `adult_nonfiction_exclusions` and may omit that field or provide an empty list.
Single-page rankings and detail routes without category continue to require non-empty `adult_nonfiction_exclusions`.

The positive `ranking_detail_max_pages` cap is checked before the first detail request.

Detail requests use the existing bounded response-size and timeout transport.

Redirects, missing pages, extra pages, duplicate pages, row-link mismatches, and detail-scope drift fail closed.

Issue #1163 amends this for a route that declares `ranking_detail_max_failed_pages`, a count below `ranking_detail_max_pages`.
Up to that many rows may lose their detail page, and only to a failure the fetch classifies where it raises it: an HTTP error status, a timeout, a dropped connection, a redirect to another URL, or detail-scope drift.
Each such row is skipped and recorded in the capture's `detail_pages_skipped` list with its rank, title, detail URL, and reason, and the source is reported as incomplete coverage naming those titles.
One more failure fails the source with that failure's own reason, and a skip of every surviving row fails too.
A block, a rate limit, a bot-protection challenge (its header on any status, or its markers in an error body), a TLS or name-lookup failure, any other unlisted error, and an exhausted capture allowance are never skipped.
Validation accepts exactly the declared skips: each names one surviving row once with its own rank and title, and every other row still carries its matching page.
A route that does not declare the field keeps the fail-closed rule above.

Captures and replay pages store a deterministic `detail_pages` list.

Each detail item records the requested detail URL as `source_page`, `detail_url`, and `resolved_url`, together with sanitized `html`.

Replay consumes this list without network access.

When retained detail evidence is complete, replay binds every surviving list row to its detail page before filtering raw categories.

Rows whose grounded raw category is outside the exact allowlist are skipped.

The first at most three eligible rows in document order become representatives.

Their grounded ISBN values are retained in `EntrySample` and in the generated mentions.

The generated mentions preserve the configured canonical candidate category.

Replay generation refuses detail routes that do not have complete retained detail evidence.

The source-candidate probe remains HEAD plus at most one GET and retains detail declarations without fetching detail pages.

## Collector and model boundary

Agent extraction and Codex replay remain the collection paths under ADR 0006.

The extractor may use only compact declared list rows and the captured bounded detail scopes.

The extractor must not browse or follow links.

The global model-input bound covers the compact list rows and all bounded detail scopes.

Omitted or truncated detail pages are marked incomplete and are not evidence.

Grounding validation remains fail closed through `ranking_mention_is_grounded` and `extraction_contract_violation`.

## Shared bounded source capture writer

Every file-backed source capture writer uses one shared bounded writer in `autoscout/capture_bounds.py`.
The writer JSON-serializes the capture, enforces the UTF-8 aggregate serialization bound derived from the configured `max_response_bytes`, creates the destination parent directory, and writes the file.
Expected JSON serialization and filesystem failures normalize into one generic capture-domain error, `CaptureWriteError`, whose diagnostics never include filesystem paths.

The live response capture is evidence, so it remains fail-closed.
A ranking capture that exceeds its bound or fails to write fails the source and yields no mentions.

The extraction response capture is optional observability, not evidence.
Its write failure is recorded as a bounded, path-free diagnostic in the source-run debug metadata and does not discard valid extracted mentions.
The source run remains collected when only the extraction response capture failed.

Ranking detail scope failures remain `RankingCaptureError`, because they describe ranking evidence defects rather than generic capture write failures.

## Consequences

Retailer rankings can ground list rank and product identifiers without bespoke source collectors.

Existing single-page ranking declarations retain their prior evidence semantics and parsing behavior.

Detail-route sources pay one bounded detail request per surviving list row, subject to the declaration cap and the hard 200-page ceiling.

Detail-route replay fixtures contain the bounded list capture and the deterministically bound detail captures needed to validate every surviving row.
