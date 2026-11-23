"""English-title backfill worklist and apply guards (Issue #645).

A contract change reaches the next extraction only. The existing rows stay
empty without a backfill, which reuses the description-backfill shape: a
metadata-only worklist, a preview, then an apply behind an explicit
confirmation. An English title is non-authoritative helper metadata: it must
never overwrite an authoritative original title or author, and it must never
overwrite an editor-corrected value.
"""

from __future__ import annotations

from typing import Any

from autoscout.english_title_backfill import apply_english_title_backfill
from autoscout.english_title_backfill import prepare_english_title_backfill


def _row(
    identity: str,
    title: str,
    authors: list[str],
    english_title: str = "",
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "identity_key": identity,
        "title": title,
        "primary_authors": list(authors),
        "english_title": english_title,
        "source_evidence": [{"source_name": "Spiegel Bestseller Sachbuch", "english_title": english_title or None}],
    }
    if extra:
        row.update(extra)
    return row


def test_prepare_selects_only_blank_rows() -> None:
    history = [
        _row("blank|a", "Die Kraft der Demokratie", ["Hedwig Richter"]),
        _row("filled|b", "Der Atlas der KI", ["Kate Crawford"], "The Atlas of AI"),
    ]
    items = prepare_english_title_backfill(history, editor_titles={"Die Kraft der Demokratie"})
    assert [item["identity_key"] for item in items] == ["blank|a"]
    assert items[0]["title"] == "Die Kraft der Demokratie"
    assert items[0]["editor_visible"] is True


def test_prepare_marks_visibility_and_never_carries_helper_values() -> None:
    history = [_row("blank|a", "Die Kraft der Demokratie", ["Hedwig Richter"])]
    items = prepare_english_title_backfill(history, editor_titles=set())
    assert items[0]["editor_visible"] is False
    assert "english_title" not in items[0]


def test_prepare_skips_english_only_rows() -> None:
    history = [_row("blank|a", "Some English Book", ["Alex Example"])]
    assert prepare_english_title_backfill(history, source_languages={"Spiegel Bestseller Sachbuch": "en"}) == []


def test_prepare_keeps_mixed_and_unknown_language_rows() -> None:
    mixed = _row("mixed|m", "Die Kraft der Demokratie", ["Hedwig Richter"])
    mixed["source_evidence"] = [
        {"source_name": "Spiegel Bestseller Sachbuch", "english_title": None},
        {"source_name": "Some English List", "english_title": None},
    ]
    unknown = _row("unknown|u", "Die Kraft der Demokratie", ["Hedwig Richter"])
    unknown["source_evidence"] = [{"source_name": "Retired Source", "english_title": None}]
    items = prepare_english_title_backfill(
        [mixed, unknown],
        source_languages={"Spiegel Bestseller Sachbuch": "de", "Some English List": "en"},
    )
    assert {item["identity_key"] for item in items} == {"mixed|m", "unknown|u"}


def test_apply_fills_a_blank_row_with_typed_helpers() -> None:
    history = [_row("blank|a", "Die Kraft der Demokratie", ["Hedwig Richter"])]
    artifacts = [{"identity_key": "blank|a", "english_title": "The Power of Democracy"}]
    updated, report = apply_english_title_backfill(history, artifacts)
    assert report["applied"] == 1
    row = next(item for item in updated if item["identity_key"] == "blank|a")
    assert row["english_title"] == "The Power of Democracy"
    assert row["english_title_type"] == "generated_rendering"
    assert row["english_title_provenance"]["kind"] == "generated_rendering"
    assert row["english_title_provenance"]["mapping"]["original_title"] == "Die Kraft der Demokratie"


def test_apply_never_touches_authoritative_title_or_authors() -> None:
    history = [_row("blank|a", "Die Kraft der Demokratie", ["Hedwig Richter"])]
    before = {key: history[0][key] for key in ("title", "primary_authors", "identity_key")}
    artifacts = [
        {
            "identity_key": "blank|a",
            "title": "MALICIOUS REPLACEMENT",
            "primary_authors": ["Someone Else"],
            "english_title": "The Power of Democracy",
        }
    ]
    updated, report = apply_english_title_backfill(history, artifacts)
    assert report["applied"] == 1
    row = next(item for item in updated if item["identity_key"] == "blank|a")
    assert {key: row[key] for key in ("title", "primary_authors", "identity_key")} == before


