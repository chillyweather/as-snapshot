# The run server and the State Store

Part of the [operator workflow](../current-weekly-operator-workflow.md).

## Prerequisite: the grounded search adapter

The grounded description stage needs a search adapter, and the adapter holds the
search credential. On the run server it is a systemd user service beside the
pipeline, so there is nothing to start before a run and no key to retype
(ADR 0021, which supersedes ADR 0016 for the scheduled run).

The scheduled run carries the endpoint in its service unit. A manual run on the
server needs the same variable:

```bash
export AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT='http://127.0.0.1:8899/search?q={query}'
```

Check it before a long run, and read the answer by status code:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8899/health
```

- `200` adapter healthy and keyed
- `503` adapter running without a key, so write `AUTOSCOUT_SEARCH_API_KEY=<key>`
  to `~/.config/autoscout/search.env` (mode 0600) and run
  `systemctl --user restart autoscout-search-adapter.service`
- `000`, no answer, adapter not running, so start that service

Without the variable the stage is offline-safe and degrades rather than failing,
so a missing endpoint shows up as a quieter weekly result, not as an error. That
is why the check is worth one command before a run.

To install it on a server, once: copy `deploy/server/autoscout-search-adapter.service`
to `~/.config/systemd/user/`, replace the `%h/auto-scout` placeholder with the
path of the production checkout, write the key file as above, and run:

```bash
loginctl enable-linger
systemctl --user daemon-reload
systemctl --user enable --now autoscout-search-adapter.service
```

Linger is what starts a user service at boot with nobody logged in. A laptop run can
still use the adapter on the Nanode over the tailnet:
`docs/nanode-search-adapter-runbook.md` covers its endpoint, start, verify,
logs, key rotation, stop, and the SSH tunnel fallback.

## The nightly timers on the run server (#1057)

The run server is the only machine that runs the scan and pushes state (ADR 0022).
Three systemd user timers start it, with no one watching:

- `autoscout-kirkus.timer` at 01:30 Asia/Jerusalem runs the Kirkus pass, `./scripts/collect-kirkus --scheduled`.
- `autoscout-run.timer` at 02:30 Asia/Jerusalem, every night, runs `./scripts/run-weekly --scheduled` and then `./scripts/doctor`.
- `autoscout-run-retry.timer` at 04:30 Asia/Jerusalem, every night, is the second attempt (#1160): `./scripts/run-weekly --scheduled` with `AUTOSCOUT_SECOND_ATTEMPT=1`, then `./scripts/doctor`.

### The second attempt of 04:30 (#1160)

Whatever stopped the run of 02:30 (a network or GitHub outage at that minute, a crash, the machine off or asleep at 02:30), the night gets a second try.
The second attempt reads the run log of the date, `workflow-log.json`, under the run lock:

- The date already has a completed run (successful or needing attention, scheduled or manual) whose log is committed: it does nothing, writes nothing and sends no report; the journal has one line.
- A run is in progress: it skips with one line and no report; a scheduled run in progress sends the night's report itself, a manual one sends none. The unit is ordered after `autoscout-run.service`, so a run of 02:30 still going at 04:30 is waited for, at most until its limit of 11 hours 30 minutes stops it at 14:00 (#1175), and the second attempt then decides on its result.
- Otherwise it runs the search: the run of 02:30 never started, was interrupted, stopped at any stage, or died while publishing its log (its completion is in the working tree only, so it may never have sent its report). It is the same operation as the first attempt, so it resumes what the first one left, as any second run on one date does.

A stop that needs a person (a repository blocker, a code gate that fails again) is repeated too: the repeat ends within seconds, or within one check of the code, and is never less safe than the next night's run, which would meet the same refusal. It also picks up a fix merged between the two attempts.

The rule for the morning report: each attempt that runs sends its own report, so a night with a failed first attempt has two; the second one says « (повторная попытка)» in the subject and, in its second line, how the first attempt ended. That second report describes the night's final state. A second attempt with nothing to do sends nothing, so a good night still has one report.
A machine off at both 02:30 and 04:30 misses the night, as before: both timers keep `Persistent=false`, so a machine powered on later starts nothing.
A machine asleep (suspended) across 04:30 is different: systemd starts an elapsed timer on resume, as it already does for 02:30, so after a daytime wake from a night whose run did not complete the second attempt runs then. There is no latest start time: the night's run comes first.
A second attempt that starts before 04:30 local time belongs to an earlier night (a wake after midnight); the wrapper skips it with one journal line, because it would take the new date and run before that night's own run of 02:30. `RETRY_NOT_BEFORE` in the wrapper holds that time and must equal the timer's.

A manual run is started on the server, the same way as always; nothing needs pausing.
A scheduled start that meets a run in progress skips itself with one line in the journal and exit 0. A lock left by a run that died is not a run in progress: within its first hour the scheduled run is refused and reports «остановлен», after that it is taken over. The second attempt takes it over at once, whatever its age, when its PID provably no longer exists (#1175): its unit starts only after the first attempt's unit has ended, and systemd ends every process of a unit with it.
A manual run that meets the scheduled run in progress is refused as before; start it again after that run ends.
A scheduled run whose own update pulls new commits stops before writing anything, releases the lock and starts again once on the merged code (#1117); only the second start sends the morning report.
The run of 02:30 reads the result of the pass of 01:30 and makes no pass of its own; when that pass is still running, the run does not wait for it.

### The gate on new code (#1124)

GitHub checks nothing (CI is disabled), so the run server checks new code itself before a night runs it.
After its repository update, and before any stage that reads or writes state, a scheduled run asks whether the checked-out code has passed `./scripts/check` on this server.

- "New code" is any change outside `runs/`. The server's own nightly state commits touch only `runs/`, so they never start the check; a merge of code, tests, configs or docs does, once.
- Code that already passed here: the run goes on with no check. The run log's `code-gate` entry says `passed`, with `checked` (whether this start ran the check) and `passed_at` (when that code passed); after a restart on pulled code, `checked` is false and `passed_at` is a few minutes earlier, from the first start.
- New code whose pins the `.venv` does not meet (a merge that bumps a pin in `pyproject.toml` or adds a dependency): before the check, the gate updates the `.venv` itself (#1130). See "A `.venv` behind the pins" below.
- New code that passes (6 minutes 24 seconds on the server on 2026-11-17): the pass is recorded and the run goes on; when its update pulled the code, or the gate updated the `.venv`, it then restarts once on it as before (#1117).
- New code that fails, or a check that does not finish within 30 minutes (the check and every test process it started are then killed): the night runs on the last code that passed here when that is safe (#1159, below). Otherwise the run stops at stage `code-gate` with `new_code_failed_check` (or `new_code_check_timed_out`), exit 1. Nothing is scanned and the State Store is never opened; the run reads only its own run log and evidence, as every start does, and writes only the run log, which the next run sets aside as usual. The lock is released. The morning report says «остановлен» and names the reason and the commit.
- A failed version stays checked out; the gate never moves the checkout back, because that would leave `main` behind origin and a daytime state commit would then diverge from it. The 01:30 Kirkus pass makes no request on it, a manual run warns, and the next night's update pulls whatever is new and checks the result again.

#### A failed gate runs the last passing code (#1159)

A failed gate must not cost the night (the operator's decision of 2026-11-20). When new code fails the check, times out, or the `.venv` cannot be brought to its pins (`new_code_failed_check`, `new_code_check_timed_out`, `server_venv_out_of_date`, `server_venv_update_failed`, `server_venv_update_timed_out`), the scheduled run runs the code of the commit recorded in `data/code-gate/passed.json` instead, against the production checkout's configs, state and Git publication.

How, without moving the checkout back:

- The run writes that commit's `autoscout/` package, and nothing else, to `data/code-gate/last-passed/` with `git archive`; it is written afresh at every such night, so it is always the commit the record names now.
- The run releases its lock and starts the same command again with the production `.venv`'s Python (the one whose packages met the pins, whatever Python decided), the snapshot first on `PYTHONPATH` and `PYTHONSAFEPATH=1` (so neither the working directory nor the editable install puts the new package first), and `AUTOSCOUT_REPOSITORY_ROOT` naming the production checkout. Every module reads `configs/`, `data/` (the State Store database included) and `runs/` from there, and the run commits and pushes state from there as on any night. The checkout stays on the new commit, on `main`, level with origin.
- The started-again run checks nothing. After its repository update it confirms that the checkout is still at the commit that was judged, that the record and the snapshot name the commit it runs, that its package came from the snapshot and that it runs on the production `.venv`; otherwise it stops at `code-gate` as before.

When it is safe (judged before the run starts again; any doubt stops the night as before, with the gate's own reason, and the run log's failed entry names why in `fallback_refused`):

- The checkout is still at the commit the gate checked; a commit pulled during the check is not judged (`checkout_moved_during_check`) and the next night checks it.
- A pass is recorded, by code that has this mechanism. With no recorded pass the night stops exactly as before. A pass recorded before #1159 cannot run as fallback (`last_pass_predates_fallback`): its modules would read the checkout next to their own files, so the first pass of this code on the server is what enables the fallback.
- The files the earlier code reads from the checkout are those it passed with. Every path that differs between the passing commit and the checked-out one must be under `autoscout/` (the snapshot brings its own), under `runs/` (state), or one the run never reads: `tests/`, `docs/`, `scripts/`, `deploy/`, `.github/`, `.claude/`, `.agents/`, `pyproject.toml` (judged by its pins, below), the top-level Markdown files, `LICENSE`, `skills-lock.json`, `.gitguardian.yml`. A merge that changes `configs/`, tracked `data/`, `ru-sources/`, `reqs/` or `.gitignore` stops the night (`new_commit_changes_files_the_run_reads`, with the paths).
- The state's format is unchanged. A merge that changes anything under `autoscout/state_store/` stops the night (`new_commit_changes_state_store`): that code defines the SQLite schema version, the JSON layout and the backend marker, so new state code may expect state the earlier code cannot read. A database already migrated past the earlier code's schema is also refused by the store itself when it opens.
- The production `.venv` meets the passing commit's runtime pins: its dependencies, every extra in use and every extra the pass was checked with (recorded in `passed.json`, so an install of newer code killed halfway cannot make an extra look unused); `dev` only serves the check. A missing requirement with an environment marker counts as missing unless its marker is false on this server. A merge whose new pins the gate already installed therefore stops the night (`venv_does_not_meet_last_pass_pins`).

What the operator sees: the run log's `code-gate` entry has status `ran_last_passed` with both short commits and the gate's reason. The morning report says «требует внимания» (at least) and, without commits: «Поиск шёл на предыдущей версии кода, прошедшей проверку на этом сервере: новая версия не прошла проверку ./scripts/check. Следующий ночной запуск проверит новый код снова.» The doctor after the run repeats that sentence. A night that could not fall back says in its report why the earlier version did not run either.

Each later night checks the new code again (7 to 20 minutes) and, while it still fails, runs on the earlier version again. The fix is the same as before: merge a fix to `main`, or repair the server and run `./scripts/code-gate --run`.

A later night starts on the failed code that is checked out; when that code cannot even start the command, the night starts from the last passing code instead (#1169, below). What it does not cover: state written by hand or by a manual run on the failed code (manual runs are never gated), beyond the State Store's code and schema; caches under `data/` that the earlier code reads with its own version checks.

The Kirkus pass of 01:30 is left as it is: on code that has not passed here it makes no request. The run of 02:30, on the earlier version too, makes the Kirkus pass itself because none ran that day, so the night loses no Kirkus pass; letting the 01:30 pass fall back as well would judge the same safety twice an hour apart for no gain.

#### A checked-out version that cannot start (#1169)

A failed version stays checked out, so the next night starts on it. When it cannot even be imported (a syntax error, a missing module, an import in `autoscout/__main__.py` or a command module that raises), the command would die before it could pull a fix, check it, fall back or send its report. The wrapper therefore starts the search, and the doctor after it, through `autoscout-scheduled-start`, a small launcher installed beside it outside the checkout, which uses the standard library alone and imports nothing from the checkout:

- It first asks a separate Python, the one `./scripts/run-weekly` would use, to `import autoscout.__main__` in the checkout (at most 2 minutes); an import that exits, even with status 0, counts as failed. When that works it becomes `./scripts/run-weekly --scheduled`, the same command as before, with no line of its own: a night on code that starts is unchanged.
- When the import fails, the journal gets the import's traceback and one line, and the launcher writes the package of the commit recorded in `data/code-gate/passed.json` to `data/code-gate/start/` with `git archive` (a snapshot of that same commit already there is kept) and starts `run-weekly --scheduled` on it, from the checkout and with the arguments of `./scripts/run-weekly`, the same way the fallback of #1159 starts a snapshot (the checkout's `.venv`, the snapshot first on `PYTHONPATH`, `PYTHONSAFEPATH=1`, `AUTOSCOUT_REPOSITORY_ROOT`), marked with `AUTOSCOUT_START_FROM_LAST_PASSED`.
- That code, which passed here, does what any scheduled start does: the lock, the preflight, the update (which pulls a fix pushed since), and the gate on the checked-out code. When the checked-out code passes, it releases the lock and restarts on it, leaving the snapshot, as after pulled code (#1117). When it fails, the rules above decide: the night runs on the last passing code, or stops at `code-gate` with `fallback_refused` and a morning report that names the reason. Nothing is written before that decision, and the checkout is never moved.
- When the last passing code cannot start either (no pass recorded, a pass of code from before #1169, no `.venv`, a snapshot that cannot be written or imported), the launcher sends the morning report itself: «остановлен», the kind of the import failure and the reason (`no_recorded_pass`, `last_pass_predates_start`, `last_pass_unavailable`, `last_pass_cannot_start`), and exits 1.
- The second attempt of 04:30 goes through the same launcher, so it is decided the same way; its report is marked as usual. A doctor that cannot start prints one line instead of a traceback. The Kirkus pass of 01:30 does not use the launcher: on code that cannot start it fails in the journal and the run of 02:30 makes the pass itself.

Only the command's own import is probed. A module the run imports later, while it runs, that is broken still stops that night with the unexpected-error report of #1158, as before.
The launcher holds its own copies of the values it shares with the package (`START_PROTOCOL` and the start's variable in `autoscout/code_gate_fallback.py`, the run's arguments, the report's settings and wording); a test keeps them equal, so a change to one is a change to both. It starts only a commit whose `autoscout/code_gate_fallback.py` declares `START_PROTOCOL`, so until a version with #1169 has passed on the server once, a night that cannot start reports `last_pass_predates_start`.

#### A `.venv` behind the pins (#1130)

Before running the check, the gate compares the production checkout's `.venv` with the pins of the checked-out `pyproject.toml`: the runtime dependencies, the `dev` extra, and every other extra the `.venv` already uses.
When the `.venv` is behind, the gate runs `.venv/bin/python -m pip install -e ".[dev,<extras in use>]"` from the checked-out commit, with a 10-minute limit (pip's own network timeout is 60 seconds per connection), then compares the `.venv` with the pins again and runs the check as usual.

- Which extras: `dev` always, because the check needs it, and every other extra of which at least one package is installed, or was installed at an earlier check (remembered in `data/code-gate/extras.json`, so an install killed halfway cannot drop an extra for good). On the server that is `pw` (`pypdf`), so the install is `".[dev,pw]"`. An extra the server never installed is never added by a night; install it by hand once and later nights keep it up to date. A requirement with an environment marker (`; sys_platform == ...`) is compared only when its package is installed.
- What it logs: the packages that were behind, pip's output and every package whose version changed (before and after) go to `data/code-gate/last-install.log`; the changed packages also go into the run log's `code-gate` entry (`environment_update`) and the morning report («Окружение .venv на сервере обновлено ночью под новый код: pytest 8.4.2 → 8.5.0.»).
- After an update the run restarts once before scanning, as after pulled code: the process may already have imported a package of the old version. The restarted run finds the pass and the update its first start recorded and reports the update. A run restarts at most twice: once for pulled code or an update, and once more only for an update its restarted start made. Code pulled by a restarted start is checked but not restarted on, as before #1130.
- An install that fails (no network, a pin that cannot be resolved, pip exits 0 but the `.venv` still differs from the pins) stops the run at stage `code-gate` with `server_venv_update_failed`; one that runs over its limit, with `server_venv_update_timed_out` (pip and every process it started are killed). Nothing is checked or scanned, the State Store is not opened, the lock is released, and the morning report says that the environment, not the code, failed. Nothing is recorded, so the next night tries again; `./scripts/code-gate --run` between runs tries now. When the last passing code can run, the night runs on it instead of stopping (#1159).
- Only the production checkout's own `.venv` is touched. A checkout without a `.venv`, or whose packages or `pyproject.toml` cannot be read, installs nothing, and the check then decides as before (without a `.venv` it refuses the system Python, `new_code_failed_check`; with a `.venv` behind the dev pins, `server_venv_out_of_date`).
- A killed install can leave the `.venv` half-updated. The next run sees it behind the pins, or the project itself missing, and installs again. Pins are compared as pip compares them (`1.0` meets `==1.0.0`).
- The Kirkus pass of 01:30 never installs: on code that has not passed here it does nothing (see below), and the run of 02:30 updates the `.venv`, checks the code and makes the pass itself.
- A manual `./scripts/run-weekly` never installs; it runs no check, as before.

The record of the last code version that passed, and the output of the last check and the last install, are in `data/code-gate/` of the checkout (`passed.json`, `last-check.log`, `last-install.log`), ignored by Git and kept per machine, with the snapshot of the last passing package a night ran on (`last-passed/`, #1159).
The check runs in its own temporary folder under `~/.cache/autoscout/code-gate/` (set `AUTOSCOUT_CODE_GATE_TMPDIR` to move it), removed afterwards, so it neither depends on `/tmp` nor leaves lock files there (#1119). It gets none of the service's `AUTOSCOUT_*` variables, so the mail settings and the live search endpoint are not in its environment.

What the gate does not cover:

- A change made only under `runs/` counts as state and is never checked, also when a person makes it (for example a backend switch in `state-backend.json`). Check such a change by hand with `./scripts/code-gate --run` after pulling it.
- A scheduled start whose checked-out code can be imported runs on it, and the lock claim, the preflight, the update and the gate itself run on that code; so does the decision to run the last passing code (#1159). That code is unchecked after a failed night, after a manual `./scripts/run-weekly` (which pulls, with its warning), and after a pull by hand such as step 1 of the install below; only a pull by the scheduled run itself is checked before anything else runs on it. Code that cannot be imported no longer stops the night (#1169, above), but code that imports and also broke the gate would not be stopped; this needs two faults in one merge, and it is the reason the GitHub check of #1103 is worth having as a second, independent gate.
- The check gets none of the service's `AUTOSCOUT_*` variables, but it runs as the service user, so files in that user's home (the mailbox key files) stay readable, as for any test run on that machine.

### Installing the gate on the run server

No unit or wrapper file changed for the gate. After the merge, between two nightly runs and never while one is in progress (`./scripts/doctor`), in the production checkout:

1. `git pull --ff-only`.
2. `./scripts/code-gate --run`: about six and a half minutes. It updates the `.venv` first when it is behind the pins (#1130) and prints each package it changed. It must end with "passed ./scripts/check and is recorded"; otherwise read `data/code-gate/last-install.log` or `data/code-gate/last-check.log`.
3. `./scripts/code-gate` now says the commit has passed.

These steps are optional. Without them the first night does the same by itself: the old code pulls the change and restarts on it, and the new code updates the `.venv` if needed and checks itself before scanning.
On 2026-11-17 it did not: the gate of #1124 treated a `.venv` behind the dev pins as a stop, and the night was lost. Since #1130 a merge that changes the pins in `pyproject.toml` needs no step on the server.
The first night after #1130 itself is merged still runs the gate of the code it started on, which installs nothing; that is harmless as long as #1130 changes no pin, and it does not.

Clearing a failed gate needs no manual reset, because only passes are recorded and every scheduled run checks unpassed code again:

1. Read why it failed: `data/code-gate/last-check.log` in the production checkout.
2. A defect in the code: merge the fix to `main`. The next night pulls it, checks it and runs.
3. A problem of the server itself (for example `server_venv_update_failed`, an install of the pins that failed, usually for lack of network; read `data/code-gate/last-install.log`): repair it, then run `./scripts/code-gate --run` in the production checkout, which also retries the install. It runs the same check and records a pass, so the next night runs without checking again. Run it between nightly runs, never while one is in progress.

`./scripts/code-gate` without arguments only says whether the checked-out code passed here.
A manual `./scripts/run-weekly` is never blocked by the gate (the operator decides); on code that has not passed here it prints «Внимание: эта версия кода не проходила проверку ./scripts/check на этой машине.» and goes on.
The Kirkus pass of 01:30 follows the same rule without running the check itself: on code that has not passed here it makes no request, prints one line and exits 0. The run of 02:30 then checks the code and, when it passes, makes the Kirkus pass itself, because no pass ran that day. The pass collects into a cache the run then reads, so letting unchecked code fill it would defeat the gate.

The server clock is UTC, and 01:30 in Jerusalem is the previous UTC day in winter.
Both services therefore set `TZ=Asia/Jerusalem`, so the pass and the run agree on the date.

All units are in `deploy/server/`, beside the search adapter:

| File | Installed to | What it is |
|---|---|---|
| `autoscout-run.service`, `autoscout-run.timer` | `~/.config/systemd/user/` | The nightly scan |
| `autoscout-run-retry.service`, `autoscout-run-retry.timer` | `~/.config/systemd/user/` | The second attempt of 04:30 (#1160) |
| `autoscout-stopped@.service` | `~/.config/systemd/user/` | The report of a scheduled unit that ended without its own (#1175); started by `OnFailure=` of the three services, never enabled; it runs `autoscout-stopped-report`, installed to `~/.local/bin/` (mode 0755) beside the wrapper |
| `autoscout-kirkus.service`, `autoscout-kirkus.timer` | `~/.config/systemd/user/` | The Kirkus pass, an hour earlier; it needs no search adapter |
| `autoscout-review.service` | `~/.config/systemd/user/` | The editor's review page, below |
| `autoscout-scheduled-run` | `~/.local/bin/` (mode 0755) | The wrapper every service calls; `kirkus` as its argument selects the pass, `retry` the second attempt |
| `autoscout-scheduled-start` | `~/.local/bin/` (mode 0755), beside the wrapper | The launcher the wrapper starts the search and the doctor with (#1169); without it, or without `python3` on the service's `PATH`, the wrapper says so in one line and starts them as before |

The wrapper and the services hold the path of the production checkout, `/mnt/data/workspace/auto-scout`; change it there for another checkout.
The wrapper's `retry` marks the start with `AUTOSCOUT_SECOND_ATTEMPT=1`, which older code ignores: installed before its code is pulled, the second attempt is an ordinary scheduled start (a second run on one date is supported), so the order of installing and pulling does not matter.
The wrapper finds the launcher next to its own file. Installing #1169 means copying both, `autoscout-scheduled-run` and `autoscout-scheduled-start`, to `~/.local/bin/` with mode 0755; no unit changes. The order of installing and pulling does not matter: the old wrapper ignores the launcher, and the launcher on older code starts `./scripts/run-weekly` as before whenever the checkout can start.
To install or update, copy the files to those places and run:

```bash
systemctl --user daemon-reload
systemctl --user enable --now autoscout-run.timer autoscout-kirkus.timer autoscout-run-retry.timer
systemctl --user list-timers 'autoscout-*'
```

`list-timers` shows the next start of each timer: 01:30, 02:30 and 04:30 Jerusalem time, printed in UTC.
`autoscout-stopped@.service` is a template that the three services start on a failure; it is copied and reloaded with them and needs no `enable`. Its instance is the failed unit's name, so `systemctl --user cat autoscout-stopped@autoscout-run.service.service` shows what it runs.
Linger, set once for the search adapter, also keeps the timers running with nobody logged in.

Every scheduled run sends one short morning report by email from the AutoScout mailbox, whatever happened: the subject says «успешно», «требует внимания», «остановлен» or «пропущен» with the date.
The second attempt of 04:30 sends one only when it runs, marked « (повторная попытка)» (see above).
The body gives the counts of a completed run, the sources with problems, the stage and reason of a stopped run, and the night's Kirkus pass; never a book title. The Kirkus pass sends nothing of its own, unless systemd stopped it (see "Time limits" below).
Each failed source carries its failure streak (#1125): a source that has failed 3 attempts in a row or more is named first as broken and makes the night «требует внимания»; a first or second failure is listed under «Сбои, пока не требующие внимания» and leaves the night «успешно» when nothing else needs attention; a broken source that works again is named once under «Снова работают». A run in which more than half of the attempted sources failed (at least two) asks for attention whatever the streaks, as a general failure. Attempts are counted, not nights: a source that was not due does not move, and two runs on one date count once. The threshold is `source_failure_streak_threshold` in the weekly config (default 3); the full rule is in `docs/cli-reference.md`, "Failure streaks per source".
The settings live in `~/.config/autoscout/notify.env`, mode 0600, outside the repository: `AUTOSCOUT_NOTIFY_SMTP_HOST`, `AUTOSCOUT_NOTIFY_SMTP_PORT`, `AUTOSCOUT_NOTIFY_SMTP_USER`, `AUTOSCOUT_NOTIFY_SMTP_PASSWORD`, `AUTOSCOUT_NOTIFY_FROM`, `AUTOSCOUT_NOTIFY_TO` (meanings in `docs/cli-reference.md`).
Without the file the report is written to the journal instead.

Each job's output goes to the journal and to a per-run log file:

```bash
journalctl --user -u autoscout-run.service
journalctl --user -u autoscout-run-retry.service
journalctl --user -u autoscout-kirkus.service
```

The files are in `~/.local/state/autoscout/` (`run-*.log`, `retry-*.log` and `kirkus-*.log`, the 30 newest of each), with `latest.log`, `latest-retry.log` and `latest-kirkus.log` pointing at the newest.
A failed run prints its stage and reason there.
A run or a collection pass (Kirkus, PM, PW) that dies on an unexpected exception prints its full traceback there too, with credential-shaped strings redacted (#1158); a config exit prints the validation errors the workflow had swallowed.
The run log (`workflow-log.json`) gets an `unexpected-error` entry with the exception's kind and location (`autoscout/file.py:12 in function`, never the message or an absolute path), and `./scripts/doctor` names the kind in one line («Последняя остановка: непредвиденная ошибка RuntimeError») for as long as the run went no further. The editor's output and the morning report stay as they were: no traceback, no path.

What a run that failed early leaves modified does not block the next run: the workflow log alone, or with the run summary when the toolchain check failed (unstaged, and the summary changed in its toolchain entry only). The next run copies the files to `data/workflow-log-residue/` in the checkout and restores them; anything else still blocks.
Nor does what a run that failed late leaves in the run directory, such as the night that failed while saving its results (#1161).
The next run proves the files are that run's: they lie in the run directory, nothing is staged, nothing is deleted that the run itself would not delete, and the run log ends with that run's entries, which show it saved its candidate state and never committed its end.
It then carries them: nothing is restored, removed or moved; the run overwrites what it regenerates and commits the rest, which is what the failed run would have committed. Only the failed run's log is copied to `data/workflow-log-residue/` for diagnosis.
This holds for a later start the same night too, such as a second attempt: the run log shows which entries were never committed, even after an earlier run that date completed and was committed.
The run directory stays marked as awaiting publication (`data/late-failure-awaiting-publication/`) until a run has committed and pushed it, so a night that stops early, at the code gate or the update, does not block the next one.
Known limit: the carried files stay modified through the update. If `origin` has a newer commit that changes one of those same files, the fast-forward is refused and the night stops at `repository-update`, as it did before this change; the marker stays and nothing is lost, but every night stops there until the operator resolves the conflicting change.
Under SQLite the database stays as it is and its export in the run directory is left in place, so the doctor finds no mismatch; an editor decision recorded after the failed run is kept and committed.
A file someone dropped into the run directory is committed with the rest, never deleted; it is scanned for credentials first. A dirty file outside the run directory, a staged change, or a checkout on another branch still blocks.
The run log, the morning report («Сохранены и результаты незавершённого поиска …», or «… ещё не сохранены; ничего не удалено …» when the night stopped) and the doctor say so; the full rule is in `docs/cli-reference.md`.

### Time limits (#1162)

Nothing a scheduled job does may hang for ever, or the run lock would stay held and every later night would skip itself as a run in progress.

- Every Git command of the workflow has a limit: 20 minutes for `fetch`, `pull` and `push`, 10 minutes for every local command. A healthy command takes a fraction of that: a status of the whole checkout takes a tenth of a second, a push of tens of megabytes a few minutes. Git never asks for a credential, neither on a terminal nor through an askpass program (`GIT_TERMINAL_PROMPT=0`, an empty `GIT_ASKPASS`, `SSH_ASKPASS_REQUIRE=never`); a missing credential fails at once instead of waiting.
- A command over its limit gets SIGTERM, on which Git removes its own lock files, and whatever of it is still running 10 seconds later gets SIGKILL, together with every process it started (the credential helper, ssh, `git-remote-https`). The run then stops at its stage (`repository-preflight`, `repository-update` or `repository-finalize`) with the reason `git_timed_out`, releases the lock and sends its morning report. A run that is interrupted (Ctrl-C) or sent SIGTERM while a Git command runs stops that command the same way first. A push that timed out leaves the commit in the checkout; the next run pushes it.
- Each scheduled service has a start limit; a oneshot unit has none without it. A run that hangs is stopped, and the night still runs the same day (#1175). The run log history (2026-07-14 to 2026-11-21, 69 runs) gives the scale: usually 20 to 40 minutes, the longest scheduled run 77 minutes, the longest of all 5 hours 49 minutes (a manual run on 2026-11-08 whose extraction took 4 hours). `autoscout-run.service` and `autoscout-run-retry.service` both have `TimeoutStartSec=11h30min`: above everything a run may spend while each part stays within its own limit, 11 hours 28 minutes (all stage budgets, about five hours, and over up to three starts the code gate at 40 minutes and every Git network command at its 20-minute limit), twice the longest run and nine times the longest scheduled one. The Russian-edition stage (#1181) is not in that figure and does not add to it: it takes only what is left of it after the stages before it and the time the editorial-fit stage and publication may need, and is skipped when too little is left (`autoscout/run_time_limit.py` computes the figure from the same budgets). A first attempt that hangs is stopped at 14:00, and the second attempt, which waits for it, then runs and ends by 01:30 at the latest, an hour before the next night's run. `autoscout-kirkus.service` keeps `TimeoutStartSec=2h`; the pass stops itself at its 40-minute allowance, and the longest took 27 minutes.
- At the limit systemd sends SIGTERM to every process of the unit and SIGKILL 90 seconds later, and the journal shows the unit failed with result `timeout`. The job sends no report then, so `OnFailure=autoscout-stopped@%n.service` starts a unit of its own that sends one: «AutoScout: ночной поиск <date> — остановлен», naming the limit («systemd остановил его по пределу времени 11 ч 30 мин»), with « (повторная попытка)» for the second attempt; a stopped Kirkus pass gets «AutoScout: сбор Kirkus <date> — остановлен». The same unit reports a search that ended in any other way without its report (killed, or exited before reporting, such as a checkout that cannot start). It sends nothing when that start of the unit delivered its own report (sent, or printed to the journal when no mail is configured; a report whose sending failed is followed by this one): the wrapper names one record file per start under `~/.local/state/autoscout/reports/`, after systemd's invocation id, and the report's delivery writes it, whole or not at all (records are kept 60 days). The launcher `autoscout-scheduled-start` (#1169), which sends its own «остановлен» report when no version can start, writes the same record. The line a delivery prints to the journal of that start («Отчёт отправлен.») counts too, so a night on last passing code that predates the record (#1159) is not reported twice. A unit systemd could not start at all (exit 203, a wrapper missing or not executable) is reported as such. A Kirkus pass that exited with its own code gets nothing, as before. It is the script `autoscout-stopped-report`, installed beside the wrapper: the system Python and the standard library alone, nothing of the checkout, so a missing checkout, a mount that is down or code that cannot import does not silence it; its output is in `journalctl --user -u 'autoscout-stopped@*'`. A job killed in the few seconds between handing its report to the mail server and writing the record is reported twice; a report is never left out on a doubt.
- A night whose first attempt was stopped has two reports: the «остановлен» one, which says that the second attempt follows if it has not run yet and reports when the date's search is not complete, and the second attempt's own, marked « (повторная попытка)», which says «первый запуск этой даты был прерван и не завершился» and describes the night's final state.
- The run lock is left with a dead PID; that is not a run in progress. The second attempt takes it over at once and the next night after its first hour. What the stopped run left in the run directory does not block the second attempt when the run was stopped before its capture began, after its capture and extraction were recorded (a same-date start resumes them, #315), or after it saved its candidate state (the late failure's leftovers, #1161, carried by the next night too). Known limit: a run stopped while its capture was writing pages, or between its extraction's finalize and the end of its pipeline (under half a minute), leaves files no rule proves to be its own, so the next start stops at `repository-preflight` with `dirty_working_tree`, reports «остановлен» and changes nothing; the operator decides what to keep. The Kirkus pass lock goes with its process, so the next pass starts.

Installing the limits and the stopped report (#1175), between two nightly runs (`./scripts/doctor` shows none in progress): `git pull --ff-only` in the production checkout; copy `autoscout-run.service`, `autoscout-run-retry.service`, `autoscout-kirkus.service` and `autoscout-stopped@.service` to `~/.config/systemd/user/`, and `autoscout-scheduled-run`, `autoscout-scheduled-start` and `autoscout-stopped-report` to `~/.local/bin/` (mode 0755; the launcher of #1169 writes the same delivery record for the report it sends itself); run `systemctl --user daemon-reload` (the timers stay enabled, the template is never enabled). Then `systemctl --user show autoscout-run.service autoscout-run-retry.service autoscout-kirkus.service -p TimeoutStartUSec -p OnFailure` prints `11h 30min`, `11h 30min` and `2h`, each with `autoscout-stopped@<its name>.service`.

`autoscout-review.service` is a small local web server for the editor's review page.
It serves `~/.local/share/autoscout-review` on `127.0.0.1:8090`, where `index.html` is a symlink to `runs/current-weekly-pilot-russian/weekly-review.html` of the production checkout, and `tailscale serve` publishes port 8090 to the tailnet on port 8453.
Each run's rerendered review is therefore visible there without a copy step.

## Converting Candidate History to one file per book (#1073)

The weekly state keeps Candidate History in `candidate-history.json` until the operator converts it, once, on the run server.
After the conversion each book is its own file under `candidate-history/<xx>/<candidate_id>.json`, so a night commits only the books that changed and no committed file grows toward GitHub's 100 MB limit.
The code reads and writes either layout, whichever the state directory has, so nightly runs keep working before and after; nothing converts on its own.
The layout and the command are described in `docs/cli-reference.md`, "Candidate History on disk".

The whole cutover, this conversion and then the SQLite backend, is one step-by-step runbook with its checks and rollbacks: [State Store cutover runbook](../state-store-cutover-runbook.md). The short form of this conversion:

Convert between two nightly runs, in the production checkout:

1. Check that no run is in progress (`./scripts/doctor`); the conversion refuses with exit `6` during a run anyway.
2. `git pull --ff-only`, so the checkout has the code that knows both layouts.
3. `./scripts/shard-candidate-history --dry-run`. It writes nothing; check that `record_count` is the number of books and `single_file_bytes_reproduced=true`.
4. `./scripts/shard-candidate-history`. It writes the shards, checks the records read back against the file, and only then removes `candidate-history.json`; a failure leaves the directory as it was.
5. Verify that the books in the state directory equal the committed file:

   ```bash
   .venv/bin/python - <<'EOF'
   import json, subprocess
   from pathlib import Path
   from autoscout.state_store import Collection, open_state_store
   state = "runs/current-weekly-pilot-russian"
   committed = json.loads(subprocess.run(["git", "show", f"HEAD:{state}/candidate-history.json"], capture_output=True, check=True).stdout)
   assert open_state_store(Path(state)).read(Collection.CANDIDATE_HISTORY) == committed
   print("records equal:", len(committed))
   EOF
   ```

   `git status --short runs/current-weekly-pilot-russian` shows `candidate-history.json` deleted and `candidate-history/` new, and nothing else.
6. Commit only the conversion and push it:

   ```bash
   git add -A runs/current-weekly-pilot-russian/candidate-history.json runs/current-weekly-pilot-russian/candidate-history
   git commit -m "Convert Candidate History to one file per book (#1073)"
   git push
   ```

The next nightly run commits only the books it changed, and the run directory no longer holds a separate `candidate-history.json` copy.

Rollback: `git revert <conversion commit>` and push.
The revert brings `candidate-history.json` back and removes the shards, and the state directory is in the single-file layout again, which the code still supports.
Revert before the next nightly run commits: once a run has committed book files, the revert would bring back the history of the conversion day.
After that, write the single file from the shards instead and commit it:

```bash
.venv/bin/python - <<'EOF'
import shutil
from pathlib import Path
from autoscout.state_store.layout import stored_history_bytes
state = Path("runs/current-weekly-pilot-russian")
history = stored_history_bytes(state)
(state / "candidate-history.json").write_bytes(history)
shutil.rmtree(state / "candidate-history")
EOF
```

## Which backend serves the weekly state, and rolling back to JSON (#1093)

The weekly state directory decides its own backend.
While `runs/current-weekly-pilot-russian/state-backend.json` is absent, it is JSON, as it always was.
`./scripts/state-database cutover` (after `./scripts/state-database rebuild`) exports the database into the directory and writes that file with `{"backend": "sqlite"}`; from then on every command uses SQLite, the editor commands without `--config` included, and a command that selects `json` through `AUTOSCOUT_STATE_BACKEND` or a config is refused.
A rebuild alone changes nothing for the commands, so it can be rehearsed on live state.
The marker is committed with the state; a checkout that pulls it without a database is refused with the rebuild command until it builds one.
The commands are described in `docs/cli-reference.md`, "The SQLite backend".

The cutover itself (pre-checks, rebuild, round-trip check, one scan by hand, a week of watching) and what the doctor's findings mean on the server and on other machines are in [State Store cutover runbook](../state-store-cutover-runbook.md).

Rollback to JSON is one step, between two nightly runs, on the run server:

1. Check that no run is in progress (`./scripts/doctor`); the command refuses with exit `6` during a run anyway.
2. `./scripts/state-database rollback`.
   It writes the database's export into the state directory, so the JSON holds everything the database holds, and then removes `state-backend.json`.
   It refuses a state directory changed outside the database since the last export; read the error, and only when the database is the state to keep, rerun with `--force`, which writes the database over that change.
3. Commit the export and the marker's removal and push them:

   ```bash
   git add -A runs/current-weekly-pilot-russian
   git commit -m "Roll the weekly state back to the JSON backend (#1093)"
   git push
   ```

From the next command on the JSON files are the state again.
The database file under `data/state-store/` is kept but stale; cutting over again starts with `./scripts/state-database rebuild --overwrite`.
Unset `AUTOSCOUT_STATE_BACKEND` wherever it was set to `sqlite`, or commands that read it keep using the stale database.
