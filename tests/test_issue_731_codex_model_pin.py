"""Issue #731: extraction runs on the repository's pinned Codex model.

The 2026-09-26 weekly run inherited a new model from the operator's global
Codex config one minute before it started, and twelve healthy captures came
back empty.  These tests pin down that extraction and Stage 0 use the pin,
report the global default beside it, and fail closed when the pin is unusable,
retired, or not applied.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from autoscout.codex_model_pin import DEFAULT_PIN_PATH
from autoscout.codex_model_pin import CodexModelPin
from autoscout.codex_model_pin import ModelPinError
from autoscout.codex_model_pin import header_profile
from autoscout.codex_model_pin import load_extraction_model_pin
from autoscout.codex_model_pin import model_unavailable_in
from autoscout.codex_model_pin import parse_model_pin
from autoscout.codex_recovery import CodexCliExtractionAgent
from autoscout.codex_recovery import ExtractionStatus
from autoscout.toolchain_preflight import CODEX_HEALTHY
from autoscout.toolchain_preflight import CODEX_MODEL_PIN_INVALID
from autoscout.toolchain_preflight import CODEX_MODEL_PIN_NOT_APPLIED
from autoscout.toolchain_preflight import CODEX_PINNED_MODEL_UNAVAILABLE
from autoscout.toolchain_preflight import build_stage_zero_result
from autoscout.toolchain_preflight import probe_codex_toolchain

PIN = CodexModelPin(model="pinned-model", reasoning_effort="medium", source="test")


def _features_ok(_flags):
    return 0, "memories  stable  false\n", ""


def _probe(**kwargs):
    defaults = {
        "codex_version_fn": lambda: ("codex-cli 0.157.1", ""),
        "features_fn": _features_ok,
        "pin_fn": lambda: PIN,
        "global_model_fn": lambda: "operator-default",
    }
    defaults.update(kwargs)
    return probe_codex_toolchain(**defaults)


def test_the_repository_pin_is_readable_and_names_a_model() -> None:
    pin = load_extraction_model_pin()

    assert DEFAULT_PIN_PATH.name == "codex-model-pin.json"
    assert pin.model == "gpt-5.6-luna"
    assert pin.reasoning_effort == "medium"
    assert pin.backend == "codex-cli"


@pytest.mark.parametrize(
    "document",
    [
        [],
        {},
        {"extraction": "gpt-5.6-luna"},
        {"extraction": {"model": "", "reasoning_effort": "medium"}},
        # A value that starts with "-" could be read as another CLI flag.
        {"extraction": {"model": "--dangerously-bypass", "reasoning_effort": "medium"}},
        {"extraction": {"model": "gpt-5.6-luna", "reasoning_effort": ""}},
        {"extraction": {"model": "gpt-5.6-luna", "reasoning_effort": 'medium" --x'}},
    ],
)
def test_an_unusable_pin_is_refused(document) -> None:
    with pytest.raises(ModelPinError):
        parse_model_pin(document)


def test_a_missing_pin_file_fails_closed_instead_of_using_the_global_default(tmp_path: Path) -> None:
    with pytest.raises(ModelPinError):
        load_extraction_model_pin(tmp_path / "codex-model-pin.json")


def test_extraction_passes_the_pin_whatever_the_global_config_says(tmp_path: Path, monkeypatch) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "operator-default"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    commands: list[list[str]] = []

    def fake_run_codex(command, isolated, response_path, bounded_timeout):
        del isolated, bounded_timeout
        commands.append(list(command))
        response_path.write_text(json.dumps({"entries": []}), encoding="utf-8")
        return 0, ""

    agent = CodexCliExtractionAgent(timeout_seconds=60, model_pin=PIN)
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
    assert command[command.index("-m") + 1] == "pinned-model"
    assert 'model_reasoning_effort="medium"' in command
    assert "operator-default" not in command
    # The prompt stays the final argument.
    assert command.index("-m") < len(command) - 1


def test_the_stage_zero_exec_probe_passes_the_pin(monkeypatch) -> None:
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

    _probe_codex_minimal_exec(timeout_seconds=1.0, pin=PIN)

    assert seen[0][seen[0].index("-m") + 1] == "pinned-model"


def test_stage_zero_reports_the_pin_beside_a_different_global_default() -> None:
    result = _probe(exec_fn=lambda: (0, "OK", "model: pinned-model\nreasoning effort: medium\n"))

    assert result["status"] == "pass"
    assert result["reason_code"] == CODEX_HEALTHY
    assert result["model"] == "pinned-model"
    detail = result["detail"]
    assert detail["model_pin"] == {"model": "pinned-model", "reasoning_effort": "medium", "source": "test"}
    assert detail["global_default_model"] == "operator-default"
    # A different global default is expected: it is reported, not a failure.
    assert detail["global_default_matches_pin"] is False
    assert detail["pin_applied"] == "verified"


def test_stage_zero_fails_closed_when_the_pinned_model_is_retired() -> None:
    stderr = (
        "model: pinned-model\n"
        'ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error",'
        '"message":"The \'pinned-model\' model is not supported when using Codex with a ChatGPT account."}}\n'
    )
    result = _probe(exec_fn=lambda: (1, "", stderr))

    assert result["status"] == "fail"
    assert result["reason_code"] == CODEX_PINNED_MODEL_UNAVAILABLE
    assert "pinned-model" in result["message"]
    assert "codex-model-pin.json" in result["message"]
    assert build_stage_zero_result(result, {"status": "pass"})["overall_status"] == "fail"


def test_stage_zero_fails_closed_when_the_cli_ignores_the_pin() -> None:
    result = _probe(exec_fn=lambda: (0, "OK", "model: operator-default\nreasoning effort: medium\n"))

    assert result["status"] == "fail"
    assert result["reason_code"] == CODEX_MODEL_PIN_NOT_APPLIED
    assert result["detail"]["reported_profile"]["model"] == "operator-default"


def test_stage_zero_fails_closed_on_an_unusable_pin_without_spending_a_request() -> None:
    exec_calls: list[int] = []

    def pin_fn():
        raise ModelPinError("codex model pin file not found: codex-model-pin.json")

    def exec_fn():
        exec_calls.append(1)
        return 0, "", ""

    result = _probe(pin_fn=pin_fn, exec_fn=exec_fn)

    assert result["status"] == "fail"
    assert result["reason_code"] == CODEX_MODEL_PIN_INVALID
    assert exec_calls == []


def test_header_profile_reads_the_cli_header_with_or_without_colour() -> None:
    stderr = "\x1b[1mmodel:\x1b[0m gpt-5.6-luna\n\x1b[1mreasoning effort:\x1b[0m Medium\n"

    assert header_profile(stderr) == {"model": "gpt-5.6-luna", "reasoning_effort": "medium"}
    assert header_profile("") == {}


def test_a_metadata_cache_warning_alone_is_not_an_unavailable_model() -> None:
    warning = "warning: Model metadata for `new-model` not found. Defaulting to fallback metadata."

    assert model_unavailable_in(warning) is False
    assert model_unavailable_in("The 'x' model is not supported when using Codex") is True
