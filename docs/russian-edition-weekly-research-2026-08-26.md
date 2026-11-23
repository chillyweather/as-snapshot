# Russian-edition checking for the weekly run

Research date: 2026-08-26.

Scope: repository code, configuration, tests, the current saved Russian-edition artifact, and first-party Russian catalog/search interfaces.
This note is a research and implementation handoff only.
It does not change canonical run state, Candidate History, or configuration.

## Executive finding

The current check is not a working Russian-edition search:

- `configs/current-weekly-pilot.json` declares Ozon, LitRes, and Book24, but each has an empty `fixture_results` list and none enables live checking.
- LitRes and Book24 have live adapters.
  Ozon is classified as a generic retailer whose live adapter deliberately returns `no implemented adapter`.
- RSL has a real, metadata-only adapter and replay tests, but is opt-in and is not present in the current weekly config.
- The prior saved `russian-edition-checks.json` reported all three configured checks as `checked`, despite empty result sets.
  The current editor list has 128 candidates and, before the bounded refresh below, all of them carried `not found in Russian`.
  This is a false sense of coverage: an empty result set is being treated as a completed negative check.

The #616 implementation makes source execution and coverage truthful before adding more sources.
A source that is disabled, unimplemented, blocked, empty, or partially queried must not produce a definitive `not found in Russian` decision.

Relevant existing seams:

- `autoscout/russian_editions.py` owns source adapters, bounded query attempts, parsing, metadata-only evidence, and run status.
- `autoscout/pipeline.py` applies the checks before candidate suppression and writes the Russian-edition artifact.
- `autoscout/scouting_candidates.py` maps a same-work Russian result to `found in Russian`, imported-only results to `not found in Russian`, and ambiguous matches to `unclear`.
- `docs/rsl-adapter.md` documents the existing RSL boundary and replay model.

## Repository audit

### Current configuration and execution

The current weekly configuration has only these three checks:

```json
[
  {"retailer": "Ozon", "url": "https://www.ozon.ru", "fixture_results": []},
  {"retailer": "LitRes", "url": "https://www.litres.ru", "fixture_results": []},
  {"retailer": "Book24", "url": "https://book24.ru", "fixture_results": []}
]
```

The adapter dispatch distinguishes LitRes, Book24, RSL, and a generic retailer branch.
Ozon, Labirint, and Chitai-gorod currently enter the generic branch; that branch can normalize recorded fixtures but its live function always returns `live check has no implemented adapter`.
Therefore adding an Ozon URL to config is not equivalent to enabling a live Ozon check.

The pipeline calls live adapters only when `enable_live` is true.
It then collects normalized matches and applies the status transition using the candidate-level verification result.
An empty, unimplemented, blocked, failed, or out-of-scope provider result is operationally incomplete and resolves to `unclear`, rather than being treated as a completed negative.

### Current status semantics

The canonical stored vocabulary is intentionally small:

- `found in Russian`: a same-work Russian-language result with a URL;
- `not found in Russian`: a negative result after complete configured coverage;
- `unclear`: an ambiguous result or incomplete verification.

Do not add a fourth canonical status as the first step: that would require migration across source mentions, validation, review rendering, history, and tests.
Instead, preserve these three values and add separate operational fields to the Russian check artifact/candidate evidence:

- `check_coverage`: `complete`, `partial`, `not_run`, or `failed`;
- `provider_outcome`: `matched`, `no_match`, `ambiguous`, `blocked`, `unimplemented`, or `failed`; null means that no single closed outcome exists;
- provider execution status: `checked`, `not_run`, `skipped`, `blocked`, `failed`, or `unimplemented`;
- `confidence`: `high`, `medium`, or `low`;
- `checked_sources`: bounded source names and check date.
- `checked_at` and `query_timestamp`: deterministic run-date timestamps.
- `evidence_urls`, `providers`, and a bounded `reason` for audit and review.

Each `providers` entry records the provider name, source type, status/outcome, bounded reason, result count, check time, evidence URLs, and metadata-only evidence.

A provider execution status of `not_run` means that no candidate-scoped provider execution supplied evidence.
A provider execution status of `skipped` means that configuration prevented the check from running.
The aggregate `provider_outcome` is null when incomplete provider execution prevents one closed outcome.
Empty fixtures and configured provider results that do not cover the candidate are not no-match results.

The implemented status rule is:

