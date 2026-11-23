# Monthly Source Discovery Nudge (#341)

The exact Russian editor trigger is: «Запусти ежемесячный поиск источников за YYYY-MM».

Use `./scripts/add-source monthly --operation-month YYYY-MM` with an explicit month.

This launcher is the deterministic schedule target for an external or editor-controlled cadence.

The repository has no installed or active scheduler.

The canonical procedure, retry rules, and artifact contract are in `docs/operator/sources.md`.

Normal statuses return exit code 0:

- `new_candidates_available`
- `only_banked_candidates_available`
- `no_candidates_this_month`
- `insufficient_portfolio_metadata`

Errors return exit code 2:

- `input_error`
- `artifact_error`

The default derived write paths are:

- `data/monthly-source-discovery/reports/YYYY-MM/monthly-source-discovery.json`
- `data/monthly-source-discovery/reports/YYYY-MM/monthly-source-discovery.md`
- `data/monthly-source-discovery/monthly-source-discovery-history.json`

The same relative paths are used under `--output-dir` when it is supplied.

A banked candidate means only an unconfigured Source Candidate Ledger entry with outcome `accept`.

A current eligible #340 candidate that is not configured and is not ledger-known is new.

Monthly history is only a derived publication index and is never evidence or classification input.

The route is offline, read-only except for atomic derived publication, and cannot probe, onboard, or modify canonical state.
