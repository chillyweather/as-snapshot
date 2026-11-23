"""Issue #1153: a returned file of editor decisions is recorded in one confirmed batch.

The batch keeps the two-phase protocol of ``record-feedback``: a preview that
writes nothing, then a confirmed write of exactly what was previewed. It
leaves the same state as the same decisions recorded one by one, on either
State Store backend (this file runs on both in ``./scripts/check``).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from state_store_support import fail_database_commits
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_databases
from state_store_support import state_snapshot

from autoscout import editor_decisions
from autoscout.__main__ import main
from autoscout.editor_decisions import DECISION_LOG_FIELD
from autoscout.feedback_batch import record_feedback_batch
from autoscout.state_store import json_backend

RUN_DATE = "2026-07-15"
DESCRIPTION = "Книга о том, как устроены города и почему люди в них остаются."
TITLES = ("City Book", "River Book", "Forest Book", "Desert Book")
# Notes as the editor wrote them: quotes, inner and trailing whitespace, a line break.
NOTE_WITH_LINE_BREAK = '  Берём — «очень» "сильная" книга.\nОбсудить с коллегами.  '


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class Scenario:
    """A weekly run over four fixture books, with its own state directory."""

    def __init__(self, base: Path) -> None:
        self.base = base
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
        self.run_mvp()
        history = self.history()
        for record in history:
            if record["title"] == "City Book":
                record["content_description"] = DESCRIPTION
        seed_state(self.state_dir, candidate_history=history)

    def run_mvp(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "autoscout",
                "run-mvp",
                "--config",
                str(self.config),
                "--output-dir",
                str(self.output_dir),
                "--state-dir",
                str(self.state_dir),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def history(self) -> list[dict[str, Any]]:
        return read_state(self.state_dir, "candidate_history")

    def record(self, title: str) -> dict[str, Any]:
        return next(record for record in self.history() if record["title"] == title)

    def stored(self) -> dict[str, Any]:
        """Every canonical collection's stored bytes; the database's own checksum names its state directory, so it is left out."""
        return {name: value for name, value in state_snapshot(self.state_dir).items() if name != "state_database"}

    def decisions_file(self, decisions: list[dict[str, Any]], name: str = "decisions.json") -> Path:
        path = self.base / name
        _write_json(path, decisions)
        return path

    def batch(self, decisions_file: Path, *extra: str) -> int:
        return main(
            ["record-feedback-batch", "--decisions", str(decisions_file), "--state-dir", str(self.state_dir), *extra]
        )


def _decisions(scenario: Scenario) -> list[dict[str, Any]]:
    """A returned file: rows by identity key and by durable Candidate ID, a rights dimension, an empty note, one book decided twice."""
    return [
        {
            "identity_key": scenario.record("City Book")["identity_key"],
            "status": "interested",
            "note": NOTE_WITH_LINE_BREAK,
        },
        {"candidate_id": scenario.record("River Book")["candidate_id"], "status": "reject", "note": "Нет."},
        {
            "identity_key": scenario.record("Forest Book")["identity_key"],
            "status": "maybe watch",
            "note": "Права, кажется, свободны.",
            "rights": "available",
        },
        {"identity_key": scenario.record("Desert Book")["identity_key"], "status": "already_known", "note": ""},
        {"identity_key": scenario.record("River Book")["identity_key"], "status": "needs_deeper_analysis"},
    ]


def _preview_digest(output: str) -> str:
    (line,) = [line for line in output.splitlines() if line.startswith("batch_digest=")]
    return line.removeprefix("batch_digest=")


def _record_one_by_one(scenario: Scenario, decisions: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]) -> None:
    by_candidate_id = {record["candidate_id"]: record["identity_key"] for record in scenario.history()}
    for decision in decisions:
        identity_key = decision.get("identity_key") or by_candidate_id[decision["candidate_id"]]
        argv = ["record-feedback", "--identity-key", identity_key, "--status", decision["status"]]
        argv += ["--note", decision.get("note", "")]
        if "rights" in decision:
            argv += ["--rights", decision["rights"]]
        argv += ["--state-dir", str(scenario.state_dir)]
        assert main(argv) == 3, capsys.readouterr().out
        assert main([*argv, "--confirmed-by-editor"]) == 0, capsys.readouterr().out


def _record_as_batch(scenario: Scenario, decisions: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]) -> str:
    decisions_file = scenario.decisions_file(decisions)
    assert scenario.batch(decisions_file) == 3
    digest = _preview_digest(capsys.readouterr().out)
    assert scenario.batch(decisions_file, "--confirmed-by-editor", "--digest", digest) == 0
    return capsys.readouterr().out


def test_a_batch_leaves_the_state_the_same_decisions_recorded_one_by_one_leave(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    one_by_one = Scenario(tmp_path / "one-by-one")
    batch = Scenario(tmp_path / "batch")
    assert batch.stored() == one_by_one.stored()

    _record_one_by_one(one_by_one, _decisions(one_by_one), capsys)
    _record_as_batch(batch, _decisions(batch), capsys)

    assert read_state(batch.state_dir, "pending_editor_rows") == read_state(one_by_one.state_dir, "pending_editor_rows")
    assert len(read_state(batch.state_dir, "pending_editor_rows")) == 5
    assert batch.stored() == one_by_one.stored()

    one_by_one.run_mvp()
    batch.run_mvp()

    assert batch.history() == one_by_one.history()
    assert batch.stored() == one_by_one.stored()
    assert read_state(batch.state_dir, "pending_editor_rows") == []


def test_notes_are_kept_byte_for_byte_and_an_empty_note_records_no_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = Scenario(tmp_path)
    output = _record_as_batch(scenario, _decisions(scenario), capsys)
    assert "staged_rows=5" in output
    scenario.run_mvp()

    city = scenario.record("City Book")
    assert city["editor_note"] == NOTE_WITH_LINE_BREAK
    assert city[DECISION_LOG_FIELD] == [
        {
            "status": "interested",
            "note": NOTE_WITH_LINE_BREAK,
            "decision_date": date.today().isoformat(),
            "description_digest": city[DECISION_LOG_FIELD][0]["description_digest"],
        }
    ]
    assert city[DECISION_LOG_FIELD][0]["description_digest"] != ""
    desert = scenario.record("Desert Book")
    assert desert["editor_status"] == "already_known"
    assert desert.get("editor_note", "") == ""
    assert [entry["note"] for entry in desert[DECISION_LOG_FIELD]] == [""]
    forest = scenario.record("Forest Book")
    assert forest["feedback_dimensions"]["rights_availability"] == "available"
    river = scenario.record("River Book")
    assert [(entry["status"], entry["note"]) for entry in river[DECISION_LOG_FIELD]] == [
        ("reject", "Нет."),
        ("needs_deeper_analysis", ""),
    ]


def test_the_preview_writes_nothing_and_shows_every_row_the_counts_and_a_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = Scenario(tmp_path)
    decisions_file = scenario.decisions_file(_decisions(scenario))
    before = (scenario.stored(), state_snapshot(scenario.state_dir), state_databases())

    assert scenario.batch(decisions_file) == 3

    assert (scenario.stored(), state_snapshot(scenario.state_dir), state_databases()) == before
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "confirmation_required=true"
    assert "batch_rows=5" in lines
    for status, count in (
        ("already_known", 1),
        ("interested", 1),
        ("maybe_watch", 1),
        ("needs_deeper_analysis", 1),
        ("reject", 1),
    ):
        assert f"status_count_{status}={count}" in lines
    assert len(_preview_digest("\n".join(lines))) == 64
    city = scenario.record("City Book")
    city_block = lines[lines.index("row=1") : lines.index("row=2")]
    assert "matched_by=identity_key" in city_block
    assert "candidate_title=City Book" in city_block
    assert "candidate_author=Alex Author" in city_block
    assert f"candidate_identity_key={city['identity_key']}" in city_block
    assert "intended_status=interested" in city_block
    assert "final_disposition=interested" in city_block
    assert f"editor_note={NOTE_WITH_LINE_BREAK}" in "\n".join(city_block)
    river_block = lines[lines.index("row=2") : lines.index("row=3")]
    assert "matched_by=durable_id" in river_block
    assert f"candidate_identity_key={scenario.record('River Book')['identity_key']}" in river_block
    forest_block = lines[lines.index("row=3") : lines.index("row=4")]
    assert "dimension_rights_availability=available" in forest_block
    assert lines[-2] == "action=stage_pending_feedback"


def test_a_confirmed_write_refuses_a_payload_other_than_the_previewed_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = Scenario(tmp_path)
    decisions = _decisions(scenario)
    decisions_file = scenario.decisions_file(decisions)
    assert scenario.batch(decisions_file) == 3
    digest = _preview_digest(capsys.readouterr().out)
    decisions[1]["note"] = "Нет!"
    scenario.decisions_file(decisions)
    before = scenario.stored()

    assert scenario.batch(decisions_file, "--confirmed-by-editor", "--digest", digest) == 1
    assert "error=batch_digest_mismatch" in capsys.readouterr().out
    assert scenario.batch(decisions_file, "--confirmed-by-editor") == 1
    assert "error=batch_digest_required" in capsys.readouterr().out

    assert scenario.stored() == before
    assert read_state(scenario.state_dir, "pending_editor_rows") == []


@pytest.mark.parametrize("confirmed", [False, True], ids=["preview", "confirmed"])
def test_one_unresolvable_row_fails_the_whole_batch_and_stages_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], confirmed: bool
) -> None:
    scenario = Scenario(tmp_path)
    decisions = _decisions(scenario)
    # A row whose identity key and durable ID name two different books is ambiguous.
    decisions.append(
        {
            "identity_key": scenario.record("City Book")["identity_key"],
            "candidate_id": scenario.record("Forest Book")["candidate_id"],
            "status": "reject",
        }
    )
    decisions.append({"identity_key": "no such book|nobody", "status": "reject", "note": "Нет."})
    decisions_file = scenario.decisions_file(decisions)
    before = scenario.stored()

    extra = ["--confirmed-by-editor", "--digest", "0" * 64] if confirmed else []
    assert scenario.batch(decisions_file, *extra) == 1

    lines = capsys.readouterr().out.splitlines()
    assert "batch_refused=true" in lines
    assert "refused_rows=2" in lines
    assert any(line.startswith("unresolved row=6 status=ambiguous") for line in lines)
    alternatives = [line for line in lines if line.startswith("alternative row=6 ")]
    assert len(alternatives) == 2
    assert any("title=Forest Book" in line for line in alternatives)
    assert any(line.startswith("unresolved row=7 status=not_found identity_key=no such book|nobody") for line in lines)
    assert "staged_rows=0" in lines
    assert scenario.stored() == before
    assert read_state(scenario.state_dir, "pending_editor_rows") == []


def test_a_row_the_single_command_would_refuse_fails_the_whole_batch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = Scenario(tmp_path)
    key = scenario.record("City Book")["identity_key"]
    decisions = [
        *_decisions(scenario),
        {"identity_key": key, "status": "love it"},
        {"identity_key": key, "status": "reject", "note": "Нет.", "rights": "unavailable"},
        {"identity_key": key, "status": "reject", "Note": "Нет."},
        {"status": "reject"},
        {"identity_key": key, "status": "reject", "note": 5},
    ]
    before = scenario.stored()

    assert scenario.batch(scenario.decisions_file(decisions)) == 1

    lines = capsys.readouterr().out.splitlines()
    assert "refused_rows=5" in lines
    assert "invalid_row row=6 unsupported_status=love it" in lines
    assert any(line.startswith("invalid_row row=7 bare_refusal_must_stay_unspecified") for line in lines)
    assert "invalid_row row=8 unknown_fields=Note" in lines
    assert "invalid_row row=9 missing_identifier" in lines
    assert "invalid_row row=10 not_text=note" in lines
    assert scenario.stored() == before


def _confirm(scenario: Scenario, decisions_file: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    assert scenario.batch(decisions_file) == 3
    digest = _preview_digest(capsys.readouterr().out)
    returncode = scenario.batch(decisions_file, "--confirmed-by-editor", "--digest", digest)
    return returncode, capsys.readouterr().out


def test_a_repeated_confirmed_batch_is_refused_and_never_stages_its_rows_twice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = Scenario(tmp_path)
    decisions_file = scenario.decisions_file(_decisions(scenario))
    assert _confirm(scenario, decisions_file, capsys)[0] == 0
    staged = scenario.stored()

    class NextDay(date):
        @classmethod
        def today(cls) -> NextDay:
            return cls.fromordinal(date.today().toordinal() + 1)

    # A retry, even on another day, is the same decisions again, not new ones.
    monkeypatch.setattr(editor_decisions, "date", NextDay)
    returncode, output = _confirm(scenario, decisions_file, capsys)

    assert returncode == 1
    lines = output.splitlines()
    assert lines[0].startswith("error=batch_already_staged")
    assert "batch_refused=true" in lines
    assert "staged_rows=0" in lines
    assert scenario.stored() == staged
    assert len(read_state(scenario.state_dir, "pending_editor_rows")) == 5


def test_a_batch_appends_after_rows_already_pending_and_leaves_them_as_they_were(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = Scenario(tmp_path)
    key = scenario.record("Forest Book")["identity_key"]
    argv = ["record-feedback", "--identity-key", key, "--status", "reject", "--note", "Раньше.", "--state-dir"]
    assert main([*argv, str(scenario.state_dir), "--confirmed-by-editor"]) == 0
    capsys.readouterr()
    (earlier,) = read_state(scenario.state_dir, "pending_editor_rows")

    assert _confirm(scenario, scenario.decisions_file(_decisions(scenario)), capsys)[0] == 0

    pending = read_state(scenario.state_dir, "pending_editor_rows")
    assert len(pending) == 6
    assert pending[0] == earlier


def test_a_resolution_that_changed_since_the_preview_is_refused_by_the_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = Scenario(tmp_path)
    decisions_file = scenario.decisions_file(_decisions(scenario))
    assert scenario.batch(decisions_file) == 3
    digest = _preview_digest(capsys.readouterr().out)
    # Row 2 names River Book by its durable ID; the record's identity key changes after the preview.
    history = scenario.history()
    for record in history:
        if record["title"] == "River Book":
            record["identity_key"] = "river book|alex author, second edition"
    seed_state(scenario.state_dir, candidate_history=history)
    before = scenario.stored()

    assert scenario.batch(decisions_file, "--confirmed-by-editor", "--digest", digest) == 1

    assert capsys.readouterr().out.splitlines()[0].startswith("error=batch_digest_mismatch")
    assert scenario.stored() == before
    assert read_state(scenario.state_dir, "pending_editor_rows") == []


def test_a_batch_whose_write_fails_to_commit_stages_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = Scenario(tmp_path)
    decisions_file = scenario.decisions_file(_decisions(scenario))
    assert scenario.batch(decisions_file) == 3
    digest = _preview_digest(capsys.readouterr().out)
    before = scenario.stored()

    def failing_publish(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(json_backend, "publish_single", failing_publish)
    fail_database_commits(monkeypatch)
    with pytest.raises(OSError):
        record_feedback_batch(decisions_file, scenario.state_dir, confirmed_by_editor=True, digest=digest)
    assert scenario.stored() == before

    monkeypatch.undo()
    assert record_feedback_batch(decisions_file, scenario.state_dir, confirmed_by_editor=True, digest=digest) == 0
    assert len(read_state(scenario.state_dir, "pending_editor_rows")) == 5


@pytest.mark.parametrize("command", ["record-feedback", "record-feedback-batch"])
def test_a_refused_confirmed_call_creates_no_state_directory_and_no_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    state_dir = tmp_path / "state"
    decisions_file = tmp_path / "decisions.json"
    _write_json(decisions_file, [{"identity_key": "no such book|nobody", "status": "reject", "note": "Нет."}])
    if command == "record-feedback":
        argv = [command, "--identity-key", "no such book|nobody", "--status", "reject", "--note", "Нет."]
    else:
        argv = [command, "--decisions", str(decisions_file), "--digest", "0" * 64]

    assert main([*argv, "--state-dir", str(state_dir), "--confirmed-by-editor"]) == 1

    assert not state_dir.exists()
    assert state_databases() == {}


def test_a_book_decided_in_two_rows_is_flagged_on_both_and_both_rows_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = Scenario(tmp_path)
    decisions_file = scenario.decisions_file(_decisions(scenario))
    assert scenario.batch(decisions_file) == 3

    lines = capsys.readouterr().out.splitlines()
    river_first = lines[lines.index("row=2") : lines.index("row=3")]
    river_second = lines[lines.index("row=5") :]
    assert "warnings=same_book_in_rows=5" in river_first
    assert "warnings=same_book_in_rows=2" in river_second
    city = lines[lines.index("row=1") : lines.index("row=2")]
    assert not any(line.startswith("warnings=") for line in city)

    digest = _preview_digest("\n".join(lines))
    assert scenario.batch(decisions_file, "--confirmed-by-editor", "--digest", digest) == 0
    river_key = scenario.record("River Book")["identity_key"]
    pending = read_state(scenario.state_dir, "pending_editor_rows")
    assert [(row["Editor Status"], row["Editor Note"]) for row in pending if row["identity_key"] == river_key] == [
        ("reject", "Нет."),
        ("needs_deeper_analysis", ""),
    ]


def test_a_candidate_without_an_identity_key_cannot_be_staged_and_refuses_the_batch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = Scenario(tmp_path)
    keyless = "candidate-" + "c" * 24
    seed_state(
        scenario.state_dir, candidate_history=[*scenario.history(), {"candidate_id": keyless, "title": "Keyless"}]
    )
    decisions_file = scenario.decisions_file([{"candidate_id": keyless, "status": "reject"}])

    assert scenario.batch(decisions_file) == 1

    lines = capsys.readouterr().out.splitlines()
    assert f"unstageable row=1 reason=candidate_without_identity_key candidate_id={keyless}" in lines
    assert not any(line.startswith("unresolved ") for line in lines)


@pytest.mark.parametrize(
    "content",
    [
        None,
        b"\xff\xfe not utf-8",
        b"[{",
        b'{"identity_key": "x"}',
        b"[]",
        b'["x"]',
        b'[{"identity_key": "x", "status": "reject", "note": "\\ud800 x"}]',
        b"[" * 100_000 + b"]" * 100_000,
        b"[" + b'{"a":' * 40_000 + b"1" + b"}" * 40_000 + b"]",
    ],
    ids=[
        "missing",
        "not-utf8",
        "invalid-json",
        "not-a-list",
        "empty",
        "not-objects",
        "lone-surrogate",
        "deeply-nested",
        "deeply-nested-objects",
    ],
)
def test_an_unreadable_decisions_file_is_one_clean_input_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: bytes | None
) -> None:
    decisions_file = tmp_path / "decisions.json"
    if content is not None:
        decisions_file.write_bytes(content)
    for extra in ([], ["--confirmed-by-editor", "--digest", "0" * 64]):
        argv = ["record-feedback-batch", "--decisions", str(decisions_file), "--state-dir", str(tmp_path / "state")]
        assert main([*argv, *extra]) == 2
        captured = capsys.readouterr()
        lines = captured.out.splitlines()
        assert len(lines) == 1 and lines[0].startswith("error="), captured.out
        assert "Traceback" not in captured.out + captured.err
    assert not (tmp_path / "state").exists()
