"""The one place that builds and runs a model call (Issues #1018, #1149).

Every model call of AutoScout used to assemble its own ``codex exec`` command
line: extraction and its empty-answer follow-up, description enrichment,
description adjudication, the description-model qualification, original-edition
completion and the Stage 0 probe.  Each repeated the sandbox mode, the
repository check, the model and effort flags and the output handling, so a
sandbox flag forgotten at one site was a security regression nobody would see,
and switching the model provider meant editing every site.

A caller now describes the call as a :class:`ModelRequest` and asks the client
for a finished call (:meth:`CodexCliClient.run`, :meth:`ClaudeApiClient.run`),
or, on the Codex CLI only, for the command line.  The Codex client always runs
the model read-only: no request field can widen the sandbox.  The Claude API
client gives the model no tool at all.

The backend is chosen per pin entry of ``configs/codex-model-pin.json``
(:attr:`CodexModelPin.backend`):

- ``codex-cli`` (the default): the Codex CLI on the operator's ChatGPT
  subscription.  The model reads the files named in the prompt from the
  working directory itself.
- ``claude-api`` (Issue #1149): direct HTTPS calls to the Claude API with the
  standard library.  The key is read from ``~/.config/autoscout/claude.env``
  and nowhere else, and it reaches nothing but the request header.  The files
  a request names in :attr:`ModelRequest.input_files` are read from the
  working directory and sent in the request.  The API is stateless, so a
  follow-up resends the earlier exchange (:attr:`ModelRequest.follows`).
  Web search is refused, and so is any request whose content mentions
  publishersweekly.com: the Publishers Weekly permission names the one
  provider that may receive its text, and this is not it.

Two callers supervise the Codex process themselves and take only the command
line: extraction, whose progress lease reads the Codex rollout while the call
runs, and the description-model qualification, which kills every running call
when a comparison is aborted.  On the Claude API they call
:meth:`ClaudeApiClient.run` instead, and :meth:`ClaudeApiClient.abort` closes
every running request.  Everything else calls ``run`` on either backend.

Each call that answered on the Claude API records its token counts into every
open :func:`recording_model_usage` ledger, so a weekly run can put the cost of
each call into its workflow log.  The Codex CLI reports no counts.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from autoscout.codex_model_pin import CLAUDE_API_BACKEND
from autoscout.codex_model_pin import CLAUDE_API_EFFORTS
from autoscout.codex_model_pin import CODEX_CLI_BACKEND
from autoscout.codex_model_pin import DEFAULT_BACKEND
from autoscout.codex_model_pin import ModelPinError
from autoscout.codex_model_pin import load_description_model_pin
from autoscout.codex_model_pin import model_unavailable_in
from autoscout.codex_model_pin import usage_limit_in
from autoscout.secret_redaction import redact_secrets

# A web-search tool is a model tool, so the read-only sandbox still permits it.
_WEB_SEARCH_CONFIG = "tools.web_search=true"
_READ_ONLY = "read-only"


@dataclass(frozen=True)
class ModelRequest:
    """One model call: what to ask, where to answer, and what the model may use.

    ``isolated`` keeps the operator's Codex home out of the call and does not
    persist its session.  ``resume_session_id`` continues an earlier Codex
    session instead of starting a new one; a resumed call keeps that session's
    working directory, so ``working_dir`` and ``output_schema_path`` stay unset
    there.

    The last four fields change nothing on the Codex CLI, whose command line
    stays exactly what it was before them:

    - ``input_files`` names the files in ``working_dir`` the prompt tells the
      model to read.  The Codex CLI reads them itself; the Claude API backend
      sends their content in the request.
    - ``max_output_tokens`` caps the answer on the Claude API, thinking
      included; 0 takes :data:`CLAUDE_DEFAULT_MAX_OUTPUT_TOKENS`.
    - ``follows`` is the earlier exchange a follow-up continues.  The Claude
      API keeps no session, so its backend resends that exchange; the Codex
      CLI continues ``resume_session_id`` instead.
    - ``purpose`` names the stage in the usage ledger.
    """

    prompt: str
    response_path: Path
    model: str = ""
    reasoning_effort: str = ""
    working_dir: Path | None = None
    output_schema_path: Path | None = None
    web_search: bool = False
    isolated: bool = False
    disabled_features: tuple[str, ...] = ()
    resume_session_id: str = ""
    input_files: tuple[str, ...] = ()
    max_output_tokens: int = 0
    follows: PriorExchange | None = None
    purpose: str = ""


@dataclass(frozen=True)
class PriorExchange:
    """An earlier request and the answer it got, for a follow-up on a stateless API."""

    request: ModelRequest
    answer: str


class ModelCallStatus(StrEnum):
    """How one finished model call ended."""

    ANSWERED = "answered"
    TIMEOUT = "timeout"
    NOT_STARTED = "not_started"
    EXIT_NONZERO = "exit_nonzero"
    UNREADABLE_RESPONSE = "unreadable_response"
    # The client refused the request before anything left this machine.
    REFUSED = "refused"


class ModelFailure(StrEnum):
    """Why a call failed, in terms every stage can act on, whichever backend ran it."""

    NONE = ""
    # The model cannot be served: a retired or unknown model, or one the
    # account cannot use.  No other call of the run would fare better.
    MODEL_UNAVAILABLE = "model_unavailable"
    # The account has no capacity left: the Codex usage limit, exhausted API
    # credit, the API spend cap, or a rate limit that outlasted its retries.
    USAGE_LIMIT = "usage_limit"
    # The Claude API key file is missing, has no key, or holds an unusable one.
    KEY_MISSING = "key_missing"
    # The API rejected the key (401) or the key lacks access (403).
    KEY_REJECTED = "key_rejected"
    # The request mentions publishersweekly.com, which this backend may not receive.
    PW_TEXT_NOT_PERMITTED = "pw_text_not_permitted"
    # The request carries a Publishers Marketplace deal logline, which this
    # backend may not receive either (docs/agents/contracts/pm-precollection.md).
    PM_TEXT_NOT_PERMITTED = "pm_text_not_permitted"
    WEB_SEARCH_UNSUPPORTED = "web_search_unsupported"
    # The output schema uses a feature the API's structured output lacks.
    SCHEMA_UNSUPPORTED = "schema_unsupported"
    # A request this backend cannot express: no model, an unknown effort, an
    # input file it cannot read, a resume without the earlier exchange.
    UNSUPPORTED_REQUEST = "unsupported_request"
    BAD_REQUEST = "bad_request"
    SERVICE_ERROR = "service_error"
    CONNECTION_FAILED = "connection_failed"
    OUTPUT_TRUNCATED = "output_truncated"
    MODEL_REFUSED = "model_refused"
    ABORTED = "aborted"
    # The answer contained the API key; it is discarded, never written or returned.
    KEY_ECHOED = "key_echoed"


@dataclass(frozen=True)
class ModelUsage:
    """The token counts one answered Claude API call reported."""

    input: int
    output: int
    cache_read_input: int = 0
    cache_creation_input: int = 0

    def as_record(self) -> dict[str, int]:
        # No key says "token": the workflow log sanitizer drops such keys.
        return {
            "input": self.input,
            "output": self.output,
            "cache_read_input": self.cache_read_input,
            "cache_creation_input": self.cache_creation_input,
        }


@dataclass(frozen=True)
class ModelCallResult:
    """The outcome of one call, on either backend.

    ``payload`` is the parsed JSON answer and is set only when ``status`` is
    ``ANSWERED``; the caller validates its shape.  ``spawn_error`` is set only
    for a Codex ``NOT_STARTED``.  ``failure`` names why a call failed; the
    Codex client reads it from the CLI's stderr markers, the Claude API client
    from the HTTP status and error type.  On the Claude API ``returncode`` is
    the HTTP status of a failed response, ``stderr`` a bounded diagnostic
    that never carries a request header, ``usage`` the token counts and
    ``served_model`` the model the API says answered.
    """

    status: ModelCallStatus
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    payload: Any = None
    spawn_error: OSError | None = None
    failure: ModelFailure = ModelFailure.NONE
    usage: ModelUsage | None = None
    served_model: str = ""


class ModelBackendError(ModelPinError):
    """The configured backend is not one this client can run."""


class CodexCliClient:
    """The Codex CLI backend: ``codex exec`` in the read-only sandbox."""

    backend = CODEX_CLI_BACKEND

    def command(self, request: ModelRequest) -> list[str]:
        """Return the ``codex exec`` command line for *request*."""
        command = ["codex", "exec"]
        if request.resume_session_id:
            command += ["resume", request.resume_session_id]
        if request.web_search:
            command += ["-c", _WEB_SEARCH_CONFIG]
        if request.resume_session_id:
            # ``codex exec resume`` takes no --sandbox flag; the config key
            # sets the same mode.
            command += ["-c", f'sandbox_mode="{_READ_ONLY}"']
        else:
            command += ["--sandbox", _READ_ONLY]
        command.append("--skip-git-repo-check")
        if request.isolated:
            command += ["--ephemeral", "--ignore-user-config"]
        for feature in request.disabled_features:
            command += ["--disable", feature]
        if request.model:
            # ``-m`` takes the next argument as its value, so a strange model
            # name can only ever be a bad model name, never another flag.
            command += ["-m", request.model]
        if request.reasoning_effort:
            # Quoted so the CLI parses it as a TOML string; every caller passes
            # a shape-checked effort, so the quoting cannot be broken.
            command += ["-c", f'model_reasoning_effort="{request.reasoning_effort}"']
        command += ["--output-last-message", str(request.response_path)]
        if request.working_dir is not None:
            command += ["-C", str(request.working_dir)]
        if request.output_schema_path is not None:
            command += ["--output-schema", str(request.output_schema_path)]
        command.append(request.prompt)
        return command

    def run(
        self,
        request: ModelRequest,
        *,
        timeout: float,
        runner: Callable[..., Any] | None = None,
    ) -> ModelCallResult:
        """Run *request* to completion within *timeout* seconds.

        ``runner`` replaces :func:`subprocess.run` and is called with the same
        arguments; it is the seam tests use.
        """
        run = runner if runner is not None else subprocess.run
        try:
            completed = run(self.command(request), capture_output=True, text=True, check=False, timeout=timeout)
        except (subprocess.TimeoutExpired, TimeoutError):
            # TimeoutError is an OSError, so it is caught first.
            return ModelCallResult(ModelCallStatus.TIMEOUT)
        except OSError as exc:
            return ModelCallResult(ModelCallStatus.NOT_STARTED, spawn_error=exc)
        returncode = completed.returncode
        stdout = str(getattr(completed, "stdout", "") or "")
        stderr = str(getattr(completed, "stderr", "") or "")
        if returncode != 0:
            return ModelCallResult(
                ModelCallStatus.EXIT_NONZERO, returncode, stdout, stderr, failure=codex_failure(stderr)
            )
        try:
            payload = json.loads(request.response_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ModelCallResult(ModelCallStatus.UNREADABLE_RESPONSE, returncode, stdout, stderr)
        return ModelCallResult(ModelCallStatus.ANSWERED, returncode, stdout, stderr, payload)


def codex_failure(stderr: str) -> ModelFailure:
    """Return what a failed Codex call's stderr says about why it failed."""
    if model_unavailable_in(stderr):
        return ModelFailure.MODEL_UNAVAILABLE
    if usage_limit_in(stderr):
        return ModelFailure.USAGE_LIMIT
    return ModelFailure.NONE


