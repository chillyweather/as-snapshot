"""Stage 0 toolchain preflight for issue #541.

A weekly run previously completed "successfully" with no grounded
descriptions because nothing checked the Codex toolchain or discovery
adapter until after capture and extraction.

Stage 0 runs before capture:
- Codex readiness: fail-closed, names model and CLI version.
- Discovery adapter: warn loudly via GET /health, never blocks, spends no
  Brave quota, distinguishes four states.

Every result is written to workflow-log.json as its own stage and
surfaced in run-summary.json (the durable place). Inconclusive checks
are "unknown", not "pass", and three-state strings beat bools.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoscout.grounded_description import PREFLIGHT_ADAPTER_KEY_MISSING
from autoscout.grounded_description import PREFLIGHT_ADAPTER_NOT_RUNNING
from autoscout.grounded_description import PREFLIGHT_ENDPOINT_NOT_CONFIGURED
from autoscout.grounded_description import PREFLIGHT_HEALTHY
from autoscout.toolchain_preflight import CODEX_HEALTHY
from autoscout.toolchain_preflight import CODEX_MODEL_UNSUPPORTED
from autoscout.toolchain_preflight import STAGE_ZERO_NAME
from autoscout.weekly_workflow import WeeklyWorkflow
from autoscout.weekly_workflow import deterministic_adapters
from autoscout.weekly_workflow import run_weekly_workflow


def _write_codex_fixture_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.json"
    config = {
        "run_date": "2026-07-13",
        "readiness_purpose": "recurring-weekly",
        "editor_row_target": 100,
        "pilot_row_min": 100,
        "pilot_row_max": 100,
        "pilot_mode": True,
        "sources": [
            {
                "name": "Test Source",
                "family": "award",
                "url": "https://example.test/source",
                "access_status": "open",
                "extraction_method": "codex-extraction-replay",
                "check_frequency": "weekly",
                "language": "en",
                "source_page_fixture_file": "test-capture.json",
                "extraction_response_file": "test-extraction.json",
            }
        ],
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def _write_capture_fixture(tmp_path: Path) -> None:
    capture_file_abs = tmp_path / "test-capture.json"
    capture_file_abs.write_text(
        json.dumps(
            {
                "source_url": "https://example.test/source",
                "html": '<html><body><a href="https://example.test/book">Test</a></body></html>',
            }
        ),
        encoding="utf-8",
    )


def _write_extraction_fixture(output_dir: Path) -> None:
    worklist = json.loads((output_dir / "codex-extraction-worklist.json").read_text(encoding="utf-8"))
    item = worklist["work_items"][0]
    result = output_dir / str(item["expected_extraction_file"])
    result.write_text(
        json.dumps(
            {
                "source_name": "Test Source",
                "source_url": "https://example.test/source",
                "operation_id": item["operation_id"],
                "capture_sha256": item["capture_sha256"],
                "entries": [
                    {
                        "title": "Test Book",
                        "primary_authors": ["Test Author"],
                        "source_url": "https://example.test/book",
                        "evidence_url": "https://example.test/book",
                        "evidence_note": "Test note.",
                        "candidate_category": "popular science",
                        "signal_type": "award shortlist",
                        "signal_strength": "shortlist",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _healthy_codex_probe() -> dict:
    return {
        "status": "pass",
        "reason_code": CODEX_HEALTHY,
        "message": "Codex toolchain is healthy (model 'gpt-5.6-sol', CLI codex-cli 0.200.0)",
        "model": "gpt-5.6-sol",
        "cli_version": "codex-cli 0.200.0",
        "detail": {},
    }


def _healthy_discovery_probe() -> dict:
    return {
        "status": "pass",
        "reason_code": PREFLIGHT_HEALTHY,
        "message": "discovery adapter is healthy",
        "endpoint": "http://127.0.0.1:8899/search?q={query}",
    }


def _codex_model_unsupported_probe() -> dict:
    return {
        "status": "fail",
        "reason_code": CODEX_MODEL_UNSUPPORTED,
        "message": "The 'gpt-5.6-sol' model requires a newer version of Codex (installed codex-cli 0.148.0). Please upgrade to the latest app or CLI and try again.",
        "model": "gpt-5.6-sol",
        "cli_version": "codex-cli 0.148.0",
        "detail": {"stderr": "400 model requires newer version"},
    }


def _discovery_endpoint_unset_probe() -> dict:
    return {
        "status": "degraded",
        "reason_code": PREFLIGHT_ENDPOINT_NOT_CONFIGURED,
        "message": "AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT is not set. Set it to http://127.0.0.1:8899/search?q={query} and run the grounded-search-adapter with AUTOSCOUT_SEARCH_API_KEY set.",
        "endpoint": "",
    }


def _discovery_key_missing_probe() -> dict:
    return {
        "status": "degraded",
        "reason_code": PREFLIGHT_ADAPTER_KEY_MISSING,
        "message": "AUTOSCOUT_SEARCH_API_KEY is not set",
        "endpoint": "http://127.0.0.1:8899/search?q={query}",
    }


def _discovery_not_running_probe() -> dict:
    return {
        "status": "degraded",
        "reason_code": PREFLIGHT_ADAPTER_NOT_RUNNING,
        "message": "discovery adapter is not running. Start it with 'python -m autoscout.grounded_search_adapter' and set AUTOSCOUT_SEARCH_API_KEY.",
        "endpoint": "http://127.0.0.1:8899/search?q={query}",
    }


def _unknown_codex_probe() -> dict:
    return {
        "status": "unknown",
        "reason_code": "codex_check_unknown",
        "message": "Could not verify Codex toolchain (model 'gpt-5.6-sol', CLI codex-cli 0.200.0): Codex probe timed out. Check Codex CLI responsiveness with 'codex --version' and retry.",
        "model": "gpt-5.6-sol",
        "cli_version": "codex-cli 0.200.0",
        "detail": {"error": "timeout"},
    }


def _unknown_discovery_probe() -> dict:
    return {
        "status": "unknown",
        "reason_code": "discovery_check_unknown",
        "message": "Could not check discovery adapter: network timeout. Verify AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT.",
        "endpoint": "http://127.0.0.1:8899/search?q={query}",
    }


def test_codex_model_unsupported_fails_at_stage_zero_before_capture(tmp_path: Path) -> None:
    """Codex model the CLI cannot serve fails at Stage 0 before capture."""
    config_path = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture_fixture(tmp_path)

    adapters = deterministic_adapters()
    adapters.stage_zero_codex_probe = _codex_model_unsupported_probe
    adapters.stage_zero_discovery_probe = _healthy_discovery_probe
    # Capture must not be invoked on fail-closed.
    capture_calls: list[str] = []
    original_prepare = adapters.capture.prepare

    def tracked_prepare(config_path, output_dir, live=False, effective_date=None):
        capture_calls.append("called")
        return original_prepare(config_path, output_dir, live=live, effective_date=effective_date)

    adapters.capture.prepare = tracked_prepare  # type: ignore[method-assign]

    result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

    assert result.failed_stage == STAGE_ZERO_NAME
    assert result.outcome.value == "intervention-required"
    assert capture_calls == []

    stages = [e.get("stage") for e in result.log]
    assert STAGE_ZERO_NAME in stages
    stage_zero_entry = next(e for e in result.log if e.get("stage") == STAGE_ZERO_NAME)
    assert stage_zero_entry.get("status") == "failed"
    detail = stage_zero_entry.get("detail", {})
    assert isinstance(detail, dict)
    codex = detail.get("codex", {})
    assert codex.get("status") == "fail"
    assert codex.get("reason_code") == "codex_model_unsupported"
    # Message must name the fix, model and CLI version.
    message = str(codex.get("message", ""))
    assert "gpt-5.6-sol" in message
    assert "0.148.0" in message
    assert "upgrade" in message.lower()

    # Also surfaced in run-summary.json (durable place).
    assert "toolchain_preflight" in result.run_summary
    assert result.run_summary["toolchain_preflight"]["codex"]["status"] == "fail"
    # File also exists.
    summary_path = output_dir / "run-summary.json"
    assert summary_path.exists()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["toolchain_preflight"]["codex"]["model"] == "gpt-5.6-sol"
    assert "0.148.0" in summary["toolchain_preflight"]["codex"]["cli_version"]
    assert "0.148.0" in summary["toolchain_preflight"]["codex"]["message"]


def test_discovery_endpoint_unset_reaches_review_and_reports_degraded(tmp_path: Path) -> None:
    """Endpoint unset is degraded, reaches review, surfaced in summary."""
    config_path = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture_fixture(tmp_path)

    adapters = deterministic_adapters()
    adapters.stage_zero_codex_probe = _healthy_codex_probe
    adapters.stage_zero_discovery_probe = _discovery_endpoint_unset_probe
    adapters.capture.prepare(config_path, output_dir, live=False)
    _write_extraction_fixture(output_dir)

    result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

    assert result.outcome.value in {"completed/no-attention", "completed/attention-required"}
    assert result.failed_stage is None
    stages = [e.get("stage") for e in result.log]
    assert STAGE_ZERO_NAME in stages
    entry = next(e for e in result.log if e.get("stage") == STAGE_ZERO_NAME)
    assert entry.get("status") in {"degraded", "unknown"}
    detail = entry.get("detail", {})
    discovery = detail.get("discovery", {})
    assert discovery.get("status") == "degraded"
    assert discovery.get("reason_code") == "discovery_endpoint_not_configured"
    # Message names the fix.
    assert "AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT" in discovery.get("message", "")

    run_summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
    assert "toolchain_preflight" in run_summary
    assert run_summary["toolchain_preflight"]["discovery"]["status"] == "degraded"
    assert run_summary["toolchain_preflight"]["discovery"]["reason_code"] == "discovery_endpoint_not_configured"
    assert run_summary["toolchain_preflight"]["overall_status"] in {"degraded", "unknown"}


def test_discovery_adapter_key_missing_503_reaches_review_and_reports_degraded(tmp_path: Path) -> None:
    """Adapter answering 503 (key missing) is degraded, reaches review."""
    config_path = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture_fixture(tmp_path)

    adapters = deterministic_adapters()
    adapters.stage_zero_codex_probe = _healthy_codex_probe
    adapters.stage_zero_discovery_probe = _discovery_key_missing_probe
    adapters.capture.prepare(config_path, output_dir, live=False)
    _write_extraction_fixture(output_dir)

    result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

    assert result.outcome.value in {"completed/no-attention", "completed/attention-required"}
    entry = next(e for e in result.log if e.get("stage") == STAGE_ZERO_NAME)
    discovery = entry.get("detail", {}).get("discovery", {})
    assert discovery.get("status") == "degraded"
    assert discovery.get("reason_code") == "discovery_adapter_key_missing"
    assert "AUTOSCOUT_SEARCH_API_KEY is not set" in discovery.get("message", "")

    run_summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
    assert run_summary["toolchain_preflight"]["discovery"]["reason_code"] == "discovery_adapter_key_missing"
    assert run_summary["toolchain_preflight"]["discovery"]["status"] == "degraded"


def test_discovery_adapter_not_running_reaches_review_and_reports_degraded(tmp_path: Path) -> None:
    """Adapter down (no answer) is degraded, reaches review."""
    config_path = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture_fixture(tmp_path)

    adapters = deterministic_adapters()
    adapters.stage_zero_codex_probe = _healthy_codex_probe
    adapters.stage_zero_discovery_probe = _discovery_not_running_probe
    adapters.capture.prepare(config_path, output_dir, live=False)
    _write_extraction_fixture(output_dir)

    result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

    assert result.outcome.value in {"completed/no-attention", "completed/attention-required"}
    entry = next(e for e in result.log if e.get("stage") == STAGE_ZERO_NAME)
    discovery = entry.get("detail", {}).get("discovery", {})
    assert discovery.get("status") == "degraded"
    assert discovery.get("reason_code") == "discovery_adapter_not_running"

    run_summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
    assert run_summary["toolchain_preflight"]["discovery"]["status"] == "degraded"
    assert "not running" in run_summary["toolchain_preflight"]["discovery"]["message"].lower()


def test_healthy_run_reports_every_check_as_passing(tmp_path: Path) -> None:
    """Healthy run reports every check as passing and behaviour is otherwise unchanged."""
    config_path = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture_fixture(tmp_path)

    adapters = deterministic_adapters()
    adapters.stage_zero_codex_probe = _healthy_codex_probe
    adapters.stage_zero_discovery_probe = _healthy_discovery_probe
    adapters.capture.prepare(config_path, output_dir, live=False)
    _write_extraction_fixture(output_dir)

    result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

    assert result.outcome.value in {"completed/no-attention", "completed/attention-required"}
    entry = next(e for e in result.log if e.get("stage") == STAGE_ZERO_NAME)
    assert entry.get("status") == "ok"
    detail = entry.get("detail", {})
    assert detail.get("overall_status") == "pass"
    assert detail.get("codex", {}).get("status") == "pass"
    assert detail.get("discovery", {}).get("status") == "pass"
    assert detail.get("codex", {}).get("reason_code") == "codex_healthy"
    assert detail.get("discovery", {}).get("reason_code") == "discovery_adapter_healthy"

    summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
    assert summary["toolchain_preflight"]["overall_status"] == "pass"
    assert summary["toolchain_preflight"]["codex"]["status"] == "pass"
    assert summary["toolchain_preflight"]["discovery"]["status"] == "pass"
    # Behaviour otherwise unchanged: grounded counts still surfaced
    assert "grounded_description_counts" in summary


def _fake_features(_flags: list[str]) -> tuple[int, str, str]:
    """Stand in for `codex features list` under the extraction flags.

    Injected so Stage 0 tests stay deterministic and never call the
    installed Codex CLI (Issue #414).
    """
    return 0, "memories                                 stable             false\n", ""


def test_stage_zero_leaves_no_lock_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stage 0 probe itself leaves no .auto-scout-workflow.lock."""
    # Run from tmp_path: the code gate runs this suite from a checkout whose
    # own scheduled run holds .auto-scout-workflow.lock in its root.
    monkeypatch.chdir(tmp_path)
    from autoscout.toolchain_preflight import probe_codex_toolchain
    from autoscout.toolchain_preflight import probe_discovery_adapter

    # Use injected fakes that do not touch git
    codex_result = probe_codex_toolchain(
        codex_version_fn=lambda: ("codex-cli 0.200.0", ""),
        model_fn=lambda: "gpt-5.6-sol",
        exec_fn=lambda: (0, "", ""),
        features_fn=_fake_features,
    )
    assert codex_result["status"] == "pass"

    class FakeEnrichment:
        def preflight(self):
            from autoscout.grounded_description import PreflightResult

            return PreflightResult(True, "discovery_adapter_healthy", "discovery adapter is healthy")

    discovery_result = probe_discovery_adapter(FakeEnrichment())
    assert discovery_result["status"] == "pass"

    lock_path = tmp_path / ".auto-scout-workflow.lock"
    assert not lock_path.exists()

    # Also run a full workflow with repo adapter and ensure no leftover lock
    config_path = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run2"
    output_dir.mkdir()
    _write_capture_fixture(tmp_path)
    adapters = deterministic_adapters()
    adapters.stage_zero_codex_probe = _healthy_codex_probe
    adapters.stage_zero_discovery_probe = _healthy_discovery_probe
    adapters.capture.prepare(config_path, output_dir, live=False)
    _write_extraction_fixture(output_dir)
    result = run_weekly_workflow(config_path, output_dir, adapters=adapters)
    assert result.outcome.value in {"completed/no-attention", "completed/attention-required"}
    assert not (Path.cwd() / ".auto-scout-workflow.lock").exists()
    assert not (tmp_path / ".auto-scout-workflow.lock").exists()
    assert not (output_dir / ".auto-scout-workflow.lock").exists()


def test_stage_zero_makes_no_brave_request(tmp_path: Path) -> None:
    """Stage 0 uses GET /health and must not request /search."""

    from autoscout.grounded_description import HTTPDiscoveryProvider
    from autoscout.grounded_description import SourceClassifier

    class FailingTransport:
        def request(self, url, *, timeout_seconds, max_response_bytes, max_redirects, allowed_hosts=None):
            if "/search" in url:
                raise AssertionError(f"Brave request made to /search: {url}")
            # Health is ok
            from autoscout.grounded_description import HTTPResponse

            return HTTPResponse(url=url, status=200, headers={}, body=b'{"status":"healthy"}')

    provider = HTTPDiscoveryProvider(
        "http://127.0.0.1:8899/search?q={query}",
        transport=FailingTransport(),
        classifier=SourceClassifier(),
    )
    result = provider.preflight()
    assert result.ok is True
    assert result.reason_code == "discovery_adapter_healthy"

    # Also via toolchain wrapper with enrichment adapter
    from autoscout.grounded_description import GroundedDescriptionStage
    from autoscout.grounded_description import GroundedDescriptionStageService
    from autoscout.toolchain_preflight import probe_discovery_adapter

    stage = GroundedDescriptionStage(discovery=provider)
    service = GroundedDescriptionStageService(stage=stage)
    discovery_result = probe_discovery_adapter(service)
    assert discovery_result["status"] == "pass"
    assert discovery_result["reason_code"] == "discovery_adapter_healthy"


def test_inconclusive_check_reported_as_unknown_not_pass(tmp_path: Path) -> None:
    """An inconclusive check is unknown, not pass, distinguishable from pass."""
    config_path = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture_fixture(tmp_path)

    adapters = deterministic_adapters()
    adapters.stage_zero_codex_probe = _unknown_codex_probe
    adapters.stage_zero_discovery_probe = _unknown_discovery_probe
    adapters.capture.prepare(config_path, output_dir, live=False)
    _write_extraction_fixture(output_dir)

    result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

    # Unknown is not pass and is distinguishable
    entry = next(e for e in result.log if e.get("stage") == STAGE_ZERO_NAME)
    detail = entry.get("detail", {})
    assert detail.get("codex", {}).get("status") == "unknown"
    assert detail.get("discovery", {}).get("status") == "unknown"
    assert detail.get("codex", {}).get("status") != "pass"
    assert detail.get("discovery", {}).get("status") != "pass"
    # Reason codes are unknown, not healthy
    assert detail.get("codex", {}).get("reason_code") == "codex_check_unknown"
    assert detail.get("discovery", {}).get("reason_code") == "discovery_check_unknown"

    summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
    assert summary["toolchain_preflight"]["codex"]["status"] == "unknown"
    assert summary["toolchain_preflight"]["discovery"]["status"] == "unknown"
    assert summary["toolchain_preflight"]["overall_status"] == "unknown"
    # Three-state string beats bool: no bool fields
    assert isinstance(summary["toolchain_preflight"]["codex"]["status"], str)
    assert summary["toolchain_preflight"]["codex"]["status"] not in {"True", "False", "true", "false"}
    # Workflow still reaches review (does not block on unknown)
    assert result.outcome.value in {"completed/no-attention", "completed/attention-required"}


def test_degraded_discovery_still_reports_codex_pass(tmp_path: Path) -> None:
    """Degraded discovery still preserves codex pass fact."""
    config_path = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture_fixture(tmp_path)

    adapters = deterministic_adapters()
    adapters.stage_zero_codex_probe = _healthy_codex_probe
    adapters.stage_zero_discovery_probe = _discovery_endpoint_unset_probe
    adapters.capture.prepare(config_path, output_dir, live=False)
    _write_extraction_fixture(output_dir)

    result = run_weekly_workflow(config_path, output_dir, adapters=adapters)
    detail = next(e for e in result.log if e.get("stage") == STAGE_ZERO_NAME).get("detail", {})
    assert detail.get("codex", {}).get("status") == "pass"
    assert detail.get("discovery", {}).get("status") == "degraded"
    assert detail.get("overall_status") == "degraded"


def test_probe_codex_returns_fail_when_model_requires_newer_version() -> None:
    """Direct probe must return fail when model is unservable - guards mutation 1."""
    from autoscout.toolchain_preflight import probe_codex_toolchain

    result = probe_codex_toolchain(
        codex_version_fn=lambda: ("codex-cli 0.148.0", ""),
        model_fn=lambda: "gpt-5.6-sol",
        exec_fn=lambda: (
            1,
            "",
            "400 Bad Request: The 'gpt-5.6-sol' model requires a newer version of Codex. Please upgrade.",
        ),
        features_fn=_fake_features,
    )
    # Literal expected values, not computed from code under test.
    assert result["status"] == "fail"
    assert result["reason_code"] == "codex_model_unsupported"
    assert result["model"] == "gpt-5.6-sol"
    assert result["cli_version"] == "codex-cli 0.148.0"
    assert "gpt-5.6-sol" in result["message"]
    assert "0.148.0" in result["message"]


def test_probe_discovery_returns_degraded_when_adapter_not_running() -> None:
    """Direct discovery probe must stay degraded when unreachable - guards mutation 2."""
    from autoscout.toolchain_preflight import probe_discovery_adapter

    class FakeUnreachable:
        def preflight(self):
            from autoscout.grounded_description import PreflightResult

            return PreflightResult(False, "discovery_adapter_not_running", "discovery adapter is not running")

    result = probe_discovery_adapter(FakeUnreachable())
    assert result["status"] == "degraded"
    assert result["reason_code"] == "discovery_adapter_not_running"
    assert result["status"] != "pass"


def test_probe_codex_returns_unknown_when_inconclusive() -> None:
    """Direct probe must return unknown when check is inconclusive - guards mutation 3."""
    from autoscout.toolchain_preflight import probe_codex_toolchain

    result = probe_codex_toolchain(
        codex_version_fn=lambda: ("", "codex --version timed out"),
        model_fn=lambda: "gpt-5.6-sol",
        exec_fn=lambda: (124, "", "codex exec timed out"),
    )
    assert result["status"] == "unknown"
    assert result["reason_code"] == "codex_check_unknown"
    assert result["status"] != "pass"
    assert result["reason_code"] != "codex_healthy"


def test_run_summary_surfacing_is_required_not_just_log(tmp_path: Path) -> None:
    """Run-summary must surface Stage 0 even though log already does - guards mutation 4."""
    config_path = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture_fixture(tmp_path)

    adapters = deterministic_adapters()
    adapters.stage_zero_codex_probe = _healthy_codex_probe
    adapters.stage_zero_discovery_probe = _discovery_endpoint_unset_probe
    adapters.capture.prepare(config_path, output_dir, live=False)
    _write_extraction_fixture(output_dir)

    result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

    # Log entry exists
    log_entry = next(e for e in result.log if e.get("stage") == STAGE_ZERO_NAME)
    assert log_entry is not None
    # But summary must also exist - log-only reporting is the defect.
    summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
    assert "toolchain_preflight" in summary
    assert summary["toolchain_preflight"]["discovery"]["status"] == "degraded"
    # Ensure the summary field is a string status, not a bool
    assert isinstance(summary["toolchain_preflight"]["discovery"]["status"], str)
    assert summary["toolchain_preflight"]["discovery"]["status"] != "pass"


def test_no_test_reaches_the_real_codex_cli(monkeypatch, tmp_path: Path) -> None:
    """A production-shaped weekly run must never shell out to the Codex CLI.

    The first version of Stage 0 fell back to the real probe whenever an
    adapter carried no probe, so three production-shaped tests spent one vendor
    request each and read the machine's installed CLI version. That made the
    suite cost money and behave differently per developer. The seam is now
    explicit: weekly_adapters() wires the real probes and a test overrides
    them, so nothing reaches a subprocess by default.
    """
    from autoscout import toolchain_preflight

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("a test reached the real Codex CLI")

    monkeypatch.setattr(toolchain_preflight, "_codex_cli_version", _forbidden)
    monkeypatch.setattr(toolchain_preflight, "_probe_codex_minimal_exec", _forbidden)

    # An adapter bundle with no probe wired must report "not checked", not
    # silently run the real toolchain probe.
    workflow = WeeklyWorkflow(adapters=deterministic_adapters())
    workflow._adapters.stage_zero_codex_probe = None
    workflow._adapters.stage_zero_discovery_probe = None

    codex = workflow._probe_stage_zero_codex()
    discovery = workflow._probe_stage_zero_discovery()

    assert codex["status"] == "unknown"
    assert codex["reason_code"] == "codex_check_unknown"
    assert "no Stage 0 probe is wired" in codex["message"]
    # The message must name the fix, not only the symptom.
    assert "weekly_adapters" in codex["message"]

    assert discovery["status"] == "unknown"
    assert discovery["reason_code"] == "discovery_check_unknown"
    assert "weekly_adapters" in discovery["message"]


def test_weekly_adapters_wires_the_real_probes_for_production(tmp_path: Path) -> None:
    """Production must still be checked, or Stage 0 would be decorative."""
    from autoscout import toolchain_preflight
    from autoscout.weekly_workflow import weekly_adapters

    adapters = weekly_adapters(tmp_path)

    assert adapters.stage_zero_codex_probe is toolchain_preflight.probe_codex_toolchain
    assert callable(adapters.stage_zero_discovery_probe)

    # An explicit override must win, so a production-shaped test can stay offline.
    sentinel = {"status": "pass", "reason_code": "codex_healthy", "message": "x", "model": "", "cli_version": ""}
    overridden = weekly_adapters(tmp_path, stage_zero_codex_probe=lambda: sentinel)
    assert overridden.stage_zero_codex_probe() is sentinel


def test_lost_durable_summary_write_leaves_a_trace(tmp_path: Path, monkeypatch) -> None:
    """A failed run-summary write must be recorded, not swallowed.

    On the fail-closed path the summary write is the only durable record,
    because workflow-log.json is replaced at the start of each run. The first
    version caught every exception and passed, so a lost write left the
    operator with nothing and no indication that anything was lost. Issue #473
    exists because a summary under-reported; a summary that silently fails to
    exist is the same defect with a larger blast radius.
    """
    from autoscout import weekly_workflow as weekly_module

    config_path = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture_fixture(tmp_path)

    real_store = weekly_module.WeeklyReportStore

    class _BrokenStore(real_store):  # type: ignore[misc, valid-type]
        def write_json(self, name, payload):
            if name == "run-summary.json":
                raise OSError("disk full")
            return super().write_json(name, payload)

    monkeypatch.setattr(weekly_module, "WeeklyReportStore", _BrokenStore)

    adapters = deterministic_adapters()
    adapters.stage_zero_codex_probe = _codex_model_unsupported_probe
    adapters.stage_zero_discovery_probe = _healthy_discovery_probe

    result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

    assert result.failed_stage == STAGE_ZERO_NAME
    trace = [
        entry
        for entry in result.log
        if entry.get("stage") == STAGE_ZERO_NAME and entry.get("status") == "summary-write-failed"
    ]
    assert len(trace) == 1
    assert "disk full" in str(trace[0].get("reason", ""))
