from __future__ import annotations

from pathlib import Path

from state_store_support import read_run_output
from state_store_support import seed_state

from autoscout.autobiographical_context import AUTOBIOGRAPHICAL_SUBJECT_CONTEXT_REQUIREMENT
from autoscout.autobiographical_context import autobiographical_subject_context_warning
from autoscout.autobiographical_context import is_autobiographical_candidate
from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.content_description import apply_description_artifacts
from autoscout.content_description import prepare_backfill_work_items
from autoscout.description_enrichment import _encode_work_items
from autoscout.description_enrichment import run_enrichment
from autoscout.json_io import write_json
from autoscout.pipeline import update_candidate_history_snapshots
from autoscout.review_rendering import render_review
from autoscout.scouting_candidates import ScoutingCandidate
from autoscout.source_collectors.agent_mentions import agent_extraction_mentions_from_response
from autoscout.source_collectors.contract import agent_extraction_prompt

EVIDENCE_URL = "https://people.example.test/michael-douglas-memoir"


def _subject_context(
    *,
    opening_phrase: str = "актёра и продюсера",
    source_phrase: str = "actor and producer",
    evidence_url: str = EVIDENCE_URL,
) -> dict[str, str]:
    return {
        "opening_phrase": opening_phrase,
        "source_phrase": source_phrase,
        "evidence_url": evidence_url,
    }


def _memoir_candidate(description: str, *, subject_context: dict[str, str] | None = None) -> dict:
    evidence = {
        "source_name": "PEOPLE Books",
        "source_url": "https://people.example.test/books",
        "evidence_url": EVIDENCE_URL,
        "signal_type": "new-memoir cover reveal",
        "evidence_note": "Landing-page card identifies the book title and author.",
    }
    candidate = {
        "identity_key": "one-helluva-ride|michael-douglas",
        "title": "One Helluva Ride",
        "primary_authors": ["Michael Douglas"],
        "candidate_category": "adult nonfiction",
        "update_delta": "new",
        "priority_label": "C",
        "independent_signal_count": 1,
        "russian_edition_status": "not found in Russian",
        "content_description": description,
        "source_evidence": [evidence],
    }
    if subject_context:
        candidate["autobiographical_subject_context"] = subject_context
        evidence["autobiographical_subject_context"] = subject_context
    return candidate


def _render(tmp_path: Path, candidate: dict) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    write_json(tmp_path / "scouting-candidates.json", [candidate])
    write_json(tmp_path / "run-summary.json", {"run_date": "2026-07-28", "editor_facing_count": 1})
    assert render_review(tmp_path) == 0
    return (tmp_path / "weekly-review.html").read_text(encoding="utf-8")


def test_live_extraction_prompt_uses_the_shared_subject_context_contract() -> None:
    prompt = agent_extraction_prompt(
        {"name": "PEOPLE Books", "family": "media/curated list", "language": "en"},
        {"html": "<article>Michael Douglas is an actor and producer.</article>"},
        5000,
    )

    assert AUTOBIOGRAPHICAL_SUBJECT_CONTEXT_REQUIREMENT in prompt
    assert "opening_phrase" in prompt
    assert "source_phrase" in prompt
    assert "evidence_url" in prompt
    assert "provided HTML" in prompt
    assert "outside knowledge" in prompt


def test_backfill_work_item_carries_the_same_subject_context_contract() -> None:
    candidate = _memoir_candidate("")

    items = prepare_backfill_work_items([candidate])

    assert len(items) == 1
    assert items[0]["description_requirements"] == [AUTOBIOGRAPHICAL_SUBJECT_CONTEXT_REQUIREMENT]
    assert _encode_work_items(items)[0]["description_requirements"] == items[0]["description_requirements"]


