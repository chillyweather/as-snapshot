# Cutover runbook: Candidate History per book, then SQLite (#1077)

For issue #1077, part of #1011 and ADR 0023.
It is read at the run server's terminal, one step at a time, by an operator who has not touched this work for weeks.
Every step is a command, what you should see, and what to do when you see something else.

The weekly state (`runs/current-weekly-pilot-russian`) moves in two separate changes, in this order:

| | Change | What it does | Rollback |
|---|---|---|---|
| Part 1 | Candidate History to one file per book | Replaces the 23 MB `candidate-history.json` with 2,932 small files under `candidate-history/<xx>/`, so a night commits only the books it changed and no file nears GitHub's 100 MB limit. Nothing else changes. | `git revert` of the conversion commit |
| Part 2 | The SQLite backend | The state lives in a local database; Git keeps a JSON export of it. | `./scripts/state-database rollback` |

Do Part 1, let at least one normal nightly run happen on it, check that run (section "After the first nightly run on Part 1"), and only then do Part 2.
Never do both in one sitting: a night between them tells you which change broke what.

The server is the only machine that runs the scan and pushes state (ADR 0022).
Both parts are done there, in the production checkout (`/mnt/data/workspace/auto-scout`, the path the units in `deploy/server/` use), on a day when nothing else is being changed.
Allow 15 minutes for Part 1 and, for Part 2, 30 minutes plus one full scan (up to two hours, `docs/weekly-runtime-budget.md`).

## Facts the steps rely on

