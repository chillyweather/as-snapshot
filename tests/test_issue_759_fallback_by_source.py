"""Offline coverage for issue #759: fallback descriptions named by source.

One total hid a source that lost most of its books at the description stage.
The coverage record now names each source whose candidates remain on the
fallback, with how many and why.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.content_description import description_coverage
from autoscout.run_health import run_health

VALID_TEXT = (
    "Книга о памяти, обществе и общественной жизни маленького города. "
    "Автор соединяет репортаж с историческим контекстом и показывает, "
    "как прошлое формирует настоящее."
)
BOKLISTA = "Bokhandlerforeningen Boklista - Generell litteratur voksne"
NONFICTION_FR = "Nonfiction.fr Critiques"
SPIEGEL = "Spiegel Bestseller Sachbuch"


def _candidate(key: str, text: str, *sources: str, reason: str = "") -> dict[str, object]:
    record: dict[str, object] = {
        "identity_key": key,
        "content_description": text,
        "source_evidence": [{"source_name": source} for source in sources],
    }
    if reason:
        record["content_description_rejection_reason"] = reason
    return record


def test_fallbacks_are_counted_under_their_source() -> None:
    coverage = description_coverage(
        [
            _candidate("a", DESCRIPTION_FALLBACK, BOKLISTA, reason="title_mismatch"),
            _candidate("b", DESCRIPTION_FALLBACK, BOKLISTA, reason="title_mismatch"),
            _candidate("c", DESCRIPTION_FALLBACK, BOKLISTA, reason="no_results"),
            _candidate("d", VALID_TEXT, BOKLISTA),
            _candidate("e", DESCRIPTION_FALLBACK, NONFICTION_FR, reason="no_results"),
            _candidate("f", VALID_TEXT, SPIEGEL),
        ]
    )

    assert coverage["fallback_count"] == 4
    assert coverage["fallback_by_source"] == [
        {
            "source_name": BOKLISTA,
            "candidate_count": 4,
            "fallback_count": 3,
            "reason_counts": {"no_results": 1, "title_mismatch": 2},
        },
        {
            "source_name": NONFICTION_FR,
            "candidate_count": 1,
            "fallback_count": 1,
            "reason_counts": {"no_results": 1},
        },
    ]


def test_a_candidate_in_two_sources_counts_once_under_each() -> None:
    coverage = description_coverage([_candidate("a", DESCRIPTION_FALLBACK, SPIEGEL, BOKLISTA, SPIEGEL)])

    assert coverage["fallback_count"] == 1
    assert [(item["source_name"], item["fallback_count"]) for item in coverage["fallback_by_source"]] == [
        (BOKLISTA, 1),
        (SPIEGEL, 1),
    ]


def test_an_invalid_description_is_counted_with_its_validation_reason() -> None:
    coverage = description_coverage([_candidate("a", "Too short.", BOKLISTA)])

    assert coverage["fallback_count"] == 1
    (item,) = coverage["fallback_by_source"]
    assert item["source_name"] == BOKLISTA
    assert item["reason_counts"] == coverage["invalid_reason_counts"]


def test_a_fallback_without_a_reason_is_still_counted() -> None:
    coverage = description_coverage([_candidate("a", DESCRIPTION_FALLBACK, BOKLISTA)])

    assert coverage["fallback_by_source"] == [
        {"source_name": BOKLISTA, "candidate_count": 1, "fallback_count": 1, "reason_counts": {}}
    ]


@pytest.mark.parametrize(
    "candidates",
    [
        [],
        [_candidate("a", VALID_TEXT, BOKLISTA)],
        # A fallback that no source evidence names cannot be attributed.
        [{"identity_key": "a", "content_description": DESCRIPTION_FALLBACK}],
        [{"identity_key": "a", "content_description": DESCRIPTION_FALLBACK, "source_evidence": "malformed"}],
        [{"identity_key": "a", "content_description": DESCRIPTION_FALLBACK, "source_evidence": [None, {}]}],
    ],
)
def test_no_source_is_named_without_an_attributable_fallback(candidates: list[dict[str, object]]) -> None:
    assert description_coverage(candidates)["fallback_by_source"] == []


def _write_health_inputs(output_dir: Path, coverage: dict[str, object]) -> None:
    run_summary = {"run_date": "2026-10-05", "operation_id": "op-1", "editor_facing_count": 2}
    (output_dir / "run-summary.json").write_text(json.dumps(run_summary), encoding="utf-8")
    (output_dir / "source-runs.json").write_text("[]", encoding="utf-8")
    (output_dir / "pilot-status.json").write_text(
        json.dumps({"ready_for_editor": True, "editor_facing_count": 2, "run_date": "2026-10-05"}),
        encoding="utf-8",
    )
    (output_dir / "description-coverage.json").write_text(json.dumps(coverage), encoding="utf-8")


def test_health_names_each_source_with_fallbacks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    coverage = description_coverage(
        [
            _candidate("a", DESCRIPTION_FALLBACK, BOKLISTA, reason="title_mismatch"),
            _candidate("b", VALID_TEXT, BOKLISTA),
            _candidate("c", VALID_TEXT, SPIEGEL),
        ]
    )
    _write_health_inputs(tmp_path, coverage)

    run_health(tmp_path, effective_date="2026-10-05")

    out = capsys.readouterr().out
    assert f"description_fallback_source={BOKLISTA}: 1 of 2" in out
    assert SPIEGEL not in out


def test_health_reads_a_coverage_record_written_before_this_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_health_inputs(tmp_path, {"coverage": "partial", "validated_count": 1, "fallback_count": 1})

    run_health(tmp_path, effective_date="2026-10-05")

    out = capsys.readouterr().out
    assert "description_fallback_count=1" in out
    assert "description_fallback_source" not in out


def test_a_review_row_carries_every_citing_source() -> None:
    """The coverage record is built from review rows, which showed only the primary source."""
    from autoscout.review_rendering import _render_neutral_row

    candidate = {**_candidate("k|a", DESCRIPTION_FALLBACK, BOKLISTA, SPIEGEL), "title": "Title", "update_delta": "new"}
    editor_row = {"Title": "Title", "Author": "Author", "Candidate ID": "k|a", "Update delta": "new"}

    row = _render_neutral_row(candidate, editor_row)

    assert row["source_names"] == [BOKLISTA, SPIEGEL]
