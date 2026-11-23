"""Issue #1149: a Claude API backend for the model client.

No test here opens a socket or needs a key: every request goes to a fake
connection installed in place of ``http.client.HTTPSConnection``, and the key
file is a temporary file holding a sentinel value.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from autoscout import codex_model_pin
from autoscout import model_client as model_client_module
from autoscout.codex_model_pin import CodexModelPin
from autoscout.codex_model_pin import ModelPinError
from autoscout.codex_model_pin import adjudication_model_pin
from autoscout.codex_model_pin import parse_model_pin
from autoscout.model_client import ClaudeApiClient
from autoscout.model_client import CodexCliClient
from autoscout.model_client import ModelCallStatus
from autoscout.model_client import ModelFailure
from autoscout.model_client import ModelRequest
from autoscout.model_client import PriorExchange
from autoscout.model_client import model_client_for
from autoscout.model_client import recording_model_usage

SENTINEL_KEY = "sk-ant-sentinel-0123456789abcdefSECRET"
CLAUDE_PIN = CodexModelPin("claude-haiku-5-5", "medium", "test", backend="claude-api")
ENTRY = {"model": "gpt-pinned", "reasoning_effort": "medium"}
CLAUDE_ENTRY = {"model": "claude-haiku-5-5", "reasoning_effort": "medium", "backend": "claude-api"}


# -- Fake API --------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self._headers = {name.casefold(): value for name, value in (headers or {}).items()}

    def getheader(self, name: str, default: Any = None) -> Any:
        return self._headers.get(name.casefold(), default)

    def read(self, amount: int | None = None) -> bytes:
        size = len(self._body) if amount is None else amount
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def close(self) -> None:
        pass


class FakeApi:
    """A connection factory that answers each request from a script, in order.

    A scripted reply is a ``FakeResponse``, an exception to raise, or a
    function of the parsed request body that returns either.
    """

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.connections = 0

    def __call__(self, host: str, timeout: float) -> FakeConnection:
        self.connections += 1
        return FakeConnection(self, host, timeout)


class FakeConnection:
    def __init__(self, api: FakeApi, host: str, timeout: float) -> None:
        self.api = api
        self.host = host
        self.timeout = timeout
        self.closed = threading.Event()

    def request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
        self.api.requests.append(
            {
                "host": self.host,
                "method": method,
                "path": path,
                "headers": dict(headers),
                "body": json.loads(body.decode("utf-8")),
                "timeout": self.timeout,
            }
        )

    def getresponse(self) -> FakeResponse:
        reply = self.api.replies.pop(0)
        if callable(reply) and not isinstance(reply, FakeResponse):
            reply = reply(self.api.requests[-1]["body"], self)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def close(self) -> None:
        self.closed.set()


def message(text: str, *, stop_reason: str = "end_turn", thinking: bool = True) -> FakeResponse:
    content: list[dict[str, Any]] = [{"type": "thinking", "thinking": "", "signature": "sig"}] if thinking else []
    content.append({"type": "text", "text": text})
    return FakeResponse(
        200,
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": content,
            "stop_reason": stop_reason,
            "usage": {
                "input_tokens": 1200,
                "output_tokens": 340,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    )


def error(status: int, error_type: str, text: str = "", **extra: Any) -> FakeResponse:
    headers = extra.pop("headers", None)
    return FakeResponse(status, {"type": "error", "error": {"type": error_type, "message": text, **extra}}, headers)


@pytest.fixture()
def key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "claude.env"
    path.write_text(f"ANTHROPIC_API_KEY={SENTINEL_KEY}\n", encoding="utf-8")
    monkeypatch.setattr(model_client_module, "CLAUDE_KEY_FILE", path)
    return path


@pytest.fixture()
def install_api(monkeypatch: pytest.MonkeyPatch, key_file: Path) -> Callable[..., FakeApi]:
    """Route every Claude API connection of the process to a fake, and never sleep."""

    def install(*replies: Any) -> FakeApi:
        api = FakeApi(*replies)
        monkeypatch.setattr(model_client_module, "_https_connection", api)
        return api

    monkeypatch.setattr(model_client_module.time, "sleep", lambda _seconds: None)
    return install


def _client(api: FakeApi, **kwargs: Any) -> ClaudeApiClient:
    return ClaudeApiClient(connection_factory=api, sleep=lambda _seconds: None, **kwargs)


def _request(tmp_path: Path, **kwargs: Any) -> ModelRequest:
    fields: dict[str, Any] = {
        "prompt": "Read items.json and answer.",
        "response_path": tmp_path / "response.json",
        "model": "claude-haiku-5-5",
        "reasoning_effort": "medium",
        "working_dir": tmp_path,
    }
    fields.update(kwargs)
    return ModelRequest(**fields)


def _texts(body: dict[str, Any]) -> list[str]:
    return [block["text"] for message in body["messages"] for block in message["content"]]


# -- Selection by the pin ----------------------------------------------------------


def test_an_entry_backend_overrides_the_top_level_one_for_that_entry_only() -> None:
    document = {"backend": "codex-cli", "extraction": ENTRY, "description": CLAUDE_ENTRY}

    assert parse_model_pin(document, key="extraction").backend == "codex-cli"
    assert parse_model_pin(document, key="description").backend == "claude-api"
    assert isinstance(model_client_for(parse_model_pin(document, key="extraction")), CodexCliClient)
    assert isinstance(model_client_for(parse_model_pin(document, key="description")), ClaudeApiClient)


def test_a_pin_without_any_backend_key_keeps_the_codex_cli() -> None:
    pin = parse_model_pin({"extraction": ENTRY, "description": ENTRY}, key="description")

    assert pin == CodexModelPin("gpt-pinned", "medium", "", backend="codex-cli")


@pytest.mark.parametrize("backend", ["deepseek", "", None, ["claude-api"]])
def test_an_unknown_entry_backend_fails_closed(backend: Any) -> None:
    with pytest.raises(ModelPinError):
        parse_model_pin({"extraction": {**ENTRY, "backend": backend}})


@pytest.mark.parametrize("effort", ["minimal", "none"])
def test_a_claude_pin_takes_only_the_api_efforts(effort: str) -> None:
    with pytest.raises(ModelPinError, match="does not take"):
        parse_model_pin({"extraction": {**CLAUDE_ENTRY, "reasoning_effort": effort}})


def test_adjudication_runs_on_the_description_pin_while_it_is_on_codex() -> None:
    document = {"extraction": ENTRY, "description": {"model": "gpt-description", "reasoning_effort": "low"}}

    assert adjudication_model_pin(document) == parse_model_pin(document, key="description")


def test_adjudication_needs_its_own_codex_entry_once_the_description_pin_moves() -> None:
    moved = {"extraction": ENTRY, "description": CLAUDE_ENTRY}

    with pytest.raises(ModelPinError, match="adjudication"):
        adjudication_model_pin(moved)
    pin = adjudication_model_pin({**moved, "adjudication": {"model": "gpt-adjudicator", "reasoning_effort": "high"}})
    assert (pin.model, pin.backend) == ("gpt-adjudicator", "codex-cli")
    with pytest.raises(ModelPinError, match="must be on"):
        adjudication_model_pin({**moved, "adjudication": CLAUDE_ENTRY})


def test_adjudication_stays_on_codex_with_web_search_when_the_description_pin_is_on_the_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoscout.description_adjudication import CodexDescriptionAdjudicationAdapter

    pin_file = tmp_path / "codex-model-pin.json"
    pin_file.write_text(
        json.dumps(
            {
                "extraction": ENTRY,
                "description": CLAUDE_ENTRY,
                "adjudication": {"model": "gpt-adjudicator", "reasoning_effort": "high"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(codex_model_pin, "DEFAULT_PIN_PATH", pin_file)
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: Any) -> Any:
        commands.append(command)
        raise FileNotFoundError("codex")

    adapter = CodexDescriptionAdjudicationAdapter(runner=runner)
    with pytest.raises(Exception, match="spawn failed"):
        adapter.adjudicate({"identity_key": "a work|an author", "title": "A Work"})

    command = commands[0]
    assert command[:2] == ["codex", "exec"]
    assert command[command.index("-m") + 1] == "gpt-adjudicator"
    assert "tools.web_search=true" in command


# -- The request -------------------------------------------------------------------


def test_the_request_carries_the_key_header_the_pinned_model_and_the_input_files(
    tmp_path: Path, key_file: Path
) -> None:
    (tmp_path / "items.json").write_text('[{"candidate_id": "c1"}]', encoding="utf-8")
    api = FakeApi(message('{"ok": true}'))

    result = _client(api).run(_request(tmp_path, input_files=("items.json",), max_output_tokens=4096), timeout=60)

    assert result.status is ModelCallStatus.ANSWERED
    sent = api.requests[0]
    assert (sent["host"], sent["method"], sent["path"]) == ("api.anthropic.com", "POST", "/v1/messages")
    assert sent["headers"] == {
        "x-api-key": SENTINEL_KEY,
        "anthropic-version": "2026-10-21",
        "content-type": "application/json",
    }
    body = sent["body"]
    assert body["model"] == "claude-haiku-5-5"
    assert body["max_tokens"] == 4096
    assert body["output_config"] == {"effort": "medium"}
    # Haiku 5.5 refuses sampling parameters, budgets and prefill.
    assert not {"temperature", "top_p", "top_k", "thinking"} & set(body)
    assert [message["role"] for message in body["messages"]] == ["user"]
    texts = _texts(body)
    assert texts[0] == '<file name="items.json">\n[{"candidate_id": "c1"}]\n</file>'
    assert texts[-1] == "Read items.json and answer."


def test_the_output_schema_is_sent_as_structured_output(tmp_path: Path, key_file: Path) -> None:
    from autoscout.original_edition import PROPOSAL_RESPONSE_SCHEMA

    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(PROPOSAL_RESPONSE_SCHEMA), encoding="utf-8")
    api = FakeApi(message('{"proposals": []}'))

    result = _client(api).run(_request(tmp_path, output_schema_path=schema_path), timeout=60)

    assert result.payload == {"proposals": []}
    output_format = api.requests[0]["body"]["output_config"]["format"]
    assert output_format["type"] == "json_schema"
    item = output_format["schema"]["properties"]["proposals"]["items"]
    # A list of types becomes an anyOf of single types; nothing else changes.
    assert item["properties"]["original_title"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert item["required"] == PROPOSAL_RESPONSE_SCHEMA["properties"]["proposals"]["items"]["required"]
    assert item["additionalProperties"] is False


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"a": {"type": "string", "minLength": 1}}, "additionalProperties": False},
        {"type": "object", "properties": {"a": {"type": "integer", "maximum": 3}}, "additionalProperties": False},
        {"type": "object", "properties": {"a": {"type": "string"}}},
        {"type": "object", "additionalProperties": {"type": "string"}},
    ],
)
def test_a_schema_structured_output_cannot_express_is_refused_before_sending(
    tmp_path: Path, key_file: Path, schema: dict[str, Any]
) -> None:
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    api = FakeApi()

    result = _client(api).run(_request(tmp_path, output_schema_path=schema_path), timeout=60)

    assert (result.status, result.failure) == (ModelCallStatus.REFUSED, ModelFailure.SCHEMA_UNSUPPORTED)
    assert api.connections == 0


def test_web_search_is_refused_before_anything_is_sent(tmp_path: Path, key_file: Path) -> None:
    api = FakeApi()

    result = _client(api).run(_request(tmp_path, web_search=True), timeout=60)

    assert (result.status, result.failure) == (ModelCallStatus.REFUSED, ModelFailure.WEB_SEARCH_UNSUPPORTED)
    assert api.connections == 0


def test_a_follow_up_resends_the_earlier_exchange(tmp_path: Path, key_file: Path) -> None:
    (tmp_path / "capture.json").write_text("{}", encoding="utf-8")
    first = _request(tmp_path, prompt="FIRST", input_files=("capture.json",))
    api = FakeApi(message('{"entries": [1]}'))

    _client(api).run(
        _request(
            tmp_path,
            prompt="AGAIN",
            working_dir=None,
            resume_session_id="",
            follows=PriorExchange(first, '{"entries": []}'),
        ),
        timeout=60,
    )

    messages = api.requests[0]["body"]["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"][-1]["text"] == "FIRST"
    assert messages[1]["content"] == [{"type": "text", "text": '{"entries": []}'}]
    assert messages[2]["content"] == [{"type": "text", "text": "AGAIN"}]


def test_a_resume_without_the_earlier_exchange_is_refused(tmp_path: Path, key_file: Path) -> None:
    api = FakeApi()

    result = _client(api).run(_request(tmp_path, resume_session_id="session-1"), timeout=60)

    assert (result.status, result.failure) == (ModelCallStatus.REFUSED, ModelFailure.UNSUPPORTED_REQUEST)
    assert api.connections == 0


# -- The answer --------------------------------------------------------------------


def test_the_answer_is_the_text_after_thinking_written_where_codex_would_write_it(
    tmp_path: Path, key_file: Path
) -> None:
    api = FakeApi(message('```json\n{"ok": true}\n```'))

    result = _client(api).run(_request(tmp_path), timeout=60)

    assert result.status is ModelCallStatus.ANSWERED
    assert result.payload == {"ok": True}
    assert json.loads((tmp_path / "response.json").read_text(encoding="utf-8")) == {"ok": True}
    assert result.served_model == "claude-haiku-5-5"
    assert (result.usage.input, result.usage.output) == (1200, 340)  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("stop_reason", "failure"),
    [("max_tokens", ModelFailure.OUTPUT_TRUNCATED), ("refusal", ModelFailure.MODEL_REFUSED)],
)
def test_an_answer_that_did_not_end_its_turn_is_not_an_answer(
    tmp_path: Path, key_file: Path, stop_reason: str, failure: ModelFailure
) -> None:
    api = FakeApi(message('{"ok": true}', stop_reason=stop_reason))

    result = _client(api).run(_request(tmp_path), timeout=60)

    assert (result.status, result.failure) == (ModelCallStatus.UNREADABLE_RESPONSE, failure)
    assert result.payload is None


# -- Failures ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "failure"),
    [
        (error(401, "authentication_error", "invalid x-api-key"), ModelFailure.KEY_REJECTED),
        (error(403, "permission_error", "no access"), ModelFailure.KEY_REJECTED),
        (error(402, "billing_error", "credit balance is too low"), ModelFailure.USAGE_LIMIT),
        (error(404, "not_found_error", "model: claude-haiku-9"), ModelFailure.MODEL_UNAVAILABLE),
        (
            error(429, "rate_limit_error", "spend cap", details={"error_code": "enforced_spend_limit_reached"}),
            ModelFailure.USAGE_LIMIT,
        ),
        (error(400, "invalid_request_error", "prompt is too long"), ModelFailure.BAD_REQUEST),
    ],
)
def test_an_error_that_a_retry_cannot_cure_fails_at_once_with_its_kind(
    tmp_path: Path, key_file: Path, reply: FakeResponse, failure: ModelFailure
) -> None:
    api = FakeApi(reply)

    result = _client(api).run(_request(tmp_path), timeout=60)

    assert (result.status, result.failure, result.returncode) == (ModelCallStatus.EXIT_NONZERO, failure, reply.status)
    assert len(api.requests) == 1
    assert result.stderr.startswith(f"HTTP {reply.status} ")


def test_a_rate_limit_with_retry_after_is_retried_and_one_that_lasts_is_a_usage_limit(
    tmp_path: Path, key_file: Path
) -> None:
    limited = error(429, "rate_limit_error", "slow down", headers={"retry-after": "3"})
    waits: list[float] = []
    api = FakeApi(limited, message('{"ok": true}'))

    recovered = ClaudeApiClient(connection_factory=api, sleep=waits.append).run(_request(tmp_path), timeout=60)

    assert recovered.status is ModelCallStatus.ANSWERED
    assert waits == [3.0]
    api = FakeApi(limited, limited, limited)
    lasting = _client(api).run(_request(tmp_path), timeout=60)
    assert (lasting.status, lasting.failure) == (ModelCallStatus.EXIT_NONZERO, ModelFailure.USAGE_LIMIT)
    assert len(api.requests) == 3


def test_an_overloaded_service_is_retried_then_reported(tmp_path: Path, key_file: Path) -> None:
    api = FakeApi(error(529, "overloaded_error"), error(500, "api_error"), error(529, "overloaded_error"))

    result = _client(api).run(_request(tmp_path), timeout=60)

    assert (result.status, result.failure) == (ModelCallStatus.EXIT_NONZERO, ModelFailure.SERVICE_ERROR)
    assert len(api.requests) == 3


def test_a_socket_timeout_is_a_timeout(tmp_path: Path, key_file: Path) -> None:
    api = FakeApi(TimeoutError("timed out"))

    result = _client(api).run(_request(tmp_path), timeout=60)

    assert result.status is ModelCallStatus.TIMEOUT


class StepClock:
    """A monotonic clock that advances by *step* on every reading."""

    def __init__(self, start: float = 100.0, step: float = 0.0) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


class FakeSocket:
    def __init__(self) -> None:
        self.timeouts: list[float] = []
        self.shut = threading.Event()

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def shutdown(self, _how: int) -> None:
        self.shut.set()


def test_the_connection_gets_what_is_left_of_the_timeout(tmp_path: Path, key_file: Path) -> None:
    api = FakeApi(message('{"ok": true}'))
    readings = iter([100.0, 104.0])
    client = ClaudeApiClient(connection_factory=api, monotonic=lambda: next(readings, 104.0))

    client.run(_request(tmp_path), timeout=30)

    assert api.requests[0]["timeout"] == pytest.approx(26.0)


class TricklingResponse(FakeResponse):
    """Sends its body ten bytes at a time, each read taking a tick of the injected clock."""

    def read(self, amount: int | None = None) -> bytes:
        return super().read(10)


def test_a_body_that_trickles_past_the_deadline_is_a_timeout(tmp_path: Path, key_file: Path) -> None:
    """Each socket operation is short, but the whole request outlasts the caller's deadline."""
    sock = FakeSocket()
    answer = message('{"ok": true}')
    slow = TricklingResponse(200, answer._body)

    def connection(host: str, timeout: float) -> FakeConnection:
        made = FakeConnection(FakeApi(slow), host, timeout)
        made.sock = sock  # type: ignore[attr-defined]
        return made

    result = ClaudeApiClient(connection_factory=connection, monotonic=StepClock(100.0, step=1.0)).run(
        _request(tmp_path), timeout=30
    )

    assert result.status is ModelCallStatus.TIMEOUT
    # The socket got what was left of the deadline before every step, never more.
    assert sock.timeouts and sock.timeouts == sorted(sock.timeouts, reverse=True)
    assert all(0 < value <= 30 for value in sock.timeouts)


