"""Resolve returned review rows to durable Candidate identities (Issue #851).

An editor returns a review as Markdown, CSV, or a list of rows. Each row is
matched to Candidate History in a fixed order and gets exactly one result:

1. durable Candidate ID, full or short; a recorded redirect counts as the
   same identity;
2. exact identity key, including a key the alias table assigns to an ID;
3. title plus author;
4. title alone.

A step that finds more than one identity reports every alternative as
``ambiguous`` and never picks one. Title matching is a fallback that is
always named in ``matched_by``, never a silent pick. The resolver only reads
state: it writes no file and has no Git, network, or model side effects.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoscout.candidate_identity import effective_candidate_id
from autoscout.identity import normalize_author_identity
from autoscout.identity import normalize_work_title
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import open_state_store

RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"
NOT_FOUND = "not_found"

_FULL_ID = re.compile(r"^candidate-[0-9a-f]+$")
# Shorter tails than six hex characters match too many IDs to be useful.
_SHORT_ID = re.compile(r"^[0-9a-f]{6,}$")

# Column names per input shape. In editor-facing.csv "Candidate ID" holds the
# identity key and "Durable Candidate ID" the durable ID; the Markdown review
# shows the short durable ID in its "ID" column.
_DURABLE_ID_FIELDS = ("durable_candidate_id", "candidate_id", "Durable Candidate ID", "ID")
_IDENTITY_KEY_FIELDS = ("identity_key", "Candidate ID")
_TITLE_FIELDS = ("title", "Title", "Название")
_AUTHOR_FIELDS = ("author", "Author", "Автор")


class ReviewRowsInputError(ValueError):
    """The returned rows cannot be read."""


@dataclass(frozen=True)
class ReturnedRow:
    durable_id: str
    identity_key: str
    title: str
    author: str
    review_number: str

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> ReturnedRow:
        def first(fields: tuple[str, ...]) -> str:
            for field in fields:
                text = str(value.get(field, "") or "").strip()
                if text:
                    return text
            return ""

        return cls(
            durable_id=first(_DURABLE_ID_FIELDS).lower(),
            identity_key=first(_IDENTITY_KEY_FIELDS),
            title=first(_TITLE_FIELDS),
            author=first(_AUTHOR_FIELDS),
            review_number=first(("#",)),
        )


def _markdown_cells(line: str) -> list[str]:
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") and not body.endswith("\\|") else body
    # A pipe inside a cell is escaped as ``\|`` (GFM) or ``&#124;``.
    cells = re.split(r"(?<!\\)\|", body)
    return [cell.strip().replace("\\|", "|").replace("&#124;", "|") for cell in cells]


def _markdown_rows(text: str) -> list[dict[str, str]]:
    """Read every pipe table whose header names an ID, a title, or an identity key."""
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for line in text.splitlines():
        # GFM allows a table without leading and trailing pipes.
        if "|" not in line:
            header = None
            continue
        cells = _markdown_cells(line)
        if header is None:
            known = {*_DURABLE_ID_FIELDS, *_IDENTITY_KEY_FIELDS, *_TITLE_FIELDS}
            header = cells if any(cell in known for cell in cells) else None
            continue
        if all(re.fullmatch(r":?-+:?", cell) for cell in cells if cell):
            continue
        rows.append(dict(zip(header, cells, strict=False)))
    return rows


def read_returned_rows(path: Path) -> list[ReturnedRow]:
    """Read returned rows from a JSON list, an editor CSV, or a Markdown review."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ReviewRowsInputError(f"cannot read returned rows: {path.name}") from exc
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ReviewRowsInputError(f"invalid JSON: {path.name}") from exc
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ReviewRowsInputError("returned rows JSON must be a list of objects")
        raw_rows: list[dict[str, Any]] = value
    elif suffix == ".csv":
        raw_rows = list(csv.DictReader(text.splitlines()))
    elif suffix in {".md", ".markdown"}:
        raw_rows = _markdown_rows(text)
    else:
        raise ReviewRowsInputError("returned rows must be a .json, .csv, or .md file")
    if not raw_rows and text.strip():
        # A nonempty file that yields no rows is unreadable, not "all resolved".
        raise ReviewRowsInputError(f"no review rows recognized in {path.name}")
    return [ReturnedRow.from_mapping(row) for row in raw_rows]


_DISPLAY_SEPARATOR = re.compile(r",|&|\s/\s")


