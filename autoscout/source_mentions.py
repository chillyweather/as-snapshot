from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any

from autoscout.author_admission import admitted_author_people
from autoscout.candidate_schema import source_mention_fields_from_candidate
from autoscout.content_description import validate_content_description
from autoscout.ranking_author_policy import mention_author_exempt
from autoscout.russian_status import RUSSIAN_STATUSES
from autoscout.source_metadata import declared_source_metadata
from autoscout.title_evidence import normalize_title_fields

FORBIDDEN_SOURCE_TEXT_FIELDS = {
    "article_body",
    "page_body",
    "full_body",
    "full_description",
    "description",
    "full_review",
    "review",
    "publisher_blurb",
    "jacket_copy",
    "copied_excerpt",
    "long_excerpt",
    "book_sample",
    "sample_text",
}

SOURCE_MENTION_REQUIRED_FIELDS = {
    "title",
    "primary_authors",
    "source_url",
    "source_check_date",
    "signal_type",
    "signal_strength",
    "evidence_note",
}

SOURCE_MENTION_STATUS_VALUES = {
    "russian_edition_status": RUSSIAN_STATUSES,
    "visible_risk_status": {
        "no visible risk",
        "text check required",
        "clear visible risk",
    },
}


def looks_like_russian_prose(value: str) -> bool:
    return any("А" <= char <= "я" or char in {"ё", "Ё"} for char in value)


@dataclass(frozen=True)
class SourceMention:
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_candidate(
        cls,
        source: dict[str, Any],
        candidate: dict[str, Any],
        source_check_date: str,
        *,
        classify_titles: bool = False,
        allow_sourced: bool = False,
    ) -> SourceMention:
        data = source_mention_fields_from_candidate(source, candidate, source_check_date)
        for field, value in declared_source_metadata(source).items():
            data.setdefault(field, value)
        normalize_title_fields(data, classify=classify_titles, allow_sourced=allow_sourced)
        return cls.from_mapping(source, data, context="source mention")

    @classmethod
    def from_collector(cls, source: dict[str, Any], mention: dict[str, Any], run_date: str) -> SourceMention:
        data = dict(mention)
        data.setdefault("source_check_date", run_date)
        data.setdefault("source_name", source.get("name", ""))
        data.setdefault("source_family", source.get("family", ""))
        data.setdefault("signal_type", data.get("source_family", source.get("family", "")))
        for field, value in declared_source_metadata(source).items():
            data.setdefault(field, value)
        normalize_title_fields(data, classify=True)
        return cls.from_mapping(source, data, context="collector mention")

    @classmethod
    def from_recorded(cls, source: dict[str, Any], mention: dict[str, Any]) -> SourceMention:
        data = dict(mention)
        data.setdefault("source_name", source.get("name", ""))
        data.setdefault("source_family", source.get("family", ""))
        data.setdefault("russian_edition_status_source", source.get("russian_edition_status_source", ""))
        data.setdefault("russian_edition_status_note", source.get("russian_edition_status_note", ""))
        data.setdefault("visible_risk_status_source", source.get("visible_risk_status_source", ""))
        data.setdefault("visible_risk_status_note", source.get("visible_risk_status_note", ""))
        data.setdefault("signal_type", data.get("source_family", source.get("family", "")))
        for field, value in declared_source_metadata(source).items():
            data.setdefault(field, value)
        normalize_title_fields(data, classify=True)
        return cls.from_mapping(source, data, context="recorded source mention", check_status_values=True)

    @classmethod
    def from_mapping(
        cls,
        source: dict[str, Any],
        data: dict[str, Any],
        *,
        context: str,
        check_status_values: bool = False,
    ) -> SourceMention:
        data = with_split_author_lists(data)
        validate_source_mention(source, data, context=context, check_status_values=check_status_values)
        return cls(dict(data))

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


def with_split_author_lists(mention: dict[str, Any]) -> dict[str, Any]:
    """Admit each author value as the people it names (issues #756, #854).

    ``Hevrøy, Hans Ola / Ngan, John`` becomes ``Hevrøy, Hans Ola`` and
    ``Ngan, John``; ``Joseph Grenny, Kerry Patterson, Ron McMillan, and
    others`` becomes three people; ``Robin Blackstone, MD`` becomes ``Robin
    Blackstone``. The identity key, the description search, author evidence,
    and the review then each see the people. Each name keeps its printed form:
    raw author values are not reordered (see ``normalize_author_display``).
    Already-stored Candidate History records are not touched here.
    """
    authors = mention.get("primary_authors")
    if not isinstance(authors, list):
        return mention
    admitted = [person for author in authors for person in admitted_author_people(author)]
    if admitted == authors:
        return mention
    return {**mention, "primary_authors": admitted}


def validate_source_mention(
    source: dict[str, Any],
    mention: dict[str, Any],
    *,
    context: str,
    check_status_values: bool = False,
) -> None:
    forbidden = FORBIDDEN_SOURCE_TEXT_FIELDS & set(mention)
    if forbidden:
        raise ValueError(
            f"{source.get('name', context)}: {context} "
            f"{mention.get('title', '<untitled>')} stores forbidden copied text field(s): "
            f"{', '.join(sorted(forbidden))}"
        )
    # A ranking-table source may declare that a row legitimately carries no
    # author.  The exemption is that declaration and nothing wider: every other
    # source keeps primary_authors required.
    required_fields = set(SOURCE_MENTION_REQUIRED_FIELDS)
    if mention_author_exempt(source):
        required_fields.discard("primary_authors")
    missing = sorted(field for field in required_fields if not source_mention_field_present(mention, field))
    if missing:
        raise ValueError(
            f"{source.get('name', context)}: {context} "
            f"{mention.get('title', '<untitled>')} missing required field(s): {', '.join(missing)}"
        )
    if "content_description" in mention:
        raw_description = mention.get("content_description")
        if raw_description == "":
            # Preserve a bounded reason from an earlier validation pass. A
            # genuinely missing description has no reason field to preserve.
            pass
        elif source.get("metadata_only") is True:
            # A title/rank capture cannot ground any description, even one
            # whose Russian prose passes validation. Keep the book available
            # for the grounded stage, with the same bounded rejection path.
            mention["content_description"] = ""
            mention["content_description_rejection_reason"] = "metadata-only source has no book prose"
        else:
            validated, reason = validate_content_description(raw_description)
            if validated is None:
                # Clear only the description so the candidate remains visible,
                # and retain the bounded reason with its Source Evidence.
                mention["content_description"] = ""
                mention["content_description_rejection_reason"] = reason or "unknown"
            else:
                mention["content_description"] = validated
                mention.pop("content_description_rejection_reason", None)
    if not check_status_values:
        return
    for field, allowed_values in SOURCE_MENTION_STATUS_VALUES.items():
        value = mention.get(field, "")
        if value and value not in allowed_values:
            allowed = ", ".join(sorted(allowed_values))
            raise ValueError(
                f"{source.get('name', context)}: {context} "
                f"{mention.get('title', '<untitled>')} has invalid {field}: {value}. "
                f"Allowed values: {allowed}"
            )


def source_mention_field_present(mention: dict[str, Any], field: str) -> bool:
    value = mention.get(field)
    if field == "primary_authors":
        return isinstance(value, list) and any(str(author).strip() for author in value)
    return bool(str(value or "").strip())