def test_slow_headers_past_the_deadline_are_a_timeout(tmp_path: Path, key_file: Path) -> None:
    clock = StepClock(100.0)

    def late(_body: dict[str, Any], _connection: FakeConnection) -> Any:
        clock.now += 31.0
        return message('{"ok": true}')

    result = ClaudeApiClient(connection_factory=FakeApi(late), monotonic=clock).run(_request(tmp_path), timeout=30)

    assert result.status is ModelCallStatus.TIMEOUT


class DetachingConnection(FakeConnection):
    """``Connection: close``: after the headers the response owns the socket, and the connection has none."""

    def __init__(self, api: FakeApi, host: str, timeout: float) -> None:
        super().__init__(api, host, timeout)
        self.sock: Any = FakeSocket()
        self.started = threading.Event()

    def getresponse(self) -> FakeResponse:
        sock = self.sock
        self.sock = None
        started = self.started

        class Blocking(FakeResponse):
            def read(self, amount: int | None = None) -> bytes:
                started.set()
                sock.shut.wait(10)
                raise OSError("socket shut down")

        return Blocking(200, b"")


def test_abort_interrupts_a_body_read_after_the_connection_gave_up_its_socket(tmp_path: Path, key_file: Path) -> None:
    connections: list[DetachingConnection] = []

    def factory(host: str, timeout: float) -> DetachingConnection:
        connections.append(DetachingConnection(FakeApi(), host, timeout))
        return connections[-1]

    client = ClaudeApiClient(connection_factory=factory)
    results: list[Any] = []
    worker = threading.Thread(target=lambda: results.append(client.run(_request(tmp_path), timeout=60)))
    worker.start()
    while not connections:
        threading.Event().wait(0.01)
    assert connections[0].started.wait(10)
    client.abort()
    worker.join(10)

    assert not worker.is_alive()
    assert results[0].failure is ModelFailure.ABORTED


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (None, "is missing"),
        ("OTHER_KEY=x\n", "has no ANTHROPIC_API_KEY"),
        ("ANTHROPIC_API_KEY=bad key\nwith newline\n", "malformed"),
    ],
)
def test_an_unusable_key_file_is_a_typed_failure_that_names_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None, reason: str
) -> None:
    path = tmp_path / "claude.env"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(model_client_module, "CLAUDE_KEY_FILE", path)
    api = FakeApi()

    result = _client(api).run(_request(tmp_path), timeout=60)

    assert (result.status, result.failure) == (ModelCallStatus.NOT_STARTED, ModelFailure.KEY_MISSING)
    assert "~/.config/autoscout/claude.env" in result.stderr and reason in result.stderr
    assert api.connections == 0


