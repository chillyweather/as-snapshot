"""Tests for the check-sources capture-only source health probe."""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

import autoscout.source_collectors as collectors_module
from autoscout.source_check import check_sources

ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _replay_config(tmp_path: Path, sources: list[dict[str, Any]]) -> Path:
    config_path = tmp_path / "config.json"
    _write_json(config_path, {"run_date": "2026-07-18", "sources": sources})
    return config_path


def _fixture_source(tmp_path: Path, name: str = "Probe Source") -> dict[str, Any]:
    page_path = tmp_path / f"{name.lower().replace(' ', '-')}-page.json"
    _write_json(
        page_path,
        {
            "source_name": name,
            "source_url": "https://example.test/list",
            "source_page": "https://example.test/list",
            "resolved_url": "https://example.test/list",
            "html": "<h1>Current List</h1><ul><li><a href='https://example.test/book'>A Book</a> Author Name</li></ul>",
        },
    )
    return {
        "name": name,
        "family": "award",
        "url": "https://example.test/list",
        "active": True,
        "access_status": "open",
        "check_frequency": "annual",
        "language": "en",
        "extraction_method": "codex-extraction-replay",
        "source_page_fixture_file": str(page_path),
    }


def test_healthy_replay_probe_reports_captured_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _replay_config(tmp_path, [_fixture_source(tmp_path)])
    exit_code = check_sources(config_path, live=False)
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Probe Source" in out
    assert "codex-captured" in out
    assert "failed=0" in out


def test_missing_fixture_reports_failed_and_exits_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = _fixture_source(tmp_path, name="Broken Source")
    broken["source_page_fixture_file"] = str(tmp_path / "does-not-exist.json")
    config_path = _replay_config(tmp_path, [_fixture_source(tmp_path), broken])
    exit_code = check_sources(config_path, live=False)
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "Broken Source" in out
    assert "failed" in out
    assert "failed=1" in out
    # A broken source must not hide the healthy one.
    assert "Probe Source" in out
    assert "codex-captured" in out


def test_inactive_and_skipped_sources_are_reported_not_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inactive = _fixture_source(tmp_path, name="Inactive Source")
    inactive["active"] = False
    paid = _fixture_source(tmp_path, name="Paid Source")
    paid["access_status"] = "paid"
    config_path = _replay_config(tmp_path, [inactive, paid])
    exit_code = check_sources(config_path, live=False)
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "inactive" in out
    assert "skipped" in out


def test_output_dir_persists_source_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = _replay_config(tmp_path, [_fixture_source(tmp_path)])
    out_dir = tmp_path / "probe-out"
    exit_code = check_sources(config_path, output_dir=out_dir, live=False)
    capsys.readouterr()
    assert exit_code == 0
    runs = json.loads((out_dir / "source-runs.json").read_text(encoding="utf-8"))
    assert runs[0]["source_name"] == "Probe Source"
    assert runs[0]["status"] == "codex-captured"


def test_omitted_output_dir_discards_captures(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = _replay_config(tmp_path, [_fixture_source(tmp_path)])
    exit_code = check_sources(config_path, live=False)
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "discarded" in out


def test_repo_pilot_config_replay_probe_is_green(capsys: pytest.CaptureFixture[str]) -> None:
    """The committed replay config must always pass a fixture-mode probe."""
    exit_code = check_sources(ROOT / "configs/current-weekly-pilot.json", live=False)
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "failed=0" in out
    assert "Premio Strega Saggistica 2026 Cinquina" in out


def test_repo_pilot_nonfiction_replay_uses_fixtures_without_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixture-mode Nonfiction.fr capture must be precollected without HTTP."""

    def forbidden_urlopen(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("fixture-mode Nonfiction.fr attempted network access")

    monkeypatch.setattr(collectors_module, "urlopen", forbidden_urlopen)
    output_dir = tmp_path / "source-check"
    exit_code = check_sources(ROOT / "configs/current-weekly-pilot.json", output_dir=output_dir, live=False)
    out = capsys.readouterr().out
    runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
    nonfiction_run = next(run for run in runs if run["source_name"] == "Nonfiction.fr Critiques")

    assert exit_code == 0
    assert "failed=0" in out
    assert nonfiction_run["status"] == "precollected"
    assert nonfiction_run["collection_mode"] == "replay"
    assert nonfiction_run["debug_metadata"]["collector_kind"] == "nonfiction-fr"
    assert nonfiction_run["debug_metadata"]["rss_fixture_file"].endswith("nonfiction-fr-rss.xml")
    assert nonfiction_run["debug_metadata"]["review_pages_fixture_file"].endswith("nonfiction-fr-review-pages.json")


# ── Blessed launcher ─────────────────────────────────────────────────────

LAUNCHER = ROOT / "scripts" / "check-sources"


def test_launcher_is_user_executable() -> None:
    assert LAUNCHER.stat().st_mode & stat.S_IXUSR, "launcher is not user-executable"


def test_launcher_resolves_interpreter_and_execs_check_sources() -> None:
    content = LAUNCHER.read_text(encoding="utf-8")
    assert content.splitlines()[0].endswith("bash")
    assert ".venv/bin/python" in content
    assert "python3" in content
    assert "-m autoscout check-sources" in content
    assert "configs/current-weekly-pilot-live.json" in content
    assert '"$@"' in content


def test_launcher_execs_probe_via_fake_interpreter(tmp_path: Path) -> None:
    """The launcher runs the canonical command through the resolved interpreter."""
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    launcher = scripts / "check-sources"
    launcher.write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    launcher.chmod(0o755)
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_python = venv_bin / "python"
    fake_python.write_text('#!/usr/bin/env bash\necho "FAKE_PYTHON $@"\n', encoding="utf-8")
    fake_python.chmod(0o755)
    result = subprocess.run([str(launcher), "--replay"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "FAKE_PYTHON -m autoscout check-sources" in result.stdout
    assert "--config configs/current-weekly-pilot-live.json" in result.stdout
    assert "--replay" in result.stdout