def test_apply_skips_nonblank_helpers_without_overwrite_flag() -> None:
    history = [_row("filled|b", "Der Atlas der KI", ["Kate Crawford"], "The Atlas of AI")]
    before = dict(history[0])
    artifacts = [{"identity_key": "filled|b", "english_title": "A Different Title"}]
    updated, report = apply_english_title_backfill(history, artifacts)
    assert report["applied"] == 0
    assert report["skipped_existing"] == 1
    assert next(item for item in updated if item["identity_key"] == "filled|b") == before


def test_apply_skips_editor_touched_rows_byte_for_byte() -> None:
    """An editor-seen row with a helper is never overwritten (Issue #645)."""
    history = [
        _row(
            "seen|d",
            "Der Atlas der KI",
            ["Kate Crawford"],
            "The Atlas of AI",
            extra={"editor_status": "interested", "editor_note": "Promising for business list."},
        )
    ]
    before = dict(history[0])
    artifacts = [{"identity_key": "seen|d", "english_title": "A Different Title"}]
    updated, report = apply_english_title_backfill(history, artifacts, allow_overwrite_valid=True)
    assert report["applied"] == 0
    assert report["skipped_existing"] == 1
    assert next(item for item in updated if item["identity_key"] == "seen|d") == before


def test_apply_skips_sourced_evidence_even_with_overwrite_flag() -> None:
    history = [
        _row(
            "sourced|c",
            "Tyll",
            ["Daniel Kehlmann"],
            "Tyll",
            extra={
                "english_title_type": "sourced_published",
                "english_title_provenance": {
                    "kind": "sourced_published",
                    "research_sources": ["https://example.test/tyll"],
                    "mapping": {
                        "original_title": "Tyll",
                        "original_authors": ["Daniel Kehlmann"],
                        "published_title": "Tyll",
                        "published_authors": ["Daniel Kehlmann"],
                    },
                },
            },
        )
    ]
    before = dict(history[0])
    artifacts = [{"identity_key": "sourced|c", "english_title": "Tyll: A Novel"}]
    updated, report = apply_english_title_backfill(history, artifacts, allow_overwrite_valid=True)
    assert report["applied"] == 0
    assert report["skipped_evidence"] == 1
    assert next(item for item in updated if item["identity_key"] == "sourced|c") == before


def test_apply_rejects_blank_and_placeholder_helpers() -> None:
    history = [_row("blank|a", "Tyll", ["Daniel Kehlmann"])]
    artifacts = [
        {"identity_key": "blank|a", "english_title": "  "},
        {"identity_key": "missing|z", "english_title": "N/A"},
    ]
    updated, report = apply_english_title_backfill(history, artifacts)
    assert report["applied"] == 0
    assert report["invalid"] == 1
    assert report["unknown_identity"] == 1
    assert updated == history


def test_apply_is_idempotent_byte_for_byte() -> None:
    history = [_row("blank|a", "Die Kraft der Demokratie", ["Hedwig Richter"])]
    artifacts = [{"identity_key": "blank|a", "english_title": "The Power of Democracy"}]
    once, first = apply_english_title_backfill(history, artifacts)
    assert first["applied"] == 1
    twice, second = apply_english_title_backfill(once, artifacts)
    assert second["applied"] == 0
    assert twice == once


def test_apply_leaves_unrelated_rows_byte_identical() -> None:
    history = [
        _row("blank|a", "Die Kraft der Demokratie", ["Hedwig Richter"]),
        _row("filled|b", "Der Atlas der KI", ["Kate Crawford"], "The Atlas of AI"),
        {"identity_key": "odd|o", "note": "a row without a title keeps its shape"},
    ]
    artifacts = [{"identity_key": "blank|a", "english_title": "The Power of Democracy"}]
    updated, _ = apply_english_title_backfill(history, artifacts)
    assert [item for item in updated if item["identity_key"] != "blank|a"] == [
        item for item in history if item["identity_key"] != "blank|a"
    ]
