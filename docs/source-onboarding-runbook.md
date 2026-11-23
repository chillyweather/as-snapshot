# Source Onboarding Runbook

The canonical checklist for adding one configured source to the weekly
portfolio. Distilled from the Issue #299 onboarding
(`docs/issue-299-uk-source-feasibility.md`), which is the worked example of
every step below. Follow the steps in order; each step gates the next.

Sections 3, 4, and 6 are mechanically enforced by
`tests/test_source_onboarding_contract.py`: a portfolio source added without
the full metadata field set, an explicit lifecycle status, or a replay
overlay with committed input fixtures fails the suite. Sources that predate
this runbook are grandfathered in that test's `LEGACY_PORTFOLIO_SOURCES`
list; do not add new names to it.

Governing rules that this runbook applies but does not restate in full:

- **ADR 0005** - configured-source discovery only: an unaccepted Source
  Proposal must never create Scouting Candidates.
- **ADR 0006** - agent extraction is the default collector; bespoke collector
  code needs explicit justification.
- **ADR 0011** - ranking tables may use one bounded declarative detail route
- **ADR 0012** - a recurring ranking whose page prints no date proves freshness by row movement
- **ADR 0013** - a ranking row may declare that it legitimately carries no author
  without adding a bespoke collector.
- `docs/operator/sources.md` - Source Proposal evaluation
  criteria and the overall weekly workflow.
- `reqs/07-learning-loop-and-source-lifecycle.en.md` - probation lifecycle and
  the periodic keep/retire/promote source review.

## 1. Feasibility evidence (before any configuration)

For exactly one proposed URL, use the bounded `./scripts/add-source probe` launcher before creating or accepting a Source Proposal:

```bash
./scripts/add-source probe \
  --url <url> \
  --name "<source name>" \
  --signal-family "<signal family>" \
  --proposing-context "<why this source is proposed>" \
  --keep-captures <capture-directory> \
  --ledger data/source-candidate-ledger.json \
  --feasibility-dir data/source-feasibility-drafts
```

The `add-source probe` wrapper accepts exactly one `--url` occurrence before it delegates.
For multiple proposed URLs, use direct `./scripts/probe-source`, which preserves the legacy batch contract.
The direct legacy batch probe returns 0 when any candidate is accepted, 1 when candidates are rejected or malformed without an accepted candidate, 2 for command, input, or storage errors, and 3 when all candidates defer.
The wrapper returns 1 for missing or repeated URLs before delegation.
The `add-source probe` wrapper delegates to the probe launcher, which performs the bounded network call.
The probe does not modify configuration, weekly state, Candidate History, visibility, Editorial Taste Profile, Source Mentions, or Scouting Candidates.
It discards captures unless `--keep-captures` is explicit.
It writes its ledger and capture outputs as separate probe results outside the onboarding commit scope.
By default, an existing ledger entry is returned as `already_evaluated` without a network request or JSON rewrite.
Pass `--re-probe` explicitly to issue one fresh bounded HEAD/GET probe for a durable prior entry, including when the requested contract family or metadata changes, and preserve the former current verdict in history.
A repeated URL in one batch is fetched at most once, and re-probe applies only to the durable entry rather than to a result created earlier in that batch.
`--proposing-context` is optional, aligned by URL order, and preserved exactly in every result and ledger entry.
A missing context uses the deterministic default `unspecified`; version-1 entries upgraded on a later write gain the explicit legacy value `legacy-v1-unrecorded`.
The `--ledger` value must be a new `.json` artifact or an existing version-1, version-2, or version-3 Source Candidate Ledger.
Version-1 and version-2 ledgers are read without JSON rewriting on duplicate-only runs and are upgraded to version 3 only when a new verdict is written; every legacy entry keeps every value.
Version-3 entries keep one current verdict per normalized URL and may carry a flat chronological `prior_verdicts` list of complete former verdicts without nested history.
Prior snapshots retain their probe date, outcome, reason code, reason, HTTP evidence, contract family and metadata, diagnostics, and other canonical fields, and are validated against the same safe schema and URL identity before network access.
Outcome counts describe current entries only and do not count historical attempts.
Legacy list ledgers, incomplete entries, aliases, invalid outcomes, duplicate identities, wrong counts, malformed history, protected state/config basenames, weekly/config/state directories, and symlink targets are rejected before network access or writes.
Every accepted candidate also gets one deterministic feasibility draft under `data/source-feasibility-drafts` (override with `--feasibility-dir`).
The draft is created only for accepted candidates, never overwrites an existing draft, and contains grounded bounded metadata plus an incomplete editorial-rationale section that needs editor or agent judgment.
A retained accepted capture includes `source_page` and `collection_mode` and can be used as a bounded replay page fixture.
The optional `--contract-family` declaration is the feasibility and collection contract, not the External Signal family.
Use the canonical values `ranking-table`, `bookinfo-weekly-json`, `ipc-top40-csv`, `perlentaucher-daily-digest`, `kirkus-coming-soon`, `audible-chart`, `amazon-charts`, `audiobook-ranking`, `exact-book-list`, `award-list`, and `generic`.
An omitted declaration uses the generic evaluator exactly as before.
The declaration is retained in the probe report, accepted capture, feasibility draft, and Source Candidate Ledger.
A prior verdict under another explicit contract family is not reused silently, and the operator must use a new ledger path.
A ranking-table contract is accepted only when its declarative row, title, author, identifier, category, and reporting-period declarations are present, plus either a row rank selector or the explicit document-order rank source.
A ranking-table detail route may supply the ISBN and category from the row-linked product page.
The detail route is flat metadata with one link selector, one URL pattern, one bounded scope selector, a non-empty `isbn` or `category` field list, field declarations, an exact category allowlist when `category` is requested, and a positive `ranking_detail_max_pages` cap.
A detail route requires `ranking_evidence_url: "detail_page"`.
Without a detail route, `ranking_evidence_url: "source_page"` remains required.
The ranking-table evaluator does not fetch detail pages during source-candidate probing.
The ranking-table evaluator may use unlinked rows and the configured page URL as both source URL and evidence URL when no detail route is declared.
The generic linked exact-list rule remains the default for sources without a declared ranking-table contract.

### Choosing between `exact-book-list`, `ranking-table`, and `award-list` (#577)

Choose by what the page is, not by which gate is easiest to pass.

- `exact-book-list`: every entry has a book-level title link. The link is the evidence URL.
- `ranking-table`: a recurring ranking. Rows may be unlinked, and each has a rank and an identifier. The page prints a reporting period, or declares `ranking_reporting_period_undated` and proves currency by row movement.
- `award-list`: a bounded annual award list. Rows are unlinked plain text under a visible category heading, the page states the award year, and the list does not change until the next edition.

Do not describe an award shortlist as a ranking table.
It has no reporting period, a declared maximum age for it would be invented, and an undated ranking would report its unchanged rows as a frozen list.
Do not describe a weekly ranking as an award list either: an award list is never checked for row movement.

An award-list probe needs the year, the category heading, the row element, the row format, and the row count:

```
./scripts/probe-source --url <url> --contract-family award-list \
  --award-year 2026 \
  --award-category-heading "General Nonfiction Book of the Year" \
  --award-row-selector li \
  --award-row-format "{title}, {author} ({ignored})" \
  --award-expected-row-count 6
```

