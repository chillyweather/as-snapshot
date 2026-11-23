"""Would the age of the work improve the review order? Measured on decided books (issue #1141).

The work-age pass (#1138) records, per book, the year the work was first
published and how many editions Open Library knows. This report measures, on
the books the editor has already decided, whether moving old, often-published
works down the review would push rejected books out of the top without losing
wanted ones. It answers four questions:

1. How many decided books have a known work age, by the language of the list
   that found them.
2. The share of wanted books by age of the work and by edition count.
3. A simulation on the recorded ``fit-v2`` scores: for each threshold of age
   and edition count, books at or above both are moved below the others, and
   the report counts the rejected books that leave the top and the wanted
   books that are lost from it or gained by it.
4. The books the best threshold gets wrong, by title.

The threshold is chosen on one half of the decided books (the development
half) and checked on the other (the held-out half). The halves are fixed by a
digest of each book's durable Candidate ID, so the same state always gives the
same halves and the same report.

The age of the work is the year AutoScout first listed the book
(``first_seen_date``) minus the year the work was first published. Today's
date is never used, so the numbers do not drift as time passes.

The editor's decisions are read only to compute the report. Nothing here
writes state, calls a model or uses the network; the one output is the report
file. Editor notes are never read into the report.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.editorial_fit_shadow import NEGATIVE_STATUSES
from autoscout.editorial_fit_shadow import POSITIVE_STATUSES
from autoscout.fit_scores import QUESTION_SET_VERSION
from autoscout.fit_scores import book_keys
from autoscout.fit_scores import durable_id
from autoscout.fit_scores import recorded_fit_scores
from autoscout.language_codes import normalize_language_code
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import open_state_store
from autoscout.work_age import BASIS_ISBN
from autoscout.work_age import BASIS_ORIGINAL_EDITION
from autoscout.work_age import BASIS_TITLE_AUTHOR
from autoscout.work_age import STATUS_FOUND
from autoscout.work_age import STATUS_NOT_FOUND
from autoscout.work_age import work_age_of

WANTED = "wanted"
REJECTED = "rejected"
ALREADY_KNOWN = "already_known"

# Why a book has or has not a work age the simulation can use.
AGE_KNOWN = "known"
AGE_PARTLY_KNOWN = "partly_known"
AGE_NOT_FOUND = "not_found"
AGE_NOT_LOOKED_UP = "not_looked_up"

DEVELOPMENT = "development"
HELD_OUT = "held_out"
# Changing the salt reshuffles the halves and starts a new evaluation.
SPLIT_SALT = "work-age-report-1141"

# The top of the review: the highest-scored share of the books, as in the
# Recall@40% of the shadow report and the ranking benchmark.
TOP_FRACTION = 0.4

# The thresholds tried, side by side: a book is moved down when the work is at
# least this many years old AND Open Library knows at least this many editions.
AGE_THRESHOLDS = (1, 2, 5, 10, 25)
EDITION_THRESHOLDS = (1, 5, 10, 25, 50)

# Age bands, in years: (label, lowest, highest); ``None`` is open.
AGE_BANDS: tuple[tuple[str, int | None, int | None], ...] = (
    ("new (published the year it was listed, or later)", None, 0),
    ("1 year", 1, 1),
    ("2 to 5 years", 2, 5),
    ("older than 5 years", 6, None),
)
EDITION_BANDS: tuple[tuple[str, int | None, int | None], ...] = (
    ("1 edition", None, 1),
    ("2 to 4 editions", 2, 4),
    ("5 to 9 editions", 5, 9),
    ("10 to 49 editions", 10, 49),
    ("50 editions or more", 50, None),
)

# Rejected books the rule leaves in the top are listed when the work is at
# least this old: a younger work is one no age rule could move.
OLD_WORK_MIN_AGE = 1

LANGUAGE_NAMES = {
    "ca": "Catalan",
    "cs": "Czech",
    "el": "Greek",
    "he": "Hebrew",
    "hu": "Hungarian",
    "is": "Icelandic",
    "ro": "Romanian",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "da": "Danish",
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "fi": "Finnish",
    "fr": "French",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "nl": "Dutch",
    "no": "Norwegian",
    "nb": "Norwegian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ru": "Russian",
    "sv": "Swedish",
    "zh": "Chinese",
}
UNKNOWN_LANGUAGE = "unknown"

# Below this share of decided books with a known work age, a language's
# coverage is called out as low.
LOW_COVERAGE_SHARE = 0.5

# What a found work age was looked up by, in the order the report names them.
BASES_IN_ORDER = (BASIS_ORIGINAL_EDITION, BASIS_ISBN, BASIS_TITLE_AUTHOR)

# Below this many wanted books in a half, a difference of one or two books is
# within chance, and the report says so.
SMALL_HALF_WANTED = 30

_TOLERANCE = 1e-9


@dataclass(frozen=True)
class Book:
    """One decided book, with only the facts the report needs."""

    title: str
    authors: str
    language: str
    decision: str
    listing_year: int | None
    first_publish_year: int | None
    edition_count: int | None
    age_status: str
    score: float | None
    half: str
    # What the found work age was looked up by (``work_age.BASES``); "" when none was found.
    basis: str = ""

    @property
    def age(self) -> int | None:
        if self.listing_year is None or self.first_publish_year is None:
            return None
        return self.listing_year - self.first_publish_year

    @property
    def simulated(self) -> bool:
        """In the simulation: wanted or rejected, with a fit score, an age and an edition count."""
        return (
            self.decision in (WANTED, REJECTED)
            and self.score is not None
            and self.age is not None
            and self.edition_count is not None
        )


# ── Reading ──────────────────────────────────────────────────────────────


def _decision(status: Any) -> str | None:
    status = str(status or "").strip().lower()
    if status in POSITIVE_STATUSES:
        return WANTED
    if status in NEGATIVE_STATUSES:
        return REJECTED
    if status == ALREADY_KNOWN:
        return ALREADY_KNOWN
    return None


def _listing_year(record: Mapping[str, Any]) -> int | None:
    try:
        return date.fromisoformat(str(record.get("first_seen_date", ""))).year
    except ValueError:
        return None


def _language(record: Mapping[str, Any]) -> str:
    """The language of the list that first found the book."""
    evidence = record.get("source_evidence")
    for entry in evidence if isinstance(evidence, list) else []:
        if isinstance(entry, Mapping):
            code = normalize_language_code(entry.get("language"))
            if code:
                return code
    return UNKNOWN_LANGUAGE


def _authors(record: Mapping[str, Any]) -> str:
    authors = record.get("english_author_display") or record.get("primary_authors") or []
    if isinstance(authors, str):
        return authors.strip()
    return (
        ", ".join(str(author).strip() for author in authors if str(author).strip()) if isinstance(authors, list) else ""
    )


def _age_facts(record: Mapping[str, Any]) -> tuple[str, int | None, int | None, str]:
    block = work_age_of(record)
    if block is None:
        return AGE_NOT_LOOKED_UP, None, None, ""
    if block["status"] == STATUS_NOT_FOUND:
        return AGE_NOT_FOUND, None, None, ""
    if block["status"] != STATUS_FOUND:
        return AGE_NOT_LOOKED_UP, None, None, ""
    year, count = block["first_publish_year"], block["edition_count"]
    status = AGE_KNOWN if year is not None and count is not None else AGE_PARTLY_KNOWN
    return status, year, count, block["basis"]


def split_half(book_id: str) -> str:
    """The fixed half of one book, from a digest of its durable ID."""
    digest = hashlib.sha256(f"{SPLIT_SALT}:{book_id}".encode()).digest()
    return DEVELOPMENT if digest[0] % 2 == 0 else HELD_OUT


def read_books(output_dir: Path, state_dir: Path, *, state_store: StateStore | None = None) -> list[Book]:
    """Every decided book of Candidate History, once, with its newest recorded fit score."""
    store = state_store if state_store is not None else open_state_store(state_dir)
    aliases = store.read(Collection.CANDIDATE_IDENTITY_ALIASES)
    history = store.read(Collection.CANDIDATE_HISTORY)
    if not isinstance(history, list):
        raise ValueError("candidate history is not a list")
    scores = recorded_fit_scores(output_dir, aliases)
    seen: set[str] = set()
    books: list[Book] = []
    for record in history:
        if not isinstance(record, dict):
            continue
        decision = _decision(record.get("editor_status"))
        keys = book_keys(record, aliases)
        if decision is None or not keys or keys & seen:
            continue
        seen |= keys
        age_status, year, count, basis = _age_facts(record)
        books.append(
            Book(
                title=str(record.get("title", "")).strip() or str(record.get("identity_key", "")),
                authors=_authors(record),
                language=_language(record),
                decision=decision,
                listing_year=_listing_year(record),
                first_publish_year=year,
                edition_count=count,
                age_status=age_status,
                score=scores.score_for(record),
                basis=basis,
                half=split_half(durable_id(record, aliases) or str(record.get("identity_key", ""))),
            )
        )
    return books


# ── Arithmetic ───────────────────────────────────────────────────────────


def top_size(count: int) -> int:
    return max(1, math.floor(count * TOP_FRACTION + 0.5)) if count else 0


def top_shares(keys: Sequence[Any]) -> list[float]:
    """Each book's share of a place in the top, ordered by *keys*, highest first.

    Books with equal keys are interchangeable, so a tie that straddles the
    edge of the top shares the places left between its books: the expectation
    over every order of the tie, with no random draw.
    """
    remaining = top_size(len(keys))
    shares = [0.0] * len(keys)
    groups: dict[Any, list[int]] = {}
    for index, key in enumerate(keys):
        groups.setdefault(key, []).append(index)
    for key in sorted(groups, reverse=True):
        members = groups[key]
        taken = min(remaining, len(members))
        for index in members:
            shares[index] = taken / len(members)
        remaining -= taken
    return shares


def moved_down(book: Book, min_age: int, min_editions: int) -> bool:
    return (book.age or 0) >= min_age and (book.edition_count or 0) >= min_editions


def simulate(books: Sequence[Book], min_age: int, min_editions: int) -> dict[str, Any]:
    """Move the books at or above both thresholds below the others, and count what changes in the top."""
    flagged = [moved_down(book, min_age, min_editions) for book in books]
    before = top_shares([book.score for book in books])
    # A moved book keeps its order among the moved books.
    after = top_shares([(not moved, book.score) for book, moved in zip(books, flagged, strict=True)])
    result = {
        "min_age": min_age,
        "min_editions": min_editions,
        "books": len(books),
        "top_size": top_size(len(books)),
        "wanted": 0,
        "rejected": 0,
        "wanted_in_top_before": 0.0,
        "wanted_in_top_after": 0.0,
        "wanted_moved_down": 0,
        "wanted_lost_from_top": 0.0,
        "wanted_gained_in_top": 0.0,
        "rejected_in_top_before": 0.0,
        "rejected_moved_down": 0,
        "rejected_left_top": 0.0,
    }
    for book, moved, was, now in zip(books, flagged, before, after, strict=True):
        if book.decision == WANTED:
            result["wanted"] += 1
            result["wanted_in_top_before"] += was
            result["wanted_in_top_after"] += now
            if moved:
                result["wanted_moved_down"] += 1
                result["wanted_lost_from_top"] += was - now
            else:
                result["wanted_gained_in_top"] += now - was
        else:
            result["rejected"] += 1
            result["rejected_in_top_before"] += was
            if moved:
                result["rejected_moved_down"] += 1
                result["rejected_left_top"] += was - now
    result["net_wanted_change"] = result["wanted_gained_in_top"] - result["wanted_lost_from_top"]
    return result


def _choice_key(result: Mapping[str, Any]) -> tuple[float, int, float]:
    """The best threshold: most wanted books gained in the top, then fewest wanted moved down, then most rejected out."""
    return (
        round(result["net_wanted_change"], 6),
        -result["wanted_moved_down"],
        round(result["rejected_left_top"], 6),
    )


def choose_threshold(results: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The best of *results*; on a full tie the first in grid order (the youngest, fewest-edition threshold)."""
    best: Mapping[str, Any] | None = None
    for result in results:
        if best is None or _choice_key(result) > _choice_key(best):
            best = result
    return best


