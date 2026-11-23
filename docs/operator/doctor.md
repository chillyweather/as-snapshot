# Doctor diagnostics

Part of the [operator workflow](../current-weekly-operator-workflow.md).

## Doctor diagnostics (read-only)

The doctor command provides deterministic, read-only diagnostics that tell
the editor why AutoScout is blocked or what safe next action is available.

**Russian trigger phrases** (or semantically equivalent):

- «Проверь AutoScout»
- «Что случилось с поиском?»
- «В каком состоянии поиск?»

**Agent does**:

```
./scripts/doctor
```

The launcher (`scripts/doctor`) resolves the project interpreter
automatically using the same policy as `scripts/run-weekly` and
`scripts/check-sources`. It execs the canonical `autoscout doctor`
command with the correct config, output, and state directories relative
to the repository root.

### Read-only guarantees

The doctor command **never**:

- Writes weekly/editorial files or diagnostics.
- Removes locks.
- Fetches, pulls, commits, or pushes.
- Accesses the network.
- Invokes Codex or any model.

No elevated/sandbox permission is required for the default read-only check.

### Diagnosis classes

The doctor classifies the state into one of:

| Diagnosis | Meaning | Exit code |
|---|---|---|
| `healthy` | No problems; prior run completed or no run has happened yet. A completed run whose failed or skipped sources are all below the failure-streak threshold (#1125) is healthy; the verdict lists them under «Сбои, пока не требующие внимания» and names recoveries under «Снова работают». | 0 |
| `tool-incompatible` | Codex CLI is missing/incompatible and a resumable operation needs it. | 1 |
| `resumable-operation` | A safe same-operation retry is possible (validated current operation). | 2 |
| `source-failure` | Completed run has a source that failed 3 attempts in a row or more (the threshold, #1125), named first under «Не работают 3 и более попыток подряд»; or a run in which more than half of the attempted sources failed (at least two); or a problem of the Kirkus, PM or PW pass; or, for a run that recorded no failure streaks (made before #1125, or without a state directory), any failed or skipped source. | 2 |
| `artifact-failure` | Required artifacts are missing, malformed, or contain secret patterns. | 1 |
| `repository-blocker` | Repository is not in a safe state (branch, remote, ambiguous lock, dirty tree). A live lock is not a blocker — see `run-in-progress`. | 1 |
| `persistence-failure` | Results were computed but canonical persistence (commit/push) failed. | 2 |
| `completed-review-refresh` | Results are saved but the review HTML is missing or stale. | 2 |
| `state-store-finding` | The State Store's SQLite database and its JSON export disagree, or the store cannot be checked (#1076). Reported only when nothing else needs the editor first (the diagnosis would otherwise be `healthy`); any other diagnosis keeps its single action and carries the finding in its facts. See "State Store" below. | 1 |
| `run-in-progress` | Another AutoScout process owns the run right now. The verdict carries the live stage, counts, stage elapsed time against the stage budget, and the last snapshot time; a stale snapshot flags a possible hang. No escalation question. | 0 |

Exit code 0 = healthy or running, 2 = safely recoverable, 1 = blocked.

### Leftovers of a run that failed late (#1161)

What a run that failed late leaves in the run directory is not a `repository-blocker` when the next run will carry and publish it (the rule is in `docs/cli-reference.md`), whether it is still to be proven or the directory already awaits publication; the doctor judges it with the same rule, read-only.
The report then carries `facts.late_failure_leftovers` with `status: pending`, the earlier run's date and the number of carried paths, and the line «Результаты незавершённого поиска YYYY-MM-DD ещё не сохранены; ничего не удалено, их сохранит следующий поиск.»
When no run has logged anything since, the verdict itself says it: «AutoScout в порядке — поиск YYYY-MM-DD не успел сохранить результаты; следующий поиск сохранит их, ничего не удаляя.» (`healthy`, exit 0), instead of the verdict that the search has never run.
After a run carried them, the latest run's log has a `late-failure-leftovers` entry; the doctor reports `status: published` with «Сохранены и результаты незавершённого поиска YYYY-MM-DD; ничего не удалено.» when that run completed, `carried` otherwise.
A tree the rule cannot prove is still a `repository-blocker`, and so is a checkout the preflight would refuse anyway (another branch or remote, a merge in progress); the doctor then names no pending leftovers.

### State Store (#1076)

The doctor always adds one line naming the backend: `Хранилище состояния: JSON.`, or
`Хранилище состояния: SQLite (локальная база, схема 1); экспорт в Git актуален.` The database is named by its
path class (local, synced folder, network disk, or not determined), never by a path; the JSON report carries the
same class, the schema version and `export_status` under `facts.state_store`.

A state directory cut over to SQLite holds a committed `state-backend.json` marker, which decides the backend whatever the config or environment say; the doctor then adds «задано отметкой в папке состояния» to its line, and `run-health` prints `state_backend_selected_by=state_directory`.

The look is read-only on both backends. It opens no State Store transaction, never migrates a database, and
creates no database, WAL file or directory. It compares every exported file with what the database would export,
by content, so a change marker that moved without changing a record is not a mismatch. It is skipped while a run is
writing (`export_status: not_checked`), because the export is written late in a run.

A mismatch is one finding with one recommended action:

| `export_status` | Meaning | The one action |
|---|---|---|
| `behind` | The database changed after the last export; the files are as that export left them. | `./scripts/state-database export` |
| `never_exported` | No export is recorded and the files differ from the database. | `./scripts/state-database export` |
| `changed_outside` | The files were changed by something other than the export; a plain export refuses. The doctor compares the state files with Git's HEAD, read-only (Git objects only, no index refresh, no fetch), and never recommends `export --force`, which would write the database over committed files (#1098). | Files equal to HEAD (`state_files_against_git=committed`; a pull brought them, the database is older): `./scripts/state-database rebuild --overwrite` (the question says the local database's extra data is lost). Files that differ from HEAD (`edited`; a hand edit): `git restore --source=HEAD --staged --worktree -- <the differing state files>` (only the state files that differ, staged edits included; with a new file Git does not have, the operator is asked instead), the edit is lost and committed state is untouched, then check again. Git cannot say (`unknown`): contact the operator; the question names what `export --force` (the files) and `rebuild --overwrite` (the database) would each discard. |
| any, with `problem=database_missing` | The state directory holds JSON, or its marker says it is kept in SQLite, but there is no database on this machine. | `./scripts/state-database rebuild` |
| `problem=backend_conflict` | The directory's `state-backend.json` marker says SQLite, but `state_backend` or `AUTOSCOUT_STATE_BACKEND` selects JSON (#1093). | Drop the JSON selection from the config or environment (the other way out, `./scripts/state-database rollback`, moves the directory back to JSON). |
| any other `problem` (`backend_unknown`, `database_location`, `database_unusable`, `schema_version_mismatch`, `other_state_directory`, `state_unreadable`) | The store cannot be checked. | Contact the operator. |

The commands name the launchers, which act on the weekly state directory and config, the same ones `./scripts/doctor` inspects. A doctor started by hand on another state directory or config must be repaired with `autoscout state-database` on that directory, because the editor-facing output never prints a path.

`./scripts/doctor --recover` never offers to repair this: which side wins is a data decision.

`autoscout run-health --state-dir <dir> [--config <weekly config>]` makes the same comparison and prints
`state_backend=`, `state_database_path_class=`, `state_schema_version=`, `state_export=` and, for a mismatch,
`state_export_file=` (at most 20), `state_files_against_git=` (for `changed_outside`) and `state_export_command=` (the one command the doctor names, which may be `rebuild --overwrite` or `git restore`); a mismatch fails the command (exit 1) whatever the
run artifacts say. The weekly run's own health stage runs before the export, so the run checks the same thing
right after it writes the export (`state-export` in `workflow-log.json`, `reason=state_export_mismatch`, outcome
`intervention-required`) and stops before the credential scan and the Git envelope.

### Output

The default output is a concise Russian one-line verdict followed by a
machine-readable JSON report. With `--json`, only the JSON report is
emitted. Both contain the same verdict and recommended action. The output
never exposes secrets, absolute local paths, raw Git directives, stack
traces, or tool-control markup.

Repairs, resume execution, review regeneration, and Git publication are
separate authorized steps — the doctor only diagnoses.

### Local repairs (explicit selection only)

The doctor's `--repair` flag runs allowlisted, idempotent **local-only**
repair actions. Repairs never run by default — they must be explicitly
selected. There is no broad "fix everything" path.

**Available repairs:**

| Repair | Purpose |
|---|---|
| `remove-stale-lock` | Remove the workflow guard lock when its recorded PID is provably dead and the lock age exceeds the stale threshold (3600 s). |
| `rerun-health` | Re-verify run health from validated canonical inputs. `run_health` only reads and reports — it writes nothing — so a passing rerun always reports `already-complete` with no changed artifacts. |
| `rerender-review` | Re-render `weekly-review.md` and `weekly-review.html` from validated canonical inputs. |
| `open-review` | Open `weekly-review.html` in the default browser (must already exist and inputs must validate). |

**Usage:**

```
./scripts/doctor --repair rerun-health
./scripts/doctor --repair remove-stale-lock --repair rerender-review
```

Repairs are sorted into a canonical execution order regardless of CLI order:
`remove-stale-lock` → `rerun-health` → `rerender-review` → `open-review`.
Duplicates are deduplicated.

**Idempotency:** every repair distinguishes *changed* (action was applied
and had an effect), *already-complete* (action was applied earlier — no-op),
and *blocked* (preconditions not met — action skipped). Repeating a repair on
a fixed state never re-does work: `remove-stale-lock` reports
*already-complete* once the lock is gone, `rerun-health` always reports
*already-complete* because it only reads, and `rerender-review` reports
*already-complete* when the re-rendered bytes are identical. `open-review` is
the exception — opening the browser has an effect every time it succeeds, so
it reports *changed* on each successful invocation. It writes nothing, so
repeating it is still safe.

**Repair exit codes:** `1` when any repair was blocked or failed; otherwise
the exit code of the post-repair diagnosis (so a fully successful repair run
can exit `2` when the underlying state is still recoverable, e.g. a
`source-failure`). The exit-code table below describes the diagnosis and
recovery phases.

**Partial failure:** when a repair is blocked or failed, later repairs in the
plan are marked *not-executed* and the report records exactly one
recommended next action.

**Confirmation boundary:** repairs are local-only and safe, but they mutate
state (remove a lock, write review files, open a browser). In Codex they
require the single tool-level sandbox-permission request. The editor-facing
output follows the Level 3 contract: Russian one-line verdict per repair,
exactly one recommended action and one concise question when blocked.

**Evidence:** structured repair evidence is stdout JSON only — repairs
never persist diagnostics to disk.

### Authorized recovery (`doctor --recover`) (#319)

When the doctor diagnosis maps to a consequential recovery action, the
`--recover` flag orchestrates the recovery in a distinct authorized phase.
Diagnosis (read-only) and recovery remain separated — nothing executes
without explicit confirmation.

**Russian trigger phrases:**

- «Почини AutoScout»
- «Восстанови поиск»
- «Продолжи прерванный поиск»
- «Проверь и почини AutoScout»

**Two-phase flow:**

1. `./scripts/doctor --recover` — read-only diagnosis + recovery plan
   preview. Exit code 3, zero mutation.
2. `./scripts/doctor --recover --confirmed-by-editor` — apply the plan
   after explicit editor confirmation.

**Diagnosis → recovery mapping:**

| Diagnosis | Recovery action | Side effects |
|---|---|---|
| `resumable-operation` | Resume weekly workflow (#315 validated same-operation resume, no recapture) | network, model, git-publication |
| `persistence-failure`, operation still current | Resume weekly workflow (re-finalize results, no recapture) | network, model, git-publication |
| `persistence-failure`, operation stale | Blocked (Level 3) — resuming would recapture sources, so the escalation is manual publication | — |
| `completed-review-refresh` | Complete locally: rerun-health → rerender-review → open-review | local-files, browser |
| `source-failure` | None — editorial follow-up (blessed source check) is separate | — |
| `healthy` | None — no recovery needed | — |
| `tool-incompatible` | Blocked (Level 3) — never auto-update Codex; an outdated install is reported distinctly from a missing one | — |
| `repository-blocker` | Blocked (Level 3) — conflicts, dirty files, wrong remote, ambiguous lock ownership | — |
| `artifact-failure` | Blocked (Level 3) — corruption risk, including a completed run with missing or malformed artifacts | — |
| `run-in-progress` | None — a live run owns the workflow; wait for it to finish | — |

`persistence-failure` requires a completed terminal outcome *and* a failed
latest `repository-finalize`. The workflow does not currently persist that
combination — a failed finalize routes through `_bad_outcome`, which writes
`terminal: intervention-required`, so a genuine publication failure is
diagnosed `resumable-operation` and recovered by the same resume that
re-finalizes without recapture. The `persistence-failure` branch is therefore
defensive: retained so a publication failure is classified correctly if the
workflow ever does persist it, not a routinely exercised path.

Persistence is judged from the **latest** `repository-finalize` entry only.
A validated same-operation retry carries the previous attempt's entries
forward (#315 prior-evidence preservation), so a successfully published retry
has a failed finalize sitting beside its successful one in the same
operation's log; requiring every entry to be `ok` would report that run as
unsaved and offer a republish.

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | Healthy, running, or completed recovery |
| 1 | Blocked or intervention required |
| 2 | Recoverable (results available, editor action needed) |
| 3 | Confirmation required (preview shown, zero mutation) |

**Flag guards.** Two flag combinations are rejected before anything runs,
printing a single `error=` line and exiting `2`:

| Invocation | Error |
|---|---|
| `--confirmed-by-editor` without `--recover` | `error=confirmed-by-editor-requires-recover` |
| `--recover` together with `--repair` | `error=recover-and-repair-are-distinct-phases` |

Exit code `2` is therefore overloaded — it means both "recoverable diagnosis"
and "rejected flag combination". Anything scripting the launcher must read the
`error=` line (or the absence of a JSON report) to tell them apart, not the
exit code alone.

**Safety boundaries:**
- Read-only doctor never mutates — the `--recover` phase is distinct.
- Resume reuses the canonical weekly workflow and repository envelope
  without bypassing validation, guard, or finalization.
- Resume requires an operation that is both still current **and**
  unfinished. The resume marker is written when capture completes and is
  never removed on success, so a valid marker alone does not mean the run
  was interrupted — a completed run is diagnosed on its own merits
  (healthy / review refresh / persistence) instead of being offered a
  re-run. A stale operation escalates rather than resuming, because the
  workflow only skips recapture while the capture is current.
- Completed runs finish health/review/open/persistence idempotently.
- Blocked diagnoses remain Level 3: exactly one recommended action and
  one question; never auto-update Codex or any external app.
- «Результаты сохранены» is claimed only from structured
  repository-finalize evidence — never inferred from a completed outcome.
- Diagnostics validate the canonical branch *and* remote, so a clean
  checkout pointing at the wrong remote is not reported safe.
- `--json` emits machine-readable output only: progress and started
  messages are suppressed so the stream stays parseable.

---
