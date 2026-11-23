# Identity correction

The confirmation boundary is in `AGENTS.md` under "Identity correction as a confirmation boundary"; this file is the procedure.
Read it in full before the first `probable-duplicates` dismissal or `merge-candidate-identity` call.

`python -m autoscout probable-duplicates --state-dir runs/current-weekly-pilot-russian`
lists probable pairs read-only (#855); a listed pair is a candidate for the
steps below, never a merge decision. Dismissing a pair the editor rejects is
two-phase: preview with `--dismiss <key> --dismiss <key> --reason "<editor
words>"`, then the same call with `--confirmed-by-editor` only after the
editor confirms.

1. **Prepare** the correction payload (exact source identity keys and
   canonical identity key) outside canonical state files. No state, Git,
   network, or model side effects before confirmation.
2. **Run** `merge-candidate-identity --dry-run` — this computes the merge
   plan with zero mutation.
3. **Explain** in Russian with one question that includes:
   - **Every** exact source identity key (do not truncate).
   - The **canonical identity key**.
   - The **canonical title and author display** (the editor-facing book
     identity that will replace the merged records).
   - How many references will be rewritten.
   - What is preserved (feedback, visibility, history).
   - The **chosen canonical description** and its retained provenance and
     evidence whenever the records disagree about the description.
4. **Only after explicit confirmation**, re-run the same correction
   without `--dry-run`, then automatically rerender and finalize.

Two duplicate records can each carry an independently generated Russian description and its own `description_provenance`.
The merge never picks one on its own.
The correction artifact resolves the conflict explicitly with a `description_resolution` block.
The block names `canonical_source_identity_key`, the `observed_descriptions`, and the `observed_description_provenance` the editor was shown, repeated verbatim apart from surrounding whitespace.
A missing, extra, stale, or incompatible entry fails the correction before any state file is written.
Without the block the merge still refuses conflicting durable content.
Different editor decisions are resolved the same way with an `editor_decision_resolution` block; the confirmation question names every decision verbatim and which one becomes canonical, and the others are kept in an audit field (#917).
The full artifact shape is in `docs/cli-reference.md`.
