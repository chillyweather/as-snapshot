# AutoScout

AutoScout is a scouting and editorial-intelligence context for finding foreign adult nonfiction books that may fit a Russian publisher's acquisition workflow.

## Language

**Scouting Candidate**:
An externally signaled adult nonfiction book that may fit the publisher's acquisition workflow. Forthcoming adult nonfiction can qualify when it has an External Signal and enough metadata to identify the work. Academic-origin books can qualify when they have mass-market External Signal and general-reader accessibility. Practical professional or business books can qualify when they have broad adult nonfiction appeal; narrow manuals, textbooks, certification guides, and technical reference works do not. Memoir, journalism, narrative nonfiction, biography, public affairs, and cultural studies can qualify when treated as adult nonfiction; pure fiction, children's, YA, and topic-only discoveries do not qualify.
_Avoid_: book lead, title, find, topic-only candidate

**External Signal**:
Public evidence that a book has market, critical, reader, audio, rights, or media traction independent of AutoScout's own topic search. MVP signal families are award, bestseller/ranking, media/curated list, reader-interest, audiobook ranking for an underlying book/work, and rights/deal mention.
_Avoid_: topic match, interesting theme, AI recommendation

**Independent Signal**:
An External Signal that contributes to ranking once per source family for a given Scouting Candidate. Multiple evidence links from the same source family are preserved but do not each increase the independent signal count.
_Avoid_: raw link count, duplicate mention count

**Signal Strength**:
The qualitative weight of an External Signal, such as winner, shortlist, top ranking, sustained bestseller, major media list, minor mention, or rights/deal evidence. Signal Strength breaks ties and can move a priority label up or down without creating a fake-precise numeric score.
_Avoid_: opaque score, raw mention count

**Primary Signal**:
The strongest External Signal shown in the Source and Source URL fields for a Weekly Report Row. If signals are equally strong, the Primary Signal is the one that first caused the Scouting Candidate to enter the current run.
_Avoid_: arbitrary source, all sources column

**Found in Russian**:
Credible public evidence that a Russian-language edition of the same work exists or existed, with a matching retailer, publisher, or catalog URL. Imported foreign-language editions, ambiguous author/title matches, unavailable listings without clear Russian-language metadata, and same-topic books by the same primary author(s) are not Found in Russian.
_Avoid_: Russian search hit, imported edition, same-author match

**Russian-edition Verification**:
Operational evidence about whether configured Russian-edition providers were checked for one Scouting Candidate.
Russian-edition Verification is separate from the editorial Russian-edition status and does not replace an editor's decision.
The editorial status keeps the canonical values `found in Russian`, `not found in Russian`, and `unclear`.
_Avoid_: editorial status, unscoped provider result, legal availability conclusion

