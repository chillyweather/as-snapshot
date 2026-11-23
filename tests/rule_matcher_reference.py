"""The Hard Rule and Soft Preference matcher as it was before #1165, kept as a test oracle.

#1165 prepares each rule's terms and each candidate's fields once instead of
on every comparison. The matching rules (#551, #966, #967) did not change, so
the prepared matcher must give exactly the results of the code below. The
functions are copied unchanged from ``autoscout/editorial_taste_profile.py``
at commit 41522335; the helpers #1165 did not touch are imported.

The two normalisers are pure functions of one string. They are memoised here
only so the oracle runs fast enough for the suite; that cannot change a
result.
"""

from __future__ import annotations

import re
from functools import cache
from typing import Any

from autoscout.editorial_taste_profile import PHRASE_QUOTE
from autoscout.editorial_taste_profile import PREFIX_MARKER
from autoscout.editorial_taste_profile import _is_phrase_term
from autoscout.editorial_taste_profile import _topic_exception_terms_for_entry
from autoscout.editorial_taste_profile import _topic_matching_terms_for_entry
from autoscout.editorial_taste_profile import author_match_keys
from autoscout.editorial_taste_profile import normalize_topic_term as _normalize_topic_term
from autoscout.editorial_taste_profile import topic_metadata_fields
from autoscout.identity import normalize_identity_text as _normalize_identity_text
from autoscout.scouting_candidates import ScoutingCandidate

normalize_topic_term = cache(_normalize_topic_term)
normalize_identity_text = cache(_normalize_identity_text)


def _candidate_rule_match_explanation(candidate: ScoutingCandidate, rule: dict[str, Any]) -> str:
    scope = str(rule.get("scope", ""))
    subject = str(rule.get("subject", ""))
    if scope == "author":
        subject_keys = author_match_keys(subject)
        for author in candidate.data.get("primary_authors", []):
            if subject_keys & author_match_keys(str(author)):
                return f"author metadata matched “{subject}”"
        return ""
    if scope == "topic":
        term, matched_field = _topic_entry_match(candidate, rule)
        if term:
            return f"topic term “{term}” matched visible {matched_field} metadata (rule subject “{subject}”)"
        return ""
    if scope == "publisher":
        candidate_publisher = str(candidate.data.get("publisher", "")).strip().lower()
        subject_lower = subject.strip().lower()
        if candidate_publisher and _token_match(subject_lower, candidate_publisher):
            return f"publisher metadata matched “{subject}”"
    if scope == "source":
        if any(
            _token_match(subject.strip().lower(), str(evidence.get("source_name", "")).lower())
            for evidence in candidate.data.get("source_evidence", [])
        ):
            return f"source metadata matched “{subject}”"
    if scope == "source_family":
        if any(
            _token_match(subject.strip().lower(), str(evidence.get("source_family", "")).lower())
            for evidence in candidate.data.get("source_evidence", [])
        ):
            return f"source-family metadata matched “{subject}”"
    return ""


def _first_term_match(candidate: ScoutingCandidate, terms: list[str]) -> tuple[str, str]:
    """Return ``(term, field)`` for the first term that matches *candidate*, or empty strings."""
    if not terms:
        return "", ""
    # Normalised once per candidate: a rule can carry dozens of terms, and the
    # review judges every Candidate History record against every rule (#1025).
    normalized_fields = _normalized_topic_fields(candidate)
    for term in terms:
        field = _matching_normalized_field(normalized_fields, term)
        if field:
            return term, field
    return "", ""


def _token_match(subject: str, field_value: str) -> bool:
    """Check whether all tokens of *subject* appear in *field_value*.

    Tokens are split on common delimiters (space, hyphen, slash, comma)
    so that e.g. "fiction" does not match "non-fiction", but
    "Bad Press" matches "Bad Press Inc.".
    """
    subject_tokens = {token for token in re.split(r"[\s\-/,]+", subject) if token}
    field_tokens = {token for token in re.split(r"[\s\-/,]+", field_value) if token}
    if not subject_tokens:
        return False
    return subject_tokens <= field_tokens


