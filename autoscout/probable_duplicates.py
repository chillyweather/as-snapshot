"""Report probable duplicate Candidate identities for a person to decide (Issue #855).

Duplicate identities for one work were found only when an editor noticed
them (#849). This report lists them. It is offline, deterministic, and never
merges anything: every pair goes to the editor through
``merge-candidate-identity --dry-run`` and the confirmation boundary in
``AGENTS.md`` and ``docs/agents/contracts/identity-correction.md``.

Rules, all closed and deterministic:

* Records are grouped by normalized work title (``normalize_work_title``).
* Within a group, two records are a pair when their authors are compatible:
  they share a surname token, or one side names no author. A person's
  surname tokens are the tokens after the given name, without single-letter
  initials, credentials, and name particles; a one-word name is its own
  surname token. Placeholder author text (#852) names nobody.
* Different titles can match by containment, a published English title, a shared
  published English title, or a confirmed original title.
* Both records carrying an ISBN and the ISBNs differing makes the pair
  ``possible_editions`` rather than ``probable_duplicate``.
* The given names of the shared-surname people are compared through a small
  closed nickname map and reported as a signal; they never decide a pair.
* A pair whose durable IDs already redirect to one survivor, or that the
  editor dismissed, is not reported again.

Dismissals live in a small derived state file. Writing one is a two-phase
editor action: a preview with zero mutation, then the same call with
``--confirmed-by-editor``.
"""

from __future__ import annotations

import json
from datetime import date
from itertools import combinations
from pathlib import Path
from typing import Any

from autoscout.author_admission import is_placeholder_author
from autoscout.candidate_identity import candidate_id_for
from autoscout.candidate_identity import effective_candidate_id
from autoscout.candidate_identity import stored_candidate_id_for_alias
from autoscout.identity import AUTHOR_LIST_SEPARATOR
from autoscout.identity import identity_isbn
from autoscout.identity import normalize_author_display
from autoscout.identity import normalize_author_identity
from autoscout.identity import normalize_isbn
from autoscout.identity import normalize_work_title
from autoscout.identity_corrections import MERGED_REFERENCE_FILES
from autoscout.identity_corrections import identity_correction_conflicts
from autoscout.identity_corrections import reference_label
from autoscout.json_io import read_json_or_default
from autoscout.json_io import write_json
from autoscout.original_edition import validate_original_edition
from autoscout.safe_artifacts import bundle_write_lock
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import open_state_store
from autoscout.title_evidence import clean_title
from autoscout.title_evidence import is_valid_sourced_published_title

STATE_PROBABLE_DUPLICATE_DISMISSALS = "probable-duplicate-dismissals.json"

PROBABLE_DUPLICATE = "probable_duplicate"
POSSIBLE_EDITIONS = "possible_editions"

# Tokens that are never a surname: credentials and honorifics, name
# particles, and the joins and tails of a fused author list.
_NOT_SURNAME_TOKENS = frozenset(
    {
        # credentials and honorifics
        "md",
        "phd",
        "dr",
        "med",
        "prof",
        "pr",
        "jr",
        "sr",
        "ii",
        "iii",
        "iv",
        # particles
        "the",
        "de",
        "da",
        "das",
        "del",
        "della",
        "der",
        "den",
        "di",
        "do",
        "dos",
        "du",
        "la",
        "le",
        "van",
        "von",
        "of",
        # list joins and contributor tails
        "and",
        "others",
        "et",
        "al",
        "und",
        "y",
        "e",
        "otros",
        "altri",
    }
)

# A closed nickname map: each key is read as its value when given names are
# compared.
NICKNAMES = {
    "babs": "barbara",
    "barb": "barbara",
    "bill": "william",
    "billy": "william",
    "bob": "robert",
    "bobby": "robert",
    "chris": "christopher",
    "dan": "daniel",
    "danny": "daniel",
    "dave": "david",
    "jim": "james",
    "jimmy": "james",
    "joe": "joseph",
    "kate": "katherine",
    "katie": "katherine",
    "liz": "elizabeth",
    "beth": "elizabeth",
    "matt": "matthew",
    "mike": "michael",
    "nick": "nicholas",
    "pat": "patricia",
    "rob": "robert",
    "sam": "samuel",
    "steve": "steven",
    "sue": "susan",
    "tom": "thomas",
    "tony": "anthony",
}


def _author_values(record: dict[str, Any]) -> list[str]:
    """The author values of a record, or of its identity key when none are stored."""
    authors = record.get("primary_authors")
    if isinstance(authors, list) and any(str(author).strip() for author in authors):
        values = [str(author) for author in authors]
    else:
        key = str(record.get("identity_key", ""))
        values = key.split("|", 1)[1].split(" & ") if "|" in key else []
    return [value for value in values if value.strip() and not is_placeholder_author(value)]


