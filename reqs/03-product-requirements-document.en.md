# AutoScout Product Requirements Document

Status: draft PRD v0.1
Date: 2026-05-18
Source requirements:
- `reqs/01-scouting-workflow-and-sources.en.md`
- `reqs/02-editorial-fit-and-risk-screening.en.md`

This PRD translates the consolidated requirements into a product shape: final vision, MVP, staged roadmap, functional requirements, and acceptance criteria.

Important framing:
- AutoScout is a scouting and editorial-intelligence product, not a legal decision system.
- Risk labels are operational screening labels and must not be presented as final legal advice.
- The product should preserve evidence and uncertainty instead of pretending that automated checks are definitive.

---

## 1. Product summary

AutoScout helps a Russian publishing editor discover promising adult nonfiction books from international public-market signals, check whether they are already published in Russian, flag visible editorial/legal risks, and produce a prioritized weekly scouting report.

The core output is not “a database of all books.” The core MVP output is a decision-ready CSV/table watchlist: a small set of externally validated book candidates, each with evidence links, signal count, Russian-edition status, visible-risk status, and an editor note field. Candidate memos are deferred until later and should be generated only for books the editor shortlists as interesting.

The long-term product can grow into a fuller acquisition-support system: source monitoring, candidate enrichment, editorial fit scoring, risk screening, competitor research, manuscript analysis, and editorial-board memos.

---

## 2. Problem statement

Editors need to identify foreign nonfiction titles early enough to evaluate translation/acquisition opportunities. Manual scouting is time-consuming because it requires checking many fragmented sources:

- awards
- bestseller lists and sales rankings
- media lists and reviews
- reader-interest platforms
- audiobook rankings
- rights and deal mentions
- Russian retailers and publishers
- visible risk indicators from public descriptions, reviews, samples, and author materials

A simple topical search is not enough. A book should enter the pipeline because an external signal suggests market, critical, rights, or reader interest.

Editors also need to avoid wasting time on candidates that are already published in Russian or obviously unsuitable because of editorial mismatch or visible risk content.

---

## 3. Target users

### 3.1 Primary user

Russian publishing editor / acquisition scout for adult nonfiction.

Initial validation workflow:
- Dima checks the technical implementation and generated CSV first.
- Dima then shares the CSV with the editor friend, likely by importing it into Google Sheets.
- The editor reviews/edits the Google Sheet and returns feedback to Dima.
- Dima exports or converts the edited sheet back to CSV and feeds the edited CSV back into AutoScout.

Needs:
- find promising books before competitors do
- understand why each candidate matters
- see evidence, not just AI summaries
- quickly reject already-published or risky books
- keep a weekly rhythm of review
- add human editorial notes

### 3.2 Secondary users

Editorial-board analyst / project editor.

Needs:
- prepare deeper analysis for shortlisted books
- check editorial fit
- check Russian-market relevance
- compare with real Russian-market competitors
- produce a recommendation memo

### 3.3 Future users

Rights manager, publisher, or legal reviewer.

Needs:
- see source evidence and risk categories
- understand whether legal review is needed
- avoid ambiguous unsupported claims

---

## 4. Product goals

1. Produce a weekly ranked list of adult nonfiction candidates with external validation signals.
2. Preserve source evidence for every candidate.
3. Check whether each candidate appears to have a Russian edition.
4. Flag visible risk content from public materials using explicit uncertainty labels.
5. Make the editor’s review faster, not replace the editor.
6. Keep MVP narrow enough to ship and test with real editorial feedback.

---

## 5. Non-goals

The MVP will not:

1. Scrape every source in the full source inventory.
2. Bypass paywalls or subscription-only sources.
3. Make final legal conclusions.
4. Guarantee that a book has no Russian edition.
5. Analyze full manuscripts unless a later stage adds uploaded-file support.
6. Build a complex dashboard before the weekly-report workflow proves useful.
7. Include fiction, children’s books, YA, or narrow academic titles without mass-market signal.
8. Select books by topic alone without external market/editorial signals.

---

## 6. Final product vision

The final version of AutoScout is an acquisition-intelligence pipeline with five connected layers.

### 6.1 Source monitoring layer

Continuously monitors or manually imports sources from selected families:

- awards and shortlists
- bestseller/ranking pages
- media “best books” and review pages
- Goodreads / StoryGraph / reader-interest signals
- audiobook rankings
- rights and deal mentions
- fair catalogues and rights pages
- Russian retailers and publisher catalogues