def _band(value: int, bands: Sequence[tuple[str, int | None, int | None]]) -> str:
    for label, low, high in bands:
        if (low is None or value >= low) and (high is None or value <= high):
            return label
    raise ValueError(f"no band holds {value}")


def _band_rows(
    books: Sequence[Book], value: Callable[[Book], int | None], bands: Sequence[tuple[str, int | None, int | None]]
) -> list[dict[str, Any]]:
    rows = []
    for label, _low, _high in bands:
        group = [book for book in books if (number := value(book)) is not None and _band(number, bands) == label]
        rows.append(
            {
                "band": label,
                "wanted": sum(1 for book in group if book.decision == WANTED),
                "rejected": sum(1 for book in group if book.decision == REJECTED),
                "already_known": sum(1 for book in group if book.decision == ALREADY_KNOWN),
            }
        )
    return rows


def _coverage_row(language: str, books: Sequence[Book]) -> dict[str, Any]:
    return {
        "language": language,
        "decided": len(books),
        "wanted": sum(1 for book in books if book.decision == WANTED),
        "rejected": sum(1 for book in books if book.decision == REJECTED),
        "already_known": sum(1 for book in books if book.decision == ALREADY_KNOWN),
        AGE_KNOWN: sum(1 for book in books if book.age_status == AGE_KNOWN),
        AGE_PARTLY_KNOWN: sum(1 for book in books if book.age_status == AGE_PARTLY_KNOWN),
        AGE_NOT_FOUND: sum(1 for book in books if book.age_status == AGE_NOT_FOUND),
        AGE_NOT_LOOKED_UP: sum(1 for book in books if book.age_status == AGE_NOT_LOOKED_UP),
        "fit_score": sum(1 for book in books if book.score is not None),
        "by_basis": {basis: sum(1 for book in books if book.basis == basis) for basis in BASES_IN_ORDER},
    }