- **The marker decides the backend.** Once `runs/current-weekly-pilot-russian/state-backend.json` (`{"backend": "sqlite"}`) is committed, every command that opens that state directory uses SQLite, with or without `--config` (#1093). Do **not** set `AUTOSCOUT_STATE_BACKEND` on the server after the cutover: the weekly run, doctor and editor commands do not need it, and with it set, the source-onboarding replay validation leaves a stray database under `data/state-store/`. A JSON selection (`AUTOSCOUT_STATE_BACKEND=json`, or `state_backend` in the weekly config) on a marked directory makes the weekly run stop at `operation-start` with `state_backend_conflict`, and makes the doctor report the same.
- **The database is not in Git.** It is `data/state-store/runs--current-weekly-pilot-russian.sqlite3` (about 26 MB), git-ignored, on the server's local disk. A database on a network share or a cloud-synced folder is refused when that can be detected. The off-site backup is the JSON export that Git keeps. A local snapshot is optional ("An optional local snapshot of the database", below).
- **The JSON files change only at an export.** Under SQLite the files in the state directory are written at the end of a weekly run, after every state-mutation command (feedback, visibility, profile, onboarding and the like), and by `./scripts/state-database export`. Anything that reads the files directly sees the last export. The Git envelope commits the export, never the database.
- **Hand-editing a state file is not supported.** The next export refuses a state directory changed by anything but the export; `--force` discards the edit (see the doctor table).
- **The weekly run checks the export itself.** Its `state-export` stage writes the export and compares every exported file with the database; a mismatch stops the run as `intervention-required` before anything is committed. The doctor's `state-store-finding` names one action for each case ("What the doctor says").
- **After Part 1 a run writes no separate `candidate-history.json`** when its output directory is the state directory, which is how `./scripts/run-weekly` starts it. The books are only under `candidate-history/`.
- **The two commands' exit codes.** `./scripts/shard-candidate-history` and `./scripts/state-database` exit `0` when done, `2` when they refuse (the line `error=` says why, nothing was changed), and `6` while a weekly run holds the run lock (`reason_code=weekly_run_in_progress`; wait for the run to end and repeat the same command; never remove the lock). `shard-candidate-history --dry-run` is allowed during a run.

## Other machines

After Part 2 the run server is the single writer. Any other checkout that pulls the marked state directory (the development machine, the Mac) has no database:

- The doctor there says «Папка состояния переведена на SQLite, но базы на этом компьютере нет» and recommends `./scripts/state-database rebuild`. Every command that opens the state is refused until that is run: `run-mvp`, `render-review`, `record-feedback`, `editorial-profile list` and the rest print one line, `error=the state directory is kept in SQLite … but its state database … does not exist on this machine; build it from the committed JSON with ./scripts/state-database rebuild …`, exit `2` and change nothing (the same refusal `state-database` gives, #1098).
- **May do** after `./scripts/state-database rebuild`: read the state, run the doctor, render a review, develop and run `./scripts/check` (its tests use their own databases).
- **After every later `git pull` that brings state, run `./scripts/state-database rebuild --overwrite`.** The doctor names it itself: when the state files equal what Git has committed and differ from the database, the database is the older side and its action is `rebuild --overwrite` (#1098); it never recommends `export --force`, which would overwrite the state you just pulled with the older local database. The local database is a throwaway copy of the export; rebuilding it loses nothing.
- **May not:** run the weekly scan, run a state-mutation command and keep its result, push state files. Such a write goes to the local database and the local files only; it competes with the server's own export and Git then conflicts. If you experiment, throw the experiment away (`git restore runs/`, and delete what it created) before the next pull. Real editor work always runs on the server.

## Stopping and starting the timers

The normal operation never pauses a timer (ADR 0022: the run lock is the only coordination), and `docs/operator/run-server.md` gives only the install commands (`enable --now`, `list-timers`).
For these two changes the timers are stopped so that no night starts between two steps. The commands below are standard systemd on the unit names in `deploy/server/`; **confirm the names against `systemctl --user list-timers 'autoscout-*'` the first time**, because this part was written from the units, not rehearsed on the server.

**Stop** (a stopped timer does not stop a run already started):

```bash
systemctl --user stop autoscout-run.timer autoscout-run-retry.timer autoscout-kirkus.timer
systemctl --user is-active autoscout-run.timer autoscout-run-retry.timer autoscout-kirkus.timer autoscout-run.service autoscout-run-retry.service autoscout-kirkus.service
```

Expect six lines, all `inactive`. `activating` on a `.service` line means a pass or a run is under way: wait for it (`journalctl --user -fu autoscout-run.service`, Ctrl-C leaves the run alone) and ask again. Do not go on while any service is `activating`.
The timers use `Persistent=false`, so a start missed while they were stopped is not made up. Do the work in the daytime and a missed night does not arise.

**Start** (done at the end of each part):

```bash
systemctl --user start autoscout-run.timer autoscout-run-retry.timer autoscout-kirkus.timer
systemctl --user list-timers 'autoscout-*'
```

Expect the three timers with a next start: 01:30 (Kirkus), 02:30 (run) and 04:30 (the second attempt, #1160) Asia/Jerusalem, printed in UTC.
If the server reboots while the timers are stopped they come back on their own (they stay enabled).
Do not leave them stopped overnight by accident: a night without a run is reported nowhere, because the morning report is sent by the run.

---

# Part 1: Candidate History to one file per book

Before: `candidate-history.json`, one 23 MB file, 2,932 books on 2026-11-14.
After: `candidate-history/<xx>/candidate-<24 hex>.json`, one file per book, 256 directories of at most 20 files each; the code reads and writes either layout, so nothing else changes.

### 1. Check the state of the checkout

```bash
cd /mnt/data/workspace/auto-scout
git status --short
./scripts/doctor
```

Expect: `git status --short` prints nothing. The doctor's first line is «AutoScout в порядке …» (`healthy`) or a source-failure verdict (normal after nights with failed sources). The doctor prints a line «Хранилище состояния: JSON.».

If not: a repository-blocker, a run in progress, a persistence failure or an unfinished operation means a night did not finish cleanly. Do not start. Follow the doctor's own single action (`docs/operator/doctor.md`, "Doctor diagnostics") and begin again.

### 2. Stop the timers

"Stopping and starting the timers", above. Expect all `inactive`.

### 3. Pull the code that knows both layouts

```bash
git pull --ff-only
git log --oneline -1
```

Expect: the pull succeeds (or says `Already up to date.`). The log shows the commit you intend to deploy; the layout code has been on `main` since PR #1088 (`git merge-base --is-ancestor 638248f HEAD && echo ok` prints `ok`).
If the pull is refused (not a fast-forward) or stops on local changes, stop. Someone changed the server's checkout by hand.

### 4. Dry run

```bash
./scripts/shard-candidate-history --dry-run
```

Expect, in under a second (0.9 s on the real state):

```
candidate_history_conversion=dry_run
record_count=2932
shard_count=2932
directory_count=256
largest_directory_count=20
total_shard_bytes=22338964
single_file_bytes=23219381
single_file_bytes_reproduced=true
replaced_existing_shards=false
seconds=0.90
```

The numbers grow with the catalogue; what must hold is `single_file_bytes_reproduced=true` and `record_count` equal to `shard_count`.
If it prints `candidate_history_conversion=refused` (exit `2`) with `error=`: `the state directory already has candidate-history/` means Part 1 is already done (go to "After the first nightly run on Part 1"). A malformed record or a missing `candidate_id` is named in the error; stop and report it, do not use `--force`. Exit `6`: a run is in progress, so wait for the end of it.

### 5. Convert

```bash
./scripts/shard-candidate-history
```

Expect `candidate_history_conversion=converted` with the same numbers as the dry run, in about 2 seconds (1.8 s). The command writes the shards into a staging directory, reads them back, checks they equal the original records, and only then moves them into place and deletes `candidate-history.json`. A failure at any point leaves the directory as it was.

### 6. Check that the books equal the committed file

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
git status --short
```

Expect `records equal: 2932` (your count), then exactly two lines:

```
 D runs/current-weekly-pilot-russian/candidate-history.json
?? runs/current-weekly-pilot-russian/candidate-history/
```

If the script raises `AssertionError` or any other line appears in `git status`, stop and do not commit. Put the old layout back with `git restore runs/current-weekly-pilot-russian/candidate-history.json && rm -r runs/current-weekly-pilot-russian/candidate-history`, then report what you saw. (Until you commit, the doctor reports a repository-blocker; that is expected.)

### 7. Commit and push the conversion only

```bash
git add -A runs/current-weekly-pilot-russian/candidate-history.json runs/current-weekly-pilot-russian/candidate-history
git commit -m "Convert Candidate History to one file per book (#1073)"
git push
git show --stat --format=%h HEAD | tail -1
```

Expect the last line `2933 files changed, …` (2,932 books added, the single file removed; one more book than in 2026-11-14 means one more file). Write the commit hash down: it is the commit to revert in the rollback below. `git status -sb` shows `## main...origin/main` with nothing ahead.
If the push is rejected, stop: the server is the only writer, so a rejection means something else pushed to `main`. Do not force. Report it.

### 8. Check, then start the timers

```bash
./scripts/doctor
```

Expect a healthy or source-failure verdict and «Хранилище состояния: JSON.». Then start the timers ("Stopping and starting the timers").

### 9. Rollback of Part 1 (before the next nightly run commits)

When: the doctor is unhappy after the conversion and its action does not clear it, or the next run stops for a reason you trace to the layout. Do it before a nightly run has committed book files.

```bash
systemctl --user stop autoscout-run.timer autoscout-run-retry.timer autoscout-kirkus.timer
git revert --no-edit <conversion commit>
git push
./scripts/doctor
```

Expect the revert to bring back `candidate-history.json` and remove the shards (`ls -d runs/current-weekly-pilot-russian/candidate-history*` prints only the file), and the doctor to be healthy («Хранилище состояния: JSON.»). Then start the timers. The code still supports the single file, so the nightly run is back where it was. (Rehearsed: the revert, the push and the doctor pass; converting again afterwards works.)

After a nightly run has committed book files, a revert would bring back the history of the conversion day. Write the single file from the books instead and commit it:

```bash
systemctl --user stop autoscout-run.timer autoscout-run-retry.timer autoscout-kirkus.timer
.venv/bin/python - <<'EOF'
import shutil
from pathlib import Path
from autoscout.state_store.layout import stored_history_bytes
state = Path("runs/current-weekly-pilot-russian")
history = stored_history_bytes(state)
(state / "candidate-history.json").write_bytes(history)
shutil.rmtree(state / "candidate-history")
EOF
git add -A runs/current-weekly-pilot-russian/candidate-history.json runs/current-weekly-pilot-russian/candidate-history
git commit -m "Return Candidate History to a single file (#1073)"
git push
./scripts/doctor
```

Expect `git status --short` to show exactly the file added and the books deleted before you commit, and the doctor to be healthy afterwards. Then start the timers.

## After the first nightly run on Part 1

The morning after Part 1, on the server:

| Check | Command | Expect |
|---|---|---|
| The morning report | the email | «успешно» or «требует внимания» for the same reasons as before (failed sources), not «остановлен» |
| Pushed | `git status -sb \| head -1` | `## main...origin/main`, nothing ahead |
| The night committed only changed books | `git show --stat --format='%h %s' $(git log -1 --format=%h -- runs/current-weekly-pilot-russian/run-summary.json) \| tail -1` | a few hundred files (the five weekly commits of 2026-11-03 to 2026-11-10 changed 162 to 474 existing books and added 14 to 279 new ones, so 180 to 750 files). About 2,900 means the whole directory was rewritten: stop and use the rollback |
| No separate copy | `test ! -e runs/current-weekly-pilot-russian/candidate-history.json && echo none` | `none` |
| No book lost | `find runs/current-weekly-pilot-russian/candidate-history -name '*.json' \| wc -l` | the conversion's count or more |
| The doctor | `./scripts/doctor` | healthy or source-failure; «Хранилище состояния: JSON.» |
| The log | the snippet below | a list holding `terminal:completed/…` and no stage you did not see the night before |

```bash
.venv/bin/python - <<'EOF'
import json
log = json.load(open("runs/current-weekly-pilot-russian/workflow-log.json"))
print([f'{e["stage"]}:{e["status"]}' for e in log if e["status"] not in ("ok", "started")])
EOF
```

A source or a description problem is a source finding, not a layout finding. Anything that names Git, `candidate-history`, `stage_failed` or a push is a Part 1 finding: stop, and use the rollback.
Go on to Part 2 only after a night like this.

---

# Part 2: The SQLite backend

### 1. Pre-checks

On the commit that will be deployed, the whole test suite on SQLite, besides the default gate (`CODING_STANDARDS.md`, "The suite on both State Store backends"):

```bash
git fetch origin && git rev-parse origin/main
./scripts/check --state-backend sqlite
./scripts/check
```

Run them in a checkout at exactly that commit, on the development machine or on the server if its `.venv` has the dev dependencies (`.venv/bin/python -m pip install -e ".[dev]"`). Each takes about 2.5 minutes on 8 cores. Expect `[check] PASS: every step completed …` twice. Write the commit hash down; the server must pull exactly it.
If the gate fails, stop: do not cut over a commit whose suite is not green on SQLite.

On the server:

```bash
cd /mnt/data/workspace/auto-scout
env | grep AUTOSCOUT_STATE_ ; ls data/state-store 2>&1 | head -3 ; df -h data
git status --short
./scripts/doctor
```

Expect: no `AUTOSCOUT_STATE_` variable; `data/state-store` absent or empty (an old database there is a leftover: ask before you delete it); free disk space of at least a few hundred MB; nothing in `git status`; the doctor healthy or source-failure with «Хранилище состояния: JSON.».
If `AUTOSCOUT_STATE_BACKEND` is set, unset it where it is set (shell profile, `~/.config/autoscout/*.env`, a unit); leave `AUTOSCOUT_STATE_DATABASE` unset too, so the default path is used everywhere.

### 2. Stop the timers

"Stopping and starting the timers". Expect all `inactive`.

### 3. Pull the commit you checked

```bash
git pull --ff-only
git rev-parse HEAD
```

Expect the hash from step 1. If `HEAD` is another commit, `main` moved: go back to step 1 for the new commit.

### 4. Build the database

```bash
./scripts/state-database rebuild
```

Expect `state_database_rebuild=built`, `database=data/state-store/runs--current-weekly-pilot-russian.sqlite3`, `layout=sharded`, `record_count=2932` (your count), the `collection_counts.*` lines (candidate_history 2932, excluded_candidate_history 33, candidate_visibility 43, archived_editor_rows 1889, editorial_profile 18, candidate_identity_aliases 3130, source_proposals 51, ranking_freshness 2, latest_scouting_candidates 49 on the rehearsal copy), `database_bytes` about 26 MB, `change_seq=1`, `non_canonical=` empty and `seconds` of 3 to 4. A rebuild changes nothing for the commands: the state is still JSON until the cutover.

If it prints `state_database_rebuild=refused` (exit `2`), nothing was created. Read `error=`:

- `these state files are not in the form the State Store writes` (`non_canonical=` lists the files): do not add `--allow-non-canonical` on your own; a canonical file differing means someone edited a file. Report it.
- an invalid record, profile, alias table or shard is named: report it, do not edit the file.
- a refused database location (network share, cloud folder): the checkout is on the wrong disk. Do not override it with `AUTOSCOUT_STATE_DATABASE`.
- the database already exists: only a rerun of this step, so `./scripts/state-database rebuild --overwrite` is right only if the old database is a leftover of a rehearsal.

### 5. Verify the round trip

Three read-only looks. The variable is set only in front of those commands, not exported:

```bash
AUTOSCOUT_STATE_BACKEND=sqlite ./scripts/doctor | head -3
AUTOSCOUT_STATE_BACKEND=sqlite .venv/bin/python -m autoscout run-health \
  --output-dir runs/current-weekly-pilot-russian --state-dir runs/current-weekly-pilot-russian \
  --config configs/current-weekly-pilot-live.json | grep -E '^(state_|run_health_status)'; echo "exit=${PIPESTATUS[0]}"
./scripts/state-database export
git status --short
```

Expect:

- the doctor's second line «Хранилище состояния: SQLite (локальная база, схема 1); экспорт в Git актуален.»: the doctor compared every exported file with what the database would export, by content;
- `run-health`: `state_backend=sqlite`, `state_export=current`, `exit=0` (`run_health_status` says whether the last run's sources were ready, which is not this check);
- `export`: `state_database_export=unchanged`, `files_written=0`, `files_deleted=0`;
- nothing in `git status --short`.

If the doctor or `run-health` says `behind`, `changed_outside` or `differing` files: the files and the database disagree, so do not cut over. Look first at which files are named (`state_export_file=`), they should be none, and do not run `export --force` on your own. Report them and go back to the pre-checks. If `git status` lists files after the export, the export changed state: do not cut over; report the file names.

### 6. Cut over

```bash
./scripts/state-database cutover
git status --short
cat runs/current-weekly-pilot-russian/state-backend.json
```

Expect `state_database_cutover=cut_over`, `export=unchanged`, `seconds` about 2; `git status --short` shows only `?? runs/current-weekly-pilot-russian/state-backend.json`; the file holds `{"backend": "sqlite"}`.
If it refuses (exit `2`): a missing database (go back to step 4) or a directory changed since the last export (repeat step 5 and read the files it names). Nothing was written.

### 7. Commit and push the marker and any export changes

```bash
git add -A runs/current-weekly-pilot-russian
git commit -m "Cut the weekly state over to SQLite (#1077)"
git push
git status -sb | head -1
```

Expect `## main...origin/main`. Commit the whole state directory: the weekly preflight refuses a dirty tree, and the export may have rewritten files. If the push is rejected, stop and do not force; the server is the only writer, so something else pushed.

### 8. The doctor and run-health, now without the variable

```bash
./scripts/doctor | head -3
.venv/bin/python -m autoscout run-health --output-dir runs/current-weekly-pilot-russian \
  --state-dir runs/current-weekly-pilot-russian --config configs/current-weekly-pilot-live.json \
  | grep -E '^(state_|run_health_status)'; echo "exit=${PIPESTATUS[0]}"
```

Expect the doctor's line «Хранилище состояния: SQLite (локальная база, схема 1), задано отметкой в папке состояния; экспорт в Git актуален.» and a healthy or source-failure verdict; `run-health` prints `state_backend=sqlite`, `state_backend_selected_by=state_directory`, `state_database_path_class=local`, `state_schema_version=1`, `state_export=current` and exits `0`.
If the doctor says the state directory is on SQLite while JSON is selected («… а в настройках выбран JSON»), some variable or the config still selects JSON: remove it. If anything else, use the doctor table, or the rollback.

### 9. One scan, started by hand

Use the service the timer starts, so the scan has the same environment as a night (time zone, search adapter endpoint, mail settings, doctor afterwards):

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8899/health
PRE=$(git log -1 --format=%h -- runs/current-weekly-pilot-russian/run-summary.json); echo "last JSON-backend run: $PRE"
systemctl --user start --no-block autoscout-run.service
journalctl --user -fu autoscout-run.service
```

Expect `200` from the adapter (`docs/operator/run-server.md`, "Prerequisite: the grounded search adapter"); the commit `PRE` is the last JSON-backend run, to compare against. The journal follows the scan (Ctrl-C leaves it running); it takes anywhere up to two hours. It is over when `systemctl --user is-active autoscout-run.service` prints `inactive` and the journal ends with `=== doctor exit code`.
The same morning email arrives as after a night. (Starting `./scripts/run-weekly` in a terminal also works, but without the unit's environment it needs `AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT` exported by hand and it tries to open the review in a browser the server does not have, which makes the log end «attention-required».)
If the run stops, the morning-report subject and the journal give the stage and the reason token. `state_backend_conflict` (a JSON selection is set somewhere), `state_store_unavailable` (the database is missing at the default path), `state_export_failed` and `state_export_mismatch` are State Store findings: use the doctor table and, if its single action does not clear it, the rollback. Anything else is an ordinary weekly-run problem, to be handled as any night's.

Then check it:

```bash
.venv/bin/python - <<'EOF'
import json
log = json.load(open("runs/current-weekly-pilot-russian/workflow-log.json"))
for e in log:
    if e["stage"] in ("operation-start", "state-export", "terminal"):
        print(e["stage"], e["status"], json.dumps(e.get("detail", {}), ensure_ascii=False)[:240])
print("not ok:", [f'{e["stage"]}:{e["status"]}' for e in log if e["status"] not in ("ok", "started")])
EOF
git status -sb | head -1
git show --stat --format='%h %s' HEAD | tail -1
ls data/state-store
```

Expect: `operation-start ok` with `'state_backend': {'backend': 'sqlite', 'selected_by': 'state_directory'}`; `state-export ok` (the stage exists only under SQLite; `state-export failed` is the mismatch stop); `terminal completed/…` as on any night; no stage you did not see in the last JSON night; `## main...origin/main`; the run's commit touching book files and the usual run files, never a `.sqlite3` file and never `candidate-history.json`; `data/state-store` holding the one database (and its `-wal` and `-shm` files while it is open), no second database.

Compare with the last JSON-backend run. The numbers differ from night to night because the sources do; what should not change is the shape:

```bash
.venv/bin/python - "$PRE" <<'EOF'
import json, subprocess, sys
path = "runs/current-weekly-pilot-russian/run-summary.json"
before = json.loads(subprocess.run(["git", "show", f"{sys.argv[1]}:{path}"], capture_output=True, check=True, text=True).stdout)
after = json.load(open(path))
def row(s):
    d = s["description_coverage"]
    return {"run_date": s["run_date"], "raw_candidate_count": s["raw_candidate_count"],
            "editor_facing_count": s["editor_facing_count"], "update_delta_counts": s["update_delta_counts"],
            "failed_source_count": s["failed_source_count"], "skipped_source_count": s["skipped_source_count"],
            "descriptions_validated": f'{d["validated_count"]}/{d["total_candidates"]}'}
for key in row(after):
    print(f"{key:24} before={row(before)[key]!s:44} after={row(after)[key]}")
EOF
```

Expect the same order of magnitude in every row, a failed-source count and a set of sources that look like the last nights', and the description coverage no lower than before by more than the new books. A run that finds half the candidates, or hundreds more failed sources, is a source or Codex problem seen on the same day: look at the morning report and `source-runs.json`, as on any night. A State Store defect shows as a stopped run or a state commit that is wrong: books missing (compare the count of files under `candidate-history/` with before), `identity` or `editor_status` changes you did not make, an export that rewrote thousands of files. The last two are reasons for the rollback.

Then the doctor and run-health once more (step 8): the line must still say «экспорт в Git актуален», `run-health` exit `0`, `state_export=current`.

### 10. Start the timers

"Stopping and starting the timers". Expect the next starts at 01:30, 02:30 and 04:30.

### 11. The week on SQLite

For seven nights, in the morning:

| Check | Command | Expect |
|---|---|---|
| The report | the email | «успешно» or «требует внимания» for ordinary reasons; never a `state_*` reason |
| Pushed | `git status -sb \| head -1` | `## main...origin/main`, a clean tree |
| The doctor | `./scripts/doctor \| head -3` | the SQLite line ending «экспорт в Git актуален»; a healthy or source-failure verdict |
| run-health | the command in step 8 | exit `0`, `state_export=current`, `state_backend_selected_by=state_directory` |
| One database | `ls -la data/state-store` | the one database; no second `.sqlite3`; size around 26 MB and growing slowly |
| The editor's work | after a feedback session, the same doctor line | still «актуален» (each mutation exported, and her agent committed it) |

**Done** means: seven consecutive nights on SQLite, each with a pushed commit, a doctor line that says «актуален» and `run-health` green (exit `0` and `state_export=current`), no `state_*` stop and no rollback. Then comment on #1077 and close it.

### 12. Rollback of Part 2

When: a night stops with a `state_*` reason that the doctor's single action does not clear, and the database itself is healthy (it opens and holds the right books). `rollback` opens the database and exports its content first, so it is a change of backend, not a repair: it works only on a database that can be read, and it moves whatever the database holds into the JSON, wrong content included. A rollback costs at most one night; it can be done at any hour between two runs.

```bash
systemctl --user stop autoscout-run.timer autoscout-run-retry.timer autoscout-kirkus.timer
systemctl --user is-active autoscout-run.service autoscout-run-retry.service autoscout-kirkus.service
./scripts/state-database rollback
git status --short
git add -A runs/current-weekly-pilot-russian
git commit -m "Roll the weekly state back to the JSON backend (#1093)"
git push
./scripts/doctor | head -3
```

Expect `state_database_rollback=rolled_back` and `export=unchanged` (or `exported`, with the files it wrote); `git status --short` shows ` D runs/current-weekly-pilot-russian/state-backend.json` and, only if the database was ahead of the files, the changed state files; after the push the doctor says «Хранилище состояния: JSON.» and is healthy. Then start the timers. The JSON files hold everything the database held: the rollback writes the export first and removes the marker after.
- Exit `6`: a run is in progress; wait. Exit `2`: it refuses a directory changed outside the database since the last export. Read the error; only when the database is the state to keep, repeat with `./scripts/state-database rollback --force`, which writes the database over that change.
- `AUTOSCOUT_STATE_BACKEND` must not be left at `sqlite` anywhere, or commands that read it keep using the database, which is now stale.
- **When the database cannot be opened** (`database_unusable`, `schema_version_mismatch`, `other_state_directory`; `rollback` then refuses and leaves SQLite on): the last export in Git is the state, as of the last night that exported. Stop the timers, check that `git status --short` is clean and that `git log -3 --stat -- runs/current-weekly-pilot-russian` shows that export, then remove the marker by hand: `git rm runs/current-weekly-pilot-russian/state-backend.json`, commit, push, and run `./scripts/doctor`, which should say «Хранилище состояния: JSON.». This is what `rollback` does after its export; it was not rehearsed. Move the broken database aside rather than deleting it (`mv data/state-store/runs--current-weekly-pilot-russian.sqlite3 ~/`); a later cutover starts again with `rebuild`.
- **When the content is wrong** (books missing, decisions lost): do not roll back; that would copy the damage into the JSON. Find the last good commit of the state directory in Git (`git log --stat -- runs/current-weekly-pilot-russian`), restore it (`git checkout <commit> -- runs/current-weekly-pilot-russian`; remove files that commit did not have), then `./scripts/state-database rebuild --overwrite`, step 5 and a commit. Decide this with care; it discards what was recorded after that commit.
- Afterwards the database file stays under `data/state-store/` and goes stale with the first JSON write. To try again: `./scripts/state-database rebuild --overwrite`, then step 5 (verify) and step 6 (cutover). (Rehearsed: rollback, JSON writes, then `rebuild --overwrite`, `cutover` and a clean tree.)

---

# What the doctor says

`./scripts/doctor` (read-only) reports one State Store finding when the database and its export disagree. It shows it as the verdict only when nothing else needs attention first: with a dirty tree the verdict is a repository-blocker and the finding is in `./scripts/doctor --json`, under `facts.state_store`, and in `run-health --state-dir` (`state_export=…`, `state_export_file=…`, `state_export_command=…`). The commands act on the weekly state, on the server. `docs/operator/doctor.md`, "State Store (#1076)", is the full list.

| The doctor says (Russian verdict) | What it means | What to run |
|---|---|---|
| «Экспорт состояния в репозитории отстаёт от базы SQLite» (`behind`) | The database changed after the last export; the files are as that export left them. | `./scripts/state-database export`, then commit and push the files it wrote |
| «Экспорт состояния ещё не записывался …» (`never_exported`) | No export is recorded and the files differ from the database. | `./scripts/state-database export` |
| «Файлы состояния совпадают с версией в Git, но не совпадают с базой SQLite …» (`changed_outside`, `state_files_against_git=committed`) | The files are exactly what Git has committed, so they are the committed side and the database is older: a pull reached a checkout with an older database, on any machine. | `./scripts/state-database rebuild --overwrite`. The question says what it discards: data in the local database that is not in the files. Add `--allow-non-canonical` if a file is not in the form the store writes. |
| «Файлы состояния изменены вручную …» (`changed_outside`, `state_files_against_git=edited`) | A state file differs from what Git has committed: an uncommitted hand edit. | `git restore --source=HEAD --staged --worktree -- <the differing state files>` (the doctor prints the exact command, naming only the state files that differ, so the run artifacts beside them keep their changes; when the edit also added a new state file, which a restore cannot remove, it asks for the operator instead): the edit is lost, the committed state is untouched. Then run the doctor again: with the files equal to the commit it says nothing, or names `rebuild --overwrite` if the database is older. To keep the edit instead, run `./scripts/state-database rebuild --overwrite` yourself (it makes the database equal the edited files). |
| «Файлы состояния не совпадают с базой SQLite, а Git не позволяет определить, какая из сторон новее» (`changed_outside`, `state_files_against_git=unknown`) | Git could not answer: no repository, no commit yet, or Git failed. | No command: contact the operator. The question names what each choice discards: `export --force` the files, `rebuild --overwrite` the database. |
| «Папка состояния переведена на SQLite, но базы на этом компьютере нет» (`database_missing`) | The marker is there and the database is not (a fresh checkout, a machine other than the server, a deleted file). | `./scripts/state-database rebuild`. On the server, if the file was lost, the rebuild loses nothing: Git holds the export. |
| «Папка состояния переведена на SQLite, а в настройках выбран JSON» (`backend_conflict`) | `AUTOSCOUT_STATE_BACKEND=json` or `state_backend` in the weekly config. | Remove that selection. (`./scripts/state-database rollback` is the other way out, if you mean to go back to JSON.) |
| «В настройках указано неизвестное хранилище …», «База SQLite лежит там, где её нельзя хранить …», «Базу SQLite не удалось прочитать», «Схема базы SQLite не совпадает …», «База SQLite принадлежит другой папке состояния», «Отметка хранилища … повреждена», «Состояние не удалось сравнить …» | The store cannot be checked. | No command: stop and decide. A broken database on the server is rebuilt with `./scripts/state-database rebuild --overwrite` only after you have looked at why it broke; otherwise the rollback for a database that cannot be opened (step 12). |

When a weekly run stops at `state-export` (the report's reason `state_export_failed` or `state_export_mismatch`), nothing was committed and the working tree holds that night's results. Run `./scripts/doctor`; it names the one action. After it, the doctor's next verdict (an unfinished operation or an unsaved result) is handled as for any night (`docs/agents/contracts/doctor-repair-and-recovery.md`). This path was not rehearsed, because it needs a scan.

`./scripts/doctor --recover` never repairs a State Store finding: which side wins is a data decision.

# An optional local snapshot of the database

The database is rebuilt from the Git export in about 4 seconds, so it needs no backup of its own; the export is the off-site backup and holds every book, decision, profile entry and alias. A local snapshot only keeps the change counters and saves the rebuild. If you want one, never copy the file with `cp` while a run or a command may be using it: the database is in WAL mode, so the newest writes sit in the `-wal` file next to it, and a plain copy can miss them or be torn. Use SQLite's own online backup, which takes a consistent copy while others write:

```bash
mkdir -p ~/backups/autoscout
.venv/bin/python - <<'EOF'
import sqlite3, datetime
from pathlib import Path
src = Path("data/state-store/runs--current-weekly-pilot-russian.sqlite3")
dst = Path.home() / "backups/autoscout" / f"state-{datetime.date.today():%Y-%m-%d}.sqlite3"
source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
target = sqlite3.connect(dst)
source.backup(target)
print("integrity:", target.execute("PRAGMA integrity_check").fetchone()[0], dst)
target.close(); source.close()
EOF
```

Expect `integrity: ok` and the file's path (0.1 s, the size of the database). Keep it on a local disk, never a synced folder, and not under the repository.
To restore, with the timers stopped and no run active, replace the file at the default path and its siblings, then check:

```bash
D=data/state-store/runs--current-weekly-pilot-russian.sqlite3
rm -f $D $D-wal $D-shm && cp ~/backups/autoscout/state-YYYY-MM-DD.sqlite3 $D
./scripts/doctor | head -3
```

Expect «экспорт в Git актуален». If the snapshot is older than the last export, the doctor says `behind` or `changed_outside`. With a clean tree it names `./scripts/state-database rebuild --overwrite` for the second, which makes the database equal the committed export. Do not follow an `export --force` from any other source: it would overwrite the committed files with the older snapshot.
