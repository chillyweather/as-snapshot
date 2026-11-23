"""Tell whether a candidate source lists books AutoScout has not met (Issue #896).

``probe-source`` proves a page can be fetched and parsed. It cannot say
whether the books on that page are new to AutoScout, so a night of unattended
scouting could collect many sources that never surface a new book. This
module answers that one question, offline and deterministically.

The input is a bounded sample of the entries the scouting agent read from the
candidate page. Each entry is matched against Candidate History with the
existing identity rules (ISBN, or the same normalized work title and one
shared author), and gets exactly one class, decided in this closed order:

* ``invalid`` - no title, or no author that names a person.
* ``duplicate`` - an earlier sample entry is the same work, by the same rules
  as below. A page that repeats one book across sections or formats lists one
  book, not several.
* ``known`` - Candidate History holds the same work. The sample may give the
  original title of a translation, and a history record its English title;
  either pairing counts.
* ``out_of_scope`` - the entry is not nonfiction.
* ``translation`` - the entry is a translation. Its rights belong to the
  original publisher, and an English original would reach AutoScout through
  its English sources, so it is not counted as a find of this source.
* ``stale`` - published before the year preceding the run year.
* ``known_author`` - no work matches, but an author does. This is often the
  same book under a translated title, so it is reported for a person to look
  at and not counted as new.
* ``new`` - everything else.

The verdict is ``promising`` when at least ``PROMISING_MIN_NEW`` entries are
new and they are at least ``PROMISING_MIN_SHARE_PERCENT`` percent of the valid
entries, ``weak`` when at least one is new, and ``no_novelty`` otherwise.
The command writes nothing and has no Git, network, or model side effects.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.author_admission import admitted_author_people
from autoscout.author_admission import is_placeholder_author
from autoscout.identity import identity_isbn
from autoscout.identity import normalized_contributor_set
from autoscout.identity import normalized_title
from autoscout.state_store import Collection
from autoscout.state_store import open_state_store

SAMPLE_VERSION = 1
MAX_SAMPLE_ENTRIES = 200
MAX_AUTHOR_MATCHES_SHOWN = 3

PROMISING_MIN_NEW = 5
PROMISING_MIN_SHARE_PERCENT = 30

CLASS_INVALID = "invalid"
CLASS_DUPLICATE = "duplicate"
CLASS_KNOWN = "known"
CLASS_OUT_OF_SCOPE = "out_of_scope"
CLASS_TRANSLATION = "translation"
CLASS_STALE = "stale"
CLASS_KNOWN_AUTHOR = "known_author"
CLASS_NEW = "new"
CLASSES = (
    CLASS_INVALID,
    CLASS_DUPLICATE,
    CLASS_KNOWN,
    CLASS_OUT_OF_SCOPE,
    CLASS_TRANSLATION,
    CLASS_STALE,
    CLASS_KNOWN_AUTHOR,
    CLASS_NEW,
)

VERDICT_PROMISING = "promising"
VERDICT_WEAK = "weak"
VERDICT_NO_NOVELTY = "no_novelty"

# The positive decisions of the 2026-10-30 yield research.
POSITIVE_STATUSES = frozenset({"interested", "maybe_watch", "needs_deeper_analysis"})
DECISION_BUCKETS = ("positive", "already_known", "reject", "undecided")

_SAMPLE_KEYS = frozenset({"version", "source_name", "url", "language", "entries"})
_ENTRY_KEYS = frozenset(
    {"title", "authors", "original_title", "isbn", "publication_year", "nonfiction", "translated_from"}
)


class SampleError(ValueError):
    """The sample file does not follow the documented shape."""


def load_sample(path: Path) -> dict[str, Any]:
    """Read and validate one sample file."""
    try:
        sample = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SampleError(f"sample is not readable JSON: {exc}") from exc
    if not isinstance(sample, dict):
        raise SampleError("sample must be a JSON object")
    unknown = sorted(set(sample) - _SAMPLE_KEYS)
    if unknown:
        raise SampleError(f"sample has unknown keys: {', '.join(unknown)}")
    if sample.get("version") != SAMPLE_VERSION:
        raise SampleError(f"sample version must be {SAMPLE_VERSION}")
    for key in ("source_name", "url", "language"):
        if not isinstance(sample.get(key), str) or not sample[key].strip():
            raise SampleError(f"sample {key} must be a nonblank string")
    entries = sample.get("entries")
    if not isinstance(entries, list) or not entries:
        raise SampleError("sample entries must be a nonempty list")
    if len(entries) > MAX_SAMPLE_ENTRIES:
        raise SampleError(f"sample holds {len(entries)} entries; at most {MAX_SAMPLE_ENTRIES} are read")
    for index, entry in enumerate(entries):
        _validate_entry(index, entry)
    return sample


def _validate_entry(index: int, entry: Any) -> None:
    where = f"entry {index}"
    if not isinstance(entry, dict):
        raise SampleError(f"{where} must be an object")
    unknown = sorted(set(entry) - _ENTRY_KEYS)
    if unknown:
        raise SampleError(f"{where} has unknown keys: {', '.join(unknown)}")
    if not isinstance(entry.get("title"), str):
        raise SampleError(f"{where} title must be a string")
    authors = entry.get("authors")
    if not isinstance(authors, list) or not all(isinstance(author, str) for author in authors):
        raise SampleError(f"{where} authors must be a list of strings")
    if not isinstance(entry.get("nonfiction"), bool):
        raise SampleError(f"{where} nonfiction must be true or false")
    for key in ("original_title", "isbn", "translated_from"):
        if key in entry and not isinstance(entry[key], str):
            raise SampleError(f"{where} {key} must be a string")
    year = entry.get("publication_year")
    if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
        raise SampleError(f"{where} publication_year must be an integer or null")


def _mention(title: str, authors: Iterable[Any], isbn: str = "") -> dict[str, Any]:
    return {"title": title, "primary_authors": [str(author) for author in authors], "isbn": isbn}


def _record_identity(record: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The titles and authors a history record is matched by.

    A legacy record may carry only its ``identity_key``, ``title|author &
    author``, with no display fields. Its key parts stand in, so a book it
    holds is still known.
    """
    authors = record.get("primary_authors")
    title = str(record.get("title") or "")
    if title and isinstance(authors, list) and authors:
        return [title, str(record.get("english_title") or "")], [str(author) for author in authors]
    key_title, separator, key_authors = str(record.get("identity_key") or "").partition("|")
    if not separator:
        return [], []
    return [key_title], [author for author in key_authors.split(" & ") if author.strip()]