def _book_row(book: Book, *, in_top_before: float, in_top_after: float) -> dict[str, Any]:
    return {
        "title": book.title,
        "authors": book.authors,
        "language": book.language,
        "half": book.half,
        "first_publish_year": book.first_publish_year,
        "listing_year": book.listing_year,
        "age": book.age,
        "edition_count": book.edition_count,
        "score": book.score,
        "in_top_before": in_top_before,
        "in_top_after": in_top_after,
    }


def _mistakes(halves: Mapping[str, Sequence[Book]], min_age: int, min_editions: int) -> dict[str, list[dict[str, Any]]]:
    """The wanted books the rule moves down, and the old rejected works it leaves in the top, in both halves."""
    wanted_moved: list[dict[str, Any]] = []
    rejected_kept: list[dict[str, Any]] = []
    for books in halves.values():
        flagged = [moved_down(book, min_age, min_editions) for book in books]
        before = top_shares([book.score for book in books])
        after = top_shares([(not moved, book.score) for book, moved in zip(books, flagged, strict=True)])
        for book, moved, was, now in zip(books, flagged, before, after, strict=True):
            row = _book_row(book, in_top_before=was, in_top_after=now)
            if book.decision == WANTED and moved:
                wanted_moved.append(row)
            elif book.decision == REJECTED and not moved and now > 0 and (book.age or 0) >= OLD_WORK_MIN_AGE:
                rejected_kept.append(row)

    def order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(rows, key=lambda row: (-(row["score"] or 0.0), row["title"].casefold(), row["authors"]))

    return {"wanted_moved_down": order(wanted_moved), "rejected_old_works_left_in_top": order(rejected_kept)}


