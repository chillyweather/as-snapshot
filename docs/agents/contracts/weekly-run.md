# Weekly run: launcher, permission, heartbeat and passes

`AGENTS.md` carries the weekly launch complete in short form, so the launch needs no read before it.
Read this file during a run: it is the full text the launch was shortened from, with the interpreter resolution, the heartbeat format and the passes.

## Blessed weekly-launch path

When the editor issues a clear Russian weekly-search intent (e.g.
«Запусти еженедельный поиск книг», «Запусти поиск», «Ищи книги»),
the agent MUST use the blessed repository launcher:

```
./scripts/run-weekly
```

The launcher resolves a usable project Python interpreter automatically
(prefers the project-local `.venv/bin/python`, falls back to `python3`)
and execs the canonical `autoscout run-weekly` command with the correct
config, output, and state directories relative to the repository root.
The agent must NOT attempt a raw `python -m autoscout run-weekly ...`
invocation first or expose interpreter selection details to the editor.

## Codex sandbox permission acquisition

The editor's Russian weekly-search intent (e.g. «Запусти еженедельный
поиск книг») authorizes the workflow path but is **not** sandbox
authorization.

The first operational action the agent takes MUST be a single
tool-level elevated/sandbox-permission request attached to the full
`./scripts/run-weekly` invocation. The Codex UI approval gates command
execution — there is no separate chat-only confirmation step,
no unprivileged launcher attempt, no shell preflight, and no Git probe
beforehand.

Attach one concise Russian rationale to the tool invocation (retain
the established editor-facing wording):

«Для безопасного обновления данных и сохранения результатов нужен
расширенный доступ. Разрешить запуск?»

The permission request and the `./scripts/run-weekly` command are a
single tool call.

If sandbox permission is denied, the workflow must not run. The agent
stops cleanly without retrying and without exposing raw Git paths or
recovery mechanics. Failed-run diagnostics remain preserved in
`workflow-log.json` and are available for inspection when the editor
asks.

## Watching a long weekly run

The description stages are the longest part of the run and report
progress through the same heartbeat as capture and extraction: at most
one Russian line per 60 seconds with finished / running / not-started
counts plus the running candidates by title
(e.g. `Проверка описаний: готово 40, выполняется 3, ожидают 98.`).

- Do NOT end the turn while `./scripts/run-weekly` is still running.
  Stay with the run until it exits.
- Relay only NEW heartbeat lines. Never fill the chat with
  "process still alive" messages that contain no numbers.
- For status questions during a run (e.g. «какое состояние
  процесса?»), run `./scripts/doctor` and relay its `run-in-progress`
  verdict: stage, counts, stage elapsed time against the stage budget,
  and the last snapshot time. A live run is reported as running, not
  as blocked, and carries no escalation question.

## Passes inside a weekly run

`./scripts/run-weekly` starts the Kirkus, PM and PW pre-collection passes itself (Kirkus only when no Kirkus pass ran that day, such as the server's 01:30 pass), fills missing ISBNs, records original editions and the work age, and, when switched on, Russian editions, so none needs a separate step.
Their heartbeat lines (`Сбор Kirkus: запрошено страниц N.`, `Сбор PM: прочитано писем N.`, `Сбор PW: обработано выпусков N.`, `Поиск русских изданий: проверено N, найдено M.`) are relayed like any other.
No outcome of a pass stops the run: a failed, partial, stale or missing result is reported for that source alone, by name.
When PW refuses the subscriber link (`pw_subscriber_link_rejected`), tell the editor in Russian to open the Digital Library from the PW email and verify the account. Never automate that verification or work around the refusal.
Each pass's full contract is in its file under `docs/agents/contracts/`.
