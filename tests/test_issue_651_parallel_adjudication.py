"""Offline coverage for issue #651: parallel adjudication with a stage clock.

The adjudication stage runs one subprocess per candidate. Running those
calls one by one costs about 111 seconds per candidate, so a 92-candidate
weekly worklist costs almost three hours. This suite pins the fix: the
per-candidate loop runs on a bounded pool that shares one fetcher, the
preview artifact is byte-identical whatever order candidates complete in,
the stage stops admitting when the remaining budget cannot fit one more
call, and no candidate failure stops the run.

Every test is offline. The Codex adapter runs with a fake subprocess seam
and the stage fetches with a fake transport, as in
``test_issue_638_live_codex_adapter``.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from state_store_support import seed_state

from autoscout.description_adjudication import ADJUDICATION_CALL_TIMEOUT_SECONDS
from autoscout.description_adjudication import ADJUDICATION_MAX_WORKERS
from autoscout.description_adjudication import ADJUDICATION_STAGE_BUDGET_SECONDS
from autoscout.description_adjudication import DECISION_NOT_ATTEMPTED
from autoscout.description_adjudication import CodexDescriptionAdjudicationAdapter
from autoscout.description_adjudication import adjudicate_descriptions
from autoscout.grounded_description import REASON_NOT_ATTEMPTED_STAGE_BUDGET
from autoscout.grounded_description import BoundedHTTPError
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.json_io import read_json

OPERATION_DATE = "2026-09-14"

KEYS = ["work one|alex author", "work two|alex author", "work three|alex author", "work four|alex author"]


class _UnreachableTransport:
    def request(self, url: str, **_kwargs: Any) -> Any:
        raise BoundedHTTPError("offline test never fetches")


def _offline_fetcher() -> BoundedHTTPFetcher:
    return BoundedHTTPFetcher(transport=_UnreachableTransport())  # type: ignore[arg-type]


def _record(candidate_id: str, identity_key: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "identity_key": identity_key,
        "title": "Work",
        "primary_authors": ["Alex Author"],
        "content_description": "",
    }


def _workspace(tmp_path: Path, keys: list[str] | None = None) -> tuple[Path, Path]:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir(exist_ok=True)
    output_dir.mkdir(exist_ok=True)
    chosen = keys or KEYS[:2]
    seed_state(
        state_dir, candidate_history=[_record(f"candidate-{index + 1:03d}", key) for index, key in enumerate(chosen)]
    )
    return state_dir, output_dir


def _valid_payload() -> dict[str, Any]:
    return {
        "resolved": True,
        "unresolved_reason": "",
        "confidence": "high",
        "content_description": "Книга о памяти, обществе и общественной жизни.",
        "citations": [],
    }


def _write_valid_response(command: list[str]) -> None:
    isolated = Path(command[command.index("-C") + 1])
    sent = json.loads((isolated / "work_item.json").read_text(encoding="utf-8"))[0]
    payload = dict(_valid_payload())
    payload["identity_key"] = sent["identity_key"]
    Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(payload), encoding="utf-8")


def _adapter(runner: Any) -> CodexDescriptionAdjudicationAdapter:
    return CodexDescriptionAdjudicationAdapter(runner=runner, timeout_seconds=240.0, model="", reasoning_effort="")


def test_worker_count_has_its_own_default_and_does_not_borrow_the_grounded_one() -> None:
    from autoscout import grounded_description

    assert ADJUDICATION_MAX_WORKERS == 3
    assert grounded_description.MAX_WORKERS == 3
    # The adjudication constant lives in its own module: raising the
    # adjudication worker count later must not move the grounded stage.
    import autoscout.description_adjudication as adjudication_module

    assert "ADJUDICATION_MAX_WORKERS" in dir(adjudication_module)
    assert ADJUDICATION_STAGE_BUDGET_SECONDS > ADJUDICATION_CALL_TIMEOUT_SECONDS


def test_candidates_run_concurrently(tmp_path: Path) -> None:
    """Two calls overlap: the second starts before the first finishes."""
    barrier = threading.Barrier(2, timeout=10.0)

    def runner(command: list[str], **kwargs: Any) -> Any:
        barrier.wait()
        _write_valid_response(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    state_dir, output_dir = _workspace(tmp_path)
    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=_adapter(runner),
        fetcher=_offline_fetcher(),
        max_workers=2,
    )
    assert result["status"] == "ok"
    assert result["considered"] == 2
    # Both parties reached the barrier, so the two calls overlapped.
    assert barrier.n_waiting == 0


def test_worker_count_is_honoured(tmp_path: Path) -> None:
    """No more than N calls are ever in flight at once."""
    lock = threading.Lock()
    in_flight = 0
    max_observed = 0

    def runner(command: list[str], **kwargs: Any) -> Any:
        nonlocal in_flight, max_observed
        with lock:
            in_flight += 1
            max_observed = max(max_observed, in_flight)
        try:
            time.sleep(0.2)
            _write_valid_response(command)
        finally:
            with lock:
                in_flight -= 1
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    state_dir, output_dir = _workspace(tmp_path, KEYS[:4])
    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=_adapter(runner),
        fetcher=_offline_fetcher(),
        max_workers=2,
    )
    assert result["considered"] == 4
    assert max_observed <= 2
    assert max_observed > 1


def test_preview_is_byte_identical_regardless_of_completion_order(tmp_path: Path) -> None:
    """The sort after the pool makes completion order unobservable."""

    def make_runner(fast_key: str) -> Any:
        def runner(command: list[str], **kwargs: Any) -> Any:
            isolated = Path(command[command.index("-C") + 1])
            sent = json.loads((isolated / "work_item.json").read_text(encoding="utf-8"))[0]
            # One candidate finishes fast, the others slow: swapping which
            # key is fast reverses the completion order.
            if sent["identity_key"] == fast_key:
                time.sleep(0.01)
            else:
                time.sleep(0.25)
            _write_valid_response(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        return runner

    first_dir = tmp_path / "first"
    first_dir.mkdir()
    state_a, out_a = _workspace(first_dir, KEYS[:3])
    adjudicate_descriptions(
        state_a,
        out_a,
        effective_date=OPERATION_DATE,
        adapter=_adapter(make_runner(KEYS[0])),
        fetcher=_offline_fetcher(),
        max_workers=3,
    )
    first_bytes = (out_a / "description-adjudication-preview.json").read_bytes()

    second_dir = tmp_path / "second"
    second_dir.mkdir()
    state_b, out_b = _workspace(second_dir, KEYS[:3])
    adjudicate_descriptions(
        state_b,
        out_b,
        effective_date=OPERATION_DATE,
        adapter=_adapter(make_runner(KEYS[2])),
        fetcher=_offline_fetcher(),
        max_workers=3,
    )
    second_bytes = (out_b / "description-adjudication-preview.json").read_bytes()

    assert first_bytes == second_bytes


def test_apply_gate_runs_in_sorted_order_and_predicts_apply(tmp_path: Path, monkeypatch: Any) -> None:
    """The pool never reaches the gate: it stays one sequential sorted pass."""
    from autoscout import description_adjudication as adjudication_module

    seen_orders: list[list[str]] = []
    real_apply = adjudication_module.apply_description_artifacts

    def recording_apply(history: list[dict[str, Any]], artifacts: list[dict[str, Any]], **kwargs: Any) -> Any:
        seen_orders.append([str(item.get("identity_key", "")) for item in artifacts])
        return real_apply(history, artifacts, **kwargs)

    monkeypatch.setattr(adjudication_module, "apply_description_artifacts", recording_apply)

    def runner(command: list[str], **kwargs: Any) -> Any:
        # Sleep by identity so completion order differs from sorted order.
        isolated = Path(command[command.index("-C") + 1])
        sent = json.loads((isolated / "work_item.json").read_text(encoding="utf-8"))[0]
        if "three" in sent["identity_key"]:
            time.sleep(0.01)
        else:
            time.sleep(0.2)
        _write_valid_response(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    dry_root = tmp_path / "dry"
    dry_root.mkdir(exist_ok=True)
    dry_state, dry_out = _workspace(dry_root, KEYS[:3])
    dry_result = adjudicate_descriptions(
        dry_state,
        dry_out,
        effective_date=OPERATION_DATE,
        adapter=_adapter(runner),
        fetcher=_offline_fetcher(),
        max_workers=3,
    )
    # Every gate call carries exactly one row, in sorted row order.
    assert seen_orders
    assert all(len(batch) == 1 for batch in seen_orders)
    flat = [key for batch in seen_orders for key in batch]
    preview = read_json(dry_out / "description-adjudication-preview.json")
    assert flat == [row["identity_key"] for row in preview["preview"]]
    assert [row["candidate_id"] for row in preview["preview"]] == [
        "candidate-001",
        "candidate-002",
        "candidate-003",
    ]

    apply_dir = tmp_path / "apply"
    apply_dir.mkdir()
    apply_state, apply_out = _workspace(apply_dir, KEYS[:3])
    seen_orders.clear()
    apply_result = adjudicate_descriptions(
        apply_state,
        apply_out,
        effective_date=OPERATION_DATE,
        adapter=_adapter(runner),
        fetcher=_offline_fetcher(),
        max_workers=3,
        apply=True,
    )
    assert apply_result["would_apply"] == dry_result["would_apply"]
    assert apply_result["applied"] == dry_result["would_apply"]


def test_stage_budget_stops_admission_and_reports_not_attempted(tmp_path: Path) -> None:
    """A budget that cannot fit one call attempts nothing and reports it."""
    calls: list[list[str]] = []

    def runner(command: list[str], **kwargs: Any) -> Any:
        calls.append(list(command))
        _write_valid_response(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    state_dir, output_dir = _workspace(tmp_path)
    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=_adapter(runner),
        fetcher=_offline_fetcher(),
        max_workers=2,
        stage_budget_seconds=0.0,
    )
    assert calls == []
    assert result["previewed"] == 2
    assert result["considered"] == 0
    assert result["not_attempted"] == 2
    preview = read_json(output_dir / "description-adjudication-preview.json")
    assert preview["counts"]["not_attempted"] == 2
    for row in preview["preview"]:
        assert row["decision"] == DECISION_NOT_ATTEMPTED
        assert row["decision_reason"] == REASON_NOT_ATTEMPTED_STAGE_BUDGET
        assert row["reason"] == REASON_NOT_ATTEMPTED_STAGE_BUDGET
        assert row["response"] == {}
        assert row["artifact"] == {}


def test_one_failure_does_not_stop_the_others_at_parallel_workers(tmp_path: Path) -> None:
    def runner(command: list[str], **kwargs: Any) -> Any:
        isolated = Path(command[command.index("-C") + 1])
        sent = json.loads((isolated / "work_item.json").read_text(encoding="utf-8"))[0]
        if "two" in sent["identity_key"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
        time.sleep(0.1)
        _write_valid_response(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    state_dir, output_dir = _workspace(tmp_path, KEYS[:3])
    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=_adapter(runner),
        fetcher=_offline_fetcher(),
        max_workers=3,
    )
    assert result["rejected"] == 1
    assert result["valid"] == 2
    preview = read_json(output_dir / "description-adjudication-preview.json")
    rows = {row["identity_key"]: row for row in preview["preview"]}
    assert rows["work two|alex author"]["decision"] == "invalid"
    assert rows["work one|alex author"]["decision"] != "invalid"
    assert rows["work three|alex author"]["decision"] != "invalid"
