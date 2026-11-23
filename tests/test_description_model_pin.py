"""The description stages run on the repository's pin, never the operator's global Codex default.

On 2026-11-10 the first weekly run on the run server found ``gpt-5.5`` in the
global Codex config, a model a ChatGPT account cannot use. Extraction was
pinned (#731) and worked; description enrichment, adjudication, the PM logline
descriptions and original-edition completion read the global default, so every
one of their calls failed and Stage 0 still passed. No live model is called here.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest import mock

import pytest

from autoscout.codex_model_pin import CodexModelPin
from autoscout.codex_model_pin import ModelPinError
from autoscout.codex_model_pin import load_description_model_pin
from autoscout.codex_model_pin import usage_limit_in
from autoscout.description_adjudication import CodexDescriptionAdjudicationAdapter
from autoscout.description_enrichment import CALL_FAILURE_EXIT
from autoscout.description_enrichment import CALL_FAILURE_MODEL_UNAVAILABLE
from autoscout.description_enrichment import CALL_FAILURE_TIMEOUT
from autoscout.description_enrichment import CALL_FAILURE_USAGE_LIMIT
from autoscout.description_enrichment import CodexDescriptionEnrichmentAgent
from autoscout.original_edition_completion import CodexOriginalTitleProposer
from autoscout.toolchain_preflight import CODEX_HEALTHY
from autoscout.toolchain_preflight import CODEX_MODEL_PIN_INVALID
from autoscout.toolchain_preflight import CODEX_PINNED_MODEL_UNAVAILABLE
from autoscout.toolchain_preflight import probe_codex_toolchain

PIN = CodexModelPin(model="pinned-model", reasoning_effort="medium", source="test")
DESCRIPTION_PIN = CodexModelPin(model="description-model", reasoning_effort="low", source="test")
ITEM = {"identity_key": "work one|a writer", "title": "Work One", "metadata": {}}
UNSUPPORTED = "ERROR: The 'gpt-5.5' model is not supported when using Codex with a ChatGPT account."
USAGE_LIMIT = "ERROR: You’ve hit your usage limit. Upgrade to Pro or try again at Nov 14th, 2026 4:22 PM."


def test_the_repository_names_a_description_pin() -> None:
    pin = load_description_model_pin()

    assert pin.model and pin.reasoning_effort
    assert pin.source == "codex-model-pin.json"


def _completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def test_the_description_agent_passes_the_pin_to_codex() -> None:
    agent = CodexDescriptionEnrichmentAgent(model_pin=DESCRIPTION_PIN)

    with mock.patch("subprocess.run", return_value=_completed(1)) as run:
        agent.generate([dict(ITEM)])

    command = run.call_args.args[0]
    assert command[command.index("-m") + 1] == "description-model"
    assert 'model_reasoning_effort="low"' in command


def test_the_description_agent_reads_the_repository_pin_by_default() -> None:
    assert CodexDescriptionEnrichmentAgent().model_pin == load_description_model_pin()


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (_completed(1, UNSUPPORTED), CALL_FAILURE_MODEL_UNAVAILABLE),
        (_completed(1, USAGE_LIMIT), CALL_FAILURE_USAGE_LIMIT),
        (_completed(1, "something else"), CALL_FAILURE_EXIT),
        (subprocess.TimeoutExpired(cmd="codex", timeout=1), CALL_FAILURE_TIMEOUT),
    ],
)
def test_a_failed_description_call_names_its_reason(outcome: Any, reason: str) -> None:
    agent = CodexDescriptionEnrichmentAgent(model_pin=DESCRIPTION_PIN)
    reasons: list[str] = []
    patch = (
        mock.patch("subprocess.run", side_effect=outcome)
        if isinstance(outcome, Exception)
        else mock.patch("subprocess.run", return_value=outcome)
    )

    with patch:
        answer = agent.generate([dict(ITEM)], _on_failure=reasons.append)

    assert answer == {}
    assert reasons == [reason]


def test_a_usage_limit_is_told_from_other_errors() -> None:
    assert usage_limit_in(USAGE_LIMIT)
    assert not usage_limit_in(UNSUPPORTED)


def test_adjudication_and_original_edition_completion_run_the_description_pin() -> None:
    pin = load_description_model_pin()

    assert CodexDescriptionAdjudicationAdapter(runner=lambda *a, **k: None)._model == pin.model
    assert CodexOriginalTitleProposer(runner=lambda *a, **k: None)._model == pin.model


def _probe(**kwargs: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "codex_version_fn": lambda: ("codex-cli 0.167.0", ""),
        "features_fn": lambda _flags: (0, "memories  stable  false\n", ""),
        "pin_fn": lambda: PIN,
        "global_model_fn": lambda: "gpt-5.5",
        "exec_fn": lambda: (0, "OK", "model: pinned-model\nreasoning effort: medium\n"),
        "description_pin_fn": lambda: DESCRIPTION_PIN,
    }
    defaults.update(kwargs)
    return probe_codex_toolchain(**defaults)


def test_stage_zero_reports_the_description_pin() -> None:
    result = _probe(description_exec_fn=lambda: (0, "OK", ""))

    assert result["reason_code"] == CODEX_HEALTHY
    assert result["detail"]["description_model_pin"] == {
        "model": "description-model",
        "reasoning_effort": "low",
        "source": "test",
    }


def test_stage_zero_fails_when_the_description_model_cannot_be_used() -> None:
    result = _probe(description_exec_fn=lambda: (1, "", UNSUPPORTED))

    assert result["status"] == "fail"
    assert result["reason_code"] == CODEX_PINNED_MODEL_UNAVAILABLE
    assert "description-model" in result["message"]
    assert "codex-model-pin.json" in result["message"]


def test_stage_zero_probes_once_when_both_pins_name_the_same_model() -> None:
    second_probes: list[int] = []

    def second_probe() -> Any:
        second_probes.append(1)
        return 0, "OK", ""

    result = _probe(description_pin_fn=lambda: PIN, description_exec_fn=second_probe)

    assert result["reason_code"] == CODEX_HEALTHY
    # The extraction probe already covers this model.
    assert second_probes == []


def test_stage_zero_fails_closed_on_an_unreadable_description_pin() -> None:
    def broken() -> CodexModelPin:
        raise ModelPinError("codex model pin has no 'description' entry")

    def spent() -> Any:
        raise AssertionError("no Codex request is spent on an unreadable pin")

    result = _probe(description_pin_fn=broken, exec_fn=spent)

    assert result["status"] == "fail"
    assert result["reason_code"] == CODEX_MODEL_PIN_INVALID
    assert "description" in result["message"]
