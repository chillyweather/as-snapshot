from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from autoscout.candidate_schema import CANDIDATE_ONLY_FIELDS
from autoscout.candidate_schema import PLACEHOLDER_EVIDENCE_NOTE
from autoscout.candidate_schema import SOURCE_MENTION_ONLY_FIELDS
from autoscout.candidate_schema import candidate_fields_from_mention
from autoscout.candidate_schema import mergeable_candidate_fields_from_mention
from autoscout.candidate_schema import russian_search_notes_from_mention
from autoscout.candidate_schema import source_evidence_from_mention
from autoscout.candidate_schema import source_mention_fields_from_candidate
from autoscout.candidate_verification import is_non_definitive_russian_normalization
from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.content_description import DESCRIPTION_UNCONFIRMED_STATUS
from autoscout.content_description import is_fiction_genre_category
from autoscout.content_description import is_unconfirmed_description
from autoscout.identity import normalize_author_display
from autoscout.identity import normalize_identity_text
from autoscout.identity import normalize_work_title
from autoscout.russian_editions import is_valid_russian_edition_verification
from autoscout.russian_status import FOUND_IN_RUSSIAN
from autoscout.russian_status import is_found_in_russian
from autoscout.russian_status import is_not_found_in_russian
from autoscout.russian_status import is_unclear
from autoscout.source_eligibility import source_mention_eligibility_exclusion_reason
from autoscout.title_evidence import clean_title
from autoscout.title_evidence import format_editor_english_author_display
from autoscout.title_evidence import format_editor_english_title
from autoscout.title_evidence import merge_title_metadata
from autoscout.title_evidence import normalize_title_fields
from autoscout.visible_risk_coverage import PARTIAL
from autoscout.visible_risk_coverage import SCREENED
from autoscout.visible_risk_coverage import VISIBLE_RISK_SCREEN_COVERAGE_FIELD
from autoscout.visible_risk_coverage import coverage_for_evidence
from autoscout.visible_risk_coverage import failed_coverage
from autoscout.visible_risk_coverage import not_run_coverage
from autoscout.visible_risk_coverage import recorded_coverage

INELIGIBLE_CANDIDATE_CATEGORIES = {
    "fiction",
    "children",
    "children's",
    "childrens",
    "ya",
    "young adult",
    "topic-only",
    "topic only",
    "narrow manual",
    "manual",
    "narrow academic",
    "audiobook-only non-book material",
    "audio-only non-book material",
}

SIGNAL_STRENGTH_RANKS = {
    "winner": 100,
    "top ranking": 90,
    "sustained bestseller": 85,
    "reader choice winner": 85,
    "reader favorite": 80,
    "major media list": 80,
    "shortlist": 70,
    "rights/deal evidence": 60,
    "rights/deal mention": 60,
    "deal mention": 60,
    "minor mention": 30,
}

RISK_KEYWORDS = {
    "Russia/politics": ("russia", "russian politics", "putin", "kremlin"),
    "war/anti-war/Russian Armed Forces": ("war", "anti-war", "russian armed forces"),
    "WWII/USSR/Nazi comparison": ("wwii", "world war ii", "ussr", "nazi"),
    "non-traditional relationships/gender transition/rejection of childbearing": (
        "lgbt",
        "transgender",
        "gender transition",
        "childfree",
        "rejection of childbearing",
    ),
    "drugs/microdosing/psychoactive substances": (
        "drug",
        "microdosing",
        "psychoactive",
        "psychedelic",
    ),
}

# Word forms that substring matching reached and that a keyword still means
# (issue #714). Word-boundary matching would otherwise drop them. Every
# keyword also matches its plural with a trailing "s".
RISK_KEYWORD_FORMS = {
    "russia": ("russian",),
    "war": ("warfare", "wartime"),
    "nazi": ("nazism",),
    "lgbt": ("lgbtq", "lgbtqia"),
    "transgender": ("transgenderism",),
    "drug": ("drugged",),
}


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    forms = (keyword, *RISK_KEYWORD_FORMS.get(keyword, ()))
    alternatives = "|".join(re.escape(form) for form in forms)
    return re.compile(rf"(?<!\w)(?:{alternatives})s?(?!\w)")