def _people(record: dict[str, Any]) -> list[tuple[str, frozenset[str]]]:
    """Return each person as (given name, surname tokens)."""
    people: list[tuple[str, frozenset[str]]] = []
    for value in _author_values(record):
        for component in AUTHOR_LIST_SEPARATOR.split(value):
            tokens = normalize_author_identity(normalize_author_display(component)).split()
            if not tokens:
                continue
            given, rest = tokens[0], tokens[1:]
            surnames = frozenset(token for token in rest if len(token) > 1 and token not in _NOT_SURNAME_TOKENS)
            if not surnames:
                # A name made only of particles or short tokens ("Thi Lê")
                # keeps them rather than parsing as nobody.
                surnames = frozenset(token for token in rest if token not in _NOT_SURNAME_TOKENS) or frozenset(
                    token for token in rest if len(token) > 1
                )
            if not rest and given not in _NOT_SURNAME_TOKENS:
                surnames = frozenset({given})
            if surnames:
                people.append((given, surnames))
    return people


def _given_relation(first: str, second: str) -> str:
    if first == second:
        return "same_given_name"
    if NICKNAMES.get(first, first) == NICKNAMES.get(second, second):
        return "nickname"
    if len(first) == 1 or len(second) == 1:
        return "initial"
    return "different_given_name"


def _author_signals(first: dict[str, Any], second: dict[str, Any]) -> list[str] | None:
    """Return the compatibility signals of two records, or None when incompatible."""
    # Only a record that names no author at all, or only placeholder text,
    # pairs on the missing author; an author that parses as nobody does not.
    if not _author_values(first) or not _author_values(second):
        return ["author_missing"]
    first_people, second_people = _people(first), _people(second)
    signals: set[str] = set()
    for given_a, surnames_a in first_people:
        for given_b, surnames_b in second_people:
            if surnames_a & surnames_b:
                signals.add("shared_surname")
                signals.add(_given_relation(given_a, given_b))
    return sorted(signals) if signals else None


def _record_summary(record: dict[str, Any], alias_table: dict[str, Any]) -> dict[str, Any]:
    authors = record.get("primary_authors")
    evidence = record.get("source_evidence", [])
    sources = sorted(
        {
            str(item.get("source_name", "")).strip()
            for item in (evidence if isinstance(evidence, list) else [])
            if isinstance(item, dict) and str(item.get("source_name", "")).strip()
        }
    )
    return {
        "identity_key": str(record.get("identity_key", "")),
        "candidate_id": _durable_id(record, alias_table),
        "title": str(record.get("title", "") or ""),
        "author_display": ", ".join(str(author) for author in authors) if isinstance(authors, list) else "",
        "editor_status": str(record.get("editor_status", "") or ""),
        "editor_note": str(record.get("editor_note", "") or ""),
        "first_seen_date": str(record.get("first_seen_date", "") or ""),
        "last_seen_date": str(record.get("last_seen_date", "") or ""),
        "sources": sources,
        "isbn": str(record.get("isbn", "") or ""),
        "isbn_discovery_only": bool(record.get("isbn_discovery_only", False)),
    }


def _conflicting_fields(
    first: dict[str, Any], second: dict[str, Any], references: dict[Collection | str, Any]
) -> list[str]:
    """Return every field merge-candidate-identity refuses for the two records, as the merge checks them."""
    keys = {str(first["identity_key"]).strip(), str(second["identity_key"]).strip()}
    return identity_correction_conflicts([first, second], references, keys)


_CANONICAL_BASES = ("only_editor_decision", "only_stored_authors", "earlier_first_sighting", "identity_key_order")


