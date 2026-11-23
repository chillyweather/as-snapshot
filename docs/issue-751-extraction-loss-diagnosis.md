# Issue #751: what still loses sources in Codex extraction

Recorded 2026-10-04 from replays of the frozen benchmark in `data/model-qualification/benchmark` on the pinned model (`gpt-5.6-luna`, medium).
No weekly run was started for this diagnosis, and no source page was fetched.

## Method

The qualification report of 2026-09-27 keeps counts and discards the model's answers, so it cannot say why a run was empty.
The replays here ran the same production agent, prompt, and validators on the same captures and kept every answer and its Codex session.
34 runs were made: 6 before the change in this PR and 28 with it.

## Finding 1: an empty answer is an unfinished answer, not a reading of the page

Two of the first six runs returned `entries: []` (TodosTusLibros, BookPage).
In both sessions the extractor's own tool calls had already printed the rows: 20 `article.book-col` rows under `#no_ficcion` for TodosTusLibros, and the review links with their titles for BookPage.
Neither answer carried the `empty_reason` the contract requires since #740.
Asked in the same session which instruction made it return nothing, the extractor answered that none did:

> No instruction in the work item justified returning no entries; the capture contained qualifying rows. […] What stopped me was my own execution error: I output an empty array instead of completing the parsed rows.

The second replay of each capture returned the full list.
So these are not page or configuration faults, and the reason-less empty answer is a signal code can read.

### Fix in this PR

When an answer has no entries and no `empty_reason`, the extraction agent resumes the same Codex session once and asks it to finish (`docs/weekly-runtime-budget.md`).
The follow-up stays inside the attempt's own time bound, so retry admission and the deadline are unchanged, and a follow-up that does not improve the answer leaves the attempt as it was.

| Replay with the follow-up | Runs | Reason-less empty | Recovered by follow-up | Valid, non-empty |
|---|---:|---:|---:|---:|
| TodosTusLibros | 10 | 3 | 3 | 10 |
| BookPage Nonfiction | 10 | 0 | – | 10 |
| Andrew Carnegie Medals | 4 | 0 | – | 4 |
| USA TODAY Nonfiction | 4 | 0 | – | 3 |

The three recovered attempts took 80, 121, and 166 seconds in total and returned 19, 19, and 17 entries.
The one failed USA TODAY run was an answer that was not valid JSON, which the existing retry already covers.
Three recoveries are a small sample: the next qualification run and the next weekly run are the evidence that settles the rate.

## Class 1: "always empty"

### TodosTusLibros Librerías Recomiendan No Ficción: corrected, fixed

It is not always empty and the capture is not the wrong page.
The homepage capture holds the heading `Las Librerías Recomiendan` and a server-rendered `#no_ficcion` tab with 20 book rows, and the committed reference found 17 of them.
The benchmark's 0 of 3 and the 2026-09-26 production failure are Finding 1.
With the follow-up, 10 of 10 replays were valid; 9 held every reference book and one held 16 of 17.
The site-root note that the run record attaches to this source is an observation, as `source_contribution.py` already says, and it is not the cause.

### Académie française Prix de l'essai: diagnosed, scoped to its current edition

The page is a cumulative laureate list, not a current-edition page: `.academie-laureate` rows back to the 1970s, 55 named works.
The committed extractions of this source held 55, 55, 55, 55, 5, 55, 0, and 1 entries across the last eight weekly runs.
The frozen reference is the newest of those and holds only the 2026 laureate, so the benchmark asks for one row while the contract, as written, admits 55.
Both replays returned all 55, in 248 and 273 seconds.
One was refused whole as `unverifiable_provenance`, because it gave the page's relative laureate links (`/anne-marie-lecoq`) as evidence URLs.
The 2026-09-27 report shows two empty answers and one timeout at 268 seconds; an answer of 55 rows with a Russian description and helper fields each sits at the edge of what one attempt completes.

The follow-up covers an empty answer here too, but it does not make a 55-row answer cheap or correct.
The open question was a product rule: whether this source contributes only its current edition.
That rule was not declared anywhere, so the first PR left it for an explicit decision.

#### Decision and fix (2026-10-04)

The operator decided that the source contributes only its current edition.
The portfolio entry now declares it in `extraction_notes`, which the extraction prompt passes to the extractor as a binding constraint:

- Only the `.academie-laureate` rows of the greatest year on the page are extracted.
- Rows of that year sharing one title are one book, with every laureate as a primary author and no civility prefix.
- `source_url` and `evidence_url` are the configured page URL, because the relative laureate links are author pages.

No code changed: the scope is source configuration, the same way Deutscher Sachbuchpreis declares its current edition.
Nothing enforces the scope in code, so an answer that ignores it is still admitted as before.
Laureates of earlier years already in Candidate History are untouched.

21 replays of the frozen capture on the pinned model, all after the scope was declared:

| | Runs | Valid | Entries per answer | Duration |
|---|---:|---:|---:|---|
| `./scripts/qualify-model --source-name` | 3 | 3 | 1 | 16–33 s |
| Replays that kept the answer | 18 | 18 | 1 | 18–46 s |

Every kept answer held the 2026 title and the page URL as evidence.
Before the scope was declared, the same capture took 248 and 273 seconds for 55 rows.

One variation remains.
Two of the 18 kept answers named only the first of the two 2026 laureates, one of them in the 8 runs made with the final wording.
A third kept the page's `Mme` and `M.` prefixes, before the wording excluded them.
The benchmark scores a row by its title and first author, so one of the three qualification runs scored recall 0 for the right book.

## Class 5: BookPage Nonfiction "recall 0.61"

Two causes, one in the measurement and one in the page.

**Measurement.**
The reference is a committed extraction, and three of its eleven rows have a blank author because the listing shows none.
The report matched on `title|first author`, so an answer that named those authors scored three misses and three extras for books it had found.
The same exact-match rule scored USA TODAY at 0.00 recall with 9 extras on an answer holding its whole list, because one run wrote "Bildner, Jim" as printed and another "Jim Bildner".
This PR scores an author as its name tokens in any order, and a reference row without an author by its title.

**Page.**
The BookPage listing does not show authors.
An author is recoverable only where the review URL names one (`…/castle-jon-ronson-book-review/`).
`valiant-women`, `high-risk-homosexual`, and `happy-snacking-dont-die-black-forager-book-review` do not, and the contract requires a non-empty `primary_authors` from the capture alone.
The extractor then either drops the row or supplies an author the capture does not show.
Rescored with the rule above, seven BookPage replays found 0.64 to 0.91 of the reference; "Valiant Women" was missing in six of them.
So BookPage is a real partial list bounded by the rows whose author the page does not carry.
Admitting such a row needs the author-optional rule that ranking tables can already declare, which is a contract change for this source and is left for a decision.

## Class 2: intermittently empty from identical input

This is Finding 1.
Andrew Carnegie Medals and USA TODAY returned their lists in 7 of 8 replays, and no reason-less empty answer occurred in those eight.
The production empty-extraction retry (#700, #732) and the follow-up are independent: the follow-up repairs an attempt in place, and the retry still runs when it does not.

## Class 3: a complete list discarded for one envelope defect

Not changed here.
The report kept no rejected artifacts, so which identity field was miscopied in the `identity_mismatch` cases is unknown, and none recurred in 34 replays.
One `unverifiable_provenance` did recur (Académie française, above), and its cause was relative evidence URLs.
Whether a defective envelope field should discard an otherwise valid list is the decision #751 asks for; the tests that encode whole-artifact refusal are product rules (#658) and stay as they are.

Update (#823): the 2026-10-15 run kept its rejected artifacts.
All four National Book Awards 2025 and Carnegie 2026 refusals, and the first-attempt PEOPLE Books and Magnesia Litera ones, matched their work item in every field except `operation_id`.
The extractor had dropped or inserted characters while retyping the 64-character hex value (for example `d07f04353b0b…` became `d07f043b0b…`).
The host now writes the dispatched `operation_id` into the answer before validation, because it writes the artifact from that very response; `source_name`, `source_url` and `capture_sha256` are still copied by the extractor and checked.
An `identity_mismatch` now also records `identity_mismatches`, naming each wrong field and a bounded kind of difference, never the values.

## Class 4: timeouts

Not changed here.
Of the three, Académie française is explained by answer size above.

## What remains open on #751

- A qualification run of the pinned model on this code, to measure the follow-up and the corrected scoring across all 31 sources.
- The per-class findings against the first weekly run on this code.
- Two decisions: author-optional rows for BookPage, and whole-artifact discard for class 3.
- Whether the Académie française co-author variation shows up in weekly runs.
