"""Focused acceptance tests for the issue #414 Codex feature policy."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from autoscout.codex_features import CODEX_FEATURE_REJECTED
from autoscout.codex_features import CODEX_FEATURE_STILL_ENABLED
from autoscout.codex_features import CODEX_FEATURES_CHECK_UNKNOWN
from autoscout.codex_features import CODEX_FEATURES_HEALTHY
from autoscout.codex_features import EXTRACTION_DISABLED_FEATURES
from autoscout.codex_features import extraction_feature_flags
from autoscout.codex_features import parse_codex_feature_states
from autoscout.codex_features import verify_extraction_feature_policy
from autoscout.codex_recovery import CodexCliExtractionAgent
from autoscout.codex_recovery import ExtractionStatus
from autoscout.toolchain_preflight import CODEX_HEALTHY
from autoscout.toolchain_preflight import build_stage_zero_result
from autoscout.toolchain_preflight import probe_codex_toolchain

FEATURE_TABLE = (
    "apply_patch_freeform                     removed            false\n"
    "apply_patch_preserve_line_endings        under development  false\n"
    "memories                                 stable             false\n"
    "multi_agent                              stable             true\n"
)


def _features_ok(_flags: list[str]) -> tuple[int, str, str]:
    return 0, FEATURE_TABLE, ""


def test_the_policy_disables_memory() -> None:
    # Literal, not derived from the module under test: the editor decided
    # this feature in issue #414.
    assert EXTRACTION_DISABLED_FEATURES == ("memories",)
    assert extraction_feature_flags() == ["--disable", "memories"]


def test_the_feature_table_is_parsed_including_two_word_states() -> None:
    states = parse_codex_feature_states(FEATURE_TABLE)

    assert states["memories"] == {"state": "stable", "value": "false"}
    assert states["apply_patch_preserve_line_endings"] == {"state": "under development", "value": "false"}
    assert states["multi_agent"]["value"] == "true"


def test_unreadable_lines_are_skipped_rather_than_guessed() -> None:
    states = parse_codex_feature_states("memories\n\nname only two\n")

    assert states == {}


def test_the_policy_passes_when_the_cli_confirms_the_feature_is_off() -> None:
    result = verify_extraction_feature_policy(features_fn=_features_ok)

    assert result["status"] == "pass"
    assert result["reason_code"] == CODEX_FEATURES_HEALTHY
    # The effective value is the evidence a run retains.
    assert result["detail"]["effective_values"] == {"memories": "false"}
    assert result["detail"]["feature_states"] == {"memories": "stable"}


def test_the_flags_are_passed_to_the_probe() -> None:
    seen: list[list[str]] = []

    def capture_flags(flags: list[str]) -> tuple[int, str, str]:
        seen.append(list(flags))
        return 0, FEATURE_TABLE, ""

    verify_extraction_feature_policy(features_fn=capture_flags)

    # The probe must ask the CLI under the same flags extraction uses, or it
    # reports the default state rather than the extraction state.
    assert seen == [["--disable", "memories"]]


def test_a_feature_name_the_cli_rejects_fails_closed() -> None:
    def rejected(_flags: list[str]) -> tuple[int, str, str]:
        return 1, "", "Error: Unknown feature flag: memories"

    result = verify_extraction_feature_policy(features_fn=rejected)

    assert result["status"] == "fail"
    assert result["reason_code"] == CODEX_FEATURE_REJECTED
    assert "renamed or removed" in result["message"]


def test_a_feature_missing_from_the_table_fails_closed() -> None:
    def without_memories(_flags: list[str]) -> tuple[int, str, str]:
        return 0, "multi_agent                              stable             true\n", ""

    result = verify_extraction_feature_policy(features_fn=without_memories)

    assert result["status"] == "fail"
    assert result["reason_code"] == CODEX_FEATURE_REJECTED
    assert result["detail"]["missing_features"] == ["memories"]


def test_a_feature_still_enabled_under_its_own_flag_fails_closed() -> None:
    def still_on(_flags: list[str]) -> tuple[int, str, str]:
        return 0, "memories                                 stable             true\n", ""

    result = verify_extraction_feature_policy(features_fn=still_on)

    assert result["status"] == "fail"
    assert result["reason_code"] == CODEX_FEATURE_STILL_ENABLED
    assert result["detail"]["still_enabled"] == ["memories"]


@pytest.mark.parametrize(
    "probe",
    [
        lambda _flags: (124, "", "codex features list timed out"),
        lambda _flags: (127, "", "Codex CLI not found: codex"),
        lambda _flags: (0, "no table here", ""),
    ],
)
def test_an_unreadable_check_is_unknown_and_never_pass(probe) -> None:
    result = verify_extraction_feature_policy(features_fn=probe)

    # "Could not check" must never be confused with "memory is off".
    assert result["status"] == "unknown"
    assert result["reason_code"] == CODEX_FEATURES_CHECK_UNKNOWN
    assert result["status"] != "pass"


def test_a_raising_probe_is_unknown_and_never_pass() -> None:
    def raises(_flags: list[str]) -> tuple[int, str, str]:
        raise OSError("probe exploded")

    result = verify_extraction_feature_policy(features_fn=raises)

    assert result["status"] == "unknown"
    assert result["reason_code"] == CODEX_FEATURES_CHECK_UNKNOWN


def test_stage_zero_records_the_feature_evidence_on_a_healthy_run() -> None:
    result = probe_codex_toolchain(
        codex_version_fn=lambda: ("codex-cli 0.153.4", ""),
        model_fn=lambda: "gpt-5.6-sol",
        exec_fn=lambda: (0, "", ""),
        features_fn=_features_ok,
    )

    assert result["status"] == "pass"
    assert result["reason_code"] == CODEX_HEALTHY
    # A run must prove what extraction had switched off, not only that
    # nothing failed.
    assert result["detail"]["feature_policy"]["effective_values"] == {"memories": "false"}


def test_stage_zero_fails_closed_when_the_feature_is_rejected() -> None:
    exec_calls: list[int] = []

    def exec_fn() -> tuple[int, str, str]:
        exec_calls.append(1)
        return 0, "", ""

    result = probe_codex_toolchain(
        codex_version_fn=lambda: ("codex-cli 0.153.4", ""),
        model_fn=lambda: "gpt-5.6-sol",
        exec_fn=exec_fn,
        features_fn=lambda _flags: (1, "", "Error: Unknown feature flag: memories"),
    )

    assert result["status"] == "fail"
    assert result["reason_code"] == CODEX_FEATURE_REJECTED
    assert result["cli_version"] == "codex-cli 0.153.4"
    # The whole run stops on a codex fail, so Stage 0 must not spend a Codex
    # request for a policy it cannot satisfy.
    assert exec_calls == []
    assert build_stage_zero_result(result, {"status": "pass"})["overall_status"] == "fail"


def test_stage_zero_is_unknown_when_the_feature_check_is_unknown() -> None:
    result = probe_codex_toolchain(
        codex_version_fn=lambda: ("codex-cli 0.153.4", ""),
        model_fn=lambda: "gpt-5.6-sol",
        exec_fn=lambda: (0, "", ""),
        features_fn=lambda _flags: (124, "", "codex features list timed out"),
    )

    assert result["status"] == "unknown"
    assert result["reason_code"] == CODEX_FEATURES_CHECK_UNKNOWN
    assert result["status"] != "pass"


def test_the_extraction_command_disables_the_feature() -> None:
    commands: list[list[str]] = []
    agent = CodexCliExtractionAgent(timeout_seconds=60)

    def fake_run_codex(command, isolated, response_path, bounded_timeout):
        del isolated, bounded_timeout
        commands.append(list(command))
        response_path.write_text(json.dumps({"entries": []}), encoding="utf-8")
        return 0, ""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        capture = root / "capture.json"
        capture.write_text("{}", encoding="utf-8")
        with mock.patch.object(CodexCliExtractionAgent, "_run_codex", side_effect=fake_run_codex):
            attempt = agent.extract_with_timeout(
                {"source_name": "Source", "source_url": "https://example.test/source"},
                capture,
                root / "artifact.json",
                timeout_seconds=60,
            )

    assert attempt.status is ExtractionStatus.PRODUCED
    command = commands[0]
    assert "--disable" in command
    assert command[command.index("--disable") + 1] == "memories"
    # The flag must precede the prompt, which is the final argument.
    assert command.index("--disable") < len(command) - 1


def test_the_stage_zero_exec_probe_runs_the_extraction_command(monkeypatch) -> None:
    seen: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = "OK"
        stderr = ""

    def fake_run(command, **_kwargs):
        seen.append(list(command))
        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    from autoscout.toolchain_preflight import _probe_codex_minimal_exec

    _probe_codex_minimal_exec(timeout_seconds=1.0)

    # A flag that breaks invocation must be found in Stage 0, not 29 sources
    # into the extraction stage.
    assert seen
    assert "--disable" in seen[0]
    assert seen[0][seen[0].index("--disable") + 1] == "memories"