def _display_people(display: str) -> tuple[str, ...]:
    """Split an author display into comparable parts, the same way on both sides.

    A returned row carries the review's comma-joined display, not the stored
    list, so the row and the record are split by one rule. No surname flip is
    applied: two identities whose displays look the same both match. The
    parts keep their order, because a surname-first display spreads one
    person over two parts: ``Smith, John, Doe, Jane`` and ``Smith, Jane,
    Doe, John`` name different people.
    """
    return tuple(
        part for part in (normalize_author_identity(piece) for piece in _DISPLAY_SEPARATOR.split(display)) if part
    )


def _author_display(record: dict[str, Any]) -> str:
    authors = record.get("primary_authors", [])
    if isinstance(authors, list):
        return ", ".join(str(author) for author in authors if str(author).strip())
    return str(authors or "")


def _summary(record: dict[str, Any]) -> dict[str, str]:
    return {
        "candidate_id": str(record.get("candidate_id", "")),
        "identity_key": str(record.get("identity_key", "")),
        "title": str(record.get("title", "")),
        "author_display": _author_display(record),
        "editor_status": str(record.get("editor_status", "")),
    }


def _record_ref(record: dict[str, Any]) -> str:
    """One identity per record: its durable ID, or its identity key when it has none."""
    candidate_id = str(record.get("candidate_id", "")).strip()
    return candidate_id or "identity_key:" + str(record.get("identity_key", "")).strip()


