"""Issue #734: qualify a Codex model by replaying the frozen benchmark.

These tests use a fake extraction agent, so they spend no Codex request.  The
agent answers every work item with the benchmark's own accepted extraction,
optionally emptied for chosen sources, so the production validators and the
qualification criteria see realistic output.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from autoscout import model_qualification as mq
from autoscout.codex_model_pin import CodexModelPin
from autoscout.codex_recovery import ExtractionAttempt
from autoscout.codex_recovery import ExtractionStatus
from autoscout.toolchain_preflight import PIN_REPLACEMENT_ACTION

SELECTED = ["PW Best Books 2025", "Bokhandlerforeningen Boklista - Generell litteratur voksne"]
FIXED_NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


class ReplayAgent:
    """Answers with the benchmark's accepted extraction for each source."""

    def __init__(self, pin: CodexModelPin, empty_for: set[str], calls: list[tuple[str, str]]) -> None:
        self._pin = pin
        self._empty_for = empty_for
        self._calls = calls
        cases = {case.source_name: case for case in mq.load_benchmark()}
        self._entries = {name: case.reference()["entries"] for name, case in cases.items()}

    def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
        del capture_path, timeout_seconds
        name = item["source_name"]
        self._calls.append((self._pin.model, name))
        entries = [] if name in self._empty_for else self._entries[name]
        artifact_path.write_text(
            json.dumps(
                {
                    "source_name": name,
                    "source_url": item["source_url"],
                    "operation_id": item["operation_id"],
                    "capture_sha256": item["capture_sha256"],
                    "entries": entries,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return ExtractionAttempt(ExtractionStatus.PRODUCED, False, 0)


def _factory(empty_for_model: dict[str, set[str]] | None = None, calls: list[tuple[str, str]] | None = None):
    recorded = calls if calls is not None else []

    def factory(pin: CodexModelPin, _timeout: float) -> ReplayAgent:
        return ReplayAgent(pin, (empty_for_model or {}).get(pin.model, set()), recorded)

    return factory


def _pin_file(root: Path, *, report: str | None = None) -> Path:
    path = root / "configs" / "codex-model-pin.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"extraction": {"model": "pinned-model", "reasoning_effort": "medium", "qualification_report": report}}
        ),
        encoding="utf-8",
    )
    return path


def _qualify(tmp_path: Path, model: str, **kwargs: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "repetitions": 2,
        "workers": 2,
        "source_names": SELECTED,
        "reports_dir": tmp_path / "reports",
        "agent_factory": _factory(),
        "now": lambda: FIXED_NOW,
    }
    defaults.update(kwargs)
    if "pin_path" not in defaults:
        defaults["pin_path"] = _pin_file(tmp_path)
    return mq.qualify_model(model, "medium", **defaults)


def test_every_benchmark_reference_still_validates_under_the_current_contract() -> None:
    cases = mq.load_benchmark()
    sources = mq.configured_sources(include_inactive=True)
    # Issue #826 (operator decision 2026-10-15): the National Book Awards
    # rollover to 2026 dropped the frozen 2025 case, as the Baillie Gifford
    # rollover did (#762). Restore the floor of 30 after the next
    # --refresh-benchmark captures the 2026 edition.
    assert len(cases) >= 29
    # The hard cases from the 2026-09-26 investigation that have a committed
    # valid extraction are in the benchmark.
    names = {case.source_name for case in cases}
    assert set(SELECTED) <= names
    for case in cases:
        capture = json.loads(case.capture_bytes().decode("utf-8"))
        reference = case.reference()
        mentions, _dropped = mq.collected_mentions(
            capture,
            {"entries": reference["entries"]},
            sources[case.source_name],
            config_path=mq.DEFAULT_CONFIG_PATH,
        )
        keys = {mq._mention_key(m.get("title"), m.get("primary_authors") or m.get("authors")) for m in mentions}
        assert mentions, case.source_name
        assert keys == set(reference["reference_keys"]), case.source_name
        assert case.reference_count == len(reference["reference_keys"])


def test_a_model_that_reproduces_the_references_passes(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    report = _qualify(tmp_path, "candidate-model", agent_factory=_factory(calls=calls))

    assert report["verdict"] == "PASS"
    assert report["baseline_compared"] is True
    # No stored baseline: the pinned model is run too, and says so.
    assert report["baseline"]["source"].startswith("fresh run")
    assert {model for model, _name in calls} == {"candidate-model", "pinned-model"}
    assert len(calls) == 2 * len(SELECTED) * 2
    for name in SELECTED:
        item = report["candidate"]["sources"][name]
        assert item["runs"] == 2
        assert item["valid_runs"] == 2
        assert item["empty_runs"] == 0
        assert item["mean_recall"] == 1.0
        assert item["reliable"] is True

    report_dir = Path(report["report_dir"])
    assert report_dir.parent == tmp_path / "reports"
    assert report_dir.name == "candidate-model--medium--20260927T120000Z"
    written = json.loads((report_dir / mq.REPORT_JSON).read_text(encoding="utf-8"))
    assert written["verdict"] == "PASS"
    markdown = (report_dir / mq.REPORT_MARKDOWN).read_text(encoding="utf-8")
    assert "**Verdict: PASS**" in markdown
    assert "(hard case)" in markdown


def test_a_model_that_empties_a_reliable_source_fails(tmp_path: Path) -> None:
    hard = "Bokhandlerforeningen Boklista - Generell litteratur voksne"
    report = _qualify(tmp_path, "candidate-model", agent_factory=_factory({"candidate-model": {hard}}))

    assert report["verdict"] == "FAIL"
    criteria = {item["name"]: item for item in report["criteria"]}
    assert criteria["no_reliability_regression"]["passed"] is False
    assert criteria["no_reliability_regression"]["detail"]["regressed_sources"] == [hard]
    assert criteria["empty_rate_not_higher"]["passed"] is False
    assert criteria["recall_at_least_95_percent_or_baseline"]["detail"]["below_threshold"] == [hard]
    assert report["candidate"]["sources"][hard]["empty_runs"] == 2


def test_a_stored_pinned_report_is_reused_instead_of_rerunning_the_baseline(tmp_path: Path) -> None:
    # First qualify the pinned model itself, which records the baseline.
    first = _qualify(tmp_path, "pinned-model")
    assert first["baseline_compared"] is False
    report_rel = Path(first["report_dir"]).relative_to(tmp_path) / mq.REPORT_JSON
    pin_path = _pin_file(tmp_path, report=str(report_rel))

    calls: list[tuple[str, str]] = []
    second = _qualify(tmp_path, "candidate-model", pin_path=pin_path, agent_factory=_factory(calls=calls))

    assert second["verdict"] == "PASS"
    assert second["baseline"]["source"] == f"stored report {report_rel}"
    assert {model for model, _name in calls} == {"candidate-model"}

    calls.clear()
    third = _qualify(
        tmp_path, "candidate-model", pin_path=pin_path, agent_factory=_factory(calls=calls), rerun_baseline=True
    )
    assert third["baseline"]["source"].startswith("fresh run")
    assert {model for model, _name in calls} == {"candidate-model", "pinned-model"}


def test_qualification_writes_only_its_report_and_never_calls_git(tmp_path: Path, monkeypatch) -> None:
    benchmark_digest = mq.manifest_digest(mq.DEFAULT_BENCHMARK_DIR)
    pin_path = _pin_file(tmp_path)
    pin_bytes = pin_path.read_bytes()

    def no_subprocess(*_args, **_kwargs):
        raise AssertionError("qualification must not start a subprocess")

    monkeypatch.setattr(subprocess, "run", no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", no_subprocess)
    before = {path for path in tmp_path.rglob("*")}
    report = _qualify(tmp_path, "candidate-model", pin_path=pin_path)

    created = {path for path in tmp_path.rglob("*")} - before
    report_dir = Path(report["report_dir"])
    assert created and all(
        path == report_dir or report_dir in path.parents or path == report_dir.parent for path in created
    )
    assert pin_path.read_bytes() == pin_bytes
    assert mq.manifest_digest(mq.DEFAULT_BENCHMARK_DIR) == benchmark_digest


def test_a_tampered_benchmark_capture_is_refused(tmp_path: Path) -> None:
    benchmark = tmp_path / "benchmark"
    shutil.copytree(mq.DEFAULT_BENCHMARK_DIR, benchmark)
    case = next(case for case in mq.load_benchmark(benchmark) if case.source_name == SELECTED[0])
    (case.directory / "capture.json.gz").write_bytes(__import__("gzip").compress(b'{"html": "tampered"}', mtime=0))

    with pytest.raises(mq.QualificationInputError, match="digest mismatch"):
        case.capture_bytes()


def test_an_unknown_source_is_an_input_error(tmp_path: Path) -> None:
    with pytest.raises(mq.QualificationInputError, match="not in the benchmark"):
        _qualify(tmp_path, "candidate-model", source_names=["No Such Source"])


def _copied_config(tmp_path: Path) -> tuple[Path, Path]:
    """Copy the configuration and its portfolio; return both paths."""
    config_dir = tmp_path / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    portfolio_name = "current-weekly-portfolio.json"
    for name in (mq.DEFAULT_CONFIG_PATH.name, portfolio_name):
        shutil.copy(mq.DEFAULT_CONFIG_PATH.parent / name, config_dir / name)
    return config_dir / mq.DEFAULT_CONFIG_PATH.name, config_dir / portfolio_name


def _rewrite_sources(path: Path, change: Any) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    document["sources"] = change(document["sources"])
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def test_a_retired_source_keeps_its_benchmark_case(tmp_path: Path) -> None:
    """Issue #764: retiring a source does not stop qualification on the frozen benchmark."""

    def retire(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {**source, "active": False, "lifecycle_status": "retired"} if source.get("name") == SELECTED[1] else source
            for source in sources
        ]

    def reactivate(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {**source, "active": True, "lifecycle_status": "established"}
            if source.get("name") == SELECTED[1]
            else source
            for source in sources
        ]

    # Both selected sources are retired in the real portfolio (Issue #996 and
    # the 2026-11-15 Boklista retirement), so the copy first makes SELECTED[1]
    # active again and the retirement under test is that source's.
    config_path, portfolio_path = _copied_config(tmp_path)
    assert SELECTED[0] not in mq.configured_sources(config_path)
    _rewrite_sources(portfolio_path, reactivate)
    assert SELECTED[1] in mq.configured_sources(config_path)
    _rewrite_sources(portfolio_path, retire)
    assert SELECTED[1] not in mq.configured_sources(config_path)
    report = _qualify(tmp_path, "pinned-model", config_path=config_path)
    assert report["verdict"] == "PASS"


def test_a_source_removed_from_the_configuration_is_an_input_error(tmp_path: Path) -> None:
    def remove(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [source for source in sources if source.get("name") != SELECTED[0]]

    config_path, portfolio_path = _copied_config(tmp_path)
    _rewrite_sources(portfolio_path, remove)
    _rewrite_sources(config_path, remove)
    with pytest.raises(mq.QualificationInputError, match="no longer configured"):
        _qualify(tmp_path, "pinned-model", config_path=config_path)


def test_duration_criterion_leaves_room_for_a_retry_round() -> None:
    summary = {
        "Slow": {
            "runs": 1,
            "valid_runs": 1,
            "empty_runs": 0,
            "reliable": True,
            "reference_count": 1,
            "mean_recall": 1.0,
            "duration_p95_seconds": 4000.0,
        }
    }
    result = mq.evaluate(summary, None, workers=1, extraction_budget_seconds=6450.0)

    criteria = {item["name"]: item for item in result["criteria"]}
    assert criteria["durations_fit_weekly_budget"]["passed"] is False
    assert criteria["durations_fit_weekly_budget"]["detail"]["allowed_first_round_seconds"] == 3225.0
    assert result["verdict"] == "FAIL"
    assert result["baseline_compared"] is False


def test_refresh_reads_git_history_and_freezes_the_newest_valid_pair(tmp_path: Path) -> None:
    # Refresh rebuilds active sources only. Both selected sources are retired
    # and keep only their frozen cases, so the refresh is exercised on an
    # active hard case.
    name = "Audible Bestselling Science & Engineering Audiobooks"
    assert name in mq.HARD_CASE_SOURCES
    case = next(case for case in mq.load_benchmark() if case.source_name == name)
    capture = case.capture_bytes()
    entries = case.reference()["entries"]
    digest = hashlib.sha256(capture).hexdigest()
    artifacts = {
        "newest": json.dumps({"capture_sha256": "other", "entries": entries}).encode(),
        "older": json.dumps({"capture_sha256": digest, "entries": entries}).encode(),
    }

    def git_commits(_root, _path, _depth):
        return ["newest", "older"]

    def git_show(_root, commit, path):
        return capture if "codex-capture-" in path else artifacts[commit]

    manifest = mq.refresh_benchmark(
        benchmark_dir=tmp_path / "benchmark",
        source_names=[name],
        git_show=git_show,
        git_commits=git_commits,
    )

    assert [entry["commit"] for entry in manifest["cases"]] == ["older"]
    refreshed = mq.load_benchmark(tmp_path / "benchmark")[0]
    assert refreshed.capture_bytes() == capture
    assert refreshed.reference_keys() == case.reference_keys()


def test_the_retired_model_message_names_the_qualification_launcher() -> None:
    assert "./scripts/qualify-model" in PIN_REPLACEMENT_ACTION
    assert "configs/codex-model-pin.json" in PIN_REPLACEMENT_ACTION


def _recall_summary(recall: float) -> dict[str, dict[str, Any]]:
    return {
        "PW": {
            "runs": 3,
            "valid_runs": 3,
            "empty_runs": 0,
            "reliable": True,
            "reference_count": 41,
            "mean_recall": recall,
            "duration_p95_seconds": 170.0,
        }
    }


def test_recall_may_match_the_pinned_model_instead_of_the_flat_threshold() -> None:
    # The live smoke run recalled 36 of 41 PW Best Books references on the
    # pinned model itself. A candidate that does as well must not fail.
    as_good = mq.evaluate(_recall_summary(0.878), _recall_summary(0.878), workers=3, extraction_budget_seconds=6450.0)
    worse = mq.evaluate(_recall_summary(0.80), _recall_summary(0.878), workers=3, extraction_budget_seconds=6450.0)
    alone = mq.evaluate(_recall_summary(0.878), None, workers=3, extraction_budget_seconds=6450.0)

    def recall(result: dict[str, Any]) -> dict[str, Any]:
        return next(item for item in result["criteria"] if item["name"] == "recall_at_least_95_percent_or_baseline")

    assert recall(as_good)["passed"] is True
    assert recall(worse)["passed"] is False
    assert recall(worse)["detail"]["below_threshold"] == ["PW"]
    assert recall(alone)["passed"] is False
