# PRD: Automated Russian-Edition Status Checks

Status: draft PRD v0.1
Date: 2026-05-25
Parent context:
- `reqs/03-product-requirements-document.en.md`
- GitHub issue #1: PRD: AutoScout MVP Pipeline
- Recorded editor pilot from `docs/recorded-media-trial.md`

## Problem Statement

The first editor-facing AutoScout pilot successfully produced useful scouting candidates, but Russian-publication status still required manual checking by Dima. This is a major bottleneck in the current editorial workflow.

After candidate discovery, the editor needs to know whether a foreign nonfiction book is already available in Russian. Manual checks across Ozon, Labirint, LitRes, Chitai-gorod, and Book24 are slow, repetitive, and easy to miss. This makes the pipeline less scalable: adding more discovery sources increases candidate volume, but also increases manual verification work.

AutoScout should automate Russian-edition status checks for discovered candidates while preserving uncertainty. The system must avoid overclaiming. It should confidently exclude clear same-work Russian editions, keep plausible open opportunities visible, and send ambiguous cases to a focused manual review queue.

## Goal

Replace manually supplied Russian-edition status in the MVP workflow with automated checks against configured Russian retailer sources.

For each discovered Scouting Candidate, the system should produce:

- Russian-edition status: `found in Russian`, `not found in Russian`, or `unclear`.
- Evidence URL when a credible match is found.
- Search attempts used to reach the decision.
- Match explanation.
- Confidence/review reason.
- Manual review flag only when automation cannot make a defensible decision.

The expected product outcome is that the editor/Dima manually checks only ambiguous candidates, not every row.

## Users

### Primary user

Dima, operating the AutoScout weekly pipeline before sending results to the editor.

Needs:

- avoid manually checking every candidate across Russian retailers
- understand why a candidate was marked found/not found/unclear
- inspect evidence quickly when automation is uncertain
- prevent false exclusions caused by imported English editions or weak title matches

### Secondary user

Russian publishing editor reviewing the weekly CSV.

Needs:

- see whether a candidate appears already published in Russian
- trust that `not found in Russian` means “not found by configured checks,” not an absolute claim
- see uncertain cases clearly
- avoid spending attention on candidates already available in Russian

## Scope

### In Scope

- Automated Russian-edition checks after candidate discovery.
- Configured retailer sources:
  - Ozon
  - Labirint
  - LitRes
  - Chitai-gorod
  - Book24
- Search using candidate metadata:
  - original title
  - primary author Latin name
  - title plus author
  - ISBN when available
  - configured or generated search translation
  - configured or generated author transliteration notes
- Match classification:
  - same-work Russian-language edition
  - imported foreign-language edition
  - ambiguous possible match
  - no credible match
- Status resolution:
  - `found in Russian`
  - `not found in Russian`
  - `unclear`
- Evidence capture:
  - retailer/source name
  - result URL
  - matched title
  - matched author
  - language/format clue when available
  - query used
  - decision reason
- Internal review queue for `unclear` cases.
- Integration into existing candidate pipeline, internal exclusions, run summary, and editor-facing CSV.
- Fixture/recorded tests for deterministic behavior.

### Out of Scope

- Bypassing retailer access controls, paywalls, CAPTCHAs, bot protections, or blocked pages.
- Russian publisher-site checks.
- Full web search as a source of scouting candidates.
- Treating search translations as confirmed metadata.
- Treating absence from configured retailer checks as proof that no Russian edition exists.
- Rights availability confirmation.
- Legal conclusions.
- Google Sheets integration.
- Live network checks in default unit tests.

## Key Product Rules

### Discovery Boundary

Russian-edition automation must run only after a candidate has already been discovered from configured scouting sources.

Retailer search results must not create new Scouting Candidates. They only enrich or exclude existing candidates.

### Status Semantics

`found in Russian` means:

- The system found credible same-work Russian-language evidence on a configured retailer source.
- The evidence includes a URL and enough matching data to support the decision.