# One pattern per category, matching any keyword as a whole word, so "war"
# no longer matches inside "award" or "drug" inside "drugstore".
RISK_CATEGORY_PATTERNS = {
    category: tuple(_keyword_pattern(keyword) for keyword in keywords) for category, keywords in RISK_KEYWORDS.items()
}

VISIBLE_RISK_KEYWORD_HINT_COMMENT = (
    "Public metadata mentions a configured risk category; check the text before treating it as clear book-content risk."
)

# What the Visible Risk screen writes on a candidate, including the priority
# label that a status can move.
VISIBLE_RISK_RESULT_FIELDS = (
    "visible_risk_status",
    "visible_risk_category",
    "visible_risk_comment",
    VISIBLE_RISK_SCREEN_COVERAGE_FIELD,
    "priority_label",
)

NO_VISIBLE_RISK_COMMENT = (
    "No configured MVP risk signal found in available public metadata; "
    "this is not legal clearance or a full-text review."
)

PRIORITY_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3}


@dataclass
class ScoutingCandidate:
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, candidate: Any) -> ScoutingCandidate:
        if isinstance(candidate, ScoutingCandidate):
            return candidate
        if isinstance(candidate, dict):
            # Defensive cleanup for legacy/current in-memory data. Do not add
            # a missing type here: untyped legacy helpers stay unverified.
            normalize_title_fields(candidate)
            return cls(candidate)
        data = dict(candidate)
        normalize_title_fields(data)
        return cls(data)

    @classmethod
    def from_source_mention(
        cls,
        mention: dict[str, Any],
        *,
        identity_key: str | None = None,
        evidence: dict[str, Any] | None = None,
        russian_search_notes: dict[str, str] | None = None,
    ) -> ScoutingCandidate:
        data = {
            "identity_key": identity_key or cls.identity_key_for_mention(mention),
            **candidate_fields_from_mention(mention),
            "russian_search_notes": russian_search_notes
            if russian_search_notes is not None
            else russian_search_notes_from_mention(mention),
            "source_evidence": [evidence if evidence is not None else source_evidence_from_mention(mention)],
        }
        return cls(data)

    @staticmethod
    def identity_key_for_mention(mention: dict[str, Any]) -> str:
        title = normalize_work_title(str(mention.get("title", "")))
        authors = " & ".join(
            normalize_identity_text(normalize_author_display(str(author)))
            for author in mention.get("primary_authors", [])
        )
        return f"{title}|{authors}"

    @staticmethod
    def source_evidence_from_mention(mention: dict[str, Any]) -> dict[str, Any]:
        return source_evidence_from_mention(mention)

    @staticmethod
    def russian_search_notes_from_mention(mention: dict[str, Any]) -> dict[str, str]:
        return russian_search_notes_from_mention(mention)

    @staticmethod
    def exclusion_reason_for_mapping(candidate_or_mention: dict[str, Any]) -> str:
        source_reason = source_mention_eligibility_exclusion_reason(candidate_or_mention)
        if source_reason:
            return source_reason
        if is_found_in_russian(candidate_or_mention.get("russian_edition_status", "")):
            return FOUND_IN_RUSSIAN
        # Authoritative work type controls the fiction dimension of
        # eligibility (Issue #391). Unrelated hard categories (children,
        # topic-only, ...) keep their own exclusion reasons.
        authoritative_work_type = str(candidate_or_mention.get("authoritative_work_type", "")).strip().casefold()
        category = str(candidate_or_mention.get("candidate_category", "")).strip().lower()
        if category in INELIGIBLE_CANDIDATE_CATEGORIES:
            if authoritative_work_type == "nonfiction" and category == "fiction":
                # A stale retailer "fiction" category must not override
                # authoritative nonfiction metadata.
                return ""
            return category
        # Authoritative fiction is excluded even when its genre does not
        # contain the word "fiction" (e.g. "Fantasy"). Keep the exact
        # "fiction" reason so editor-facing labels stay stable.
        if authoritative_work_type == "fiction":
            return "fiction"
        # Authoritative nonfiction metadata wins over a retailer fiction-genre
        # label (e.g. genre "Science Fiction Studies" stays eligible).
        # Without authoritative work type, fiction genres ("Science Fiction",
        # "science-fiction") are pure fiction while "adult nonfiction" and
        # "non-fiction" stay eligible.
        if authoritative_work_type != "nonfiction" and is_fiction_genre_category(category):
            return "fiction"
        return ""

    def identity_key(self) -> str:
        return str(self.data.get("identity_key") or self.identity_key_for_mention(self.data))

    def exclusion_reason(self) -> str:
        return self.exclusion_reason_for_mapping(self.data)

    def merge_source_mention(self, mention: dict[str, Any]) -> None:
        title_fields = {"english_title", "english_title_type", "english_title_provenance"}
        for field_name in mergeable_candidate_fields_from_mention(mention):
            if field_name in title_fields:
                continue
            if not self.data.get(field_name) and mention.get(field_name):
                self.data[field_name] = mention[field_name]
        title_metadata = merge_title_metadata(self.data, mention)
        if title_metadata:
            self.data.update(title_metadata)
        if not self.data.get("russian_search_notes"):
            self.data["russian_search_notes"] = russian_search_notes_from_mention(mention)

    def append_source_evidence(self, evidence: dict[str, Any]) -> None:
        self.data.setdefault("source_evidence", []).append(evidence)

    def source_mention_fields(self, source: dict[str, Any], source_check_date: str) -> dict[str, Any]:
        return source_mention_fields_from_candidate(source, self.data, source_check_date)

    def apply_signal_aggregation(self) -> None:
        families = self.independent_signal_families()
        self.data["independent_signal_count"] = len(families)
        self.data["independent_signal_families"] = families
        self.data["primary_signal"] = self.primary_signal()
        self.data["priority_label"] = self.priority_label()
        family_label = ", ".join(families)
        plural = "s" if len(families) != 1 else ""
        locator_text = self.signal_locator_text()
        locator_suffix = f" Evidence locator: {locator_text}." if locator_text else ""
        self.data["signal_summary"] = (
            f"{len(families)} independent signal{plural}: {family_label}. "
            "Multiple links from the same family are preserved as evidence without increasing the count."
            f"{locator_suffix}"
        )

    def independent_signal_families(self) -> list[str]:
        families = []
        for evidence in self.data.get("source_evidence", []):
            family = str(evidence.get("source_family", "")).strip()
            if family and family not in families:
                families.append(family)
        return families

    def independent_signal_count(self) -> int:
        if "independent_signal_count" in self.data:
            return int(self.data.get("independent_signal_count", 0))
        return len(self.independent_signal_families())

    def primary_signal(self) -> dict[str, Any]:
        evidence = self.data.get("source_evidence", [])
        if not evidence:
            return {}
        return max(evidence, key=self.signal_strength_rank)

    @staticmethod
    def signal_strength_rank(evidence: dict[str, Any]) -> int:
        strength = str(evidence.get("signal_strength", "")).strip().lower()
        return SIGNAL_STRENGTH_RANKS.get(strength, 0)

    def priority_label(self) -> str:
        """Return the priority label with hard floors enforced inside.

        This is the future Editorial Taste Profile insertion point: soft
        preference nudges should be applied inside this method after hard-floor
        checks, so callers never bypass floor semantics.
        """
        if self.has_clear_visible_risk_hard_floor():
            return "D"
        return self._priority_label_before_soft_preferences()

    def has_clear_visible_risk_hard_floor(self) -> bool:
        visible_risk_status = str(self.data.get("visible_risk_status", "")).strip().lower()
        risk_scope = str(self.data.get("risk_signal_scope", "")).strip().lower()
        return visible_risk_status == "clear book-content visible risk" or (
            visible_risk_status == "clear visible risk" and risk_scope == "book-content"
        )

    def has_no_visible_risk(self) -> bool:
        """Whether the Visible Risk status permits priority A."""
        return str(self.data.get("visible_risk_status", "")).strip().lower() in {"", "no visible risk"}

    def _priority_label_before_soft_preferences(self) -> str:
        independent_count = self.independent_signal_count()
        primary_rank = self.signal_strength_rank(self.data.get("primary_signal") or self.primary_signal())
        publisher_fit = str(self.data.get("publisher_fit", "")).strip().lower()

        has_clear_fit = publisher_fit in {"clear fit", "high", "strong", "strong fit"}
        candidate_is_not_found_in_russian = is_not_found_in_russian(self.data.get("russian_edition_status", ""))

        if (
            independent_count >= 2
            and has_clear_fit
            and candidate_is_not_found_in_russian
            and self.has_no_visible_risk()
        ):
            return "A"
        if independent_count >= 2 and primary_rank >= SIGNAL_STRENGTH_RANKS["shortlist"]:
            return "B"
        if independent_count >= 1:
            return "C"
        return "D"

    def sort_key(self) -> tuple[int, int, int]:
        return (
            PRIORITY_ORDER.get(str(self.data.get("priority_label", "")), 4),
            -self.independent_signal_count(),
            -self.signal_strength_rank(self.data.get("primary_signal") or self.primary_signal()),
        )

    def signal_locator_text(self) -> str:
        locators = []
        for evidence in self.data.get("source_evidence", []):
            locator = str(evidence.get("evidence_locator", "")).strip().rstrip(".")
            if locator and locator not in locators:
                locators.append(locator)
        return " | ".join(locators)

    def apply_visible_risk_screening(self) -> None:
        """Set the editorial Visible Risk status and its operational coverage.

        The coverage record always names what the screen did, so an empty
        status is never the only signal (issue #714).
        """
        if self.data.get("visible_risk_status"):
            self.data.setdefault("visible_risk_category", "")
            self.data.setdefault("visible_risk_comment", "")
            self.data[VISIBLE_RISK_SCREEN_COVERAGE_FIELD] = not_run_coverage()
            return
        try:
            evidence = self.visible_risk_evidence()
            coverage = coverage_for_evidence(evidence)
            self._apply_visible_risk_status(self._visible_risk_category_in(evidence), coverage["coverage"])
        except Exception as exc:  # a failed screen is recorded as coverage, not raised
            for field_name in ("visible_risk_status", "visible_risk_category", "visible_risk_comment"):
                self.data[field_name] = ""
            coverage = failed_coverage(type(exc).__name__)
        self.data[VISIBLE_RISK_SCREEN_COVERAGE_FIELD] = coverage

    def rescreen_visible_risk(self) -> bool:
        """Screen again a candidate the screen could not conclude on.

        Description enrichment runs after the pipeline, so a Content
        Description that arrives later can let a `partial` screen conclude
        (issue #714). Only `partial` coverage is screened again: a supplied
        status (`not_run`) is not the screen's, a `screened` result already
        concluded, and a `failed` screen is not retried silently. A new
        status keeps the two priority rules it controls: clear book-content
        risk forces D, and A requires no visible risk. Returns whether any
        Visible Risk field or the priority label changed.
        """
        if recorded_coverage(self.data.get(VISIBLE_RISK_SCREEN_COVERAGE_FIELD)) != PARTIAL:
            return False
        before = {name: self.data.get(name) or "" for name in VISIBLE_RISK_RESULT_FIELDS}
        self._set_visible_risk("", "", "")
        self.apply_visible_risk_screening()
        if self.has_clear_visible_risk_hard_floor():
            self.data["priority_label"] = "D"
        elif self.data.get("priority_label") == "A" and not self.has_no_visible_risk():
            self.data["priority_label"] = self._priority_label_before_soft_preferences()
        return before != {name: self.data.get(name) or "" for name in VISIBLE_RISK_RESULT_FIELDS}

    def _apply_visible_risk_status(self, risk_category: str, coverage: str) -> None:
        public_data_status = str(self.data.get("public_data_status", "")).strip().lower()
        if risk_category and self.is_weak_author_context_signal():
            self._set_visible_risk(
                "text check required",
                risk_category,
                "Author/context signal in public metadata is weak, indirect, or unverified; "
                "check the text before treating it as clear book-content risk.",
            )
            return
        if risk_category and self.is_credible_book_content_signal():
            evidence_note = str(self.primary_signal().get("evidence_note", "")).rstrip(".")
            comment = (
                f"Book-content signal from public metadata requires human/legal review: "
                f"{evidence_note}. Evidence: {self.data.get('risk_evidence_url', '')}."
            )
            if risk_category == "drugs/microdosing/psychoactive substances":
                comment += (
                    " Drug/microdosing screening treats the September 2025 drug-content rule as operationally relevant."
                )
            self._set_visible_risk("clear visible risk", risk_category, comment)
            return
        # A collector's explicit public-data declaration keeps its meaning.
        if public_data_status == "insufficient":
            self._set_visible_risk(
                "text check required",
                "",
                "Public metadata is insufficient for configured MVP risk screening; "
                "full text or richer public materials are needed.",
            )
            return
        if public_data_status == "sufficient":
            self._set_visible_risk("no visible risk", "", NO_VISIBLE_RISK_COMMENT)
            return
        if risk_category:
            self._set_visible_risk("text check required", risk_category, VISIBLE_RISK_KEYWORD_HINT_COMMENT)
            return
        if coverage == SCREENED:
            self._set_visible_risk("no visible risk", "", NO_VISIBLE_RISK_COMMENT)
        # Partial coverage without a signal concludes nothing; the coverage
        # record explains the empty status.

    def _set_visible_risk(self, status: str, category: str, comment: str) -> None:
        self.data["visible_risk_status"] = status
        self.data["visible_risk_category"] = category
        self.data["visible_risk_comment"] = comment

    def visible_risk_evidence(self) -> dict[str, str]:
        """Return the nonblank public metadata the screen reads, by evidence field."""
        notes = [
            note
            for note in (
                str(evidence.get("evidence_note", "")).strip() for evidence in self.data.get("source_evidence", [])
            )
            if note and note != PLACEHOLDER_EVIDENCE_NOTE
        ]
        content_description = str(self.data.get("content_description", "")).strip()
        # The fallback text says a description is missing; it describes nothing.
        if content_description == DESCRIPTION_FALLBACK:
            content_description = ""
        fields = {
            "title": str(self.data.get("title", "")).strip(),
            "short_description": str(self.data.get("short_description", "")).strip(),
            "content_description": content_description,
            "evidence_notes": " ".join(notes),
        }
        return {name: text for name, text in fields.items() if text}

    def visible_risk_category(self) -> str:
        return self._visible_risk_category_in(self.visible_risk_evidence())

    @staticmethod
    def _visible_risk_category_in(evidence: dict[str, str]) -> str:
        normalized = " ".join(evidence.values()).lower()
        for category, patterns in RISK_CATEGORY_PATTERNS.items():
            if any(pattern.search(normalized) for pattern in patterns):
                return category
        return ""

    def is_weak_author_context_signal(self) -> bool:
        text = " ".join(str(evidence.get("evidence_note", "")) for evidence in self.data.get("source_evidence", []))
        normalized = text.lower()
        return "author" in normalized and any(marker in normalized for marker in ("unverified", "weak", "indirect"))

    def is_credible_book_content_signal(self) -> bool:
        return str(self.data.get("risk_signal_scope", "")).strip().lower() == "book-content" and bool(
            str(self.data.get("risk_evidence_url", "")).strip()
        )

    def exclusion_record(self, exclusion_reason: str, run_date: str) -> dict[str, Any]:
        evidence = self.data.get("russian_edition_evidence") or self.data.get("source_evidence", [])
        record = {
            "identity_key": self.data.get("identity_key", ""),
            "title": self.data.get("title", ""),
            "primary_authors": self.data.get("primary_authors", []),
            "exclusion_reason": exclusion_reason,
            "exclusion_date": run_date,
            "russian_edition_url": self.data.get("russian_edition_url", ""),
            "evidence": evidence,
        }
        verification = self.data.get("russian_edition_verification")
        if is_valid_russian_edition_verification(verification):
            record["russian_edition_verification"] = deepcopy(verification)
        note = self.data.get("russian_edition_note")
        if note:
            record["russian_edition_note"] = str(note)
        return record

    def set_editor_status(self, editor_status: str) -> None:
        self.data["editor_status"] = editor_status

    def set_editor_note(self, editor_note: str) -> None:
        self.data["editor_note"] = editor_note

    def set_feedback_dimensions(self, dimensions: dict[str, str]) -> None:
        """Store confirmed structured feedback dimensions (Issue #678)."""
        self.data["feedback_dimensions"] = dict(dimensions)

    def set_material_change_reasons(self, reasons: list[str]) -> None:
        self.data["material_change_reasons"] = reasons

    def generated_fact_value(self, field: str) -> str:
        value = self.data.get(field, "")
        if field == "primary_authors":
            return ", ".join(value)
        return str(value)

    def material_change_reasons(self, history: dict[str, Any] | None) -> list[str]:
        if not history:
            return []
        reasons = []
        previous_families = set(history.get("independent_signal_families", []))
        current_families = set(self.data.get("independent_signal_families", []))
        if current_families - previous_families:
            reasons.append("new independent signal family")

        previous_strength = str(history.get("primary_signal_strength", ""))
        current_strength = str(self.data.get("primary_signal", {}).get("signal_strength", ""))
        if SIGNAL_STRENGTH_RANKS.get(current_strength.lower(), 0) > SIGNAL_STRENGTH_RANKS.get(
            previous_strength.lower(), 0
        ):
            reasons.append("signal strength improved")

        for field_name, label in (
            ("russian_edition_status", "Russian status changed"),
            ("visible_risk_status", "Visible Risk status changed"),
        ):
            if not str(history.get(field_name, "")) or str(self.data.get(field_name, "")) == str(
                history.get(field_name, "")
            ):
                continue
            if field_name == "russian_edition_status":
                verification = self.data.get("russian_edition_verification")
                if (
                    is_not_found_in_russian(history.get(field_name, ""))
                    and is_unclear(self.data.get(field_name, ""))
                    and isinstance(verification, dict)
                    and bool(verification)
                    and is_non_definitive_russian_normalization(self.data, history)
                ):
                    continue
            if field_name == "visible_risk_status" and not str(self.data.get(field_name, "")):
                # An empty status is a screen that concluded nothing, for
                # example before the Content Description arrives (issue
                # #714). Unknown is not a change; the post-enrichment
                # rescreen labels a status it concludes later.
                continue
            reasons.append(label)

        if history.get("explicit_reset"):
            reasons.append("explicit reset")
        if history.get("accepted_correction_changed_identity_or_status"):
            reasons.append("accepted correction changed identity or status")
        return reasons

    def editor_row(self) -> dict[str, str]:
        evidence = self.data.get("primary_signal") or (
            self.data["source_evidence"][0] if self.data.get("source_evidence") else {}
        )
        description = self.editor_description()
        description_status = (
            DESCRIPTION_UNCONFIRMED_STATUS
            if is_unconfirmed_description(description, self.data.get("description_provenance"))
            else ""
        )
        return {
            "Check date": str(evidence.get("source_check_date", "")),
            "Source": str(evidence.get("source_name", "")),
            "Source URL": str(evidence.get("source_url", "")),
            "Title": clean_title(self.data.get("title", "")),
            "Author": ", ".join(self.data.get("primary_authors", [])),
            "Annotation": self.editor_annotation(evidence),
            "Description": description,
            "Description status": description_status,
            "English title": format_editor_english_title(self.data),
            "English author display": format_editor_english_author_display(self.data),
            "English annotation": str(self.data.get("english_annotation", "")),
            "Publisher / year": self.publisher_year(),
            "Category / topic": str(self.data.get("candidate_category", "")),
            "Why it entered the list": str(evidence.get("evidence_note", "")),
            "Signal count": str(self.data.get("independent_signal_count", "")),
            "Signal summary": str(self.data.get("signal_summary", "")),
            "Priority label": str(self.data.get("priority_label", "")),
            "Russian-edition status": str(self.data.get("russian_edition_status", "")),
            "Russian-edition URL": str(self.data.get("russian_edition_url", "")),
            "Visible-risk status": str(self.data.get("visible_risk_status", "")),
            "Risk comment": str(self.data.get("visible_risk_comment", "")),
            "Links": self.evidence_links(),
            "Update delta": str(self.data.get("update_delta", "")),
            "Taste profile notes": str(self.data.get("editorial_profile_notes", "")),
            "Editor status": str(self.data.get("editor_status", "")),
            "Editor note": str(self.data.get("editor_note", "")),
            "Candidate ID": str(self.data.get("identity_key", "")),
            "Durable Candidate ID": str(self.data.get("candidate_id", "")),
        }

    def publisher_year(self) -> str:
        publisher = str(self.data.get("publisher", ""))
        year = str(self.data.get("publication_year", ""))
        if publisher and year:
            return f"{publisher} / {year}"
        return publisher or year

    def editor_annotation(self, evidence: dict[str, Any]) -> str:
        short_description = str(self.data.get("short_description", "")).strip()
        category = str(self.data.get("candidate_category", "")).strip()
        evidence_note = str(evidence.get("evidence_note", "")).strip()
        signal_summary = str(self.data.get("signal_summary", "")).strip()
        public_data_status = str(self.data.get("public_data_status", "")).strip().lower()
        visible_risk_status = str(self.data.get("visible_risk_status", "")).strip()
        visible_risk_comment = str(self.data.get("visible_risk_comment", "")).strip()

        context_sentences = []
        if short_description:
            context_sentences.append(short_description if short_description.endswith(".") else f"{short_description}.")
        if category and category.lower() not in short_description.lower():
            context_sentences.append(f"Category/topic: {category}.")
        if evidence_note and evidence_note != short_description:
            context_sentences.append(evidence_note if evidence_note.endswith(".") else f"{evidence_note}.")
        if signal_summary:
            context_sentences.append(signal_summary if signal_summary.endswith(".") else f"{signal_summary}.")
        if public_data_status == "insufficient":
            context_sentences.append("Public metadata is insufficient for a complete editorial read.")
        if visible_risk_status and visible_risk_status != "no visible risk":
            risk_sentence = f"Visible Risk: {visible_risk_status}"
            if visible_risk_comment:
                risk_sentence = f"{risk_sentence}; {visible_risk_comment}"
            context_sentences.append(f"{risk_sentence}.")

        return truncate_annotation(" ".join(context_sentences))

    def editor_description(self) -> str:
        content_description = str(self.data.get("content_description", "")).strip()
        if content_description:
            return truncate_annotation(content_description)
        return DESCRIPTION_FALLBACK

    def evidence_links(self) -> str:
        links = []
        for evidence in self.data.get("source_evidence", []):
            url = str(evidence.get("evidence_url") or evidence.get("source_url", ""))
            if url and url not in links:
                links.append(url)
            source_url = str(evidence.get("source_url", ""))
            if source_url and source_url not in links:
                links.append(source_url)
        russian_url = str(self.data.get("russian_edition_url", ""))
        if russian_url and russian_url not in links:
            links.append(russian_url)
        risk_url = str(self.data.get("risk_evidence_url", ""))
        if risk_url and risk_url not in links:
            links.append(risk_url)
        return " | ".join(links)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