def test_the_environment_key_is_never_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL_KEY)
    monkeypatch.setattr(model_client_module, "CLAUDE_KEY_FILE", tmp_path / "absent.env")

    result = _client(FakeApi()).run(_request(tmp_path), timeout=60)

    assert result.failure is ModelFailure.KEY_MISSING


def test_the_key_reaches_only_the_request_header(tmp_path: Path, install_api: Callable[..., FakeApi]) -> None:
    """A sentinel key appears in no result field, diagnostic, ledger entry or written file."""
    scenarios = [
        message('{"ok": true}'),
        error(401, "authentication_error", "invalid x-api-key"),
        # An upstream error that echoes the key back.
        error(400, "invalid_request_error", f"x-api-key {SENTINEL_KEY} is not allowed here"),
        OSError("connection reset"),
        message('{"ok": true}', stop_reason="max_tokens"),
        # An answer that echoes the key back.
        message(json.dumps({"ok": SENTINEL_KEY})),
    ]
    results = []
    with recording_model_usage() as ledger:
        for index, reply in enumerate(scenarios):
            api = install_api(reply, reply, reply)
            results.append(
                ClaudeApiClient().run(_request(tmp_path, response_path=tmp_path / f"r{index}.json"), timeout=60)
            )
            assert all(sent["headers"]["x-api-key"] == SENTINEL_KEY for sent in api.requests)
    for result in results:
        assert SENTINEL_KEY not in repr(result)
    assert SENTINEL_KEY not in repr(ledger.drain())
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.name != "claude.env":
            assert SENTINEL_KEY not in path.read_text(encoding="utf-8", errors="replace"), path.name


