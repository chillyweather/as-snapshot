# PRD: Amazon Charts Most Read Nonfiction source onboarding

**Status:** Accepted

**Owner:** AutoScout workflow

**Related work:** Parent issue #368; delivery tickets #369-#373; `docs/source-onboarding-runbook.md`; `docs/research-big-retailer-bestseller-sources.md`; the Audible chart onboarding (#314) and its feasibility seam; the UK onboarding worked example (#299).

## Problem statement

AutoScout has no Amazon source in its weekly portfolio.

The 2026-07-18 big-retailer research deferred Amazon because the `/gp/bestsellers` pages are bot-walled (nondeterministic 503, browser-User-Agent-conditional) and the top-level page has no umbrella nonfiction node.

That research did not probe Amazon Charts.

New live evidence collected on 2026-08-04 shows that Amazon Charts Most Read Nonfiction is a weekly, first-party Amazon nonfiction list that passes every Source Onboarding Runbook gate as written, including the HEAD 405 plain-GET fallback.

The portfolio currently contains three `bestseller/ranking` sources (Publishers Weekly, USA TODAY, Spiegel).

Adding Amazon Charts gives the portfolio the largest US retail reader-interest signal without weakening the easy/open source principle.

## Goal

Onboard Amazon Charts Most Read Nonfiction as one configured `bestseller/ranking` source through the canonical Source Onboarding Runbook, replay-first, into probation.

The source must collect the current week's 20 ranked nonfiction titles with metadata only, fail closed on Amazon's silent-fiction quirk, and never trigger live collection before its replay fixtures are proven.

## Non-goals

- Amazon `/gp/bestsellers` subcategory pages (still bot-walled; a separate decision).
- The Most Sold Nonfiction chart (a separate follow-up source with the same parser).
- Amazon Charts on amazon.de or amazon.co.uk (separate follow-ups).
- PA-API, browser automation, API keys, hosted services, or requirement changes.
- Generalizing the Audible feasibility seam or changing Audible behavior.
- Automatic retirement or promotion of the source after probation.

## Proposed solution

### Source facts (live evidence, 2026-08-04)

The weekly list URL is `https://www.amazon.com/charts/<YYYY-MM-DD>/mostread/nonfiction`.

The week label is a Sunday, so the URL date is a Sunday.

HEAD returns 405 on the dated URL.

Plain GET returns 200 in every repeated attempt (15+ total), with no redirect, no captcha markers, and a response around 72 KB.

The page is server-rendered with exactly 20 rank blocks `id="rank1"` through `id="rank20"`, each carrying the rank number, the title, the "by <author>" line, the "N weeks on the list" marker, and a direct `/dp/` book link.

The page heading is "Most Read Nonfiction | Amazon Charts".

The landing page `https://www.amazon.com/charts` is stable (4/4 attempts at 200) and links the current week via `/charts/<DATE>/mostread/nonfiction`.

robots.txt has no rule covering `/charts`.

Two quirks force the design:

1. The date-free URL `/charts/mostread/nonfiction` silently renders the fiction list.
2. A wrong date in the URL returns 404.

The runbook's feasibility checklist explicitly allows a plain-GET fallback when HEAD returns 405 or 501, provided that GET returns 200 without an unrelated redirect and the raw bounded response contains the expected heading, exact bounded ranks, visible titles and authors, and direct book links.

### Runbook sequence

The onboarding follows the seven runbook steps in order.

Each step gates the next, and a failed gate is recorded as rejected or deferred with evidence, never worked around.

1. **Feasibility evidence**: write `docs/issue-368-amazon-charts-source-feasibility.md` recording the 2026-08-04 probe results above.
2. **Source Proposal**: add the proposal with signal-family `bestseller/ranking` and access-status `open`, evaluate every operator-workflow criterion, and accept deliberately (ADR 0005: no collection before acceptance).
3. **Configuration**: add the portfolio entry with the full metadata field set and the `amazon_charts_weekly` flag, plus replay and live overlay entries (ADR 0006: agent extraction, no bespoke collector).
4. **Replay fixtures first**: commit a pinned 2026-07-26 page capture and a bounded extraction fixture under `data/current-weekly-pilot/`.
5. **Validation**: pass the shared exact-list contract battery, a green replay run, and the full suite.
6. **Probation entry**: `lifecycle_status: "probation"` with a `lifecycle_note` naming issue #368.
7. **Commit**: one commit with config, fixtures, tests, and the evidence doc, with the config `notes` referencing #368.

