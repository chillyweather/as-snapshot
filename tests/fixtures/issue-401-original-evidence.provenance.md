# Issue #401 original evidence

Deduplicated in #1022: the identical copy under `issue-401-retained/evidence/` was removed (its only test asserted that the two copies were equal), and `capture-integrity-after.json` was removed because it was byte-identical to `capture-integrity-before.json`; `integrity-comparison.json` records `"identical": true` for the pair. The earlier copies are in git history.
