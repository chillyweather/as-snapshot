"""State files stay byte-identical across the State Store refactor (#1068, #1011).

The golden-run fixture config is run with a state directory through the
four writers of canonical state that the editor relies on: a weekly run, a
confirmed feedback action, a second weekly run that consumes it, and a
confirmed identity merge. After each step every file in the state
directory is compared byte for byte with the copy recorded before the
State Store existed, in ``tests/fixtures/golden-run/state-after-each-step.json``.

The recorded copy is a tripwire, not a product rule: the call-site
migration (#1069-#1071) must leave it untouched. A deliberate change of a
state file's content updates it in the PR that makes the change, which then
says why.

The recorded run starts from a state directory in the single-file layout
Candidate History had before #1073, which the backend keeps byte for byte.
The same run on a new state directory keeps Candidate History one file per
book; it must hold the same records after every step, leave every other file
byte-identical, and write the same run-output copy and the same rendered
review.

Under ``--state-backend sqlite`` (#1075) the same steps run on the SQLite
backend, and its JSON export after each step must equal the same recorded
bytes.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from state_store_support import SHARDED
from state_store_support import SINGLE_FILE
from state_store_support import history_shards
from state_store_support import read_state
from state_store_support import sync_state
from state_store_support import use_layout
from test_golden_run import _build_config
from test_golden_run import _diff_excerpt

from autoscout import editor_decisions
from autoscout.__main__ import main
from autoscout.cli.weekly import run_mvp
from autoscout.json_io import serialize_json
from autoscout.review_rendering import render_review
from autoscout.state_store import export_json

GOLDEN_STATE = Path(__file__).resolve().parent / "fixtures" / "golden-run" / "state-after-each-step.json"

FEEDBACK = (
    ("recorded golden book|riley recorded", "reject", "Не наш профиль: слишком узкая тема."),
    ("agent golden book|riley golden", "interested", "Интересно, посмотрю подробнее."),
)

# The day the editor confirms the feedback. A confirmed decision is stamped
# with the day it was confirmed (#1053), so the recorded bytes pin that day.
DECISION_DAY = date(2026, 6, 16)


class _DecisionDay(date):
    @classmethod
    def today(cls) -> date:
        return DECISION_DAY


IDENTITY_CORRECTION = {
    "source_identity_keys": ["npr golden book|kara golden", "collector golden book|casey collector"],
    "canonical": {
        "identity_key": "npr golden book|kara golden",
        "title": "NPR Golden Book",
        "primary_authors": ["Kara Golden"],
    },
}


RUN_OUTPUTS = ("candidate-history.json", "weekly-review.md", "weekly-review.html", "editor-facing.csv")


def _snapshot(state_dir: Path) -> dict[str, str]:
    """The state directory's files; sharded Candidate History is shown in its single-file form.

    Under the SQLite backend the files are the database's export, written
    first, as a weekly run writes it before its commit (#1075).
    """
    export_json(state_dir)
    files = {path.name: path.read_bytes().decode("utf-8") for path in sorted(state_dir.iterdir()) if path.is_file()}
    if history_shards(state_dir):
        assert "candidate-history.json" not in files
        files["candidate-history.json"] = serialize_json(read_state(state_dir, "candidate_history"))
    return files


def state_after_each_step(tmp_path: Path, layout: str = SINGLE_FILE) -> dict[str, dict[str, str]]:
    """Run the four state writers in order and return the state directory after each."""
    config_path = _build_config(tmp_path)
    state_dir = tmp_path / "state"
    use_layout(state_dir, layout)
    sync_state(state_dir)
    steps: dict[str, dict[str, str]] = {}

    run_mvp(config_path, tmp_path / "run-1", state_dir)
    steps["weekly-run"] = _snapshot(state_dir)

    for identity_key, status, note in FEEDBACK:
        result = main(
            [
                "record-feedback",
                "--identity-key",
                identity_key,
                "--status",
                status,
                "--note",
                note,
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            ]
        )
        assert result == 0
    steps["feedback-confirm"] = _snapshot(state_dir)

    run_mvp(config_path, tmp_path / "run-2", state_dir)
    steps["weekly-run-consuming-feedback"] = _snapshot(state_dir)

    correction_path = tmp_path / "correction.json"
    correction_path.write_text(json.dumps(IDENTITY_CORRECTION), encoding="utf-8")
    result = main(
        ["merge-candidate-identity", "--state-dir", str(state_dir), "--correction-file", str(correction_path)]
    )
    assert result == 0
    steps["identity-merge"] = _snapshot(state_dir)
    return steps


def test_state_files_after_each_writer_match_the_recorded_bytes(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(editor_decisions, "date", _DecisionDay)
    expected = json.loads(GOLDEN_STATE.read_text(encoding="utf-8"))
    actual = state_after_each_step(tmp_path)
    assert not (tmp_path / "state" / "candidate-history").exists()
    _assert_matches(expected, actual)


def test_sharded_state_holds_the_recorded_records_and_bytes_after_each_writer(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(editor_decisions, "date", _DecisionDay)
    expected = json.loads(GOLDEN_STATE.read_text(encoding="utf-8"))
    actual = state_after_each_step(tmp_path, SHARDED)
    assert history_shards(tmp_path / "state")
    _assert_matches(expected, actual)


def test_a_run_on_sharded_state_writes_the_same_run_copy_and_review_as_on_single_file_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(editor_decisions, "date", _DecisionDay)
    outputs: dict[str, dict[str, bytes]] = {}
    for layout in (SINGLE_FILE, SHARDED):
        root = tmp_path / layout
        root.mkdir()
        state_after_each_step(root, layout)
        produced: dict[str, bytes] = {}
        for run in ("run-1", "run-2"):
            assert render_review(root / run, root / "state", effective_date="2026-06-15") == 0
            produced.update({f"{run}/{name}": (root / run / name).read_bytes() for name in RUN_OUTPUTS})
        outputs[layout] = produced

    assert outputs[SHARDED] == outputs[SINGLE_FILE]


def _assert_matches(expected: dict[str, dict[str, str]], actual: dict[str, dict[str, str]]) -> None:
    assert list(actual) == list(expected)
    mismatches = []
    for step, files in expected.items():
        if set(actual[step]) != set(files):
            mismatches.append(f"{step}: file set changed, expected {sorted(files)}, got {sorted(actual[step])}")
            continue
        for name, text in files.items():
            if actual[step][name] != text:
                excerpt = _diff_excerpt(text.encode("utf-8"), actual[step][name].encode("utf-8"))
                mismatches.append(f"--- {step}: {name} ---\n{excerpt}")
    assert not mismatches, "state files drifted from the recorded bytes:\n\n" + "\n\n".join(mismatches)
