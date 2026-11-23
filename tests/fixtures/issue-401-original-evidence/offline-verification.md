# Issue #401 offline verification

This report uses the fixed captured BookMarks page, deterministic fixture outputs, and sanitized counts from the first authorized four-call probe.
It does not run a weekly workflow, live recapture, Codex, a model, or network access.
The first authorized post-prompt probe produced 3 non-empty validated outputs and 1 empty output.
The empty output is preserved as unexpected_empty_extraction and is not counted as repaired or validated.
This report does not claim final production repair.

## Status separation

| Status | Count |
| --- | ---: |
| validated | 3 |
| invalid | 5 |
| censored | 1 |
| timeout | 1 |
| command_failure | 1 |

## First authorized post-prompt probe

| Mode | Workers | Status | Diagnostic | Raw | Validated |
| --- | ---: | --- | --- | ---: | ---: |
| intrinsic | 1 | validated | validated | 40 | 40 |
| production-shaped | 1 | invalid | unexpected_empty_extraction | 0 | 0 |
| production-shaped | 2 | validated | validated | 39 | 39 |
| production-shaped | 3 | validated | validated | 40 | 40 |

## Deterministic validation orchestration

| Mode | Workers | Status | Diagnostic | Raw | Validated |
| --- | ---: | --- | --- | ---: | ---: |
| intrinsic | 1 | validated | validated | 1 | 1 |
| production-shaped | 2 | validated | validated | 1 | 1 |
| production-shaped | 3 | validated | validated | 1 | 1 |

## Invalid fixture outputs

| Fixture | Diagnostic | Raw | Validated |
| --- | --- | ---: | ---: |
| tests/fixtures/issue-401/missing-required-fields.json | missing_required_fields | 1 | 0 |
| tests/fixtures/issue-401/unverifiable-provenance.json | unverifiable_provenance | 1 | 1 |
| tests/fixtures/issue-401/relative-provenance.json | validation_failed | 1 | 1 |
| tests/fixtures/issue-401/malformed-json.json | malformed_json | null | null |
| tests/fixtures/issue-401/empty-entries.json | unexpected_empty_extraction | 0 | 0 |

## Censored boundary

| Status | Diagnostic | Stage | Supplier |
| --- | --- | --- | --- |
| budget_exhausted | portfolio_budget_exhausted | queue | portfolio_deadline |

## Timeout boundary

| Status | Diagnostic | Stage | Supplier |
| --- | --- | --- | --- |
| timeout | codex_timeout | not applicable | not applicable |

## Command-failure boundary

| Status | Diagnostic | Stage | Supplier |
| --- | --- | --- | --- |
| command_failed | command_failed_permission | not applicable | not applicable |

## Policy conclusion

One observation for each mode is not enough to make a timeout or worker-policy decision.
No timeout or worker policy change is justified by offline repair evidence.
A separate timeout and worker-policy investigation remains needed only if production-faithful post-repair evidence still shows slow or censored outcomes.
