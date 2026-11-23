# Doctor repairs and weekly recovery

This file covers read-only diagnosis with `./scripts/doctor` and what follows it.

## Read-only doctor diagnostics

When the editor asks why AutoScout is blocked or what can be done next
(e.g. «Проверь AutoScout», «Что случилось с поиском?», «В каком
состоянии поиск?»), the agent MUST use the blessed launcher:

```
./scripts/doctor
```

The doctor command is **read-only**: it never writes weekly/editorial
files, removes locks, fetches/pulls/commits/pushes, accesses the network,
or invokes Codex or any model. No elevated/sandbox permission is needed
for the default read-only check.

The agent relays the result as a one-line Russian verdict followed by
exactly one recommended action and one concise question when blocked.
Repairs, resume execution, review regeneration, and Git publication are
separate authorized steps — the doctor only diagnoses, it does not fix.

The doctor also names the State Store backend in one line, and under SQLite a database that disagrees with its JSON export is one finding (`state-store-finding`) with exactly one recommended command; it creates and changes nothing on either backend (#1076).

## Local doctor repairs (explicit request only)

Repairs run ONLY on explicit editor request via `autoscout doctor --repair
<name>` (repeatable). Four local-only repairs are available:

| Repair | Russian purpose |
|---|---|
| `remove-stale-lock` | Удаление устаревшей блокировки процесса |
| `rerun-health` | Перезапуск проверки здоровья артефактов |
| `rerender-review` | Пересборка файлов обзора |
| `open-review` | Открытие обзора в браузере |

**Local-only boundary:** repairs never resume collection, invoke Codex/a
model, fetch/pull/commit/push, alter configuration/candidate-history/
feedback/visibility/Editorial Taste Profile state, or write to
workflow-log.json. Allowed mutations: deleting a provably-dead stale lock,
writing derived weekly-review.md / weekly-review.html, opening the browser.

**Russian trigger examples:**

- «Удали устаревшую блокировку»
- «Обнови обзор»
- «Пересобери обзор»
- «Открой обзор»

The files a run that failed early leaves modified need no repair (#1057). When the dirty paths are exactly the unstaged run log alone, or with an unstaged `run-summary.json` of the same directory that differs from `HEAD` only in its top-level `toolchain_preflight` key (what a Stage 0 failure writes), the next weekly run copies them to the Git-ignored `data/workflow-log-residue/` and restores them before its own repository check, and the doctor does not report them as a repository blocker. Anything else, staged or changed elsewhere, still blocks.

The leftovers of a run that failed late need no repair either (#1161). When the next weekly run can prove the dirty files are that run's (the rule is in `docs/cli-reference.md`), it carries them and publishes them with its own results, restoring and removing nothing; a file someone dropped into the run directory is committed too. The doctor then reports `late_failure_leftovers` with status `pending` and the line «Результаты незавершённого поиска … ещё не сохранены; ничего не удалено, их сохранит следующий поиск», not a repository blocker. Do not restore, remove or commit those files by hand, and do not delete the marker under `data/late-failure-awaiting-publication/`. A tree the rule cannot prove stays a repository blocker.

In Codex, mutating repairs need the single tool-level sandbox-permission
request with a concise Russian rationale («Для безопасного ремонта
нужен расширенный доступ. Разрешить запуск?»).

## Authorized weekly recovery (doctor --recover) (#319)

When the editor asks to repair or recover AutoScout after a diagnosis,
the agent orchestrates the one-step consequential recovery through the
blessed doctor launcher with `--recover`.

**Russian triggers:**
- «Почини AutoScout»
- «Восстанови поиск»
- «Продолжи прерванный поиск»
- «Проверь и почини AutoScout»

**Flow:**
1. Run read-only `./scripts/doctor` first to diagnose the state.
2. When the diagnosis maps to a consequential recovery
   (`resumable-operation`, `persistence-failure`, or
   `completed-review-refresh`), run `./scripts/doctor --recover` —
   this computes the recovery plan, prints a preview, and returns
   exit code 3 with **zero mutation** (no network, model, Git, or
   file side effects).
3. Present the single Russian confirmation question from the plan.
4. Only after explicit editor confirmation, run
   `./scripts/doctor --recover --confirmed-by-editor` with the single
   tool-level sandbox-permission request attached and the Russian
   rationale:

   «Для безопасного восстановления данных и сохранения результатов
   нужен расширенный доступ. Разрешить запуск?»

   Permission and command are one tool call — same pattern as the
   weekly-launch path.

**Guarantees to state:**
- The read-only doctor never crosses into network/model/Git side
  effects.
- Resume reuses the canonical weekly workflow (#315 validated
  same-operation resume — no recapture) and the repository envelope
  (validation, guard, finalization, output) without duplicating or
  bypassing them.
- Completed runs finish health/review/open/persistence idempotently.
- Blocked diagnoses (`tool-incompatible`, `repository-blocker`
  including conflicts/unrelated dirty files/rejected pushes,
  `artifact-failure`, changed/stale operation identity) remain
  Level 3 — exactly one recommended action and one question; never
  auto-update Codex or any external app.
- «Результаты сохранены» is claimed only from structured
  repository-finalize evidence.