1. `found in Russian` only when a source returns a verified same-work Russian edition.
2. `unclear` when identity, language, or title translation is unresolved, or when required source coverage is incomplete, not run, blocked, failed, or unimplemented.
3. `not found in Russian` only when the configured required source set completed successfully, found no Russian edition, and did not find a conflicting ambiguous/imported result.

This keeps the public status vocabulary stable while preventing incomplete source execution from masquerading as a negative result.

## First-party source assessment

### 1. Russian State Library / RSL (Ленинка)

Primary interface: [RSL search](https://search.rsl.ru/ru/search).

The live catalog exposes structured advanced-search fields for author, title, content, keywords, note/annotation, publisher, ISBN, publication year, and document identifiers.
Its query language documents `AND`, `OR`, `NOT`, phrase search, morphology, prefix search, and field-qualified queries.
That is a strong bibliographic existence signal for a Russian-language edition.

AutoScout already has an RSL adapter.
It bootstraps a session, obtains the CSRF token, posts to the catalog's AJAX search route, parses bounded metadata, and records only title, author, language clue, publication data, ISBN, record URL, query, source, and date.
The existing adapter correctly treats RSL as a library catalog, not a retailer: an RSL hit proves cataloged publication, not current commercial availability.

Recommendation: make RSL the first default production check for new and updated candidates, after a fresh bounded live probe confirms the current markup and response behavior.
Keep it metadata-only and retain replay fixtures for positive, ambiguous, foreign-language, empty, blocked, timeout, and changed markup cases.

### 2. Labirint

Primary interfaces: [Labirint books catalog](https://www.labirint.ru/books/) and [Labirint search help](https://www.labirint.ru/helpgopage/top/yang-edalt-angliyskiy/).

Labirint's own help describes full-text search over catalog content and allows queries using title, author name, series, and publisher, with search in descriptions/excerpts and category filters.
The public books route exposes server-rendered product rows and direct product pages in ordinary responses.

Recommendation: implement Labirint as the first retailer-specific adapter after RSL.
Require a deterministic product-card selector, direct product URL, title, author, and a language/edition clue.
Treat a foreign-language item or an ambiguous same-title result as non-positive evidence.
Do not infer a Russian edition merely from the product being listed in the Russian store.

### 3. Ozon

Primary consumer surface: [Ozon books category](https://www.ozon.ru/category/knigi-uchebnye/).
Official developer documentation: [Ozon Seller API](https://docs.ozon.ru/api/seller/).

Ozon visibly exposes a large consumer catalog, but the official API surface found for developers is the Seller API: its documented methods are for a seller's products, offers, SKUs, stock, and seller analytics and require seller credentials.
That is not a public, unauthenticated catalog-search API for checking arbitrary books.

Recommendation: do not build the weekly check on Seller API unless a separate approved seller account and credential-management design is provided.
First run a bounded feasibility probe against the consumer search/category/product pages: verify server-rendered metadata, stable product URLs, response size, rate-limit behavior, and whether the page identifies Russian language/ISBN/author.
If the probe is not reproducible, keep Ozon as a manual or Yandex-locator source and do not mark its check as `checked`.

Ozon should be treated as a retailer signal, not a bibliographic authority: marketplace listings can be imported foreign-language editions, duplicates, or seller-generated cards.
A positive Ozon result should require title/author identity plus a Russian-language or ISBN/publisher clue.

### 4. Yandex Search

Primary interfaces: [Yandex Search API](https://yandex.cloud/ru/services/search-api) and [Yandex Search API quickstart](https://aistudio.yandex.ru/docs/ru/search-api/quickstart/index.html).

Yandex Search API provides a Russian search type and REST/gRPC/SDK access, returns XML/JSON/HTML formats, and supports bounded web queries.
The official quickstart shows an authenticated REST endpoint and API-key/folder setup.
This is a real integration option, but it introduces a paid/credentialed external dependency and returns web search results rather than normalized book records.

Recommendation: use Yandex as a locator and evidence-discovery provider, not as a direct status oracle.
Queries should be domain-restricted and bounded, for example:

- `site:labirint.ru <title> <author>`;
- `site:ozon.ru <title> <author>`;
- `site:book24.ru <title> <author>`;
- `site:search.rsl.ru <title> <author>`;
- `"<Russian title>" <author>` when a published Russian title is known.

The adapter must parse only result URLs/titles/snippets into a short-lived locator artifact, follow an allowed first-party URL, and let a source-specific parser decide whether the result is a verified edition.
Never scrape the consumer Yandex HTML page as a substitute for the official API, and never mark `found in Russian` from a title-only search snippet.

### 5. NЭБ (National Electronic Library)

Primary interfaces: [NЭБ portal](https://rusneb.ru/) and its [official search manual](https://rusneb.ru/upload/iblock/13c/neb_search_manual.pdf).

NЭБ is useful as a supplementary catalog/discovery source.
Its official manual describes edition search, sorting, and filters for author, date, publisher, library, place of publication, collection, classifier, and access.
It combines records from sources such as RSL and the library electronic catalog.

It should not be the sole proof of a current Russian commercial edition: NЭБ is an electronic-library/catalog and holdings/access surface, not a complete retail inventory.
Add it only after RSL/Labirint replay coverage is stable and only if its current search route can be queried and parsed deterministically.

### 6. Existing and secondary Russian sources

- LitRes and Book24 already have source-specific adapters and should remain in the required retailer set, but their live behavior and source health must be reported honestly.
- Chitai-gorod is recognized by the code but currently has only the generic unimplemented live branch; it is a later adapter candidate after Labirint.
- The Russian Book Chamber / national bibliography would be valuable for authoritative publication evidence, but its current public machine-readable interface was not verified in this research pass.
  Treat it as a separate feasibility investigation, not as an assumed implementation dependency.

## Bounded integration plan

### Completed one-time refresh on 2026-08-26

The current 128-row editor list was checked against RSL with one bounded title/translation query per candidate.
Six high-confidence RSL transitions were applied from the primary-title pass, and two more from the saved Russian title mappings (`Der Atlas der KI` and `Wie man Freunde gewinnt`).
The applied records are:

- `48 Laws of Power`;
- `Der Atlas der KI`;
- `Good to Great`;
- `Homo Deus`;
- `How to Win Friends & Influence People`;
- `The 48 Laws of Power`;
- `The Power of Now`;
- `Wie man Freunde gewinnt`.

Every applied transition preserves the exact Candidate ID, RSL record URL, query, result title/author, language clue, publication metadata, and check date.
The primary pass also recorded 103 ambiguous results and 9 source failures; the translation-mapping pass recorded 31 ambiguous results and 5 failures.
Those rows were not downgraded or marked as published.
A false map/catalog match was retained in the preview but blocked by the final apply-time matcher.

The compact batch summary is retained in `runs/current-weekly-pilot-russian/russian-edition-refresh-summary-2026-08-26.json`.

### Phase 0 — One-time existing-list backfill

This is separate from the weekly run and should not silently mutate history.

1. Read candidates and excluded-history records by exact Candidate ID.
2. Select only candidates with `not found in Russian`, `unclear`, or missing Russian evidence; skip manual/editorial overrides unless explicitly included.
3. Run a bounded research batch with RSL first, then Labirint/LitRes/Book24, and Yandex only as a locator fallback.
4. Produce a preview artifact containing Candidate ID, old status, proposed status, source outcomes, direct evidence URLs, match rationale, and unresolved ambiguities.
5. Apply only after explicit editor confirmation through the existing guarded state-update path.
   Preserve every prior check and record the new evidence as a dated status transition.

The backfill must report separate totals for `found`, `unchanged negative`, `unclear`, `source failed`, and `not attempted`.
A source outage must never turn into a mass status downgrade.

### Phase 1 - Make source execution truthful (implemented)

The implementation now records execution truth separately from the three stable public Russian-edition statuses:

1. Each candidate receives `check_coverage`, `provider_outcome`, `confidence`, `checked_sources`, timestamps, evidence URLs, provider records, and a bounded reason.
2. Empty, unimplemented, blocked, failed, and out-of-scope providers remain operationally incomplete instead of becoming successful empty checks.
3. A negative status is possible only after all required providers complete and produce a candidate-scoped no-match outcome.
4. `russian-edition-checks.json`, `run-summary.json`, and the review show source status, candidate coverage, and the safe reason a result is `unclear`.
5. Legacy negative values without positive evidence are normalized to `unclear` before history reconciliation, without creating a material update or resurfacing a suppressed candidate.
6. Recorded fixtures and replay files remain the default deterministic test path; live mode remains explicit and bounded.

### Phase 2 — RSL default provider

1. Add RSL to the weekly check configuration with an explicit bounded timeout.
2. Query only new and materially updated candidates in the normal weekly run.
3. Prefer two deterministic queries per candidate: ISBN when present, then title-plus-author.
   Use translated-title/transliteration queries only when the candidate carries verified mappings; do not issue every possible query by default.
4. Stop after the first high-confidence match, but retain bounded query attempts and the selected metadata record.
5. Set `found in Russian` only for a same-work Russian-language record with a record URL; otherwise preserve `unclear` or a qualified negative.
6. Capture metadata-only records for replay, never page bodies or copied prose.

### Phase 3 — Labirint and existing retailer adapters

1. Implement Labirint as a dedicated adapter with a source-specific parser.
2. Revalidate LitRes and Book24 live paths with current fixtures and blocked/changed-markup tests.
3. Implement Chitai-gorod only after a bounded server-rendered feasibility probe proves stable title, author, language, and direct URL fields.
4. For every retailer, distinguish a Russian edition from an imported foreign-language listing and from an ambiguous same-title product.

### Phase 4 — Ozon feasibility, then optional adapter

1. Probe the public consumer route without credentials and without bypassing bot protection.
2. Record response status, bounded size, stable selectors, direct product-link availability, language/ISBN/author fields, and rate-limit behavior.
3. If the probe passes, implement a narrow parser plus fixtures.
   If it fails, retain Ozon as `deferred` and do not include it in the required negative coverage set.

### Phase 5 — Yandex locator and NЭБ supplement

1. Decide whether Yandex API credentials and cost are acceptable for weekly operation; keep the API key outside repository config and state.
2. Add a domain allowlist and a strict request/result budget.
3. Store only bounded locator metadata and source URLs; follow through to first-party pages for verification.
4. Add NЭБ only as supplementary bibliographic evidence after a feasibility probe.
   It must not override a contradictory direct publisher/retailer/RSL identity without review.

## Suggested weekly policy

For each new or materially updated candidate:

```text
RSL ISBN or title+author
  -> if verified Russian record: found in Russian
  -> otherwise Labirint/LitRes/Book24 retailer checks
  -> if a verified Russian product: found in Russian
  -> otherwise optional Ozon/Yandex locator and NЭБ supplement
  -> if all required checks complete with a candidate-scoped no-match: not found in Russian
  -> if any ambiguity, not_run, block, failure, partial coverage, or translation uncertainty: unclear
```

For previously seen unresolved candidates, use a separate rolling budget (for example, 25–50 records per weekly run or a monthly sweep) rather than querying the entire accumulated history every week.
Recheck candidates when a source recovers, a new verified title mapping appears, or the previous negative check is older than the configured freshness window.

The weekly report should expose:

- candidates checked and candidates skipped by budget;
- positive matches by source;
- definitive negatives only after complete required coverage;
- ambiguous/partial/blocked checks requiring review;
- source-level status (`checked`, `not_run`, `failed`, `blocked`, `unimplemented`, `skipped`, or `deferred`);
- direct evidence URLs for every positive result.

## Acceptance criteria for the implementation handoff

The integration contract for the standard weekly run is:

1. A current run cannot report `checked` for an unimplemented provider.
2. An empty or blocked source cannot create a definitive `not found in Russian`.
3. RSL positive, RSL ambiguous, imported foreign-language, empty, timeout, blocked, and changed-markup fixtures pass.
4. Labirint positive, imported, ambiguous, empty, and changed-markup fixtures pass before live onboarding.
5. Ozon has either a passing bounded feasibility report and parser fixtures or is explicitly `deferred`.
6. The current three-value status vocabulary remains backward compatible, while evidence and coverage fields make incomplete checks visible.
7. Every status transition preserves Candidate ID, previous status, check date, source, query, direct URL, and bounded metadata rationale.
8. A replay run is deterministic and does not require live credentials.
9. The full weekly health/review flow distinguishes source failures from candidate-level Russian-edition decisions.

## Sources

- [RSL search and advanced-search syntax](https://search.rsl.ru/ru/search)
- [AutoScout RSL adapter note](rsl-adapter.md)
- [Labirint books catalog](https://www.labirint.ru/books/)
- [Labirint search help](https://www.labirint.ru/helpgopage/top/yang-edalt-angliyskiy/)
- [Ozon consumer books category](https://www.ozon.ru/category/knigi-uchebnye/)
- [Ozon Seller API documentation](https://docs.ozon.ru/api/seller/)
- [Yandex Search API](https://yandex.cloud/ru/services/search-api)
- [Yandex Search API quickstart](https://aistudio.yandex.ru/docs/ru/search-api/quickstart/index.html)
- [NЭБ portal](https://rusneb.ru/)
- [NЭБ official search manual](https://rusneb.ru/upload/iblock/13c/neb_search_manual.pdf)
