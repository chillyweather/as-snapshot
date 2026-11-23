from __future__ import annotations

import json
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.feedback_finalization import attempt_finalization
from autoscout.pipeline import run_pipeline
from autoscout.review_rendering import render_review
from autoscout.weekly_report import WEEKLY_REPORT_ARTIFACTS
from autoscout.weekly_report import WeeklyReportStore
from autoscout.weekly_report import WeeklyReportTransactionInProgress
from autoscout.weekly_report_row import EDITOR_FIELDNAMES


def _write_health_snapshot(store: WeeklyReportStore, run_date: str = "2026-08-03") -> None:
    store.write_json("pilot-status.json", {"run_date": run_date, "ready_for_editor": True})
    store.write_json("run-summary.json", {"run_date": run_date, "editor_facing_count": 0, "raw_candidate_count": 0})
    store.write_json("source-runs.json", [])


def test_report_store_has_coherent_typed_snapshot_and_shared_date_validation(tmp_path: Path) -> None:
    store = WeeklyReportStore(tmp_path)
    _write_health_snapshot(store)
    snapshot = store.snapshot("health", "2026-08-03")
    assert snapshot.coherent is True
    assert snapshot.missing == ()
    assert "workflow-log.json" not in {artifact.name for artifact in WEEKLY_REPORT_ARTIFACTS}

    store.write_json("run-summary.json", {"run_date": "2026-08-04", "editor_facing_count": 0})
    stale = store.snapshot("health", "2026-08-03")
    assert stale.coherent is False
    assert stale.date_inconsistent == ("run-summary.json",)


def test_report_transaction_hides_partial_snapshot_from_readers(tmp_path: Path) -> None:
    store = WeeklyReportStore(tmp_path)
    _write_health_snapshot(store)
    with store.transaction("feedback"):
        assert store.transaction_in_progress() is True
        partial = store.snapshot("health", "2026-08-03")
        assert partial.coherent is False
        with pytest.raises(WeeklyReportTransactionInProgress):
            store.read("run-summary.json")
    assert store.transaction_in_progress() is False
    assert store.snapshot("health", "2026-08-03").coherent is True


def test_competing_report_transactions_have_one_owner(tmp_path: Path) -> None:
    store = WeeklyReportStore(tmp_path)
    marker = store.transaction_marker_path()

    owner_holding = threading.Event()
    release_owner = threading.Event()
    contender_done = threading.Event()
    entered: list[str] = []
    errors: list[BaseException] = []

    def owner() -> None:
        with store.transaction("competing"):
            entered.append("owner")
            owner_holding.set()
            assert release_owner.wait(timeout=3)

    def contender() -> None:
        assert owner_holding.wait(timeout=3)
        try:
            with store.transaction("competing"):
                entered.append("contender")
        except BaseException as exc:
            errors.append(exc)
        finally:
            contender_done.set()

    owner_thread = threading.Thread(target=owner)
    contender_thread = threading.Thread(target=contender)
    owner_thread.start()
    contender_thread.start()

    assert owner_holding.wait(timeout=3)
    # The contender's failed attempt must happen while the owner still holds
    # the transaction, so it cannot acquire the lock after the owner exits.
    assert contender_done.wait(timeout=3)
    release_owner.set()
    owner_thread.join(timeout=3)
    contender_thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in (owner_thread, contender_thread))
    assert entered == ["owner"]
    assert len(errors) == 1
    assert isinstance(errors[0], WeeklyReportTransactionInProgress)
    assert not marker.exists()


def test_transaction_marker_has_unique_restrictive_owner_token(tmp_path: Path) -> None:
    store = WeeklyReportStore(tmp_path)
    tokens: list[str] = []

    for purpose in ("first", "second"):
        with store.transaction(purpose):
            marker = store.transaction_marker_path()
            payload = json.loads(marker.read_text(encoding="utf-8"))
            tokens.append(payload["owner_token"])
            assert payload["purpose"] == purpose
            assert payload["status"] == "in-progress"
            assert stat.S_IMODE(marker.stat().st_mode) == 0o600

    assert len(set(tokens)) == 2


@pytest.mark.parametrize("replacement", [None, "not-json"])
def test_transaction_owner_preserves_missing_or_malformed_replacement_marker(
    tmp_path: Path, replacement: str | None
) -> None:
    store = WeeklyReportStore(tmp_path)

    with store.transaction("owner"):
        marker = store.transaction_marker_path()
        if replacement is None:
            marker.unlink()
        else:
            marker.write_text(replacement, encoding="utf-8")

    if replacement is None:
        assert not store.transaction_marker_path().exists()
    else:
        assert store.transaction_marker_path().read_text(encoding="utf-8") == replacement