SCHEMA_DECLARED_ONE_SIDE_ONLY_FIELDS = CANDIDATE_ONLY_FIELDS | SOURCE_MENTION_ONLY_FIELDS


def truncate_annotation(value: str, limit: int = 600) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def append_source_evidence(candidate: dict[str, Any] | ScoutingCandidate, evidence: dict[str, Any]) -> None:
    ScoutingCandidate.from_mapping(candidate).append_source_evidence(evidence)


def authoritative_exclusion_record(
    candidate: ScoutingCandidate | dict[str, Any],
    exclusion_reason: str,
    run_date: str,
) -> dict[str, Any]:
    """Build one canonical exclusion record preserving authoritative metadata.

    The authoritative work type and genre travel with the record (Issue
    #391) so cumulative excluded state keeps the grounded evidence across
    runs. The free-form authoritative subject statement is acceptance-gate
    evidence only and never appears here.
    """
    entity = ScoutingCandidate.from_mapping(candidate)
    record = entity.exclusion_record(exclusion_reason, run_date)
    authoritative_genre = str(entity.get("authoritative_genre", "")).strip()
    if authoritative_genre:
        record["authoritative_genre"] = authoritative_genre
    authoritative_work_type = str(entity.get("authoritative_work_type", "")).strip()
    if authoritative_work_type:
        record["authoritative_work_type"] = authoritative_work_type
    return record
