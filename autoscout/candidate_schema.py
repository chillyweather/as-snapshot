from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from autoscout.identity import normalize_isbn
from autoscout.source_metadata import SOURCE_METADATA_FIELDS

# The Evidence Note a Source Mention receives when neither the mention nor its
# source says anything. It is a placeholder, not public metadata about the
# book, so the Visible Risk screen does not count it as evidence (#714).
PLACEHOLDER_EVIDENCE_NOTE = "Configured source mention."


@dataclass(frozen=True)
class CandidateIsbn:
    """One candidate ISBN value and its identity-use state as a single owner.

    Issue #531: a grounded learned ISBN must never separate from its
    ``isbn_discovery_only`` safety marker, so this type owns reading the
    pair from a record and writing it back atomically.  The persisted JSON
    shape stays flat: ``isbn`` plus the optional ``isbn_discovery_only``
    marker.  Normalization reuses ``autoscout.identity.normalize_isbn``;
    this type adds no second normalizer.
    """

    value: str = ""
    discovery_only: bool = False

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> CandidateIsbn:
        value = str(record.get("isbn", "") or "")
        return cls(
            value=value,
            # A marker without a nonblank ISBN value is not a valid
            # discovery-only state: the persisted pair is internally valid
            # by construction.
            discovery_only=bool(record.get("isbn_discovery_only", False)) and bool(normalize_isbn(value)),
        )

    @property
    def normalized(self) -> str:
        return normalize_isbn(self.value)

    def __bool__(self) -> bool:
        return bool(self.normalized)

    def apply_to(self, record: dict[str, Any]) -> None:
        """Write the value and its marker as one atomic pair.

        Provenance of an earlier value never describes this one (issue
        #979), so ``isbn_provenance`` is removed; a writer that knows where
        this value came from records it after applying.
        """
        record.pop("isbn_provenance", None)
        record["isbn"] = self.value
        if self.discovery_only:
            record["isbn_discovery_only"] = True
        else:
            record.pop("isbn_discovery_only", None)

    def clear_from(self, record: dict[str, Any]) -> None:
        """Remove the value, its marker and its provenance together."""
        record.pop("isbn", None)
        record.pop("isbn_discovery_only", None)
        record.pop("isbn_provenance", None)


@dataclass(frozen=True)
class CandidateMentionField:
    """Declared Source Mention ↔ Scouting Candidate field projection.

    The same schema drives both mention → candidate construction and
    candidate → mention projection. Direction flags make one-side-only fields
    explicit instead of relying on silent omission.
    """

    name: str
    mention_to_candidate: bool = True
    candidate_to_mention: bool = True
    merge_to_existing_candidate: bool = False
    candidate_default: Any = ""
    candidate_omit_when_empty: bool = False
    mention_default: Any = ""
    mention_value: Callable[[dict[str, Any], dict[str, Any], str], Any] | None = None
    mention_stringify: bool = True
    mention_omit_when_empty: bool = False


