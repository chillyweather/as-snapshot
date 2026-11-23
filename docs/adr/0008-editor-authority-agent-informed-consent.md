# Editor is the authority; the agent acts on informed consent

The editor is the decision authority for AutoScout — what she wants should be done. The agent operates with broad autonomy in service of her requests: it applies per-candidate feedback, configuration, source curation, and other changes directly. For any change with risky or non-obvious consequences (e.g. disabling a risk-screening category, removing a source), the agent must first **explain the consequences and offer concrete options**, then act on the editor's choice. This is informed consent, not a developer-approval gate. The agent's only hard line is refusing actions that would **destroy the weekly workflow or break the app entirely**; short of that, the editor decides.

Dima (developer) is an implementer and the async escape hatch for actual breakage (see [ADR 0007](0007-editor-run-agent-on-git-repo.md)), **not** an approver of editorial or configuration decisions. No routine change waits on Dima.

## Considered Options

- **Gate sensitive changes through Dima.** Rejected: inserts the developer as a bottleneck and contradicts both the editor-as-authority principle and the "Dima rarely involved" operating model.
- **Editorial-only restricted autonomy** (agent may only touch per-candidate state; everything else is a proposal). Rejected: too limiting — the editor should be able to shape how the system behaves, not just label candidates.

## Consequences

- The blessed agent command/skill set is **broad**, not a narrow whitelist. The safety mechanism is consequence-explanation plus reversibility (git history), not permission gates.
- The agent needs an internal sense of which actions are "workflow-destroying / app-breaking" (the only hard refusal) versus merely consequential (explain + offer options) versus routine (just do it).
- Risky-but-allowed changes (e.g. ADR 0004 risk-screening edits) are honored after the editor is informed; the protection against silent harm is the explanation, not a veto.
