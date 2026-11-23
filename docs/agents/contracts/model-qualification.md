# Model qualification

## Blessed model qualification (Issue #734)

The Codex extraction model is pinned in `configs/codex-model-pin.json` (Issue #731).
When the operator asks to evaluate a new model for extraction, or Stage 0 reports `codex_pinned_model_unavailable`, use:

```
./scripts/qualify-model --model <name> [--effort medium]
```

It replays the frozen benchmark through Codex, several times per source, and writes a PASS/FAIL report under `data/model-qualification/reports/`.
`--backend claude-api` runs the candidate on the Claude API instead (#1149), with the key in `~/.config/autoscout/claude.env`; a source the backend refuses because it mentions `publishersweekly.com` is excluded, not scored, and named in the report under "Excluded sources".
It does not fetch source pages, call Git, or touch weekly or editorial state, and it never changes the pin.
Changing the pin is a separate reviewed commit that cites a PASS report in `qualification_report`.
`./scripts/qualify-model --refresh-benchmark` rebuilds the benchmark from committed weekly runs, and runs only on explicit operator request.

## Blessed description model qualification (Issue #843)

Grounded description synthesis runs on no pinned model yet.
When the operator asks to compare models for description synthesis, use:

```
./scripts/qualify-description-model --candidate <model>:<effort> [--candidate ...]
```

It replays the frozen synthesis benchmark through Codex and writes a comparison report and a blind-review sheet under `data/description-qualification/reports/`.
A candidate written `claude-api/<model>:<effort>` runs on the Claude API (#1149); items it refuses because their evidence mentions `publishersweekly.com` are excluded, not scored, and named under "Excluded items".
It does not fetch source pages, call Git, or touch weekly or editorial state, and it never changes a pin.
`--score <report-dir>` folds the human blind-review ratings into that report; `--refresh-benchmark` rebuilds the benchmark from Git history, and runs only on explicit operator request.
The procedure, metrics, and pass criteria are in `docs/description-model-qualification.md`.
