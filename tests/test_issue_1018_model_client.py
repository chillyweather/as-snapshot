"""Issue #1018: one model-client module builds and runs every model call.

The ``codex exec`` command line used to be written out at seven call sites,
each repeating the sandbox mode.  These tests pin down that only the model
client builds it, that every call it builds is read-only, that the backend
comes from the pin file, and that a finished call ends in one typed outcome.

The Claude API backend (Issue #1149) is held to the same rule: only the model
client talks to the API or reads its key file, and no request it would send
carries Publishers Weekly text.
"""

from __future__ import annotations

import ast
import dataclasses
import itertools
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import autoscout
from autoscout import model_client as model_client_module
from autoscout.codex_model_pin import ModelPinError
from autoscout.codex_model_pin import parse_model_pin
from autoscout.model_client import ClaudeApiClient
from autoscout.model_client import CodexCliClient
from autoscout.model_client import ModelBackendError
from autoscout.model_client import ModelCallStatus
from autoscout.model_client import ModelFailure
from autoscout.model_client import ModelRequest
from autoscout.model_client import PriorExchange
from autoscout.model_client import model_client
from autoscout.model_client import model_client_for

PACKAGE_DIR = Path(autoscout.__file__).resolve().parent
MODEL_CLIENT_FILE = Path(model_client_module.__file__).resolve()
PIN_ENTRY = {"model": "pinned-model", "reasoning_effort": "medium"}


def test_only_the_model_client_passes_exec_to_a_subprocess() -> None:
    offenders: list[str] = []
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        if path.resolve() == MODEL_CLIENT_FILE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "exec":
                offenders.append(f"{path.relative_to(PACKAGE_DIR)}:{node.lineno}")

    assert offenders == [], "build the command with autoscout.model_client instead"


def _sandbox_modes(command: list[str]) -> list[str]:
    modes = [command[index + 1] for index, part in enumerate(command) if part == "--sandbox"]
    modes += [
        part.split("=", 1)[1].strip('"')
        for part in command
        if part.startswith("sandbox_mode=") or part.startswith("sandbox_mode =")
    ]
    return modes


def test_every_request_the_client_builds_runs_read_only(tmp_path: Path) -> None:
    for web_search, isolated, resume in itertools.product((False, True), repeat=3):
        request = ModelRequest(
            prompt="PROMPT",
            response_path=tmp_path / "response.json",
            model="pinned-model",
            reasoning_effort="medium",
            working_dir=None if resume else tmp_path,
            output_schema_path=None if resume else tmp_path / "schema.json",
            web_search=web_search,
            isolated=isolated,
            disabled_features=("memories",),
            resume_session_id="session-1" if resume else "",
        )

        command = CodexCliClient().command(request)

        assert command[:2] == ["codex", "exec"], (web_search, isolated, resume)
        assert _sandbox_modes(command) == ["read-only"], command
        assert command[-1] == "PROMPT"


def test_a_request_has_no_field_that_could_widen_the_sandbox() -> None:
    names = {field.name for field in dataclasses.fields(ModelRequest)}

    assert not {name for name in names if "sandbox" in name or "approval" in name or "bypass" in name}


def test_the_backend_defaults_to_the_codex_cli_when_the_pin_names_none() -> None:
    pin = parse_model_pin({"extraction": PIN_ENTRY})

    assert pin.backend == "codex-cli"
    assert isinstance(model_client_for(pin), CodexCliClient)


def test_the_pin_file_selects_the_backend() -> None:
    pin = parse_model_pin({"backend": "codex-cli", "extraction": PIN_ENTRY})

    assert model_client_for(pin).backend == "codex-cli"


@pytest.mark.parametrize("backend", ["deepseek", "", 7, ["codex-cli"]])
def test_an_unknown_backend_fails_closed(backend: Any) -> None:
    with pytest.raises(ModelPinError):
        parse_model_pin({"backend": backend, "extraction": PIN_ENTRY})


def test_the_client_refuses_an_unknown_backend() -> None:
    with pytest.raises(ModelBackendError):
        model_client("deepseek")


def _request(tmp_path: Path) -> ModelRequest:
    return ModelRequest(prompt="PROMPT", response_path=tmp_path / "response.json", working_dir=tmp_path)


def _raising(error: BaseException) -> Any:
    def run(_command: list[str], **_kwargs: Any) -> Any:
        raise error

    return run


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (subprocess.TimeoutExpired(cmd="codex", timeout=1), ModelCallStatus.TIMEOUT),
        (TimeoutError(), ModelCallStatus.TIMEOUT),
        (FileNotFoundError("codex"), ModelCallStatus.NOT_STARTED),
        (PermissionError("codex"), ModelCallStatus.NOT_STARTED),
    ],
)
def test_a_call_that_never_finished_ends_in_a_typed_failure(
    tmp_path: Path, error: BaseException, status: ModelCallStatus
) -> None:
    result = CodexCliClient().run(_request(tmp_path), timeout=1.0, runner=_raising(error))

    assert result.status is status
    assert result.payload is None
    assert (result.spawn_error is error) is (status is ModelCallStatus.NOT_STARTED)


