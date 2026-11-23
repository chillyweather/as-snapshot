from __future__ import annotations

import csv
import io

from autoscout.html_review import build_html_review
from autoscout.pipeline import update_candidate_history_snapshots
from autoscout.scouting_candidates import ScoutingCandidate
from autoscout.source_mentions import SourceMention
from autoscout.title_evidence import TITLE_TYPE_GENERATED_RENDERING
from autoscout.title_evidence import TITLE_TYPE_SOURCED_PUBLISHED
from autoscout.title_evidence import format_editor_english_author_display
from autoscout.title_evidence import normalize_title_fields
from autoscout.title_evidence import title_is_identity_evidence


def _sourced() -> dict:
    return {
        "title": "Das Buch, von dem du dir wünschst, deine Eltern hätten es gelesen",
        "primary_authors": ["Philippa Perry"],
        "english_title": "The Book You Wish Your Parents Had Read",
        "english_title_type": TITLE_TYPE_SOURCED_PUBLISHED,
        "english_title_provenance": {
            "kind": TITLE_TYPE_SOURCED_PUBLISHED,
            "research_sources": ["https://publisher.example/books/parents"],
            "mapping": {
                "original_title": "Das Buch, von dem du dir wünschst, deine Eltern hätten es gelesen",
                "original_authors": ["Philippa Perry"],
                "published_title": "The Book You Wish Your Parents Had Read",
                "published_authors": ["Philippa Perry"],
            },
        },
        "identity_key": "das buch von dem du dir wünschst deine eltern hätten es gelesen|philippa perry",
        "source_evidence": [],
    }


def _rendering() -> dict:
    mention = {
        "title": "Dreihundert Männer",
        "primary_authors": ["Konstantin Richter"],
        "english_title": "Three Hundred Men",
    }
    return normalize_title_fields(mention, classify=True)


def test_published_title_mapping_is_identity_evidence_and_literal_translation_is_not() -> None:
    sourced = _sourced()
    assert title_is_identity_evidence(sourced)

    literal = {**sourced, "english_title": "The Book You Would Have Wanted for Your Parents"}
    assert not title_is_identity_evidence(literal)


def test_rendering_is_typed_but_never_identity_evidence() -> None:
    rendering = _rendering()
    assert rendering["english_title_type"] == TITLE_TYPE_GENERATED_RENDERING
    assert rendering["english_title_provenance"]["research_sources"] == []
    assert not title_is_identity_evidence(rendering)


def test_invalid_typed_titles_are_unverified_and_raw_collector_claims_are_downgraded() -> None:
    malformed = {
        "english_title": "Published Helper",
        "english_title_type": TITLE_TYPE_SOURCED_PUBLISHED,
        "english_title_provenance": {"kind": TITLE_TYPE_SOURCED_PUBLISHED, "research_sources": []},
    }
    candidate = ScoutingCandidate.from_mapping(malformed)
    assert candidate.editor_row()["English title"] == "Published Helper (unverified)"
    malformed_original = ScoutingCandidate.from_mapping(
        {
            "title": "Original Work",
            "english_title": "Different Helper",
            "english_title_type": "original",
            "english_title_provenance": {"kind": "original", "research_sources": []},
        }
    )
    assert malformed_original.editor_row()["English title"] == "Different Helper (unverified)"

    raw_claim = {
        **_sourced(),
        "english_title": "The Book You Wish Your Parents Had Read",
    }
    normalized = normalize_title_fields(raw_claim, classify=True)
    assert normalized["english_title_type"] == TITLE_TYPE_GENERATED_RENDERING


def test_sourced_title_survives_candidate_source_mention_projection() -> None:
    candidate = ScoutingCandidate.from_mapping(_sourced())
    source = {"name": "Canonical", "family": "award", "url": "https://example.test/source"}
    mention = SourceMention.from_candidate(
        source,
        candidate.data,
        "2026-08-14",
        classify_titles=True,
        allow_sourced=True,
    ).to_dict()
    assert mention["english_title_type"] == TITLE_TYPE_SOURCED_PUBLISHED
    reconstructed = ScoutingCandidate.from_source_mention(mention)
    assert title_is_identity_evidence(reconstructed.data)


def test_evidence_rejects_wrong_author_empty_provenance_and_preserves_original_title() -> None:
    sourced = _sourced()
    assert sourced["title"].startswith("Das Buch")
    wrong_author = {**sourced, "primary_authors": ["Wrong Author"]}
    assert not title_is_identity_evidence(wrong_author)
    wrong_published_author = {
        **sourced,
        "english_title_provenance": {
            **sourced["english_title_provenance"],
            "mapping": {
                **sourced["english_title_provenance"]["mapping"],
                "published_authors": ["Wrong Author"],
            },
        },
    }
    assert not title_is_identity_evidence(wrong_published_author)
    missing = {**sourced, "english_title_provenance": {"kind": TITLE_TYPE_SOURCED_PUBLISHED}}
    assert not title_is_identity_evidence(missing)
    untyped = {key: value for key, value in sourced.items() if key != "english_title_type"}
    assert not title_is_identity_evidence(untyped)