### Technical design

The implementation mirrors the proven Audible chart pattern.

**Parser** (`autoscout/source_page_identity.py`):

- `amazon_charts_entries(html_content, source_page)` returns the 20 direct `/dp/` links with title and bounded entry context, following the `_EntryBlockCollector` pattern.
- `amazon_charts_mention_is_grounded(...)` grounds rank, title, evidence URL, and every primary author within one bounded entry.

**Feasibility seam** (`autoscout/source_feasibility.py`):

- `evaluate_amazon_charts_feasibility(source, *, head_status, get_status, resolved_url, html, landing_html, author_marker=None, reference_date)` applies the HEAD 405/501 fallback to one plain GET.
- The GET must be 200, resolve to the same scheme/netloc/path, carry no query-string pagination, and pass `likely_blocked_response`.
- The page heading must contain "Nonfiction", which fails closed against the silent-fiction quirk.
- The page must expose exactly 20 titled entries, each with a nonblank primary author.
- `landing_html` is mandatory evidence: the recorded landing-page capture, without which the seam rejects even when the dated page evidence is valid.
- The landing evidence must resolve a recent dated link for the configured `chart_path_segment` (Issue #378).
- `author_marker` selects the per-entry primary-author marker rendered by the chart locale (`by` for the English charts, `von` for the German charts).
- `reference_date` pins the week-recency window; the seam is pure and never consults the clock.

**Week resolution**:

- A pure helper takes the landing page HTML and the configured base URL and returns the current week's dated URL.
- The helper fails closed when the nav link is absent or malformed.
- The live capture path fetches the landing page, resolves the dated URL, fetches the dated page, and captures the dated page.
- The replay path uses the pinned fixture and never touches the network.

**Config** (`autoscout/config.py`):

- The source-specific flag `amazon_charts_weekly` mirrors the `audible_explicit_chart` pattern.
- Validation requires family `bestseller/ranking`, `metadata_exact_book_list: true`, the landing-page URL without query or fragment, and `max_response_bytes` in the allowed range.

### Portfolio entry

- `name`: "Amazon Charts Most Read Nonfiction"
- `family`: "bestseller/ranking"
- `url`: `https://www.amazon.com/charts` (the stable landing page; the week-dated list URL is resolved per run)
- `access_status`: "open"
- `check_frequency`: "weekly"
- `language`: "en"
- `country`: "United States"
- `market`: "United States"
- `expected_category`: "General Nonfiction"
- `signal_family`: "bestseller/ranking"
- `refresh_cadence`: "weekly"
- `url_stability`: "week-dated chart URL resolved from the stable landing page"
- `expected_page_heading`: "Most Read Nonfiction"
- `metadata_exact_book_list`: true
- `amazon_charts_weekly`: true
- `expected_signal_strength`: "top ranking"
- `eligible_candidate_category`: "adult nonfiction"
- `max_response_bytes`: 1500000
- `lifecycle_status`: "probation"
- `lifecycle_note`: "Issue #368: active and reversible pending source metrics."

## Artifacts

- `docs/issue-368-amazon-charts-source-feasibility.md` - the feasibility evidence doc.
- `docs/prd-amazon-charts-most-read-nonfiction.md` - this PRD.
- `data/current-weekly-pilot/amazon-charts-most-read-nonfiction-page.json` - the pinned page capture.
- `data/current-weekly-pilot/amazon-charts-most-read-nonfiction-extraction.json` - the bounded extraction fixture.
- Portfolio and overlay changes in `configs/`.
- Parser, feasibility seam, week-resolution helper, config validation, and their tests.
- One onboarding commit and one PR.

## User stories

1. As the operator, I want the weekly run to collect the current week's Amazon nonfiction chart, so that the portfolio reflects the largest US retail reader signal.
2. As the operator, I want the source to fail closed when Amazon serves the fiction list under the nonfiction URL, so that fiction never enters the pipeline silently.
3. As the operator, I want the week URL resolved from the landing page, so that the collection never guesses the week date.
4. As the operator, I want replay fixtures proven before any live collection, so that the weekly run cannot be broken by a misconfigured new source.
5. As Dima, I want the implementation to reuse the Audible chart patterns, so that the new source stays deterministic and testable without bespoke collector code.

## Acceptance criteria

### Feasibility evidence (#369)

- The evidence doc records the 2026-08-04 probe facts: HEAD 405, GET 200 repeated, no redirect, no bot-wall markers, 20 rank blocks with titles and authors, the Nonfiction heading, the landing-page week link, the absence of a `/charts` robots rule, and both quirks.
- The Source Proposal is added, evaluated, and accepted in state.
- No candidate collection happened before acceptance.

### Implementation (#370)

- The parser returns exactly 20 entries on the pinned fixture with correct titles, authors, and `/dp/` paths.
- The feasibility seam accepts the recorded live evidence.
- The seam rejects a fiction-heading page, a missing landing nav link, a wrong-date 404, and bot-wall HTML.
- Config validation rejects a misconfigured `amazon_charts_weekly` entry.
- Tests use deterministic fixtures only and never touch the network.

### Configuration and fixtures (#371)

- The portfolio entry validates against all config rules.
- Four-config parity holds across portfolio, pilot, pilot-live, and live overlays.
- Every evidence URL in the extraction fixture is verifiably present in the page capture.

### Validation (#372)

- The shared exact-list contract battery passes for the new source.
- The replay run is green: the source appears as `collected` in `source-runs.json`, and its mentions appear in `source-mentions.json` with the configured signal fields exactly.
- The full suite stays green.
  Existing source collection outputs must not regress.
  Every configured source keeps its collection status, extracted and valid mention counts, and signal fields.
  The golden and weekly-fixture outputs do not shift.
- The editor-facing mix can change deterministically when new candidates rank within the fixed 100-row limit.
  A new source competes through the normal ranking process and takes finite slots from lower-ranked rows.
  The shared regression tests are updated to the new mix, mirroring the Issue #366 precedent.
- The expected post-onboarding German contribution mix is recorded and tested.
  Spiegel Bestseller Sachbuch contributes 0 editor-facing rows.
  Deutscher Sachbuchpreis contributes 1.
  Die Zeit Sachbuch-Bestenliste contributes 0.
  The German total is 1, asserted in `tests/test_source_collection.py`.

**Superseded by Issue #379.**
The two acceptance items above assumed a fixed 100-row editor limit.
That limit was a configured cap, not an editorial decision, and it discarded eligible ranked candidates on every run.
Issue #379 raised the cap, which restored the displaced German rows and left the Amazon Charts contribution unchanged.
This source therefore never competed with the German rows on merit, only on slot count.
No mix is recorded here on purpose.
The shared regression tests now assert invariants instead of absolute counts, and this document must not become the new carrier of the counts they stopped pinning.
Read the current mix from `run-summary.json` in a real run.

### Probation and commit (#373)

- The probation fields are present and correct.
- The branch carries exactly one onboarding commit with the full deliverable set.
- The PR references #368 and the feasibility evidence.
- The source is never collected live before probation entry.

## Implementation notes

- Keep the production weekly pipeline, extraction envelope, and all existing sources unchanged in this slice.
- Reuse the shared page-shape validator, `likely_blocked_response`, and the `_EntryBlockCollector` pattern.
- Do not modify the Audible feasibility seam or its German `Von:` handling.
- The Amazon Charts seam is a parallel addition, not a generalization.
- The weekly run may lose the current week's chart when Amazon publishes late; the collection reports the captured week date and a missing link is a source failure, not a silent fallback.
- The replay overlay pins the 2026-07-26 week; the live overlays use the resolution helper.
- The eventual Most Sold variant, DE/UK Charts, or `/gp/bestsellers` work are separate follow-up issues, not part of this PRD's tickets.

## Delivery plan

Parent PRD issue #368.

- **A. #369** - Feasibility evidence doc and Source Proposal gate.
- **B. #370** - Page-identity parser, feasibility policy seam, current-week URL resolution, config flag, and tests.
- **C. #371** - Portfolio and overlay configuration plus replay fixtures.
- **D. #372** - Shared exact-list contract validation and green replay run.
- **E. #373** - Probation entry check and the single onboarding commit and PR.

Dependencies are A before B and C, B and C before D, and D before E.

## Follow-up decision gate

After the source completes probation, choose exactly one engineering outcome:

1. **Keep the source** when contribution metrics justify it.
2. **Retire the source softly** (`active: false` with a recorded reason) when it contributes little and corroboration is not worth the risk.
3. **Extend the family** with the Most Sold Nonfiction chart as a second source when the parser proves reliable.

The probation review is a conversation with the editor, never an automatic action.