# -- Publishers Weekly guard -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Source: https://www.publishersweekly.com/9780593241035",
        "see PUBLISHERSWEEKLY.COM for reviews",
        "http://blogs.publishersweekly.com/x",
    ],
)
def test_a_request_that_mentions_publishers_weekly_is_refused_before_anything_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    (tmp_path / "work_items.json").write_text(json.dumps({"evidence": text}), encoding="utf-8")
    monkeypatch.setattr(model_client_module, "CLAUDE_KEY_FILE", tmp_path / "absent.env")
    api = FakeApi()

    from_file = _client(api).run(_request(tmp_path, input_files=("work_items.json",)), timeout=60)
    from_prompt = _client(api).run(_request(tmp_path, prompt=text), timeout=60)
    from_history = _client(api).run(
        _request(tmp_path, prompt="again", follows=PriorExchange(_request(tmp_path), text)), timeout=60
    )

    for result in (from_file, from_prompt, from_history):
        assert (result.status, result.failure) == (ModelCallStatus.REFUSED, ModelFailure.PW_TEXT_NOT_PERMITTED)
    assert api.connections == 0


@pytest.mark.parametrize("text", ["notpublishersweekly.com", "Publishers Weekly said", "publishersweekly.company"])
def test_a_look_alike_is_not_publishers_weekly(text: str) -> None:
    assert not model_client_module.mentions_publishers_weekly(text)