# -- Claude API ----------------------------------------------------------------

CLAUDE_API_HOST = "api.anthropic.com"
CLAUDE_API_PATH = "/v1/messages"
CLAUDE_API_VERSION = "2026-10-21"
CLAUDE_KEY_NAME = "ANTHROPIC_API_KEY"
# The only place the key is read from.  The environment is deliberately not
# consulted: an agent session that launches a run may carry its own key there.
CLAUDE_KEY_FILE = Path.home() / ".config" / "autoscout" / "claude.env"
CLAUDE_KEY_FILE_DISPLAY = "~/.config/autoscout/claude.env"
# Room for the adaptive thinking the model does before it answers, plus a
# description batch or an original-edition proposal list.  Extraction asks for
# more through ``max_output_tokens``.
CLAUDE_DEFAULT_MAX_OUTPUT_TOKENS = 16_000
# A non-streaming answer of the largest allowed size is well under this.
CLAUDE_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# Retries after a rate limit with a retry-after, an overloaded or failing
# service, or a dropped connection.  Each waits inside the caller's timeout.
CLAUDE_MAX_RETRIES = 2
CLAUDE_RETRY_BACKOFF_SECONDS = 2.0
_DIAGNOSTIC_LIMIT = 300
_KEY_SHAPE = re.compile(r"^[A-Za-z0-9_\-]{8,512}$")
_FILE_NAME_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504, 529})
_SPEND_CAP_CODE = "enforced_spend_limit_reached"

