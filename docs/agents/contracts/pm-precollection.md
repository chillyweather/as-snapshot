# PM deal-email pre-collection (Issue #946)

Publishers Marketplace deals reach AutoScout as the member's "Today's Deals" emails in a dedicated mailbox, read by their own pass.
`./scripts/run-weekly` starts that pass itself before the source check, on every weekly run, so a weekly run needs no separate PM step.
The pass opens the PM mail folder read-only, may spend at most 5 minutes, and reports through the shared heartbeat as `Сбор PM: прочитано писем N.`, at most once per 60 seconds and with no titles.
During the pass, `./scripts/doctor` reports it as `run-in-progress` with its counts, elapsed time against its budget and the last snapshot time, with no escalation question.
When idle, `./scripts/doctor` also reports the last PM pass: its status, date and counts, and the size of the last result.
No outcome of the pass stops the run: a failed or cut-short pass is reported for PM alone, by name.
Deals emails that parse into no deal at all are a named failure (`pm_no_deals_parsed`), not an empty success; a week with emails but no new deal, or with no new email, is reported as collected.
Only one PM pass runs at a time: a second pass refuses with `pm_pass_in_progress` and writes nothing.
When the operator or the editor asks to collect or refresh PM deals outside a weekly run, use:

```
./scripts/collect-pm-deals
```

It reads the mailbox over the network, so in Codex the single tool-level sandbox-permission request is attached to the full `./scripts/collect-pm-deals` invocation, with this Russian rationale:

«Для обновления данных Publishers Marketplace нужен сетевой доступ. Разрешить запуск?»

It writes only under the Git-ignored `data/pm-precollection/` and prints counts with no titles.
It never prints or stores the mailbox address or password; they come from the environment or the operator's key file outside the repository.

The deal logline leaves the Git-ignored pass result only for the PM logline descriptions of the weekly run (#950): to the pinned Codex description model, and, when that call fails and `pm_logline_deepseek_fallback` is on, to the DeepSeek API.
The operator confirmed on 2026-11-12 that PM deal text may be sent to DeepSeek when necessary.
The Claude API model backend (#1149) is neither: it refuses, before sending anything, any request that carries a deal logline, so with the description pin on that backend the PM logline stage sends no logline anywhere, reports each book as `pm_text_not_permitted`, and does not turn to the DeepSeek fallback, whose precondition is a failed Codex call.
Only the title, the authors and the logline of one deal are sent, and nothing but the logline's digest and the Russian description is kept.
The DeepSeek key comes from the environment or the operator's key file outside the repository and is never printed or stored.
The mailbox setup and the procedure are in `docs/operator/pre-collection.md`.
