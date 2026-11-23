# Frozen chart evidence baseline

These ten capture and extraction files are a frozen copy from commit `d212b4f` and the 2026-08-13 weekly run.

## Why the copy exists

`runs/current-weekly-pilot-russian` is a moving alias.
Every weekly operation can overwrite it, so tests that read chart artifacts from that directory can change results without a code change.

The chart evidence tests need stable capture and extraction pairs to preserve the expected collected status, counts, ranks, and evidence URL behavior.

## The rule

No weekly run may overwrite these files.

Tests must not assert fixed status, count, rank, row identity, or similar outcomes against `runs/current-weekly-pilot-russian`.

Chart tests must read this frozen fixture directory when they assert fixed outcomes from the 2026-08-13 weekly run.

## Updating this baseline

Any future fixture change must first be justified by a diff of the affected rows posted before the expected values are edited.

Only update the fixture and its expected values after that justification shows that the new bytes represent the intended evidence and behavior.
