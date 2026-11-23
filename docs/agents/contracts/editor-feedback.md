# Editor-feedback commands and previews

Read this file in full before the first `record-feedback`, `record-feedback-batch` or `editorial-profile add` preview.
The confirmation boundary and the rules that hold in every session (steps 4 to 6, verbatim notes, the weekly-run refusal and the important constraints) are in `AGENTS.md` under "Mandatory editor-feedback protocol"; the step numbers below are the same as there.

## Phase 1 — Preview (no mutation)

1. **Resolve the exact candidate.** Use the Candidate History
   (`candidate-history.json`, or one file per book under
   `candidate-history/`) or latest scouting candidates
   (`latest-scouting-candidates.json`) state files — never guess from a
   loose title or anecdotal memory. If the candidate cannot be found, stop
   and report the resolution failure.

   When the editor returns a review (Markdown, HTML copy, or CSV rows),
   resolve every returned row with the read-only resolver before any
   `record-feedback` preview (#851):
   ```
   python -m autoscout resolve-review-rows \
     --rows <returned rows .md|.csv|.json> \
     --state-dir runs/current-weekly-pilot-russian
   ```
   Every review row carries the durable Candidate ID (the `ID` column in
   Markdown, the ID line in HTML, `Durable Candidate ID` in CSV), and the
   resolver matches it first. Use only `resolved` rows for previews. Show
   an `ambiguous` row to the editor with every alternative (title, author,
   identity key, editor status) and ask which one is meant; never pick one.
   Report a `not_found` row as a resolution failure. A row resolved by
   title alone, or with `other_identities_share_title`, is named as such
   in the confirmation question.

2. **Run the preview command without `--confirmed-by-editor`.**
   - For per-candidate feedback:
     ```
     python -m autoscout record-feedback \
       --identity-key "<full Candidate ID>" \
       --status <interested|maybe_watch|reject|already_known|needs_deeper_analysis> \
       --note "<verbatim editor note>" \
       --state-dir runs/current-weekly-pilot-russian
     ```
     The preview prints: matched title, author display, full Candidate ID /
     identity key, intended canonical status, the editor note verbatim, and
     `confirmation_required=true`. Return code 3 means "preview shown,
     confirmation required." No state file is created or modified.

   - For a returned review file, preview all decisions in one batch
     (#1153); single-book `record-feedback` stays for conversation:
     ```
     python -m autoscout record-feedback-batch \
       --decisions <decisions.json> \
       --state-dir runs/current-weekly-pilot-russian
     ```
     The file is a JSON list, one object per `resolved` row (shape and exit
     codes in `docs/cli-reference.md`). The preview prints every row, the
     count per status and `batch_digest=`; one bad row refuses the whole
     batch and stages nothing.

   - For Editorial Taste Profile entries:
     ```
     python -m autoscout editorial-profile add \
       --kind <hard-rule|soft-preference> \
       --scope <author|topic|publisher|source|source_family> \
       --subject "<subject>" \
       --quote "<verbatim editor words>" \
       --provenance "<conversation context>" \
       --state-dir runs/current-weekly-pilot-russian
     ```
     The preview prints: kind, scope, subject, the verbatim editor quote,
     the concrete future effect (a Hard Rule will hide matching candidates
     while remaining reversible; a Soft Preference will nudge ranking), and
     `confirmation_required=true`. Return code 3 means "preview shown,
     confirmation required." No profile entry is allocated or written.

3. **Present one concise Russian confirmation question** to the editor
   that includes:
   - The exact candidate identity that will be affected (title, author,
     full identity key).
   - The intended action (status, rejection, hide, hard rule, preference).
   - If both a per-book decision AND a standing profile rule are inferred
     from the same editor statement, explain and confirm them as two
     separate effects in that one question.
   - For a batch, one question covers the whole file: the row count, the
     count per status, and each row the preview flags with `warnings=`.

## Phase 2 — Apply (after confirmation only)

7. **Re-run the same command with `--confirmed-by-editor` added.** The
   confirmed invocation must apply exactly the payload that was previewed.
   A batch also takes `--digest`. `staged_rows` above 0 with
   `state_export=failed`: rows staged; don't blindly repeat. Run
   `./scripts/doctor`, follow its action, then preview and confirm again
   (`batch_already_staged`: the rows were kept).

8. **After mutation**, rerender/finalize safely (re-run `run-mvp` to
   consume pending feedback, then `render-review` to refresh the review).