def test_sourced_title_survives_candidate_merge_and_history_snapshot() -> None:
    candidate = ScoutingCandidate.from_source_mention(_sourced())
    candidate.merge_source_mention(_rendering())
    assert candidate.data["english_title"] == "The Book You Wish Your Parents Had Read"
    assert candidate.data["english_title_type"] == TITLE_TYPE_SOURCED_PUBLISHED

    history = update_candidate_history_snapshots([], [candidate], "2026-08-14")
    assert history[0]["english_title_type"] == TITLE_TYPE_SOURCED_PUBLISHED
    assert history[0]["english_title_provenance"]["research_sources"]


def test_typed_title_survives_identity_history_merge() -> None:
    sourced = _sourced()
    rendering = _rendering()
    rendering.update(
        {
            "identity_key": sourced["identity_key"],
            "title": sourced["title"],
            "primary_authors": sourced["primary_authors"],
        }
    )
    candidate = ScoutingCandidate.from_source_mention(sourced)
    candidate.merge_source_mention(rendering)
    history = update_candidate_history_snapshots([rendering], [candidate], "2026-08-14")
    assert history[0]["english_title_type"] == TITLE_TYPE_SOURCED_PUBLISHED


def test_collector_entities_are_unescaped_without_completing_truncation() -> None:
    source = {"name": "Collector", "family": "media/curated list", "url": "https://example.test/source"}
    raw_mentions = [
        {
            "title": "Finding Renée Richards: The Groundbreaking Stor&hellip;",
            "primary_authors": ["Jane Doe"],
            "source_url": "https://example.test/finding",
            "source_check_date": "2026-08-14",
            "signal_type": "media/curated list",
            "signal_strength": "major media list",
            "evidence_note": "A bounded collector mention.",
        },
        {
            "title": "The Jackal: The Rise and Fall of Carlos, the Wor&hellip;",
            "primary_authors": ["John Doe"],
            "source_url": "https://example.test/jackal",
            "source_check_date": "2026-08-14",
            "signal_type": "media/curated list",
            "signal_strength": "major media list",
            "evidence_note": "A bounded collector mention.",
        },
    ]
    mentions = [SourceMention.from_collector(source, raw, "2026-08-14").to_dict() for raw in raw_mentions]
    assert mentions[0]["title"].endswith("Stor…")
    assert mentions[1]["title"].endswith("Wor…")
    assert "&hellip;" not in " ".join(mention["title"] for mention in mentions)

    csv_row = io.StringIO()
    writer = csv.writer(csv_row)
    writer.writerow([mention["title"] for mention in mentions])
    assert "&hellip;" not in csv_row.getvalue()
    html = build_html_review(
        [
            {"idx": index, "identity_key": str(index), "title": mention["title"], "authors": "Author"}
            for index, mention in enumerate(mentions, start=1)
        ],
        {"total_count": 2},
    )
    assert "&hellip;" not in html
    assert "Stor…" in html
    assert "Wor…" in html


def test_html_entities_are_cleaned_without_completing_truncation() -> None:
    candidate = ScoutingCandidate.from_mapping(
        {
            "title": "A &hellip; &copy; title...",
            "primary_authors": ["Author"],
            "english_title": "A &hellip; &copy; title...",
            "english_title_type": "original",
            "english_title_provenance": {"kind": "original", "research_sources": []},
            "identity_key": "a title|author",
        }
    )
    row = candidate.editor_row()
    csv_row = io.StringIO()
    writer = csv.DictWriter(csv_row, fieldnames=["English title"])
    writer.writeheader()
    writer.writerow({"English title": row["English title"]})
    csv_text = csv_row.getvalue()
    assert "&hellip;" not in csv_text
    assert "&copy;" not in csv_text
    assert "A … © title..." in csv_text

    html = build_html_review(
        [
            {
                "idx": 1,
                "identity_key": candidate.data["identity_key"],
                "title": candidate.data["title"],
                "authors": "Author",
                "editor_row": row,
                "english_title": candidate.data["english_title"],
                "english_title_type": candidate.data["english_title_type"],
                "english_title_sources": [],
            }
        ],
        {"total_count": 1},
    )
    assert "&hellip;" not in html
    assert "&copy;" not in html
    assert "title..." in html


def test_author_display_marking_preserves_helper_text() -> None:
    original = {"primary_authors": ["M. Alain de BENOIST"], "english_author_display": "M. Alain de BENOIST"}
    transliterated = {"primary_authors": ["M. Alain de BENOIST"], "english_author_display": "Ален де Бенуа"}
    assert format_editor_english_author_display(original) == "M. Alain de BENOIST (original)"
    assert format_editor_english_author_display(transliterated) == "Ален де Бенуа (transliteration)"


def test_editor_marking_and_published_source_link_are_visible() -> None:
    candidate = ScoutingCandidate.from_mapping(_sourced())
    row = candidate.editor_row()
    assert row["English title"].endswith("(published title)")
    html = build_html_review(
        [
            {
                "idx": 1,
                "identity_key": candidate.data["identity_key"],
                "title": candidate.data["title"],
                "authors": "Philippa Perry",
                "primary_authors": ["Philippa Perry"],
                "english_author_display": "Philippa Perry",
                "editor_row": row,
                "english_title": candidate.data["english_title"],
                "english_title_type": candidate.data["english_title_type"],
                "english_title_provenance": candidate.data["english_title_provenance"],
                "english_title_sources": ["https://publisher.example/books/parents"],
            }
        ],
        {"total_count": 1},
    )
    assert "published title" in html
    assert "Philippa Perry (original)" in html
    assert "https://publisher.example/books/parents" in html