class _HistoryIndex:
    """Candidate History keyed by normalized title, ISBN, and contributor."""

    def __init__(self, history: list[Any]) -> None:
        self.by_title: dict[str, list[dict[str, Any]]] = {}
        self.by_isbn: dict[str, list[dict[str, Any]]] = {}
        self.by_contributor: dict[str, list[dict[str, Any]]] = {}
        self.contributors: dict[int, frozenset[str]] = {}
        for record in history:
            if not isinstance(record, dict):
                continue
            record_titles, authors = _record_identity(record)
            contributors = normalized_contributor_set(_mention("", authors))
            self.contributors[id(record)] = contributors
            titles = {normalized_title(_mention(title, authors)) for title in record_titles}
            for title in sorted(titles - {""}):
                self.by_title.setdefault(title, []).append(record)
            # The record itself, so its discovery-only ISBN marker holds.
            isbn = identity_isbn(record)
            if isbn:
                self.by_isbn.setdefault(isbn, []).append(record)
            for contributor in sorted(contributors):
                self.by_contributor.setdefault(contributor, []).append(record)

    def work_matches(self, titles: set[str], contributors: frozenset[str], isbn: str) -> list[dict[str, Any]]:
        matched = {id(record): record for record in self.by_isbn.get(isbn, [])} if isbn else {}
        for title in titles:
            for record in self.by_title.get(title, []):
                if contributors & self.contributors[id(record)]:
                    matched[id(record)] = record
        return _sorted_records(matched.values())

    def author_matches(self, contributors: frozenset[str]) -> list[dict[str, Any]]:
        matched = {id(record): record for name in contributors for record in self.by_contributor.get(name, [])}
        return _sorted_records(matched.values())


def _sorted_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        records, key=lambda record: (str(record.get("candidate_id", "")), str(record.get("identity_key", "")))
    )


def _record_ref(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": str(record.get("candidate_id", "")),
        "title": str(record.get("title", "")),
        "editor_status": str(record.get("editor_status") or ""),
    }


def _decision_bucket(status: str) -> str:
    if status in POSITIVE_STATUSES:
        return "positive"
    if status in {"already_known", "reject"}:
        return status
    return "undecided"


_EntryIdentity = tuple[frozenset[str], frozenset[str], str]


def _authors(entry: dict[str, Any]) -> list[str]:
    """The people the copied author values name, admitted as candidate admission does."""
    people = [person for author in entry["authors"] for person in admitted_author_people(author)]
    return [person for person in people if not is_placeholder_author(person)]


def _entry_identity(entry: dict[str, Any]) -> _EntryIdentity:
    """Normalized titles, contributors, and identity ISBN of one sample entry."""
    authors = _authors(entry)
    titles = {normalized_title(_mention(entry.get(field, ""), authors)) for field in ("title", "original_title")}
    titles.discard("")
    contributors = normalized_contributor_set(_mention("", authors))
    return frozenset(titles), contributors, identity_isbn(_mention("", authors, entry.get("isbn", "")))