def _suggested_canonical(first: dict[str, Any], second: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """A deterministic suggestion and the rule that chose it; the editor decides.

    Prefer the single record with an editor decision, then the single record
    with a stored title and authors, then the earlier first sighting, then the
    identity key. The basis is reported so a weak tie-break is never read as
    evidence.
    """

    def rank(record: dict[str, Any]) -> tuple[int, int, str, str]:
        authors = record.get("primary_authors")
        return (
            0 if str(record.get("editor_status", "") or "").strip() else 1,
            0 if record.get("title") and isinstance(authors, list) and authors else 1,
            str(record.get("first_seen_date", "") or "9999-99-99"),
            str(record.get("identity_key", "")),
        )

    ranks = rank(first), rank(second)
    deciding = next(index for index in range(4) if ranks[0][index] != ranks[1][index])
    return (first if ranks[0] < ranks[1] else second), _CANONICAL_BASES[deciding]


def _comparable_isbn(raw: object) -> str:
    """Return an ISBN in its ISBN-13 form so both printed forms of one book compare equal."""
    isbn = normalize_isbn(str(raw or "")).upper()
    if len(isbn) == 10 and isbn[:9].isdigit() and (isbn[9].isdigit() or isbn[9] == "X"):
        body = "978" + isbn[:9]
        check = (10 - sum(int(digit) * (1 if index % 2 == 0 else 3) for index, digit in enumerate(body)) % 10) % 10
        return body + str(check)
    return isbn


def _durable_id(record: dict[str, Any], alias_table: dict[str, Any]) -> str:
    """The record's durable ID: stored, else assigned to its key in the alias table, else derived."""
    key = str(record.get("identity_key", ""))
    return (
        str(record.get("candidate_id", "")).strip()
        or stored_candidate_id_for_alias(alias_table, key)
        or candidate_id_for(key)
    )


def _pair_ref(first_id: str, second_id: str, alias_table: dict[str, Any]) -> tuple[str, str]:
    """A pair as its two surviving durable IDs, so a later merge keeps a dismissal."""
    first, second = sorted(
        (effective_candidate_id(first_id, alias_table), effective_candidate_id(second_id, alias_table))
    )
    return first, second


def load_dismissals(state_dir: Path) -> list[dict[str, Any]]:
    value = read_json_or_default(state_dir / STATE_PROBABLE_DUPLICATE_DISMISSALS, [])
    if not isinstance(value, list):
        raise ValueError(f"{STATE_PROBABLE_DUPLICATE_DISMISSALS} must be a list")
    return [item for item in value if isinstance(item, dict)]


def _dismissed_refs(dismissals: list[dict[str, Any]], alias_table: dict[str, Any]) -> set[tuple[str, str]]:
    refs: set[tuple[str, str]] = set()
    for item in dismissals:
        ids = item.get("candidate_ids")
        if isinstance(ids, list) and len(ids) == 2:
            refs.add(_pair_ref(str(ids[0]), str(ids[1]), alias_table))
    return refs


# A shorter title must keep this many tokens to count as contained in a longer
# one, so "the body" never pairs with "the body keeps the score" (#916).
MIN_CONTAINED_TITLE_TOKENS = 3


def _record_title(record: dict[str, Any]) -> str:
    title = str(record.get("title", "") or "") or str(record["identity_key"]).split("|", 1)[0]
    return normalize_work_title(title)


def _contained_title(first: str, second: str) -> str:
    """The shorter title when it is the token prefix or suffix of the longer one, else "" (#916)."""
    shorter, longer = sorted((first.split(), second.split()), key=len)
    size = len(shorter)
    if size < MIN_CONTAINED_TITLE_TOKENS or size == len(longer):
        return ""
    return " ".join(shorter) if longer[:size] == shorter or longer[-size:] == shorter else ""


def _published_english_title(record: dict[str, Any]) -> str:
    """The sourced published English title of a record, normalized, or "" (#916).

    A generated rendering is a guess at a translation, never identity evidence,
    and a published title counts only with complete publication evidence for
    this record's title and authors (review of #916).
    """
    if not is_valid_sourced_published_title(record):
        return ""
    return normalize_work_title(clean_title(record.get("english_title", "")))


def _confirmed_original_title(record: dict[str, Any]) -> str:
    original = record.get("original_edition")
    if not isinstance(original, dict) or not validate_original_edition(original) or original["status"] != "confirmed":
        return ""
    return normalize_work_title(clean_title(original["original_title"]))


def _surname_tokens(record: dict[str, Any]) -> frozenset[str]:
    return frozenset(token for _given, surnames in _people(record) for token in surnames)


def probable_duplicate_pairs(
    history: list[Any],
    alias_table: dict[str, Any],
    dismissals: list[dict[str, Any]] | None = None,
    references: dict[Collection | str, Any] | None = None,
) -> dict[str, Any]:
    """Pair records of one work: same title with compatible authors, or a contained or translated title (#916).

    ``references`` maps the state files a merge also combines (latest and
    scouting candidates, visibility) to their records, so conflicts stored
    there are reported too. Same-title records whose authors share no surname
    are listed apart, in ``author_disagreements``: often two books, sometimes
    a misattributed one, never a merge suggestion.
    """
    records = [record for record in history if isinstance(record, dict) and str(record.get("identity_key", "")).strip()]
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        normalized = _record_title(record)
        if normalized:
            groups.setdefault(normalized, []).append(record)
    dismissed = _dismissed_refs(dismissals or [], alias_table)
    pairs: list[dict[str, Any]] = []
    disagreements: list[dict[str, Any]] = []
    skipped = {"already_merged": 0, "dismissed": 0}

    def admit(first: dict[str, Any], second: dict[str, Any], *, count: bool = True) -> bool:
        # Author disagreements are listed apart and do not change the skip
        # counts of the merge suggestions.
        if effective_candidate_id(_durable_id(first, alias_table), alias_table) == effective_candidate_id(
            _durable_id(second, alias_table), alias_table
        ):
            skipped["already_merged"] += count
            return False
        if _pair_ref(_durable_id(first, alias_table), _durable_id(second, alias_table), alias_table) in dismissed:
            skipped["dismissed"] += count
            return False
        return True

    def add_pair(title: str, first: dict[str, Any], second: dict[str, Any], signals: list[str], kind: str) -> None:
        # A discovery-only ISBN is a search hint, never identity evidence
        # (#531): it gives no ISBN signal and no edition split.
        first_isbn = _comparable_isbn(identity_isbn(first))
        second_isbn = _comparable_isbn(identity_isbn(second))
        if first_isbn and second_isbn:
            if first_isbn == second_isbn:
                signals = sorted({*signals, "isbn_match"})
            else:
                kind = POSSIBLE_EDITIONS
                signals = sorted({*signals, "different_isbns"})
        conflicting = _conflicting_fields(first, second, references or {})
        canonical, canonical_basis = _suggested_canonical(first, second)
        pairs.append(
            {
                "normalized_title": title,
                "kind": kind,
                "signals": signals,
                "editor_decisions_conflict": bool({"editor_status", "editor_note", "visibility"} & set(conflicting)),
                # Fields merge-candidate-identity refuses to merge until
                # they are resolved (a description through a
                # description_resolution block, the rest through feedback).
                "conflicting_fields": conflicting,
                "records": [_record_summary(first, alias_table), _record_summary(second, alias_table)],
                # The shape merge-candidate-identity --correction-file reads;
                # the canonical choice is a suggestion for the editor.
                "suggested_canonical_basis": canonical_basis,
                "correction_draft": {
                    "source_identity_keys": [str(first["identity_key"]), str(second["identity_key"])],
                    "canonical": {
                        "identity_key": str(canonical["identity_key"]),
                        "title": str(canonical.get("title", "") or ""),
                        "primary_authors": list(canonical.get("primary_authors") or []),
                    },
                },
            }
        )

    for title in sorted(groups):
        members = sorted(groups[title], key=lambda record: str(record["identity_key"]))
        for first, second in combinations(members, 2):
            signals = _author_signals(first, second)
            if signals is None:
                if admit(first, second, count=False):
                    disagreements.append(
                        {
                            "normalized_title": title,
                            "signals": ["no_shared_surname"],
                            "records": [_record_summary(first, alias_table), _record_summary(second, alias_table)],
                        }
                    )
                continue
            if admit(first, second):
                add_pair(title, first, second, signals, PROBABLE_DUPLICATE)

    # Records of different titles meet only through a shared surname, so the
    # cross-title rules never compare two unrelated authors (#916).
    by_surname: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if _record_title(record):
            for token in _surname_tokens(record):
                by_surname.setdefault(token, []).append(record)
    cross: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    for token in sorted(by_surname):
        for first, second in combinations(by_surname[token], 2):
            first, second = sorted((first, second), key=lambda record: str(record["identity_key"]))
            if _record_title(first) != _record_title(second):
                cross.setdefault((str(first["identity_key"]), str(second["identity_key"])), (first, second))
    for first, second in (cross[key] for key in sorted(cross)):
        first_title, second_title = _record_title(first), _record_title(second)
        signals = _author_signals(first, second)
        if signals is None:
            continue
        first_published, second_published = _published_english_title(first), _published_english_title(second)
        edition_signals = []
        if (first_published and first_published == second_title) or (
            second_published and second_published == first_title
        ):
            edition_signals.append("translated_title")
        if first_published and first_published == second_published:
            edition_signals.append("shared_english_title")
        if _confirmed_original_title(first) == second_title or _confirmed_original_title(second) == first_title:
            edition_signals.append("original_title")
        if edition_signals:
            if admit(first, second):
                add_pair(
                    min(first_title, second_title),
                    first,
                    second,
                    sorted({*signals, *edition_signals}),
                    POSSIBLE_EDITIONS,
                )
            continue
        contained = _contained_title(first_title, second_title)
        if contained and admit(first, second):
            add_pair(contained, first, second, sorted({*signals, "title_containment"}), PROBABLE_DUPLICATE)

    pairs.sort(key=lambda pair: (pair["normalized_title"], *pair["correction_draft"]["source_identity_keys"]))
    counts = {
        PROBABLE_DUPLICATE: sum(pair["kind"] == PROBABLE_DUPLICATE for pair in pairs),
        POSSIBLE_EDITIONS: sum(pair["kind"] == POSSIBLE_EDITIONS for pair in pairs),
        **{f"skipped_{reason}": count for reason, count in skipped.items()},
    }
    return {
        "record_count": len(records),
        "pair_count": len(pairs),
        "counts": counts,
        "pairs": pairs,
        "author_disagreement_count": len(disagreements),
        "author_disagreements": disagreements,
    }


def _history(store: StateStore) -> list[Any]:
    history = store.read(Collection.CANDIDATE_HISTORY)
    if not isinstance(history, list):
        raise ValueError("candidate history must be a list")
    return history


def probable_duplicates_command(
    state_dir: Path,
    *,
    dismiss_keys: list[str] | None = None,
    reason: str = "",
    confirmed_by_editor: bool = False,
    state_store: StateStore | None = None,
) -> int:
    """Print the report, or preview and record one dismissal.

    Exit 0 report printed or dismissal recorded, 3 dismissal preview (no
    mutation), 2 input error.
    """
    if not state_dir.is_dir():
        return _input_error(f"state directory not found: {state_dir}")
    store = open_state_store(state_dir) if state_store is None else state_store
    try:
        if not dismiss_keys:
            history, alias_table, dismissals, references = _load(state_dir, store)
            report = probable_duplicate_pairs(history, alias_table, dismissals, references)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        # One lock over reading, checking, and writing, so two confirmed
        # dismissals never overwrite each other.
        with bundle_write_lock(state_dir / STATE_PROBABLE_DUPLICATE_DISMISSALS):
            history, alias_table, dismissals, _references = _load(state_dir, store)
            return _dismiss(state_dir, history, alias_table, dismissals, dismiss_keys, reason, confirmed_by_editor)
    except (OSError, ValueError) as exc:
        return _input_error(str(exc))


def _input_error(message: str) -> int:
    print(json.dumps({"status": "input_error", "error": message}, ensure_ascii=False, indent=2))
    return 2


def _load(
    state_dir: Path, store: StateStore
) -> tuple[list[Any], dict[str, Any], list[dict[str, Any]], dict[Collection | str, Any]]:
    references: dict[Collection | str, Any] = {}
    for filename in MERGED_REFERENCE_FILES:
        if isinstance(filename, Collection):
            records = store.read(filename)
        else:
            records = read_json_or_default(state_dir / filename, [])
        if not isinstance(records, list):
            raise ValueError(f"{reference_label(filename)} must be a list")
        references[filename] = records
    return (
        _history(store),
        store.read(Collection.CANDIDATE_IDENTITY_ALIASES),
        load_dismissals(state_dir),
        references,
    )


def _dismiss(
    state_dir: Path,
    history: list[Any],
    alias_table: dict[str, Any],
    dismissals: list[dict[str, Any]],
    keys: list[str],
    reason: str,
    confirmed_by_editor: bool,
) -> int:
    error = _input_error
    if len(keys) != 2 or keys[0] == keys[1]:
        return error("a dismissal names exactly two different identity keys")
    if not reason.strip():
        return error("a dismissal needs the editor's reason")
    report = probable_duplicate_pairs(history, alias_table, dismissals)
    wanted = sorted(keys)
    pair = next(
        (item for item in report["pairs"] if sorted(item["correction_draft"]["source_identity_keys"]) == wanted),
        None,
    )
    if pair is None:
        return error("the two identity keys are not a reported probable-duplicate pair")
    first, second = pair["records"]
    candidate_ids = sorted(
        (
            first["candidate_id"],
            second["candidate_id"],
        )
    )
    entry = {
        "candidate_ids": candidate_ids,
        "identity_keys": wanted,
        "kind": pair["kind"],
        "reason": reason,
        "dismissed_date": date.today().isoformat(),
    }
    print(json.dumps({"dismissal": entry, "confirmation_required": not confirmed_by_editor}, ensure_ascii=False))
    if not confirmed_by_editor:
        return 3
    write_json(state_dir / STATE_PROBABLE_DUPLICATE_DISMISSALS, [*dismissals, entry])
    return 0