def build_report(books: Sequence[Book]) -> dict[str, Any]:
    """The report's numbers for *books*, as ``read_books`` returns them."""
    languages = sorted({book.language for book in books}, key=lambda code: (code == UNKNOWN_LANGUAGE, code))
    coverage = [_coverage_row(code, [book for book in books if book.language == code]) for code in languages]
    decided_for_fit = [book for book in books if book.decision in (WANTED, REJECTED)]
    simulated = [book for book in books if book.simulated]
    halves = {half: [book for book in simulated if book.half == half] for half in (DEVELOPMENT, HELD_OUT)}
    grid = [(age, editions) for age in AGE_THRESHOLDS for editions in EDITION_THRESHOLDS]
    thresholds = [
        {half: simulate(halves[half], age, editions) for half in (DEVELOPMENT, HELD_OUT)} for age, editions in grid
    ]
    best = choose_threshold([row[DEVELOPMENT] for row in thresholds])
    chosen = None
    if best is not None and halves[DEVELOPMENT]:
        chosen = next(
            row
            for row in thresholds
            if (row[DEVELOPMENT]["min_age"], row[DEVELOPMENT]["min_editions"])
            == (best["min_age"], best["min_editions"])
        )
    return {
        "question_set_version": QUESTION_SET_VERSION,
        "top_fraction": TOP_FRACTION,
        "coverage": coverage,
        "totals": _coverage_row("all", books),
        "outside": {
            "wanted_or_rejected": len(decided_for_fit),
            "no_work_age": sum(1 for book in decided_for_fit if book.age is None or book.edition_count is None),
            "no_listing_year": sum(1 for book in decided_for_fit if book.listing_year is None),
            "work_age_but_no_fit_score": sum(
                1
                for book in decided_for_fit
                if book.age is not None and book.edition_count is not None and book.score is None
            ),
            "simulated": len(simulated),
            "already_known": sum(1 for book in books if book.decision == ALREADY_KNOWN),
        },
        "by_age": _band_rows(books, lambda book: book.age, AGE_BANDS),
        "by_editions": _band_rows(books, lambda book: book.edition_count, EDITION_BANDS),
        "halves": {half: len(members) for half, members in halves.items()},
        "half_wanted": {
            half: sum(1 for book in members if book.decision == WANTED) for half, members in halves.items()
        },
        "thresholds": thresholds,
        "chosen": chosen,
        # Whether the chosen threshold added wanted books to the top of the development half. When
        # it did not, it is only the least harmful of the thresholds, never a recommendation.
        "chosen_helps": chosen is not None and chosen[DEVELOPMENT]["net_wanted_change"] > _TOLERANCE,
        "mistakes": _mistakes(halves, chosen[DEVELOPMENT]["min_age"], chosen[DEVELOPMENT]["min_editions"])
        if chosen
        else None,
    }