def test_transaction_owner_preserves_foreign_replacement_marker(tmp_path: Path) -> None:
    store = WeeklyReportStore(tmp_path)
    foreign = {"format": 1, "owner_token": "foreign-owner", "purpose": "foreign", "status": "in-progress"}

    with store.transaction("owner"):
        store.transaction_marker_path().write_text(json.dumps(foreign), encoding="utf-8")

    assert json.loads(store.transaction_marker_path().read_text(encoding="utf-8")) == foreign


def test_transaction_owner_preserves_structurally_malformed_marker(tmp_path: Path) -> None:
    store = WeeklyReportStore(tmp_path)

    with store.transaction("owner"):
        marker = store.transaction_marker_path()
        owner_token = json.loads(marker.read_text(encoding="utf-8"))["owner_token"]
        marker.write_text(json.dumps({"owner_token": owner_token}), encoding="utf-8")

    assert json.loads(store.transaction_marker_path().read_text(encoding="utf-8")) == {"owner_token": owner_token}


def test_interprocess_lock_blocks_replacement_after_marker_removal(tmp_path: Path) -> None:
    script = """
import sys
from pathlib import Path
from autoscout.weekly_report import WeeklyReportStore

store = WeeklyReportStore(Path(sys.argv[1]))
with store.transaction("child"):
    store.transaction_marker_path().unlink()
    print("ready", flush=True)
    sys.stdin.read(1)
"""
    store = WeeklyReportStore(tmp_path)
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        assert not store.transaction_marker_path().exists()
        with pytest.raises(WeeklyReportTransactionInProgress):
            with store.transaction("replacement"):
                pass
        assert child.stdin is not None
        child.stdin.write("release")
        child.stdin.close()
        assert child.wait(timeout=3) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)


def test_active_transaction_lock_blocks_replacement_after_marker_removal(tmp_path: Path) -> None:
    store = WeeklyReportStore(tmp_path)
    replacement_attempted = threading.Event()
    replacement_entered = threading.Event()
    errors: list[BaseException] = []

    def replacement_transaction() -> None:
        replacement_attempted.set()
        try:
            with store.transaction("replacement"):
                replacement_entered.set()
        except BaseException as exc:
            errors.append(exc)

    replacement_thread = threading.Thread(target=replacement_transaction)
    with store.transaction("owner"):
        store.transaction_marker_path().unlink()
        replacement_thread.start()
        assert replacement_attempted.wait(timeout=3)
        assert not replacement_entered.wait(timeout=0.2)
        replacement_thread.join(timeout=3)

    assert not replacement_thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], WeeklyReportTransactionInProgress)
    assert not store.transaction_marker_path().exists()


def test_active_transaction_lock_is_detected_before_marker_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WeeklyReportStore(tmp_path)
    marker_creation_started = threading.Event()
    allow_marker_creation = threading.Event()
    errors: list[BaseException] = []
    original_create_marker = WeeklyReportStore._create_transaction_marker

    def pause_before_marker(marker: Path, purpose: str) -> str:
        marker_creation_started.set()
        assert allow_marker_creation.wait(timeout=3)
        return original_create_marker(marker, purpose)

    monkeypatch.setattr(WeeklyReportStore, "_create_transaction_marker", staticmethod(pause_before_marker))

    def transaction_worker() -> None:
        try:
            with store.transaction("short-interval"):
                pass
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=transaction_worker)
    worker.start()
    assert marker_creation_started.wait(timeout=3)
    assert not store.transaction_marker_path().exists()
    assert store.transaction_in_progress() is True
    with pytest.raises(WeeklyReportTransactionInProgress):
        store.read("run-summary.json")
    assert store.snapshot("health").transaction_in_progress is True
    allow_marker_creation.set()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert errors == []
    assert store.transaction_in_progress() is False


def test_read_only_diagnostics_do_not_create_stable_lock_file(tmp_path: Path) -> None:
    store = WeeklyReportStore(tmp_path / "missing-output")
    lock_path = store.transaction_lock_path()

    assert store.transaction_in_progress() is False
    assert store.read("run-summary.json") is None
    snapshot = store.snapshot("health")

    assert snapshot.transaction_in_progress is False
    assert not lock_path.exists()