def _term_word_matches(term_word: str, value_word: str) -> bool:
    if term_word.endswith(PREFIX_MARKER):
        return value_word.startswith(term_word[: -len(PREFIX_MARKER)])
    return term_word == value_word


def _topic_term_match(normalized_term: str, normalized_value: str) -> bool:
    """Check whether a normalised topic term matches *normalized_value*.

    A plain word must equal a metadata word, so "fiction" does not match
    "fictional". A word ending in the prefix marker matches any metadata word
    that starts with its stem (#966). The words of an unquoted term may appear
    anywhere in the value; the words of a quoted phrase must be adjacent and in
    order (#967).
    """
    phrase = _is_phrase_term(normalized_term)
    words = normalized_term.strip(PHRASE_QUOTE).split()
    if not words:
        return False
    value_words = normalized_value.split()
    if phrase:
        width = len(words)
        return any(
            all(
                _term_word_matches(word, value_word)
                for word, value_word in zip(words, value_words[start : start + width], strict=True)
            )
            for start in range(len(value_words) - width + 1)
        )
    return all(any(_term_word_matches(word, value_word) for value_word in value_words) for word in words)


def _normalized_topic_fields(candidate: ScoutingCandidate) -> dict[str, str]:
    return {label: normalize_identity_text(value) for label, value in topic_metadata_fields(candidate).items()}


def _matching_normalized_field(normalized_fields: dict[str, str], subject: str) -> str:
    subject_normalized = normalize_topic_term(subject)
    russian_term = bool(re.search(r"[а-яё]", subject_normalized, flags=re.IGNORECASE))
    for label, value in normalized_fields.items():
        if russian_term and label in {"English title", "English annotation"}:
            continue
        if _topic_term_match(subject_normalized, value):
            return label
    return ""


def matching_metadata_field(candidate: ScoutingCandidate, subject: str) -> str:
    return _matching_normalized_field(_normalized_topic_fields(candidate), subject)


def _topic_entry_match(candidate: ScoutingCandidate, entry: dict[str, Any]) -> tuple[str, str]:
    """Return ``(term, field)`` when a topic entry applies to *candidate*.

    The entry applies when a matching term matches and no exception term
    does (#967); otherwise both values are empty.
    """
    term, field = _first_term_match(candidate, _topic_matching_terms_for_entry(entry))
    if not term or _first_term_match(candidate, _topic_exception_terms_for_entry(entry))[0]:
        return "", ""
    return term, field


def _candidate_matches_soft_preference(candidate: ScoutingCandidate, pref: dict[str, Any]) -> bool:
    """Check whether *candidate* matches a soft preference entry of any scope."""
    scope = str(pref.get("scope", ""))
    subject = str(pref.get("subject", ""))
    if scope == "author":
        subject_keys = author_match_keys(subject)
        return any(
            bool(subject_keys & author_match_keys(str(author))) for author in candidate.data.get("primary_authors", [])
        )
    if scope == "topic":
        return bool(_topic_entry_match(candidate, pref)[0])
    if scope == "publisher":
        candidate_publisher = str(candidate.data.get("publisher", "")).strip().lower()
        subject_lower = subject.strip().lower()
        return bool(candidate_publisher and _token_match(subject_lower, candidate_publisher))
    if scope == "source":
        return any(
            _token_match(subject.strip().lower(), str(evidence.get("source_name", "")).lower())
            for evidence in candidate.data.get("source_evidence", [])
        )
    if scope == "source_family":
        return any(
            _token_match(subject.strip().lower(), str(evidence.get("source_family", "")).lower())
            for evidence in candidate.data.get("source_evidence", [])
        )
    return False


def hard_rule_hidden_record(candidate: ScoutingCandidate, rules: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the hidden record for the first of *rules* that matches *candidate*, if any."""
    for rule in rules:
        match_explanation = _candidate_rule_match_explanation(candidate, rule)
        if not match_explanation:
            continue
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