# ── Rendering ────────────────────────────────────────────────────────────


def _number(value: float) -> str:
    """A count: whole when whole, else one decimal (a tie shared between places)."""
    if abs(value - round(value)) < _TOLERANCE:
        return f"{round(value):,}"
    return f"{value:,.1f}"


def _of(part: float, whole: float) -> str:
    """``part of whole (share%)``: every percentage comes with its counts."""
    if not whole:
        return f"{_number(part)} of {_number(whole)}"
    return f"{_number(part)} of {_number(whole)} ({100 * part / whole:.0f}%)"


def language_name(code: str) -> str:
    if code == UNKNOWN_LANGUAGE:
        return "Unknown language"
    return LANGUAGE_NAMES.get(code, code)


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ") if text not in (None, "") else "—"


def _threshold_name(result: Mapping[str, Any]) -> str:
    years = "year" if result["min_age"] == 1 else "years"
    editions = "edition" if result["min_editions"] == 1 else "editions"
    return f"at least {result['min_age']} {years} old with at least {result['min_editions']} {editions}"


def _basis_sentence(totals: Mapping[str, Any]) -> str:
    by_basis = totals["by_basis"]
    found = sum(by_basis.values())
    return (
        f"Of the {found:,} work ages found, {_of(by_basis[BASIS_ORIGINAL_EDITION], found)} were looked up by the "
        f"book's confirmed original edition, {_of(by_basis[BASIS_ISBN], found)} by the book's own ISBN, and "
        f"{_of(by_basis[BASIS_TITLE_AUTHOR], found)} by its title and author."
    )


