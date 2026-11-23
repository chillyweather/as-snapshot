"""Issue #678: retained decision-support data.

The renderer retains the most common decision blockers when evidence
exists and clearly marks unknown values: method, scope, genre/treatment,
author relevance, usable translated title, Russian-edition status, and
rights/acquisition status.
"""

from __future__ import annotations

from autoscout.html_review import build_html_review
from autoscout.review_presentation import decision_support_items
from autoscout.review_rendering import _render_neutral_row
from tests.issue_678_helpers import make_dims as _dims

UNKNOWN = "нет данных"


def _candidate(**overrides):
    candidate = {
        "identity_key": "decision book|author",
        "title": "Decision Book",
        "primary_authors": ["Author"],
        "candidate_category": "adult nonfiction",
    }
    candidate.update(overrides)
    return candidate


def _row(**overrides):
    row = {
        "Title": "Decision Book",
        "Author": "Author",
        "Editor status": "",
        "Editor note": "",
        "Candidate ID": "decision book|author",
        "Update delta": "new",
        "Priority label": "",
        "Signal count": "1",
        "Russian-edition status": "",
        "Source": "Test Source",
        "Source URL": "",
    }
    row.update(overrides)
    return row


def _rendered(candidate: dict, row: dict | None = None) -> dict:
    return _render_neutral_row(candidate, row or _row())


class DecisionSupportItemTests:
    def test_all_seven_blockers_are_listed_with_unknowns_marked(self) -> None:
        items = decision_support_items(_rendered(_candidate()))
        labels = [label for label, _value in items]
        assert labels == [
            "Метод",
            "Область",
            "Жанр/подача",
            "Релевантность автора",
            "Перевод названия",
            "Русское издание",
            "Права/приобретение",
        ]
        values = dict(items)
        # No evidence for method, scope, author relevance, translation,
        # Russian edition, or rights: every one is clearly marked unknown.
        assert values["Метод"] == UNKNOWN
        assert values["Область"] == UNKNOWN
        assert values["Релевантность автора"] == UNKNOWN
        assert values["Перевод названия"] == UNKNOWN
        assert values["Русское издание"] == UNKNOWN
        assert values["Права/приобретение"] == UNKNOWN
        # Genre/treatment has evidence: the candidate category.
        assert values["Жанр/подача"] == "adult nonfiction"

    def test_available_evidence_fills_the_blockers(self) -> None:
        dims = _dims(rights_availability="unavailable")
        rendered = _rendered(
            _candidate(
                narrative_classification="oral history",
                english_title="Decision Book",
                english_title_type="generated_rendering",
                english_title_provenance={"kind": "generated_rendering", "research_sources": []},
                russian_edition_status="not_found",
                feedback_dimensions=dict(dims),
            )
        )
        values = dict(decision_support_items(rendered))
        assert values["Метод"] == "oral history"
        assert values["Перевод названия"] == "Decision Book (machine translation)"
        assert values["Русское издание"] != UNKNOWN
        assert values["Права/приобретение"] != UNKNOWN

    def test_unverified_helper_title_keeps_its_qualifier_on_the_card(self) -> None:
        """Class-4 contract: an untyped helper never renders as a usable translation."""
        rendered = _rendered(_candidate(english_title="Decision Book"))
        assert dict(decision_support_items(rendered))["Перевод названия"] == "Decision Book (unverified)"

    def test_author_relevance_uses_editorial_profile_notes_as_evidence(self) -> None:
        rendered = _rendered(_candidate(editorial_profile_notes="Автор уже в предпочтениях редактора"))
        values = dict(decision_support_items(rendered))
        assert values["Релевантность автора"] == "Автор уже в предпочтениях редактора"

    def test_rights_dimension_values_map_to_editor_wording(self) -> None:
        for raw, expected in (
            ("available", "права доступны"),
            ("unavailable", "права недоступны"),
            ("unknown", "права: неизвестно"),
        ):
            dims = _dims(rights_availability=raw)
            rendered = _rendered(_candidate(feedback_dimensions=dict(dims)))
            assert dict(decision_support_items(rendered))["Права/приобретение"] == expected

    def test_card_covers_exactly_the_seven_spec_blockers(self) -> None:
        """Issue #678 item 3 enumerates exactly seven blockers; the quality
        and information dimensions are recorded feedback data, not card
        rows, so they never appear in the decision-support line."""
        dims = _dims(quality_problem="description_missing", information_needed="purchase_status")
        rendered = _rendered(_candidate(feedback_dimensions=dict(dims)))
        labels = [label for label, _value in decision_support_items(rendered)]
        assert len(labels) == 7
        assert "Проблема качества" not in labels
        assert "Нужно узнать" not in labels


class DecisionSupportCardTests:
    def test_card_omits_the_decision_support_line(self) -> None:
        dims = _dims(rights_availability="unavailable")
        rendered = _rendered(_candidate(feedback_dimensions=dict(dims)))
        html = build_html_review([rendered], _summary())
        assert "Подсказка для решения" not in html
        assert "права недоступны" not in html

    def test_card_does_not_show_unknown_decision_values(self) -> None:
        rendered = _rendered(_candidate())
        html = build_html_review([rendered], _summary())
        assert "Подсказка для решения" not in html
        assert UNKNOWN not in html


def _summary(**overrides):
    summary = {
        "delta_counts": {"new": 1, "upgraded": 0, "changed": 0, "unchanged": 0},
        "hidden_by_rule_count": 0,
        "editor_rows_count": 1,
        "total_count": 1,
    }
    summary.update(overrides)
    return summary
