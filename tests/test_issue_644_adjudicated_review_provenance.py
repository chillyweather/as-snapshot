"""Offline coverage for issue #644: adjudicated descriptions in the review.

An adjudicated description is a stage outcome, not a separate status: two
owners keep the existing two-source status, one owner keeps the existing
single-source status. What the review must newly show is the provenance -
which sources those owners were - in Russian, with no implementation
terminology. A failing grounded stage record from the same run must not
mask a durable adjudicated description (case B). A retained identity note
surfaces as one closed Russian sentence. Every test is offline.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from state_store_support import seed_state

from autoscout.content_description import DESCRIPTION_ADJUDICATED_PROVENANCE
from autoscout.content_description import DESCRIPTION_ATTEMPTED_STATUS
from autoscout.content_description import DESCRIPTION_GROUNDED_STATUS
from autoscout.content_description import DESCRIPTION_HOST_GROUNDED_PROVENANCE
from autoscout.content_description import DESCRIPTION_SINGLE_SOURCE_STATUS
from autoscout.content_description import description_provenance_counts
from autoscout.description_adjudication import ADJUDICATION_PROVENANCE
from autoscout.description_adjudication import plan_adjudication_application
from autoscout.grounded_description import OUTCOME_ATTEMPTED_NO_DESCRIPTION
from autoscout.grounded_description import OUTCOME_GROUNDED
from autoscout.grounded_description import OUTCOME_SINGLE_SOURCE
from autoscout.grounded_description import REASON_FEWER_THAN_TWO
from autoscout.json_io import write_json
from autoscout.review_presentation import DESCRIPTION_IDENTITY_NOTE_TEXT
from autoscout.review_presentation import description_provenance_label
from autoscout.review_rendering import _render_neutral_row
from autoscout.review_rendering import render_review
from autoscout.russian_workflow import completion_message
from autoscout.weekly_report_row import OPTIONAL_EDITOR_FIELDNAMES
from autoscout.weekly_report_row import editor_csv_fieldnames

IDENTITY_KEY = "city book|alex author"
CANDIDATE_ID = "candidate-001"
VALID_TEXT = (
    "Книга о том, как города меняют поведение людей и повседневные привычки. "
    "Автор разбирает улицы, дворы и площади как скрытые сценарии встреч. "
    "Редакционный крючок — взгляд на город как на соавтора человеческих решений."
)

HOST_GROUNDED_LABEL = "Источники из проверенного списка"
ADJUDICATED_LABEL = "Источники проверены по тексту страниц"

FORBIDDEN_TERMS = (
    "agent",
    "adjudication",
    "adjudicated",
    "agent-adjudicated",
    "allowlist",
    "allow-list",
    "fewer_than_two",
    "attempted_no_description",
    "stage_outcome",
    "grounded-description-stage",
)


def _history_record(
    provenance: str = ADJUDICATION_PROVENANCE,
    *,
    owner_count: int = 2,
    grounded_outcome: str = OUTCOME_GROUNDED,
    identity_note_present: bool = False,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "candidate_id": CANDIDATE_ID,
        "identity_key": IDENTITY_KEY,
        "title": "City Book",
        "primary_authors": ["Alex Author"],
        "content_description": VALID_TEXT,
        "description_provenance": {
            "identity_key": IDENTITY_KEY,
            "content_description": VALID_TEXT,
            "provenance": provenance,
            "applied_date": "2026-09-14",
            "evidence_basis": ["https://publisher.example.test/city-book"],
            "candidate_id": CANDIDATE_ID,
            "owner_count": owner_count,
            "confidence": "high",
        },
        "grounded_outcome": grounded_outcome,
    }
    if identity_note_present:
        provenance_record = record["description_provenance"]
        assert isinstance(provenance_record, dict)
        provenance_record["identity_note_present"] = True
    return record


def _candidate() -> dict[str, Any]:
    return {"identity_key": IDENTITY_KEY, "title": "City Book", "primary_authors": ["Alex Author"]}


def _failing_grounded_record() -> dict[str, Any]:
    return {
        "candidate_id": CANDIDATE_ID,
        "identity_key": IDENTITY_KEY,
        "outcome": OUTCOME_ATTEMPTED_NO_DESCRIPTION,
        "reason_code": REASON_FEWER_THAN_TWO,
        "content_description": "",
    }


def test_case_b_failing_grounded_record_does_not_mask_adjudicated_description() -> None:
    """The test that proves the ticket: same run, grounded failed, adjudication repaired."""
    rendered = _render_neutral_row(
        _candidate(),
        {},
        _history_record(),
        grounded_stage_record=_failing_grounded_record(),
    )

    assert rendered["description"] == VALID_TEXT
    assert rendered["description_status"] == DESCRIPTION_GROUNDED_STATUS
    assert rendered["description_stage_outcome"] == OUTCOME_GROUNDED
    assert rendered["description_stage_reason"] == ""
    assert rendered["editor_row"]["Description reason"] == ""


def test_case_b_without_live_record_keeps_existing_status() -> None:
    rendered = _render_neutral_row(_candidate(), {}, _history_record())

    assert rendered["description"] == VALID_TEXT
    assert rendered["description_status"] == DESCRIPTION_GROUNDED_STATUS


def test_host_grounded_row_shows_allowlist_provenance_with_unchanged_status() -> None:
    rendered = _render_neutral_row(
        _candidate(),
        {},
        _history_record(provenance="grounded", owner_count=2, grounded_outcome=OUTCOME_GROUNDED),
    )

    assert rendered["description_status"] == DESCRIPTION_GROUNDED_STATUS
    assert rendered["description_provenance_label"] == HOST_GROUNDED_LABEL


def test_adjudicated_two_source_row_shows_adjudicated_provenance() -> None:
    rendered = _render_neutral_row(_candidate(), {}, _history_record())

    assert rendered["description_status"] == DESCRIPTION_GROUNDED_STATUS
    assert rendered["description_provenance_label"] == ADJUDICATED_LABEL


def test_adjudicated_single_source_row_shows_adjudicated_provenance() -> None:
    rendered = _render_neutral_row(
        _candidate(),
        {},
        _history_record(owner_count=1, grounded_outcome=OUTCOME_SINGLE_SOURCE),
    )

    assert rendered["description_status"] == DESCRIPTION_SINGLE_SOURCE_STATUS
    assert rendered["description_provenance_label"] == ADJUDICATED_LABEL


def test_succeeded_grounded_record_still_wins_over_adjudicated_history() -> None:
    live_record = {
        "candidate_id": CANDIDATE_ID,
        "identity_key": IDENTITY_KEY,
        "outcome": OUTCOME_GROUNDED,
        "reason_code": "",
        "content_description": "Книга о реках, мостах и ночных поездах.",
        "description_provenance": {"provenance": "grounded"},
    }
    rendered = _render_neutral_row(_candidate(), {}, _history_record(), grounded_stage_record=live_record)

    assert rendered["description"] == "Книга о реках, мостах и ночных поездах."
    assert rendered["description_status"] == DESCRIPTION_GROUNDED_STATUS
    assert rendered["description_provenance_label"] == HOST_GROUNDED_LABEL


def test_failing_grounded_record_still_wins_without_adjudicated_history() -> None:
    record = _history_record(provenance="grounded", owner_count=2, grounded_outcome=OUTCOME_GROUNDED)
    record["content_description"] = "Книга о реках, мостах и ночных поездах."
    rendered = _render_neutral_row(_candidate(), {}, record, grounded_stage_record=_failing_grounded_record())

    assert rendered["description_status"] == DESCRIPTION_ATTEMPTED_STATUS


def test_provenance_labels_name_no_machinery() -> None:
    assert description_provenance_label("grounded") == HOST_GROUNDED_LABEL
    assert description_provenance_label(ADJUDICATION_PROVENANCE) == ADJUDICATED_LABEL
    assert description_provenance_label({"provenance": ADJUDICATION_PROVENANCE}) == ADJUDICATED_LABEL
    assert description_provenance_label("") == ""
    assert description_provenance_label("backfilled") == ""
    assert description_provenance_label("internal_stage_name") == ""
    for label in (HOST_GROUNDED_LABEL, ADJUDICATED_LABEL, DESCRIPTION_IDENTITY_NOTE_TEXT):
        assert re.search(r"[А-Яа-яЁё]", label)
        assert not re.search(r"[A-Za-z]", label)


def _verification(counting_notes: list[str], dropped_notes: list[str] | None = None) -> dict[str, Any]:
    surviving = [
        {
            "url": f"https://example.test/page-{index}",
            "host": "example.test",
            "owner": f"owner-{index}",
            "owner_from_policy": True,
            "evidence_digest": f"digest-{index}",
            "describes_this_work": True,
            "identity_note": note,
            "counts_toward_independence": True,
        }
        for index, note in enumerate(counting_notes)
    ]
    for index, note in enumerate(dropped_notes or []):
        surviving.append(
            {
                "url": f"https://example.test/dropped-{index}",
                "host": "example.test",
                "owner": f"dropped-owner-{index}",
                "owner_from_policy": True,
                "evidence_digest": f"dropped-digest-{index}",
                "describes_this_work": False,
                "identity_note": note,
                "counts_toward_independence": False,
            }
        )
    return {
        "resolved": True,
        "confidence": "high",
        "surviving": surviving,
        "dropped": [],
        "counting_urls": [entry["url"] for entry in surviving if entry["counts_toward_independence"]],
        "counting_digests": [entry["evidence_digest"] for entry in surviving if entry["counts_toward_independence"]],
        "owner_count": sum(1 for entry in surviving if entry["counts_toward_independence"]),
    }


def _response() -> dict[str, Any]:
    return {
        "identity_key": IDENTITY_KEY,
        "resolved": True,
        "confidence": "high",
        "content_description": VALID_TEXT,
    }


def test_identity_note_on_counting_citation_reaches_artifact() -> None:
    plan = plan_adjudication_application(
        _response(),
        _verification(["", "page is about a different edition"], []),
        identity_matched=True,
        candidate_id=CANDIDATE_ID,
    )

    assert plan["decision"] == "apply_two_source"
    assert plan["artifact"]["identity_note_present"] is True


def test_identity_note_only_on_dropped_citation_does_not_reach_artifact() -> None:
    plan = plan_adjudication_application(
        _response(),
        _verification(["", ""], ["page is about a different edition"]),
        identity_matched=True,
        candidate_id=CANDIDATE_ID,
    )

    assert plan["decision"] == "apply_two_source"
    assert "identity_note_present" not in plan["artifact"]


def test_row_with_identity_note_shows_closed_russian_sentence() -> None:
    rendered = _render_neutral_row(_candidate(), {}, _history_record(identity_note_present=True))

    assert rendered["description_identity_note"] == DESCRIPTION_IDENTITY_NOTE_TEXT
    assert rendered["editor_row"]["Description provenance"].startswith(ADJUDICATED_LABEL)
    assert DESCRIPTION_IDENTITY_NOTE_TEXT in rendered["editor_row"]["Description provenance"]


def test_row_without_identity_note_shows_no_note_line() -> None:
    rendered = _render_neutral_row(_candidate(), {}, _history_record())

    assert rendered["description_identity_note"] == ""
    assert rendered["editor_row"]["Description provenance"] == ADJUDICATED_LABEL


def test_csv_header_gains_optional_provenance_field_only_when_used() -> None:
    assert "Description provenance" in OPTIONAL_EDITOR_FIELDNAMES
    assert "Description provenance" not in editor_csv_fieldnames([{"Title": "Plain"}])
    assert "Description provenance" in editor_csv_fieldnames([{"Title": "Plain", "Description provenance": "x"}])


def _write_review_state(tmp_path: Path, record: dict[str, Any]) -> None:
    candidate = {
        "identity_key": IDENTITY_KEY,
        "title": "City Book",
        "primary_authors": ["Alex Author"],
        "update_delta": "new",
        "priority_label": "A",
        "independent_signal_count": 2,
        "content_description": VALID_TEXT,
        "source_evidence": [{"source_name": "Source", "source_url": "https://example.test/book"}],
    }
    write_json(tmp_path / "scouting-candidates.json", [candidate])
    seed_state(tmp_path, candidate_history=[record])
    write_json(tmp_path / "run-summary.json", {"run_date": "2026-09-14", "editor_facing_count": 1})


def test_markdown_and_html_reviews_show_provenance() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _write_review_state(tmp_path, _history_record(identity_note_present=True))

        assert render_review(tmp_path) == 0
        markdown = (tmp_path / "weekly-review.md").read_text(encoding="utf-8")
        html = (tmp_path / "weekly-review.html").read_text(encoding="utf-8")

    assert "Источники описания" in markdown
    assert ADJUDICATED_LABEL in markdown
    assert DESCRIPTION_IDENTITY_NOTE_TEXT in markdown
    assert "Источники описания" in html
    assert ADJUDICATED_LABEL in html
    assert DESCRIPTION_IDENTITY_NOTE_TEXT in html


def test_html_summary_gains_no_description_field() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _write_review_state(tmp_path, _history_record())

        assert render_review(tmp_path) == 0
        html = (tmp_path / "weekly-review.html").read_text(encoding="utf-8")

    summaries = re.findall(r"<summary>.*?</summary>", html, flags=re.DOTALL)
    assert summaries
    for summary in summaries:
        assert ADJUDICATED_LABEL not in summary
        assert DESCRIPTION_GROUNDED_STATUS not in summary


def test_rendered_reviews_name_no_machinery() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _write_review_state(tmp_path, _history_record())

        assert render_review(tmp_path) == 0
        markdown = (tmp_path / "weekly-review.md").read_text(encoding="utf-8")
        html = (tmp_path / "weekly-review.html").read_text(encoding="utf-8")

    for review in (markdown, html):
        lowered = review.lower()
        for term in FORBIDDEN_TERMS:
            assert term not in lowered, f"implementation term {term!r} reached editor output"


def test_russian_summary_reports_counts_by_provenance() -> None:
    msg = completion_message(
        outcome="completed/no-attention",
        new_count=1,
        updated_count=0,
        attention_required=False,
        description_coverage={"validated_count": 4, "fallback_count": 0, "total_candidates": 4},
        grounded_description_counts={
            "grounded": 3,
            "single_source": 1,
            "attempted_no_description": 0,
            "not_attempted": 0,
        },
        description_provenance_counts={"grounded": 3, "agent-adjudicated": 1},
    )

    assert "Описаний на русском: 4 из 4." in msg
    assert "подтверждено несколькими источниками - 3" in msg
    assert "источники из проверенного списка - 3" in msg
    assert "источники проверены по тексту страниц - 1" in msg


def test_russian_summary_without_adjudicated_descriptions_reads_as_before() -> None:
    kwargs: dict[str, Any] = {
        "outcome": "completed/no-attention",
        "new_count": 1,
        "updated_count": 0,
        "attention_required": False,
        "description_coverage": {"validated_count": 4, "fallback_count": 0, "total_candidates": 4},
        "grounded_description_counts": {
            "grounded": 4,
            "single_source": 0,
            "attempted_no_description": 0,
            "not_attempted": 0,
        },
    }
    before = completion_message(**kwargs)
    after = completion_message(**kwargs, description_provenance_counts={"grounded": 4, "agent-adjudicated": 0})

    assert after == before
    assert "Происхождение описаний" not in after


def test_provenance_counts_cover_host_grounded_and_adjudicated() -> None:
    records = [
        _history_record(provenance="grounded", owner_count=2, grounded_outcome=OUTCOME_GROUNDED),
        _history_record(provenance="grounded", owner_count=1, grounded_outcome=OUTCOME_SINGLE_SOURCE),
        _history_record(),
        {
            "candidate_id": "candidate-009",
            "identity_key": "fallback|author",
            "content_description": "Описание требует дополнительной проверки",
            "description_provenance": {"provenance": "fallback"},
        },
    ]

    assert description_provenance_counts(records) == {"grounded": 2, "agent-adjudicated": 1}


def test_repeated_provenance_values_match_their_owners() -> None:
    """The repeated provenance values must not drift from their owners.

    ``content_description`` cannot import either stage module, because the
    imports run the other way. The values are therefore repeated, and this
    pins them equal.
    """
    assert DESCRIPTION_ADJUDICATED_PROVENANCE == ADJUDICATION_PROVENANCE
    assert DESCRIPTION_HOST_GROUNDED_PROVENANCE == OUTCOME_GROUNDED
    assert set(description_provenance_counts([])) == {
        DESCRIPTION_HOST_GROUNDED_PROVENANCE,
        DESCRIPTION_ADJUDICATED_PROVENANCE,
    }


def _table_cell_counts(markdown: str) -> list[tuple[str, int]]:
    """Return the cell count of every line of the candidate table.

    A GFM table row is split on unescaped pipes, with the leading and the
    trailing pipe dropped. The header, the delimiter row, and every body
    row must agree, or the table is not a table.
    """
    counted: list[tuple[str, int]] = []
    started = False
    for line in markdown.splitlines():
        if line.startswith("| # | ID | Название |"):
            started = True
        elif started and not line.startswith("|"):
            break
        if not started:
            continue
        stripped = line.strip()
        body = stripped[1:-1] if stripped.startswith("|") and stripped.endswith("|") else stripped
        counted.append((stripped, len(body.split("|"))))
    return counted


def _assert_table_is_a_table(markdown: str) -> None:
    counted = _table_cell_counts(markdown)
    assert counted, "the candidate table was not rendered"
    header_line, header_cells = counted[0]
    delimiter_line, delimiter_cells = counted[1]
    # GFM: "The header row must match the delimiter row in the number of
    # cells. If not, a table will not be recognized." A mismatch renders
    # the whole candidate table as raw pipe text (issue #644 review).
    assert delimiter_cells == header_cells, (
        f"delimiter row has {delimiter_cells} cells, header has {header_cells}\n"
        f"header:    {header_line}\ndelimiter: {delimiter_line}"
    )
    assert set(delimiter_line) <= set("|-"), delimiter_line
    for line, cells in counted[2:]:
        assert cells == header_cells, f"body row has {cells} cells, header has {header_cells}\n{line}"


def test_markdown_table_stays_a_table_without_the_provenance_column() -> None:
    """A week with no adjudicated description still renders a real table.

    The header and the delimiter row were two hand-aligned literals.
    Splitting them to insert one column left the delimiter one cell wider
    than the header, in both states, and nothing pinned the shape: all the
    other tests here check content (issue #644 review).
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        record = _history_record()
        # No usable description, so no row carries a provenance line and the
        # optional column stays hidden.
        record["content_description"] = ""
        record.pop("description_provenance", None)
        _write_review_state(tmp_path, record)

        assert render_review(tmp_path) == 0
        markdown = (tmp_path / "weekly-review.md").read_text(encoding="utf-8")

    assert "Источники описания" not in markdown
    _assert_table_is_a_table(markdown)


def test_markdown_table_stays_a_table_with_the_provenance_column() -> None:
    """The optional column widens the header and the delimiter row together."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _write_review_state(tmp_path, _history_record(identity_note_present=True))

        assert render_review(tmp_path) == 0
        markdown = (tmp_path / "weekly-review.md").read_text(encoding="utf-8")

    assert "Источники описания" in markdown
    _assert_table_is_a_table(markdown)