def _coverage_lines(report: Mapping[str, Any]) -> list[str]:
    totals = report["totals"]
    outside = report["outside"]
    lines = [
        "## 1. How many decided books have a known work age",
        "",
        f"The editor has decided {totals['decided']:,} books: {totals['wanted']:,} wanted, "
        f"{totals['rejected']:,} rejected and {totals['already_known']:,} already known. "
        f"Both the year of first publication and the edition count are known for "
        f"{_of(totals[AGE_KNOWN], totals['decided'])}. "
        f"For {totals[AGE_PARTLY_KNOWN]:,} more only one of the two is known, "
        f"Open Library has no matching work for {totals[AGE_NOT_FOUND]:,}, "
        f"and {totals[AGE_NOT_LOOKED_UP]:,} have not been looked up yet.",
        "",
        _basis_sentence(totals),
        "",
        "A book is counted under the language of the list that first found it.",
        "",
        "| List language | Decided | Wanted | Rejected | Already known | Age and editions known | Only one known "
        "| No match | Not looked up | Fit score recorded |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in [*report["coverage"], totals]:
        name = "All languages" if row is totals else language_name(row["language"])
        lines.append(
            f"| {name} | {row['decided']:,} | {row['wanted']:,} | {row['rejected']:,} | {row['already_known']:,} | "
            f"{_of(row[AGE_KNOWN], row['decided'])} | {row[AGE_PARTLY_KNOWN]:,} | {row[AGE_NOT_FOUND]:,} | "
            f"{row[AGE_NOT_LOOKED_UP]:,} | {row['fit_score']:,} |"
        )
    low = [row for row in report["coverage"] if row["decided"] and row[AGE_KNOWN] / row["decided"] < LOW_COVERAGE_SHARE]
    lines.append("")
    if low:
        parts = [f"{language_name(row['language'])}: {_of(row[AGE_KNOWN], row['decided'])}" for row in low]
        lines.append(
            "Coverage is low, under half of the decided books, for these list languages: "
            + "; ".join(parts)
            + ". What this report finds says little about books from those lists."
        )
        lines.append("")
    lines.append(
        f"Only wanted and rejected books take part in the simulation below; the {outside['already_known']:,} "
        "already known books do not, because that decision is about what the editor knew, not about fit. "
        f"Of the {outside['wanted_or_rejected']:,} wanted and rejected books, {outside['no_work_age']:,} have no "
        "usable work age (no year of first publication, no edition count, or no date AutoScout first listed them) "
        f"and so are outside the simulation. Another {outside['work_age_but_no_fit_score']:,} have a work age but "
        f"no recorded fit score and are outside it too. That leaves {outside['simulated']:,} books in the simulation."
    )
    lines.append("")
    return lines


def _band_lines(title: str, rows: Sequence[Mapping[str, Any]], unknown_note: str) -> list[str]:
    lines = [
        f"| {title} | Wanted | Rejected | Share wanted | Already known |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        decided = row["wanted"] + row["rejected"]
        lines.append(
            f"| {row['band']} | {row['wanted']:,} | {row['rejected']:,} | {_of(row['wanted'], decided)} | "
            f"{row['already_known']:,} |"
        )
    lines.extend(["", unknown_note, ""])
    return lines


def _share_lines(report: Mapping[str, Any]) -> list[str]:
    lines = [
        "## 2. The share of wanted books by age of the work and by edition count",
        "",
        "The age of the work is the year AutoScout first listed the book minus the year the work was first "
        "published. For a book whose original edition is confirmed, the year is the original's. For a translation "
        "whose original is not identified, the work age was looked up by the translation's own ISBN or title, so "
        "the year may be the translation's: the work may then be older than shown, never younger. Today's date is not "
        'used, so the numbers do not change as time passes. "Share wanted" is the wanted books among the '
        "wanted and rejected books of that row; already known books are shown apart.",
        "",
    ]
    lines.extend(
        _band_lines(
            "Age of the work",
            report["by_age"],
            "Books with no known year of first publication are not in this table.",
        )
    )
    lines.extend(
        _band_lines(
            "Editions in Open Library",
            report["by_editions"],
            "Books with no known edition count are not in this table.",
        )
    )
    return lines


def _half_size_sentence(half_wanted: Mapping[str, int]) -> str:
    sentence = (
        f"The development half holds {half_wanted[DEVELOPMENT]:,} wanted books and the held-out half "
        f"{half_wanted[HELD_OUT]:,}."
    )
    if min(half_wanted.values()) < SMALL_HALF_WANTED:
        sentence += (
            f" With fewer than {SMALL_HALF_WANTED} wanted books in a half, a difference of one or two wanted books, "
            "between thresholds or between the halves, can come from chance alone and should not be read as a result."
        )
    return sentence


def _simulation_lines(report: Mapping[str, Any]) -> list[str]:
    halves = report["halves"]
    top_percent = f"{100 * report['top_fraction']:.0f}%"
    lines = [
        "## 3. Simulation: moving old, often-published works down",
        "",
        f"The books are ordered by their newest recorded `{report['question_set_version']}` fit score, highest "
        f"first. The top is the highest-scored {top_percent} of the books. For each threshold, every book whose work "
        "is at least that old and has at least that many editions is moved below all the other books, keeping its "
        "order among the moved books. The table counts what that changes in the top. When books with the same "
        "score straddle the edge of the top, they share the places left, so a count can have a decimal.",
        "",
        f"The {report['outside']['simulated']:,} books in the simulation are split into two fixed halves by a "
        f"digest of their Candidate ID: a development half of {halves[DEVELOPMENT]:,} books, on which the best "
        f"threshold is chosen, and a held-out half of {halves[HELD_OUT]:,} books, on which it is checked. The "
        "held-out numbers are the ones to trust: the threshold was not chosen on them.",
        "",
        _half_size_sentence(report["half_wanted"]),
        "",
        "The best threshold is the one that, on the development half, adds the most wanted books to the top "
        "(wanted books that enter the top as others leave it, minus wanted books moved out of it); on a tie, "
        "the one that moves fewer wanted books down, then the one that moves more rejected books out of the top.",
        "",
    ]
    if report["chosen"] is None:
        lines.extend(["No book has both a fit score and a known work age, so there is nothing to simulate.", ""])
        return lines
    chosen = report["chosen"]
    label = "chosen" if report["chosen_helps"] else "least harmful"
    for half, title in ((DEVELOPMENT, "Development half"), (HELD_OUT, "Held-out half")):
        sample = report["thresholds"][0][half]
        lines.extend(
            [
                f"### {title}",
                "",
                f"{sample['books']:,} books: {sample['wanted']:,} wanted and {sample['rejected']:,} rejected. "
                f"The top holds {sample['top_size']:,} books, of them {_number(sample['wanted_in_top_before'])} "
                f"wanted and {_number(sample['rejected_in_top_before'])} rejected before any book is moved.",
                "",
                "| Moved down when the work is | Books moved down | Rejected books that leave the top "
                "| Wanted books lost from the top | Wanted books that enter the top | Wanted books in the top after |",
                "| --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for row in report["thresholds"]:
            result = row[half]
            mark = f" ({label})" if row is chosen else ""
            lines.append(
                f"| {_threshold_name(result)}{mark} | "
                f"{result['rejected_moved_down'] + result['wanted_moved_down']:,} "
                f"({result['wanted_moved_down']:,} wanted) | "
                f"{_of(result['rejected_left_top'], result['rejected_in_top_before'])} | "
                f"{_of(result['wanted_lost_from_top'], result['wanted_in_top_before'])} | "
                f"{_number(result['wanted_gained_in_top'])} | "
                f"{_of(result['wanted_in_top_after'], result['wanted'])} |"
            )
        lines.append("")
    development, held_out = chosen[DEVELOPMENT], chosen[HELD_OUT]
    if report["chosen_helps"]:
        opening = f"The chosen threshold moves a book down when the work is {_threshold_name(development)}. "
    else:
        opening = (
            "No threshold added wanted books to the top on the development half: the best net change was "
            f"{_number(development['net_wanted_change'])} wanted books. So no threshold is recommended. The one "
            f"described here is only the least harmful of them: it moves a book down when the work is "
            f"{_threshold_name(development)}. "
        )
    verdict = (
        "On the held-out half it adds wanted books to the top."
        if held_out["net_wanted_change"] > _TOLERANCE
        else "On the held-out half it does not add wanted books to the top."
        if held_out["net_wanted_change"] > -_TOLERANCE
        else "On the held-out half it costs wanted books in the top."
    )
    lines.extend(
        [
            "### Result",
            "",
            opening
            + f"On the development half it moves {_of(development['rejected_left_top'], development['rejected_in_top_before'])} "
            "rejected books in the top out of it, loses "
            f"{_of(development['wanted_lost_from_top'], development['wanted_in_top_before'])} wanted books in the "
            f"top, and lets {_number(development['wanted_gained_in_top'])} wanted books enter it. "
            f"On the held-out half it moves {_of(held_out['rejected_left_top'], held_out['rejected_in_top_before'])} "
            "rejected books in the top out of it, loses "
            f"{_of(held_out['wanted_lost_from_top'], held_out['wanted_in_top_before'])} wanted books in the top, "
            f"and lets {_number(held_out['wanted_gained_in_top'])} wanted books enter it. "
            f"The wanted books in the held-out top go from {_number(held_out['wanted_in_top_before'])} to "
            f"{_number(held_out['wanted_in_top_after'])} of {held_out['wanted']:,}. {verdict}",
            "",
        ]
    )
    return lines


def _book_table(rows: Sequence[Mapping[str, Any]], *, top_column: str, top_key: str) -> list[str]:
    lines = [
        f"| Title | Author | List language | Half | First published | Age | Editions | Fit score | {top_column} |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        top = row[top_key]
        lines.append(
            f"| {_cell(row['title'])} | {_cell(row['authors'])} | {language_name(row['language'])} | "
            f"{'development' if row['half'] == DEVELOPMENT else 'held-out'} | {_cell(row['first_publish_year'])} | "
            f"{_cell(row['age'])} | {_cell(row['edition_count'])} | {row['score']:.1f} | "
            f"{'yes' if top >= 1 - _TOLERANCE else 'no' if top <= _TOLERANCE else f'shared ({top:.1f})'} |"
        )
    return lines


def _mistake_lines(report: Mapping[str, Any]) -> list[str]:
    mistakes = report["mistakes"]
    if mistakes is None:
        return [
            "## 4. The books the chosen threshold gets wrong",
            "",
            "There is no threshold, so no book is listed.",
            "",
        ]
    if report["chosen_helps"]:
        lines = ["## 4. The books the chosen threshold gets wrong", ""]
    else:
        lines = [
            "## 4. The books the least harmful threshold gets wrong",
            "",
            "No threshold helped on the development half, so these lists are not the mistakes of a recommended "
            "rule. They show what even the least harmful threshold would cost, and which old works it would leave "
            "in the top.",
            "",
        ]
    wanted = mistakes["wanted_moved_down"]
    rejected = mistakes["rejected_old_works_left_in_top"]
    lines.extend(
        [
            'Both halves are listed. "Age" is in years; "Fit score" is on the 0 to 4 scale of the fit question.',
            "",
            f"### Wanted books the threshold moves down ({len(wanted):,})",
            "",
            "These are books the editor wanted that the rule would push below every book it does not move. "
            '"In the top before" says whether the book was in the top before the move.',
            "",
        ]
    )
    if wanted:
        lines.extend(_book_table(wanted, top_column="In the top before", top_key="in_top_before"))
    else:
        lines.append("None: the threshold moves no wanted book down.")
    lines.extend(
        [
            "",
            f"### Rejected books the threshold leaves in the top although the work is at least "
            f"{OLD_WORK_MIN_AGE} year old ({len(rejected):,})",
            "",
            "These are rejected books whose work is not new, but whose edition count or age is under the "
            "threshold, so they stay in the top. Rejected books whose work is new are not listed: no age rule "
            "could move them.",
            "",
        ]
    )
    if rejected:
        lines.extend(_book_table(rejected, top_column="In the top after", top_key="in_top_after"))
    else:
        lines.append("None.")
    lines.append("")
    return lines


def render_report(report: Mapping[str, Any]) -> str:
    lines = [
        "# Would the age of the work improve the review order?",
        "",
        "This report looks at the books the editor has already decided and asks whether moving old, "
        "often-published works down the review would put fewer rejected and more wanted books near the top. "
        "It reads the editor's decisions, the work age AutoScout recorded from Open Library, and the recorded "
        "fit scores. It changed nothing, called no model and used no network.",
        "",
        "A book counts as wanted when the editor marked it interested, maybe/watch, or needs deeper analysis, "
        "and as rejected when the editor rejected it. Already known books are counted apart.",
        "",
    ]
    lines.extend(_coverage_lines(report))
    lines.extend(_share_lines(report))
    lines.extend(_simulation_lines(report))
    lines.extend(_mistake_lines(report))
    return "\n".join(lines).rstrip() + "\n"


def _checked_report_file(report_file: Path, state_dir: Path) -> Path:
    """The report file, refused inside the state directory.

    An untracked file in the run directory would block the next weekly run
    (dirty working tree), so the launcher writes the report under the
    Git-ignored ``data/work-age-report/``.
    """
    resolved = report_file.resolve()
    if resolved.is_relative_to(state_dir.resolve()):
        raise ValueError("the report file must not be inside the state directory")
    return resolved


def write_report(output_dir: Path, state_dir: Path, report_file: Path, *, state_store: StateStore | None = None) -> str:
    """Build the report from state and the recorded scores, and write its one file."""
    path = _checked_report_file(report_file, state_dir)
    text = render_report(build_report(read_books(output_dir, state_dir, state_store=state_store)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text
