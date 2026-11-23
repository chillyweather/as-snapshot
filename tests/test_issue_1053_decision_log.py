"""Issue #1053: every confirmed editor decision is kept with its date.

Candidate History showed only the latest decision and never when it was made.
Each record now keeps ``editor_decisions``: one flat entry per confirmed
decision with its status, note, decision date and the digest of the
description the editor saw.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from state_store_support import state_databases

from autoscout import editor_decisions
from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.editor_decisions import DECISION_LOG_FIELD
from autoscout.editor_decisions import description_digest
from autoscout.editor_decisions import merge_decision_logs
from autoscout.editor_decisions import record_returned_decisions
from autoscout.editor_decisions import shown_description_digest
from autoscout.editor_feedback import record_feedback
from autoscout.identity_corrections import apply_identity_correction
from autoscout.weekly_report import RUN_HISTORY_COPY
from tests.state_store_support import read_state
from tests.state_store_support import seed_state

RUN_DATE = "2026-07-15"
DESCRIPTION = "Книга о том, как устроены города и почему люди в них остаются."
OTHER_DESCRIPTION = "Другое описание той же книги."
TITLES = ("City Book", "River Book", "Forest Book")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "autoscout", *args], check=False, capture_output=True, text=True)


def _tree(directory: Path) -> dict[str, bytes]:
    files = {
        str(path.relative_to(directory)): path.read_bytes() for path in sorted(directory.rglob("*")) if path.is_file()
    }
    return {**files, **state_databases()}


class Scenario:
    """A weekly run over three fixture books, with its own state directory."""

    def __init__(self, base: Path) -> None:
        self.config = base / "config.json"
        self.output_dir = base / "output"
        self.state_dir = base / "state"
        self.output_dir.mkdir(parents=True)
        self.state_dir.mkdir(parents=True)
        _write_json(
            self.config,
            {
                "run_date": RUN_DATE,
                "sources": [
                    {
                        "name": "TestAwards",
                        "family": "award",
                        "url": "https://example.test/awards",
                        "access_status": "open",
                        "extraction_method": "fixture",
                        "check_frequency": "weekly",
                        "language": "en",
                        "fixture_candidates": [
                            {
                                "title": title,
                                "primary_authors": ["Alex Author"],
                                "source_url": f"https://example.test/awards/{index}",
                                "signal_strength": "shortlist",
                                "evidence_note": "Shortlisted.",
                                "candidate_category": "popular science",
                            }
                            for index, title in enumerate(TITLES)
                        ],
                    }
                ],
            },
        )

    def run_mvp(self) -> None:
        result = _run(
            "run-mvp",
            "--config",
            str(self.config),
            "--output-dir",
            str(self.output_dir),
            "--state-dir",
            str(self.state_dir),
        )
        assert result.returncode == 0, result.stderr

    def history(self) -> list[dict[str, Any]]:
        return read_state(self.state_dir, "candidate_history")

    def record(self, title: str) -> dict[str, Any]:
        return next(record for record in self.history() if record["title"] == title)

    def edit_record(self, title: str, **fields: Any) -> None:
        history = self.history()
        for record in history:
            if record["title"] == title:
                record.update(fields)
        seed_state(self.state_dir, candidate_history=history)

    def confirm(self, title: str, status: str, note: str) -> None:
        result = _run(
            "record-feedback",
            "--identity-key",
            self.record(title)["identity_key"],
            "--status",
            status,
            "--note",
            note,
            "--state-dir",
            str(self.state_dir),
            "--confirmed-by-editor",
        )
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def scenario(tmp_path: Path) -> Scenario:
    built = Scenario(tmp_path)
    built.run_mvp()
    built.edit_record("City Book", content_description=DESCRIPTION)
    return built


def test_a_confirmed_decision_carries_its_date_and_the_description_digest(scenario: Scenario) -> None:
    scenario.confirm("City Book", "interested", "Берём на рассмотрение.")
    scenario.run_mvp()

    record = scenario.record("City Book")
    assert record["editor_status"] == "interested"
    assert record[DECISION_LOG_FIELD] == [
        {
            "status": "interested",
            "note": "Берём на рассмотрение.",
            "decision_date": date.today().isoformat(),
            "description_digest": _digest(DESCRIPTION),
        }
    ]
    # The archived row keeps the moment of confirmation too.
    (archived,) = read_state(scenario.state_dir, "archived_editor_rows")
    assert archived["Decision Date"] == date.today().isoformat()
    assert archived["Description Digest"] == _digest(DESCRIPTION)


def test_a_book_without_a_description_logs_an_empty_digest(scenario: Scenario) -> None:
    scenario.confirm("River Book", "reject", "Нет.")
    scenario.run_mvp()

    (entry,) = scenario.record("River Book")[DECISION_LOG_FIELD]
    assert entry["description_digest"] == ""


def test_changing_a_decision_keeps_the_earlier_one_with_its_own_date(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    class EarlierDate(date):
        @classmethod
        def today(cls) -> EarlierDate:
            return cls(2026, 11, 1)

    identity_key = scenario.record("City Book")["identity_key"]
    monkeypatch.setattr(editor_decisions, "date", EarlierDate)
    assert record_feedback(identity_key, "maybe_watch", "Подождём.", scenario.state_dir, confirmed_by_editor=True) == 0
    monkeypatch.undo()
    scenario.run_mvp()
    scenario.edit_record("City Book", content_description=OTHER_DESCRIPTION)
    scenario.confirm("City Book", "reject", "Нет, описание не убеждает.")
    scenario.run_mvp()

    record = scenario.record("City Book")
    assert (record["editor_status"], record["editor_note"]) == ("reject", "Нет, описание не убеждает.")
    assert record[DECISION_LOG_FIELD] == [
        {
            "status": "maybe_watch",
            "note": "Подождём.",
            "decision_date": "2026-11-01",
            "description_digest": _digest(DESCRIPTION),
        },
        {
            "status": "reject",
            "note": "Нет, описание не убеждает.",
            "decision_date": date.today().isoformat(),
            "description_digest": _digest(OTHER_DESCRIPTION),
        },
    ]


def test_two_decisions_consumed_by_one_run_are_both_kept(scenario: Scenario) -> None:
    scenario.confirm("City Book", "reject", "Нет.")
    scenario.confirm("City Book", "interested", "Передумала: берём.")
    scenario.run_mvp()

    record = scenario.record("City Book")
    assert record["editor_status"] == "interested"
    assert [(entry["status"], entry["note"]) for entry in record[DECISION_LOG_FIELD]] == [
        ("reject", "Нет."),
        ("interested", "Передумала: берём."),
    ]


def test_the_full_pipeline_path_logs_the_decision_like_finalization(scenario: Scenario, tmp_path: Path) -> None:
    scenario.confirm("City Book", "interested", "Берём.")
    # A fresh output directory makes run-mvp consume the row in the full pipeline.
    shutil.rmtree(scenario.output_dir)
    scenario.output_dir.mkdir()
    scenario.run_mvp()

    (entry,) = scenario.record("City Book")[DECISION_LOG_FIELD]
    assert entry == {
        "status": "interested",
        "note": "Берём.",
        "decision_date": date.today().isoformat(),
        "description_digest": _digest(DESCRIPTION),
    }
    (output_record,) = [
        record for record in _read_json(scenario.output_dir / RUN_HISTORY_COPY) if record["title"] == "City Book"
    ]
    assert output_record[DECISION_LOG_FIELD] == [entry]


def test_an_undated_decision_is_not_rewritten_by_an_unrelated_save_and_stays_undated(scenario: Scenario) -> None:
    scenario.edit_record("City Book", editor_status="reject", editor_note="Старое решение.")
    shutil.rmtree(scenario.output_dir)
    scenario.output_dir.mkdir()
    scenario.run_mvp()

    record = scenario.record("City Book")
    assert (record["editor_status"], record["editor_note"]) == ("reject", "Старое решение.")
    assert DECISION_LOG_FIELD not in record

    scenario.confirm("City Book", "interested", "Новое решение.")
    scenario.run_mvp()

    assert scenario.record("City Book")[DECISION_LOG_FIELD] == [
        {"status": "reject", "note": "Старое решение.", "decision_date": None, "description_digest": None},
        {
            "status": "interested",
            "note": "Новое решение.",
            "decision_date": date.today().isoformat(),
            "description_digest": _digest(DESCRIPTION),
        },
    ]


def test_a_feedback_preview_leaves_the_state_byte_identical(scenario: Scenario) -> None:
    before = _tree(scenario.state_dir)
    result = _run(
        "record-feedback",
        "--identity-key",
        scenario.record("City Book")["identity_key"],
        "--status",
        "reject",
        "--note",
        "Нет.",
        "--state-dir",
        str(scenario.state_dir),
    )
    assert result.returncode == 3
    assert _tree(scenario.state_dir) == before


def test_the_rendered_review_does_not_change_with_the_decision_log(scenario: Scenario, tmp_path: Path) -> None:
    scenario.confirm("City Book", "maybe_watch", "Подождём.")
    scenario.confirm("City Book", "interested", "Берём.")
    scenario.run_mvp()
    assert scenario.record("City Book")[DECISION_LOG_FIELD]

    without_log = tmp_path / "without-log"
    shutil.copytree(scenario.state_dir, without_log / "state")
    shutil.copytree(scenario.output_dir, without_log / "output")
    history = scenario.history()
    for record in history:
        record.pop(DECISION_LOG_FIELD, None)
    seed_state(without_log / "state", candidate_history=history)

    for state_dir, output_dir in (
        (scenario.state_dir, scenario.output_dir),
        (without_log / "state", without_log / "output"),
    ):
        result = _run(
            "render-review",
            "--output-dir",
            str(output_dir),
            "--state-dir",
            str(state_dir),
            "--config",
            str(scenario.config),
        )
        assert result.returncode == 0, result.stderr
    assert _tree(scenario.output_dir) == _tree(without_log / "output")


def test_consuming_the_same_row_twice_adds_one_entry() -> None:
    row = {"identity_key": "x", "Editor Status": "reject", "Editor Note": "Нет.", "Decision Date": "2026-11-14"}
    record: dict[str, Any] = {"identity_key": "x", "editor_status": "reject", "editor_note": "Нет."}

    record_returned_decisions(record, "", "", [row])
    record_returned_decisions(record, "reject", "Нет.", [row])

    assert record[DECISION_LOG_FIELD] == [
        {"status": "reject", "note": "Нет.", "decision_date": "2026-11-14", "description_digest": None}
    ]


def _row(status: str, note: str) -> dict[str, Any]:
    return {
        "identity_key": "x",
        "Editor Status": status,
        "Editor Note": note,
        "Decision Date": "2026-11-14",
        "Description Digest": "",
    }


def test_retrying_a_batch_of_two_decisions_adds_nothing() -> None:
    rows = [_row("reject", "Нет."), _row("interested", "Берём.")]
    record: dict[str, Any] = {"identity_key": "x", "editor_status": "interested", "editor_note": "Берём."}

    record_returned_decisions(record, "", "", rows)
    record_returned_decisions(record, "interested", "Берём.", rows)

    assert [entry["status"] for entry in record[DECISION_LOG_FIELD]] == ["reject", "interested"]


def test_a_merge_keeps_a_decision_repeated_on_the_same_day() -> None:
    record: dict[str, Any] = {"identity_key": "x", "editor_status": "reject", "editor_note": "Нет."}
    record_returned_decisions(
        record, "", "", [_row("reject", "Нет."), _row("interested", "Берём."), _row("reject", "Нет.")]
    )

    merged = merge_decision_logs([record])

    assert [entry["status"] for entry in merged] == ["reject", "interested", "reject"]


def test_a_retry_after_a_note_only_row_adds_nothing() -> None:
    note_only = {"identity_key": "x", "Editor Note": "Уточнить права.", "Decision Date": "2026-11-14"}
    rows = [note_only, _row("interested", "Берём.")]
    record: dict[str, Any] = {"identity_key": "x", "editor_status": "interested", "editor_note": "Берём."}

    record_returned_decisions(record, "reject", "Нет.", rows)
    # The retry starts from the already updated record.
    record_returned_decisions(record, "interested", "Берём.", rows)

    assert [(entry["status"], entry["note"]) for entry in record[DECISION_LOG_FIELD]] == [
        ("reject", "Нет."),
        ("reject", "Уточнить права."),
        ("interested", "Берём."),
    ]


def test_the_digest_is_of_the_description_the_review_shows() -> None:
    assert description_digest({"content_description": DESCRIPTION}) == _digest(DESCRIPTION)
    # The placeholder and a description the review rejects show nothing.
    assert description_digest({"content_description": DESCRIPTION_FALLBACK}) == ""
    assert description_digest({"content_description": "A book about cities and why people stay in them."}) == ""
    # A Candidate History placeholder wins over the latest candidate's text, as in the review.
    assert (
        shown_description_digest({"content_description": DESCRIPTION_FALLBACK}, {"content_description": DESCRIPTION})
        == ""
    )
    assert shown_description_digest({"content_description": ""}, {"content_description": DESCRIPTION}) == _digest(
        DESCRIPTION
    )


OWN_TIME = "in my own time|christopher lloyd"
GLAD = "glad i m still alive|christopher lloyd"


def _merge_history() -> list[dict[str, Any]]:
    return [
        {
            "candidate_id": "candidate-own-time",
            "identity_key": OWN_TIME,
            "title": "IN MY OWN TIME",
            "primary_authors": ["Christopher Lloyd"],
            "content_description": DESCRIPTION,
            "editor_status": "reject",
            "editor_note": "Нет.",
            DECISION_LOG_FIELD: [
                {"status": "reject", "note": "Нет.", "decision_date": "2026-11-03", "description_digest": "aaaa"}
            ],
        },
        {
            "candidate_id": "candidate-glad",
            "identity_key": GLAD,
            "title": "Glad I'm Still Alive",
            "primary_authors": ["Christopher Lloyd"],
            "content_description": DESCRIPTION,
            "editor_status": "needs_deeper_analysis",
            "editor_note": "Стоит оценить полный текст.",
        },
    ]


def _merge_correction() -> dict[str, Any]:
    return {
        "source_identity_keys": [OWN_TIME, GLAD],
        "canonical": {"identity_key": GLAD, "title": "Glad I'm Still Alive", "primary_authors": ["Christopher Lloyd"]},
        "editor_decision_resolution": {
            "canonical_source_identity_key": GLAD,
            "observed_editor_decisions": {
                OWN_TIME: {"editor_status": "reject", "editor_note": "Нет."},
                GLAD: {"editor_status": "needs_deeper_analysis", "editor_note": "Стоит оценить полный текст."},
            },
        },
    }


def test_a_merge_that_chooses_a_decision_logs_it_after_every_decision_it_replaces(tmp_path: Path) -> None:
    seed_state(tmp_path, candidate_history=_merge_history())

    apply_identity_correction(tmp_path, _merge_correction())

    (merged,) = read_state(tmp_path, "candidate_history")
    assert merged[DECISION_LOG_FIELD] == [
        {
            "status": "needs_deeper_analysis",
            "note": "Стоит оценить полный текст.",
            "decision_date": None,
            "description_digest": None,
        },
        {"status": "reject", "note": "Нет.", "decision_date": "2026-11-03", "description_digest": "aaaa"},
        {
            "status": "needs_deeper_analysis",
            "note": "Стоит оценить полный текст.",
            "decision_date": date.today().isoformat(),
            "description_digest": _digest(DESCRIPTION),
        },
    ]


def test_a_merge_dry_run_writes_nothing(tmp_path: Path) -> None:
    seed_state(tmp_path, candidate_history=_merge_history())
    before = _tree(tmp_path)

    apply_identity_correction(tmp_path, _merge_correction(), dry_run=True)

    assert _tree(tmp_path) == before