# -- Abort and usage ---------------------------------------------------------------


def test_abort_closes_a_running_request_and_refuses_new_ones(tmp_path: Path, key_file: Path) -> None:
    started = threading.Event()

    def hang(_body: dict[str, Any], connection: FakeConnection) -> Any:
        started.set()
        connection.closed.wait(10)
        return OSError("closed")

    client = _client(FakeApi(hang))
    results: list[Any] = []
    worker = threading.Thread(target=lambda: results.append(client.run(_request(tmp_path), timeout=60)))
    worker.start()
    assert started.wait(10)
    client.abort()
    worker.join(10)

    assert results[0].failure is ModelFailure.ABORTED
    assert client.run(_request(tmp_path), timeout=60).failure is ModelFailure.ABORTED


def test_every_answered_call_records_its_counts_while_a_ledger_is_open(tmp_path: Path, key_file: Path) -> None:
    api = FakeApi(message('{"ok": true}'), message('{"ok": true}'))
    _client(api).run(_request(tmp_path, purpose="before"), timeout=60)

    with recording_model_usage() as ledger:
        _client(api).run(_request(tmp_path, purpose="extraction"), timeout=60)

    assert ledger.drain() == [
        {
            "purpose": "extraction",
            "backend": "claude-api",
            "model": "claude-haiku-5-5",
            "input": 1200,
            "output": 340,
            "cache_read_input": 0,
            "cache_creation_input": 0,
        }
    ]
    assert ledger.totals()["calls"] == 1


