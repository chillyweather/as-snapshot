# Issue #401 first authorized probe verification

This report preserves the sanitized outcomes from the first authorized four-call post-prompt probe.
This report is deterministic, sanitized, and contains no performance measurements, absolute or temporary paths, credentials, or raw model output.

## Capture integrity

| Phase | Capture path | Bytes | SHA-256 | Identical |
| --- | --- | ---: | --- | --- |
| before | runs/current-weekly-pilot-russian/codex-capture-bookmarks--lithub----new-nonfiction-d824846c.json | 310789 | 63b69d6f95ddac75eb8bc6909ad7be7779835ccdcc13d4658d851cc82137218a | True |
| after | runs/current-weekly-pilot-russian/codex-capture-bookmarks--lithub----new-nonfiction-d824846c.json | 310789 | 63b69d6f95ddac75eb8bc6909ad7be7779835ccdcc13d4658d851cc82137218a | True |

## Observations

| Mode | Workers | Status | Diagnostic | Raw | Validated |
| --- | ---: | --- | --- | ---: | ---: |
| intrinsic | 1 | validated | validated | 40 | 40 |
| production-shaped | 1 | invalid | unexpected_empty_extraction | 0 | 0 |
| production-shaped | 2 | validated | validated | 39 | 39 |
| production-shaped | 3 | validated | validated | 40 | 40 |

## Status counts

| Status | Count |
| --- | ---: |
| validated | 3 |
| invalid | 1 |
| censored | 0 |
| timeout | 0 |
| command_failure | 0 |

Non-empty validated observations: 3.
Empty output observations: 1.