**Russian-edition Finding**:
What the Russian-edition completion pass learned about one Scouting Candidate (#1181): `confirmed` (the Russian State Library or LitRes shows a Russian-language edition of the same work), `unconfirmed` (only LiveLib located one), or `no_find`.
A `confirmed` or `unconfirmed` finding is shown as a mark in the review with its evidence link; the book stays in the review and is never excluded by it.
It is separate from the editorial Russian-edition status and never sets Found in Russian.
_Avoid_: Found in Russian, Russian-edition status, exclusion

**Check Coverage**:
The closed operational coverage result for Russian-edition Verification.
Canonical values are `complete`, `partial`, `not_run`, and `failed`.
_Avoid_: provider outcome, editorial status, coverage inferred from an empty fixture

**Provider Outcome**:
The closed operational result from a Russian-edition provider.
Canonical values are `matched`, `no_match`, `ambiguous`, `blocked`, `unimplemented`, and `failed`.
A null aggregate value means no single closed outcome exists because the required provider checks did not all complete.
_Avoid_: Russian-edition status, not found without candidate-scoped evidence, execution status

**Provider Execution Status**:
The operational state of one configured Russian-edition provider check.
Canonical values are `checked`, `not_run`, `skipped`, `blocked`, `failed`, and `unimplemented`.
A provider execution status is separate from its Provider Outcome; `not_run` and `skipped` providers have no Provider Outcome.
_Avoid_: Provider Outcome, editorial status

**Verification Confidence**:
The bounded confidence of a candidate-scoped Russian-edition Verification record.
Canonical values are `high`, `medium`, and `low`; this confidence describes operational evidence and is not an editorial Russian-edition status.
_Avoid_: editorial certainty, legal certainty, provider availability

**Visible Risk**:
A public-data signal that a Scouting Candidate may contain one of the configured screening categories and therefore needs human, editorial, or legal review before acquisition. Visible Risk is not a final legal conclusion and does not mean the full text has been checked. No visible risk means no configured MVP risk signal was found in available public materials, not that the book is safe or fully checked. The text check required status must explain whether public data hints at a category, public data is insufficient, or full text is needed for a specific reason. Risk comments distinguish book-content signals from author/context signals. Clear visible risk requires credible public evidence with a link and explanation; weak, indirect, or unverified signals use text check required.
_Avoid_: legal verdict, safe, prohibited book

**Visible Risk Screen Coverage**:
The closed operational record of whether the Visible Risk screen ran for one Scouting Candidate and which public metadata it read.
Canonical values are `screened`, `partial`, `not_run`, and `failed`; `screened` means every configured category was checked against the title and a description, and `partial` means no description was available, so Evidence Notes alone never conclude.
Coverage is computed by the screen, never inherited from a collector field or inferred from an absent one, and it is separate from the editorial Visible Risk status.
_Avoid_: Visible Risk status, legal clearance, coverage inferred from an empty status

**Weekly Report Row**:
One editor-facing CSV row for one Scouting Candidate. The row may summarize multiple External Signals, but duplicate source mentions belong in internal evidence or run logs rather than separate editor-facing rows. Its Source and Source URL fields show the Primary Signal.
_Avoid_: source mention row, signal row, duplicate candidate row

**Editor-Facing Exclusion**:
A Scouting Candidate omitted from the MVP weekly CSV because it is Found in Russian, outside the target category, a duplicate, or lacks an External Signal. Clear Visible Risk deprioritizes a candidate but does not by itself create an Editor-Facing Exclusion.
_Avoid_: hidden risk candidate, legal auto-reject

**Candidate History**:
Cross-run memory of every Scouting Candidate the editor has seen, including identity, editor status, editor note, and last-seen date. Candidate History is what lets a weekly run honor Reject and Already Known, detect Material Change, and present updates instead of rediscoveries. It persists between runs and is distinct from any single run's output artifacts. Each record also keeps every confirmed editor decision with its date in `editor_decisions` (#1053).
_Avoid_: single-run log, config snippet, editor-facing column

**State Store**:
The single interface through which the canonical cross-week collections, Candidate History and the dated decision log among them, are read and written (ADR 0023). It has a JSON and a SQLite backend; the database file is never committed, and Git keeps a deterministic JSON export of it, with Candidate History as one file per candidate. The run server is its only writer. Run artifacts, pre-collection results, the per-source seen ledgers, source row history, runs and metrics, and the editorial memory index are outside it.
_Avoid_: database, state files, cache

**Artifact Freshness**:
Artifact Freshness is the closed state of an extraction artifact relative to the current operation: Fresh, Reused, or Unbound.
A future validated cache hit must use a distinct state rather than Reused, and fault surfaces must use the shared fault predicate rather than infer a fault from missing freshness.
_Avoid_: cache hit, stale artifact, file exists

**Fresh**:
Fresh means an extraction artifact is bound to the current operation and its captured evidence.
_Avoid_: current-looking file, validated cache hit

**Reused**:
Reused means an extraction artifact is bound to an earlier operation or cannot prove the current binding, so it is a stale-artifact fault that must remain visible to the operator.
_Avoid_: validated cache hit, collected this week

**Unbound**:
Unbound means an extraction artifact has no operation identity against which freshness can be checked, so AutoScout does not claim that it is fresh or stale.
_Avoid_: fresh by default, stale by default

**Source Run Status**:
Source Run Status is the separate closed domain for the operational result of one configured source in a run.
Its values are inactive, skipped, ignored, failed, collected, reused, precollected, codex-captured, and not-due.
Source Run Status is not Artifact Freshness, even when the collection boundary converts artifact freshness into a serialized source status.
_Avoid_: artifact state, source freshness, open-ended source label

**Not Due**:
A source is not due when the weekly run's date falls outside its collection schedule: an annual or monthly source is collected only on the collection weekday, Sunday by default.
A not-due source is neither captured nor extracted; its books are carried forward unchanged from its last collected list, kept in the state directory.
Not due is neither a failure, a skip, nor stale evidence, and it is never named to the editor as a source problem.
_Avoid_: skipped source, stale source, failed source

**Source-Run Stale Predicate**:
The Source-Run Stale Predicate classifies operator-facing stale-evidence faults from normalized Source Run Status values.
Operator fault surfaces use this predicate instead of importing Artifact Freshness or inferring faults from aggregate counts.
A future cache-hit source state requires explicit non-fault handling and explicit conversion at the collection boundary.
_Avoid_: stale artifact predicate, missing aggregate count, cache-hit inference

**Run Identity**:
The pair of operation identity and evidence digest that proves which captured inputs a pipeline result consumed.
Run Identity prevents a same-date invocation from treating an earlier invocation's report as its own.
_Avoid_: calendar date alone, report timestamp

**Evidence Digest**:
A deterministic fingerprint of the effective run settings and the bytes of the file-backed evidence that the pipeline will consume.
Evidence Digest is recomputed at run time and is compared with the stored report before the feedback-only path is used.
_Avoid_: declared historical digest, source count alone

**Pipeline Path**:
The explicit outcome of a pipeline invocation: a scouting pass, a scouting pass that also had pending feedback, applied feedback, or a no-op.
Pipeline Path must remain visible to workflow orchestration so a no-op cannot be published as a completed scouting result.
_Avoid_: bare success code, missing result

**Excluded Candidate History**:
Internal memory of Scouting Candidates omitted from the editor-facing CSV, including identity, exclusion reason, evidence, and date. Excluded Candidate History prevents Found in Russian, duplicate, non-target, or no-signal candidates from being repeatedly rediscovered as new.
_Avoid_: editor-facing row, silent deletion

**Duplicate Candidate**:
A Scouting Candidate that represents the same work-level identity by the same primary author(s) as another candidate, even when source metadata varies by subtitle, edition, retailer page, or publication market. Revised or materially different editions with substantial new content, workbooks, companion volumes, series entries, and different books with similar titles are not Duplicate Candidates unless evidence is strong.
_Avoid_: similar topic, similar title, same author only

**Already Known**:
An editor status meaning the Scouting Candidate is already known to the publisher or editorial workflow and should not be presented as a new discovery again. Already Known does not imply rejection; the editor note should explain whether it is tracked, previously evaluated, in rights discussion, or irrelevant.
_Avoid_: rejected, duplicate, already published in Russian

**Reject**:
An editor status meaning the editor does not want the Scouting Candidate shown again as a normal candidate. Reject should preserve an editor note or reason when possible and may reappear only after a material change or explicit reset.
_Avoid_: Found in Russian, duplicate, legal verdict

**Interested**:
An editor status meaning the Scouting Candidate shows acquisition or editorial promise and should be preserved prominently in review history. Interested does not automatically request a candidate memo or deeper research package.
_Avoid_: memo request, final acquisition decision

**Maybe / Watch**:
An editor status meaning the editor is not ready to request deeper analysis but wants AutoScout to keep tracking the Scouting Candidate for new signals or status changes.
_Avoid_: shortlist, memo request

**Needs Deeper Analysis**:
An editor status meaning the editor wants a candidate memo or deeper research package for the Scouting Candidate when that workflow exists.
_Avoid_: watch, passive tracking

**Publisher Fit**:
How well a Scouting Candidate appears to match the publisher's adult nonfiction interests after hard exclusions. Publisher Fit affects priority and fit notes; it is not a strict MVP gate for borderline adult nonfiction.
_Avoid_: hard category gate, perfect-fit requirement

**Source Proposal**:
A source suggested for collection by the editor, the agent, or Dima, with a reason and provenance for the suggestion. A Source Proposal is not collected from until Dima explicitly accepts it into configuration; the agent may propose sources it encounters but never collects from one on its own. Rejected or infeasible proposals are remembered with a reason so they are not re-proposed as new; an accepted but uncollectable proposal becomes a Skipped Source.
_Avoid_: configured source, auto-added source, silent source expansion

**Skipped Source**:
A valuable or configured source that AutoScout does not collect from in the MVP because it is paid, blocked, fragile, unavailable, or otherwise unsuitable for easy/open automation. Skipped Sources may remain in configuration or logs with an explicit reason so they are not rediscovered as accidental omissions.
_Avoid_: failed source, ignored source, manual import source

**Stage 1 Prototype**:
The first editor-validation report, produced with manual or semi-automated collection to learn whether the CSV format and candidate selection are useful. The Stage 1 Prototype is not the repeatable MVP Pipeline.
_Avoid_: MVP pipeline, production import flow

**MVP Pipeline**:
The first repeatable automated AutoScout system for weekly reports, using easy/open sources only and logging Skipped Sources rather than manually importing them.
_Avoid_: Stage 1 prototype, manual-import workflow

**Material Change**:
A change significant enough to resurface a suppressed or previously seen Scouting Candidate, such as a new Independent Signal family, meaningful Signal Strength improvement, Russian-publication status change, Visible Risk status change, editor reset or tracking request, accepted editor correction that changes identity or status, or metadata correction that changes identity or exclusion status. Raw duplicate links, minor enrichment, or another mention from the same source family are not Material Changes by themselves.
_Avoid_: minor metadata update, duplicate link, same-family mention

**Evidence Note**:
A concise generated or factual note that explains why an External Signal, Russian-publication status, or Visible Risk status was recorded. Evidence Notes should point to source URLs and avoid storing large copied excerpts, article bodies, reviews, or book samples.
_Avoid_: copied article text, full review, book sample storage

**Content Description**:
An original Russian scouting summary of what a Scouting Candidate is about, used in the editor-facing Description column. It should help the editor understand the book's subject, angle, and editorial hook regardless of the source language. Content Description is not copied publisher copy, jacket copy, review text, article body, or book sample text, and it does not overwrite original title, author, or Non-English Candidate helper metadata.
_Avoid_: English description, copied blurb, source-language description, confirmed title translation

**Autobiographical Subject Context**:
An evidence-backed Russian phrase naming the public role or notability of the person at the center of a memoir, autobiography, or first-person personal book. It is shown to editors under the Russian label "Контекст героя". The structured value records the exact inflected Russian opening phrase, the exact supporting source-language phrase, and its Evidence URL. If it is missing, unsupported, or absent from the opening, the review shows a specific verification warning instead of inferring the person's significance.
_Avoid_: role inferred from outside knowledge, free-form occupation matching, unsupported fame claim

**Recorded Source Mention**:
A Source Mention captured from a real configured source page as structured metadata for deterministic trial runs. It preserves source provenance, source URL, check date, title, author, signal family, Signal Strength, and Evidence Note without live fetching or copied source bodies. Recorded Source Mentions are real-world input examples, not synthetic fixture candidates and not manual-import workflow.
_Avoid_: live scrape, source fixture, copied page content, manual import

**Agent-Extracted Mention**:
A Source Mention produced by live-fetching a configured source page and extracting structured mention metadata with an LLM, validated against the same schema as a Recorded Source Mention. It must carry a real evidence URL from the fetched page; an extraction without verifiable provenance is dropped and logged, never invented. Agent-Extracted Mentions are the default collection mode; Recorded Source Mentions remain for deterministic trial runs, and bespoke parsers are a per-source optimization.
_Avoid_: hallucinated mention, recorded mention, scraped row without provenance

**Non-English Candidate**:
A Scouting Candidate whose original publication or source metadata is not in English. Non-English Candidates can enter the MVP when configured easy/open sources provide enough metadata to identify the work, preserve evidence, and perform Russian-edition checking without inventing translations. English title, English author display, and English annotation fields are editor-facing helper metadata only; they do not overwrite original title/author metadata and are not confirmed edition metadata.
_Avoid_: invented English title, invented Russian title

**Search Translation**:
A generated title translation used only as a Russian-edition search note or query attempt. A Search Translation is not candidate metadata and must not be presented as the book's Russian title unless a real Russian edition is found.
_Avoid_: display title, Russian edition title

**Author Transliteration Note**:
A generated author-name transliteration used only as a Russian-edition search note or query attempt. It is not authoritative author metadata unless confirmed by a real Russian-edition match.
_Avoid_: display author, confirmed Russian author name

**Editorial Taste Profile**:
A human-readable state file of the editor's standing rules and preferences, applied deterministically on every run. Every entry preserves the editor's verbatim words, its creation date, and a revocable status; only the editor's explicit confirmation creates or changes an entry. The profile stores Hard Rules and Soft Preferences; derived statistics live outside it and only ever produce suggestions.
_Avoid_: learned weights, ML model, hidden score adjustment, agent-invented rule

**Hard Rule**:
An Editorial Taste Profile entry meaning matching Scouting Candidates are hidden from the editor-facing list. Hidden candidates are counted in every review summary, recorded in a run artifact naming the responsible rule, still enter Candidate History, and can be resurfaced by revoking the rule. A Hard Rule suppresses display; it never deletes.
The rule covers the whole cumulative review, not only the candidates of one run: every render judges each Candidate History card against the active rules, whatever the editor decided about the single book, and lists the cards it withheld in `hidden-from-review.json` (#1025).
_Avoid_: permanent deletion, silent filter, Editor-Facing Exclusion, Reject

**Withdrawn Source**:
A retired configured source marked `retired_cards_hidden: true`, at the editor's request. A card whose every source is withdrawn leaves the cumulative review and returns when an active source reports the same book or the source is made active again; a card with at least one other source stays and keeps the withdrawn source in its evidence. A source that reported the book in an earlier run counts too: Candidate History keeps only the last run's evidence, so the names that drop out of it are kept in `earlier_source_names`. A card the editor marked interested, maybe watch or needs deeper analysis stays. Retirement without the mark only stops collection. Nothing is deleted from Candidate History, and `hidden-from-review.json` names the withdrawn sources of every card it withheld (#1025).
_Avoid_: deleting retired cards, hiding by lifecycle status alone, Hard Rule on a source

**Soft Preference**:
An Editorial Taste Profile entry that nudges a matching Scouting Candidate's priority label by exactly one step, up or down, with the reason shown on the editor-facing row. A Soft Preference never hides a candidate and never overrides hard floors such as clear visible risk forcing priority D.
_Avoid_: hidden reweighting, multi-step boost, exclusion rule

**Unique Contribution Rate**:
The share of a configured source's mentions, over a trailing window, that no other configured source surfaced. Together with corroboration rate and editor yield it measures a source's value: low unique contribution does not make a source worthless (corroboration still raises signal counts), but marks it as a first candidate for retirement.
_Avoid_: raw mention volume, source quality score, automatic removal trigger

**Source Capture**:
A file-backed copy of one source interaction, such as a live response or an extraction response, written by the shared bounded capture writer under the configured capture bound. The live response capture is evidence and its bound failure fails the source closed; the extraction response capture is optional observability and its failure only records a diagnostic. Capture diagnostics never contain filesystem paths.
_Avoid_: raw log dump, unbounded page save, silent capture failure

**Probation Source**:
A newly accepted configured source that is collected normally but judged on its contribution metrics after a set number of weeks before being considered established. Probation is recorded in configuration as `lifecycle_status: probation`, with `probation_started_at` and `probation_evidence_window_weeks` when the trial is dated; the keep/retire decision is made in a source review conversation, not automatically. The HTML review names a Probation Source in violet, so the editor can judge the source by its books; the colour stays until that decision.
_Avoid_: auto-added source, trial mention discount, second-class signal

**Work Age**:
How old the work behind a Scouting Candidate is: the year the work was first published anywhere and how many editions a catalogue knows, with the status `found`, `not_found` or `not_checked`. A translation with a confirmed original edition takes the original's age. A doubtful catalogue match leaves the Work Age unknown, and `not_found` (the catalogue has no agreeing work, usual for a new book) differs from `not_checked`.
_Avoid_: edition year, publication year of the listed edition, guessed first year

## Example Dialogue

**Developer**: Does this celebrity memoir count as a Scouting Candidate?

**Domain Expert**: Only if it is treated as adult nonfiction and has an external signal, such as an award, bestseller placement, authoritative media mention, reader-interest signal, or rights/deal mention.

**Developer**: Can a forthcoming book be a Scouting Candidate?

**Domain Expert**: Yes, if it has an External Signal and enough metadata to identify the work; mark the publication date as expected or forthcoming.

**Developer**: Can a forthcoming original book be marked not found in Russian?

**Domain Expert**: Yes, after configured retailer searches find no Russian edition or preorder, with an uncertainty note that the original is forthcoming.

**Developer**: What about a useful business book found by searching for "AI leadership"?

**Domain Expert**: Not by itself. It becomes a Scouting Candidate only when an external signal explains why it should enter the pipeline.

**Developer**: Can a trend report make a book eligible?

**Domain Expert**: Not alone. The specific book still needs an External Signal of its own.

**Developer**: Do three media mentions count as three Independent Signals?

**Domain Expert**: No. They are three evidence links but one media/curated-list Independent Signal.

**Developer**: If two candidates each have two Independent Signals, are they equal?

**Domain Expert**: Not necessarily. Signal Strength determines which evidence is stronger and explains the priority label.

**Developer**: Which source appears in the Source column for a candidate with several signals?

**Domain Expert**: Show the Primary Signal there, and put the remaining evidence in signal summary and links.

**Developer**: Ozon has an English paperback listing for the book. Is it Found in Russian?

**Domain Expert**: No. It is only Found in Russian when the evidence points to a Russian-language edition of the same work.

**Developer**: If retailer checks are unclear, should the MVP Pipeline check Russian publisher sites?

**Domain Expert**: No. Keep the status unclear and note that publisher-site checks are deferred.

**Developer**: Can the weekly report say a book is legally safe?

**Domain Expert**: No. Scouting only reports Visible Risk from public data; legal safety requires a separate review.

**Developer**: If one candidate appears on an award list and two media lists, how many rows go in the weekly CSV?

**Domain Expert**: One Weekly Report Row. The extra sources are preserved in signal summary and evidence links.

**Developer**: Should a candidate with Clear Visible Risk disappear from the weekly report?

**Domain Expert**: No. It should be clearly marked and deprioritized, but not hidden unless another exclusion reason applies.

**Developer**: Should Found in Russian candidates be forgotten after filtering?

**Domain Expert**: No. Keep Excluded Candidate History with the matching URL and exclusion date.

**Developer**: Can a previously Found in Russian candidate come back into the report?

**Domain Expert**: Yes, if new evidence changes the Russian-publication status to unclear or not found in Russian; mark it as Russian status changed.

**Developer**: Should a workbook with the same title be merged into the main book?

**Domain Expert**: No, not unless evidence shows it is the same work rather than a companion or derivative volume.

**Developer**: Does Already Known mean rejected?

**Domain Expert**: No. It means the candidate is not new to the editorial workflow; the note carries the actual business meaning.

**Developer**: Can AutoScout clean up or summarize the Editor note in place?

**Domain Expert**: No. Preserve the raw human note exactly; derived summaries belong in separate fields.

**Developer**: If the editor changes a title in Google Sheets, does that overwrite candidate facts?

**Domain Expert**: Not silently. Treat feedback fields as authoritative, but treat generated-fact edits as proposed corrections unless accepted.

**Developer**: Should Dima's technical review be stored as Editor status?

**Domain Expert**: No. It is report-level QA; Editor status is reserved for domain/editor feedback.

**Developer**: Does Reject delete the candidate forever?

**Domain Expert**: No. It suppresses normal future display, but a material change or explicit reset can bring it back.

**Developer**: Does Interested create a memo task?

**Domain Expert**: No. It preserves the candidate as promising; Needs Deeper Analysis creates the memo or research task.

**Developer**: Should Maybe / Watch create a memo task?

**Domain Expert**: No. Only Needs Deeper Analysis promotes the candidate into the memo or research queue.

**Developer**: Should a borderline memoir with strong signals be excluded for weak Publisher Fit?

**Domain Expert**: No. Keep it with a lower priority or fit note unless it is clearly outside the target category.

**Developer**: Should a paid source disappear from configuration?

**Domain Expert**: No. Keep it as a Skipped Source with a reason, but do not collect from it in the MVP.

**Developer**: Should failed sources appear in the editor CSV?

**Domain Expert**: No. Put skipped and failed sources in the run summary for Dima to review before sharing the CSV.

**Developer**: If paid rights databases are skipped, does no rights signal mean no rights activity?

**Domain Expert**: No. It only means no easy/open rights signal was found by the MVP Pipeline.

**Developer**: Can the Stage 1 Prototype use manual collection?

**Domain Expert**: Yes, but that does not make manual import part of the MVP Pipeline.

**Developer**: Should another media link resurface a rejected candidate?

**Domain Expert**: Not by itself. It needs a Material Change, such as a new Independent Signal family or stronger signal.

**Developer**: Should we store the full review text as evidence?

**Domain Expert**: No. Store the URL and an Evidence Note; avoid large copied source text in the MVP.

**Developer**: Should AutoScout invent an English title for a German candidate?

**Domain Expert**: No. Preserve the original title or mark translation fields unknown unless there is source evidence.

**Developer**: Can a generated Russian title be stored as the candidate's title?

**Domain Expert**: No. Store it only as a Search Translation note unless a real Russian edition confirms it.

**Developer**: Should generated author transliteration appear as display metadata?

**Domain Expert**: No. Keep it as an Author Transliteration Note unless a Russian-edition match confirms it.

**Developer**: Does MVP Visible Risk need a quote and page number?

**Domain Expert**: No. Scouting uses an Evidence Note plus URL; quote and page/location belong to full-text review.

**Developer**: The editor said "enough of this author" — is that a Reject?

**Domain Expert**: No. Reject is per-book. That is a Hard Rule in the Editorial Taste Profile; the agent should confirm the scope and echo back what it recorded.

**Developer**: A Hard Rule hid three books this week — do they just disappear?

**Domain Expert**: No. The review summary reports the hidden count, the run artifact names the rule, and revoking the rule brings them back.

**Developer**: Nine of ten rejected books were business leadership — should the system stop showing that topic?

**Domain Expert**: Not on its own. Show it as a suggestion; only the editor's confirmation turns it into a rule.

**Developer**: A source has near-zero Unique Contribution Rate — remove it from configuration?

**Domain Expert**: Not automatically. Its corroboration may still raise signal counts; propose retirement in a source review and let the editor decide, keeping it as `active: false` with a reason.

### Row Evidence Page

Any page from the Independent Signal's own owner, whether reached through the row link or through search, can ground only a single-source description.

A book-level page linked by a candidate row's evidence URL. It is fetched first
and counts as one description source. A page owned by the Independent Signal's
own owner may ground a single-source description, but never supplies an
independent second source. Shared list pages, site roots, data feeds and other
list-level endpoints are skipped (Issue #760).