# The Publishers Weekly permission names the one provider that may receive PW
# text (docs/agents/contracts/pw-precollection.md); this backend is not it.
# Any mention of the host, a subdomain or a link included, refuses the call.
_PW_HOST = re.compile(r"(?<![a-z0-9-])publishersweekly\.com(?![a-z0-9-])", re.IGNORECASE)
# The PM permission names the pinned Codex model and DeepSeek as the only
# receivers of a deal logline.  The work-item encoder sends it under this key,
# and the PM logline prompt names it, so any request carrying one is refused.
_PM_DEAL_TEXT = re.compile(r"deal_logline", re.IGNORECASE)
# One body read waits at most this many bytes before the deadline is checked again.
_READ_CHUNK_BYTES = 65_536

_SYSTEM_PROMPT = (
    "You have no tools. Every file the task names is supplied above the task in a <file> element whose "
    "name attribute is the file name; read it there. Reply with the JSON the task asks for and nothing "
    "else: no Markdown fence and no prose around it."
)
_FENCE = re.compile(r"^```[A-Za-z]*\s*\n(.*)\n```$", re.DOTALL)

# The JSON Schema subset the API's structured output takes.  A schema using
# anything else is refused, so no constraint is ever dropped silently.
_SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "anyOf",
        "allOf",
        "$ref",
        "$defs",
        "definitions",
        "description",
        "title",
        "format",
    }
)
_SCHEMA_FORMATS = frozenset(
    {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
)


class _Refusal(Exception):
    """A request this backend refuses before anything is sent."""

    def __init__(self, failure: ModelFailure, detail: str) -> None:
        super().__init__(detail)
        self.failure = failure
        self.detail = detail


def carries_pm_deal_text(text: str) -> bool:
    """Return whether *text* carries a Publishers Marketplace deal logline."""
    return bool(_PM_DEAL_TEXT.search(str(text or "")))


def mentions_publishers_weekly(text: str) -> bool:
    """Return whether *text* mentions the publishersweekly.com host or a subdomain of it."""
    return bool(_PW_HOST.search(str(text or "")))


def claude_output_schema(schema: Any) -> dict[str, Any]:
    """Return *schema* in the form the API's structured output takes, or raise ``ValueError``.

    One adaptation is made, and it changes no constraint: a list of types
    becomes an ``anyOf`` of single types.  Every object must forbid other
    properties, and any keyword outside the supported subset is refused.
    """
    if not isinstance(schema, dict):
        raise ValueError("schema is not an object")
    adapted: dict[str, Any] = _adapt_schema(schema, "$")
    return adapted


def _adapt_schema(node: Any, where: str) -> Any:
    if isinstance(node, list):
        return [_adapt_schema(item, f"{where}[{index}]") for index, item in enumerate(node)]
    if not isinstance(node, dict):
        return node
    unknown = sorted(set(node) - _SCHEMA_KEYWORDS)
    if unknown:
        raise ValueError(f"{where} uses {', '.join(unknown)}")
    if "format" in node and node["format"] not in _SCHEMA_FORMATS:
        raise ValueError(f"{where} uses format {str(node['format'])[:40]}")
    if "additionalProperties" in node and node["additionalProperties"] is not False:
        raise ValueError(f"{where} allows additional properties")
    types = node.get("type")
    if types == "object" or (isinstance(types, list) and "object" in types):
        if node.get("additionalProperties") is not False:
            raise ValueError(f"{where} does not forbid additional properties")
    adapted: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("properties", "$defs", "definitions"):
            if not isinstance(value, dict):
                raise ValueError(f"{where}.{key} is not an object")
            adapted[key] = {name: _adapt_schema(child, f"{where}.{name}") for name, child in value.items()}
        elif key in ("items", "anyOf", "allOf"):
            adapted[key] = _adapt_schema(value, f"{where}.{key}")
        else:
            adapted[key] = value
    if isinstance(types, list):
        rest = {key: value for key, value in adapted.items() if key != "type"}
        branches = [{"type": kind} for kind in types]
        if "anyOf" in rest:
            return {"allOf": [{"anyOf": branches}, rest]}
        return {**rest, "anyOf": branches}
    return adapted


def read_claude_key(path: Path | None = None) -> tuple[str | None, str]:
    """Return the key from the key file and ``""``, or ``None`` and why it is unusable.

    The reason names the file and never its content.
    """
    key_path = CLAUDE_KEY_FILE if path is None else Path(path)
    try:
        lines = key_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None, f"{CLAUDE_KEY_FILE_DISPLAY} is missing"
    except (OSError, UnicodeDecodeError):
        return None, f"{CLAUDE_KEY_FILE_DISPLAY} is unreadable"
    for line in lines:
        name, separator, value = line.partition("=")
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export ") :].strip()
        value = value.strip().strip("'\"")
        if separator and name == CLAUDE_KEY_NAME and value:
            if not _KEY_SHAPE.match(value):
                # A malformed value could break the header line, and the
                # exception that reports a bad header carries the value.
                return None, f"{CLAUDE_KEY_FILE_DISPLAY} holds a malformed {CLAUDE_KEY_NAME}"
            return value, ""
    return None, f"{CLAUDE_KEY_FILE_DISPLAY} has no {CLAUDE_KEY_NAME}"


def _https_connection(host: str, timeout: float) -> Any:
    return http.client.HTTPSConnection(host, timeout=timeout)


def _resolve(host: str) -> list[tuple[Any, ...]]:
    """Resolve *host*: the one step of a request no watchdog can cut short."""
    return socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)


