# Issue #363 retained-run provenance

This directory is the immutable captured run (160 files when frozen) identified by operation `f4245698f3e9e7e2358e3b27a47121cd1f8cacf6d2ce01c0531af81fcc22a9ab`.

This sidecar is outside the hashed fixture directory and is not part of its manifest.

Trimmed in #1022: only the files that tests still read are kept (`codex-extraction-worklist.json`, `source-proposals.json`, and each `*-codex-extraction.json` with the capture its validation case names, read by `tests/test_issue_422_calibration_validation_diagnostics.py` and the source tests). The other 112 files (Candidate History, review, CSV and the captures of sources that have no extraction artifact here) were read by no test; the full copy is in git history.