Each source record stores:
- source name
- source family
- URL
- access status: open, partially paywalled, paid/subscription required, blocked, manual import
- fetch/check date
- extracted candidate titles, if available
- Evidence Note or snapshot metadata, where allowed and copyright-safe

### 6.2 Candidate intelligence layer

Normalizes candidate books across sources and enriches them with:

- title
- primary author(s)
- additional contributors, where useful
- original publisher
- publication year/date, including expected/forthcoming date when applicable
- category/topic
- description/annotation
- ISBN, where available
- links: publisher page, Amazon, Goodreads, review pages, source evidence
- signal list
- independent signal count
- signal strength
- duplicate-source merging
- freshness / recency

The system should group the same work even if titles, editions, listings, or metadata vary across sources. Editor decisions attach to the work-level candidate, while edition/listing details remain evidence or metadata.

### 6.3 Editorial-fit layer

Evaluates whether a candidate appears aligned with the publisher’s nonfiction interests:

- business
- popular psychology / self-development / personal effectiveness
- popular science
- health / wellness / beauty
- travel / country studies / cultural studies
- parenting
- memoirs, journalism, public-affairs writing when relevant
- gift editions and sets, if data supports this category later; MVP does not treat gift format as a separate category

Fit assessment should consider:
- charismatic author
- lively, accessible language
- scientific or empirical basis
- no esotericism
- novelty, usefulness, and reader value
- Russian-market relevance
- broad enough audience

### 6.4 Risk and Russian-market screening layer

Checks:

1. Russian-edition status:
   - `not found in Russian`
   - `found in Russian`
   - `unclear`

For MVP reporting, candidates with `found in Russian` should be excluded from the final candidate CSV rather than shown as low-priority rows. They may still be kept in internal run logs for traceability.

2. Scouting-stage visible-risk status:
   - `no visible risk`
   - `clear visible risk`
   - `text check required`

Risk comments should name the visible category. MVP detection should start with the first five highest-priority categories:
- Russia / USSR / Putin / contemporary Russian politics
- war / anti-war framing / Russian Armed Forces
- WWII / Great Patriotic War / USSR vs Nazi Germany comparison
- non-traditional relationships / gender transition / rejection of childbearing
- drugs / microdosing / psychoactive substances

Later versions may add:
- suicide
- Nazi symbols / justification of Nazism
- extremist / terrorist / undesirable organizations
- foreign agents / extremist / undesirable organization
- esotericism / pseudoscience
- violence / crime / lawbreaking
- obscene language

The final system may support full-text manuscript analysis, but scouting-stage checks must remain separate from full-text review.

### 6.5 Editorial workflow layer

Supports a weekly editor workflow:

1. System collects candidate signals.
2. System deduplicates and enriches candidates.
3. System ranks candidates by signal strength and fit.
4. System flags Russian-publication and visible-risk status.
5. Editor receives a weekly report.
6. Editor marks candidates as:
   - interested
   - maybe / watch
   - reject
   - already known
   - needs deeper analysis
7. For deeper analysis, system can generate an editorial-board memo in later stages.

---

## 7. MVP definition

### 7.1 MVP objective

Prove that AutoScout can produce a useful weekly scouting report for a real editor from a limited set of sources, with enough evidence and structure to support fast human review.

The MVP success question:

> Can the system reliably deliver 10 plausible editor-facing adult nonfiction candidates per week after exclusions, sorted by evidence strength, with source links, Russian-edition status, visible-risk status, and clear editor-review fields?

### 7.2 MVP user experience

The MVP should generate a CSV report rather than a full web app. Markdown or Google Sheets export can be added later only if the editor workflow requires it.

Visible risk flags should be shown directly in the main CSV report, not hidden in a separate internal-only view.

Minimum weekly report columns, in order:

1. Title
2. Author
3. Annotation
4. Description
5. Signal count
6. Visible-risk status
7. Risk comment
8. English title
9. English author display
10. English annotation
11. Check date
12. Source
13. Source URL
14. Publisher / year
15. Category / topic
16. Why it entered the list
17. Signal summary
18. Priority label
19. Russian-edition status
20. Russian-edition URL
21. Links
22. Editor status
23. Editor note

