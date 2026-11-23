# Extraction disables the Codex memories feature and proves it

Issue: #414.

Decided by the editor on 2026-09-06, recorded on the issue as
[comment 5560309395](https://github.com/chillyweather/auto-scout/issues/414#issuecomment-5560309395),
which re-scoped the issue from `external_agent_memory_import` to the live feature, and
[comment 5560865901](https://github.com/chillyweather/auto-scout/issues/414#issuecomment-5560865901),
which records the decision and what it authorised.
The editor authorised three things together: the flag, the fail-closed check on a feature name the CLI no longer knows, and the effective feature state in the Stage 0 evidence.
The issue title was changed at the same time, so the tracker names the feature that is actually disabled.

Later runtime-policy decisions stay with #354, as issue #414 states.
This ADR settles one feature for the extraction stage and nothing else.

## Context

An extraction work item must ground every Agent-Extracted Mention in the retained Source Capture.
Issue #411 stated that rule as a prompt instruction, which is advisory.
The model can read whatever its own environment offers it, and the run had no way to say what that was.

The rollout evidence in `docs/issue-449-no-progress-diagnosis.md` shows what the environment offers.
Every extraction task on 2026-08-14 and 2026-09-03 received this injected input immediately after `task_started`:

```
## Memory

You have access to a memory folder with guidance from prior runs. It can save
time and help you stay consistent.
```

The folder is `~/.codex/memories` on the operator machine.
It is not generic.
It holds `MEMORY.md`, `raw_memories.md`, and a `rollout_summaries/` directory of per-run notes, including `autoscout_weekly_search_amazon_source_issue_612.md` and `autoscout_weekly_search_backfill_12_russian_descriptions.md`.
`MEMORY.md` names the Configured Sources the run extracts: Académie française, Cúspide, Bokhandlerforeningen, Audible, PublishNews.

So an extraction item is invited to read notes about the same Configured Source it is extracting.
A mention that came from a note rather than from the captured page cannot be told apart in the evidence.

Issue #414 named the feature `external_agent_memory_import`, measured on Codex CLI 0.147.0.
On the installed CLI, which is `codex-cli 0.153.4`, `codex features list` reports that feature as `under development` and `false`.
The live feature is `memories`, reported as `stable` and `true`.
The toggle as the issue wrote it would have changed nothing and would have reported a safety extraction did not have.

## Decision

Extraction disables the `memories` feature, and Stage 0 proves that the flag took effect before the run starts.

The extraction command carries `--disable memories`.
The policy lives in `EXTRACTION_DISABLED_FEATURES` in `autoscout/codex_features.py`, so one constant names what extraction switches off.

The proof is a local command and costs no model request.
`codex features list` reports the effective value of every feature under the flags it is given, so Stage 0 asks the CLI with the extraction flags applied and reads back what extraction will see.
The Stage 0 evidence records the effective value, not the intention.

The gate is fail-closed in three ways, and it runs before the minimal exec probe so that an unsatisfiable policy spends no Codex request:

A feature name the CLI does not know is a failure.
The CLI rejects an unknown flag with `Error: Unknown feature flag: <name>` and exit code 1, so a renamed or removed feature stops the run with `codex_feature_rejected` instead of passing silently.

A feature that is still enabled under its own disable flag is a failure.
The flag did not take effect, so the run stops with `codex_feature_still_enabled`.

A check that cannot be read is unknown, never a pass.
A probe that times out, a missing CLI, or an unreadable feature table returns `codex_features_check_unknown`, because "could not check" must not be confused with "memory is off".

The Stage 0 exec probe runs the same command extraction runs, feature flags included.
A flag that breaks invocation is then found in Stage 0 rather than 29 sources into the extraction stage.

## Consequences

A Codex fail in Stage 0 already stops the run with `INTERVENTION_REQUIRED` before capture, so this gate inherits that behaviour and wastes no capture work.

The toggle overrides an operator setting.
That is the point of the decision, and the boundary from issue #414 still holds: extraction correctness must not depend on the toggle alone.
The prompt instruction from issue #411 stays in place beside it.

A Codex release that renames the feature stops the weekly operation.
This is deliberate.
The alternative is a run that claims grounded extraction while the model reads prior-run notes.
The repair is one line in `EXTRACTION_DISABLED_FEATURES` plus the tripwire in `tests/test_codex_recovery.py`, and the failure message names both, together with the installed CLI version.

An older CLI stops the weekly operation for the same reason.
A CLI that predates the `memories` feature does not list it, so the gate reports `codex_feature_rejected` rather than passing.
The repair is a CLI upgrade, and the message says so.
This is the honest outcome: on such a CLI the feature this ADR disables cannot be proven off, and the older `external_agent_memory_import` toggle that issue #414 first named is not a substitute, because it governs a different feature.

Extraction may behave differently without memory.
The notes could have carried useful conventions about a Configured Source.
The first live operation after this change should be compared against the previous one, per Configured Source, so a drop in valid mentions is noticed rather than assumed away.

The offline fixture path is unaffected.
`deterministic_adapters` supplies its own Stage 0 probe, so no offline run invokes the CLI.

The same decision shape applies to two further injections the rollout shows, `<multi_agent_mode>` and `<recommended_plugins>`.
They are recorded in issue #414 and are not adopted here.
