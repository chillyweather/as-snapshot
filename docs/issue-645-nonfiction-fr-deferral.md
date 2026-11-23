# Issue #645: Nonfiction.fr helper-field deferral

Nonfiction.fr Critiques is the only affected source the extraction-contract
fix cannot reach.

## Why this source is different

`autoscout/source_collectors/nonfiction_fr.py` is a live RSS collector with
its own metadata parse.
It never calls `extraction_contract`, so declaring `fr` and giving `generic`
the helper contract change nothing for it.
It has no extraction model, so it cannot translate or transliterate on its
own.
A deterministic collector must never invent helper values.

## What is ready

The collector accepts the same version-1 helper sidecar as the Codex path
(`helper_metadata_file` plus `helper_metadata_sha256` on the source).
When configured, each French mention gains `english_title`,
`search_translation`, and `author_transliteration_note` from the curated
sidecar row, the authoritative French title and authors stay untouched, and
`helper_attached_count` in the collector debug metadata reports how many
mentions matched.
Proven by `test_helper_sidecar_attaches_english_helpers_to_french_mentions`.

## What is deferred

No curated sidecar exists for this source yet, so current rows still carry
no English title.
Proven by `test_replay_without_sidecar_reports_zero_attached_helpers`,
which pins `helper_attached_count == 0` and no `english_title` key.

The 9 existing rows are covered by the Issue #645 English-title backfill
like every other affected row: they are existing history, not a forward
collection decision.

## What remains

Curate one sidecar row per reviewed French book, preferring the real
published English edition title with the provenance contract from
`docs/issue-458-title-evidence-contract.md`, and configure the two
`helper_metadata_*` fields on the source.
Until then the next extraction still produces helper-less mentions, and
that is a recorded gap, not a silent one.