`Annotation` is a bounded synthesized scouting note from safe metadata, evidence notes, signal context, public-data status, and visible-risk context. `Description` is original English `content_description` scouting prose when available, falling back to the short description. Neither field should store copied source bodies, reviews, publisher descriptions, jacket copy, or book samples.

### 7.3 MVP source scope

Start with a deliberately small, high-signal set of source families.

Working decision for the first prototype: exact source choice is not strategically important yet. The first prototype may use a pragmatic/random-but-reasonable source mix, as long as it includes at least a couple of German-market sources.

Recommended first-prototype source set:

1. Awards:
   - Financial Times Business Book of the Year, where accessible
   - Porchlight Business Book Awards
   - Baillie Gifford Prize for Non-Fiction
   - Royal Society Science Book Prize

2. Media / curated lists:
   - McKinsey Author Talks / McKinsey on Books
   - The Guardian Books
   - NPR Books
   - Five Books
   - Next Big Idea Club

3. Reader / interest signals:
   - Goodreads Choice Awards
   - Goodreads Most Anticipated or year lists, where accessible

4. Retail / ranking signals:
   - Amazon US selected nonfiction categories
   - Amazon UK selected nonfiction categories
   - Audible selected nonfiction/business categories
   - Amazon Germany selected nonfiction categories
   - Der Spiegel Bestseller — Sachbuch

5. Russian-edition checks:
   - Ozon
   - Labirint
   - LitRes
   - Chitai-gorod
   - Book24

Russian publisher-site checks are deferred; MVP Russian-edition verification uses retailers only, even when retailer results are unclear.

This is enough to test value without drowning in source complexity. The source list can be changed after the first report if the editor finds the results noisy or irrelevant.

### 7.4 MVP must-have behavior

The MVP must:

1. Accept a configured list of sources. Candidate discovery uses configured sources only; targeted search is allowed for enrichment and Russian-edition checking after a candidate already has an External Signal.
2. Automate only easy/open sources in the MVP; fragile, blocked, paid, or subscription-only sources should be skipped or logged as inaccessible rather than handled through manual import or brittle scraping.
3. Collect candidate books from easy/open configured sources and log skipped sources when automation is unreliable. Non-English adult nonfiction can be included when metadata is sufficient for identity, evidence, and Russian-edition checking.
4. Filter out obvious non-target categories:
   - fiction
   - children’s / YA
   - esotericism, pseudoscience, or devotional religion as the book's core proposition
   - narrow academic books without mass-market signal or general-reader accessibility
   - books with no external signal
   - gift editions/sets unless they are clearly adult nonfiction books with normal candidate metadata and External Signals
   - narrow professional manuals, textbooks, certification guides, or technical reference works without broad adult nonfiction appeal
5. Normalize book records.
6. Merge obvious duplicates across sources.
7. Count independent external signals.
8. Sort candidates by independent signal count and signal quality.
9. Add source evidence links for every signal.
10. Check Russian-publication status using multiple search methods:
   - original title
   - author name in Latin script
   - author transliteration as a search note only
   - possible Russian title translation as a search note only
   - ISBN, if available
11. Record Russian-edition status as `not found in Russian`, `found in Russian`, or `unclear`.
12. Exclude candidates with `found in Russian` from the final MVP candidate CSV; keep them only in internal run logs if useful for traceability.
13. Run a public-data visible-risk check from descriptions, reviews, table of contents, samples, and author/media materials when available, focused on the MVP risk categories: Russia/politics, war/anti-war/Russian Armed Forces, WWII/USSR/Nazi Germany comparison, non-traditional relationships/gender transition/rejection of childbearing, and drugs/microdosing/psychoactive substances.
14. Record risk status as `no visible risk`, `clear visible risk`, or `text check required`. `No visible risk` means no configured MVP risk signal was found in available public materials; it is not legal clearance or full-text review.
15. Generate a weekly report with editor-review fields.
16. Preserve uncertainty and avoid unsupported claims.

### 7.5 MVP should-have behavior

The MVP should:

1. Keep a local candidate history for editor-facing and excluded candidates so the same candidate is not repeatedly presented or rediscovered as new.
2. Mark candidate freshness and exclusion memory:
   - new this week
   - seen before
   - signal increased
   - Russian status changed
   - excluded before, with internal exclusion reason and evidence
   - re-entered after Russian status changed
   - material change since last editor decision