def test_explicit_metadata_classifies_memoir_without_reading_description_prose() -> None:
    memoir = _memoir_candidate("Описание без жанрового слова.")
    non_memoir = {
        "title": "The Mind Electric",
        "candidate_category": "science",
        "content_description": "Невролог соединяет клинические истории, историю медицины, мемуар и путешествия.",
        "source_evidence": [{"signal_type": "media review"}],
    }

    assert is_autobiographical_candidate(memoir) is True
    assert is_autobiographical_candidate(non_memoir) is False


def test_first_person_classification_uses_the_closed_vocabulary() -> None:
    exact = {
        "title": "A Life",
        "source_evidence": [{"narrative_classification": "first-person personal-life narrative"}],
    }
    invented_variant = {
        "title": "A Life",
        "source_evidence": [{"narrative_classification": "first person personal book"}],
    }

    assert is_autobiographical_candidate(exact) is True
    assert is_autobiographical_candidate(invented_variant) is False


def test_personal_history_prose_does_not_classify_non_memoirs() -> None:
    for title, description in [
        (
            "Histoire personnelle de la France",
            "Личная история Франции соединяет национальное прошлое с индивидуальной точкой зрения автора.",
        ),
        (
            "Crown",
            "Личная история становится исследованием власти чужого взгляда и самоопределения.",
        ),
    ]:
        candidate = {
            "title": title,
            "candidate_category": "adult nonfiction",
            "content_description": description,
            "source_evidence": [{"signal_type": "award"}],
        }
        assert is_autobiographical_candidate(candidate) is False


def test_existing_memoir_without_grounded_context_is_not_retried() -> None:
    candidate = _memoir_candidate("Мемуары Майкла Дугласа рассказывают о его жизни и карьере в кино.")

    items = prepare_backfill_work_items([candidate])

    assert items == []


def test_backfill_repairs_legacy_memoir_only_with_preverified_context() -> None:
    context = _subject_context()
    existing = _memoir_candidate(
        "Мемуары Майкла Дугласа рассказывают о его жизни и карьере в кино.",
        subject_context=context,
    )
    improved = "Мемуары актёра и продюсера Майкла Дугласа рассказывают о его жизни и карьере в кино."

    items = prepare_backfill_work_items([existing])
    updated, results = apply_description_artifacts(
        [existing],
        [
            {
                "identity_key": existing["identity_key"],
                "content_description": improved,
                "autobiographical_subject_context": context,
                "research_sources": [EVIDENCE_URL],
            }
        ],
    )

    assert items[0]["replaces_existing_description"] is True
    assert results["applied"] == 1
    assert updated[0]["content_description"] == improved
    assert updated[0]["autobiographical_subject_context"] == context


def test_explicit_override_replaces_warned_memoir_without_grounded_context() -> None:
    existing = _memoir_candidate("Мемуары Майкла Дугласа рассказывают о его жизни и карьере в кино.")
    corrected = "Мемуары Майкла Дугласа подробно рассматривают его путь в кино и семейную историю."

    updated, results = apply_description_artifacts(
        [existing],
        [
            {
                "identity_key": existing["identity_key"],
                "content_description": corrected,
                "research_sources": [EVIDENCE_URL],
            }
        ],
        allow_overwrite_valid=True,
    )

    assert results["applied"] == 1
    assert results["skipped"] == 0
    assert updated[0]["content_description"] == corrected
    assert autobiographical_subject_context_warning(updated[0])


def test_invented_context_is_not_authorized_by_an_unrelated_evidence_url() -> None:
    existing = _memoir_candidate("Мемуары Майкла Дугласа рассказывают о его жизни и карьере в кино.")
    invented = _subject_context(
        opening_phrase="лауреата Нобелевской премии",
        source_phrase="Nobel Prize winner",
    )

    updated, results = apply_description_artifacts(
        [existing],
        [
            {
                "identity_key": existing["identity_key"],
                "content_description": ("Мемуары лауреата Нобелевской премии Майкла Дугласа рассказывают о его жизни."),
                "autobiographical_subject_context": invented,
                "research_sources": [EVIDENCE_URL],
            }
        ],
    )

    assert results["applied"] == 0
    assert results["skipped"] == 1
    assert updated[0]["content_description"] == existing["content_description"]
    assert "autobiographical_subject_context" not in updated[0]


