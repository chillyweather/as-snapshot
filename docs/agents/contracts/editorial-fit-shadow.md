# Editorial-fit shadow report (Issue #725)

Each weekly run records advisory editorial-fit scores from TypeSafe Jev (`fit-v2`) in shadow mode when `editorial_fit_shadow` is set in the weekly config and a TypeSafe key is available.
The stage is part of `./scripts/run-weekly`; it needs no separate step and can never stop or change the run.
Shadow scores change no priority label and no weekly or editorial state.
The same stage then scores the undecided books of earlier runs that have no score yet (`backlog-scores-<run-date>.json`); the review reads those scores, and the report and the ranking benchmark do not.
The review shows a book's newest recorded score in the «Оценка» column (#1105); a review with no recorded score has no such column.
With `editorial_fit_ordering: true` in the weekly config, the books of one date are listed by that score, highest first, the unscored books after them, and the priority-D books last in the same order, and the review says so; the default is off, and turning it off restores the earlier order on the next render.
Each score artifact of a run whose review was ordered this way carries `review_ordered_by_fit`, and the shadow report marks that run as after activation.
When the operator asks how the shadow scores compare with the editor's decisions, use:

```
./scripts/editorial-fit-shadow report
```

The report is offline and read-only: it prints the comparison and writes nothing.

When the operator asks whether an ordering is better than the current review order, use:

```
./scripts/ranking-benchmark
```

It is offline and read-only too.
It compares each ordering with the order the review showed, on the same books, and prints `pass` or `fail` on the held-out runs by the rule fixed in #1104.
The split date and the rule are constants; changing either starts a new evaluation.
When the operator asks whether the age of the work would improve the review order, use:

```
./scripts/work-age-report
```

It is offline, changes no state, and writes one Git-ignored report under `data/work-age-report/`.
`./scripts/editorial-fit-shadow score` is live, separately billed usage and runs only on explicit operator request.
This is not an editor workflow, and shadow scores are not mentioned in normal editor output.
Populating `publisher_fit` requires a separate explicit editor decision backed by the report.
The procedure and artifact contract are in `docs/operator/editorial-fit.md`.
