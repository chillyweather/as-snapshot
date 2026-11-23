# Live Weekly Calibration 2026-07-02

This calibration run verifies the live weekly scout after oversized source-page compaction from issue #189.

This calibration used `configs/current-weekly-live.json` and the live weekly workflow.

## Historical Artifact Location

The historical live-run artifacts remain available in Git history.
Commit 1, `ae50040218bb8a37983c5cefee2549d49508976d`, is the last commit that contains the artifacts previously stored under `runs/current-weekly-live-russian-after-189`.

## Result

The run is not ready for editor review.

- `run_health_status=not_ready`
- `classification=source health problem`
- `collection_mode=mixed`
- `collection_modes=live,real-live,real-live-unavailable`
- `ready_for_editor=false`
- `editor_facing_count=16`
- `editor_row_target=100`
- `skipped_source_count=3`
- `failed_source_count=6`

Source contributions:

- `BookPage Nonfiction`: 9 editor-facing rows
- `Publishers Marketplace Rights Offerings`: 7 editor-facing rows


## Oversized-Input Verification

The prior `input_oversized` gate is no longer the active blocker for the formerly oversized sources.

- `Goodreads Choice Awards 2025 Nonfiction`: moved past oversized input and failed later with `model_output_malformed`.
- `USA TODAY Bestselling Books Nonfiction`: moved past oversized input and reached `real-live`; the extraction response contained no mentions.
- `PEOPLE Books`: moved past oversized input and failed later with `model_output_malformed`.
- `BookPage Nonfiction`: moved past oversized input, completed as `real-live`, and contributed 9 editor-facing rows. Its source-run debug metadata shows `raw_model_input_chars=328176`, `compacted_model_input_chars=228141`, `model_input_chars=120000`, `model_input_compacted=true`, and `model_input_truncated=true`.

## Remaining Source Health Findings

These are now separate source-health findings, not the oversized-input implementation bug.

- CAPTCHA-like blocks: `Current Weekly Nonfiction Agent Extraction`, `National Book Awards 2025 Nonfiction`.
- HTTP 403: `Libro.fm Nonfiction Audiobook Bestsellers`.
- HTTP 404: `Spiegel Bestseller Sachbuch`, `Die Zeit Sachbuch-Bestenliste`.
- Malformed model output: `Goodreads Choice Awards 2025 Nonfiction`, `PEOPLE Books`.
- Empty extraction output: `USA TODAY Bestselling Books Nonfiction`.
- Transport timeout: `Deutscher Sachbuchpreis 2024`.

## Operator Conclusion

Issue #189 is verified: oversized fetched pages are now attempted through bounded real extraction instead of failing at the input-size gate.

The weekly live report is still not ready for editor review. Follow-up work should target model-output robustness, empty-extraction handling, and stale or blocked configured sources. Do not treat the 16-row shortfall as an editor-ready package.