3. Generate concise candidate memos later only for books the editor shortlists as interesting.
4. Keep extraction logs for debugging, limited to source URL, check time, status, candidate identifiers found, parser/debug messages, and concise Evidence Notes; do not store full page bodies, full reviews, or long copied excerpts in normal MVP logs.
5. Store source access status, especially when a source is paid/subscription-only.

### 7.6 MVP not-yet behavior

The MVP should not yet include:

1. Full manuscript upload and analysis.
2. Legal-review workflow.
3. Browser dashboard with user accounts.
4. Complex scoring based on proprietary sales estimates.
5. Automated scraping of difficult/paywalled sources.
6. Full source inventory coverage.
7. Automated rights availability confirmation.

---

## 8. Candidate scoring model

The first scoring model should be simple and explainable.

### 8.1 Score components

Candidate rank should prioritize:

1. Independent signal count
   - award + media + reader signal is stronger than three mentions from the same source family.

2. Signal strength
   - winner or shortlist is stronger than a generic media mention.
   - top-ranking placement is stronger than low ranking.
   - multi-territory rights/deal signal is strong, if available from easy/open sources for MVP.

3. Publisher fit
   - adult nonfiction category match.
   - practical value / novelty / usefulness.
   - author credibility.

4. Availability opportunity
   - `not found in Russian` is stronger than `unclear`.
   - `found in Russian` excludes the candidate from the final MVP candidate CSV, though it may be retained in internal logs.

5. Risk status
   - `no visible risk` is easier to review.
   - `text check required` stays in the list if other signals are strong.
   - `clear visible risk` cannot receive priority label `A`. Clear book-content risk should normally receive `D — deprioritize / review only`; author/context risk requires review and may lower priority but does not automatically force `D` unless severe or directly blocking.

### 8.2 Recommended MVP scoring labels

Instead of a fake-precise numeric score, use labels:

- `A — strong candidate`: multiple independent signals, clear fit, `not found in Russian`, and no visible risk.
- `B — promising / needs review`: good signal but has uncertainty, weaker fit, `unclear` Russian status, or text check required.
- `C — watch only`: one signal or weak/ambiguous fit.
- `D — deprioritize / review only`: clear book-content risk or other low-priority visible candidates that remain useful to show for review. True exclusions, such as Found in Russian, duplicate, no External Signal, or out-of-scope category, do not appear in the editor-facing CSV and are recorded with internal exclusion reasons in excluded history or run logs.

The report should show the reasons behind the label.

---

## 9. Functional requirements

### 9.1 Source configuration

The system must allow sources to be configured with:

- source name
- source family
- URL
- target category rules
- extraction method: automated for MVP; semi-automated or manual import may be added later
- access status
- check frequency
- notes

MVP automation rule:
- Automate only easy/open sources.
- Fragile, blocked, paid, or subscription-only sources should be skipped or logged as inaccessible for MVP rather than represented through manual import or brittle scraping.

Acceptance criteria:
- A new open source can be added without changing core candidate logic.
- Paid sources can be tracked without bypassing access restrictions.
- Failed source checks are visible in a run summary/log for Dima, not as rows or columns in the editor-facing CSV.

### 9.2 Candidate extraction

The system must extract candidate book data from configured easy/open sources for MVP.

Minimum extracted fields:
- title
- primary author(s)
- source URL
- source name
- reason for inclusion
- source date or check date

Acceptance criteria:
- Every candidate has at least one source evidence link.
- Candidates without external signal are excluded.
- Fiction/children/YA are excluded when identifiable.

### 9.3 Candidate enrichment

The system must enrich candidates with public metadata where possible.

Fields:
- publisher
- publication year/date, including expected/forthcoming date when applicable
- category/topic
- short description
- ISBN, if available
- publisher page link
- Amazon link
- Goodreads link
- review/media links

Acceptance criteria:
- Missing data is marked blank or unknown, not invented. Forthcoming dates are marked as expected/forthcoming when applicable.
- Original titles and scripts are preserved where available; English or Russian translations are not invented.
- Source URLs are preserved for verification.

### 9.4 Deduplication

The system must merge obvious duplicate candidates.

Deduplication should treat the same work by the same primary author(s) as one candidate, even when subtitles, editions, retailer pages, or publication markets vary. Do not merge revised/materially different editions with substantial new content, workbooks, companion volumes, series entries, or different books with similar titles unless evidence is strong.

