# Escalation, editor authority and the handoff to Dima

Part of the [operator workflow](../current-weekly-operator-workflow.md).

## Three-Level Learning, Technical-Request, and Escalation Policy (#248)

Every editorial interaction is classified into one of three levels.
This policy layers on top of the mandatory two-phase confirmation protocol
and auto-completion rules ([one-request path](one-request-path.md#auto-completion-requirements)) — it does not replace them.

### Level 1 — Learning (preview → confirm → acknowledge)

Learning covers per-candidate feedback (`record-feedback`) and standing
Editorial Taste Profile entries (`editorial-profile add`).

**Decision rule:** the agent may prepare learning inferences automatically
from conversational context, but every mutation requires preview (code 3,
zero mutation) then explicit editor confirmation.

**After each confirmed learning action**, the agent presents a summary
under the exact heading:

```
Что AutoScout узнал
```

The summary preserves verbatim editor wording and describes:
- What was recorded (candidate, status, or profile rule).
- The concrete future effect (e.g. "Книга 'Deep House' отклонена",
  "Автор E.N. Anonymous скрыт — правило применено к текущему списку").

Routine safe follow-up (rerender, persist, push) continues in the same
request without another confirmation.

**Example:**

> **Что AutoScout узнал**
>
> — «Сомнительное происхождение — отклоняем» → книга «Deep House»
>   (E.N. Anonymous) отклонена (reject).
> — «Никогда. Категорически нет.» → создано жёсткое правило: скрывать
>   книги автора E.N. Anonymous. Затронута 1 книга в текущем списке.

### Level 2 — Explicit technical request (issue filing)

**Decision rule:** an explicit editor request for code, config, source,
workflow, report, field, command, or integration changes authorises
creating a focused `editor-request` issue **without redundant approval**.
The agent must NOT implement it incidentally during weekly review.

An ambiguous or suggestive technical remark does NOT authorise an issue.
Instead, the agent asks exactly one concise clarification.

Editorial preferences (taste, priority, hiding) stay at Level 1 — they
never become technical issues.

**Level 1 + Level 2 combined statement precedence:** when an editor
statement combines Level 1 feedback with a Level 2 technical request,
Level 1 takes precedence: the agent does not file an issue while
feedback confirmation is pending. Once the Level 1 confirmation boundary
is resolved, the explicitly requested technical issue may be filed
independently without redundant approval.

**Examples:**

| Editor says | Agent action |
|---|---|
| «Добавь в отчёт колонку с датой первой публикации» | File `editor-request` issue; do NOT implement during review |
| «Неплохо бы когда-нибудь добавить французские источники» | Do NOT file an issue; ask one clarification ("Вы хотите, чтобы я создал задачу на добавление французских источников?") |
| «Этот автор мне неинтересен» | Level 1 — editorial preference, not a technical issue |
| «Отклоняю эту книгу и добавь фильтр по годам» | Level 1 takes precedence: preview feedback first, defer issue filing until after confirmation |

### Level 3 — Escalation (stop before unsafe/further mutation)

**Decision rule:** the agent stops before the unsafe or consequential
mutation (or before any further mutation once a conflict or rejected
push is discovered) when facing:
- State or data corruption risk
- Git conflict or rejected push
- Security or privacy concern
- Repeated source failures indicating systemic breakage
- Broad ambiguity (editor intent cannot be narrowed with one question)
- Destructive or irreversible action
- Identity merges (preserve the existing `--dry-run` boundary)

**For all Level 3 situations**, the agent:
1. Preserves sanitized diagnostics when applicable (normal editor output
   does not expose raw diagnostic file paths).
2. Explains the situation in plain Russian.
3. Gives **exactly one** recommended next action.
4. Asks **exactly one** minimum-confirmation question.

**Example:**

> «Источник NPR не отвечает третью неделю подряд. Диагностика
> сохранена. Рекомендую связаться с Димой для диагностики и
> возможного отключения источника. Связаться с Димой?»

The identity-merge procedure in "Identity correction as a confirmation
boundary" ([one-request path](one-request-path.md#identity-correction-as-a-confirmation-boundary)) is the canonical Level 3 identity-merge rule — this
section references it rather than duplicating it.

---

## Diagnostics And Dima Handoff

### Launcher interpreter auto-resolution

The blessed launcher (`scripts/run-weekly`) resolves a usable Python
interpreter automatically: it prefers the project-local `.venv/bin/python`
when executable, and falls back to the host `python3`. This means the
agent does not need to know which interpreter the project uses or
whether `python` is on PATH. When diagnosing a launch failure, check:

- Is `.venv/bin/python` present and executable?
- Is `python3` on the host PATH?

If neither is found the launcher fails with a concise message naming both
checked locations. The operator should install or restore the project
virtualenv (e.g. `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`)
and retry.

### Codex sandbox permission pre-acquisition

In Codex, the editor's weekly-search intent (e.g. «Запусти еженедельный
поиск книг») authorizes the workflow path but is **not** sandbox
authorization. The first operational action must be a single tool-level
elevated/sandbox-permission request attached to the full
`./scripts/run-weekly` invocation — no separate chat-only confirmation,
no unprivileged launcher attempt, no shell preflight, and no Git probe
beforehand. Attach one concise Russian rationale to the tool call:

«Для безопасного обновления данных и сохранения результатов нужен
расширенный доступ. Разрешить запуск?»

If sandbox permission is denied, the workflow must not run and the agent
stops cleanly without retrying. Normal editor output must not expose raw
Git paths or recovery mechanics; failed-run diagnostics remain preserved
in `workflow-log.json`.

### Diagnostic starting point

Start diagnostics from `pilot-status.json`, then inspect `run-summary.json`,
`source-runs.json`, `workflow-log.json`, and relevant queue/exclusion artifacts.
Classify the issue honestly:

- **Source scarcity**: pipeline ran, but configured open sources did not
  provide enough eligible Scouting Candidates. This is not an app crash.
- **Skipped or failed source health**: `source-runs.json` shows paid, blocked,
  fragile, unavailable, extraction, or validation failures for specific
  configured sources.
- **Withheld source**: `source-runs.json` reports `failed` with the reason
  `Codex extraction produced no artifact for this configured source`.
  The source was configured and captured, but extraction produced nothing, so
  `finalize-codex-run` withheld it from the generated replay config.
  It is recorded there as `active: false` plus
  `inactive_reason: extraction-artifact-missing`, which is what separates it from
  a source the operator disabled deliberately.
  A withheld source counts toward `failed_source_count`, turns
  `codex_workflow_health` to `failed`, and appears in `major_warnings`.
  Read `workflow-log.json` for the codex-stage diagnostics to see whether the
  cause was the per-attempt timeout or the portfolio budget.
  Never read a withheld source as an operator decision.
- **Refused artifact**: `source-runs.json` reports `failed` with the reason
  `Codex extraction produced an artifact that failed validation for this
  configured source`, followed by the validation class and how many extracted
  mentions the refusal discarded.
  This is not the case above. Extraction ran and returned grounded work, and
  validation refused the artifact, so the fix is in the artifact rather than in
  capture or in the source website.
  It is recorded in the replay config as `inactive_reason:
  extraction-artifact-rejected` plus a `withheld_detail` clause.
  `finalize-codex-run` lists it under `rejected_artifact=`, never under
  `pending_artifact=`. Saving the same bytes under the same filename earns the
  same refusal, so re-saving is not the next step here.
  The refused bytes are preserved under
  `$TMPDIR/autoscout-rejected-artifacts/<digest>/`, and the codex-stage
  diagnostics in `workflow-log.json` name the same class in `diagnostic_class`
  with the discarded count in `raw_extracted_count`.
  An `identity_mismatch` class means the artifact omitted or altered one of the
  four envelope fields described in [Codex Capture and Replay Path](capture-and-extraction.md#codex-capture-and-replay-path).
- **Validation/drop problem**: `source-mentions.json`, `scouting-candidates.json`,
  `internal-exclusions.json`, or `russian-review-queue.csv` explains candidates
  being dropped or queued.
- **Git problem**: branch, pull, conflict, dirty state, or push failure blocks
  safe finalization.
- **Pipeline failure**: command exits non-zero, required artifacts are missing,
  or artifacts contradict expected invariants.

Diagnostic package for Dima:
- Commands run and exit status.
- Short terminal summary, not full noisy logs unless requested.
- Branch, remote tracking, and `git status` summary.
- Changed files, with likely relevant versus unrelated changes separated.
- Key artifact paths inspected.
- Failing or skipped source names from `source-runs.json`.
- Observed behavior, expected behavior, and why this is source scarcity,
  source health, validation/drop, git, or pipeline failure.
- Suggested next action.

It is safe to commit and push broken state for Dima only when the artifacts
are useful for diagnosis, the commit message says it is diagnostic/broken
state, and no secrets or unrelated user changes are included.

---

## Editor Authority And Informed Consent

ADR 0008 governs agent behavior: the editor is the authority; Dima is the async escape hatch, not an approver for routine editorial or configuration decisions.

**Routine actions** can be applied directly after normal confirmation:
- Recording candidate feedback.
- Rendering review.
- Explaining diagnostics.
- Recording Source Proposals.
- Applying clearly safe configuration updates.

**Risky or consequential actions** require explaining consequences and
offering concrete options before acting:
- Disabling, removing, or demoting configured sources.
- Changing row targets or readiness thresholds.
- Changing risk-screening behavior.
- Changing Russian-edition logic.
- Deleting Candidate History, Excluded Candidate History, pending feedback,
  memo queue, source proposals, or evidence.
- Removing evidence links or Evidence Notes.
- Broadening discovery beyond configured sources.

**Refuse workflow/app-breaking requests** and offer a safe alternative:
- Destroying git history or using destructive recovery commands.
- Deleting the weekly state so rejects are forgotten.
- Creating Scouting Candidates from topic search or unaccepted Source Proposals.
- Fabricating rows, sources, evidence, or copied source bodies.
- Bypassing validation to make a broken report look ready.

---