def _same_work(a: _EntryIdentity, b: _EntryIdentity) -> bool:
    return bool(a[2] and a[2] == b[2]) or bool(a[0] & b[0] and a[1] & b[1])


def classify_entry(entry: dict[str, Any], index: _HistoryIndex, run_year: int) -> dict[str, Any]:
    """Return the report row for one validated sample entry, before duplicates."""
    row: dict[str, Any] = {"title": entry["title"].strip(), "authors": _authors(entry)}
    titles, contributors, isbn = _entry_identity(entry)
    if not row["title"] or not titles or not contributors:
        return {**row, "class": CLASS_INVALID}
    work_matches = index.work_matches(set(titles), contributors, isbn)
    if work_matches:
        return {**row, "class": CLASS_KNOWN, "matches": [_record_ref(record) for record in work_matches]}
    if not entry["nonfiction"]:
        return {**row, "class": CLASS_OUT_OF_SCOPE}
    if entry.get("translated_from", "").strip():
        return {**row, "class": CLASS_TRANSLATION}
    year = entry.get("publication_year")
    if year is not None and year < run_year - 1:
        return {**row, "class": CLASS_STALE}
    author_matches = index.author_matches(contributors)
    if author_matches:
        return {
            **row,
            "class": CLASS_KNOWN_AUTHOR,
            "author_match_count": len(author_matches),
            "matches": [_record_ref(record) for record in author_matches[:MAX_AUTHOR_MATCHES_SHOWN]],
        }
    return {**row, "class": CLASS_NEW}


def novelty_report(sample: dict[str, Any], history: list[Any], run_date: date) -> dict[str, Any]:
    """Classify every sample entry and derive the counts and the verdict."""
    index = _HistoryIndex(history)
    rows: list[dict[str, Any]] = []
    seen: list[_EntryIdentity] = []
    for entry in sample["entries"]:
        row = classify_entry(entry, index, run_date.year)
        if row["class"] != CLASS_INVALID:
            identity = _entry_identity(entry)
            if any(_same_work(identity, earlier) for earlier in seen):
                row = {"title": row["title"], "authors": row["authors"], "class": CLASS_DUPLICATE}
            # A duplicate may add an ISBN a later edition shares, so it is kept.
            seen.append(identity)
        rows.append(row)
    counts = {name: 0 for name in CLASSES}
    for row in rows:
        counts[row["class"]] += 1
    decisions = {bucket: 0 for bucket in DECISION_BUCKETS}
    for row in rows:
        if row["class"] == CLASS_KNOWN:
            # One work may match several records; the decision closest to
            # positive counts, in DECISION_BUCKETS order.
            buckets = {_decision_bucket(match["editor_status"]) for match in row["matches"]}
            decisions[next(bucket for bucket in DECISION_BUCKETS if bucket in buckets)] += 1
    valid = len(rows) - counts[CLASS_INVALID] - counts[CLASS_DUPLICATE]
    new = counts[CLASS_NEW]
    if new >= PROMISING_MIN_NEW and new * 100 >= valid * PROMISING_MIN_SHARE_PERCENT:
        verdict = VERDICT_PROMISING
    elif new >= 1:
        verdict = VERDICT_WEAK
    else:
        verdict = VERDICT_NO_NOVELTY
    return {
        "status": "success",
        "source_name": sample["source_name"].strip(),
        "url": sample["url"].strip(),
        "language": sample["language"].strip(),
        "run_date": run_date.isoformat(),
        "verdict": verdict,
        "counts": counts,
        "valid_entry_count": valid,
        "new_share": round(new / valid, 3) if valid else None,
        "known_editor_decisions": decisions,
        "thresholds": {
            "promising_min_new": PROMISING_MIN_NEW,
            "promising_min_share_percent": PROMISING_MIN_SHARE_PERCENT,
        },
        "entries": rows,
    }


def source_novelty_command(sample_path: Path, state_dir: Path, run_date: str | None = None) -> int:
    """Print the novelty report. Exit 0 on a report, 2 on an input error."""
    try:
        effective_date = date.fromisoformat(run_date) if run_date else date.today()
    except ValueError:
        return _input_error(f"run date is not an ISO date: {run_date}")
    if not state_dir.is_dir():
        return _input_error(f"state directory not found: {state_dir}")
    try:
        sample = load_sample(sample_path)
        history = open_state_store(state_dir).read(Collection.CANDIDATE_HISTORY)
        if not history:
            # Without history every book reads as new.
            return _input_error(f"candidate history not found in {state_dir}")
    except (OSError, ValueError) as exc:
        return _input_error(str(exc))
    if not isinstance(history, list):
        return _input_error("candidate history must be a list")
    print(json.dumps(novelty_report(sample, history, effective_date), ensure_ascii=False, indent=2))
    return 0


def _input_error(message: str) -> int:
    print(json.dumps({"status": "input_error", "error": message}, ensure_ascii=False, indent=2))
    return 2