def test_successful_transaction_leaves_no_report_or_lock_artifact(tmp_path: Path) -> None:
    output = tmp_path / "run"
    store = WeeklyReportStore(output)

    with store.transaction("success"):
        pass

    assert list(output.iterdir()) == []
    assert store.transaction_lock_path().parent != output
    assert store.transaction_lock_path().is_file()


def test_failed_transaction_retains_marker_and_releases_stable_lock(tmp_path: Path) -> None:
    store = WeeklyReportStore(tmp_path)

    with pytest.raises(RuntimeError, match="injected failure"):
        with store.transaction("failure"):
            raise RuntimeError("injected failure")

    assert store.transaction_marker_path().is_file()
    assert store.transaction_lock_path().is_file()
    assert store.transaction_in_progress() is True


def test_report_store_rejects_unknown_paths_and_malformed_json(tmp_path: Path) -> None:
    store = WeeklyReportStore(tmp_path)
    with pytest.raises(ValueError):
        store.path("workflow-log.json")
    store.path("run-summary.json").write_text("not json", encoding="utf-8")
    snapshot = store.snapshot("health")
    assert "run-summary.json" in snapshot.malformed
    assert json.loads('{"ok": true}') == {"ok": True}


def test_catalog_matches_every_golden_pipeline_artifact() -> None:
    from tests.test_golden_run import ARTIFACT_NAMES

    names = {artifact.name for artifact in WEEKLY_REPORT_ARTIFACTS}
    assert set(ARTIFACT_NAMES) <= names
    assert "source-contribution.json" not in names
    assert {"source-review.md", "derived-rule-suggestions.json", "derived-rule-suggestions.md"} <= names


def test_snapshot_retains_values_from_one_read(tmp_path: Path) -> None:
    store = WeeklyReportStore(tmp_path)
    store.write_json("pilot-status.json", {"run_date": "2026-08-03", "ready_for_editor": True})
    store.write_json("run-summary.json", {"run_date": "2026-08-03", "editor_facing_count": 1, "raw_candidate_count": 1})
    store.write_json("source-runs.json", [])
    store.write_editor_csv([{field: "" for field in EDITOR_FIELDNAMES}])

    snapshot = store.snapshot("health", "2026-08-03")
    store.write_json(
        "run-summary.json", {"run_date": "2026-08-03", "editor_facing_count": 99, "raw_candidate_count": 99}
    )

    assert snapshot.json("run-summary.json")["editor_facing_count"] == 1
    assert snapshot.csv("editor-facing.csv")[0]["Candidate ID"] == ""
    assert snapshot.coherent is True


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("pilot-status.json", {"run_date": "2026-08-03"}),
        ("run-summary.json", {"run_date": "2026-08-03"}),
        ("source-runs.json", ["not-an-object"]),
    ],
)
def test_snapshot_rejects_malformed_semantic_shapes(tmp_path: Path, name: str, value: object) -> None:
    store = WeeklyReportStore(tmp_path)
    _write_health_snapshot(store)
    store.write_json(name, value)

    snapshots = [store.snapshot(purpose, "2026-08-03") for purpose in ("renderable", "health", "completed", "feedback")]

    assert all(name in snapshot.malformed for snapshot in snapshots)
    assert all(snapshot.coherent is False for snapshot in snapshots)


def test_snapshot_derives_date_and_rejects_internal_mismatch(tmp_path: Path) -> None:
    store = WeeklyReportStore(tmp_path)
    store.write_json("pilot-status.json", {"run_date": "2026-08-04", "ready_for_editor": True})
    store.write_json("run-summary.json", {"run_date": "2026-08-03", "editor_facing_count": 1})
    store.write_json("source-runs.json", [{"source_check_date": "2026-08-03", "source_name": "S"}])

    snapshot = store.snapshot("health")

    assert snapshot.effective_date == "2026-08-03"
    assert snapshot.coherent is False
    assert snapshot.date_inconsistent == ("pilot-status.json",)


def test_snapshot_detects_file_changed_after_early_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = WeeklyReportStore(tmp_path)
    _write_health_snapshot(store)
    original_read_bytes = Path.read_bytes
    changed = False

    def read_bytes(path: Path) -> bytes:
        nonlocal changed
        value = original_read_bytes(path)
        if path.name == "run-summary.json" and not changed:
            changed = True
            store.path("pilot-status.json").write_text(
                json.dumps({"run_date": "2026-08-03", "ready_for_editor": False}), encoding="utf-8"
            )
        return value

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    snapshot = store.snapshot("health", "2026-08-03")

    assert snapshot.read_inconsistent is True
    assert snapshot.coherent is False


