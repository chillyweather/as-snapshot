# Kirkus pre-collection (Issue #816)

Kirkus is collected by its own pass, because the site asks for 20 seconds between requests.
`./scripts/run-weekly` starts that pass itself, once a day, alongside capture and extraction of the other sources, so a weekly run needs no separate Kirkus step.
The pass itself takes no more than 40 minutes and reports through the shared heartbeat as `Сбор Kirkus: запрошено страниц N.` next to the current stage's counts, at most once per 60 seconds overall.
After the main extraction, the run waits for any unfinished pass, reports that wait with its page count, then captures and extracts Kirkus.
During the overlap, `./scripts/doctor` reports both stages with their counts, elapsed time, and last snapshot time as `run-in-progress`, with no escalation question.
No outcome of the pass stops the run: a missing, stale or cut-short result is reported for Kirkus alone, by name.
So is a run whose pass failed and that reads the result of an earlier week.
When the operator or the editor asks to collect or refresh Kirkus outside a weekly run, use:

```
./scripts/collect-kirkus
```

One pass takes up to about half an hour and makes live requests, so in Codex the single tool-level sandbox-permission request is attached to the full `./scripts/collect-kirkus` invocation, with this Russian rationale:

«Для обновления данных Kirkus нужен сетевой доступ. Разрешить запуск?»

A weekly run on a day that already had a pass, whether it finished, was cut short or failed, makes no second pass.
Do not run it twice in a row after a `partial` or failed pass: the site refused a request, and a later pass continues where this one stopped.

On the run server a timer starts the pass every night at 01:30 Asia/Jerusalem, an hour before the scan, with `./scripts/collect-kirkus --scheduled` (#1057).
Every pass, whoever starts it (the timer, the operator or a weekly run), holds a pass lock beside its result in `data/kirkus-precollection/` for the whole pass, around checking the day, recording the attempt and collecting. The lock is an operating-system lock (`flock`) on that file, not the file itself: the kernel releases it when the pass ends or its process dies, so there is never a stale lock to remove, and the file stays in place.
So two passes never request the site together.
The scheduled pass makes no pass when another pass holds the lock, or when a pass already started that day, whether it finished, failed or is still running: the attempt record is written as a pass starts.
It then prints one Russian line and exits 0.
The run of 02:30 reads that pass's result and starts no pass of its own; when the pass is still running, the run does not wait and reads the newest finished result, naming Kirkus as for a failed pass.
A failed scheduled pass prints its stage and reason code to the journal, keeps its exit code and sends nothing: the morning report of the run that follows states the night's pass from its attempt record.
Without `--scheduled` the command makes a pass whenever it is asked, unless another pass is running: it then reports `kirkus_pass_in_progress`, exits 2 and leaves the result alone.
A weekly run that finds the lock held starts no pass and does not wait, as for a day that already had its pass.
It writes only its result file under the Git-ignored `data/kirkus-precollection/` and prints counts with no titles.
The procedure is in `docs/operator/pre-collection.md`.
