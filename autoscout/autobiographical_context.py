"""Evidence-backed subject context for autobiographical books.

This module owns the complete contract for deciding whether the rule applies
and whether a Russian opening satisfies it. Callers do not infer public roles
from URLs, provenance records, or free-form descriptions.
"""

from __future__ import annotations

import html
import re
from typing import Any
from typing import TypedDict
from urllib.parse import urlparse

AUTOBIOGRAPHICAL_NARRATIVE_CLASSIFICATIONS = (
    "memoir",
    "autobiography",
    "first-person personal-life narrative",
)
NARRATIVE_CLASSIFICATIONS = (
    *AUTOBIOGRAPHICAL_NARRATIVE_CLASSIFICATIONS,
    "third-person biography",
    "narrative history",
    "reported nonfiction",
    "other nonfiction",
)

AUTOBIOGRAPHICAL_SUBJECT_CONTEXT_REQUIREMENT = (
    "For a memoir, autobiography, or explicitly classified first-person personal book, the opening sentence must "
    "identify the subject's relevant public role or notability. Return autobiographical_subject_context as an object "
    "with opening_phrase, source_phrase, and evidence_url. opening_phrase must be the exact inflected Russian "
    "phrase used in the opening sentence; source_phrase must be an exact phrase present in the provided source; and "
    "evidence_url must be the matching supplied evidence URL. If the source does not support that context, omit "
    "autobiographical_subject_context, keep any otherwise valid original Russian content_description, and let the "
    "review flag the missing context."
)

AUTOBIOGRAPHICAL_SUBJECT_CONTEXT_WARNING = (
    "В начале описания не указано, чем известен герой книги. "
    "Проверьте публичную роль или значимость по источникам; не добавляйте её без подтверждения."
)

_AUTOBIOGRAPHICAL_TOKEN = re.compile(
    r"(?<!\w)(?:memoirs?|autobiograph(?:y|ies|ical)?|мемуар\w*|автобиограф\w*)(?!\w)",
    re.IGNORECASE,
)
_SENTENCE_BOUNDARY = re.compile(r"(?<!\b[^\W\d_]\.)(?<!\b[^\W\d_]{2}\.)(?<=\.)\s+|(?<=[!?])\s+")
_HTML_TAG = re.compile(r"(?s)<[^>]+>")
_EVIDENCE_CONTEXT_WINDOW_CHARS = 4000
_SUBJECT_CONTEXT_FIELD_MAX_CHARS = 500


class AutobiographicalSubjectContext(TypedDict):
    """Persistable Russian opening phrase tied to an exact source phrase."""

    opening_phrase: str
    source_phrase: str
    evidence_url: str


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def parse_autobiographical_subject_context(value: Any) -> AutobiographicalSubjectContext | None:
    """Parse the bounded structured context value, rejecting legacy strings."""
    if not isinstance(value, dict):
        return None
    opening_phrase = str(value.get("opening_phrase", "")).strip()
    source_phrase = str(value.get("source_phrase", "")).strip()
    evidence_url = str(value.get("evidence_url", "")).strip()
    if not opening_phrase or not source_phrase or not _valid_http_url(evidence_url):
        return None
    if any(len(part) > _SUBJECT_CONTEXT_FIELD_MAX_CHARS for part in (opening_phrase, source_phrase, evidence_url)):
        return None
    return {
        "opening_phrase": opening_phrase,
        "source_phrase": source_phrase,
        "evidence_url": evidence_url,
    }


def ground_autobiographical_subject_context(
    value: Any,
    *,
    source_html: str,
    evidence_url: str,
    source_page: str = "",
) -> AutobiographicalSubjectContext | None:
    """Return a context only when its source phrase is near its captured URL."""
    context = parse_autobiographical_subject_context(value)
    if context is None or context["evidence_url"] != evidence_url:
        return None
    evidence_index = source_html.find(evidence_url)
    if evidence_index >= 0:
        bounded_html = source_html[
            max(0, evidence_index - _EVIDENCE_CONTEXT_WINDOW_CHARS) : evidence_index
            + len(evidence_url)
            + _EVIDENCE_CONTEXT_WINDOW_CHARS
        ]
    elif evidence_url == source_page:
        bounded_html = source_html
    else:
        return None
    source_text = html.unescape(_HTML_TAG.sub(" ", bounded_html))
    if _normalized_text(context["source_phrase"]) not in _normalized_text(source_text):
        return None
    return context


def is_autobiographical_candidate(record: dict[str, Any]) -> bool:
    """Return whether explicit bounded metadata classifies the work as autobiographical."""
    textual_values = [
        record.get("title", ""),
        record.get("candidate_category", ""),
    ]
    classifications: set[str] = set()
    source_evidence = record.get("source_evidence", [])
    if isinstance(source_evidence, list):
        for evidence in source_evidence:
            if not isinstance(evidence, dict):
                continue
            textual_values.append(evidence.get("signal_type", ""))
            classification = str(evidence.get("narrative_classification", "")).strip().casefold()
            if classification:
                classifications.add(classification)
    metadata = " ".join(str(value) for value in textual_values if value)
    return bool(
        not classifications.isdisjoint(AUTOBIOGRAPHICAL_NARRATIVE_CLASSIFICATIONS)
        or _AUTOBIOGRAPHICAL_TOKEN.search(metadata)
    )


def _first_sentence(value: str) -> str:
    """Return the opening sentence without splitting one- or two-letter initials."""
    return _SENTENCE_BOUNDARY.split(value.strip(), maxsplit=1)[0]


def _grounded_evidence_contexts(
    record: dict[str, Any],
) -> list[AutobiographicalSubjectContext]:
    contexts: list[AutobiographicalSubjectContext] = []
    source_evidence = record.get("source_evidence", [])
    if not isinstance(source_evidence, list):
        return contexts
    for evidence in source_evidence:
        if not isinstance(evidence, dict):
            continue
        evidence_context = parse_autobiographical_subject_context(evidence.get("autobiographical_subject_context"))
        evidence_url = str(evidence.get("evidence_url", "") or evidence.get("source_url", "")).strip()
        if evidence_context is not None and evidence_url == evidence_context["evidence_url"]:
            contexts.append(evidence_context)
    return contexts


def has_grounded_autobiographical_subject_context(record: dict[str, Any]) -> bool:
    """Return whether Source Evidence contains any valid grounded context."""
    return bool(_grounded_evidence_contexts(record))


def verified_context_from_artifact(
    record: dict[str, Any],
    artifact_value: Any,
) -> AutobiographicalSubjectContext | None:
    """Accept an artifact context only when it already exists in Source Evidence."""
    context = parse_autobiographical_subject_context(artifact_value)
    if context is None or context not in _grounded_evidence_contexts(record):
        return None
    return context


def autobiographical_subject_context_warning(
    record: dict[str, Any],
    *,
    content_description: str | None = None,
    subject_context: AutobiographicalSubjectContext | None = None,
) -> str:
    """Return the warning after applying optional proposed description values."""
    if not is_autobiographical_candidate(record):
        return ""
    context = parse_autobiographical_subject_context(subject_context or record.get("autobiographical_subject_context"))
    description = (
        content_description.strip()
        if content_description is not None
        else str(record.get("content_description", "")).strip()
    )
    supported = context is not None and context in _grounded_evidence_contexts(record)
    opening_contains_phrase = bool(
        context
        and description
        and _normalized_text(context["opening_phrase"]) in _normalized_text(_first_sentence(description))
    )
    if supported and opening_contains_phrase:
        return ""
    return AUTOBIOGRAPHICAL_SUBJECT_CONTEXT_WARNING
