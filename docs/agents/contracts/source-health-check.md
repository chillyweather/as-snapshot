# Blessed source health check

When the editor asks whether the sources are working
(e.g. «Проверь источники», «Источники в порядке?», "check the
sources"), the agent MUST use the blessed launcher:

```
./scripts/check-sources
```

Like the weekly launcher, it resolves the project interpreter
automatically; the agent must NOT expose raw `python` invocations. The
probe fetches source pages live, so in Codex the single tool-level
sandbox-permission request is attached to the full
`./scripts/check-sources` invocation, with a concise Russian rationale:

«Для проверки доступности источников нужен сетевой доступ. Разрешить
запуск?»

The check is read-only diagnostics: it never modifies weekly state,
candidate history, configuration, or Git, and it does not trigger the
weekly workflow, auto-completion, or the editor-feedback protocol. The
agent relays the result as a one-line Russian summary («Все N
источников доступны» or «Недоступны: <names>») followed by the
technical verdict table for reference. A failed source here is a
source-health finding to report, not something to silently repair.