def test_the_workflow_log_keeps_the_per_call_counts() -> None:
    """The log sanitizer drops any key that says "token"; the counts must survive it."""
    from autoscout.weekly_workflow import _sanitize_log_entry

    calls = [{"purpose": "extraction", "backend": "claude-api", "model": "claude-haiku-5-5", "input": 9, "output": 2}]

    assert _sanitize_log_entry({"stage": "codex", "status": "ok", "model_calls": calls})["model_calls"] == calls


def test_codex_failures_come_from_the_cli_markers(tmp_path: Path) -> None:
    def failing(stderr: str) -> Any:
        return lambda *_a, **_k: type("Completed", (), {"returncode": 1, "stdout": "", "stderr": stderr})()

    request = _request(tmp_path)
    unavailable = CodexCliClient().run(request, timeout=1, runner=failing("model_not_found"))
    limited = CodexCliClient().run(request, timeout=1, runner=failing("You've hit your usage limit."))
    other = CodexCliClient().run(request, timeout=1, runner=failing("boom"))

    assert unavailable.failure is ModelFailure.MODEL_UNAVAILABLE
    assert limited.failure is ModelFailure.USAGE_LIMIT
    assert other.failure is ModelFailure.NONE


def test_no_key_shaped_value_is_printed_by_the_refusal_diagnostics(tmp_path: Path, key_file: Path) -> None:
    result = _client(FakeApi()).run(_request(tmp_path, web_search=True), timeout=60)

    assert not re.search(r"sk-ant", result.stderr)


def test_a_request_that_carries_a_pm_deal_logline_is_refused_before_anything_is_sent(
    tmp_path: Path, key_file: Path
) -> None:
    from autoscout.description_enrichment import _encode_work_items

    items = _encode_work_items([{"identity_key": "k", "title": "T", "deal_logline": "A history of salt roads."}])
    (tmp_path / "work_items.json").write_text(json.dumps(items), encoding="utf-8")
    api = FakeApi()

    result = _client(api).run(_request(tmp_path, input_files=("work_items.json",)), timeout=60)

    assert (result.status, result.failure) == (ModelCallStatus.REFUSED, ModelFailure.PM_TEXT_NOT_PERMITTED)
    assert api.connections == 0


# -- Review round 2 ----------------------------------------------------------------


class TricklingConnection(FakeConnection):
    """A server that keeps sending a byte now and then, inside one ``getresponse()`` or ``read()``.

    ``http.client`` does several socket reads inside each of those calls, so
    the client's own checks between calls never run; only a shutdown of the
    socket ends the call.
    """

    def __init__(self, api: FakeApi, host: str, timeout: float, *, in_headers: bool) -> None:
        super().__init__(api, host, timeout)
        self.sock: Any = FakeSocket()
        self.in_headers = in_headers

    def _trickle(self) -> None:
        while not self.sock_ref.shut.wait(0.02):
            pass
        raise OSError("socket shut down")

    def getresponse(self) -> FakeResponse:
        self.sock_ref = self.sock
        if self.in_headers:
            self._trickle()
        connection = self

        class Trickling(FakeResponse):
            def read(self, amount: int | None = None) -> bytes:
                connection._trickle()
                return b""

        return Trickling(200, b"")


@pytest.mark.parametrize("in_headers", [True, False])
def test_a_server_that_keeps_trickling_is_cut_at_the_deadline(tmp_path: Path, key_file: Path, in_headers: bool) -> None:
    import time

    made: list[TricklingConnection] = []

    def factory(host: str, timeout: float) -> TricklingConnection:
        made.append(TricklingConnection(FakeApi(), host, timeout, in_headers=in_headers))
        return made[-1]

    started = time.monotonic()
    result = ClaudeApiClient(connection_factory=factory).run(_request(tmp_path), timeout=0.3)
    elapsed = time.monotonic() - started

    assert result.status is ModelCallStatus.TIMEOUT
    assert elapsed < 1.5
    assert len(made) == 1  # a timeout is never retried