Deduplication keys may include:
- normalized title + primary author(s)
- ISBN
- publisher page URL
- Goodreads work/book URL

Acceptance criteria:
- The same book appearing in three sources becomes one candidate with three signals.
- Similar but different books are not merged unless evidence is strong.
- Duplicate detection is conservative when metadata conflicts.

### 9.5 Russian-edition check

The system must check whether the candidate appears to have been published in Russian.

Search methods:
- original title
- primary author Latin name
- author transliteration as a search note only
- possible translated title as a search note only
- ISBN

Sources for MVP:
- Ozon
- Labirint
- LitRes
- Chitai-gorod
- Book24

Russian publisher-site checks are deferred until a later stage.

Statuses:
- `not found in Russian`
- `found in Russian`
- `unclear`

Acceptance criteria:
- `found in Russian` includes a URL.
- Candidates with `found in Russian` are excluded from the final MVP candidate CSV.
- `unclear` includes a short explanation, such as retailer search inconclusive and publisher-site check deferred. For forthcoming original books, use the normal statuses but include an uncertainty note such as `original forthcoming`; `not found in Russian` is acceptable after configured retailer searches find no Russian edition or preorder.
- The system does not claim “not published in Russian” absolutely; it says `not found in Russian`.

### 9.6 Visible-risk screening

The system must screen public materials for visible risk categories. For MVP, detection is limited to:

1. Russia / USSR / Putin / contemporary Russian politics
2. War / anti-war framing / Russian Armed Forces
3. WWII / Great Patriotic War / USSR vs Nazi Germany comparison
4. Non-traditional relationships / gender transition / rejection of childbearing
5. Drugs / microdosing / psychoactive substances

Drug/microdosing screening should treat the September 2025 drug-content rule as operationally relevant for current MVP screening.

Any detected risk in these MVP categories should require human/legal review. Weak or ambiguous public-data signals may still use `text check required`, but they should not be treated as cleared.

Additional risk categories can be added after the scouting workflow proves useful.

Evidence types:
- annotation
- publisher description
- table of contents
- sample pages / Look Inside
- reviews
- Goodreads reviews
- author interviews
- media materials

Scouting-stage labels:
- `no visible risk`
- `clear visible risk`
- `text check required`

Acceptance criteria:
- Risk output includes a short category label when risk is visible.
- Risk comment identifies whether evidence is a book-content signal or author/context signal.
- Clear visible risk requires credible public evidence with a link and explanation; weak, indirect, or unverified signals use `text check required`.
- MVP scouting risk evidence uses an Evidence Note plus URL rather than requiring stored quotes; quote and page/location requirements belong to later full-text review.
- Any detected MVP risk category is marked as requiring human/legal review.
- The system does not claim a book is legally safe without full-text review.
- If public data is insufficient to screen meaningfully, use `text check required`, not `no visible risk`.

### 9.7 Weekly report generation

The system must generate a weekly report sorted by candidate priority.

Report format for MVP:
- CSV

Acceptance criteria:
- Report contains all required MVP columns, including Priority label.
- Rows are sorted by independent signal count and priority label.
- Editor note/status fields are present and empty/editable.
- Every recommendation is traceable to links and evidence.
- Signal summary names source families in readable form; the editor-facing CSV does not need a separate Source family column.

### 9.8 Candidate history

The system should store historical candidates and prior run results, including candidates excluded from the editor-facing CSV.

Statuses:
- new
- seen before
- signal increased
- Russian status changed
- visible risk changed
- material change since editor decision
- accepted correction changed identity or status
- editor marked interested
- editor rejected

Acceptance criteria:
- Weekly report can separate new candidates from repeated candidates.
- Excluded candidates are remembered internally with identity, exclusion reason, evidence, and date so they are not repeatedly rediscovered. A candidate excluded as Found in Russian may re-enter the editor-facing CSV if new evidence changes its Russian-publication status to unclear or not found in Russian.
- Existing editor decisions are preserved.
- Raw human Editor note text is preserved exactly as entered; any later summaries or normalized tags must be stored separately.

---

## 10. Future functional requirements

### 10.1 Deep candidate memo

For shortlisted candidates, generate a memo including:

- annotation
- author credibility
- topic relevance in Russia
- target audience
- style/format notes, based on available samples/reviews
- competitor hypotheses
- USP hypothesis
- risks and unknowns
- recommendation: investigate / reject / needs manuscript / needs rights check

