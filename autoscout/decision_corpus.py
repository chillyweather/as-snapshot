"""Export a dated, frozen decision corpus for the editorial memory prototype (issues #929, #821).

One record per book the editor has decided, keyed by its durable Candidate
ID. Each record carries the editor's decisions as one log in the shape of
``autoscout.editor_decisions`` (#1053): decisions recorded with a date since
#1053 are taken from the record; earlier decisions are rebuilt from Git
history, dated with the first commit on the main line where each one appears,
with the digest of the description in that commit.

The export is read-only. Current state is read through the State Store, past
versions through ``autoscout.candidate_history_git``; the output goes to a
local, Git-ignored directory as ``decisions.jsonl`` plus ``manifest.json``.
The field reference is ``docs/editorial-memory/decision-corpus.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from autoscout.candidate_history_git import GitHistoryError
from autoscout.candidate_history_git import HistoryVersion
from autoscout.candidate_history_git import commit_date
from autoscout.candidate_history_git import history_versions
from autoscout.candidate_history_git import repository_root
from autoscout.candidate_history_git import resolve_commit
from autoscout.candidate_identity import candidate_id_for
from autoscout.candidate_identity import effective_candidate_id
from autoscout.candidate_identity import ensure_alias_table
from autoscout.candidate_identity import resolve_candidate_id
from autoscout.content_description import current_english_description
from autoscout.editor_decisions import EDITOR_STATUSES
from autoscout.editor_decisions import decision_entry
from autoscout.editor_decisions import decision_log
from autoscout.editor_decisions import description_digest
from autoscout.feedback_dimensions import UNSPECIFIED
from autoscout.feedback_dimensions import dimensions_from_record
from autoscout.feedback_dimensions import is_bare_refusal
from autoscout.state_store import Collection
from autoscout.state_store import open_state_store

FORMAT_VERSION = 1
DEFAULT_OUTPUT_DIR = PurePosixPath("data/decision-corpus")
RECORDS_FILE = "decisions.jsonl"
MANIFEST_FILE = "manifest.json"
# Repository directories the export never writes into.
PROTECTED_TOP_LEVEL_DIRS = ("runs", "configs")

# Why a decision has no date. The export never guesses one.
UNDATED_PRESENT_IN_OLDEST_VERSION = "present_in_oldest_version"
UNDATED_FIRST_SEEN_IN_MERGE_COMMIT = "first_seen_in_merge_commit"
UNDATED_NOT_IN_GIT_HISTORY = "not_in_git_history"

NOTE_TYPES = (
    "already_known",
    "no_note",
    "bare_refusal",
    "rights_availability",
    "quality_data_problem",
    "taste_reason",
    "unknown",
)

type Decision = tuple[str, str]


class DecisionCorpusError(ValueError):
    """The export was refused before anything was written."""


@dataclass
class _Entry:
    """One decision rebuilt from Git, with what the editor saw when it first appeared."""

    status: str
    note: str
    decision_date: str | None
    digest: str | None
    commit: str
    undated_reason: str | None
    seen: dict[str, Any]

    def logged(self) -> dict[str, str | None]:
        return decision_entry(self.status, self.note, self.decision_date, self.digest)


@dataclass
class _BookHistory:
    entries: list[_Entry] = field(default_factory=list)
    shown: set[Decision] = field(default_factory=set)
    # How many rebuilt entries came before the first version whose record
    # carries a dated #1053 log entry; from there on the record's log is used.
    entries_before_log: int | None = None


def _decision(record: dict[str, Any]) -> Decision | None:
    status = str(record.get("editor_status", "") or "").strip()
    if status not in EDITOR_STATUSES:
        return None
    return status, str(record.get("editor_note", "") or "")


def _has_dated_log(record: dict[str, Any]) -> bool:
    return any(entry.get("decision_date") for entry in decision_log(record))


def _sources(record: dict[str, Any]) -> list[dict[str, str]]:
    evidence = record.get("source_evidence")
    if not isinstance(evidence, list) or not evidence:
        signal = record.get("primary_signal")
        evidence = [signal] if isinstance(signal, dict) else []
    sources = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        source = {
            "source_name": str(item.get("source_name", "") or ""),
            "evidence_url": str(item.get("evidence_url", "") or ""),
        }
        if source not in sources:
            sources.append(source)
    return sources


def _authors(record: dict[str, Any]) -> list[str]:
    authors = record.get("primary_authors")
    return [str(author) for author in authors] if isinstance(authors, list) else []


def _seen(record: dict[str, Any], commit: str) -> dict[str, Any]:
    """What the record held in one commit: the book as the editor could see it then."""
    return {
        "commit": commit,
        "title": str(record.get("title", "") or ""),
        "english_title": str(record.get("english_title", "") or ""),
        "authors": _authors(record),
        "description": str(record.get("content_description", "") or ""),
        "english_description": current_english_description(record),
        "description_digest": description_digest(record),
        "sources": _sources(record),
    }


class _Resolver:
    """Map a record of any version to its durable Candidate ID under today's alias table."""

    def __init__(self, table: dict[str, Any]) -> None:
        self._table = table
        self._cache: dict[str, str] = {}

    def __call__(self, record: dict[str, Any]) -> str:
        key = json.dumps(
            [
                record.get("candidate_id"),
                record.get("identity_key"),
                record.get("title"),
                record.get("primary_authors"),
                record.get("isbn"),
            ],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        cached = self._cache.get(key)
        if cached is None:
            cached = resolve_candidate_id(record, self._table) or self._fallback(record)
            self._cache[key] = cached
        return cached

    def _fallback(self, record: dict[str, Any]) -> str:
        explicit = str(record.get("candidate_id", "") or "").strip()
        if explicit:
            return effective_candidate_id(explicit, self._table)
        return effective_candidate_id(candidate_id_for(str(record.get("identity_key", "") or "").strip()), self._table)


def rebuild_decisions(
    versions: Iterable[HistoryVersion], resolve: Callable[[dict[str, Any]], str]
) -> dict[str, _BookHistory]:
    """Rebuild each book's decisions from the versions of Candidate History, oldest first.

    A decision (status and verbatim note) is new for a book when no record of
    that book showed it in the version before. Before duplicates are merged a
    book has several records in one version, so it shows the set of their
    decisions; the merge that keeps one of them logs it again, so the last
    entry is always the decision the book shows. A decision that disappears
    and comes back is a new decision again.
    """
    books: dict[str, _BookHistory] = {}
    first = True
    for version in versions:
        shown_now: dict[str, dict[Decision, dict[str, Any]]] = {}
        logged_now: set[str] = set()
        for record in version.records:
            decision = _decision(record)
            if decision is None:
                continue
            candidate_id = resolve(record)
            shown_now.setdefault(candidate_id, {}).setdefault(decision, record)
            if _has_dated_log(record):
                logged_now.add(candidate_id)
        for candidate_id in set(books) - set(shown_now):
            books[candidate_id].shown = set()
        for candidate_id, decisions in shown_now.items():
            book = books.setdefault(candidate_id, _BookHistory())
            if candidate_id in logged_now and book.entries_before_log is None:
                book.entries_before_log = len(book.entries)
            added = sorted(set(decisions) - book.shown)
            if not added and set(decisions) < book.shown:
                # Duplicates merged into one book and the merge kept some of
                # their decisions: like a confirmed merge since #1053, the
                # kept decision is logged again, dated with the merge.
                added = sorted(decisions)
            for decision in added:
                book.entries.append(_entry(decision, decisions[decision], version, first))
            book.shown = set(decisions)
        first = False
    return books


def _entry(decision: Decision, record: dict[str, Any], version: HistoryVersion, first: bool) -> _Entry:
    reason = None
    if first:
        reason = UNDATED_PRESENT_IN_OLDEST_VERSION
    elif version.is_merge:
        reason = UNDATED_FIRST_SEEN_IN_MERGE_COMMIT
    seen = _seen(record, version.commit)
    return _Entry(
        status=decision[0],
        note=decision[1],
        decision_date=None if reason else version.date,
        digest=None if reason else seen["description_digest"],
        commit=version.commit,
        undated_reason=reason,
        seen=seen,
    )


def note_type(record: dict[str, Any]) -> str:
    """Label the latest note from existing fields only; ``unknown`` rather than a guess.

    ``already_known`` is the editor status. ``no_note`` and ``bare_refusal``
    are read from the note itself, the latter with the bounded #678 list.
    The other labels come only from confirmed ``feedback_dimensions``.
    """
    status = str(record.get("editor_status", "") or "")
    note = str(record.get("editor_note", "") or "")
    if status == "already_known":
        return "already_known"
    if not note.strip():
        return "no_note"
    if is_bare_refusal(note):
        return "bare_refusal"
    dims = dimensions_from_record(record) or {}
    if dims.get("rights_availability") == "unavailable":
        return "rights_availability"
    if dims.get("quality_problem") not in (None, UNSPECIFIED, "none"):
        return "quality_data_problem"
    if dims.get("editorial_appeal") not in (None, UNSPECIFIED):
        return "taste_reason"
    return "unknown"


def _aliases(table: dict[str, Any]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    keys: dict[str, set[str]] = {}
    for alias, stored in (table.get("aliases") or {}).items():
        if isinstance(stored, str) and stored:
            keys.setdefault(effective_candidate_id(stored, table), set()).add(str(alias))
    retired: dict[str, set[str]] = {}
    for old in table.get("redirects") or {}:
        retired.setdefault(effective_candidate_id(str(old), table), set()).add(str(old))
    return (
        {candidate_id: sorted(values) for candidate_id, values in keys.items()},
        {candidate_id: sorted(values) for candidate_id, values in retired.items()},
    )


def _latest_log(
    record: dict[str, Any], book: _BookHistory | None
) -> tuple[list[dict[str, Any]], _Entry | None, str | None]:
    """Return the record's one decision log, the rebuilt entry behind its latest decision, and why it is undated.

    The last log entry is always the decision the record shows.
    """
    latest = _decision(record)
    assert latest is not None
    entries = list(book.entries) if book else []
    recorded = [dict(entry) for entry in decision_log(record) if entry.get("decision_date")]
    if recorded:
        cut = book.entries_before_log if book and book.entries_before_log is not None else len(entries)
        log = [entry.logged() for entry in entries[:cut]] + recorded
        seen_entry = next((entry for entry in reversed(entries[cut:]) if (entry.status, entry.note) == latest), None)
        if (log[-1].get("status"), log[-1].get("note")) == latest:
            return log, seen_entry, None
    matching = [index for index, entry in enumerate(entries) if (entry.status, entry.note) == latest]
    if not matching or book is None or latest not in book.shown:
        log = [entry.logged() for entry in entries]
        log.append(decision_entry(latest[0], latest[1], None, None))
        return log, None, UNDATED_NOT_IN_GIT_HISTORY
    # A merge can keep a decision that appeared before another one; the
    # decision the record shows still goes last, with its own date.
    chosen = entries[matching[-1]]
    ordered = [entry for index, entry in enumerate(entries) if index != matching[-1]] + [chosen]
    log = [entry.logged() for entry in ordered]
    return log, chosen, chosen.undated_reason


def build_corpus(
    history: list[dict[str, Any]], alias_table: dict[str, Any], versions: Iterable[HistoryVersion]
) -> tuple[list[dict[str, Any]], int]:
    """Return the corpus records, sorted by Candidate ID, and the number of rebuilt books no longer in history."""
    table = ensure_alias_table(alias_table)
    resolve = _Resolver(table)
    books = rebuild_decisions(versions, resolve)
    alias_keys, retired_ids = _aliases(table)
    records: list[dict[str, Any]] = []
    present: set[str] = set()
    for record in history:
        if _decision(record) is None:
            continue
        candidate_id = resolve(record)
        present.add(candidate_id)
        log, entry, undated_reason = _latest_log(record, books.get(candidate_id))
        latest = log[-1]
        records.append(
            {
                "candidate_id": candidate_id,
                "identity_key": str(record.get("identity_key", "") or ""),
                "aliases": sorted(set(alias_keys.get(candidate_id, [])) - {str(record.get("identity_key", ""))}),
                "retired_candidate_ids": retired_ids.get(candidate_id, []),
                "title": str(record.get("title", "") or ""),
                "authors": _authors(record),
                "editor_status": latest["status"],
                "editor_note": latest["note"],
                "decision_date": latest["decision_date"],
                "undated_reason": undated_reason if latest["decision_date"] is None else None,
                "decision_commit": entry.commit if entry else None,
                "note_type": note_type(record),
                "feedback_dimensions": dimensions_from_record(record),
                "seen": entry.seen if entry and latest["decision_date"] is not None else None,
                "editor_decisions": log,
            }
        )
    records.sort(key=lambda item: item["candidate_id"])
    orphans = sum(1 for candidate_id, book in books.items() if candidate_id not in present and book.entries)
    return records, orphans


def _records_text(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)


def _write_atomically(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _checked_output_dir(output_dir: Path, repo_root: Path, state_dir: Path) -> Path:
    resolved = output_dir.resolve()
    forbidden = [repo_root / name for name in PROTECTED_TOP_LEVEL_DIRS] + [state_dir.resolve()]
    for directory in forbidden:
        if resolved == directory or directory in resolved.parents:
            raise DecisionCorpusError(f"output_dir_not_allowed: the export never writes inside {directory}")
    return resolved


@dataclass(frozen=True)
class ExportResult:
    export_dir: Path
    manifest: dict[str, Any]


def export_decision_corpus(
    state_dir: Path, output_dir: Path | None = None, ref: str = "HEAD", now: datetime | None = None
) -> ExportResult:
    """Write the corpus for the commit ``ref`` names; return where it went and its manifest."""
    started = time.monotonic()
    state_dir = state_dir.resolve()
    try:
        repo_root = repository_root(state_dir).resolve()
        commit = resolve_commit(repo_root, ref)
    except GitHistoryError as error:
        raise DecisionCorpusError(f"git_unavailable: {error}") from error
    try:
        state_rel = PurePosixPath(state_dir.relative_to(repo_root).as_posix())
    except ValueError as error:
        raise DecisionCorpusError("state_dir_outside_repository") from error
    target_root = _checked_output_dir(output_dir or repo_root / DEFAULT_OUTPUT_DIR, repo_root, state_dir)

    store = open_state_store(state_dir)
    history = [record for record in store.read(Collection.CANDIDATE_HISTORY) if isinstance(record, dict)]
    alias_table = store.read(Collection.CANDIDATE_IDENTITY_ALIASES)

    versions_read: list[str] = []
    last_records: list[dict[str, Any]] = []

    def versions() -> Iterator[HistoryVersion]:
        nonlocal last_records
        for version in history_versions(repo_root, commit, state_rel):
            versions_read.append(version.commit)
            last_records = version.records
            yield version

    records, orphans = build_corpus(history, alias_table, versions())
    undated = Counter(record["undated_reason"] for record in records if record["decision_date"] is None)
    text = _records_text(records)
    export_dir = target_root / f"{commit_date(repo_root, commit)}-{commit[:12]}"
    export_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format_version": FORMAT_VERSION,
        "source_commit": commit,
        "state_dir": state_rel.as_posix(),
        "history_versions_read": len(versions_read),
        "first_history_commit": versions_read[0] if versions_read else None,
        "last_history_commit": versions_read[-1] if versions_read else None,
        "state_matches_source_commit": _same_records(history, last_records),
        "record_count": len(records),
        "dated_count": len(records) - sum(undated.values()),
        "undated_count": sum(undated.values()),
        "undated_by_reason": dict(sorted(undated.items())),
        "note_type_counts": dict(sorted(Counter(record["note_type"] for record in records).items())),
        "decided_books_no_longer_in_history": orphans,
        "records_file": RECORDS_FILE,
        "records_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "exported_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
    }
    _write_atomically(export_dir / RECORDS_FILE, text)
    _write_atomically(
        export_dir / MANIFEST_FILE, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    manifest = {**manifest, "elapsed_seconds": round(time.monotonic() - started, 1)}
    return ExportResult(export_dir, manifest)


def _same_records(current: list[dict[str, Any]], committed: list[dict[str, Any]]) -> bool:
    """Whether the current state is the version the source commit holds, ignoring record order."""

    def keyed(records: list[dict[str, Any]]) -> list[str]:
        return sorted(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records)

    return keyed(current) == keyed(committed)


def export_decision_corpus_command(state_dir: Path, output_dir: Path | None, ref: str) -> int:
    """CLI delegate: 0 when the export was written, 2 when it was refused."""
    try:
        result = export_decision_corpus(state_dir, output_dir, ref)
    except DecisionCorpusError as error:
        print(f"reason_code={str(error).split(':', 1)[0]}")
        print(f"error={error}")
        return 2
    manifest = result.manifest
    print(f"export_dir={result.export_dir}")
    for key in (
        "source_commit",
        "history_versions_read",
        "state_matches_source_commit",
        "record_count",
        "dated_count",
        "undated_count",
    ):
        print(f"{key}={json.dumps(manifest[key]) if isinstance(manifest[key], bool) else manifest[key]}")
    for reason, count in manifest["undated_by_reason"].items():
        print(f"undated_{reason}={count}")
    print(f"decided_books_no_longer_in_history={manifest['decided_books_no_longer_in_history']}")
    print(f"elapsed_seconds={manifest['elapsed_seconds']}")
    return 0
