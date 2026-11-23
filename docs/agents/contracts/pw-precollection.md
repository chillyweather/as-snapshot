# PW issue pre-collection (Issue #956)

Publishers Weekly reviews reach AutoScout as the subscriber's weekly Digital Library issue, announced by email in the `PW` label of the AutoScout mailbox and read by their own pass.
`./scripts/run-weekly` starts that pass itself before the source check, on every weekly run, so a weekly run needs no separate PW step.
The pass reports through the shared heartbeat as `Сбор PW: обработано выпусков N.`, at most once per 60 seconds and with no titles, and starts no new issue after 15 minutes.
During the pass, `./scripts/doctor` reports it as `run-in-progress` with its counts, elapsed time against its budget and the last snapshot time, with no escalation question; when idle, it reports the last PW pass.
No outcome of the pass stops the run: a failed or partial pass is reported for PW alone, by name.
A week with no new issue is not a failure, and opens no subscriber link; an issue with no Nonfiction review is the named failure `pw_zero_nonfiction_rows`.
When PW refuses the subscriber link (`pw_subscriber_link_rejected`, usually its device session limit), the pass stops at once; tell the editor in Russian to open the Digital Library from the PW email and verify the account. Never automate that verification or work around the refusal.
When the operator or the editor asks to collect or refresh PW outside a weekly run, use:

```
./scripts/collect-pw-issue
```

It reads the mailbox and downloads the issue over the network, so in Codex the single tool-level sandbox-permission request is attached to the full `./scripts/collect-pw-issue` invocation, with this Russian rationale:

«Для обновления данных Publishers Weekly нужен сетевой доступ. Разрешить запуск?»

The editor's own forwarding address is read from `AUTOSCOUT_PW_FORWARDING_SENDER` (the environment, or the PM mailbox key file) and is never written to the repository; without it only the configured vendor sender is read.

PW's written permission (#935) binds every use: no PDF or review text is republished, committed or shared outside the organisation, nothing is used for training or fine-tuning, and only the subscription's own credentials are used.
One exception stands, confirmed by the operator on 2026-11-11 as permitted by PW: `./scripts/describe-pw-pdfs --provider deepseek --allow-review-text-upload` sends review text from the local PDF archive to the DeepSeek API to draft descriptions.
The exception covers that command alone; no other pass, model provider or tool may receive PDF or review text on its strength.
The Claude API model backend (#1149) refuses, before sending anything, any request whose prompt or files mention `publishersweekly.com`, so no stage moved to it can pass PW text to that provider.
The pass writes only under the Git-ignored `data/pw-precollection/`, deletes each PDF after reading it, and prints counts with no titles.
The procedure is in `docs/operator/pre-collection.md`.