Important: competitor analysis must use real Russian marketplace results and must not invent competitors.

### 10.2 Full manuscript analysis

Later-stage full-text workflow must follow the strict sequence from requirements:

1. Read/check the text.
2. Check prohibited/risky content first.
3. Prepare structural summary following the table of contents.
4. Prepare business/market analysis.
5. Produce conclusion.

Full-text risk output must include:
- risk category
- quote
- page/location
- explanation
- possible handling: reject, legal review required, 18+ marking likely, rights-holder approval/consultation required, text check required, no issue found

### 10.3 Editorial-board memo

For candidate books with full text or enough source material, generate a structured editorial-board memo:

- annotation
- author
- relevance
- target audience
- style
- competitors
- USP
- risks
- conclusion
- recommendation
- bestseller/long-seller potential
- publication conditions: adaptation, format, supporting apparatus

### 10.4 Rights and deal intelligence

Future versions may track:
- Publishers Marketplace / Publishers Lunch entries, if user has access
- Publishers Weekly rights/deal items
- The Bookseller rights/news, if accessible
- fair catalogues
- multi-territory sales
- rights-holder / agent data

Paid/subscription rights sources are Skipped Sources for the MVP Pipeline and may be considered later through authorized access. Absence of rights/deal evidence in MVP does not mean no rights activity exists.

---

## 11. Data model draft

### 11.1 Source

- id
- name
- family
- url
- access_status
- extraction_method
- check_frequency
- active
- notes

### 11.2 Source run

- id
- source_id
- run_date
- status
- raw_candidates_found_count
- error_message
- raw_snapshot_reference, if allowed and copyright-safe

### 11.3 Candidate book

- id
- work_identity
- normalized_title
- display_title
- primary_authors
- additional_contributors, where useful
- publisher
- publication_date
- year
- category
- topic
- description
- isbn_10
- isbn_13
- language_or_market
- created_at
- updated_at

### 11.4 Signal

- id
- candidate_id
- source_id
- source_family
- source_url
- signal_type
- signal_strength
- signal_date
- evidence_note
- evidence_url
- notes

### 11.5 Russian-publication check

- id
- candidate_id
- check_date
- status
- matched_title
- matched_primary_authors
- matched_url
- matched_publisher
- evidence_note
- evidence_url
- uncertainty_note

### 11.6 Risk check

- id
- candidate_id
- check_date
- stage: scouting_public_data or full_text
- status
- category
- evidence_note
- evidence_url_or_location
- explanation
- recommended_handling
- confidence

### 11.7 Editor decision

- id
- candidate_id
- report_id
- status: interested, maybe_watch, reject, already_known, needs_deeper_analysis
- raw_note
- derived_note, if generated later
- updated_by
- updated_at

### 11.8 Report

- id
- report_date
- report_type
- generated_at
- editor_facing_candidate_count
- raw_candidates_considered_count
- excluded_candidates_count
- shortfall_reason, if fewer than 10 editor-facing rows
- file_path_or_url

---

## 12. Staged roadmap

### Stage 0 — Requirements and product framing

Goal: turn raw/editorial requirements into a coherent product plan.

Deliverables:
- consolidated requirements documents
- PRD
- MVP source shortlist
- first data model draft
- open questions list

Exit criteria:
- MVP scope is clear enough to implement.
- Non-goals and uncertainty rules are explicit.

### Stage 1 — Manual/semi-automated weekly report prototype

Goal: validate the report format and editorial value before building heavy automation. Stage 1 may use human collection to learn quickly, but it is not a manual-import feature for the MVP pipeline.

Implementation shape:
- use a small source list
- manually or semi-automatically collect candidates for this prototype only
- store rows in CSV
- Dima performs a report-level technical review first
- Dima imports the CSV into Google Sheets for the editor friend when needed
- editor feedback is returned through the edited sheet
- Dima exports the edited sheet back to CSV for ingestion/analysis
- use AI assistance for summarization, risk comments, and Russian-search query generation

Deliverables:
- first weekly CSV report with 10 editor-facing candidates after exclusions
- edited feedback CSV returned from the editor workflow
- editor feedback on columns, scoring, usefulness, false positives, and missing sources
- revised scoring labels

Exit criteria:
- editor can review the report without asking “why is this book here?”
- every candidate has source evidence
- at least several candidates are plausibly worth deeper review
- edited CSV can be consumed by the project without manual restructuring. Returned sheet values are source of truth for editor feedback fields only; edits to generated facts are treated as proposed corrections unless explicitly accepted