def test_valid_description_is_preserved_when_context_is_unsupported() -> None:
    candidate = _memoir_candidate("")
    description = "Мемуары Майкла Дугласа подробно рассказывают о его жизни и карьере в кино."

    updated, results = apply_description_artifacts(
        [candidate],
        [
            {
                "identity_key": candidate["identity_key"],
                "content_description": description,
                "research_sources": [EVIDENCE_URL],
            }
        ],
    )

    assert results["applied"] == 1
    assert results["fallback"] == 0
    assert updated[0]["content_description"] == description
    assert updated[0]["content_description"] != DESCRIPTION_FALLBACK
    assert autobiographical_subject_context_warning(updated[0])


def test_review_flags_fallback_and_legacy_memoirs_without_context(tmp_path: Path) -> None:
    for index, description in enumerate(
        [
            "",
            "Мемуары Майкла Дугласа рассказывают о его жизни и карьере в кино.",
        ]
    ):
        html = _render(tmp_path / str(index), _memoir_candidate(description))
        assert "<dt>Контекст героя</dt>" in html
        assert "В начале описания не указано, чем известен герой книги." in html


def test_inflected_opening_phrase_is_valid_when_grounded_source_phrase_matches() -> None:
    candidate = _memoir_candidate(
        "Мемуары актёра и продюсера Майкла Дугласа рассказывают о его жизни и карьере в кино.",
        subject_context=_subject_context(
            opening_phrase="актёра и продюсера",
            source_phrase="actor and producer",
        ),
    )

    assert autobiographical_subject_context_warning(candidate) == ""


def test_initials_do_not_truncate_the_opening_context_check() -> None:
    context = _subject_context(
        opening_phrase="писателя и филолога",
        source_phrase="writer and philologist",
    )
    candidate = _memoir_candidate(
        "Мемуары J. R. R. Толкина, писателя и филолога, охватывают его жизнь. Вторая часть посвящена семье.",
        subject_context=context,
    )

    assert autobiographical_subject_context_warning(candidate) == ""


def test_source_mention_preserves_structured_subject_context() -> None:
    context = _subject_context()
    candidate = ScoutingCandidate.from_source_mention(
        {
            "title": "One Helluva Ride",
            "primary_authors": ["Michael Douglas"],
            "content_description": ("Мемуары актёра и продюсера Майкла Дугласа рассказывают о его карьере в кино."),
            "autobiographical_subject_context": context,
            "source_name": "Publisher",
            "source_family": "publisher",
            "source_url": EVIDENCE_URL,
            "evidence_url": EVIDENCE_URL,
            "signal_type": "memoir",
            "evidence_note": "Publisher metadata identifies the subject.",
        }
    )

    assert candidate.data["autobiographical_subject_context"] == context
    assert candidate.data["source_evidence"][0]["autobiographical_subject_context"] == context


def test_verified_subject_context_survives_history_only_review(tmp_path: Path) -> None:
    context = _subject_context()
    description = "Мемуары актёра и продюсера Майкла Дугласа рассказывают о его карьере в кино."
    candidate = _memoir_candidate(description, subject_context=context)
    history = update_candidate_history_snapshots([], [candidate], "2026-07-28")

    assert history[0]["autobiographical_subject_context"] == context

    write_json(tmp_path / "scouting-candidates.json", [])
    seed_state(tmp_path, candidate_history=history)
    write_json(tmp_path / "run-summary.json", {"run_date": "2026-07-29", "editor_facing_count": 1})

    assert render_review(tmp_path) == 0
    html = (tmp_path / "weekly-review.html").read_text(encoding="utf-8")
    assert "В начале описания не указано, чем известен герой книги." not in html