class ReviewRowResolver:
    """Read-only lookups over one Candidate History and its alias table."""

    def __init__(self, history: list[dict[str, Any]], alias_table: dict[str, Any]) -> None:
        self._alias_table = alias_table
        # A legacy record without a durable ID still takes part in identity-key
        # and title matching, so it is never dropped from the alternatives.
        self._records = [
            record
            for record in history
            if isinstance(record, dict)
            and (str(record.get("candidate_id", "")).strip() or str(record.get("identity_key", "")).strip())
        ]
        self._by_id = {
            str(record["candidate_id"]).strip(): record
            for record in self._records
            if str(record.get("candidate_id", "")).strip()
        }
        self._by_key = {
            str(record.get("identity_key", "")).strip(): record
            for record in self._records
            if str(record.get("identity_key", "")).strip()
        }
        candidates = alias_table.get("candidates", {})
        redirects = alias_table.get("redirects", {})
        self._known_ids = sorted(
            {
                *self._by_id,
                *(candidates if isinstance(candidates, dict) else {}),
                *(redirects if isinstance(redirects, dict) else {}),
            }
        )
        self._by_title: dict[str, list[dict[str, Any]]] = {}
        for record in self._records:
            stored_title = str(record.get("title", "")) or str(record.get("identity_key", "")).split("|", 1)[0]
            title = normalize_work_title(stored_title)
            if title:
                self._by_title.setdefault(title, []).append(record)

    def _records_for_ids(self, candidate_ids: list[str]) -> list[dict[str, Any]]:
        effective = sorted({effective_candidate_id(candidate_id, self._alias_table) for candidate_id in candidate_ids})
        return [self._by_id[candidate_id] for candidate_id in effective if candidate_id in self._by_id]

    def _by_durable_id(self, value: str) -> tuple[str, list[dict[str, Any]]]:
        if _FULL_ID.match(value):
            return "durable_id", self._records_for_ids([value] if value in self._known_ids else [])
        digest = value.removeprefix("…").removeprefix("...")
        if _SHORT_ID.match(digest):
            matches = [candidate_id for candidate_id in self._known_ids if candidate_id.endswith(digest)]
            return "short_durable_id", self._records_for_ids(matches)
        return "durable_id", []

    def _by_identity_key(self, value: str) -> list[dict[str, Any]]:
        record = self._by_key.get(value)
        if record is not None:
            return [record]
        aliases = self._alias_table.get("aliases", {})
        alias_id = aliases.get(value) if isinstance(aliases, dict) else None
        return self._records_for_ids([str(alias_id)]) if alias_id else []

    def _by_title_author(self, row: ReturnedRow) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        same_title = list(self._by_title.get(normalize_work_title(row.title), []))
        if not row.author:
            return same_title, []
        wanted = _display_people(row.author)
        # Every person must agree: a shared surname or one of several
        # co-authors is not enough to pick one identity over another.
        with_author = [record for record in same_title if wanted and wanted == _display_people(_author_display(record))]
        return same_title, with_author

    def resolve(self, row: ReturnedRow, number: int) -> dict[str, Any]:
        result: dict[str, Any] = {
            "row": number,
            "input": {
                "review_number": row.review_number,
                "durable_id": row.durable_id,
                "identity_key": row.identity_key,
                "title": row.title,
                "author": row.author,
            },
            "status": NOT_FOUND,
            "matched_by": "",
            "warnings": [],
        }
        steps: list[tuple[str, list[dict[str, Any]]]] = []
        if row.durable_id:
            method, matches = self._by_durable_id(row.durable_id)
            steps.append((method, matches))
            if not matches:
                result["warnings"].append("durable_id_not_found")
        if row.identity_key:
            matches = self._by_identity_key(row.identity_key)
            steps.append(("identity_key", matches))
            if not matches:
                result["warnings"].append("identity_key_not_found")
        exact = [(method, matches) for method, matches in steps if matches]
        if exact:
            method, matches = exact[0]
            agreeing = {_record_ref(record): record for _method, found in exact for record in found}
            if len(agreeing) > 1:
                # A durable ID and an identity key that name different
                # identities are a conflict for the editor, not a pick.
                result["status"] = AMBIGUOUS
                result["matched_by"] = "+".join(name for name, _found in exact)
                result["warnings"].append("identifiers_disagree")
                result["alternatives"] = [_summary(agreeing[ref]) for ref in sorted(agreeing)]
                return result
            result["matched_by"] = method
            return self._finish(result, matches, row)
        if not row.title:
            return result
        same_title, with_author = self._by_title_author(row)
        if with_author:
            result["matched_by"] = "title_author"
            finished = self._finish(result, with_author, row)
            others = [record for record in same_title if record not in with_author]
            if others:
                # The author picked one identity, but the title names others
                # too; the editor sees them instead of a silent narrowing.
                finished["warnings"].append("other_identities_share_title")
                finished["same_title_alternatives"] = [_summary(record) for record in others]
            return finished
        if row.author and same_title:
            result["warnings"].append("author_not_matched")
        if same_title:
            result["matched_by"] = "title"
            return self._finish(result, same_title, row)
        return result

    def _finish(self, result: dict[str, Any], matches: list[dict[str, Any]], row: ReturnedRow) -> dict[str, Any]:
        unique = {_record_ref(record): record for record in matches}
        if len(unique) == 1:
            record = next(iter(unique.values()))
            result["status"] = RESOLVED
            result["candidate"] = _summary(record)
            if (
                row.durable_id
                and _FULL_ID.match(row.durable_id)
                and row.durable_id != str(record.get("candidate_id", "")).strip()
            ):
                result["warnings"].append("redirected_durable_id")
            if (
                row.title
                and result["matched_by"] not in {"title", "title_author"}
                and normalize_work_title(row.title) != normalize_work_title(str(record.get("title", "")))
            ):
                result["warnings"].append("title_differs")
        else:
            result["status"] = AMBIGUOUS
            result["alternatives"] = [_summary(unique[candidate_id]) for candidate_id in sorted(unique)]
        return result


def resolve_review_rows(
    rows: list[ReturnedRow], state_dir: Path, state_store: StateStore | None = None
) -> dict[str, Any]:
    store = open_state_store(state_dir) if state_store is None else state_store
    history = store.read(Collection.CANDIDATE_HISTORY)
    if not isinstance(history, list):
        raise ReviewRowsInputError("candidate history must be a list")
    resolver = ReviewRowResolver(history, store.read(Collection.CANDIDATE_IDENTITY_ALIASES))
    results = [resolver.resolve(row, number) for number, row in enumerate(rows, start=1)]
    counts = {
        status: sum(result["status"] == status for result in results) for status in (RESOLVED, AMBIGUOUS, NOT_FOUND)
    }
    return {"row_count": len(results), "counts": counts, "results": results}


def resolve_review_rows_command(rows_path: Path, state_dir: Path) -> int:
    """Print one JSON result per returned row.

    Exit code 0 means every row resolved, 1 that at least one row is
    ambiguous or not found and needs the editor, and 2 an input error.
    """
    try:
        report = resolve_review_rows(read_returned_rows(rows_path), state_dir)
    except (ReviewRowsInputError, ValueError) as exc:
        print(json.dumps({"status": "input_error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["counts"][RESOLVED] == report["row_count"] else 1
