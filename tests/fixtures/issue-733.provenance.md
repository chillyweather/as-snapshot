# Issue #733 fixture provenance

`issue-733/boklista-2026-09-26-capture.json` is a byte-for-byte copy of the capture that the 2026-09-26 weekly run retained for Bokhandlerforeningen Boklista - Generell litteratur voksne:

`runs/current-weekly-pilot-russian/codex-capture-bokhandlerforeningen-boklista---generell-litteratur-voksne-4fc8ef89.json` at commit `459cef9`.

Its SHA-256 is `ee3e6fab9f7b4d99a689a976a293c2e9935e0a8f462b129d05680af80f921b5e`, the `capture_sha256` that the run's extraction work item and extraction artifact both carry.
Codex extraction returned `entries: []` from this capture, although the capture holds all 26 ranked rows.

Tests read the copy rather than the run directory, because tests must not construct the repository-root weekly run path (Issue #475).
