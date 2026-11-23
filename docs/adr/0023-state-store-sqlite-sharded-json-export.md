---
status: accepted
---

# One State Store seam, SQLite behind it, Candidate History sharded in Git

This narrows ADR 0007: canonical state stops being a set of JSON files and becomes a database file, and Git stays the remote record.
The operator decided it on 2026-11-14, in #1011.

ADR 0007 put canonical cross-week state in a private Git repository, as JSON files that the agent pulls before a run and pushes after.
ADR 0022 made the run server the only machine that runs the scan and pushes state.
That design held while the state was small.
The largest file, `candidate-history.json`, no longer is.

## Why the files no longer fit

Candidate History was 23.2 MB with 2,932 records on 2026-11-13, up from 18.7 MB and 2,208 records in the audit of 2026-11-10.
Four fields hold two thirds of the audited file: `description_provenance`, `source_evidence`, `primary_signal` and `russian_edition_verification`, 17% each.
Every weekly state commit rewrites the whole file, and there were 116 of them by 2026-11-10; the local `.git` is 396 MB.
At that size, 150 more versions would be 943 MB raw and 97 MB packed.
GitHub refuses a file over 100 MB, which is about four times today's size, and the operator plans for ten times.
So the file will not push long before the editor's catalogue stops growing.

The code is as exposed as the repository.
The package has 85 `json.load(s)` and 130 `json.dump(s)` call sites, `dict[str, Any]` appears 2,238 times, and 30 modules name the state files directly.
Nothing says what a record contains except the code that happens to read it.

## Decisions

**One State Store seam.**
The canonical collections are read and written through a single interface, the State Store, and no module outside it opens a canonical state file.
The interface is where typed records replace `dict[str, Any]` for those collections.
It has two backends, JSON and SQLite, and the JSON backend stays the default until cutover, so the migration changes no editor-facing behaviour.

**The state directory decides its backend (#1093, decided 2026-11-14).**
Editor commands take no weekly config, so a backend chosen only by the config or the environment would let one of them write the JSON files of a state directory kept in SQLite.
A cut-over state directory therefore carries a small committed marker, `state-backend.json` with `{"backend": "sqlite"}`, and every command that opens it uses SQLite; a config or environment selection of JSON there is refused, and the JSON backend refuses to write a marked directory except as the SQLite backend's export.
The marker names no database path, because the database is outside Git at a path that differs between machines; a checkout with the marker but no database is refused with the rebuild command, never read as empty.
Cutover and rollback are each one explicit command (`state-database cutover`, `state-database rollback`), separate from the rebuild, so a rebuild can be rehearsed on live state without cutting over.
A state directory without the marker keeps the earlier selection (environment, then config, then JSON), so an explicit SQLite selection still works on an unmarked directory for tests and rehearsals.
The seam owns exactly the collections #1011 lists:

- Candidate History, with the decision log inside each record until the SQLite backend gives it its own table;
- Excluded Candidate History;
- candidate visibility;
- the pending and the archived returned editor rows;
- the Editorial Taste Profile;
- the candidate identity aliases;
- Source Proposals;
- the ranking-freshness ledger;
- the latest Scouting Candidates.

What stays outside is listed below.

**SQLite, from the standard library only.**
The SQLite backend uses the standard library's `sqlite3` and no extension, so the project keeps its zero-dependency design.
The database file holds no embeddings and loads no extension.

**The database is never committed.**
A binary file that changes every night would repeat the problem it was meant to solve, and Git could neither diff nor merge it.
Git keeps a deterministic JSON export of the State Store instead: the same state always produces the same bytes, so an unchanged record is an unchanged file and a commit shows what changed.

**Candidate History is exported as one file per `candidate_id`, under `candidate-history/<2 hex>/`.**
The exact path inside the state directory is `candidate-history/<xx>/<candidate_id>.json`, where `<candidate_id>` is `candidate-` plus 24 lowercase hex characters and `<xx>` its first two hex characters (#1073).
The two hex characters shard the directory, so no directory grows without bound.
The JSON backend keeps whichever layout a state directory has, the single `candidate-history.json` or the shards, and an explicit one-time command converts a directory; a state directory holding both is refused.
A weekly commit then rewrites only the records that changed, not the whole file, and the 100 MB per-file limit stops being reachable by growth alone.
This also answers the question #1011 left open: the export is rewritten and committed in full for a single file, so SQLite alone would not have reduced the Git cost; sharding does.
The export is what Git holds as the remote record.

**The run server is the single writer, at cutover and after.**
ADR 0022 already makes the server the only machine that runs the scan and pushes state.
The State Store keeps that rule: the run server writes the database and the export, and no other machine does.
The run lock (`.auto-scout-workflow.lock`) stays as the only coordination.

**The decision log is its own table.**
#1053 records each confirmed editor decision with its date and a digest of the description she saw, and keeps earlier decisions.
Under the SQLite backend that is a table with Candidate ID, status, note, decision date and description digest, not a list inside the per-record blob.
#929 backfills older decisions from Git history in the same shape.
Its readers are the editorial memory prototype (#821), ordering by rejection reasons (#895) and the MCP server (#1029).

**Heavy fields may leave the record.**
`description_provenance`, `source_evidence`, `primary_signal` and `russian_edition_verification` are candidates for their own tables rather than the per-record JSON blob.
Whether each one does is decided in implementation, field by field.

**Each candidate carries a change marker.**
The candidate table has an indexed "last changed" value or content digest, so a derived index refreshes only what changed.
Which of the two is decided in implementation.

**Vectors stay outside the State Store (decided 2026-11-14).**
The editorial memory index (#821) is a separate SQLite file with the `sqlite-vec` extension; on the run server extension loading works with the system Python, and a search over 30,000 vectors takes 124 ms.
The index is a reader only.
It needs a stable Candidate ID, the change marker and the dated decision log, and all three are named above.
It is not canonical state: it is not exported, not committed and not backed up, and a lost index is rebuilt from the State Store.
Its update runs as its own step after the nightly scan, so the scan does not depend on the embedding model.

## What stays outside

These keep their present form and are not part of the State Store:

- run artifacts;
- pre-collection results;
- the per-source seen ledgers;
- source row history;
- runs and metrics;
- the #821 vector index.

The weekly state directory also tracks 56 raw page captures and 48 extraction files, several over 1 MB.
They are evidence and not State Store collections.
Whether they stay in Git is not decided here; #1011 leaves it for cutover.

## Consequences

- The Git cost of a week follows the number of records that changed, not the size of the history, and the largest file in Git stops growing with the catalogue.
- A change of record shape is made once, in the typed records of the seam, instead of in every module that reads the JSON.
- There are two copies of canonical state, the database and the export, and only the export is pushed.
- Until cutover the JSON backend is the one the weekly run uses, so nothing changes for the editor.
- ADR 0007 still holds for who operates the system and where the remote record lives. Only its statement that the canonical store is a set of JSON files in Git no longer does.
- #1018 is small and touches some of the same modules, so it lands before the call-site migration. #1019, #1020 and the rest of the audit of 2026-11-10 are merged.