def test_dropped_connections_and_overloads_share_one_retry_count(tmp_path: Path, key_file: Path) -> None:
    from autoscout.model_client import CLAUDE_MAX_RETRIES

    overloaded = error(529, "overloaded_error")
    api = FakeApi(OSError("reset"), overloaded, OSError("reset"), overloaded, OSError("reset"), message("{}"))

    result = _client(api).run(_request(tmp_path), timeout=60)

    assert len(api.requests) == 1 + CLAUDE_MAX_RETRIES
    assert result.status is not ModelCallStatus.ANSWERED


# -- Review round 3 ----------------------------------------------------------------


class StallingSocket(FakeSocket):
    """A TCP socket whose connect never completes until it is shut down, or its own timeout passes."""

    def __init__(self) -> None:
        super().__init__()
        self.timeout = 10.0
        self.closed = False

    def settimeout(self, value: float) -> None:
        super().settimeout(value)
        self.timeout = value

    def connect(self, _address: Any) -> None:
        if self.shut.wait(self.timeout):
            raise OSError("connect interrupted")
        raise TimeoutError("connect timed out")

    def close(self) -> None:
        self.closed = True


class StallingTls(FakeSocket):
    """A TLS socket whose handshake never completes until it is shut down."""

    def __init__(self) -> None:
        super().__init__()
        self.handshaking = threading.Event()

    def do_handshake(self) -> None:
        self.handshaking.set()
        self.shut.wait(10)
        raise OSError("handshake interrupted")

    def close(self) -> None:
        pass


class RecordingContext:
    def __init__(self, tls: StallingTls) -> None:
        self.tls = tls
        self.wraps: list[dict[str, Any]] = []

    def wrap_socket(self, sock: Any, **kwargs: Any) -> StallingTls:
        self.wraps.append({"sock": sock, **kwargs})
        return self.tls


def _real_connection_factory(made: list[Any]) -> Callable[[str, float], Any]:
    import http.client

    def factory(host: str, timeout: float) -> Any:
        made.append(http.client.HTTPSConnection(host, timeout=timeout))
        return made[-1]

    return factory


def _install_network(
    monkeypatch: pytest.MonkeyPatch, addresses: int, connect_ok: bool
) -> tuple[list[Any], RecordingContext]:
    sockets: list[Any] = []

    def new_socket(_family: int, _kind: int, _proto: int) -> Any:
        sock = FakeSocket() if connect_ok else StallingSocket()
        if connect_ok:
            sock.connect = lambda _address: None  # type: ignore[attr-defined]
            sock.close = lambda: None  # type: ignore[attr-defined]
        sockets.append(sock)
        return sock

    resolved = [(10, 1, 6, "", ("::1", 443, 0, 0)), (2, 1, 6, "", ("127.0.0.1", 443))][:addresses]
    monkeypatch.setattr(model_client_module, "_resolve", lambda _host: resolved)
    monkeypatch.setattr(model_client_module, "_new_socket", new_socket)
    context = RecordingContext(StallingTls())
    monkeypatch.setattr(model_client_module, "_tls_context", lambda: context)
    return sockets, context


