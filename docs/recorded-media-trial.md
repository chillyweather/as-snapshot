# Recorded Media Trial

## Dima review

This trial fixture is intentionally not canonical editor data yet. It is a reviewable input set for checking whether Recorded Source Mentions from real open media/curated-list pages produce a useful MVP Pipeline report.

Sources:

- NPR Summer 2024 nonfiction staff picks.
- TIME 2024 nonfiction and must-read book pages.
- BookPage Best Nonfiction 2024.
- Official 2024 nonfiction award signals.
- Publishers Weekly hardcover nonfiction bestseller snapshot.

Review focus:

- Are the Recorded Source Mentions plausible Scouting Candidates?
- Are Evidence Notes short enough but still useful?
- Are Found in Russian and Visible Risk controls visible in the right artifacts?
- Does the generated CSV look useful enough to show to the editor friend?

The fixture stores metadata, URLs, concise Evidence Notes, and explicit statuses only. It does not store copied article bodies, full reviews, book samples, or long excerpts.

## Review decision

The recorded media-list trial is not yet ready to show to the editor friend as a small pilot scouting report.

The run is mechanically healthy: it produced 16 raw Recorded Source Mentions, four Found in Russian exclusions, and 10 editor-facing Weekly Report Rows without a row-count shortfall.
The CSV contract, internal exclusions, multi-family signal aggregation, evidence locators, Russian-edition review metadata, and Visible Risk handling are working.
The Russian-edition checks are incomplete for the visible rows, so `pilot-status.json` correctly marks the output as not ready to share.

The pilot should still be framed as trial scouting output from recorded open-source metadata, not a production weekly report or legal/rights clearance.

## Main failure modes

- **Independent Signal**: the trial includes `award` and `bestseller/ranking` signals as additional source families. Two visible Scouting Candidates have multiple Independent Signals, which proves the recorded multi-family path at small scale.
- **Evidence Note**: Evidence Notes are concise and copyright-safe, and page-level sources now include title-level locators in the Signal Summary.
- **Russian-edition status**: The pilot uses automated Russian-edition fixture checks for the known Found in Russian exclusions, but the visible rows have incomplete provider coverage. They therefore remain `unclear` instead of being treated as `not found in Russian`, and the incomplete checks appear in the Russian review queue.
- **Visible Risk**: clear Visible Risk and text-check rows behave correctly, but the status values are manually recorded scouting-stage labels, not legal clearance.

## Pilot share decision

Reproducible command:

```bash
python -m autoscout run-mvp --config configs/recorded-media-trial.json --output-dir tmp/recorded-media-trial
```

The generated `pilot-status.json` says the pilot is not ready to send to the editor friend until the incomplete Russian-edition checks are resolved.

## Pilot package demo

Regenerate the pilot package with:

```bash
python -m autoscout run-mvp --config configs/recorded-media-trial.json --output-dir tmp/recorded-media-trial
```

Shareable pilot artifacts:

- `editor-facing.csv` - the editor-facing Weekly Report Rows.
- `editor-review-guide.md` - the review guide and pilot caveats to send with the CSV.

Internal review artifacts:

- `pilot-status.json` - readiness status, shareable/internal artifact list, caveats, and feedback intake contract.
- `run-summary.json` - source counts, warning counts, shortfall reason, and operational health.
- `source-runs.json` - per-source collection status, counts, skipped or failed reasons, and safe debug metadata.
- `internal-exclusions.json` - Found in Russian and other hard exclusions kept outside the CSV.
- `russian-review-queue.csv` - manual review queue for unclear Russian-edition statuses, when applicable.

Manual check:

1. Open `pilot-status.json` and confirm `ready_for_editor` is false while Russian-edition verification is incomplete.
2. Open `editor-facing.csv` and confirm it still has one Weekly Report Row per visible Scouting Candidate.
3. Open `editor-review-guide.md` and confirm the editor can understand what to review without implementation context.
4. Open `run-summary.json` and `source-runs.json` only for Dima's operational review.

Verification command:

```bash
python -m unittest discover -s tests
```

## Follow-up issues

- https://github.com/chillyweather/auto-scout/issues/16
- https://github.com/chillyweather/auto-scout/issues/18