def test_extraction_keeps_only_context_grounded_in_captured_html() -> None:
    page_data = {
        "source_page": "https://people.example.test/books",
        "html": (
            '<a href="https://people.example.test/michael-douglas-memoir">One Helluva Ride</a>'
            "<p>Michael Douglas is an actor and producer.</p>"
        ),
    }
    source = {
        "name": "PEOPLE Books",
        "family": "media/curated list",
        "url": page_data["source_page"],
        "language": "en",
    }
    base_mention = {
        "title": "One Helluva Ride",
        "primary_authors": ["Michael Douglas"],
        "source_url": page_data["source_page"],
        "evidence_url": EVIDENCE_URL,
        "signal_type": "memoir",
        "signal_strength": "major media list",
        "evidence_note": "The captured page identifies the title and author.",
        "content_description": ("Мемуары актёра и продюсера Майкла Дугласа рассказывают о его карьере в кино."),
    }

    grounded, _ = agent_extraction_mentions_from_response(
        page_data,
        {"mentions": [{**base_mention, "autobiographical_subject_context": _subject_context()}]},
        source,
        "2026-07-28",
    )
    invented, _ = agent_extraction_mentions_from_response(
        page_data,
        {
            "mentions": [
                {
                    **base_mention,
                    "autobiographical_subject_context": _subject_context(source_phrase="Nobel Prize winner"),
                }
            ]
        },
        source,
        "2026-07-28",
    )

    assert grounded[0]["autobiographical_subject_context"] == _subject_context()
    assert "autobiographical_subject_context" not in invented[0]
    assert invented[0]["content_description"] == base_mention["content_description"]


def test_empty_source_mention_omits_optional_structured_context() -> None:
    candidate = ScoutingCandidate.from_source_mention(
        {
            "title": "Reported Book",
            "primary_authors": ["Alex Example"],
            "source_url": "https://example.test/list",
            "evidence_url": "https://example.test/book",
            "signal_type": "media review",
            "signal_strength": "minor mention",
            "evidence_note": "The source identifies the book.",
        }
    )

    assert "autobiographical_subject_context" not in candidate.data
    assert "autobiographical_subject_context" not in candidate.data["source_evidence"][0]


def test_enrichment_uses_only_context_already_verified_in_source_evidence(tmp_path: Path) -> None:
    context = _subject_context()
    candidate = _memoir_candidate("", subject_context=context)
    write_json(tmp_path / "scouting-candidates.json", [candidate])
    seed_state(tmp_path, candidate_history=[candidate])

    class SupportedContextAgent:
        def generate(self, work_items):
            return {
                work_items[0]["identity_key"]: {
                    "content_description": (
                        "Мемуары актёра и продюсера Майкла Дугласа рассказывают о его карьере в кино."
                    ),
                    "autobiographical_subject_context": context,
                }
            }

    result = run_enrichment(tmp_path, agent=SupportedContextAgent())
    history = read_run_output(tmp_path, "candidate_history")

    assert result["enriched"] == 1
    assert history[0]["autobiographical_subject_context"] == context


def test_enrichment_keeps_description_when_subject_context_is_unavailable(tmp_path: Path) -> None:
    candidate = _memoir_candidate("")
    write_json(tmp_path / "scouting-candidates.json", [candidate])
    seed_state(tmp_path, candidate_history=[candidate])

    class DescriptionOnlyAgent:
        def generate(self, work_items):
            return {
                work_items[0]["identity_key"]: {
                    "content_description": ("Мемуары Майкла Дугласа рассказывают о его жизни и карьере в кино."),
                }
            }

    result = run_enrichment(tmp_path, agent=DescriptionOnlyAgent())
    history = read_run_output(tmp_path, "candidate_history")

    assert result["enriched"] == 1
    assert history[0]["content_description"].startswith("Мемуары Майкла Дугласа")
    assert autobiographical_subject_context_warning(history[0])