def test_two_stalled_addresses_share_one_deadline(
    tmp_path: Path, key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    sockets, _context = _install_network(monkeypatch, addresses=2, connect_ok=False)
    made: list[Any] = []

    started = time.monotonic()
    result = ClaudeApiClient(connection_factory=_real_connection_factory(made)).run(_request(tmp_path), timeout=0.5)
    elapsed = time.monotonic() - started

    assert result.status is ModelCallStatus.TIMEOUT
    assert elapsed < 0.85  # one budget, not one per address
    assert len(made) == 1 and sockets and sockets[0].timeouts[0] <= 0.5


def test_a_stalled_tls_handshake_ends_at_the_deadline(
    tmp_path: Path, key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    sockets, context = _install_network(monkeypatch, addresses=1, connect_ok=True)

    started = time.monotonic()
    result = ClaudeApiClient(connection_factory=_real_connection_factory([])).run(_request(tmp_path), timeout=0.3)

    assert result.status is ModelCallStatus.TIMEOUT
    assert time.monotonic() - started < 1.5
    # The handshake is deferred, so the TLS socket is registered before it starts.
    (wrap,) = context.wraps
    assert wrap == {"sock": sockets[0], "server_hostname": "api.anthropic.com", "do_handshake_on_connect": False}
    assert context.tls.shut.is_set()


def test_abort_ends_a_stalled_tls_handshake(tmp_path: Path, key_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _sockets, context = _install_network(monkeypatch, addresses=1, connect_ok=True)
    client = ClaudeApiClient(connection_factory=_real_connection_factory([]))
    results: list[Any] = []
    worker = threading.Thread(target=lambda: results.append(client.run(_request(tmp_path), timeout=60)))
    worker.start()
    assert context.tls.handshaking.wait(10)
    client.abort()
    worker.join(10)

    assert not worker.is_alive()
    assert results[0].failure is ModelFailure.ABORTED


def test_the_tls_context_keeps_certificate_and_hostname_verification() -> None:
    import ssl

    context = model_client_module._tls_context()

    assert context.check_hostname is True and context.verify_mode is ssl.CERT_REQUIRED


def test_a_schema_that_mentions_publishers_weekly_is_refused_before_connecting(tmp_path: Path, key_file: Path) -> None:
    schema = {
        "type": "object",
        "description": "Shaped like https://www.publishersweekly.com/review rows",
        "properties": {"a": {"type": "string"}},
        "required": ["a"],
        "additionalProperties": False,
    }
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    api = FakeApi()

    result = _client(api).run(_request(tmp_path, output_schema_path=schema_path), timeout=60)

    assert (result.status, result.failure) == (ModelCallStatus.REFUSED, ModelFailure.PW_TEXT_NOT_PERMITTED)
    assert api.connections == 0


def test_an_answer_that_echoes_the_key_is_discarded(tmp_path: Path, key_file: Path) -> None:
    result = _client(FakeApi(message(json.dumps({"ok": SENTINEL_KEY})))).run(_request(tmp_path), timeout=60)

    assert (result.status, result.failure) == (ModelCallStatus.UNREADABLE_RESPONSE, ModelFailure.KEY_ECHOED)
    assert result.payload is None and SENTINEL_KEY not in result.stderr
    assert not (tmp_path / "response.json").exists()


def _cut(response: FakeResponse, keep: int) -> FakeResponse:
    full = response._body
    return FakeResponse(200, full[:keep], {"content-length": str(len(full))})


def test_a_body_cut_short_is_a_dropped_connection_and_is_retried(tmp_path: Path, key_file: Path) -> None:
    from autoscout.model_client import CLAUDE_MAX_RETRIES

    whole = message('{"ok": true}')
    api = FakeApi(_cut(message('{"ok": true}'), 20), whole)

    result = _client(api).run(_request(tmp_path), timeout=60)

    assert result.status is ModelCallStatus.ANSWERED and len(api.requests) == 2
    api = FakeApi(*[_cut(message('{"ok": true}'), 20) for _ in range(5)])
    cut = _client(api).run(_request(tmp_path), timeout=60)
    assert (cut.status, cut.failure) == (ModelCallStatus.NOT_STARTED, ModelFailure.CONNECTION_FAILED)
    assert len(api.requests) == 1 + CLAUDE_MAX_RETRIES


def test_a_pm_logline_that_also_links_publishers_weekly_is_refused_as_pm_text(tmp_path: Path, key_file: Path) -> None:
    from autoscout.description_enrichment import _encode_work_items

    items = _encode_work_items(
        [{"identity_key": "k", "title": "T", "deal_logline": "As reviewed at https://www.publishersweekly.com/x."}]
    )
    (tmp_path / "work_items.json").write_text(json.dumps(items), encoding="utf-8")

    result = _client(FakeApi()).run(_request(tmp_path, input_files=("work_items.json",)), timeout=60)

    assert result.failure is ModelFailure.PM_TEXT_NOT_PERMITTED


# -- Review round 5 ----------------------------------------------------------------


class _ManualWatchdog:
    def __init__(self) -> None:
        self.actions: list[Callable[[], None]] = []

    def __call__(self, _seconds: float, action: Callable[[], None]) -> Any:
        self.actions.append(action)
        return self

    def cancel(self) -> None:
        pass


def _lost_file_reply(on_read: Callable[[], None]) -> FakeResponse:
    class LostFile(FakeResponse):
        def read(self, amount: int | None = None) -> bytes:
            on_read()
            # What http.client does once another thread closed the response.
            raise AttributeError("'NoneType' object has no attribute 'close'")

    return LostFile(200, b"")


def test_a_lost_response_file_after_the_deadline_is_a_timeout(tmp_path: Path, key_file: Path) -> None:
    watchdog = _ManualWatchdog()
    api = FakeApi(_lost_file_reply(lambda: watchdog.actions[-1]()))

    result = ClaudeApiClient(connection_factory=api, watchdog=watchdog).run(_request(tmp_path), timeout=60)

    assert result.status is ModelCallStatus.TIMEOUT


def test_a_lost_response_file_after_abort_is_aborted(tmp_path: Path, key_file: Path) -> None:
    holder: list[ClaudeApiClient] = []
    api = FakeApi(_lost_file_reply(lambda: holder[0].abort()))
    holder.append(ClaudeApiClient(connection_factory=api, watchdog=_ManualWatchdog()))

    result = holder[0].run(_request(tmp_path), timeout=60)

    assert (result.status, result.failure) == (ModelCallStatus.NOT_STARTED, ModelFailure.ABORTED)


@pytest.mark.parametrize(
    ("text", "failure"),
    [
        ("Your credit balance is too low to access the Anthropic API.", ModelFailure.USAGE_LIMIT),
        ("max_tokens: must be at most 128000", ModelFailure.BAD_REQUEST),
    ],
)
def test_a_400_for_a_low_credit_balance_is_a_usage_limit(
    tmp_path: Path, key_file: Path, text: str, failure: ModelFailure
) -> None:
    api = FakeApi(error(400, "invalid_request_error", text))

    result = _client(api).run(_request(tmp_path), timeout=60)

    assert (result.status, result.failure) == (ModelCallStatus.EXIT_NONZERO, failure)
    assert len(api.requests) == 1