### Stage 2 — MVP pipeline

Goal: automate repeatable parts of the weekly report from easy/open sources while preserving human review. Manual import is out of scope unless added in a later stage.

Features:
- configured source list
- source fetch layer
- candidate extraction
- enrichment
- deduplication
- signal counting
- Russian-edition checks
- visible-risk screening
- candidate history
- CSV weekly report

Deliverables:
- runnable weekly pipeline
- local storage/database
- report generator
- logs and error handling
- documented source configuration

Exit criteria:
- one command or scheduled run produces the weekly report
- repeated candidates are tracked
- source failures are visible in a run summary/log
- output is good enough for regular editor testing

### Stage 3 — Editor feedback loop and review workspace

Goal: make AutoScout useful over multiple weeks, not just one report.

Features:
- persistent editor status and notes
- candidate detail pages or structured files
- status filters: new, watch, interested, rejected, already known
- change detection: new signal, Russian status changed, risk status changed
- export to Google Sheets or similar shared format

Deliverables:
- reviewable candidate history
- editor decision tracking
- improved reports based on past decisions

Exit criteria:
- editor feedback from previous weeks changes future reports
- rejected/already-known candidates stop wasting attention

### Stage 4 — Deep-dive candidate memos

Goal: support deeper evaluation for shortlisted books.

Features:
- candidate memo generator
- author/platform research
- Russian-market relevance analysis
- competitor discovery from Russian marketplaces
- USP hypothesis
- risk/unknowns section
- recommendation section

Deliverables:
- memo template
- memo generator
- evidence-linked competitor list

Exit criteria:
- editor can move from weekly shortlist to deeper evaluation without starting from scratch
- competitors are real and linked
- recommendations are specific, not generic

### Stage 5 — Full manuscript / editorial-board analysis

Goal: support later-stage manuscript evaluation when text is available.

Features:
- secure file upload/import
- full-text risk check with quote/page/location references
- structural summary by table of contents
- editorial-board analysis
- recommendation memo

Deliverables:
- full-text analysis workflow
- risk output with citations
- editorial-board memo output

Exit criteria:
- analysis follows the required sequence
- no conclusion is produced before risk and structural checks
- output is grounded in the uploaded text and cited sources

### Stage 6 — Production product

Goal: turn the validated workflow into a stable product.

Features:
- robust scheduling
- source health monitoring
- user roles
- permissions for risk/legal views
- dashboard or workspace
- integration with Sheets/Docs/Drive/Notion if useful
- backups and audit logs
- observability and cost tracking

Deliverables:
- deployed app or stable internal tool
- documentation
- operational runbook
- source maintenance workflow

Exit criteria:
- tool can be used weekly with low developer involvement
- failures are diagnosable
- editorial workflow is stable

---

## 13. MVP technical architecture draft

Recommended simple architecture:

1. Source config files
   - YAML/JSON list of sources, categories, and extraction methods.

2. Pipeline runner
   - scheduled or manually run command.

3. Collectors
   - one collector per source family where practical.
   - fragile, blocked, paid, or subscription-only sources are skipped or logged for MVP.

4. Candidate normalizer
   - title/primary-author cleanup.
   - duplicate matching.

5. Enrichment services
   - publisher/retailer metadata lookup.
   - Goodreads/Amazon/publisher link collection where accessible.

6. Russian-edition checker
   - search queries against selected Russian retailers/search engines.
   - Evidence Notes and URLs for Russian-edition status.

7. Risk screener
   - public-data only for MVP.
   - category-based output with uncertainty.

8. Storage
   - SQLite is sufficient for MVP.
   - CSV exports for reports.

9. Report generator
   - weekly CSV report.
   - optional Google Sheets export later.

10. Review state
   - editor status and notes stored separately from generated candidate facts. Returned sheet values are source of truth for editor feedback fields only; edits to generated facts are proposed corrections unless explicitly accepted.
   - Dima's technical review is report-level QA, not a candidate editor status.

---

## 14. Quality and safety requirements

1. Evidence-first output
   - Every candidate must have a source URL.
   - Every risk claim must have a reason or evidence pointer. Store URLs and concise generated notes rather than large copied source text.

2. No invented facts
   - Missing metadata stays missing.
   - Competitors must be linked to real marketplace pages.

