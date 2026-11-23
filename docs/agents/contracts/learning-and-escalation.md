# Three-level learning, technical-request, and escalation policy (#248)

`AGENTS.md` states each level and its boundary; this file is the full policy.

Every editorial interaction falls into one of three levels.

## Level 1 — Learning (preview, confirm, acknowledge)

Learning is the normal conversational feedback path: per-candidate status
(`record-feedback`) and standing Editorial Taste Profile entries
(`editorial-profile add`). The agent may prepare learning inferences
automatically from conversational context, but **every mutation requires**
the existing two-phase protocol: preview first with zero mutation
(return code 3), then apply only after explicit editor confirmation with
`--confirmed-by-editor`.

After each confirmed learning action, the agent MUST present a concise
Russian summary under the **exact heading `Что AutoScout узнал`**. This
summary preserves verbatim editor wording and plainly describes what was
recorded (which candidate, what status, which profile rule) and its
concrete future effect. Routine safe follow-up (rerender, persist,
push) then continues in the same request without another confirmation.

No state, Git, network, or model side effects occur before confirmation.
The existing two-phase protocol, auto-completion, and identity-merge
rules are **not** replaced — this section adds the acknowledgement step.

## Level 2 — Explicit technical request (issue filing authorised)

When the editor makes an **explicit Russian request** for code, config,
source, workflow, report, field, command, or integration changes, the
agent is authorised to create a focused `editor-request` issue **without
redundant approval**. The agent must NOT implement the request
incidentally during weekly review — the issue records it as a separate
tracked work item for later.

An **ambiguous or merely suggestive** technical remark does **not**
authorise an issue. In that case the agent asks **exactly one** concise
Russian clarification first. Editorial preferences (taste, priority,
hiding) are editorial — they remain Level 1 and never become technical
issues.

**Level 1 + Level 2 combined statement precedence:** when a single
editor utterance combines Level 1 feedback/profile learning with a
Level 2 explicit technical request, **Level 1 takes precedence**. The
mandatory confirmation boundary must be resolved (yes or no) before any
side effect — the agent does not file a GitHub issue while feedback
confirmation is pending. Once the Level 1 boundary is resolved, the
explicitly requested technical issue may be filed independently without
redundant approval.

## Level 3 — Escalation (stop before unsafe/further mutation)

The agent **stops before the unsafe or consequential mutation** (or
before any **further** mutation once a conflict or rejected push is
discovered) when facing:
- State or data corruption risk
- Git conflict or rejected push
- Security or privacy concern
- Repeated source failures indicating systemic breakage
- Broad ambiguity (editor's intent cannot be narrowed with one question)
- Destructive or irreversible action
- Identity merges (preserve the existing `--dry-run` boundary — see
  "Identity correction as a confirmation boundary" in `AGENTS.md`)

For all Level 3 situations the agent:
1. Preserves sanitized diagnostics when applicable (normal editor output
   does not expose raw diagnostic file paths).
2. Explains the situation in plain Russian.
3. Gives **exactly one** recommended next action.
4. Asks **exactly one** minimum-confirmation question.