`--award-row-format` is the row written out with placeholders.
`{title}` and `{author}` are required, `{rank}` and `{identifier}` are optional, and `{ignored}` stands for text such as a publisher that the mention does not carry.
`--award-year-selector` is optional and names the one element stating the year when the headings do not.
`--award-author-conjunction` is optional and is the word the page puts between the last two author names, for example `and`.
Declare it whenever a row lists coauthors that way, or the joined names are read as one author.
The probe reports each row's authors joined with ` / `, for example `Daniel Mercer / Ines Okoro`; use that exact display as `author` in the helper sidecar.
`--award-row-split-first true` is an explicit rule for rows that repeat a separator, such as a comma between the title and the author and again between coauthors.
Without it such a row refuses the page, because it can be divided in more than one way.
Declare it only after reading every row of the category and confirming that the first separator always ends the first field.
`--award-row-title-element em` names the emphasis element each row sets its title in (#578).
Use it when a title can contain the separator, as in `<em>Always Home, Always Homesick</em>, Hannah Kent (Picador)`; the title is then the element's text, and a row without exactly one such element refuses the page.
It cannot be combined with `--award-row-split-first`.
A page that lists several categories is one source, because a source is identified by its URL (#578).
Declare the first category with `--award-category-heading` and each further one with a repeated `--award-additional-category-heading`.
Every declared category must hold the declared row count, and a book listed under two declared categories refuses the page.
Leave a category that is not adult nonfiction undeclared.
The additional headings apply to the first `--url` of a probe, so probe such a page on its own.
`--award-category-label-element strong` is for a page that labels its lists with a bold paragraph and not with a heading element, as in `<p><strong>Nominowani w 2026 roku</strong></p>` (#795).
The category heading is then the paragraph whose whole text is that one element, and the category ends at the next such paragraph or at the next heading.
It cannot be combined with `--award-row-selector p`.
The decision and its limits are in [ADR 0019](adr/0019-bounded-annual-award-list.md).

### Explicit BookInfo supplier JSON contract

The editor approved this narrow exception on 7 October 2026. Declare
`contract_family: "bookinfo-weekly-json"` only for
`https://www.bookinfo.com.br/maisvendidos`, with
`bookinfo_period_evidence: "week-options-and-request"` and a 14-day
period-start age limit (`bookinfo_max_age_days`). The shared adapter fetches
at most two first-party month-option responses and one selected ranking
response. It does not fetch the JavaScript application shell, run a browser,
retry an empty ranking, or scan older periods.

The official options plus exact request establish the selected completed
sales week; the ranking does not echo it. Retained evidence records this
limitation explicitly. All twenty raw JSON rows must carry distinct valid
ISBN-13s, titles, author displays, publishers, the exact nonfiction category,
and nonincreasing sales volumes. Original positions remain ranks after
`adult_nonfiction_exclusions` filtering. The actual ranking request URL is
row evidence, and the publisher landing URL remains source identity.
No book URL or HTML heading/date is fabricated.
A mention may not carry a publication year, and a publisher, when given,
must be the row's publisher exactly (#782). The extractor receives the
validated rows, not the raw JSON, in both the inline prompt and the saved
capture file, and an empty extraction from a ranking with qualifying rows is
refused. The supplier's author display is kept as one primary author; some
multi-author rows arrive with the names rearranged around a semicolon, and
splitting them is not part of this contract.

Probe, live capture and replay validate the same raw response/digest bundle.
Replay retains the complete twenty-row bundle and selects at most three
representative extraction/helper entries. Source Proposal, feasibility,
probation, full validation, offline preview and separate editor confirmation
still apply. The generic server-rendered/linked-list gate below is unchanged
for sources without this explicit contract. Full details and authorization:
[BookInfo source contract](research/bookinfo-source-contract-2026-10-07.md).

### Explicit Independent Press Top 40 CSV contract

The operator approved this narrow exception on 9 October 2026 (#780).
Declare `contract_family: "ipc-top40-csv"` only for `https://www.indiepubs.org/top40`, with `ipc_top40_period_evidence: "hosted-file-upload-date"` and a 14-day file age limit (`ipc_top40_max_age_days`).
The shared adapter fetches the publisher page and then the one nonfiction CSV file that page names.
It does not fetch the PDF, the Bookshop.org list, the LitHub republication, or any older file, and it never retries.

The current file is the single link whose visible text is `NONFICTION EXCEL`.
The page also carries unlabelled links to earlier files; they are ignored.
A missing label, a second labelled link, or a link that is not the nonfiction CSV path is refused before the file is requested.

The CSV states no sales week.
The file must resolve to the publisher's own hosted-file URL, and the upload time in that URL is the published date.
That date is recorded as `source_week` and checked against the age limit; it is not the week-ending date, which only the PDF prints.
Retained evidence records this limitation explicitly.

All forty raw rows must carry ranks 1 to 40 in file order, distinct valid ISBN-13s, titles, author displays and publishers.
Original positions remain ranks after `adult_nonfiction_exclusions` filtering.
The requested CSV file URL is row evidence, and the publisher page URL remains source identity.
The list has no publication dates, so long-selling backlist titles are not filtered by this contract.
A mention may not carry a publication year, and a publisher, when given, must be the CSV publisher exactly.
The CSV joins several contributors in one author field.
The adapter splits it on commas into individual primary authors, drops marked translators, illustrators and bare credentials, and keeps a marked editor.
A mention must carry exactly that list, so a book matches the same title from sources that give no ISBN.
The extractor receives the validated rows, not the raw CSV, in both the inline prompt and the saved capture file.

Probe, live capture and replay validate the same bundle.
The bundle keeps the complete CSV body with its digest; of the publisher page it keeps only the digest and the selected link, not the page text.
Replay retains the complete forty-row bundle and selects at most three representative extraction/helper entries.
Source Proposal, feasibility, probation, full validation, offline preview and separate editor confirmation still apply.

### Explicit Perlentaucher daily digest contract

The editor approved this source on 12 October 2026 (#809), with the fiction split made by the extraction model.
Declare `contract_family: "perlentaucher-daily-digest"` only for `https://www.perlentaucher.de/buecherschau`, with `perlentaucher_max_daily_pages` (1 to 8), `perlentaucher_max_age_days` (1 to 14), and `perlentaucher_translation_policy: "exclude-translator-note"`.

The shared adapter fetches the index page and then the dated daily pages it links, newest first, up to the declared cap.
A daily page older than the age limit is not read, and an index that links no page inside the limit is refused as stale.
The adapter never requests a book page, and never requests the query URLs that `robots.txt` disallows.
It never retries.

Each daily page must carry the heading `Bücherschau des Tages`, print one date equal to the date in its URL, and list at least one book row under a paper heading.
Title, authors, publisher, publication year, ISBN-13 and the book link are read from the row by code.
A row with no author, no valid ISBN-13, or no `Publisher, Place Year` line is rejected and counted; it is never completed from elsewhere.
A page that rejects more rows than it grounds is refused.

The row ends with the opening of the publisher blurb.
It is read once, to see whether it opens with a translator note such as `Aus dem Französischen von …`, and is not retained.
A row with a translator note is excluded.
The flag means "has a translator note"; a row without one is not shown to be a German original.

A daily page body contains blurbs, so the bundle keeps each page's digest and its parsed rows, never the body.
A book reviewed in several papers or on several days is one row, keyed by ISBN and bound to the earliest captured daily page that reviewed it.
`adult_nonfiction_exclusions` are publisher genre labels: a row is excluded when its subtitle, or the last sentence of its subtitle, equals a declared label.

The extractor receives the remaining rows with title, subtitle, authors, publisher, year, ISBN, daily page date and book link.
It decides one thing: whether a row is adult nonfiction.
A mention must repeat its row's title, authors, publisher, year, ISBN, date and link exactly, so the extractor cannot supply or correct an identity field.
The subtitle is evidence for the decision and is not part of the mention.
An empty extraction from a capture that holds rows is refused.

A live overlay declares `seen_guids_file`, the seen-item index of ISBNs already given to the extractor.
The capture reads it and never writes it, so a source health check marks nothing as seen.
It is written when an extraction of the capture is collected with accepted mentions, and it records every row the extractor was given, including the rows it left out.
Each accepted mention is first built as the Source Mention the collection stage builds, so a mention that stage would refuse fails before the index moves.
When the contract refused any returned entry, only the accepted rows are recorded and every other row is shown again, because a refused row cannot be told from a row left out.
A translator note on any captured row of a book excludes the book.
A book first recorded on the run date is shown again, so a second run on the same day loses nothing.
A run with no new row reports no entries on both extraction routes, which is this source's normal state under "A source that may publish nothing (#464)".
A German mention must also carry valid helper fields, as for every other non-English source.

Probe, live capture and replay validate the same bundle.
Replay retains the complete bundle and selects at most three representative extraction/helper entries.
Source Proposal, feasibility, probation, full validation, offline preview and separate editor confirmation still apply.

### Explicit Kirkus coming-soon listing contract

The operator approved this source on 13 October 2026 (#816), with the capitalised title Kirkus prints accepted as the source's title.
Declare `contract_family: "kirkus-coming-soon"` only for `https://www.kirkusreviews.com/discover-books/nonfiction`, with `kirkus_max_listing_pages` (1 to 40), `kirkus_max_detail_pages` (1 to 60), `kirkus_request_delay_seconds` (20 to 120), `kirkus_max_collection_age_days` (1 to 14), `kirkus_max_review_age_days` (1 to 366), and a nonempty `kirkus_genre_labels` list.

The site's `robots.txt` asks for 20 seconds between requests, so this source is collected by its own pass and not inside the capture stage.
`./scripts/run-weekly` starts the pass once a day, before it checks the sources; `./scripts/collect-kirkus` runs the same pass by hand.
The capture stage and `./scripts/check-sources` read the result of the last pass and request nothing from Kirkus.
A missing result, or one older than `kirkus_max_collection_age_days`, fails this source alone with a named reason, and the run continues.
So does a pass the site cut short that left no new row: nothing new because a request was refused is a collection failure, not a quiet week.
A pass cut short after it reached rows reports them and is named as an incomplete source in the weekly summary, the source health check and the review, with how many of the listed books it read.
A pass that only reached its declared cap is not a shortfall.
A run whose own pass failed, and that reads an earlier result still inside the age limit, is named as an incomplete source in the same three places, with the date of the result it used.
A pass started by the weekly run stops at a 40-minute allowance and is then a pass cut short, with the stop reason `kirkus_time_budget`.
Each pass records its date and outcome beside the result file, and the weekly run starts no second pass on a date that already had one.

One pass reads the listing in its `Release Date (Coming Soon)` order, `?order=1&page=N`, until a page lists no book.
A listing longer than the declared page cap is refused, so the cap is a contract and not a silent cut.
It then requests the review page of each listed book it has not opened in an earlier pass, up to `kirkus_max_detail_pages`; the rest wait for the next pass.
The declared delay is kept before every request after the first, and nothing is retried.
A listing request the site refuses fails the pass and leaves the earlier result in place.
A review page request it refuses ends the pass there, keeps the pages already opened, and records the reason.
The pass never requests a quick-view panel, a search URL, or any path `robots.txt` disallows.

Title, authors, genre label and publication date are read from the listing card by code.
The title is kept in capitals exactly as printed and is never re-cased.
Authors are the names after `by` or `edited by`, up to the first semicolon; an illustrator or translator named after it is not a primary author.
ISBN-13, publisher and the date the review was posted are read from the labelled paragraphs of the review page.
A review page also carries two further reviews; only what stands between the first title heading and the second is read, and that heading must be the card's title.

A row is rejected and counted, never completed from elsewhere, when:

- the card names no primary author or prints no publication date;
- the review page prints a `Review Program`, which Kirkus does only for a review its author paid for;
- the review page prints no publisher or `None`, which it does for a self-published book;
- the ISBN is missing or invalid, the review date is missing, or the page's publication date is not the card's.

A rejected review page is not opened again.
A card whose genre label is not in `kirkus_genre_labels` is not given to the extractor; this is the rule that keeps children's, young-adult and fiction labels out.
A review posted more than `kirkus_max_review_age_days` before the pass is not given to the extractor either.
Each row is bound to the date its review was posted, which is its `source_week`.

A review page holds the review, so the result keeps each response's digest and the parsed rows, never a body.

The extractor receives the remaining rows with title, authors, publisher, publication year, ISBN, review date and review link, and is told to return every row.
A mention must repeat its row exactly, so the extractor cannot supply, correct or re-case a field.
An empty extraction from a capture that holds rows is refused.

A live overlay declares `kirkus_collection_file`, the result of the pre-collection step, and `seen_guids_file`, the seen-item index of ISBNs already reported.
The result file lives under `data/kirkus-precollection/`, which Git ignores: it is written outside the committed weekly state, and the run's repository preflight refuses any untracked or modified file.
It is a machine-local cache and is never committed; the seen-item index is weekly state and is.
The capture reads both and writes neither.
The index is written when an extraction is collected with accepted mentions, and it records only the accepted rows: every row is nonfiction by the site's own label, so a row the extractor did not return was lost and is shown again.
A run with no new row reports no entries, which is this source's normal state under "A source that may publish nothing (#464)".

A probe is one pre-collection pass with the declared delay and caps, so it takes as long as a pass.
Probe, pre-collection, capture and replay validate the same bundle.
Replay retains the complete bundle and selects at most three representative extraction/helper entries.
Source Proposal, feasibility, probation, full validation, offline preview and separate editor confirmation still apply.

Capture live proof that the source is collectable, and record it in a
per-source feasibility doc `docs/issue-<N>-<market>-source-feasibility.md`
(pattern: `docs/issue-299-uk-source-feasibility.md`):

- [ ] **HTTP status**: 200 without an unrelated redirect. Prefer HEAD for
      feasibility only. A HEAD response of **405 or 501** may fall back to one
      plain GET. An explicitly declared ranking-table GET source may also opt
      into HEAD 404 with `ranking_head_404_get_fallback: true` (#776, editor
      authorized); the default still refuses HEAD 404. That GET must be 200
      without an unrelated redirect and its raw
      bounded response must contain the expected heading, exact bounded ranks,
      visible titles/authors, and direct book links. There is no fallback for
      403, 429, CAPTCHA/bot walls, or any other 5xx. This feasibility probe does
      not require the ordinary weekly collector to issue a speculative HEAD.
      `autoscout/source_feasibility.py` provides the pure, tested policy seam;
      it evaluates captured evidence without adding network calls to collection.
- [ ] **Server-rendered**: the exact page heading and the book data (titles,
      primary authors, book-level links) are present in the raw HTML response,
      not JS-loaded. A JS-loaded page is a **deferral**, not a workaround
      (see the #199 rejected/deferred table for precedents).
- [ ] **Exact bounded list**: the page exposes a bounded qualifying list with
      visible book-level title links and primary authors - not a
      year-at-a-glance landing page, navigation page, or prose article whose
      book names are unlinked. Record representative exact titles and URLs.
- [ ] **Adult-nonfiction fit**: the list is adult nonfiction, or the
      extraction notes must state the deterministic filter that excludes
      fiction, children/YA, narrow manuals, and topic-only entries.
For a Portuguese activity-row source, use bounded terms such as `colorindo com aquarela` and `livro de atividades` as deterministic exclusions.
Confirm the final canonical terms against accepted live samples before onboarding.
Reuse the same explicit exclusion mechanism for other source-specific children's activity or coloring terms.
The exclusion terms are declarative metadata and are applied without semantic or model inference.
- [ ] **Access**: open - no paywall, CAPTCHA, or bot-wall. A blocked source
      is rejected with the evidence recorded.
- [ ] **URL stability**: note whether the URL is stable or year-specific and
      what the refresh cadence is (annual award URLs must be refreshed each
      year - see the National Book Awards / Goodreads Choice / Baillie
      Gifford pattern).
- [ ] **Annual edition** (annual sources only): find on the official site
      when an edition is first published and where it is announced, and
      record the basis in the feasibility notes. The portfolio entry declares
      it in `annual_edition` (section 5).

## 2. Source Proposal (the acceptance gate)

- [ ] Record the proposal:

  ```bash
  python -m autoscout source-proposal add \
    --source-url <url> \
    --name "<source name>" \
    --proposed-by <who> \
    --reason "<why this source>" \
    --signal-family <award|bestseller/ranking|media/curated list|...> \
    --access-status open \
    --caveats "<caveats>" \
    --state-dir <state-dir>
  ```

- [ ] Evaluate and record every criterion from the operator workflow: source
      URL and provenance, signal family, access status, stability,
      adult-nonfiction fit, metadata-only safety, reason and caveats.
- [ ] Accept it deliberately (`python -m autoscout source-proposal accept --language <en|de|fr|...>`).
      Until accepted into configuration, the source must not be collected
      from (ADR 0005). `--language` is required: every configured source
      must declare its language (Issue #645).

### A URL whose proposal was rejected before (#578)

A rejected Source Proposal stays rejected, and `./scripts/add-source` refuses it.
A fresh accepted probe does not reopen it by itself.
When the reason for the rejection no longer holds, reopen it explicitly:

```bash
python -m autoscout source-proposal reconsider \
  --id <proposal id> \
  --state-dir <state-dir> \
  --ledger data/source-candidate-ledger.json \
  --reason "<what changed since the rejection>" \
  --reconsidered-by <who>
```

The fresh evidence is the URL's current entry in the Source Candidate Ledger, which must be an `accept` probed after the rejection date.
Without `--confirmed-by-editor` the command prints the preview and its `confirmation_token`, returns 3, and writes nothing.
Repeat it with `--confirmation-token <token> --confirmed-by-editor` only after explicit confirmation.
The proposal becomes `proposed`, not accepted: its old rejection is kept in `prior_decisions`, and the onboarding preview and confirmation still follow.
A changed ledger, reason, or proposal makes the token stale, and the command refuses with a `reason_code`.
The reopened proposal keeps its `source_name`, so probe and onboard the source under that name.

## 3. Configuration (portfolio + overlays)

Source metadata lives once, in **`configs/current-weekly-portfolio.json`**;
the run configs (`current-weekly-pilot.json`,
`current-weekly-pilot-live.json`, `current-weekly-live.json`) reference it
via `weekly_portfolio` and add only per-mode overlay fields, merged by
`name` (or explicit `portfolio_name`).

- [ ] **Portfolio entry** with the full metadata field set (copy the shape of
      the Baillie Gifford entry):
      `name`, `family`, `url`, `active`, `access_status`, `check_frequency`,
      `language`, `country`, `market`, `expected_category`, `signal_family`,
      `refresh_cadence`, `url_stability`, `expected_page_heading`,
      `expected_sort_label`, `metadata_exact_book_list`, `expected_signal_strength`,
      `eligible_candidate_category`, `extraction_notes`,
      `lifecycle_status: "probation"`, `lifecycle_note`, `notes`.
When an exact-book-list page shows more than one list, for example an award page that keeps the rest of the longlist below the shortlist, declare `entry_section_heading` with the exact heading of the list to collect.
The page must then carry exactly one `<section>` with that heading, and a book linked anywhere else on the page is refused as an entry of this source.
An annual source (`check_frequency: "annual"`) also declares `annual_edition`, which the annual source audit judges (#680):
`edition_year` (the edition configured now; equal to `award_year` when both are set), `landing_url` (the official https page where a new edition is announced; the configured `url` for a stable landing page), `release_window` (`opens` and `closes` as `MM-DD`, when the edition labelled Y first publishes its first stage, with `calendar_year_offset` of -1, 0 or 1 when that happens in Y-1 or Y+1), `stages` (in order, from `longlist`, `semifinalists`, `shortlist`, `finalists`, `nominees`, `winner`, `list`) and `tracked_stage` (the stage the source is configured to show).
Optionally add `edition_marker` and `next_edition_url` (each containing `{year}`) and `stage_markers` (stage to visible text), only when the page or a past edition establishes them.
The audit looks for `edition_marker` anywhere in the page's visible text, the title included, so choose text that names the published edition and nothing else: a call for entries that reads "Gesucht: Sachbuch des Jahres 2027" would match "Sachbuch des Jahres {year}".
A `stage_markers` entry must equal one visible heading of the page.
`url_stability` must begin with `stable` (the configured URL carries each new edition) or `year-specific` (the next edition has another URL); config load refuses anything else on a source that declares `annual_edition`.
Widen a window that rests on a single past announcement rather than guess a narrow one.
A newly onboarded annual or monthly source is first collected on the next Sunday run (#1057).
When a stable page shows no list between two editions, for example an award homepage that moves on to the next call for entries, declare `between_editions_page_heading` and `between_editions_reason` beside `expected_page_heading`.
A run that finds that heading and not the expected one records the source as skipped with the reason `between editions: …` and requests no extraction.
The expected heading wins when both are present, and a page that shows neither still fails page-shape validation.
Choose a heading that stays on the page for the whole gap, and write a reason that stays true in later years.
For a ranking-table source, set `contract_family: "ranking-table"`, `metadata_ranking_table: true`, the row and field declarations, `ranking_rank_source`, the category marker, the reporting-period pattern, the date format, the maximum age in days, the raw-row count contract, and the single-page `ranking_evidence_url: "source_page"` value.
For an award-list source, set `contract_family: "award-list"`, `award_year`, `award_category_heading`, `award_row_selector`, `award_row_format`, `award_expected_row_count`, an annual `check_frequency`, and `award_author_conjunction` when rows join coauthors with a word.
Add `award_additional_category_headings` when the page lists more than one qualifying category, and `award_row_title_element` when rows set the title in emphasis markup.
Add `award_category_label_element` when the page labels a category with a bold paragraph and not with a heading element.
Set neither `metadata_exact_book_list` nor `metadata_ranking_table`, and declare no `ranking_*` field, no `adult_nonfiction_exclusions`, and no `expected_sort_label`: the configuration loader refuses them.
When the next edition is published, change `award_year`, the row count, and the URL or heading together; until then a page that states a newer year is refused rather than collected under the old one.
An older ranking table that is read without row selectors and prints its week in a source note, such as `Source: Circana BookScan, for the week ended Sep. 19, 2026`, declares `source_week_max_age_days`.
The week is then read from the note by code and aged against the run date, and a week older than the limit fails page-shape validation with both dates in the reason.
Set the limit to the publication lag plus one cadence; for a weekly chart posted about a week after its week ends, that is 14.
The replay page fixture must carry a `reference_date`.
When a bounded row legitimately carries no author, for example an anonymous, collective, or institutional title, declare `ranking_author_optional: true`.
That declaration still requires `ranking_author_selector`, so an absent author means the page was examined and carried none.
A row that does carry author text is grounded exactly as before, and a missing title, identifier, or detail link still rejects the page.
An author-less mention from a non-English source omits `author_transliteration_note`, because there is no author to transliterate.
Prefer an exact `ranking_expected_raw_row_count` with this declaration, so a selector that silently stops matching cannot pass as a page of author-less rows.
A source may additionally declare `ranking_accept_incomplete: true` only with an exact bound alone and only when the editor has authorized partial coverage (Issue #668): a short but non-empty capture is then collected with a visible incomplete-ranking warning carrying the observed and expected row counts plus missing/duplicate-rank diagnostics, instead of rejecting the valid rows. Over-count and empty captures still fail, and wrong-page, identity, evidence, detail-route, and all other gates are unchanged.
When the ranking page prints no date at all, declare `ranking_reporting_period_undated: true` instead of the reporting-period pattern, the date format, and the maximum age, and declare none of those three.
An undated recurring ranking proves currency by row movement: the bounded rows are fingerprinted each run, an unchanged fingerprint is reported as a frozen list, and the source publishes no mentions for that run.
Within the source's declared cadence an unchanged fingerprint is skipped as not yet refreshed instead of failed, so declare the list's real `check_frequency`.
An undated ranking requires a state directory, because it cannot prove movement without a retained previous fingerprint.
Choose the dated declarations whenever the page prints a period, and reserve the undated declaration for a page where no pattern can succeed.
Single-page rankings and detail routes without `category` require non-empty `adult_nonfiction_exclusions` terms.
A detail route with `category` uses the exact detail-category allowlist as its gate and may omit `adult_nonfiction_exclusions` or provide an empty list.
A detail route with `category` never filters by title terms.
When ISBN or category is on each linked product page, declare `ranking_detail_link_selector`, `ranking_detail_url_pattern`, `ranking_detail_scope_selector`, `ranking_detail_fields`, the matching identifier or category declarations, and positive `ranking_detail_max_pages`.
The `ranking_detail_max_pages` declaration must not exceed the hard ceiling of 200 pages.
A route may declare `ranking_detail_max_failed_pages`, a count below `ranking_detail_max_pages`, for how many rows may lose their detail page without failing the source; the skipped rows are named as incomplete coverage, and without the declaration every detail page must be read (Issue #1163).
A detail route that supplies ISBN makes the list-page identifier declarations optional, but every ranking mention still requires a nonblank ISBN.
A detail route that supplies category requires a nonblank `eligible_candidate_category`, `ranking_detail_category_selector`, `ranking_detail_category_pattern`, and a non-empty exact `ranking_detail_category_allowlist`.
The raw category from the detail page is checked only against `ranking_detail_category_allowlist`.
The mention `candidate_category` remains the configured `eligible_candidate_category` and must not copy the raw category.
A detail route must use `ranking_evidence_url: "detail_page"`.
Use `ranking_rank_source: selector` for a grounded row rank field.
Use `ranking_rank_source: document_order` for a grounded one-based bounded row order when the page has no rank field.
The default is `selector` for legacy ranking declarations, and selector mode requires `ranking_rank_selector`.
Document-order mode is limited to the ranking-table contract and must not be applied to generic linked lists.
The accepted `expected_page_heading` records feasibility evidence, but ranking page shape uses the category marker and reporting-period declarations for each weekly page.
The reporting period is checked against the pinned probe or collection run date and is not pinned in canonical weekly metadata.
When a page prints one list date outside its category heading, declare `ranking_reporting_date_selector` (probe flag `--ranking-reporting-date-selector`) using the supported class-selector syntax.
It must resolve to exactly one visible element outside site chrome containing exactly one date; the reporting-period pattern reads that element, and the date-format and maximum-age checks apply to that date.
Missing, hidden, duplicated, malformed, future, or stale date evidence fails closed, with no heading fallback.
Without this declaration, the existing two-date reporting-period-in-heading contract applies unchanged.

### Missing author verification by ISBN

For a ranking row with a missing author and a visible ISBN-13, run the separately authorized bounded check:

```bash
./scripts/check-isbn-author --isbn 9783462055306 --title 'Jenseits der Brandmauer' \
  --output tmp/isbn-author-verification/9783462055306.json
```

The check makes one GET to the DNB SRU catalog, with a 15-second timeout, 100,000-byte bound, no redirects or retries, and at most two returned records. It accepts only one record with the exact ISBN, checksum-valid ISBN-13, normalized exact main title, one explicit author role (`aut`), and a simple author responsibility statement. Ambiguous, missing, or conflicting data remains unverified. It retains only the needed bibliographic metadata, record URL, and digest; it discards descriptions, subjects, prices, and content links. The optional output file must not exist.

Pass the returned JSON array as `--ranking-author-evidence-json` on a later `./scripts/probe-source --re-probe` call. This optional `ranking_author_evidence_json` declaration is retained with the ranking contract metadata, accepted capture, and ledger so replay and collection can verify it without contacting a catalog. A probe still makes only its original bounded HEAD/GET calls to the proposed source.

At most five retained checks and 100,000 JSON bytes are accepted. The parser rechecks the evidence digest, catalog URL, MARC record, author metadata, ISBN, and title. Verified author data fills only a missing author on the matching ISBN/title row, and never overwrites a visible author. An unknown ISBN still has no fallback and requires its own separately authorized check. This does not declare authors optional and does not add automatic catalog queries to the weekly run. Source onboarding still requires the normal accepted-proposal, replay, validation, and separate confirmation gates.
When diagnostics report both `%d/%m/%Y` and `%m/%d/%Y`, the operator must resolve the order from first-party source context or a later unambiguous observation and must not guess.
Do not set `metadata_exact_book_list` on a ranking-table source.
- [ ] **Extraction notes are metadata-only**: preserve authoritative original
      title and primary author; helper translation/transliteration is
      non-authoritative; never retain review prose, blurbs, or article bodies
      (`FORBIDDEN_COLLECTOR_RESPONSE_FIELDS` enforces this at collection
      time).
- [ ] **Replay overlay** (`current-weekly-pilot.json`): `extraction_method`
      (default `codex-extraction-replay`) plus `source_page_fixture_file` and
      `extraction_response_file` pointing under `data/current-weekly-pilot/`.
- [ ] **Live overlays** (`current-weekly-pilot-live.json`,
      `current-weekly-live.json`): the live extraction method and capture
      file names.
- [ ] **Collector choice**: reuse `codex-extraction-replay` /
      `agent-extraction-*` (ADR 0006). A bespoke collector (new
      `COLLECTOR_REGISTRY` entry in `autoscout/source_collectors/`) is the
      exception and needs its own justification, tests, and fixture family -
      see the BookPage/NPR pattern.

## 4. Replay fixtures first

Never onboard straight to live. Capture fixtures under
`data/current-weekly-pilot/<source-slug>-page.json` and
`<source-slug>-extraction.json`:

- [ ] **Page capture**: `source_name`, `source_url`, `resolved_url`,
      `source_page`, and minimal representative server-rendered HTML
      containing the qualifying list links and authors. No copied article
      prose.
- [ ] **Entry markup keeps its live structure**: the fixture preserves the
      live element nesting and element boundaries of every qualifying entry
      exactly as the page serves them — a card whose year, title and author
      sit in separate child elements of one link must stay that shape.
      Trimming unrelated page chrome (navigation, imagery, prose) is fine;
      restructuring an entry's markup (for example flattening a whole book
      card into a bare-title link) is not, because a restructured fixture
      can satisfy a gate the live page then fails on every run (issue #715).
      The accepted anchor shapes and whole-segment title rule are documented
      in the `exact_book_list_mention_is_grounded` docstring in
      `autoscout/source_page_identity.py`.
A ranking-table page capture contains bounded rows with title, primary author, category, reporting period, and the configured list URL.
A single-page ranking also contains its identifier in the row.
A detail-route ranking contains exactly one declared same-domain detail link in each row and stores the matching detail captures in a deterministic `detail_pages` list.
The top-level capture `source_page` is the configured ranking-list URL.
An award-list page capture contains the page heading, the text stating the award year, each declared category heading with its row lines, and a `reference_date`.
A declared title element stays in each row line.
A declared category label stays a bold paragraph.
Every award-list mention uses the configured page as both `source_url` and `evidence_url`, and carries no `source_week`.
Each detail item uses URLs canonically equivalent to the requested row-linked detail URL for `source_page`, `detail_url`, and `resolved_url`, together with sanitized `html`.
The top-level capture keeps `source_url` as the configured list URL, and each mention keeps `source_url` as that list URL and `evidence_url` as the detail URL.
The ranking rank is either a grounded row field or the declared one-based bounded row order.
Generic linked-list captures do not use document order as a rank.
A hidden machine-readable identifier is allowed only through the declared ranking-row identifier selector.
A detail-route replay must ground each requested ISBN and raw category inside its one declared visible bounded detail scope.
Retained detail evidence must contain a complete detail page for every surviving list row before replay generation can proceed.
Rows whose grounded raw category is outside the exact allowlist are skipped.
The canonical `candidate_category` remains the configured eligible category.
- [ ] **Representative entry limit**: replay evidence uses at most three representative entries.
      The selection is deterministic: the first three eligible bounded entries in document order, however long the source list is.
      A longer bounded list produces three selected extraction mentions and
      three helper sidecar entries.
      Document-order ranking fixtures may also retain bounded unselected
      positional rows so selected ranks remain their original one-based
      document positions.
- [ ] **Extraction fixture**: a bounded representative subset of mentions
      with exact original titles, primary authors, evidence URLs that are
      verifiably present in the page capture, the configured `signal_type` /
      `signal_strength` / `candidate_category` values preserved exactly, and
      concise metadata-only notes.
- [ ] **Retained helper metadata sidecar**: a version-1 JSON file supplied to
      `capture-source-fixtures` and `scaffold-source` with `source_url`,
      `language`, and one `entries` item for every selected representative
      title, author, and evidence URL.
      The sidecar must carry exactly the selected representative entries, in
      the same document order, and nothing else.
      Because replay evidence uses at most three representative entries, a
      sidecar with one entry per item of a longer bounded list is rejected as
      an extra-entry disagreement.
      Each item must contain nonblank retained `english_title`,
      `search_translation`, and `author_transliteration_note` values.
      English normal exact-list sources require `english_title` equal to the
      original title; non-English sources give the English rendering, which
      may equal the original title when the title reads the same in English
      (a name or a shared word), and `search_translation` must differ from the
      original title.
      Missing, stale, mismatched, or placeholder helper input fails before
      fixture or configuration writes.
      The generator never translates, infers, or fills helper values.

## 5. Validation

- [ ] **Shared exact-list contract**: a portfolio entry with
      `metadata_exact_book_list: true` and replay fixtures is automatically
      enrolled in `tests/test_exact_book_list_source_contract.py`. The shared
      battery checks four-config parity, page identity, grounding, metadata
      safety, replay behavior, deterministic exclusions, health, and failure
      isolation from configuration and fixtures.
- [ ] **Source-specific coverage only when needed**: keep tests for pinned
      books, URLs, or genuinely source-specific behavior out of the shared
      contract. New files use `tests/test_source_<source>.py`, with the
      onboarding issue number in the module docstring. Existing issue-numbered
      filenames remain unchanged for history; do not copy a full prior source
      test file as an onboarding template.
- [ ] **Replay run is green**: the replay pipeline collects the source, the
      new source appears as `collected` in `source-runs.json`, and its
      mentions appear in `source-mentions.json` with correct signal fields.
- [ ] **Full suite passes** (`pytest`), including config validation and the
      golden/weekly-fixture runs - this is the regression backstop that
      existing outputs did not shift.
- [ ] Only after replay is proven, enable the live variant. A misbehaving
      live source degrades to a `failed`/`skipped` source-run record; it must
      never break the weekly report.
The configured SourceCollection replay uses the same declared contract family as the retained probe.
A live source failure is isolated to its source-run record and sibling sources continue.

## 6. Probation entry

- [ ] The portfolio entry carries `lifecycle_status: "probation"` and a
      `lifecycle_note` naming the onboarding issue and stating the source is
      active and reversible pending source metrics.
- [ ] The source is then judged by the periodic keep/retire/promote source
      review (`reqs/07-learning-loop-and-source-lifecycle.en.md`);
      retirement is soft and reversible (`active: false` with a recorded
      reason).

## 7. Commit

- [ ] One commit (or PR) containing together: the portfolio/config changes,
      the replay fixtures, any source-specific test that is actually needed,
      any composition pin the new source moves (declared with
      `--onboarding-artifact`), and the feasibility evidence doc. The config `notes` field references
      the onboarding issue number.

## 7.1 Source-gap discovery

Use `./scripts/add-source discover` when the editor explicitly requests source-gap discovery without a URL.
Use `./scripts/add-source discover --signal-family '<family>'` to limit discovery to one canonical signal family.
The offline command needs no URL.
The Search Optimization Review is optional.
Without `--review`, discovery reads `runs/current-weekly-pilot-russian/search-optimization-review.json` only when it exists.
When absent, configured sources are labelled `not-reviewed` and no allocation recommendations are used.
Use `--review <path>` as an explicit override; that path keeps strict file and schema validation.
The command resolves a `weekly_portfolio` reference relative to the supplied `--portfolio` path before validation and analysis, including overlay active state.
It crosses the resolved configured portfolio with the allocation labels in the Search Optimization Review when one is available.
It reports thin or reduce-dominated signal families and proposes only repository-grounded source URLs, names, families, and concise reasons.
Unclassified sources are surfaced once in `sources_without_signal_family` with bounded identity fields and a missing or unsupported reason.
It does not inspect or report country, market, or language gaps.
It is strictly offline and read-only.
It does not probe, fetch, invoke a model, repair a ledger sidecar, or change configuration, Source Proposals, Source Mentions, Scouting Candidates, Candidate History, or weekly state.
Configured URLs are silently omitted.
URLs already in the Source Candidate Ledger are reported as ledger exclusions before probing.
Successful offline reports have top-level status `success` and return exit code 0.
Reports with status `insufficient_metadata` return exit code 0 and suppress weakness conclusions, candidate proposals, and probing.
Supply canonical signal-family metadata or a resolvable weekly portfolio reference before relying on exact coverage.
Input, configuration, review, and ledger failures have status `input_error` and return exit code 2.
A structured live-probe exit code 2 has status `probe_error`.
Unsupported options such as `--url` are discovery input errors and return exit code 2.
The offline command stops after its report.
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

The separate monthly report trigger is `./scripts/add-source monthly --operation-month YYYY-MM` for «Запусти ежемесячный поиск источников за YYYY-MM».
It is an external or editor-controlled schedule target with no installed or active scheduler, and merging code alone does not activate it.
Its complete offline, idempotency, artifact, and non-effect contract is defined in `docs/operator/sources.md`; it is not an onboarding or probe command.

## 8. Offline confirmation-gated driver

The URL-driven workflow must call `./scripts/add-source probe` first with the exact Russian network rationale «Для проверки доступности предложенных источников нужен сетевой доступ. Разрешить запуск?»
That mode delegates to `./scripts/probe-source`.
The probe is the only network step and the workflow stops on reject or defer with the recorded reason and one concrete action.
Direct `./scripts/probe-source` uses the legacy batch probe contract: 0 when any candidate is accepted, 1 when candidates are rejected or malformed without an accepted candidate, 2 for command, input, or storage errors, and 3 when all candidates defer.
The offline `./scripts/add-source` contract is 0 for success, 1 for correctable or other errors including repeated `--url`, 3 when confirmation is required, 4 for retained-evidence defer, and 5 for retained-evidence reject.
After an accepted probe and completed evidence, all preview and confirmation calls use `./scripts/add-source`.
The add-source command accepts one scalar `--url` value and rejects repeated URL options.
The `add-source` command is the offline driver for the complete checklist.
It consumes the accepted retained probe capture and never performs a network or model call.
Use `--keep-captures`, one explicit ledger JSON path, and an explicit feasibility-draft directory with `./scripts/add-source probe`.
The probe creates the ledger Markdown sidecar beside the JSON ledger and renders prior verdict dates, outcomes, and reason codes without changing current-source counts.
Structural diagnostics for rejected GET responses retain no raw bytes, titles, authors, descriptions, review prose, identifiers, prices, or other page text.
A rejected GET verdict may carry `structural_diagnostics` with only repeated visible direct-child signatures and bounded counts, shaped as `{"repeated_siblings": [{"parent": {"tag": "...", "class_tokens": [...]}, "child": {"tag": "...", "class_tokens": ["row"]}, "count": 2}]}`.
The diagnostic ranks repeated groups by bounded frequency with stable structural tie-breakers, reports only child signatures with a safe class token, and uses visible rendered elements only, ASCII identifier-shaped tags and class tokens, 48-character token truncation, sensitive-token filtering, at most 24 class tokens, at most 12 structures, and counts from 1 through 1000.
Keep probe captures, ledger files, and feasibility drafts as separate read-only outputs.
Do not pass ledger or capture-output options to the offline `./scripts/add-source` phase.
The offline phase consumes only the retained accepted probe evidence and onboarding inputs.
Retain the exact helper metadata sidecar produced for the same representative entries.
Replay evidence uses at most three representative entries, so the sidecar has at most three `entries` items.
Read the selected identities from the preview report field `representative_entries` and mirror them exactly, in the reported order.
Prepare the sidecar in this shape, with one item per reported representative entry:

```json
{
  "version": 1,
  "source_url": "https://example.test/list",
  "language": "en",
  "entries": [
    {
      "title": "<exact visible title>",
      "author": "<exact visible primary author>",
      "evidence_url": "<exact book-level link>",
      "english_title": "<English title>",
      "search_translation": "<Russian search phrase>",
      "author_transliteration_note": "<Russian author transliteration>"
    }
  ]
}
```

The preview runs the same helper-sidecar agreement check as the confirmed operation.
A stale, missing, extra, duplicated, reordered, or mismatched entry fails the preview with exit code 1, a specific `reason_code`, one concrete `next_action`, and the `representative_entries` the sidecar must mirror.
The reason codes are `helper_metadata_unreadable`, `helper_metadata_schema_invalid`, `helper_metadata_entry_invalid`, `helper_metadata_duplicate_entry`, and `helper_metadata_identity_mismatch`.
The editor is therefore never asked to confirm helper metadata that the confirmed fixture-generation contract must reject.
Supply every `SOURCE_SCAFFOLD_REQUIRED_METADATA_FIELDS` value explicitly.
Only `expected_sort_label` may be omitted.
Supply the completed feasibility evidence file with `--feasibility-evidence` and its canonical destination with `--feasibility-doc`.
The destination must be `docs/issue-<N>-<market>-source-feasibility.md` for the explicit numeric onboarding issue.
Supply the onboarding issue identifier with `--onboarding-issue`.
If `--proposals-file` is supplied, its basename must be exactly `source-proposals.json`.
The lifecycle note, config notes, and completed feasibility evidence must use the normalized onboarding issue as an explicit `#<N>` reference.
The commit message uses the same normalized issue identity.
The unconfirmed preview returns `confirmation_required=true` and has zero filesystem, Git, network, model, configuration, fixture, evidence, and proposal-state mutation.
An accepted preview requires an explicit ISO `--run-date`, and confirmation must repeat that exact date.
The preview returns the complete onboarding request, the effective run date, every onboarding input path, every onboarding input SHA-256 digest, the canonical proposal-state path and presence flag, and the proposal-state SHA-256 digest when present.
The deterministic `confirmation_token` covers that complete artifact identity and request.
Do not add `--confirmed-by-editor` until the editor confirms the exact metadata, slug, reason, issue, run date, artifact identity, and token.
The confirmed invocation must pass the exact token and the same explicit ISO `--run-date`.
The confirmed invocation copies the helper sidecar to exactly `<slug>-helper-metadata.json` beside the replay fixtures.
The generated portfolio and three overlays also carry the relative `helper_metadata_file` reference atomically, so Codex replay and live agent extraction use the same retained sidecar.
The runtime reference is part of the replay source metadata and the helper sidecar bytes are included in the run evidence digest.
The driver validates replay first in a temporary repository.
That temporary repository never receives the canonical `.git` directory; it gets its own local throwaway Git repository with one baseline commit, so repository tests that assert Git facts run correctly inside the stage and no coverage is dropped.
The throwaway repository is fully isolated: it reads no system, global, or user Git configuration, it resets every inherited credential helper, it resolves hooks and its init template to stage-only paths that are never created, and it has no remote, no worktree registration, and no alternate object store.
Every staged Git command and every staged validation subprocess also runs without the Git variables that relocate a repository, work tree, index, or object store, so an inherited `GIT_DIR`, `GIT_WORK_TREE`, or `GIT_INDEX_FILE` can never reach canonical Git metadata or run a canonical hook.
Canonical Git commands drop those same relocating variables while keeping the operator's own identity configuration, so the working directory is the only authority for which repository a command touches.
The throwaway repository is removed with the stage.
It then runs `tests/test_source_onboarding_contract.py`, `tests/test_exact_book_list_source_contract.py`, and the full pytest suite in that order.
Canonical files are not published while those validations run.
A validation failure reports the stage that failed and preserves every validation stage that already passed.
A source-specific test is accepted only when it is an explicitly supplied dirty file directly under `tests/` named `tests/test_source_<name>.py` with a module docstring containing the normalized onboarding issue reference.
Probe evidence, retained captures, ledger JSON and Markdown, and feasibility drafts remain separate read-only probe outputs.
They are excluded from the onboarding commit.
A new source can require a change to a repository-wide composition pin, for example the pinned portfolio size in `tests/test_issue_645_english_title_helpers.py`, the verdict baseline in `tests/fixtures/issue-566-configured-source-verdicts.json`, or the dispatch baseline in `tests/test_issue_568_dispatch.py` (#664).
Make each pin change in the working tree and declare the file with a repeatable `--onboarding-artifact`, in the preview and in the confirmed invocation.
An onboarding artifact is accepted only when it is an existing regular file under `tests/`, is not a symlink, is declared once, and is not the source-specific test.
The preview reports each declared path and SHA-256 digest under `artifact_identity.onboarding_artifacts`, and the `confirmation_token` covers them, so a changed, added, or removed artifact requires a new preview.
The confirmed invocation requires every declared artifact to carry a working-tree content change, validates the staged suite with those changes, and commits them with the bundle.
Do not commit a pin change ahead of the onboarding commit: the pin would be wrong until the source arrives.
The final local commit contains the accepted proposal, four configs, both replay fixtures, the canonical helper sidecar, the completed feasibility evidence, an explicitly supplied source-specific test, and the declared onboarding artifacts.
The driver rejects every other dirty path as unrelated and never pushes.
A failure before commit restores the canonical files and Git index.

## 9. Night scouting (#896)

An agent can do steps 1 to 6 overnight and prepare the commit of step 7, which still waits for the operator's confirmation.
Start it in the main checkout on the always-on machine, either in Claude Code or in Codex:

```
/scout-sources                              find new sources (Claude Code)
/scout-sources <url> [<url> ...] [note]     onboard these sources (Claude Code)
./scripts/night-scout                       find new sources (Codex)
./scripts/night-scout <url> [...] [note]    onboard these sources (Codex)
```

`./scripts/night-scout` runs Codex with web search, full access and no approval prompts, since nobody answers them overnight; set `NIGHT_SCOUT_MODEL` or `NIGHT_SCOUT_EFFORT` to change the model or effort.
Both run the same procedure.

Without arguments, the run searches under a rotating theme, probes up to 15 candidates, keeps those `./scripts/source-novelty` calls `promising`, and prepares at most two.
It skips pages that list everything published in a market or genre, because the editor wants sources where someone has selected the books.
With URLs, it skips the search and works on those sources only, in the order given, for up to six hours.
It tries a declarative contract, then data embedded in the page, then the page's own data endpoints, then agent extraction, then a bespoke collector with tests.
It stops on a site only when the site refuses automated access, such as a login, a ban in its terms or `robots.txt`, or a CAPTCHA.
Any text after the URLs is a note the run follows, such as which section of the page to collect.

In the morning, read `data/night-scout/<date>/report.md` in the main checkout.
Each prepared source has a draft PR against the night's `night-scout/<date>` branch, and the report gives its confirmed `./scripts/add-source` command.
The operator's explicit confirmation authorises that command in place of the editor's (`docs/agents/contracts/night-source-scouting.md`).
Merge the night PR first; after the first source merges, re-run the second source's preview, because its confirmation token is stale.
A source the run could not onboard is listed with every approach it tried and what would unblock it.

The run never confirms, merges, changes Source Proposals, or writes weekly or editorial state.
The procedure is `.claude/skills/scout-sources/SKILL.md`.

## A source that may publish nothing (#464)

A Configured Source that produces no mention always carries a reason.
An empty reason is not an acceptable outcome.

The reason is derived from what the source publishes, not from what it is called.

A source that reports only items it has not seen before declares a seen-item ledger, through `seen_guids_file` in a live config or `seen_guids_fixture_file` in a replay config.
Nothing new in a week is its normal state, so its zero is recorded as collected with a named reason.

Every other source republishes a complete list on every check.
A chart, a bestseller list, or a bounded recommendation list that publishes no entry is broken, so its zero is a failure and counts against source health.

A source that publishes a complete list but may legitimately stand empty between announcements declares that:

```json
"may_report_no_entries": true,
"may_report_no_entries_reason": "an annual award page lists no laureate between announcements"
```

The reason is required.
A silent permission to contribute nothing is the fault this rule removes, so a declaration without a recorded reason is refused at config load.
The declaration does not apply to a source that already reports only new items, and declaring both is refused.

The declaration permits an empty page.
It never permits losing entries: a capture from which extraction produced entries that the contract then refused every one of is a failure whatever the source declares, and the reason names how many were lost and why.

## A dated ranking capture must carry a readable reference date (#599)

A ranking-table source that declares `ranking_reporting_period_date_format` can have its reporting period aged, so the period is aged on every route.
For a visible CPNB edition label such as `Week 40 - 2026`, the explicit
`iso-publication-week-wednesday` format maps the ISO year/week to its Wednesday
publication date. It requires `ranking_reporting_date_selector`, preserves the
original label, and rejects invalid ISO weeks, duplicate labels, future editions,
and editions older than the declared maximum age. This is an edition date, not
a sales-period or book-publication date (#776, editor authorized).

Ageing needs a reference date to compare against, and a capture that carries none can never be judged stale.

Three outcomes are named separately, because they are different facts:

- the capture states a `reference_date` the system cannot parse, which is a page shape failure naming the field;
- the capture states no date at all, which is refused because a declared maximum age cannot be enforced without one;
- the period is readable and older than `ranking_reporting_period_max_age_days`, which is the ordinary staleness refusal.

The refusal is at read time rather than at capture time.
A writer-side check would protect new captures and leave every committed fixture unaged, and a committed fixture with no reference date is exactly what #597 rested on.

What this does not catch is a page that carries a well formed but factually wrong date.
Nothing in a page proves its own date, so that case needs different evidence.

## Annual rollover: applying one confirmed proposal (#680)

`./scripts/audit-annual-sources --live --propose` reads the pages each annual source declares and prints, for a source whose edition or award stage has moved on, one proposal block ending in `proposal.mutation=none` and `proposal.confirmation_required=true`.
The audit proposes and never applies: it changes no configuration, no weekly state and no repository file.
An edition proposal keeps the tracked stage of a source that declares no `stage_markers`, because one URL carries its whole cycle; a source with `stage_markers` is proposed at its first stage unless the new page shows a later one.
A stage-change proposal is a review prompt; moving to the newer stage, keeping the tracked stage and retiring the source until the next edition are all the operator's decision.

Apply one proposal at a time, only after the operator confirms it:

1. **Feasibility.** For a new URL, probe it with the command in the proposal's first `proposal.validation` line and keep the evidence (section 1). A URL "to be located from the landing page" is found by hand on that page first.
2. **Record handling.** `rename-in-place` edits the existing record. `retire-old-and-add-new` keeps the old record retired (`source-lifecycle retire`, with a reason naming the issue) and adds a new one, because a frozen model-qualification benchmark case names the old source and stays valid only while that source is configured. `update-in-place` keeps the name. Removing the benchmark case instead, as #762 and #826 did, lowers the benchmark floor and is a separate operator decision.
3. **Replay fixtures first.** Refresh every fixture file the proposal lists for the overlays, then the helper-metadata sidecar and its sha256 (section 4).
4. **Source contract.** Update every page-shape field and the `annual_edition` block the proposal lists, the three overlays, and the display label in `autoscout/html_review.py`; rerun the audit with `--source "<new name>"` against the kept evidence.
5. **Full suite.** `./scripts/check` (section 5), including any test the proposal names as spelling out the old source.
6. **Canonical persistence.** Commit and push the change as one source change (section 7).

Books already in Candidate History keep their identities: a Candidate identity is the book's title and authors, not the source name.