3. Uncertainty is a first-class state
   - Use `unclear` and `text check required` when data is insufficient.

4. No paywall bypass
   - Paid/subscription sources are marked as such and skipped for MVP.
   - Manual authorized import can be considered in a later stage if the user has access.

5. Risk labels are not legal advice
   - Scouting-stage risk is based only on public materials.
   - Full legal review remains human/legal responsibility.
   - For MVP drug/microdosing screening, the September 2025 drug-content rule is treated as operationally relevant and should inform risk flags.

6. Copyright-sensitive data handling
   - MVP only handles public metadata, links, generated notes, and minimal factual snippets needed for traceability. It should not store large copied excerpts, article bodies, reviews, or book samples.
   - Copyrighted excerpt storage is deferred until future manuscript-analysis requirements are defined.
   - Full-text manuscript support, when added, must define storage, access, excerpt, and retention rules before implementation.

---

## 15. Success metrics

### 15.1 MVP usefulness metrics

- Weekly report contains 10 plausible editor-facing candidates after exclusions.
- 100% of candidates have source evidence links.
- 80%+ of rows have enough metadata for initial review.
- Russian status is completed or marked unclear for every candidate.
- Risk status is completed or marked text check required for every candidate.
- Editor marks at least a few candidates per month as `interested` or `needs deeper analysis`.

### 15.2 Quality metrics

- Low duplicate rate in weekly report.
- Low rate of obvious fiction/YA/non-target books.
- No unsupported claims of Russian non-publication.
- No risk output presented as final legal conclusion.
- Source failures are visible and explainable in the run summary/log.

### 15.3 Workflow metrics

- Editor review time per report decreases compared with manual source browsing.
- Rejected/already-known candidates do not repeatedly appear as new.
- Top candidates have clear “why this entered the list” explanations.

---

## 16. Key open questions

1. Resolved: exact source choice is not critical at this stage; use a pragmatic/random-but-reasonable first source set, with at least a couple of German sources included.
2. Resolved: MVP report format is CSV only for now; markdown and Google Sheets can wait.
3. Resolved: Dima is the first technical reviewer; the real editor friend reviews after Dima imports the CSV into Google Sheets. The editor edits the sheet and returns it; Dima exports/returns the edited CSV to AutoScout.
4. Resolved: first prototype should target 10 editor-facing candidates per weekly report after exclusions.
5. Resolved: visible risk flags should be shown directly in the main CSV report, including risk status/category/comment columns.
6. Resolved: MVP visible-risk detection covers categories 1-5 only: Russia/politics, war/anti-war/Russian Armed Forces, WWII/Great Patriotic War/USSR-vs-Nazi-Germany comparison, non-traditional relationships/gender transition/rejection of childbearing, and drugs/microdosing/psychoactive substances.
7. Resolved: any detected MVP risk category should require human/legal review; weak/ambiguous signals may be marked `text check required` but should not be treated as cleared.
8. Resolved: decide copyrighted excerpt storage later; MVP only handles public metadata, links, generated notes, and minimal factual snippets needed for traceability. It should not store large copied excerpts, article bodies, reviews, or book samples.
9. Resolved: automate only easy/open sources for MVP; fragile, blocked, paid, or subscription-only sources should be skipped or logged as inaccessible rather than handled through manual import.
10. Resolved: candidates already found in Russian should be excluded entirely from the final MVP candidate CSV, not shown as low-priority rows.
11. Resolved: MVP Russian-edition checks use retailers only: Ozon, Labirint, LitRes, Chitai-gorod, and Book24. Russian publisher-site checks are deferred.
12. Resolved: yes, the September 2025 drug-content rule is operationally relevant now and should inform MVP drug/microdosing risk flags.
13. Resolved: final handoff format for MVP is the detailed CSV/table only; candidate memos come later only for books the editor shortlists as interesting.

---

## 17. Recommended immediate next step

Build Stage 1 before engineering the full MVP:

1. Select 10-15 MVP sources.
2. Produce one manual/semi-automated weekly report with 10 editor-facing candidates after exclusions.
3. Ask the editor to mark each row:
   - interested
   - maybe
   - reject
   - already known
   - needs deeper analysis
4. Use that feedback to finalize MVP scoring, columns, source priority, and automation needs.

This avoids overbuilding a dashboard or scraper before proving which signals actually help the editor make decisions.