def test_snapshot_detects_marker_appearing_during_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = WeeklyReportStore(tmp_path)
    _write_health_snapshot(store)
    original_read_bytes = Path.read_bytes
    marker_written = False

    def read_bytes(path: Path) -> bytes:
        nonlocal marker_written
        value = original_read_bytes(path)
        if path.name == "run-summary.json" and not marker_written:
            marker_written = True
            store.transaction_marker_path().write_text("stale", encoding="utf-8")
        return value

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    snapshot = store.snapshot("health", "2026-08-03")

    assert snapshot.transaction_in_progress is True
    assert snapshot.coherent is False


def test_existing_marker_is_stale_and_new_transaction_fails_closed(tmp_path: Path) -> None:
    store = WeeklyReportStore(tmp_path)
    store.transaction_marker_path().write_text("stale", encoding="utf-8")

    with pytest.raises(WeeklyReportTransactionInProgress):
        with store.transaction("pipeline"):
            pass
    assert store.transaction_in_progress() is True


def test_pipeline_write_failure_leaves_hidden_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"sources": []}), encoding="utf-8")
    output = tmp_path / "run"
    original_write_json = WeeklyReportStore.write_json

    def fail_write_json(store: WeeklyReportStore, name: str, value: object) -> None:
        if store.output_dir == output and name == "source-mentions.json":
            raise OSError("injected pipeline write failure")
        original_write_json(store, name, value)

    monkeypatch.setattr(WeeklyReportStore, "write_json", fail_write_json)
    with pytest.raises(OSError, match="injected pipeline write failure"):
        run_pipeline(config, output)

    store = WeeklyReportStore(output)
    assert store.transaction_in_progress() is True
    assert store.snapshot("health").coherent is False


def test_render_write_failure_leaves_hidden_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "run"
    output.mkdir()
    store = WeeklyReportStore(output)
    store.write_json("run-summary.json", {"run_date": "2026-08-03", "editor_facing_count": 0})
    store.write_json("scouting-candidates.json", [])
    original_write_text = WeeklyReportStore.write_text

    def fail_write_text(store: WeeklyReportStore, name: str, value: str) -> None:
        if store.output_dir == output and name == "weekly-review.md":
            raise OSError("injected render write failure")
        original_write_text(store, name, value)

    monkeypatch.setattr(WeeklyReportStore, "write_text", fail_write_text)
    with pytest.raises(OSError, match="injected render write failure"):
        render_review(output)

    assert store.transaction_in_progress() is True
    assert store.snapshot("renderable").coherent is False


def test_pipeline_and_render_fail_closed_on_report_transaction(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"sources": []}), encoding="utf-8")
    store = WeeklyReportStore(tmp_path / "run")
    store.transaction_marker_path().parent.mkdir(parents=True, exist_ok=True)
    store.transaction_marker_path().write_text("stale", encoding="utf-8")

    with pytest.raises(WeeklyReportTransactionInProgress):
        run_pipeline(config, tmp_path / "run")
    assert render_review(tmp_path / "run") == 1


def test_run_health_rejects_derived_date_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = WeeklyReportStore(tmp_path)
    store.write_json("run-summary.json", {"run_date": "2026-08-03", "editor_facing_count": 1})
    store.write_json("pilot-status.json", {"run_date": "2026-08-04", "ready_for_editor": True})
    store.write_json("source-runs.json", [])

    from autoscout.run_health import run_health

    assert run_health(tmp_path) == 1
    output = capsys.readouterr().out
    assert "run_health_status=date_inconsistent" in output
    assert "date_mismatch=pilot_status run_date=2026-08-04 expected=2026-08-03" in output


def test_feedback_refuses_date_inconsistent_artifact_before_state_mutation(tmp_path: Path) -> None:
    output = tmp_path / "run"
    state = tmp_path / "state"
    state.mkdir()
    store = WeeklyReportStore(output)
    output.mkdir()
    store.write_json("source-mentions.json", [{"source_check_date": "2026-08-03"}])
    store.write_json("scouting-candidates.json", [{"identity_key": "book|author"}])
    store.write_json("run-summary.json", {"run_date": "2026-08-04", "editor_facing_count": 1, "raw_candidate_count": 1})
    store.write_editor_csv([{field: "" for field in EDITOR_FIELDNAMES} | {"Candidate ID": "book|author"}])
    seed_state(state, pending_editor_rows=[{"identity_key": "book|author", "Editor Status": "interested"}])
    before = state_snapshot(state)

    assert attempt_finalization(output, state, "2026-08-03") == "skipped"
    assert state_snapshot(state) == before
