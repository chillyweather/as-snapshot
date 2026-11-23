---
status: accepted
---

# The scan runs every night, with the run lock as its only coordination

This extends ADR 0021, which moved the scheduled weekly run to a home server.

From 2026-11-10 that server ran the scan from a systemd user timer on Wednesday and Sunday at 02:30 Asia/Jerusalem.
Since #1057 the timer starts it every night at 02:30.
A second timer starts the Kirkus pass at 01:30, an hour earlier, so the run that follows reads a fresh result and does not spend up to 40 minutes of its own on the site's 20-second delays.
The names stay as they are: the command is still `run-weekly`, the review is still the weekly review.

The server is the only machine that runs the scan and pushes state.
A manual run is started on the server, the way the editor's agent always starts one.
Nothing tells the operator to pause, stop or resume a timer.
The run lock (`.auto-scout-workflow.lock`), which every run already takes, is the only coordination: a scheduled start that finds it held by a process that is verifiably alive skips itself, exits 0 and writes nothing, because the run in progress owns the log and the artifacts. A lock whose process is dead or unreadable is not a run in progress, so a night after a crash is reported, never skipped silently.
A manual run that meets a live lock is refused as before.
There is no "already ran today" rule for the scan: a manual run earlier that day does not cancel the night's run.
The Kirkus pass keeps its own once-a-day rule, which already existed for the weekly run: its attempt record is written as a pass starts, so the run of 02:30 neither starts a second pass nor waits for one still running.
Both services set `TZ=Asia/Jerusalem`. The server clock is UTC, 01:30 in Jerusalem is the previous UTC day in winter, and both the launcher's run date and the pass's date are the local date.

A scheduled run has no browser stage, because nobody sits at the server's screen; the editor reads the review through the review service on the tailnet.

## Failures must be heard, and must not block the next night

On 2026-11-11 the run stopped at the toolchain check after nine seconds (a Codex usage limit), printed no reason, and nobody heard about it until the review was opened.
It also left the tracked `workflow-log.json` modified, and the next run's repository check refuses a dirty working tree, so every later run would have been refused until somebody restored the file by hand.
On 2026-11-10 a rejected push of the log-only commit had the same effect.

So every scheduled run sends the operator one morning report when it ends, whatever happened, and a run that stops also prints its stage and reason to the journal.
This replaced, on 2026-11-14, the first rule of this ADR (a notice only for a stopped or attention-required run and for a failed Kirkus pass). The operator decided it: "I just want to get every morning a mail with a report on the nightly run. If it's successful, it should say successful. If it was somehow broken, it's broken. In any case, nobody is going to fix it in the middle of the night."
The subject carries the verdict and the date (успешно, требует внимания, остановлен, пропущен), so the inbox line is enough. The verdict follows the workflow outcome, with no rule of its own.
The body states, in a few lines, the counts of a completed run (new and updated books, Russian description coverage), the sources with problems and their category, the number of description problems, the failed stage and reason token of a stopped run, and the night's Kirkus pass, read from its attempt record.
The Kirkus pass sends nothing of its own; its failure is in the journal and in the next report.
The report never carries a book title, an author, a path, a credential or raw error output; reasons are tokens and numbers are counts from the result.
It goes by email from the AutoScout mailbox through the standard library's SMTP client, configured by environment variables in a 0600 file outside the repository, behind a transport seam so another channel can replace email later.
Without configuration the report is written to the journal instead.

And what such a run leaves behind no longer blocks.
That residue is exactly the run log with an unstaged change, alone or together with an unstaged change to the same directory's `run-summary.json` that differs from `HEAD` only in its top-level `toolchain_preflight` key: a Stage 0 failure records its probe there for the doctor, as the 2026-11-11 run did.
Under the run lock, the preflight copies every residue file to an ignored folder under one shared stamp (keeping the 30 newest sets), restores them only when every copy succeeded, and the new run's log records that it did.
A staged file, whose index content a restore would discard, a summary with any other change, and any other modified path still block, as any other dirty path does.
An unpushed earlier commit never blocked; the next successful push carries it.

## Consequences

- The units and the wrapper are in `deploy/server/`, beside the search adapter's; `docs/operator/run-server.md` carries the install steps.
- Seven runs a week instead of two spend more Codex and search quota; a run stopped by a usage limit is now in the next morning's report instead of passing unnoticed.
- A night run that collides with a long manual run is lost for that night, by design; the next night runs as usual.
- The doctor stays read-only and no longer reports that residue as a repository blocker, since the next run handles it; it uses the preflight's own predicate.
- The Kirkus pass takes a small lock of its own for the whole pass, so the timer's pass, a manual pass and a run's pass never request the site together.