def test_a_nonzero_exit_keeps_its_code_and_stderr(tmp_path: Path) -> None:
    result = CodexCliClient().run(
        _request(tmp_path),
        timeout=1.0,
        runner=lambda *_a, **_k: SimpleNamespace(returncode=2, stdout="", stderr="model_not_found"),
    )

    assert result.status is ModelCallStatus.EXIT_NONZERO
    assert (result.returncode, result.stderr) == (2, "model_not_found")


@pytest.mark.parametrize("body", [None, b"not json", b"\xff\xfe"])
def test_an_answer_that_cannot_be_read_is_unreadable(tmp_path: Path, body: bytes | None) -> None:
    request = _request(tmp_path)
    if body is not None:
        request.response_path.write_bytes(body)

    result = CodexCliClient().run(request, timeout=1.0, runner=lambda *_a, **_k: SimpleNamespace(returncode=0))

    assert result.status is ModelCallStatus.UNREADABLE_RESPONSE
    assert result.payload is None


def test_an_answered_call_returns_the_parsed_answer_and_passes_the_deadline(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def run(command: list[str], **kwargs: Any) -> Any:
        seen.update(kwargs)
        Path(command[command.index("--output-last-message") + 1]).write_text('{"ok": true}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="out", stderr="err")

    result = CodexCliClient().run(_request(tmp_path), timeout=12.5, runner=run)

    assert result.status is ModelCallStatus.ANSWERED
    assert result.payload == {"ok": True}
    assert seen["timeout"] == 12.5


# -- Claude API backend (Issue #1149) ------------------------------------------

# What only the model client may name: the API host, its headers and the key file.
CLAUDE_API_MARKERS = ("api.anthropic.com", "anthropic-version", "x-api-key", "claude.env", "ANTHROPIC_API_KEY")


def test_only_the_model_client_talks_to_the_claude_api_or_reads_its_key() -> None:
    offenders: list[str] = []
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        if path.resolve() == MODEL_CLIENT_FILE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if any(marker.casefold() in node.value.casefold() for marker in CLAUDE_API_MARKERS):
                    offenders.append(f"{path.relative_to(PACKAGE_DIR)}:{node.lineno}")

    assert offenders == [], "call the Claude API through autoscout.model_client instead"


def test_the_pin_file_can_select_the_claude_api_backend_per_entry() -> None:
    pin = parse_model_pin(
        {"backend": "codex-cli", "extraction": {**PIN_ENTRY, "model": "claude-haiku-5-5", "backend": "claude-api"}}
    )

    assert model_client_for(pin).backend == "claude-api"


class _NoConnection:
    """A connection factory that fails the test if the client ever connects."""

    def __call__(self, host: str, timeout: float) -> Any:
        raise AssertionError("the client connected")


@pytest.mark.parametrize(
    "where",
    ["prompt", "input_file", "earlier_prompt", "earlier_answer"],
)
def test_no_claude_request_that_mentions_publishers_weekly_is_sent(tmp_path: Path, where: str) -> None:
    pw = "https://www.PublishersWeekly.com/review"
    (tmp_path / "work_items.json").write_text(pw if where == "input_file" else "[]", encoding="utf-8")
    earlier = ModelRequest(
        prompt=pw if where == "earlier_prompt" else "FIRST", response_path=tmp_path / "first.json", model="m"
    )
    request = ModelRequest(
        prompt=pw if where == "prompt" else "PROMPT",
        response_path=tmp_path / "response.json",
        model="m",
        working_dir=tmp_path,
        input_files=("work_items.json",),
        follows=PriorExchange(earlier, pw if where == "earlier_answer" else "{}"),
    )

    result = ClaudeApiClient(key_file=tmp_path / "no-key.env", connection_factory=_NoConnection()).run(
        request, timeout=1.0
    )

    assert (result.status, result.failure) == (ModelCallStatus.REFUSED, ModelFailure.PW_TEXT_NOT_PERMITTED)


def test_the_claude_api_backend_refuses_web_search_before_connecting(tmp_path: Path) -> None:
    request = ModelRequest(prompt="P", response_path=tmp_path / "r.json", model="m", web_search=True)

    result = ClaudeApiClient(key_file=tmp_path / "no-key.env", connection_factory=_NoConnection()).run(
        request, timeout=1.0
    )

    assert (result.status, result.failure) == (ModelCallStatus.REFUSED, ModelFailure.WEB_SEARCH_UNSUPPORTED)


def test_no_claude_request_that_carries_a_pm_deal_logline_is_sent(tmp_path: Path) -> None:
    from autoscout.description_enrichment import _encode_work_items

    items = _encode_work_items([{"identity_key": "k", "title": "T", "deal_logline": "A history of salt roads."}])
    (tmp_path / "work_items.json").write_text(json.dumps(items), encoding="utf-8")
    request = ModelRequest(
        prompt="PROMPT",
        response_path=tmp_path / "response.json",
        model="m",
        working_dir=tmp_path,
        input_files=("work_items.json",),
    )

    result = ClaudeApiClient(key_file=tmp_path / "no-key.env", connection_factory=_NoConnection()).run(
        request, timeout=1.0
    )

    assert (result.status, result.failure) == (ModelCallStatus.REFUSED, ModelFailure.PM_TEXT_NOT_PERMITTED)
