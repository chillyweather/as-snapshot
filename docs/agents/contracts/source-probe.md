# Source Candidate probe

When the editor explicitly asks to probe proposed or unconfigured sources, the agent MUST use the blessed launcher:

```
./scripts/probe-source
```

In Codex, the single tool-level sandbox-permission request is attached to the full `./scripts/probe-source` invocation, with this exact Russian operator rationale:

«Для проверки доступности предложенных источников нужен сетевой доступ. Разрешить запуск?»

The probe accepts repeatable `--url` values and optional per-candidate `--name`, `--signal-family`, and `--proposing-context` values.
The legacy batch probe returns 0 when any candidate is accepted, 1 when candidates are rejected or malformed without an accepted candidate, 2 for command, input, or storage errors caught by `probe_source_candidates`, and 3 when all candidates defer.
Offline `./scripts/add-source` uses a separate contract: 0 for success, 1 for correctable or other errors including repeated `--url`, 3 when confirmation is required, 4 for retained-evidence defer, and 5 for retained-evidence reject.
A missing proposing context uses the deterministic default `unspecified`; a supplied context is preserved exactly and never inferred from the URL.

Every accepted candidate also gets one deterministic feasibility draft in `data/source-feasibility-drafts` (override with `--feasibility-dir`).
The draft is created only for accepted candidates, never overwrites an existing draft, and contains grounded bounded metadata with an incomplete editorial-rationale section that needs editor or agent judgment.

The probe is read-only with respect to configuration, weekly state, Candidate History, visibility, Editorial Taste Profile, Source Mentions, and Scouting Candidates.
Captures are discarded unless `--keep-captures` is explicit.
Ledger writes use the documented `data/source-candidate-ledger.json` default or the explicit `--ledger` path, both outside weekly state.
Without `--re-probe`, an existing ledger entry returns `already_evaluated` without network access or JSON rewriting.
Pass `--re-probe` explicitly for one fresh bounded HEAD/GET probe of a durable entry; repeated URLs in one batch are still fetched at most once.
The complete history and structural-diagnostic privacy contract is defined in `docs/source-onboarding-runbook.md`.
