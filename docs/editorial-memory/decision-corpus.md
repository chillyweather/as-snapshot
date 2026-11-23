# Decision corpus for the editorial memory prototype

The editorial memory prototype (#821) reads a frozen corpus of the editor's decisions.
`./scripts/export-decision-corpus` writes it (#929).
The export is read-only: it reads current state through the State Store and the earlier versions of Candidate History from Git, and never changes canonical state.

## Output

The export goes to `data/decision-corpus/<commit date>-<commit>/`, which Git ignores:

- `decisions.jsonl`: one record per book with an editor decision, sorted by Candidate ID.
- `manifest.json`: the source commit, the Candidate History path read, the number of versions read and the first and last of them, whether the current state is the version the source commit holds (`state_matches_source_commit`), the record, dated and undated counts, the undated count per reason, the note-type counts, the number of rebuilt books no longer in Candidate History, the SHA-256 of `decisions.jsonl`, and the export time.

Exporting the same commit with the same state again gives the same `decisions.jsonl` and the same manifest apart from `exported_at`.

## How a decision is dated

A decision is a status with its verbatim note.
The export reads every version of Candidate History on the source commit's first-parent line, oldest first.
A decision gets the author date of the first commit where the book shows it.
The date is the calendar date in the commit's own time zone.
Records of one book that were merged later are followed through today's alias table, so a decision made on a duplicate keeps its own date.
When an identity merge keeps one of several decisions, the kept decision is logged again with the merge's date, as a confirmed merge has done since #1053.
Since #1053 a confirmed decision carries its own date on the record; from the first version where a book has such a dated entry, the record's own log is used and Git no longer adds entries for that book.

A decision with no date is never guessed. `decision_date` is `null` and `undated_reason` says why:

| `undated_reason` | Meaning |
|---|---|
| `present_in_oldest_version` | The decision is already in the oldest version read, so it was made before Git tracked it. |
| `first_seen_in_merge_commit` | The decision reached the main line in a Git merge commit, so when it was made on the other branch is not known. |
| `not_in_git_history` | The current state shows a decision that no commit read holds yet. |

Only versions where Candidate History is one file are read.
Both layouts of Candidate History are read: the single file, and one file per book since #1073.

## Record fields

| Field | Meaning |
|---|---|
| `candidate_id` | The durable Candidate ID. |
| `identity_key` | The record's identity key today. |
| `aliases` | Every other identity key that resolves to this book; exclude them with the ID when the book is the query. |
| `retired_candidate_ids` | Candidate IDs merged into this one. |
| `title`, `authors` | The book today. |
| `editor_status`, `editor_note` | The latest decision, verbatim. |
| `decision_date` | The ISO date of the latest decision, or `null`. |
| `undated_reason` | Why `decision_date` is `null` (see above); `null` when dated. |
| `decision_commit` | The commit where the latest decision first appears; `null` when no commit holds it. |
| `note_type` | See below. |
| `feedback_dimensions` | The confirmed #678 dimensions, or `null`. |
| `seen` | The book as that commit held it, for a dated latest decision: `commit`, `title`, `english_title`, `authors`, `description`, `english_description`, `description_digest`, and `sources` (source name and evidence URL). `null` when undated. |
| `editor_decisions` | Every decision, oldest first, in the #1053 shape: `status`, `note`, `decision_date`, `description_digest`. The last entry is the latest decision. |

`description_digest` is the digest function of #1053: the first 16 hex characters of the SHA-256 of the description the review would show, `""` when it would show none.
A description that today's validation rejects therefore has `""` even when `seen.description` holds text.

## Note types

The label comes from existing fields only; a note is never interpreted.

| `note_type` | Rule |
|---|---|
| `already_known` | The status is `already_known`. |
| `no_note` | The note is empty. |
| `bare_refusal` | The note is one of the bounded bare refusals of #678, such as «Нет.». |
| `rights_availability` | `feedback_dimensions.rights_availability` is `unavailable`. |
| `quality_data_problem` | `feedback_dimensions.quality_problem` names a problem. |
| `taste_reason` | `feedback_dimensions.editorial_appeal` is specified. |
| `unknown` | Anything else. Most notes carry a reason in words that no field records. |