`not found in Russian` means:

- The system searched configured sources using configured query strategies.
- No credible same-work Russian-language match was found.
- This is scoped to configured checks only.

`unclear` means:

- The system found possible but insufficient evidence.
- Examples include weak title match, partial author match, missing language clue, suspicious metadata, or multiple conflicting results.

### Imported Editions

Imported English or other foreign-language editions listed on Russian retailers must not count as `found in Russian`.

They should be recorded as evidence of a retailer listing, but classified as `imported foreign-language edition` or equivalent non-excluding match type.

### Ambiguity Handling

The system should prefer `unclear` over false `found in Russian`.

A candidate should only be excluded from the editor-facing CSV when the match evidence is strong enough to support `found in Russian`.

### Generated Search Values

Generated Russian title translations and author transliterations are search aids only.

They must not overwrite display title, author, original metadata, or confirmed Russian-edition metadata unless backed by a real match.

## User Stories

1. As Dima, I want AutoScout to check configured Russian retailers automatically, so that I do not manually search every candidate.
2. As Dima, I want the system to show search attempts and evidence URLs, so that I can audit status decisions.
3. As Dima, I want ambiguous matches flagged as `unclear`, so that manual review is focused on hard cases.
4. As Dima, I want imported English retailer listings not to exclude candidates, so that valid opportunities are not hidden.
5. As an editor, I want Found in Russian candidates removed from the main CSV, so that the watchlist focuses on plausible open opportunities.
6. As an editor, I want `not found in Russian` to mean “not found by configured checks,” so that the report does not overclaim.
7. As an editor, I want unclear Russian status visible in the CSV, so that I know which candidates need caution.
8. As Dima, I want Russian-status checks logged in machine-readable artifacts, so that weekly runs are auditable.
9. As Dima, I want status changes to count as Material Changes, so that previously suppressed candidates can re-enter when evidence changes.
10. As a future implementer, I want retailer adapters and match classification separated, so that new sources can be added without rewriting status logic.

## Functional Requirements

### Source Configuration

- Retailer check sources must be configured separately from scouting discovery sources.
- Each retailer config should include:
  - source name
  - source family: `russian-retailer`
  - base URL
  - access status
  - search method
  - check frequency
  - notes
- Paid, blocked, CAPTCHA-protected, fragile, or unavailable retailer checks must be logged as skipped/failed checks, not bypassed.

### Query Planning

For each candidate, the system should build a bounded set of search attempts from available metadata.

Query inputs may include:

- original title
- normalized title
- primary author
- title plus primary author
- ISBN
- search translation
- author transliteration note

The query planner must preserve the distinction between original metadata and generated search aids.

### Retailer Result Collection

Each retailer adapter should return normalized result metadata:

- source name
- source URL
- query
- result URL
- result title
- result author
- result language when available
- format/product type when available
- ISBN when available
- debug metadata safe to store

Adapters must not store full page bodies, full descriptions, full reviews, or copied copyrighted text.

### Match Classification

The classifier should compare candidate metadata with retailer result metadata and output:

- `same-work Russian edition`
- `imported foreign-language edition`
- `ambiguous possible match`
- `not same work`

Classification should consider:

- title similarity
- author similarity
- ISBN match
- language clue
- format/product type
- translation/import clues
- edition/work ambiguity

### Status Resolution

The resolver should combine classified matches into one candidate-level status.

Rules:

- Any credible `same-work Russian edition` match produces `found in Russian`.
- Only non-matches and imported foreign-language editions produce `not found in Russian`.
- Any unresolved plausible match produces `unclear` unless a stronger same-work Russian match exists.
- `found in Russian` requires an evidence URL.

### Artifacts

The pipeline should write:

- `russian-edition-checks.json`
- updated `scouting-candidates.json`
- updated `internal-exclusions.json`
- updated `run-summary.json`
- updated `editor-facing.csv`
- optional `russian-review-queue.csv` for unclear cases

`russian-edition-checks.json` should include query attempts, normalized results, classification decisions, resolved status, and review reason.