def _new_socket(family: int, kind: int, proto: int) -> socket.socket:
    return socket.socket(family, kind, proto)


def _tls_context() -> ssl.SSLContext:
    """The TLS context of every request: the default one, certificate and hostname verification on."""
    return ssl.create_default_context()


class ClaudeApiClient:
    """The Claude API backend: one HTTPS request to the Messages API per call.

    The model gets no tool, no file system and no web.  ``key_file``,
    ``connection_factory``, ``monotonic``, ``sleep`` and ``watchdog`` are the
    seams tests use; ``connection_factory(host, timeout)`` returns an object
    shaped like :class:`http.client.HTTPSConnection`, and
    ``watchdog(seconds, action)`` starts a timer that runs *action* once the
    seconds pass and returns an object with ``cancel()``.
    """

    backend = CLAUDE_API_BACKEND

    def __init__(
        self,
        *,
        key_file: Path | None = None,
        connection_factory: Callable[[str, float], Any] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        watchdog: Callable[[float, Callable[[], None]], Any] | None = None,
    ) -> None:
        self._key_file = key_file
        self._watchdog = watchdog if watchdog is not None else _start_watchdog
        self._connection_factory = connection_factory
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._sleep = sleep if sleep is not None else time.sleep
        self._lock = threading.Lock()
        self._active: set[Any] = set()
        self._aborted = False

    def abort(self) -> None:
        """Close every running request, its body read included, and refuse new ones."""
        with self._lock:
            self._aborted = True
            active = list(self._active)
        for call in active:
            call.interrupt()

    def run(
        self,
        request: ModelRequest,
        *,
        timeout: float,
        runner: Callable[..., Any] | None = None,
    ) -> ModelCallResult:
        """Run *request* to completion within *timeout* seconds.

        ``runner`` is the Codex subprocess seam and is not used here.
        """
        del runner
        deadline = self._monotonic() + max(0.0, float(timeout))
        try:
            body = self._body(request)
        except _Refusal as refusal:
            return _refused(refusal.failure, refusal.detail)
        key_file = CLAUDE_KEY_FILE if self._key_file is None else self._key_file
        key, reason = read_claude_key(key_file)
        if key is None:
            return ModelCallResult(ModelCallStatus.NOT_STARTED, stderr=reason, failure=ModelFailure.KEY_MISSING)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "x-api-key": key,
            "anthropic-version": CLAUDE_API_VERSION,
            "content-type": "application/json",
        }
        # One retry count per model call, whether a retry follows a dropped
        # connection or a retryable status: at most 1 + CLAUDE_MAX_RETRIES POSTs.
        retries = 0
        while True:
            outcome = self._send(payload, headers, deadline)
            if isinstance(outcome, ModelCallResult):
                return outcome
            if isinstance(outcome, _ConnectionFailed):
                wait = _retry_wait(None, retries)
                if retries < CLAUDE_MAX_RETRIES and self._monotonic() + wait < deadline:
                    retries += 1
                    self._sleep(wait)
                    continue
                return ModelCallResult(
                    ModelCallStatus.NOT_STARTED,
                    stderr=f"connection failed: {outcome.error_class}",
                    failure=ModelFailure.CONNECTION_FAILED,
                )
            status, retry_after, data = outcome
            if status == 200:
                return self._answer(request, data, key)
            failure, detail, retryable = _classify_error(status, retry_after, data, key)
            wait = _retry_wait(retry_after, retries)
            if retryable and retries < CLAUDE_MAX_RETRIES and self._monotonic() + wait < deadline:
                retries += 1
                self._sleep(wait)
                continue
            return ModelCallResult(ModelCallStatus.EXIT_NONZERO, status, stderr=detail, failure=failure)

    def _send(
        self, payload: bytes, headers: dict[str, str], deadline: float
    ) -> ModelCallResult | _ConnectionFailed | tuple[int, float | None, bytes]:
        """Send one request and return its status, retry-after and body, a dropped connection, or the call's end.

        The deadline bounds the whole request, not one socket operation: the
        socket gets what is left of it before the connect, the upload, the
        headers and every chunk of the body, and a watchdog shuts the socket
        down at the deadline itself, so a read that keeps receiving a byte at
        a time inside ``getresponse()`` or ``read()`` still ends there.  A
        passed deadline is a timeout and is never retried.
        """
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            return ModelCallResult(ModelCallStatus.TIMEOUT)
        factory = self._connection_factory if self._connection_factory is not None else _https_connection
        with self._lock:
            if self._aborted:
                return _refused(ModelFailure.ABORTED, "the client was aborted")
            call = _ActiveCall(factory(CLAUDE_API_HOST, remaining))
            self._active.add(call)
        watchdog = self._watchdog(remaining, call.expire)
        try:
            connection = call.connection
            if isinstance(connection, http.client.HTTPSConnection) and connection.sock is None:
                self._connect(call, connection, deadline)
            else:
                connect = getattr(connection, "connect", None)
                if callable(connect):
                    connect()
            # Kept: on ``Connection: close`` the response takes the socket from the connection.
            call.sock = getattr(connection, "sock", None)
            self._bound(call, deadline)
            connection.request("POST", CLAUDE_API_PATH, body=payload, headers=headers)
            self._bound(call, deadline)
            response = connection.getresponse()
            call.response = response
            status = int(response.status)
            retry_after = _retry_after(response.getheader("retry-after"))
            data = self._read_body(call, response, deadline)
            if call.expired:
                return ModelCallResult(ModelCallStatus.TIMEOUT)
            return status, retry_after, data
        except (_DeadlinePassed, TimeoutError):
            return ModelCallResult(ModelCallStatus.TIMEOUT)
        except (OSError, http.client.HTTPException, ValueError, AttributeError) as exc:
            # ``AttributeError`` too: ``http.client`` drops a response's file
            # when another thread closes it, and the next read fails on ``None``.
            # The exception text is never kept: only its class is safe.
            if call.expired:
                return ModelCallResult(ModelCallStatus.TIMEOUT)
            with self._lock:
                aborted = self._aborted
            if aborted:
                return ModelCallResult(
                    ModelCallStatus.NOT_STARTED, stderr="the client was aborted", failure=ModelFailure.ABORTED
                )
            return _ConnectionFailed(type(exc).__name__)
        finally:
            watchdog.cancel()
            with self._lock:
                self._active.discard(call)
            call.close()

    def _connect(self, call: _ActiveCall, connection: http.client.HTTPSConnection, deadline: float) -> None:
        """Connect *connection* so the watchdog can cut every step after the TCP connect.

        The TCP socket is registered with the call before the TLS handshake,
        so a handshake that never completes is shut down at the deadline.
        Verification is the default context's: certificate and hostname.
        """
        raw = self._open_socket(call, connection.host, deadline)
        # The handshake is deferred so the TLS socket is registered with the
        # call before it starts: the watchdog and ``abort()`` can then shut it
        # down mid-handshake. The default context still checks the certificate
        # and the hostname, in ``do_handshake()``.
        wrapped = _tls_context().wrap_socket(raw, server_hostname=connection.host, do_handshake_on_connect=False)
        call.sock = wrapped
        self._bound(call, deadline)
        wrapped.do_handshake()
        connection.sock = wrapped

    def _open_socket(self, call: _ActiveCall, host: str, deadline: float) -> Any:
        """Connect to each resolved address in turn inside what is left of the deadline.

        Each socket is registered with the call before its ``connect()``, so
        the watchdog can shut a stalled connect down; once the deadline has
        passed no further address is tried.  When every address fails, the
        last connection error is raised.
        """
        last_error: OSError = OSError("no address resolved")
        for family, kind, proto, _name, address in _resolve(host):
            remaining = deadline - self._monotonic()
            if remaining <= 0 or call.expired:
                raise _DeadlinePassed
            sock = _new_socket(family, kind, proto)
            call.sock = sock
            try:
                sock.settimeout(remaining)
                sock.connect(address)
                return sock
            except OSError as exc:
                last_error = exc
                try:
                    sock.close()
                except OSError:
                    pass
                if call.expired or deadline - self._monotonic() <= 0:
                    raise _DeadlinePassed from exc
        raise last_error

    def _bound(self, call: _ActiveCall, deadline: float) -> None:
        """Give the socket what is left of the deadline, or raise once it has passed or the client aborted."""
        with self._lock:
            if self._aborted:
                raise ConnectionAbortedError("aborted")
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise _DeadlinePassed
        for sock in call.sockets():
            try:
                sock.settimeout(remaining)
            except (OSError, AttributeError):
                pass

    def _read_body(self, call: _ActiveCall, response: Any, deadline: float) -> bytes:
        """Read the body in chunks, checking the deadline before each, up to one byte past the limit."""
        chunks: list[bytes] = []
        size = 0
        while size <= CLAUDE_MAX_RESPONSE_BYTES:
            self._bound(call, deadline)
            chunk = response.read(min(_READ_CHUNK_BYTES, CLAUDE_MAX_RESPONSE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        declared = _declared_length(response)
        if declared is not None and size < declared:
            # The server dropped the connection partway through the body.
            raise ConnectionResetError("the response body ended early")
        return b"".join(chunks)

    def _body(self, request: ModelRequest) -> dict[str, Any]:
        """Return the request body, or raise ``_Refusal`` before anything is sent."""
        if request.web_search:
            raise _Refusal(ModelFailure.WEB_SEARCH_UNSUPPORTED, f"web search is not offered on {self.backend}")
        if not request.model:
            raise _Refusal(ModelFailure.UNSUPPORTED_REQUEST, f"{self.backend} needs an explicit model")
        if request.reasoning_effort and request.reasoning_effort not in CLAUDE_API_EFFORTS:
            raise _Refusal(
                ModelFailure.UNSUPPORTED_REQUEST, f"{self.backend} has no effort {request.reasoning_effort!r}"
            )
        if request.resume_session_id and request.follows is None:
            raise _Refusal(ModelFailure.UNSUPPORTED_REQUEST, f"{self.backend} resumes only from the earlier exchange")
        messages = _messages(request)
        body: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens
            if request.max_output_tokens > 0
            else CLAUDE_DEFAULT_MAX_OUTPUT_TOKENS,
            "system": _SYSTEM_PROMPT,
            "messages": messages,
        }
        output_config: dict[str, Any] = {}
        if request.reasoning_effort:
            output_config["effort"] = request.reasoning_effort
        if request.output_schema_path is not None:
            try:
                schema = json.loads(Path(request.output_schema_path).read_text(encoding="utf-8"))
                output_config["format"] = {"type": "json_schema", "schema": claude_output_schema(schema)}
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _Refusal(ModelFailure.UNSUPPORTED_REQUEST, "the output schema is unreadable") from exc
            except ValueError as exc:
                raise _Refusal(ModelFailure.SCHEMA_UNSUPPORTED, f"structured output cannot express {exc}") from exc
        if output_config:
            body["output_config"] = output_config
        # Both guards read every string of the finished body, the output
        # schema included, so nothing leaves in a field they did not see.
        outgoing = list(_strings(body))
        if any(carries_pm_deal_text(text) for text in outgoing):
            raise _Refusal(
                ModelFailure.PM_TEXT_NOT_PERMITTED,
                f"the request carries a Publishers Marketplace deal logline, which {self.backend} may not receive",
            )
        if any(mentions_publishers_weekly(text) for text in outgoing):
            raise _Refusal(
                ModelFailure.PW_TEXT_NOT_PERMITTED,
                f"the request mentions publishersweekly.com, which {self.backend} may not receive",
            )
        return body

    def _answer(self, request: ModelRequest, data: bytes, key: str) -> ModelCallResult:
        """Read a 200 response: check why it stopped, then write and parse the answer."""
        if len(data) > CLAUDE_MAX_RESPONSE_BYTES:
            return ModelCallResult(ModelCallStatus.UNREADABLE_RESPONSE, 200, stderr="response too large")
        try:
            message = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ModelCallResult(ModelCallStatus.UNREADABLE_RESPONSE, 200, stderr="response is not JSON")
        if not isinstance(message, dict):
            return ModelCallResult(ModelCallStatus.UNREADABLE_RESPONSE, 200, stderr="response is not an object")
        usage = _usage(message.get("usage"))
        served_model = _scrub(str(message.get("model") or ""), key)[:128]
        if usage is not None:
            _record_usage(request, served_model or request.model, usage)
        stop_reason = str(message.get("stop_reason") or "")
        common: dict[str, Any] = {"returncode": 200, "usage": usage, "served_model": served_model}
        if stop_reason == "max_tokens":
            return ModelCallResult(
                ModelCallStatus.UNREADABLE_RESPONSE,
                stderr="the answer was cut at max_tokens",
                failure=ModelFailure.OUTPUT_TRUNCATED,
                **common,
            )
        if stop_reason == "refusal":
            return ModelCallResult(
                ModelCallStatus.UNREADABLE_RESPONSE,
                stderr="the model declined the request",
                failure=ModelFailure.MODEL_REFUSED,
                **common,
            )
        if stop_reason not in ("end_turn", "stop_sequence"):
            return ModelCallResult(
                ModelCallStatus.UNREADABLE_RESPONSE, stderr=f"stop_reason {_scrub(stop_reason, key)[:40]!r}", **common
            )
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        text = "".join(
            str(block.get("text") or "") for block in blocks if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        fenced = _FENCE.match(text)
        if fenced:
            text = fenced.group(1).strip()
        if key and key in text:
            # Never written, returned or resent in a follow-up.
            return ModelCallResult(
                ModelCallStatus.UNREADABLE_RESPONSE,
                stderr="the answer contained the API key and was discarded",
                failure=ModelFailure.KEY_ECHOED,
                **common,
            )
        try:
            request.response_path.write_text(text, encoding="utf-8")
        except OSError:
            return ModelCallResult(
                ModelCallStatus.UNREADABLE_RESPONSE, stderr="the answer could not be written", **common
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return ModelCallResult(ModelCallStatus.UNREADABLE_RESPONSE, stderr="the answer is not JSON", **common)
        return ModelCallResult(ModelCallStatus.ANSWERED, payload=payload, **common)


def _strings(value: Any) -> Iterator[str]:
    """Yield every string in a JSON-shaped value, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for name, item in value.items():
            yield str(name)
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def _refused(failure: ModelFailure, detail: str) -> ModelCallResult:
    return ModelCallResult(ModelCallStatus.REFUSED, stderr=detail, failure=failure)


class _DeadlinePassed(Exception):
    """The call's deadline passed between two steps of the request."""


@dataclass(frozen=True)
class _ConnectionFailed:
    """One request attempt lost its connection; only the exception's class is kept."""

    error_class: str


def _start_watchdog(seconds: float, action: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(max(0.0, seconds), action)
    timer.daemon = True
    timer.start()
    return timer


class _ActiveCall:
    """One running request and everything that can wake a thread blocked on it."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.sock: Any = None
        self.response: Any = None
        self.expired = False

    def expire(self) -> None:
        """The deadline passed: mark the call timed out and wake whatever read it is blocked in."""
        self.expired = True
        self.interrupt()

    def sockets(self) -> list[Any]:
        found = [self.sock, getattr(self.connection, "sock", None)]
        return [sock for index, sock in enumerate(found) if sock is not None and sock not in found[:index]]

    def interrupt(self) -> None:
        """Shut the socket down, wherever it now lives, so a blocked read returns."""
        for sock in self.sockets():
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except (OSError, AttributeError):
                pass
        self.close()

    def close(self) -> None:
        for closable in (self.response, self.connection, self.sock):
            try:
                if closable is not None:
                    closable.close()
            except Exception:
                pass


def _declared_length(response: Any) -> int | None:
    """Return the body length the response's ``content-length`` header declares, or ``None``."""
    getheader = getattr(response, "getheader", None)
    value = getheader("content-length") if callable(getheader) else None
    try:
        length = int(str(value).strip()) if value is not None else None
    except ValueError:
        return None
    return length if length is not None and length >= 0 else None


def _scrub(text: str, key: str) -> str:
    """Remove the exact key, then any other recognisable credential, from upstream text."""
    if key:
        text = text.replace(key, "[redacted]")
    return redact_secrets(text)


def _user_content(request: ModelRequest) -> list[dict[str, str]]:
    """Return the input files and the prompt of *request* as text blocks."""
    blocks: list[dict[str, str]] = []
    for name in request.input_files:
        if not _FILE_NAME_SHAPE.match(name) or ".." in name or request.working_dir is None:
            raise _Refusal(
                ModelFailure.UNSUPPORTED_REQUEST, "an input file is not a plain name in the working directory"
            )
        try:
            content = (Path(request.working_dir) / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise _Refusal(ModelFailure.UNSUPPORTED_REQUEST, f"input file {name} is unreadable") from exc
        blocks.append({"type": "text", "text": f'<file name="{name}">\n{content}\n</file>'})
    blocks.append({"type": "text", "text": request.prompt})
    return blocks


def _messages(request: ModelRequest) -> list[dict[str, Any]]:
    """Return the conversation: every earlier exchange resent, then this request."""
    messages: list[dict[str, Any]] = []
    if request.follows is not None:
        messages += _messages(request.follows.request)
        messages.append({"role": "assistant", "content": [{"type": "text", "text": request.follows.answer}]})
    messages.append({"role": "user", "content": _user_content(request)})
    return messages


def _retry_after(value: Any) -> float | None:
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _retry_wait(retry_after: float | None, retries: int) -> float:
    if retry_after is not None:
        return retry_after
    return float(CLAUDE_RETRY_BACKOFF_SECONDS * 2 ** min(retries, 8))


def _classify_error(
    status: int, retry_after: float | None, data: bytes, key: str = ""
) -> tuple[ModelFailure, str, bool]:
    """Return the failure, a bounded diagnostic and whether a retry may help."""
    error_type = ""
    message = ""
    error_code = ""
    try:
        body = json.loads(data[:65536].decode("utf-8", errors="replace"))
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            error_type = str(error.get("type") or "")[:64]
            message = str(error.get("message") or "")
            details = error.get("details")
            if isinstance(details, dict):
                error_code = str(details.get("error_code") or "")[:64]
    except (ValueError, AttributeError):
        pass
    # The exact key is removed before the text is cut, so no fragment of it survives.
    detail = _scrub(f"HTTP {status} {error_type}: {' '.join(message.split())}".strip(), key)[:_DIAGNOSTIC_LIMIT]
    if status in (401, 403):
        return ModelFailure.KEY_REJECTED, detail, False
    if status == 402:
        return ModelFailure.USAGE_LIMIT, detail, False
    if status == 404:
        return ModelFailure.MODEL_UNAVAILABLE, detail, False
    if status == 429:
        # The spend cap comes without retry-after, and retries fail until access resumes.
        spend_cap = error_code == _SPEND_CAP_CODE or retry_after is None
        return ModelFailure.USAGE_LIMIT, detail, not spend_cap
    if status in _RETRYABLE_STATUSES:
        return ModelFailure.SERVICE_ERROR, detail, True
    if status == 400 and "credit balance" in message.casefold():
        # Exhausted credit also comes as a 400 ("Your credit balance is too low ...").
        return ModelFailure.USAGE_LIMIT, detail, False
    if status in (400, 413, 422):
        return ModelFailure.BAD_REQUEST, detail, False
    return ModelFailure.SERVICE_ERROR, detail, False


def _usage(value: Any) -> ModelUsage | None:
    if not isinstance(value, dict):
        return None

    def count(name: str) -> int:
        raw = value.get(name)
        return int(raw) if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0

    return ModelUsage(
        count("input_tokens"),
        count("output_tokens"),
        count("cache_read_input_tokens"),
        count("cache_creation_input_tokens"),
    )


# -- Usage ledger --------------------------------------------------------------


class ModelUsageLedger:
    """The token counts of every Claude API call made while it was open."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[dict[str, Any]] = []
        self._totals: dict[str, int] = {
            "calls": 0,
            "input": 0,
            "output": 0,
            "cache_read_input": 0,
            "cache_creation_input": 0,
        }

    def record(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._pending.append(dict(entry))
            self._totals["calls"] += 1
            for name in ("input", "output", "cache_read_input", "cache_creation_input"):
                self._totals[name] += int(entry.get(name, 0))

    def drain(self) -> list[dict[str, Any]]:
        """Return the calls recorded since the last drain, oldest first."""
        with self._lock:
            pending, self._pending = self._pending, []
        return pending

    def totals(self) -> dict[str, int]:
        with self._lock:
            return dict(self._totals)


_LEDGERS: list[ModelUsageLedger] = []
_LEDGERS_LOCK = threading.Lock()


@contextmanager
def recording_model_usage() -> Iterator[ModelUsageLedger]:
    """Open a ledger that every Claude API call of this process records into until it closes."""
    ledger = ModelUsageLedger()
    with _LEDGERS_LOCK:
        _LEDGERS.append(ledger)
    try:
        yield ledger
    finally:
        with _LEDGERS_LOCK:
            _LEDGERS.remove(ledger)


def _record_usage(request: ModelRequest, model: str, usage: ModelUsage) -> None:
    entry = {
        "purpose": request.purpose or "unnamed",
        "backend": CLAUDE_API_BACKEND,
        "model": model,
        **usage.as_record(),
    }
    with _LEDGERS_LOCK:
        ledgers = list(_LEDGERS)
    for ledger in ledgers:
        ledger.record(entry)


# -- Selection -----------------------------------------------------------------


ModelClient = CodexCliClient | ClaudeApiClient


def exec_help_command(executable: str) -> list[str]:
    """Return the command that prints ``codex exec`` help and runs no model."""
    return [executable, "exec", "--help"]


def model_client(backend: str = DEFAULT_BACKEND) -> ModelClient:
    """Return the client for *backend*, the ``backend`` of a model pin."""
    if backend == CODEX_CLI_BACKEND:
        return CodexCliClient()
    if backend == CLAUDE_API_BACKEND:
        return ClaudeApiClient()
    raise ModelBackendError(f"unknown model backend: {backend!r}")


def model_client_for(pin: Any) -> ModelClient:
    """Return the client for the backend *pin* names; a pin without one uses the default."""
    return model_client(str(getattr(pin, "backend", DEFAULT_BACKEND) or DEFAULT_BACKEND))


def description_model_client() -> ModelClient:
    """Return the client the description pin names, or the default when the pin cannot be read.

    Stage 0 stops the weekly run on an unreadable pin before any stage
    starts, so the default here only keeps construction from raising.
    """
    try:
        return model_client_for(load_description_model_pin())
    except ModelPinError:
        return model_client()


__all__ = [
    "CLAUDE_API_HOST",
    "CLAUDE_DEFAULT_MAX_OUTPUT_TOKENS",
    "CLAUDE_KEY_FILE",
    "CLAUDE_KEY_FILE_DISPLAY",
    "CLAUDE_KEY_NAME",
    "ClaudeApiClient",
    "CodexCliClient",
    "ModelBackendError",
    "ModelCallResult",
    "ModelCallStatus",
    "ModelClient",
    "ModelFailure",
    "ModelRequest",
    "ModelUsage",
    "ModelUsageLedger",
    "PriorExchange",
    "carries_pm_deal_text",
    "claude_output_schema",
    "codex_failure",
    "description_model_client",
    "exec_help_command",
    "mentions_publishers_weekly",
    "model_client",
    "model_client_for",
    "read_claude_key",
    "recording_model_usage",
]
