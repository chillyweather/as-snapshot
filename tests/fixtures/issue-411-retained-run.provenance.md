# Issue 411 retained run freeze

The source directory is `runs/current-weekly-pilot-russian`.

The Run Identity is `1925473f21281137a40be08035345fd696f8cd1d77af4dd86669c8b15aeca4ee`.

The run date is `2026-08-12`.

This immutable copy exists to prepare the separately authorized post-repair production-confidence Campaign required by #411 for evidence under #354, without risking overwrite by the next weekly operation.

No live Campaign has run.

Only the files that tests still read are kept: `codex-extraction-worklist.json` and the eight captures that `tests/test_issue_422_calibration_validation_diagnostics.py` validates. The rest of the frozen run and its checksum list were removed with the issue #411 reproduction tooling (#1016); the full copy is in git history.
