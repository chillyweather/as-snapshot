# Editorial-fit scoring

Part of the [operator workflow](../current-weekly-operator-workflow.md).

## Editorial-Fit Shadow Scoring (#725)

The weekly ordering carries almost no editorial signal, so each weekly run also records an advisory editorial-fit score for every editor-facing candidate.
The score comes from TypeSafe Jev and the `fit-v2` question set: a five-level fit Score, an audience Choice, and an author-reach Choice.
`fit-v2` replaces the `fit-v1` rubric of the 2026-09-25 backtest, which rewarded serious books for educated readers while the editor favours popular, broadly appealing nonfiction.
Code combines the three answers into one composite, fixed before any `fit-v2` result existed: `fit.expected + 0.15 * P(mass_market) + 0.10 * P(author widely known)`.
There is no topic-saturation question, because the model input holds no evidence about the Russian market and Jev cannot look anything up.

### What shadow mode guarantees

- `priority_label`, `publisher_fit`, review ordering, Candidate History, and every other state file stay unchanged.
- The review does not show the score, so it cannot influence the decisions it is later compared with.
- The model input is a strict allowlist of public book facts: titles, authors, category, publisher, year, the English annotation, the English and Russian descriptions, signal families, and up to five source signals.
- Editor decisions, editor notes, priority, visibility, Visible Risk, and the Editorial Taste Profile never enter the model input, and the questions quote no editor text.
- The model is pinned to `jev-1.13.0`; a response that reports another model is rejected.
- Populating `publisher_fit` needs a separate explicit editor decision backed by the shadow report.
- The review shows the newest recorded score of a book in the «Оценка» column, and `editorial_fit_ordering: true` in the weekly config lists the books of one date by it (#1105, the operator's decision of 2026-11-15). The default is off; the live weekly config sets it since 2026-11-16.

### Weekly stage

The stage runs after descriptions are final and before the review is rendered, so every score is recorded before the editor reviews the book.
It runs only when the weekly config sets `editorial_fit_shadow: true` and a TypeSafe key is available in `TYPESAFE_API_KEY` or in `~/.config/autoscout/typesafe.env`.
Without a key the stage is skipped: no call is attempted and nothing is written.
No outcome of the stage can stop or change the weekly run; it is logged as `editorial-fit-shadow` in `workflow-log.json`.
Each call has a 10-second timeout and at most three attempts with backoff, the stage has a 180-second budget, and an authentication failure stops further calls.
A failed call is recorded as `unavailable` with a reason code and is counted in the report, never dropped.
A weekly set of about 160 candidates costs about $0.02.

After the candidates of the run, the stage scores the backlog: the books of Candidate History with no editor decision and no recorded score, hidden books left out, the newest first.
A run scores only its own candidates, so without this a book of an earlier run has no score and the review cannot order it.
The backlog has what the run left of the 180-second budget; a call that was not made leaves no record, and the book waits for the next night.
A failure of the backlog is logged under `backlog` in the stage's entry and leaves the scores of the run as they are.
A failed call is recorded as `unavailable` and the book is called again on the next night.
When the key was refused during the run, the backlog is skipped.

### Artifacts

The stage writes only `runs/current-weekly-pilot-russian/editorial-fit-shadow/`:

- `scores-<run-date>.json`: one record per candidate with the answer distributions, the composite, the review position, and the priority label at scoring time.
- `backlog-scores-<run-date>.json`: the scores of the backlog books called that day. The review and `./scripts/work-age-report` read them; the report and `./scripts/ranking-benchmark` do not, because a backlog score was not made beside a run and has no review position to compare with.
- `report.json` and `report.md`: the cumulative comparison with the editor's decisions.

The files are persisted with the other run artifacts.

### Fit score in the review (#1105)

`render-review` reads the newest scored record of each book from the `scores-*.json` and `backlog-scores-*.json` files, by identity key or durable Candidate ID, and calls no model.
A book with a score shows it with one decimal in the «Оценка» column of the Markdown and the HTML review, and the HTML column sorts; a review with no recorded score has no such column.
With `editorial_fit_ordering: true` the books of one date are listed by the score, highest first, and the books with no score follow in the order they have without it; priority-D books come after all the others in the same order, because the score knows nothing of their content risk; the review header says so.
No book is hidden or moved to another date, and no priority label or state file changes.
The mark is set by the scoring stage of a run, so a review rendered on its own between turning the value on and the next run is not marked; the scan runs every night, so that is at most one day.
The score artifact of such a run carries `review_ordered_by_fit: true`, a rescore of the same day keeps it, its `canonical_rank` keeps recording the order without the score, and the shadow report marks the run as after activation.
Setting the value to `false` restores the earlier order on the next render.

With the same value on, the «Без решения» tab of the HTML review is one continuous list with no date sections (#1106): the highest score first, then the books with no score, the newest date first, and the priority-D books last in the same order; books waiting in an archive block are part of the same list.
The cards are written once, under their dates; a script moves the undecided ones into one table when the tab opens and back when «Все» opens, so the page does not grow.
Without the script, or with the value off, the tab is the timeline filtered by date, as before.
The other tabs are unchanged.

### Work-age report (#1141)

```
./scripts/work-age-report
```

It measures, on the books the editor has decided, whether moving old, often-published works down would put fewer rejected and more wanted books in the top of the review.
It reads Candidate History, the newest recorded `fit-v2` score of each book (run and backlog artifacts: a fit score never sees the editor's decision), and the `work_age` block of #1138.
The age of the work is the year AutoScout first listed the book minus the year the work was first published; today's date is not used, so the same state gives the same report.
The report has four parts: how many decided books have a known work age, by the language of the list that first found them; the share of wanted books by age and by edition count; a simulation in which the books at or above an age and an edition-count threshold are moved below the others, counting the rejected books that leave the top 40% and the wanted books lost from it or entering it, for 25 thresholds side by side; and the books the chosen threshold gets wrong, by title.
The threshold is chosen on a development half of the decided books and reported on a held-out half; the halves are fixed by a digest of the Candidate ID.
It says how many books have no work age or no fit score and so are outside the simulation.
The command is offline and calls no model; it changes no state and writes only `data/work-age-report/work-age-report.md`, which is Git-ignored so it cannot block the next weekly run.
Editor notes are not read into the report, and nothing it computes reaches a candidate record, a score file, or the fit score input.

### Shadow report

```
./scripts/editorial-fit-shadow report
```

The command is offline and read-only: it prints the report and writes nothing, so it can run at any time without leaving uncommitted files that would block the next weekly run.
The weekly stage refreshes the stored `report.json` and `report.md`; `report --write` does the same on demand.
It reports AUC with a bootstrap interval, recall and precision at 20% and 40%, positive rate by audience and by fit level, cost, and latency, per week and cumulatively, against the current priority label and the current review order.
Positive is `interested`, `maybe_watch`, or `needs_deeper_analysis`; negative is `reject`; `already_known` is excluded.
A book counts once, on its first shadow score, and only when that score was recorded before the editor decided on it.
A failed call ranks below every scored book.
The report marks the evidence `insufficient` until three weekly runs have scored and decided books, then `ready_for_editor_decision`; it never activates anything itself.

`./scripts/editorial-fit-shadow preview` prints the exact request for one candidate and sends nothing.
`./scripts/editorial-fit-shadow score` scores the current candidates on demand; it is live, separately billed usage and refuses to run without a key.
It writes the scores and the report, which must then be committed before the next weekly run.

Changing a question's wording is a new question set: bump `QUESTION_SET_VERSION` and start a new shadow evaluation, because earlier weeks no longer test the same rubric.

---