CANDIDATE_MENTION_FIELD_SCHEMA: tuple[CandidateMentionField, ...] = (
    CandidateMentionField("title", mention_value=lambda _source, candidate, _date: str(candidate.get("title", ""))),
    CandidateMentionField(
        "primary_authors",
        mention_value=lambda _source, candidate, _date: _candidate_authors(candidate),
        mention_stringify=False,
    ),
    CandidateMentionField(
        "candidate_category",
        merge_to_existing_candidate=True,
        mention_value=lambda _source, candidate, _date: str(
            candidate.get("candidate_category") or candidate.get("topic", "")
        ),
    ),
    CandidateMentionField("publisher", merge_to_existing_candidate=True),
    CandidateMentionField("publication_year", merge_to_existing_candidate=True),
    CandidateMentionField("publisher_fit", merge_to_existing_candidate=True),
    CandidateMentionField("russian_edition_status", merge_to_existing_candidate=True),
    CandidateMentionField("russian_edition_url", mention_to_candidate=False),
    CandidateMentionField("russian_edition_status_source", merge_to_existing_candidate=True),
    CandidateMentionField("russian_edition_status_note", merge_to_existing_candidate=True),
    CandidateMentionField("visible_risk_status", merge_to_existing_candidate=True),
    CandidateMentionField("visible_risk_category", merge_to_existing_candidate=True),
    CandidateMentionField("visible_risk_comment", merge_to_existing_candidate=True),
    CandidateMentionField("visible_risk_status_source", merge_to_existing_candidate=True),
    CandidateMentionField("visible_risk_status_note", merge_to_existing_candidate=True),
    CandidateMentionField("public_data_status", merge_to_existing_candidate=True),
    CandidateMentionField("short_description", merge_to_existing_candidate=True),
    CandidateMentionField("content_description", merge_to_existing_candidate=True),
    CandidateMentionField(
        "description_provenance",
        merge_to_existing_candidate=True,
        candidate_omit_when_empty=True,
        mention_omit_when_empty=True,
    ),
    CandidateMentionField(
        "autobiographical_subject_context",
        merge_to_existing_candidate=True,
        candidate_omit_when_empty=True,
        mention_stringify=False,
        mention_omit_when_empty=True,
    ),
    CandidateMentionField(
        "content_description_rejection_reason",
        mention_to_candidate=False,
        mention_omit_when_empty=True,
    ),
    CandidateMentionField("english_title", merge_to_existing_candidate=True),
    CandidateMentionField(
        "english_title_type",
        merge_to_existing_candidate=True,
        candidate_omit_when_empty=True,
        mention_omit_when_empty=True,
    ),
    CandidateMentionField(
        "english_title_provenance",
        merge_to_existing_candidate=True,
        candidate_omit_when_empty=True,
        mention_stringify=False,
        mention_omit_when_empty=True,
    ),
    CandidateMentionField("english_author_display", merge_to_existing_candidate=True),
    CandidateMentionField("english_annotation", merge_to_existing_candidate=True),
    CandidateMentionField("risk_evidence_url", merge_to_existing_candidate=True),
    CandidateMentionField("risk_signal_scope", merge_to_existing_candidate=True),
    CandidateMentionField("publication_status", merge_to_existing_candidate=True),
    CandidateMentionField("isbn"),
    # The identity-use state of the ISBN is one atomic pair with the value
    # above: a learned ISBN must never project without its marker.
    CandidateMentionField(
        "isbn_discovery_only",
        candidate_omit_when_empty=True,
        mention_stringify=False,
        mention_omit_when_empty=True,
    ),
    # Ranking metadata belongs to each independent source signal, not to the
    # deduplicated book. It is retained in source_evidence below.
    CandidateMentionField("current_rank", mention_to_candidate=False, mention_omit_when_empty=True),
    CandidateMentionField("narrative_classification", mention_to_candidate=False, mention_omit_when_empty=True),
    CandidateMentionField(
        "retailer_discovery", mention_to_candidate=False, mention_omit_when_empty=True, mention_stringify=False
    ),
    CandidateMentionField("source_week", mention_to_candidate=False, mention_omit_when_empty=True),
    # Issue #945: a rights deal's structured facts belong to its signal, like a rank.
    CandidateMentionField(
        "rights_deal", mention_to_candidate=False, mention_omit_when_empty=True, mention_stringify=False
    ),
    CandidateMentionField("search_translation", mention_to_candidate=False),
    CandidateMentionField("author_transliteration_note", mention_to_candidate=False),
    CandidateMentionField(
        "source_url",
        mention_to_candidate=False,
        mention_value=lambda source, candidate, _date: str(candidate.get("source_url") or source.get("url", "")),
    ),
    CandidateMentionField("evidence_url", mention_to_candidate=False),
    CandidateMentionField("evidence_locator", mention_to_candidate=False),
    CandidateMentionField(
        "source_name",
        mention_to_candidate=False,
        mention_value=lambda source, _candidate, _date: str(source.get("name", "")),
    ),
    CandidateMentionField(
        "source_family",
        mention_to_candidate=False,
        mention_value=lambda source, _candidate, _date: str(source.get("family", "")),
    ),
    CandidateMentionField(
        "source_check_date",
        mention_to_candidate=False,
        mention_value=lambda _source, _candidate, source_check_date: source_check_date,
    ),
    CandidateMentionField(
        "signal_type",
        mention_to_candidate=False,
        mention_value=lambda source, candidate, _date: str(candidate.get("signal_type") or source.get("family", "")),
    ),
    CandidateMentionField(
        "signal_strength",
        mention_to_candidate=False,
        mention_value=lambda _source, candidate, _date: str(candidate.get("signal_strength") or "minor mention"),
    ),
    CandidateMentionField(
        "evidence_note",
        mention_to_candidate=False,
        mention_value=lambda source, candidate, _date: str(
            candidate.get("evidence_note")
            or candidate.get("why_it_entered")
            or source.get("notes")
            or PLACEHOLDER_EVIDENCE_NOTE
        ),
    ),
    CandidateMentionField(
        "referenced_source_urls",
        mention_to_candidate=False,
        mention_default=[],
        mention_stringify=False,
        mention_omit_when_empty=True,
    ),
    CandidateMentionField(
        "underlying_work_type",
        mention_to_candidate=False,
        mention_omit_when_empty=True,
    ),
)

