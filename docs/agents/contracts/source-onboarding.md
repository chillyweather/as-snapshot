# URL source onboarding

When the editor clearly requests adding or onboarding one configured URL source, start with `./scripts/add-source probe` and this exact Russian network rationale: «Для проверки доступности предложенных источников нужен сетевой доступ. Разрешить запуск?»
The `add-source probe` mode delegates to `./scripts/probe-source`.
The initial probe command and its permission request are one tool call.
The probe is the only network step and the workflow must stop cleanly on reject or defer with its recorded reason and one concrete action.
After an accepted probe and completed evidence are available, use `./scripts/add-source` for every offline preview and confirmation call instead of a raw Python command.
The unconfirmed `./scripts/add-source` preview is offline and has zero filesystem, Git, network, and model mutation.
The preview also agrees the retained helper metadata sidecar with the selected representative entries.
Replay evidence uses at most three representative entries, and the preview returns them in `representative_entries`.
A stale, missing, extra, duplicated, reordered, or mismatched sidecar entry fails the preview with exit code 1, a specific `reason_code`, and one concrete `next_action`.
Correct the sidecar with the reported `representative_entries` and repeat the preview.
The editor is never asked to confirm helper metadata that the confirmed operation must reject.
Only a separate explicit editor confirmation may authorize the mutating `./scripts/add-source` invocation.
The confirmed add-source command and its permission request are one tool call.
Reuse the exact preview payload, explicit run date, artifact identity, and confirmation token for that confirmed call.
The artifact identity includes the canonical proposal-state path, deterministic presence flag, and SHA-256 digest when the proposal file is present.
Request one concise Russian tool rationale for that invocation: «Для безопасного добавления источника и сохранения результатов нужен расширенный доступ. Разрешить запуск?»
The confirmed launcher adds no probe, network, model, or push behavior and can create one guarded local commit after validation.
That commit contains only the accepted proposal, four configs, replay fixtures, the helper sidecar, feasibility evidence, explicitly supplied source-specific test, and explicitly declared onboarding artifacts.
An onboarding artifact is a dirty file under `tests/` that the new source must change, such as a portfolio-composition pin; declare each one with a repeatable `--onboarding-artifact` in the preview and in the confirmed call (#664).
It excludes probe evidence, captures, ledgers, and feasibility drafts.
Follow the canonical checklist in `docs/source-onboarding-runbook.md`: feasibility evidence → Source Proposal gate → portfolio/overlay configuration → replay fixtures before any live collection → validation (per-source contract test plus full suite) → probation lifecycle entry → single commit with the evidence doc.
Sources are onboarded one at a time; a source that fails a gate is recorded as rejected/deferred with evidence, not worked around.
