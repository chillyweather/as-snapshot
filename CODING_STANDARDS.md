# Coding Standards

## The gate

`./scripts/check` is the same gate as CI.
Make it pass before pushing.
Run `./scripts/install-hooks` once per clone to turn on the pre-push hook (`.githooks/pre-push`).
It lets a `task/*` branch reach GitHub only when `./scripts/check` passed on exactly the pushed commit: the gate records the tree it passed on, and the hook runs the gate itself when that record does not match.
Pushes to `main` are not gated, so the weekly workflow's state push is unaffected.
CI runs the same script for pull requests and for `main`, not for every branch push.
Each checkout or worktree needs its own `.venv` with the project installed into it (`.venv/bin/python -m pip install -e ".[dev]"`); `./scripts/check` refuses to run against an environment installed from another checkout.

## The suite on both State Store backends (#1075)

`./scripts/check` runs the pytest suite on the JSON State Store backend, and then the State Store's own tests (`tests/test_state_store*.py`) on SQLite.
`./scripts/check --state-backend sqlite` runs the whole suite on SQLite instead (directly: `pytest --state-backend sqlite`).
The SQLite run is required before the cutover to SQLite (#1077) and for every change under `autoscout/state_store/`; run both commands then.

Under `--state-backend sqlite` every test selects SQLite through `AUTOSCOUT_STATE_BACKEND` and keeps its databases in its own temporary directory (`AUTOSCOUT_STATE_DATABASE_DIR`), and a run that leaves a database in `data/state-store/` fails.
State is set up and observed through `tests/state_store_support.py`: `seed_state`, `read_state` and `state_snapshot` follow the selected backend, and `state_snapshot` adds the database's content checksum, so a zero-mutation test sees a database write.
A test that writes state files itself calls `sync_state` before the command it tests; a test of a run without a state directory reads what the run left with `read_run_output`, and a committed fixture is read with `read_fixture_state`.
A test about the JSON backend's files (malformed bytes on disk, file names, the sharded layout) carries `@pytest.mark.json_backend_only("<why>")` and is skipped under SQLite; keep that list short.

## Product-rule tests are findings to report (#658)

When an existing test encodes a product rule and a change cannot pass it, stop and report the conflict.
Do not weaken the test to obtain a passing suite.
Disabling the test, skipping it, marking it expected-to-fail, narrowing its assertion, and deriving its expected value from the artifact being checked all require the same explicit decision.
A test may change when the product rule genuinely changes.
Then the PR must record which rule changed, why it changed, and who authorized it.
A literal expected value can be a deliberate tripwire.
Update it intentionally and explain why in the PR.
This protects product rules.
It does not prohibit ordinary test maintenance or removal of proven duplicates.

## Do not test documentation wording (#1017)

A test must not assert that a doc, skill, README or launcher text mentions a phrase.
A test may read a doc only to protect a real invariant, named in the test name, and compares it with the code's own value, not a literal copied into the test.