CANDIDATE_ONLY_FIELDS = {
    "identity_key",
    "russian_search_notes",
    "source_evidence",
    "independent_signal_count",
    "independent_signal_families",
    "primary_signal",
    "priority_label",
    "signal_summary",
    "material_change_reasons",
    "reentry_reason",
    "editor_status",
    "editor_note",
    "russian_edition_evidence",
    "russian_edition_note",
    "isbn_disagreement",
    # Computed by the Visible Risk screen, never carried by a Source Mention.
    "visible_risk_screen_coverage",
}

SOURCE_MENTION_ONLY_FIELDS = {field.name for field in CANDIDATE_MENTION_FIELD_SCHEMA if not field.mention_to_candidate}


SOURCE_EVIDENCE_FIELDS: tuple[str, ...] = (
    "source_name",
    "source_family",
    "source_url",
    "evidence_url",
    "evidence_locator",
    "source_check_date",
    "signal_type",
    "signal_strength",
    "evidence_note",
    "current_rank",
    "narrative_classification",
    "retailer_discovery",
    "source_week",
    "content_description_rejection_reason",
    "autobiographical_subject_context",
    # Issue #945: the structured facts of a rights deal (publisher, agents, agency, territory, category).
    "rights_deal",
    *SOURCE_METADATA_FIELDS,
)

RUSSIAN_SEARCH_NOTE_FIELDS: tuple[str, ...] = (
    "search_translation",
    "author_transliteration_note",
)


def _candidate_authors(candidate: dict[str, Any]) -> Any:
    authors = candidate.get("primary_authors")
    if authors is None:
        author = str(candidate.get("author", "")).strip()
        return [author] if author else []
    return authors


def candidate_fields_from_mention(mention: dict[str, Any]) -> dict[str, Any]:
    return {
        field.name: mention.get(field.name, field.candidate_default)
        for field in CANDIDATE_MENTION_FIELD_SCHEMA
        if field.mention_to_candidate
        and not (field.candidate_omit_when_empty and not mention.get(field.name, field.candidate_default))
    }


def mergeable_candidate_fields_from_mention(mention: dict[str, Any]) -> dict[str, Any]:
    return {
        field.name: mention.get(field.name, field.candidate_default)
        for field in CANDIDATE_MENTION_FIELD_SCHEMA
        if field.mention_to_candidate and field.merge_to_existing_candidate
    }


def source_mention_fields_from_candidate(
    source: dict[str, Any], candidate: dict[str, Any], source_check_date: str
) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for field in CANDIDATE_MENTION_FIELD_SCHEMA:
        if not field.candidate_to_mention:
            continue
        value = (
            field.mention_value(source, candidate, source_check_date)
            if field.mention_value is not None
            else candidate.get(field.name, field.mention_default)
        )
        if field.mention_omit_when_empty and not value:
            continue
        data[field.name] = str(value) if field.mention_stringify else value
    return data


def source_evidence_from_mention(mention: dict[str, Any]) -> dict[str, Any]:
    evidence = {field: mention.get(field, "") for field in SOURCE_EVIDENCE_FIELDS}
    for field in (
        "content_description_rejection_reason",
        "autobiographical_subject_context",
        "current_rank",
        "narrative_classification",
        "retailer_discovery",
        "source_week",
        "rights_deal",
    ):
        if not evidence[field]:
            evidence.pop(field)
    for field in SOURCE_METADATA_FIELDS:
        if not evidence[field]:
            evidence.pop(field)
    return evidence


def russian_search_notes_from_mention(mention: dict[str, Any]) -> dict[str, str]:
    notes = {field: str(mention.get(field, "")) for field in RUSSIAN_SEARCH_NOTE_FIELDS}
    return {key: value for key, value in notes.items() if value}