### Report Integration

The editor-facing CSV should keep the current Russian-edition columns:

- `Russian-edition status`
- `Russian-edition URL`

`found in Russian` candidates should be excluded from the editor-facing CSV and stored in Excluded Candidate History.

`unclear` candidates should remain visible unless another true exclusion applies.

### History And Material Change

Russian status changes are Material Changes:

- `found in Russian` -> `unclear`
- `found in Russian` -> `not found in Russian`
- `not found in Russian` -> `found in Russian`
- `unclear` -> `found in Russian`
- `unclear` -> `not found in Russian`

Found-in-Russian candidates may re-enter the editor-facing workflow if later evidence changes status to `unclear` or `not found in Russian`.

## Acceptance Criteria

- [ ] The pipeline can run without manually supplied Russian-edition status for discovered candidates.
- [ ] Configured Russian retailer sources are loaded separately from scouting discovery sources.
- [ ] The system performs bounded query planning from title, author, ISBN, search translation, and transliteration notes.
- [ ] Retailer checks produce metadata-only result records and do not store full page bodies or long copied text.
- [ ] Same-work Russian-language matches resolve to `found in Russian` with evidence URL.
- [ ] Imported English/foreign-language retailer listings do not resolve to `found in Russian`.
- [ ] Ambiguous possible matches resolve to `unclear`, not false positive `found in Russian`.
- [ ] No credible matches after configured checks resolve to `not found in Russian`.
- [ ] Found in Russian candidates are excluded from the editor-facing CSV and retained in internal exclusions.
- [ ] Unclear candidates remain visible in the editor-facing CSV unless another true exclusion applies.
- [ ] The run summary reports Russian-check skipped/failed counts and unclear count.
- [ ] The pipeline writes auditable Russian-check artifacts with query attempts, evidence URLs, classifications, and decision reasons.
- [ ] Existing recorded trial can be migrated away from manually supplied Russian statuses or run with automated status fixtures.
- [ ] Tests cover same-work match, imported edition, ambiguous match, no match, skipped source, failed source, status resolution, CSV integration, and history/material-change behavior.

## Testing Strategy

Default tests should be deterministic and fixture-driven.

Use local fixture files for retailer search results. Do not require live network access for unit or default integration tests.

Test groups:

- query planning from available metadata
- retailer result normalization
- match classification
- status resolution
- exclusion behavior
- unclear review queue behavior
- artifact shape
- CSV integration
- history/material-change behavior
- copyright-safe storage constraints

Optional live integration tests may be added later behind an explicit flag, but they must not be required for normal CI/local verification.

## Success Metrics

For the next editor pilot after implementation:

- At least 80% of candidates receive automated `found in Russian` or `not found in Russian`.
- Manual review is needed only for `unclear` cases.
- No known imported English retailer listing is incorrectly marked `found in Russian`.
- Every `found in Russian` exclusion has a working evidence URL.
- Dima can explain every automated Russian-status decision from generated artifacts.

## Suggested Implementation Slices

1. Russian retailer check contract and artifacts.
2. Query planner for candidate title/author/ISBN/search-note variants.
3. Fixture-backed retailer result adapters.
4. Match classifier for same-work, imported edition, ambiguous, and not-same-work cases.
5. Candidate-level status resolver.
6. Pipeline integration with exclusions, CSV fields, run summary, and review queue.
7. History/material-change behavior for Russian status changes.
8. Recorded trial migration from manually supplied statuses to automated status fixtures.

## Open Questions

- Should the first implementation use search-result fixtures only, or should it include one live opt-in retailer adapter?
- Which retailer should be attempted first for a live adapter if we choose one?
- Do we need ISBN enrichment before Russian checks, or can ISBN remain optional for this milestone?
- Should `russian-review-queue.csv` be separate from the main editor CSV, or is the visible `unclear` status enough for the next pilot?
- What is the acceptable false-negative rate for `not found in Russian` in the pilot stage?
