"""Editorial Taste Profile state and rule application."""

from __future__ import annotations

import json
import re
from bisect import bisect_left
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from functools import cached_property
from pathlib import Path
from typing import Any

from autoscout.identity import normalize_identity_text
from autoscout.scouting_candidates import ScoutingCandidate
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import open_state_store


def _json_compact(obj: object) -> str:
    """Return a compact JSON representation with ensure_ascii=False."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ── Overlap detection (#282) ────────────────────────────────────────────────

# Generic connector/domain words filtered from probable-overlap token matching.
# These are words that appear so commonly in rule subjects that matches on them
# alone would create false positives (e.g. "history books" vs "science books"
# should NOT overlap just because both contain "books").
_GENERIC_WORDS: frozenset[str] = frozenset(
    normalize_identity_text(w)
    for w in [
        "book",
        "books",
        "topic",
        "topics",
        "themed",
        "rule",
        "author",
        "authors",
        "книга",
        "книги",
        "тема",
        "темы",
        "тематика",
        "тематики",
        "правило",
        "правила",
        "автор",
        "авторы",
    ]
)

# Function words (conjunctions, prepositions, and articles) are a closed
# allowlist, following the precedent of _GERMAN_MEDICAL_CREDENTIAL_PREFIXES in
# autoscout/identity.py: an explicit, enumerable set is safer than a heuristic
# because its behavior is fully predictable and auditable. A token-length
# threshold was rejected on purpose: it would silently drop every short token,
# including real two-letter proper nouns and meaningful stems, and it would
# widen with every unrelated subject. Each word below was added deliberately;
# a new false positive is fixed by extending this list, never by loosening
# the rule.
_FUNCTION_WORDS: frozenset[str] = frozenset(
    normalize_identity_text(w)
    for w in [
        # Russian conjunctions.
        "и",
        "а",
        "но",
        "да",
        "или",
        "либо",
        "же",
        "что",
        "чтобы",
        "как",
        "когда",
        "если",
        "тоже",
        "также",
        "однако",
        # Russian prepositions.
        "в",
        "во",
        "на",
        "по",
        "о",
        "об",
        "от",
        "до",
        "для",
        "при",
        "с",
        "со",
        "к",
        "ко",
        "у",
        "без",
        "из",
        "за",
        "над",
        "под",
        "про",
        "через",
        "между",
        "около",
        "вокруг",
        "после",
        "перед",
        # English conjunctions.
        "and",
        "or",
        "but",
        "nor",
        "yet",
        "so",
        # English articles.
        "the",
        "a",
        "an",
        # English prepositions.
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "by",
        "from",
        "about",
        "into",
        "onto",
        "over",
        "under",
        "between",
        "through",
        "after",
        "before",
    ]
)


@dataclass(frozen=True)
class OverlapDetail:
    """Structured overlap information for a single existing profile entry."""

    id: str
    subject: str
    verbatim_quote: str
    kind: str
    scope: str
    reason: str  # classification: "exact_duplicate" or "probable_overlap"
    detail: str = ""  # human-facing extra detail (e.g. shared tokens, "same_subject_different_behavior")

    def as_json(self) -> dict[str, str]:
        result: dict[str, str] = {
            "id": self.id,
            "subject": self.subject,
            "verbatim_quote": self.verbatim_quote,
            "kind": self.kind,
            "scope": self.scope,
            "reason": self.reason,
        }
        if self.detail:
            result["detail"] = self.detail
        return result


_TERM_WORD_SPLIT = re.compile(r"[\s\-/,]+")
PREFIX_MARKER = "*"
PHRASE_QUOTE = '"'
MIN_PREFIX_STEM_LENGTH = 4


def _is_phrase_term(term: str) -> bool:
    stripped = term.strip()
    return len(stripped) >= 2 and stripped.startswith(PHRASE_QUOTE) and stripped.endswith(PHRASE_QUOTE)


def normalize_topic_term(term: str) -> str:
    """Normalise a topic term, keeping its prefix markers and phrase quotes.

    A word written as ``трансгендер*`` matches every metadata word that starts
    with ``трансгендер`` (``трансгендерной``, ``трансгендерные``), so an editor
    term covers the inflected forms of a Russian word (#966). Words without the
    marker keep matching whole words only.

    A term wrapped in double quotes, such as ``"истори* кухн*"``, is a phrase:
    its words must stand next to each other in that order (#967). Without
    quotes, the words of a term may appear anywhere in the same field.
    """
    phrase = _is_phrase_term(term)
    body = term.strip()[1:-1] if phrase else term
    words: list[str] = []
    for raw in _TERM_WORD_SPLIT.split(body):
        normalized = normalize_identity_text(raw.rstrip(PREFIX_MARKER))
        if not normalized:
            continue
        parts = normalized.split()
        if raw.endswith(PREFIX_MARKER):
            parts[-1] += PREFIX_MARKER
        words.extend(parts)
    joined = " ".join(words)
    return f"{PHRASE_QUOTE}{joined}{PHRASE_QUOTE}" if phrase and joined else joined


def topic_term_error(term: str) -> str:
    """Return the error code for a malformed topic term, or ``""`` when it is usable.

    The prefix marker may only end a word, and the stem before it must be long
    enough that the term cannot match unrelated words by accident. Double
    quotes may only wrap the whole term.
    """
    stripped = term.strip()
    body = stripped[1:-1] if _is_phrase_term(stripped) else stripped
    if PHRASE_QUOTE in body:
        return "phrase_quotes_must_wrap_the_whole_term"
    for raw in _TERM_WORD_SPLIT.split(body):
        if PREFIX_MARKER in raw.rstrip(PREFIX_MARKER) or raw.endswith(PREFIX_MARKER * 2) or raw == PREFIX_MARKER:
            return "prefix_marker_must_end_a_word"
    words = normalize_topic_term(term).strip(PHRASE_QUOTE).split()
    if not words:
        return "term_has_no_words"
    for word in words:
        if word.endswith(PREFIX_MARKER) and len(word) - len(PREFIX_MARKER) < MIN_PREFIX_STEM_LENGTH:
            return "prefix_stem_too_short"
    return ""


def _normalized_topic_terms(stored: Any) -> list[str]:
    """Return the normalised, stably deduplicated terms of a stored term list."""
    if not isinstance(stored, list):
        return []
    return _deduplicate_matching_terms(
        [normalized for term in stored if (normalized := normalize_topic_term(str(term)))]
    )


def _effective_topic_terms(entry: Mapping[str, Any]) -> frozenset[str]:
    """Return the normalised effective matching terms for a topic rule entry.

    For entries with ``matching_terms`` the explicit terms are normalised;
    legacy entries (no matching_terms) use the normalised subject.
    """
    stored = entry.get("matching_terms")
    if isinstance(stored, list) and stored:
        return frozenset(normalize_topic_term(str(t)) for t in stored)
    return frozenset({normalize_identity_text(str(entry.get("subject", "")))})


def _topic_matching_terms_for_entry(entry: Mapping[str, Any]) -> list[str]:
    """Return explicit, normalized terms that are active for topic matching.

    Legacy topic entries without explicit terms remain loadable, but they are
    inert until an editor supplies terms because a descriptive subject is not
    a safe matching criterion.
    """
    stored = entry.get("matching_terms")
    if not isinstance(stored, list) or not stored:
        return []
    effective = _effective_topic_terms(entry)
    return [normalized for term in stored if (normalized := normalize_topic_term(str(term))) in effective]


def _topic_exception_terms_for_entry(entry: Mapping[str, Any]) -> list[str]:
    """Return the normalised exception terms of a topic entry (#967).

    A candidate that matches any exception term is outside the entry, even
    when it also matches a matching term: a cookbook rule can keep food
    history visible this way.
    """
    return _normalized_topic_terms(entry.get("exception_terms"))


def _normalized_subject(entry: dict[str, Any]) -> str:
    """Return normalised subject text for an entry."""
    return normalize_identity_text(str(entry.get("subject", "")))


def _normalized_direction(entry: dict[str, Any]) -> str:
    """Return normalised direction for a soft-preference entry, empty for hard rules."""
    if entry.get("kind") != "soft_preference":
        return ""
    return normalize_direction(str(entry.get("direction", "")))


def _topic_token_union(subject: str, terms: frozenset[str]) -> frozenset[str]:
    """Return the union of normalised subject tokens and effective matching-term tokens."""
    tokens: set[str] = set(normalize_identity_text(subject).split())
    for term in terms:
        tokens.update(word.rstrip(PREFIX_MARKER) for word in term.strip(PHRASE_QUOTE).split())
    return frozenset(tokens)


def _detect_overlaps(
    profile: dict[str, Any],
    normalized_kind: str,
    normalized_scope: str,
    normalized_subject: str,
    normalized_direction: str,
    effective_topic_terms: frozenset[str],
    effective_exception_terms: frozenset[str] = frozenset(),
) -> tuple[list[OverlapDetail], list[OverlapDetail]]:
    """Detect exact duplicates and probable overlaps among active profile entries.

    Returns ``(exact_duplicates, probable_overlaps)`` — each a list of
    ``OverlapDetail`` referencing the existing active entries that conflict.
    A topic entry's behaviour is its matching terms together with its
    exception terms, so entries that differ only in exceptions are not exact
    duplicates.

    Detection is deterministic only: no network, model, or translation service.
    """
    exacts: list[OverlapDetail] = []
    probables: list[OverlapDetail] = []

    # Pre-compute the candidate token union for topic rules.
    candidate_token_union: frozenset[str] = (
        _topic_token_union(normalized_subject, effective_topic_terms)
        if normalized_scope == "topic"
        else frozenset(normalized_subject.split())
    )

    for entry in profile.get("entries", []):
        if entry.get("status") != "active":
            continue
        if entry.get("kind") != normalized_kind:
            continue
        if entry.get("scope") != normalized_scope:
            continue

        existing_subject = _normalized_subject(entry)

        # ── Exact duplicate check ──
        # Same kind, scope, normalized subject, and behavior.
        # Topic hard rules: behavior = effective matching term set.
        # Soft preferences: behavior = direction.
        # Other hard rules: behavior is implicit (all hide).
        if normalized_scope == "topic":
            existing_terms = _effective_topic_terms(entry)
            same_topic_behavior = existing_terms == effective_topic_terms and (
                frozenset(_topic_exception_terms_for_entry(entry)) == effective_exception_terms
            )
            if normalized_kind == "soft_preference":
                same_topic_behavior = same_topic_behavior and (_normalized_direction(entry) == normalized_direction)
            if existing_subject == normalized_subject and same_topic_behavior:
                detail = OverlapDetail(
                    id=str(entry["id"]),
                    subject=str(entry["subject"]),
                    verbatim_quote=str(entry["verbatim_quote"]),
                    kind=normalized_kind,
                    scope=normalized_scope,
                    reason="exact_duplicate",
                )
                exacts.append(detail)
                continue
        elif normalized_kind == "soft_preference":
            existing_dir = _normalized_direction(entry)
            if existing_subject == normalized_subject and existing_dir == normalized_direction:
                detail = OverlapDetail(
                    id=str(entry["id"]),
                    subject=str(entry["subject"]),
                    verbatim_quote=str(entry["verbatim_quote"]),
                    kind=normalized_kind,
                    scope=normalized_scope,
                    reason="exact_duplicate",
                )
                exacts.append(detail)
                continue
        else:
            # author/publisher/source/source_family hard rules: only subject matters for duplication.
            if existing_subject == normalized_subject:
                detail = OverlapDetail(
                    id=str(entry["id"]),
                    subject=str(entry["subject"]),
                    verbatim_quote=str(entry["verbatim_quote"]),
                    kind=normalized_kind,
                    scope=normalized_scope,
                    reason="exact_duplicate",
                )
                exacts.append(detail)
                continue

        # ── Probable overlap check ──
        # Build the existing token union (subject + effective terms for topic rules).
        if normalized_scope == "topic":
            existing_token_union = _topic_token_union(existing_subject, _effective_topic_terms(entry))
        else:
            existing_token_union = frozenset(existing_subject.split())

        # Check 1: meaningful token overlap.
        shared_tokens = (existing_token_union & candidate_token_union) - (_GENERIC_WORDS | _FUNCTION_WORDS)
        if shared_tokens:
            detail = OverlapDetail(
                id=str(entry["id"]),
                subject=str(entry["subject"]),
                verbatim_quote=str(entry["verbatim_quote"]),
                kind=normalized_kind,
                scope=normalized_scope,
                reason="probable_overlap",
                detail=f"shared_tokens={_json_compact(sorted(shared_tokens))}",
            )
            probables.append(detail)
            continue

        # Check 2: same normalized subject but different behavior
        # (applies to all kinds: soft-pref direction, topic matching_terms, etc.).
        if existing_subject == normalized_subject:
            # For soft preferences: direction must differ.
            if normalized_kind == "soft_preference":
                existing_dir = _normalized_direction(entry)
                if existing_dir != normalized_direction:
                    detail = OverlapDetail(
                        id=str(entry["id"]),
                        subject=str(entry["subject"]),
                        verbatim_quote=str(entry["verbatim_quote"]),
                        kind=normalized_kind,
                        scope=normalized_scope,
                        reason="probable_overlap",
                        detail="same_subject_different_behavior",
                    )
                    probables.append(detail)
            else:
                # For hard rules (any scope): same subject always means probable
                # overlap — even topic rules where matching_terms differ are
                # caught here because exact-duplicate check already passed
                # and matching_terms must differ to reach this point.
                detail = OverlapDetail(
                    id=str(entry["id"]),
                    subject=str(entry["subject"]),
                    verbatim_quote=str(entry["verbatim_quote"]),
                    kind=normalized_kind,
                    scope=normalized_scope,
                    reason="probable_overlap",
                    detail="same_subject_different_behavior",
                )
                probables.append(detail)

    return exacts, probables


def _print_overlap_details(exacts: list[OverlapDetail], probables: list[OverlapDetail]) -> None:
    """Print machine-readable overlap information."""
    if exacts:
        print(f"exact_duplicates={len(exacts)}")
        for detail in exacts:
            print(f"exact_duplicate={_json_compact(detail.as_json())}")
    if probables:
        print(f"probable_overlaps={len(probables)}")
        for detail in probables:
            print(f"probable_overlap={_json_compact(detail.as_json())}")


KINDS = {"hard_rule", "soft_preference"}
SCOPES = {"author", "topic", "publisher", "source", "source_family"}
DIRECTIONS = {"positive", "negative"}
STATUSES = {"active", "revoked"}


class TasteProfileError(ValueError):
    pass


def empty_profile() -> dict[str, Any]:
    return {"entries": []}


def normalize_kind(kind: str) -> str:
    value = kind.strip().lower().replace("-", "_").replace(" ", "_")
    if value in {"hard", "rule"}:
        return "hard_rule"
    if value in {"soft", "preference"}:
        return "soft_preference"
    return value


def normalize_direction(direction: str) -> str:
    value = direction.strip().lower()
    if value in {"", "+", "plus", "positive", "up"}:
        return "positive"
    if value in {"-", "minus", "negative", "down"}:
        return "negative"
    return value


def _print_history_hide_preview(store: StateStore, rule: dict[str, Any]) -> None:
    """Print the Candidate History cards a Hard Rule will hide from the review (#1025).

    A Hard Rule hides every matching card of the cumulative review, not only
    the candidates of the next run, so the editor confirms the rule knowing
    how many cards she has already seen will leave the review. *rule* is the
    entry as it would be stored. An empty history prints ``history_matches=0``.
    """
    records = store.read(Collection.CANDIDATE_HISTORY)
    if not isinstance(records, list):
        records = []
    matched: list[dict[str, Any]] = []
    prepared = PreparedRule.of(rule)
    for record in records:
        if not isinstance(record, dict):
            continue
        candidate = rule_matchable_candidate(record)
        explanation = prepared.explain(candidate)
        if not explanation:
            continue
        row = {
            "title": str(candidate.data.get("title", "")),
            "author": ", ".join(candidate.data.get("primary_authors", [])),
            "identity_key": candidate.identity_key(),
            "match": explanation,
        }
        editor_status = str(record.get("editor_status", "")).strip()
        if editor_status:
            row["editor_status"] = editor_status
        matched.append(row)
    print(f"history_matches={len(matched)}")
    print("history_effect=matching Candidate History cards leave the review at the next render (reversible)")
    for row in matched:
        print(f"history_match={_json_compact(row)}")


def load_taste_profile(state_dir: Path | None) -> dict[str, Any]:
    """The Editorial Taste Profile of the State Store over *state_dir*; empty without state."""
    if state_dir is None:
        return empty_profile()
    profile: dict[str, Any] = open_state_store(state_dir).read(Collection.EDITORIAL_PROFILE)
    return profile


def save_taste_profile(state_dir: Path, profile: dict[str, Any]) -> None:
    """Replace the Editorial Taste Profile of the State Store over *state_dir*; it is validated first."""
    open_state_store(state_dir).replace(Collection.EDITORIAL_PROFILE, profile)


def validate_taste_profile(profile: Any) -> None:
    if not isinstance(profile, dict):
        raise TasteProfileError("malformed editorial taste profile: root must be an object")
    entries = profile.get("entries")
    if not isinstance(entries, list):
        raise TasteProfileError("malformed editorial taste profile: entries must be a list")
    seen_ids: set[str] = set()
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise TasteProfileError(f"malformed editorial taste profile: entries[{idx}] must be an object")
        for field in (
            "id",
            "kind",
            "scope",
            "subject",
            "verbatim_quote",
            "provenance",
            "created_at",
            "created_by",
            "status",
        ):
            if not isinstance(entry.get(field), str) or not entry.get(field, "").strip():
                raise TasteProfileError(
                    f"malformed editorial taste profile: entries[{idx}].{field} must be a non-empty string"
                )
        if entry["id"] in seen_ids:
            raise TasteProfileError(f"malformed editorial taste profile: duplicate id {entry['id']}")
        seen_ids.add(entry["id"])
        if entry["kind"] not in KINDS:
            raise TasteProfileError(
                f"malformed editorial taste profile: entries[{idx}].kind must be hard_rule or soft_preference"
            )
        if entry["scope"] not in SCOPES:
            raise TasteProfileError(
                f"malformed editorial taste profile: entries[{idx}].scope must be one of {sorted(SCOPES)}"
            )
        if entry["status"] not in STATUSES:
            raise TasteProfileError(
                f"malformed editorial taste profile: entries[{idx}].status must be active or revoked"
            )
        if entry["kind"] == "soft_preference":
            direction = entry.get("direction", "positive")
            if direction not in DIRECTIONS:
                raise TasteProfileError(
                    f"malformed editorial taste profile: entries[{idx}].direction must be positive or negative"
                )
        matching_terms = entry.get("matching_terms")
        if matching_terms is not None:
            if entry["scope"] != "topic":
                raise TasteProfileError(
                    f"malformed editorial taste profile: entries[{idx}].matching_terms is only allowed on topic entries"
                )
            if not isinstance(matching_terms, list) or not all(
                isinstance(t, str) and t.strip() for t in matching_terms
            ):
                raise TasteProfileError(
                    f"malformed editorial taste profile: entries[{idx}].matching_terms"
                    f" must be a non-empty list of non-blank strings"
                )
            if not matching_terms:
                raise TasteProfileError(
                    f"malformed editorial taste profile: entries[{idx}].matching_terms"
                    f" must be a non-empty list of non-blank strings"
                )
            _validate_stored_term_forms(idx, "matching_terms", matching_terms)
        exception_terms = entry.get("exception_terms")
        if exception_terms is not None:
            if not isinstance(matching_terms, list) or not matching_terms:
                raise TasteProfileError(
                    f"malformed editorial taste profile: entries[{idx}].exception_terms"
                    f" is only allowed on topic entries with matching_terms"
                )
            if (
                not isinstance(exception_terms, list)
                or not exception_terms
                or not all(isinstance(t, str) and t.strip() for t in exception_terms)
            ):
                raise TasteProfileError(
                    f"malformed editorial taste profile: entries[{idx}].exception_terms"
                    f" must be a non-empty list of non-blank strings"
                )
            _validate_stored_term_forms(idx, "exception_terms", exception_terms)
        if entry["status"] == "revoked":
            for field in ("revoked_at", "revoked_by", "revocation_reason"):
                if not isinstance(entry.get(field), str) or not entry.get(field, "").strip():
                    raise TasteProfileError(
                        f"malformed editorial taste profile: revoked entries[{idx}].{field} must be a non-empty string"
                    )
        # Optional audit-link fields (#282)
        for audit_field in ("replaced_by", "replaces"):
            value = entry.get(audit_field)
            if value is not None:
                if audit_field == "replaced_by":
                    if not isinstance(value, str) or not value.strip():
                        raise TasteProfileError(
                            f"malformed editorial taste profile: entries[{idx}].{audit_field} must be a non-empty string"
                        )
                elif audit_field == "replaces":
                    if (
                        not isinstance(value, list)
                        or not value
                        or not all(isinstance(i, str) and i.strip() for i in value)
                    ):
                        raise TasteProfileError(
                            f"malformed editorial taste profile: entries[{idx}].{audit_field} must be a non-empty list of non-blank strings"
                        )


def _validate_stored_term_forms(idx: int, field: str, terms: list[str]) -> None:
    """Reject a stored topic term whose form the matcher cannot apply safely."""
    for term in terms:
        error = topic_term_error(term)
        if error:
            raise TasteProfileError(f"malformed editorial taste profile: entries[{idx}].{field} term {term!r}: {error}")


def _term_form_error_lines(terms: list[str], error_name: str) -> list[str]:
    """Return the CLI error lines for the first malformed term, or ``[]``."""
    for term in terms:
        error = topic_term_error(term)
        if error:
            return [f"error={error_name}", f"term={term}", f"reason={error}"]
    return []


def next_entry_id(entries: list[dict[str, Any]]) -> str:
    max_seen = 0
    for entry in entries:
        ident = str(entry.get("id", ""))
        if ident.startswith("taste-") and ident[6:].isdigit():
            max_seen = max(max_seen, int(ident[6:]))
    return f"taste-{max_seen + 1:04d}"


def _deduplicate_matching_terms(terms: list[str]) -> list[str]:
    """Deduplicate *terms* stably, preserving first-supplied order."""
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        if term not in seen:
            seen.add(term)
            result.append(term)
    return result


OVERLAP_RESOLUTIONS = {"keep_existing", "replace_existing", "add_distinct"}


def _kind_label(kind: str) -> str:
    """Return the operator-facing label for a profile entry kind."""
    return "Hard Rule" if kind == "hard_rule" else "Soft Preference"


def _print_entry_effect(kind: str, direction: str) -> None:
    """Print the operator-facing effect line for one profile entry kind.

    Shared by the proposal preview and the set-terms preview so both paths
    describe identical effects. For soft preferences the direction is
    normalised here, defaulting to positive like everywhere else.
    """
    if kind == "hard_rule":
        print("effect=hide matching future candidates from recommendations (reversible; candidates remain in history)")
        return
    resolved_direction = normalize_direction(direction) or "positive"
    dir_label = (
        "positive (nudge priority up one step)"
        if resolved_direction == "positive"
        else "negative (nudge priority down one step)"
    )
    print(f"effect=ranking adjustment — {dir_label}; does not hide candidates")


def _print_proposal_preview(
    normalized_kind: str,
    normalized_scope: str,
    subject: str,
    quote: str,
    provenance: str,
    normalized_direction: str,
    description: str,
    effective_terms: list[str],
    store: StateStore,
    exception_terms: list[str] | None = None,
) -> None:
    """Print the full proposal preview (kind, scope, subject, quote, provenance,
    effect, matching terms, exception terms, and topic candidate impact).  Used
    by all preview paths so that overlap-enabled previews never regress
    #276/#281."""
    kind_label = _kind_label(normalized_kind)
    print("confirmation_required=true")
    print(f"kind={kind_label}")
    print(f"scope={normalized_scope}")
    print(f"subject={subject.strip()}")
    print(f"verbatim_quote={quote}")
    print(f"provenance={provenance.strip()}")
    if description.strip():
        print(f"description={description.strip()}")
    if normalized_scope == "topic":
        terms_for_preview = effective_terms if effective_terms else [subject.strip()]
        print(f"matching_terms={_json_compact(terms_for_preview)}")
        if exception_terms:
            print(f"exception_terms={_json_compact(exception_terms)}")
    _print_entry_effect(normalized_kind, normalized_direction)
    if normalized_scope == "topic":
        _print_topic_candidate_impact_preview(store, subject.strip(), effective_terms, exception_terms or [])
    if normalized_kind == "hard_rule":
        _print_history_hide_preview(
            store,
            {
                "scope": normalized_scope,
                "subject": subject.strip(),
                "matching_terms": list(effective_terms),
                "exception_terms": list(exception_terms or []),
            },
        )


def add_profile_entry(
    state_dir: Path,
    kind: str,
    scope: str,
    subject: str,
    quote: str,
    provenance: str,
    created_by: str,
    direction: str = "",
    description: str = "",
    example_books: str = "",
    confirmed_by_editor: bool = False,
    matching_terms: list[str] | None = None,
    overlap_resolution: str = "",
    replace_ids: list[str] | None = None,
    replacement_reason: str = "",
    exception_terms: list[str] | None = None,
    state_store: StateStore | None = None,
) -> int:
    normalized_kind = normalize_kind(kind)
    normalized_scope = scope.strip().lower().replace(" ", "_")
    if normalized_kind not in KINDS:
        print("profile_entry_created=false")
        print("error=invalid_kind")
        return 2
    if normalized_scope not in SCOPES:
        print("profile_entry_created=false")
        print("error=invalid_scope")
        return 2
    if not subject.strip() or not quote.strip() or not provenance.strip():
        print("profile_entry_created=false")
        print("error=subject_quote_and_provenance_required")
        return 2
    normalized_direction = normalize_direction(direction)
    if normalized_kind == "soft_preference" and normalized_direction not in DIRECTIONS:
        print("profile_entry_created=false")
        print("error=invalid_direction")
        return 2

    # Matching terms are valid for both topic Hard Rules and topic Soft Preferences.
    if matching_terms is not None and normalized_scope != "topic":
        print("profile_entry_created=false")
        print("error=matching_terms_only_allowed_for_topic_entries")
        return 2

    effective_terms: list[str]
    if matching_terms is not None:
        trimmed = [term.strip() for term in matching_terms if term.strip()]
        if not trimmed:
            print("profile_entry_created=false")
            print("error=matching_terms_must_be_non_empty")
            return 2
        effective_terms = _deduplicate_matching_terms(trimmed)
    else:
        effective_terms = []

    if normalized_scope == "topic" and not effective_terms:
        print("profile_entry_created=false")
        print("error=matching_terms_required_for_topic")
        return 2

    # Exception terms narrow a topic entry (#967) and need its matching terms.
    effective_exceptions: list[str] = []
    if exception_terms is not None:
        if normalized_scope != "topic":
            print("profile_entry_created=false")
            print("error=exception_terms_only_allowed_for_topic_entries")
            return 2
        effective_exceptions = _deduplicate_matching_terms([term.strip() for term in exception_terms if term.strip()])
        if not effective_exceptions:
            print("profile_entry_created=false")
            print("error=exception_terms_must_be_non_empty")
            return 2

    term_errors = _term_form_error_lines(effective_terms, "invalid_matching_term") or _term_form_error_lines(
        effective_exceptions, "invalid_exception_term"
    )
    if term_errors:
        print("profile_entry_created=false")
        for line in term_errors:
            print(line)
        return 2
    shared_terms = {normalize_topic_term(t) for t in effective_terms} & {
        normalize_topic_term(t) for t in effective_exceptions
    }
    if shared_terms:
        # The entry could never apply: every match would also be an exception.
        print("profile_entry_created=false")
        print("error=exception_term_repeats_matching_term")
        print(f"terms={_json_compact(sorted(shared_terms))}")
        return 2

    # Validate overlap-resolution flag values early.
    overlap_resolution = overlap_resolution.strip().lower().replace("-", "_").replace(" ", "_")
    if overlap_resolution and overlap_resolution not in OVERLAP_RESOLUTIONS:
        print("profile_entry_created=false")
        print("error=invalid_overlap_resolution")
        return 2

    # ── Load profile for overlap detection ──
    store = open_state_store(state_dir) if state_store is None else state_store
    try:
        profile = store.read(Collection.EDITORIAL_PROFILE)
    except TasteProfileError as exc:
        print(str(exc))
        return 1

    # Build normalised subject and effective topic terms for overlap detection.
    norm_subject = normalize_identity_text(subject.strip())
    norm_effective_topic_terms: frozenset[str]
    if normalized_scope == "topic":
        terms_for_detection = effective_terms if effective_terms else [subject.strip()]
        norm_effective_topic_terms = frozenset(normalize_topic_term(t) for t in terms_for_detection)
    else:
        norm_effective_topic_terms = frozenset()

    exacts, probables = _detect_overlaps(
        profile,
        normalized_kind,
        normalized_scope,
        norm_subject,
        normalized_direction,
        norm_effective_topic_terms,
        frozenset(normalize_topic_term(t) for t in effective_exceptions),
    )

    # ── Exact duplicates: no new effect needed ──
    if exacts:
        print("profile_entry_created=false")
        print("confirmation_required=false")
        print("reason=exact_duplicate_exists_no_new_effect_needed")
        _print_overlap_details(exacts, [])
        return 0

    # ── Reject overlap-resolution flags when no probable overlap exists (#282 finding 3) ──
    if not probables and (overlap_resolution or replace_ids or replacement_reason.strip()):
        print("profile_entry_created=false")
        print("error=overlap_resolution_supplied_without_probable_overlap")
        return 2

    # ── Reject auxiliary flags unless resolution is replace-existing ──
    # (also covers blank resolution with aux flags — finding 1, review round 2)
    if overlap_resolution != "replace_existing":
        if replace_ids:
            print("profile_entry_created=false")
            print("error=replace_ids_only_valid_with_replace_existing_resolution")
            return 2
        if replacement_reason.strip():
            print("profile_entry_created=false")
            print("error=replacement_reason_only_valid_with_replace_existing_resolution")
            return 2

    # ── Probable overlap: must be resolved ──
    if probables:
        probable_ids = {d.id for d in probables}

        if not overlap_resolution:
            # Unresolved probable overlap — preview with full proposal info, zero mutation.
            _print_proposal_preview(
                normalized_kind,
                normalized_scope,
                subject,
                quote,
                provenance,
                normalized_direction,
                description,
                effective_terms,
                store,
                exception_terms=effective_exceptions,
            )
            print("profile_entry_created=false")
            print("reason=probable_overlap_unresolved")
            _print_overlap_details([], probables)
            print(f"resolution_options={_json_compact(sorted(OVERLAP_RESOLUTIONS))}")
            print("hint=resolve with --overlap-resolution keep-existing|replace-existing|add-distinct")
            return 3

        if overlap_resolution == "keep_existing":
            # Explicit keep-existing: no mutation, report existing rules remain.
            print("profile_entry_created=false")
            print("confirmation_required=false")
            print("reason=keep_existing_probable_overlap_rules_preserved")
            _print_overlap_details([], probables)
            return 0

        if overlap_resolution == "replace_existing":
            provided_ids = set(replace_ids or [])
            if not provided_ids:
                print("profile_entry_created=false")
                print("error=replace_existing_requires_explicit_replace_ids")
                print(f"probable_overlap_ids={_json_compact(sorted(probable_ids))}")
                return 2
            if provided_ids != probable_ids:
                missing = probable_ids - provided_ids
                extra = provided_ids - probable_ids
                print("profile_entry_created=false")
                print("error=replace_ids_must_match_all_current_probable_overlap_ids")
                if missing:
                    print(f"missing_ids={_json_compact(sorted(missing))}")
                if extra:
                    print(f"stale_or_extra_ids={_json_compact(sorted(extra))}")
                return 2
            if not replacement_reason.strip():
                print("profile_entry_created=false")
                print("error=replacement_reason_required")
                return 2

            if not confirmed_by_editor:
                # Preview replacement — full proposal + overlap info, zero mutation.
                _print_proposal_preview(
                    normalized_kind,
                    normalized_scope,
                    subject,
                    quote,
                    provenance,
                    normalized_direction,
                    description,
                    effective_terms,
                    store,
                    exception_terms=effective_exceptions,
                )
                print("profile_entry_created=false")
                print("reason=replace_existing_preview")
                _print_overlap_details([], probables)
                print(f"replace_ids={_json_compact(sorted(provided_ids))}")
                print(f"replacement_reason={replacement_reason.strip()}")
                print("hint=re-run with --confirmed-by-editor to apply replacement")
                return 3

            # ── Apply: atomic revoke+add with bidirectional audit links ──
            new_id = next_entry_id(profile["entries"])
            revoked_at = date.today().isoformat()
            revoked_by = created_by.strip() or "agent"

            for entry in profile["entries"]:
                if entry.get("id") in probable_ids:
                    # Preserve all existing data; mark revoked with replacement link.
                    entry["status"] = "revoked"
                    entry["revoked_at"] = revoked_at
                    entry["revoked_by"] = revoked_by
                    entry["revocation_reason"] = replacement_reason.strip()
                    entry["replaced_by"] = new_id

            new_entry: dict[str, Any] = {
                "id": new_id,
                "kind": normalized_kind,
                "scope": normalized_scope,
                "subject": subject.strip(),
                "verbatim_quote": quote,
                "provenance": provenance.strip(),
                "created_at": revoked_at,
                "created_by": created_by.strip() or "agent",
                "status": "active",
                "replaces": sorted(probable_ids),
            }
            if description.strip():
                new_entry["description"] = description.strip()
            if example_books.strip():
                new_entry["example_books"] = [book.strip() for book in example_books.split(",") if book.strip()]
            if normalized_kind == "soft_preference":
                new_entry["direction"] = normalized_direction
            if normalized_scope == "topic":
                new_entry["matching_terms"] = effective_terms
            if effective_exceptions:
                new_entry["exception_terms"] = effective_exceptions

            profile["entries"].append(new_entry)
            store.replace(Collection.EDITORIAL_PROFILE, profile)

            print("profile_entry_created=true")
            print(f"id={new_id}")
            print(f"kind={new_entry['kind']}")
            print(f"status={new_entry['status']}")
            print(f"replaces={_json_compact(sorted(probable_ids))}")
            for pid in sorted(probable_ids):
                print(f"revoked={pid}")
            return 0

        if overlap_resolution == "add_distinct":
            if not confirmed_by_editor:
                # Preview — full proposal + overlap info, zero mutation.
                _print_proposal_preview(
                    normalized_kind,
                    normalized_scope,
                    subject,
                    quote,
                    provenance,
                    normalized_direction,
                    description,
                    effective_terms,
                    store,
                    exception_terms=effective_exceptions,
                )
                print("profile_entry_created=false")
                print("reason=add_distinct_preview")
                _print_overlap_details([], probables)
                print("resolution=add_distinct")
                print("hint=re-run with --confirmed-by-editor to add as distinct")
                return 3
            # Fall through to normal add below.

    # ── Normal preview path (no overlaps or add-distinct not yet confirmed) ──
    if not confirmed_by_editor:
        _print_proposal_preview(
            normalized_kind,
            normalized_scope,
            subject,
            quote,
            provenance,
            normalized_direction,
            description,
            effective_terms,
            store,
            exception_terms=effective_exceptions,
        )
        if probables:
            # add-distinct preview: show overlap info + resolution context
            _print_overlap_details([], probables)
            print("resolution=add_distinct")
        print("hint=re-run with --confirmed-by-editor to apply")
        return 3

    # ── Normal confirmed add ──
    try:
        # The profile is read, the entry allocated and written in one store transaction.
        with store.transaction():
            profile = store.read(Collection.EDITORIAL_PROFILE)
            _entry: dict[str, Any] = {
                "id": next_entry_id(profile["entries"]),
                "kind": normalized_kind,
                "scope": normalized_scope,
                "subject": subject.strip(),
                "verbatim_quote": quote,
                "provenance": provenance.strip(),
                "created_at": date.today().isoformat(),
                "created_by": created_by.strip() or "agent",
                "status": "active",
            }
            if description.strip():
                _entry["description"] = description.strip()
            if example_books.strip():
                _entry["example_books"] = [book.strip() for book in example_books.split(",") if book.strip()]
            if normalized_kind == "soft_preference":
                _entry["direction"] = normalized_direction
            if normalized_scope == "topic":
                _entry["matching_terms"] = effective_terms
            if effective_exceptions:
                _entry["exception_terms"] = effective_exceptions
            profile["entries"].append(_entry)
            store.replace(Collection.EDITORIAL_PROFILE, profile)
    except TasteProfileError as exc:
        print(str(exc))
        return 1
    print("profile_entry_created=true")
    print(f"id={_entry['id']}")
    print(f"kind={_entry['kind']}")
    print(f"status={_entry['status']}")
    return 0


def list_profile_entries(state_dir: Path, state_store: StateStore | None = None) -> int:
    store = open_state_store(state_dir) if state_store is None else state_store
    try:
        profile = store.read(Collection.EDITORIAL_PROFILE)
    except TasteProfileError as exc:
        print(str(exc))
        return 1
    if not profile["entries"]:
        print("Editorial Taste Profile is empty.")
        return 0
    print("Editorial Taste Profile")
    for entry in sorted(profile["entries"], key=lambda item: str(item["id"])):
        label = _kind_label(entry["kind"])
        direction_str = ""
        if entry["kind"] == "soft_preference" and entry.get("direction"):
            direction_str = f" ({entry['direction']})"
        display_status = entry["status"]
        if entry["status"] == "active" and entry["scope"] == "topic" and not _topic_matching_terms_for_entry(entry):
            display_status = "needs_terms"
        print(
            f"- {entry['id']} [{display_status}] {label}{direction_str} for {entry['scope']} {entry['subject']}: "
            f"“{entry['verbatim_quote']}”"
        )
        print(f"  Provenance: {entry['provenance']}; created {entry['created_at']} by {entry['created_by']}")
        if entry.get("description"):
            print(f"  Description: {entry['description']}")
        if entry.get("example_books"):
            print(f"  Example books: {', '.join(entry['example_books'])}")
        if entry["scope"] == "topic":
            terms = entry.get("matching_terms")
            if isinstance(terms, list) and terms:
                print(f"  Matching terms: {_json_compact(terms)}")
            exceptions = entry.get("exception_terms")
            if isinstance(exceptions, list) and exceptions:
                print(f"  Exception terms: {_json_compact(exceptions)}")
        if entry["status"] == "revoked":
            print(f"  Revoked {entry['revoked_at']} by {entry['revoked_by']}: {entry['revocation_reason']}")
        if entry.get("replaces"):
            print(f"  Replaced entries: {_json_compact(entry['replaces'])}")
        if entry.get("replaced_by"):
            print(f"  Replaced by: {entry['replaced_by']}")
    return 0


def revoke_profile_entry(
    state_dir: Path,
    entry_id: str,
    reason: str,
    revoked_by: str,
    confirmed_by_editor: bool = False,
    state_store: StateStore | None = None,
) -> int:
    if not reason.strip():
        print("profile_entry_revoked=false")
        print("error=revocation_reason_required")
        return 2
    store = open_state_store(state_dir) if state_store is None else state_store
    try:
        profile = store.read(Collection.EDITORIAL_PROFILE)
    except TasteProfileError as exc:
        print(str(exc))
        return 1
    entry = next((item for item in profile["entries"] if item.get("id") == entry_id), None)
    if entry is None:
        print("profile_entry_revoked=false")
        print("error=profile_entry_not_found")
        return 1
    if entry.get("status") == "revoked":
        print("profile_entry_revoked=false")
        print("error=profile_entry_already_revoked")
        print(f"id={entry_id}")
        print(f"subject={entry['subject']}")
        print(f"revoked_at={entry.get('revoked_at', '')}")
        print(f"revocation_reason={entry.get('revocation_reason', '')}")
        return 1

    if not confirmed_by_editor:
        print("confirmation_required=true")
        print(f"id={entry['id']}")
        print(f"kind={entry['kind']}")
        print(f"scope={entry['scope']}")
        print(f"subject={entry['subject']}")
        print(f"verbatim_quote={entry['verbatim_quote']}")
        print(f"revocation_reason={reason.strip()}")
        print("hint=re-run with --confirmed-by-editor to apply")
        return 3

    try:
        # The profile is read, decided on and written in one store transaction.
        with store.transaction():
            # Re-load fresh profile to avoid stale state.
            profile = store.read(Collection.EDITORIAL_PROFILE)
            entry = next((item for item in profile["entries"] if item.get("id") == entry_id), None)
            if entry is None:
                print("profile_entry_revoked=false")
                print("error=profile_entry_not_found")
                return 1
            if entry.get("status") == "revoked":
                print("profile_entry_revoked=false")
                print("error=profile_entry_already_revoked")
                print(f"id={entry_id}")
                return 1
            entry["status"] = "revoked"
            entry["revoked_at"] = date.today().isoformat()
            entry["revoked_by"] = revoked_by.strip() or "agent"
            entry["revocation_reason"] = reason.strip()
            store.replace(Collection.EDITORIAL_PROFILE, profile)
    except TasteProfileError as exc:
        print(str(exc))
        return 1
    print("profile_entry_revoked=true")
    print(f"id={entry_id}")
    return 0


def _resolve_set_terms_entry(profile: dict[str, Any], entry_id: str) -> tuple[dict[str, Any] | None, str]:
    """Resolve one profile entry for set-terms and return (entry, error_code).

    Returns ``(entry, "")`` when the entry is an active topic entry without
    explicit matching terms; otherwise returns ``(None, error_code)``.
    """
    entry = next((item for item in profile.get("entries", []) if item.get("id") == entry_id), None)
    if entry is None:
        return None, "profile_entry_not_found"
    if entry.get("status") == "revoked":
        return None, "profile_entry_revoked"
    if entry.get("scope") != "topic":
        return None, "profile_entry_not_topic"
    stored_terms = entry.get("matching_terms")
    if isinstance(stored_terms, list) and stored_terms:
        return None, "profile_entry_already_has_terms"
    return entry, ""


def set_profile_entry_terms(
    state_dir: Path,
    entry_id: str,
    matching_terms: list[str] | None,
    confirmed_by_editor: bool = False,
    state_store: StateStore | None = None,
) -> int:
    """Supply explicit matching terms for one existing active topic entry.

    Two-phase like add/revoke: the preview run (without
    ``--confirmed-by-editor``) shows the existing entry facts, the requested
    deduplicated terms, the effect, and current candidate matches, and
    performs zero mutation (return code 3). The confirmed run updates the
    same entry in place, preserving ``verbatim_quote`` and ``provenance``
    byte for byte, without allocating a new ID.

    Restricted to active topic entries that do not already carry explicit
    terms, so an existing Hard Rule term set can never be silently broadened
    or replaced through this operation.
    """
    if matching_terms is None or not any(term.strip() for term in matching_terms):
        print("profile_entry_terms_set=false")
        print("error=matching_terms_required")
        print(f"id={entry_id}")
        return 2
    deduped = _deduplicate_matching_terms([term.strip() for term in matching_terms if term.strip()])
    term_errors = _term_form_error_lines(deduped, "invalid_matching_term")
    if term_errors:
        print("profile_entry_terms_set=false")
        for line in term_errors:
            print(line)
        print(f"id={entry_id}")
        return 2

    store = open_state_store(state_dir) if state_store is None else state_store
    try:
        profile = store.read(Collection.EDITORIAL_PROFILE)
    except TasteProfileError as exc:
        print(str(exc))
        return 1
    entry, error = _resolve_set_terms_entry(profile, entry_id)
    if entry is None:
        print("profile_entry_terms_set=false")
        print(f"error={error}")
        print(f"id={entry_id}")
        return 1

    kind_label = _kind_label(str(entry.get("kind")))

    if not confirmed_by_editor:
        print("confirmation_required=true")
        print(f"id={entry['id']}")
        print(f"kind={kind_label}")
        print(f"scope={entry['scope']}")
        print(f"subject={entry['subject']}")
        print(f"verbatim_quote={entry['verbatim_quote']}")
        print(f"provenance={entry['provenance']}")
        print(f"matching_terms={_json_compact(deduped)}")
        _print_entry_effect(str(entry.get("kind")), str(entry.get("direction", "")))
        _print_topic_candidate_impact_preview(store, str(entry["subject"]), deduped)
        if entry.get("kind") == "hard_rule":
            _print_history_hide_preview(store, {**entry, "matching_terms": deduped})
        print("hint=re-run with --confirmed-by-editor to apply")
        return 3

    try:
        # No mutation happened between the resolve above and here, so the
        # already-resolved entry is still valid; mutate it and save once.
        entry["matching_terms"] = deduped
        store.replace(Collection.EDITORIAL_PROFILE, profile)
    except TasteProfileError as exc:
        print(str(exc))
        return 1
    print("profile_entry_terms_set=true")
    print(f"id={entry_id}")
    print(f"kind={kind_label}")
    print(f"status={entry['status']}")
    return 0


def active_hard_rules(profile: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        entry
        for entry in profile.get("entries", [])
        if entry.get("status") == "active" and entry.get("kind") == "hard_rule"
    ]


def active_soft_preferences(profile: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        entry
        for entry in profile.get("entries", [])
        if entry.get("status") == "active" and entry.get("kind") == "soft_preference"
    ]


# Backward-compatible alias for tests that import the old name.
active_author_hard_rules = active_hard_rules


def author_match_keys(value: str) -> set[str]:
    normalized = normalize_identity_text(value)
    keys = {normalized} if normalized else set()
    if "," in value:
        parts = [part.strip() for part in value.split(",", 1)]
        if all(parts):
            keys.add(normalize_identity_text(f"{parts[1]} {parts[0]}"))
    return keys


# How the rule matchers read a candidate and a rule (#1165).
#
# The review judges every Candidate History record against every active
# Hard Rule on each render, and a topic rule carries dozens of terms. Each
# rule is therefore prepared once (``PreparedRule``): its terms normalised and
# split into words, its subject split into tokens. Each candidate is read
# once per judgement (``_RuleSubject``): its metadata fields normalised and
# split into words on first use. The matching rules themselves (#551, #966,
# #967) are unchanged; only the repeated work is gone.

_ENGLISH_METADATA_FIELDS = frozenset({"English title", "English annotation"})
_CYRILLIC_LETTER = re.compile(r"[а-яё]", flags=re.IGNORECASE)
_SUBJECT_TOKEN_SPLIT = re.compile(r"[\s\-/,]+")


def _subject_tokens(value: str) -> frozenset[str]:
    """Return the tokens ``_token_match`` compares, split on space, hyphen, slash and comma."""
    return frozenset(token for token in _SUBJECT_TOKEN_SPLIT.split(value) if token)


def _token_match(subject: str, field_value: str) -> bool:
    """Check whether all tokens of *subject* appear in *field_value*.

    Tokens are split on common delimiters (space, hyphen, slash, comma)
    so that e.g. "fiction" does not match "non-fiction", but
    "Bad Press" matches "Bad Press Inc.".
    """
    subject_tokens = _subject_tokens(subject)
    return bool(subject_tokens) and subject_tokens <= _subject_tokens(field_value)


@dataclass(frozen=True)
class _ValueWords:
    """The words of one normalised metadata value, split once."""

    words: tuple[str, ...]
    word_set: frozenset[str]
    sorted_words: tuple[str, ...]

    @classmethod
    def of(cls, normalized_value: str) -> _ValueWords:
        words = tuple(normalized_value.split())
        word_set = frozenset(words)
        return cls(words, word_set, tuple(sorted(word_set)))

    def has_word_starting_with(self, stem: str) -> bool:
        # The words that start with *stem* sort together, from where *stem* would go.
        index = bisect_left(self.sorted_words, stem)
        return index < len(self.sorted_words) and self.sorted_words[index].startswith(stem)


def _term_word_matches(term_word: str, value_word: str) -> bool:
    if term_word.endswith(PREFIX_MARKER):
        return value_word.startswith(term_word[: -len(PREFIX_MARKER)])
    return term_word == value_word


@dataclass(frozen=True)
class _TermPattern:
    """The words of one normalised topic term, split once, with its phrase and prefix forms."""

    phrase: bool
    words: tuple[str, ...]
    whole_words: frozenset[str]
    prefix_stems: tuple[str, ...]

    @classmethod
    def of(cls, normalized_term: str) -> _TermPattern:
        words = tuple(normalized_term.strip(PHRASE_QUOTE).split())
        return cls(
            phrase=_is_phrase_term(normalized_term),
            words=words,
            whole_words=frozenset(word for word in words if not word.endswith(PREFIX_MARKER)),
            prefix_stems=tuple(word[: -len(PREFIX_MARKER)] for word in words if word.endswith(PREFIX_MARKER)),
        )

    def matches(self, value: _ValueWords) -> bool:
        """Check whether the term matches *value*.

        A plain word must equal a metadata word, so "fiction" does not match
        "fictional". A word ending in the prefix marker matches any metadata
        word that starts with its stem (#966). The words of an unquoted term
        may appear anywhere in the value; the words of a quoted phrase must be
        adjacent and in order (#967).
        """
        if not self.words or not self.whole_words <= value.word_set:
            return False
        if not all(value.has_word_starting_with(stem) for stem in self.prefix_stems):
            return False
        if not self.phrase:
            return True
        # Every word of the phrase occurs somewhere; now they must stand together.
        width = len(self.words)
        value_words = value.words
        return any(
            all(
                _term_word_matches(word, value_word)
                for word, value_word in zip(self.words, value_words[start : start + width], strict=True)
            )
            for start in range(len(value_words) - width + 1)
        )


def _topic_term_match(normalized_term: str, normalized_value: str) -> bool:
    """Check whether a normalised topic term matches *normalized_value* (see ``_TermPattern.matches``)."""
    return _TermPattern.of(normalized_term).matches(_ValueWords.of(normalized_value))


@dataclass(frozen=True)
class PreparedTopicTerm:
    """One topic term as the matchers use it.

    ``term`` is the term as a match explanation names it; the pattern is its
    normalised form. A term with a Russian letter is not matched against the
    English title and annotation.
    """

    term: str
    russian: bool
    pattern: _TermPattern

    @classmethod
    def of(cls, term: str) -> PreparedTopicTerm:
        normalized = normalize_topic_term(term)
        return cls(term, bool(_CYRILLIC_LETTER.search(normalized)), _TermPattern.of(normalized))

    def matching_field(self, fields: Sequence[tuple[str, _ValueWords]]) -> str:
        """Return the label of the first of *fields* the term matches, or ``""``."""
        for label, value in fields:
            if self.russian and label in _ENGLISH_METADATA_FIELDS:
                continue
            if self.pattern.matches(value):
                return label
        return ""


def topic_metadata_fields(candidate: ScoutingCandidate) -> dict[str, str]:
    fields = {
        "title": str(candidate.data.get("title", "")),
        "category/topic": str(candidate.data.get("candidate_category", "")),
        "short description": str(candidate.data.get("short_description", "")),
        "description": str(candidate.data.get("content_description", "")),
        "English title": str(candidate.data.get("english_title", "")),
        "English annotation": str(candidate.data.get("english_annotation", "")),
    }
    evidence_notes = " ".join(
        str(evidence.get("evidence_note", "")) for evidence in candidate.data.get("source_evidence", [])
    )
    if evidence_notes.strip():
        fields["source evidence note"] = evidence_notes
    return {label: value for label, value in fields.items() if value.strip()}


class _RuleSubject:
    """What the rule matchers read from one candidate, each part worked out on first use.

    The candidate must not change while its ``_RuleSubject`` is in use.
    """

    def __init__(self, candidate: ScoutingCandidate) -> None:
        self.candidate = candidate
        self._evidence_tokens: dict[str, tuple[frozenset[str], ...]] = {}

    @cached_property
    def topic_fields(self) -> tuple[tuple[str, _ValueWords], ...]:
        return tuple(
            (label, _ValueWords.of(normalize_identity_text(value)))
            for label, value in topic_metadata_fields(self.candidate).items()
        )

    @cached_property
    def author_keys(self) -> frozenset[str]:
        keys: set[str] = set()
        for author in self.candidate.data.get("primary_authors", []):
            keys |= author_match_keys(str(author))
        return frozenset(keys)

    @cached_property
    def publisher_tokens(self) -> frozenset[str]:
        publisher = str(self.candidate.data.get("publisher", "")).strip().lower()
        return _subject_tokens(publisher) if publisher else frozenset()

    def evidence_tokens(self, field_name: str) -> tuple[frozenset[str], ...]:
        """Return the tokens of *field_name* in each source evidence item, in order."""
        if field_name not in self._evidence_tokens:
            self._evidence_tokens[field_name] = tuple(
                _subject_tokens(str(evidence.get(field_name, "")).lower())
                for evidence in self.candidate.data.get("source_evidence", [])
            )
        return self._evidence_tokens[field_name]


def _first_prepared_term_match(subject: _RuleSubject, terms: Sequence[PreparedTopicTerm]) -> tuple[str, str]:
    """Return ``(term, field)`` for the first of *terms* that matches, or empty strings."""
    for term in terms:
        field = term.matching_field(subject.topic_fields)
        if field:
            return term.term, field
    return "", ""


def _topic_terms_match(
    subject: _RuleSubject,
    matching_terms: Sequence[PreparedTopicTerm],
    exception_terms: Sequence[PreparedTopicTerm],
) -> tuple[str, str]:
    """Return ``(term, field)`` when a matching term matches and no exception term does (#967)."""
    term, field = _first_prepared_term_match(subject, matching_terms)
    if not term or _first_prepared_term_match(subject, exception_terms)[0]:
        return "", ""
    return term, field


def _prepared_topic_terms(
    entry: Mapping[str, Any],
) -> tuple[tuple[PreparedTopicTerm, ...], tuple[PreparedTopicTerm, ...]]:
    """Return the prepared matching terms and exception terms of a topic entry."""
    return (
        tuple(PreparedTopicTerm.of(term) for term in _topic_matching_terms_for_entry(entry)),
        tuple(PreparedTopicTerm.of(term) for term in _topic_exception_terms_for_entry(entry)),
    )


@dataclass(frozen=True)
class PreparedRule:
    """An Editorial Taste Profile entry made ready for matching, once per render or run (#1165).

    It holds the entry as it was when prepared; prepare it again after the
    entry changes.
    """

    entry: Mapping[str, Any]
    scope: str
    subject: str
    matching_terms: tuple[PreparedTopicTerm, ...] = ()
    exception_terms: tuple[PreparedTopicTerm, ...] = ()
    subject_author_keys: frozenset[str] = frozenset()
    subject_tokens: frozenset[str] = frozenset()

    @classmethod
    def of(cls, entry: Mapping[str, Any] | PreparedRule) -> PreparedRule:
        if isinstance(entry, PreparedRule):
            return entry
        scope = str(entry.get("scope", ""))
        subject = str(entry.get("subject", ""))
        if scope == "author":
            return cls(entry, scope, subject, subject_author_keys=frozenset(author_match_keys(subject)))
        if scope == "topic":
            matching_terms, exception_terms = _prepared_topic_terms(entry)
            return cls(entry, scope, subject, matching_terms=matching_terms, exception_terms=exception_terms)
        return cls(entry, scope, subject, subject_tokens=_subject_tokens(subject.strip().lower()))

    def explain(self, candidate: ScoutingCandidate) -> str:
        """Return why the entry matches *candidate*, or ``""`` when it does not."""
        return self.match_explanation(_RuleSubject(candidate))

    def match_explanation(self, candidate: _RuleSubject) -> str:
        """Return why the entry matches *candidate*, or ``""`` when it does not."""
        subject = self.subject
        if self.scope == "author":
            if self.subject_author_keys & candidate.author_keys:
                return f"author metadata matched “{subject}”"
            return ""
        if self.scope == "topic":
            term, matched_field = _topic_terms_match(candidate, self.matching_terms, self.exception_terms)
            if term:
                return f"topic term “{term}” matched visible {matched_field} metadata (rule subject “{subject}”)"
            return ""
        if not self.subject_tokens:
            return ""
        if self.scope == "publisher":
            if self.subject_tokens <= candidate.publisher_tokens:
                return f"publisher metadata matched “{subject}”"
            return ""
        if self.scope == "source":
            if any(self.subject_tokens <= tokens for tokens in candidate.evidence_tokens("source_name")):
                return f"source metadata matched “{subject}”"
            return ""
        if self.scope == "source_family":
            if any(self.subject_tokens <= tokens for tokens in candidate.evidence_tokens("source_family")):
                return f"source-family metadata matched “{subject}”"
        return ""


def prepare_rules(rules: Sequence[Mapping[str, Any] | PreparedRule]) -> list[PreparedRule]:
    """Prepare each of *rules* for matching; an already prepared rule is kept as it is."""
    return [PreparedRule.of(rule) for rule in rules]


def _candidate_matches_rule(candidate: ScoutingCandidate, rule: Mapping[str, Any]) -> bool:
    """Check whether *candidate* matches a hard rule entry of any scope."""
    return _candidate_rule_match_explanation(candidate, rule) != ""


def _candidate_rule_match_explanation(candidate: ScoutingCandidate, rule: Mapping[str, Any]) -> str:
    return PreparedRule.of(rule).explain(candidate)


def _first_term_match(candidate: ScoutingCandidate, terms: list[str]) -> tuple[str, str]:
    """Return ``(term, field)`` for the first term that matches *candidate*, or empty strings."""
    return _first_prepared_term_match(_RuleSubject(candidate), [PreparedTopicTerm.of(term) for term in terms])


def _print_topic_candidate_impact_preview(
    store: StateStore,
    subject: str,
    matching_terms: list[str],
    exception_terms: list[str] | None = None,
) -> None:
    """Print a preview of which current candidates a topic rule would match.

    Used for both topic Hard Rules and topic Soft Preferences so the operator
    sees the same candidate impact (match count, titles, term and field
    attribution) before anything is written. When the entry has exception
    terms, the candidates an exception keeps outside the entry are listed as
    ``kept_by_exception`` rows, so the editor sees both sides of the rule.

    Reads the latest Scouting Candidates from *store*; when there are none,
    prints an unambiguous ``current_matches=0`` count (and no
    ``current_match=`` rows) instead of failing.
    """
    data = store.read(Collection.LATEST_SCOUTING_CANDIDATES)
    if not isinstance(data, list):
        data = []

    terms = [PreparedTopicTerm.of(term) for term in (matching_terms if matching_terms else [subject])]
    exceptions = [PreparedTopicTerm.of(term) for term in exception_terms or []]

    matched_candidates: list[dict[str, Any]] = []
    kept_candidates: list[dict[str, Any]] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        candidate = ScoutingCandidate(raw)
        rule_subject = _RuleSubject(candidate)
        term, field = _first_prepared_term_match(rule_subject, terms)
        if not term:
            continue
        # Use the stored identity_key when present; derive as fallback (finding 6).
        stored_ik = str(raw.get("identity_key", ""))
        row = {
            "title": str(candidate.data.get("title", "")),
            "author": ", ".join(candidate.data.get("primary_authors", [])),
            "identity_key": stored_ik if stored_ik else candidate.identity_key(),
            "match": f"term “{term}” in {field}",
        }
        exception_term, exception_field = _first_prepared_term_match(rule_subject, exceptions)
        if exception_term:
            row["exception"] = f"exception term “{exception_term}” in {exception_field}"
            kept_candidates.append(row)
        else:
            matched_candidates.append(row)

    print(f"current_matches={len(matched_candidates)}")
    for _idx, m in enumerate(matched_candidates, 1):
        print(f"current_match={_json_compact(m)}")
    if exceptions:
        print(f"kept_by_exception={len(kept_candidates)}")
        for row in kept_candidates:
            print(f"kept_by_exception_match={_json_compact(row)}")


def matching_metadata_field(candidate: ScoutingCandidate, subject: str) -> str:
    return PreparedTopicTerm.of(subject).matching_field(_RuleSubject(candidate).topic_fields)


def _topic_entry_match(candidate: ScoutingCandidate, entry: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``(term, field)`` when a topic entry applies to *candidate*.

    The entry applies when a matching term matches and no exception term
    does (#967); otherwise both values are empty.
    """
    matching_terms, exception_terms = _prepared_topic_terms(entry)
    return _topic_terms_match(_RuleSubject(candidate), matching_terms, exception_terms)


def candidate_matches_author_rule(candidate: ScoutingCandidate, rule: dict[str, Any]) -> bool:
    """Backward-compatible alias for _candidate_matches_rule (author scope)."""
    return _candidate_matches_rule(candidate, rule)


def _candidate_matches_soft_preference(candidate: ScoutingCandidate, pref: Mapping[str, Any]) -> bool:
    """Check whether *candidate* matches a soft preference entry of any scope.

    A Soft Preference matches a candidate exactly when a Hard Rule with the
    same scope, subject and terms would.
    """
    return bool(PreparedRule.of(pref).explain(candidate))


def apply_soft_preferences(
    candidates: list[ScoutingCandidate],
    profile: dict[str, Any],
) -> dict[str, dict[str, int]]:
    """Nudge priorities in place and return deterministic per-entry effects."""
    prefs = active_soft_preferences(profile)
    effects: dict[str, dict[str, int]] = {
        str(pref["id"]): {"match_count": 0, "priority_steps_caused": 0, "notes_count": 0} for pref in prefs
    }
    if not prefs:
        return effects
    label_order = {"A": 0, "B": 1, "C": 2, "D": 3}
    order_to_label = {0: "A", 1: "B", 2: "C", 3: "D"}
    prepared_prefs = [(pref, PreparedRule.of(pref)) for pref in prefs]
    for candidate in candidates:
        rule_subject = _RuleSubject(candidate)
        matched_prefs = [pref for pref, prepared in prepared_prefs if prepared.match_explanation(rule_subject)]
        if not matched_prefs:
            continue
        for pref in matched_prefs:
            entry_effect = effects[str(pref["id"])]
            entry_effect["match_count"] += 1
            entry_effect["notes_count"] += 1
        directions = {str(pref.get("direction", "positive")) for pref in matched_prefs}
        current_label = str(candidate.data.get("priority_label", "D"))
        current_idx = label_order.get(current_label, 3)
        notes: list[str] = []
        if len(directions) > 1:
            for pref in matched_prefs:
                notes.append(
                    f"Soft preference {pref['id']} ({pref.get('direction', 'positive')}) matched but did not change "
                    f"priority because matching preferences conflict: “{pref['verbatim_quote']}”"
                )
        else:
            direction = next(iter(directions))
            new_idx = max(0, current_idx - 1) if direction == "positive" else min(3, current_idx + 1)
            if candidate.has_clear_visible_risk_hard_floor():
                new_idx = 3
            elif new_idx == 0 and not candidate.has_no_visible_risk():
                # A requires no visible risk (ADR 0018), so a nudge stops at B.
                new_idx = current_idx
            new_label = order_to_label[new_idx]
            if new_label != current_label:
                candidate.data["priority_label"] = new_label
                for pref in matched_prefs:
                    effects[str(pref["id"])]["priority_steps_caused"] += 1
                note_template = (
                    "Soft preference {id} ({direction}) nudged priority from "
                    f"{current_label} to {new_label}: “{{quote}}”"
                )
            else:
                note_template = (
                    "Soft preference {id} ({direction}) matched but did not change "
                    f"priority (already {current_label}): “{{quote}}”"
                )
            for pref in matched_prefs:
                notes.append(
                    note_template.format(
                        id=pref["id"],
                        direction=direction,
                        quote=pref["verbatim_quote"],
                    )
                )
        existing = candidate.data.get("editorial_profile_notes", "")
        candidate.data["editorial_profile_notes"] = f"{existing}; {' '.join(notes)}" if existing else " ".join(notes)
    return effects


SOFT_PREFERENCE_RESULT_FIELDS = ("priority_label", "editorial_profile_notes")


def reapply_soft_preferences(
    candidates: list[ScoutingCandidate],
    profile: dict[str, Any],
) -> dict[str, dict[str, int]]:
    """Apply Soft Preferences to candidates that may already carry a nudge.

    Description enrichment runs after the pipeline's first application, so
    the final candidates are judged again once their descriptions exist
    (issue #747). Each candidate first returns to its priority before Soft
    Preferences and drops its profile notes, which only Soft Preferences
    write, so a nudge is never applied on top of an earlier nudge.
    """
    for candidate in candidates:
        candidate.data["priority_label"] = candidate.priority_label()
        candidate.data["editorial_profile_notes"] = ""
    return apply_soft_preferences(candidates, profile)


def editorial_taste_profile_effects(
    candidates: list[ScoutingCandidate],
    profile: dict[str, Any],
    hidden_by_rule: list[dict[str, Any]],
    soft_effects: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    """Report per-entry matching, hiding, ranking, and note effects."""
    hidden_counts: dict[str, int] = {}
    for record in hidden_by_rule:
        rule_id = str(record.get("hidden_by_rule_id", ""))
        hidden_counts[rule_id] = hidden_counts.get(rule_id, 0) + 1

    effects: list[dict[str, Any]] = []
    for entry in [item for item in profile.get("entries", []) if item.get("status") == "active"]:
        entry_id = str(entry["id"])
        kind = str(entry.get("kind", ""))
        scope = str(entry.get("scope", ""))
        can_match = bool(str(entry.get("subject", "")).strip())
        matching_terms: list[str] = []
        exception_terms: list[str] = []
        if scope == "topic":
            # Report only explicit terms; a legacy untermed topic entry matches
            # nothing, so its report must not imply a subject fallback (#556).
            matching_terms = sorted(_topic_matching_terms_for_entry(entry))
            exception_terms = sorted(_topic_exception_terms_for_entry(entry))
            can_match = bool(matching_terms)

        if kind == "hard_rule":
            prepared = PreparedRule.of(entry)
            match_count = sum(1 for candidate in candidates if prepared.explain(candidate))
            priority_steps = 0
            notes_count = 0
            hidden_count = hidden_counts.get(entry_id, 0)
        else:
            soft_effect = soft_effects.get(entry_id, {})
            match_count = int(soft_effect.get("match_count", 0))
            priority_steps = int(soft_effect.get("priority_steps_caused", 0))
            notes_count = int(soft_effect.get("notes_count", 0))
            hidden_count = 0

        status = "cannot_match" if not can_match else ("matched" if match_count else "matched_nothing")
        effect: dict[str, Any] = {
            "id": entry_id,
            "kind": kind,
            "scope": scope,
            "subject": str(entry.get("subject", "")),
            "matching_terms": matching_terms,
            "can_match": can_match,
            "match_count": match_count,
            "hidden_count": hidden_count,
            "priority_steps_caused": priority_steps,
            "notes_count": notes_count,
            "status": status,
        }
        if exception_terms:
            effect["exception_terms"] = exception_terms
        effects.append(effect)
    return effects


def rule_matchable_candidate(record: Mapping[str, Any]) -> ScoutingCandidate:
    """Return a Candidate History record in the shape the rule matchers read.

    An old record can hold its authors as one string and its evidence as one
    object or nothing at all; the matchers expect lists.
    """
    data = dict(record)
    authors = data.get("primary_authors")
    if isinstance(authors, str):
        data["primary_authors"] = [authors] if authors.strip() else []
    elif isinstance(authors, list):
        data["primary_authors"] = [str(author) for author in authors if str(author).strip()]
    else:
        data["primary_authors"] = []
    evidence = data.get("source_evidence")
    if isinstance(evidence, dict):
        data["source_evidence"] = [evidence]
    elif isinstance(evidence, list):
        data["source_evidence"] = [item for item in evidence if isinstance(item, dict)]
    else:
        data["source_evidence"] = []
    return ScoutingCandidate(data)


def hard_rule_hidden_record(
    candidate: ScoutingCandidate, rules: Sequence[Mapping[str, Any] | PreparedRule]
) -> dict[str, Any] | None:
    """Return the hidden record for the first of *rules* that matches *candidate*, if any.

    A caller that judges many candidates passes rules from ``prepare_rules``,
    so each rule is prepared once rather than once per candidate (#1165).
    """
    rule_subject = _RuleSubject(candidate)
    for prepared in (PreparedRule.of(rule) for rule in rules):
        match_explanation = prepared.match_explanation(rule_subject)
        if not match_explanation:
            continue
        rule = prepared.entry
        scope_label = str(rule.get("scope", "author"))
        return {
            "identity_key": candidate.identity_key(),
            "title": str(candidate.data.get("title", "")),
            "primary_authors": list(candidate.data.get("primary_authors", [])),
            "hidden_by_rule_id": str(rule["id"]),
            "reason": f"Hidden by active {scope_label} Hard Rule {rule['id']}: {match_explanation}; editor said “{rule['verbatim_quote']}”",
            "match_explanation": match_explanation,
            "verbatim_quote": str(rule["verbatim_quote"]),
        }
    return None


def hide_candidates_by_rules(
    candidates: list[ScoutingCandidate], profile: dict[str, Any]
) -> tuple[list[ScoutingCandidate], list[dict[str, Any]]]:
    rules = prepare_rules(active_hard_rules(profile))
    visible: list[ScoutingCandidate] = []
    hidden: list[dict[str, Any]] = []
    for candidate in candidates:
        hidden_record = hard_rule_hidden_record(candidate, rules)
        if hidden_record is None:
            visible.append(candidate)
        else:
            hidden.append(hidden_record)
    return visible, hidden
