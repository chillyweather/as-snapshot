# Source-gap discovery contract (#340)

This source-gap route is available only after an explicit editor request.
Use `./scripts/add-source discover` for offline report mode.
Offline report mode needs no URL and accepts optional `--signal-family`.
The Search Optimization Review is optional: without `--review`, discovery reads `runs/current-weekly-pilot-russian/search-optimization-review.json` only when it exists; when absent, configured sources are `not-reviewed` and no allocation recommendations are used.
`--review <path>` is an explicit override and keeps strict file and schema validation.
Discovery resolves a `weekly_portfolio` reference relative to the supplied `--portfolio` path before validation and analysis.
Overlay active state and canonical source metadata are applied before coverage is calculated.
Unsupported options such as `--url` are discovery input errors.
Offline report mode returns exit code 0 on success or status `insufficient_metadata`, and exit code 2 for command, input, or storage errors.
Unclassified sources are listed once in `sources_without_signal_family` with bounded identity, active state, declared family, and reason fields.
Status `insufficient_metadata` suppresses weakness conclusions, candidate proposals, and probing until canonical signal-family metadata or a resolvable weekly portfolio is supplied.
It computes signal-family coverage, silently omits configured URLs, and reports only ledger-known exclusions.
It does not access the network, invoke a model, or modify configuration, Source Proposals, Source Mentions, Scouting Candidates, Candidate History, or weekly state.
The offline command stops after the report.

Run `./scripts/add-source discover --probe` only when the editor explicitly asks to probe the remaining candidates.
The live mode computes coverage, configured URL filtering, and ledger exclusions offline before invoking the existing `./scripts/probe-source` launcher.
It passes `--keep-captures tmp/source-candidate-captures`, `--ledger data/source-candidate-ledger.json`, and `--feasibility-dir data/source-feasibility-drafts` to that launcher.
Retained captures stay outside Git publication in the ignored temporary directory.
The live mode may write only the existing probe ledger and Markdown sidecar, retained accepted captures, and feasibility drafts.
Protected configuration and weekly/editorial state remain unchanged.
The full launcher invocation requires one tool-level network authorization with this exact rationale: «Для проверки доступности предложенных источников нужен сетевой доступ. Разрешить запуск?»
Live discovery-probe mode propagates exit code 0 when any candidate accepts, exit code 1 when no candidate accepts and a reject or malformed result exists, exit code 2 for discovery, probe, command, input, or storage errors, and exit code 3 when all probed candidates defer.
If no candidates remain after configured and ledger filtering, it returns exit code 0 with a clear no-candidates status and makes no network call.
The live command stops after the probe.
Successful reports have top-level status `success`.
Reports with insufficient family metadata have top-level status `insufficient_metadata` and exit code 0, including when `--probe` is supplied.
Input and configuration failures have status `input_error`; probe launcher failures have status `probe_error`.
Both modes require a later explicit editor choice of exactly one candidate, and that candidate must be accepted, before onboarding.
The direct `./scripts/probe-source` batch contract and the URL-driven `./scripts/add-source probe` contract remain unchanged.
