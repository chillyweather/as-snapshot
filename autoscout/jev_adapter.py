"""The one seam between AutoScout and TypeSafe Jev (issue #725).

Jev answers closed questions about a supplied state: a Score over ordered
levels, a Choice among labelled options, or a Noul yes-probability. It writes
no prose, so every answer is a probability distribution that this module
validates into AutoScout-owned types before any caller sees it.

Callers depend on ``JevAdapter`` and never on the vendor wire format. The live
adapter is separately billed usage: it runs only when a caller selects it
explicitly and a ``TYPESAFE_API_KEY`` is present, and it never falls back to
another adapter. Every failure is normalized into one bounded ``unavailable``
assessment with a safe reason code, so a vendor outage degrades the advisory
output and nothing else.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Protocol

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
# The exact versioned model. A moving alias such as ``jev-latest`` is never
# requested, and a response reporting any other model is rejected.
JEV_PINNED_MODEL = "jev-1.13.0"
JEV_PRICE_USD_PER_MILLION_INPUT_TOKENS = 0.042
JEV_API_KEY_ENV = "TYPESAFE_API_KEY"
# Outside the repository, mode 0600. Read only when the environment has no key.
JEV_API_KEY_FILE = Path.home() / ".config" / "autoscout" / "typesafe.env"

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 1.0
_MAX_RESPONSE_BYTES = 1_000_000
_PROBABILITY_TOLERANCE = 0.02

QUESTION_TYPES = ("score", "choice", "noul")

# Closed reason codes for an unavailable assessment.
REASON_CREDENTIAL_MISSING = "credential_missing"
REASON_AUTH_FAILED = "auth_failed"
REASON_INVALID_REQUEST = "invalid_request"
REASON_RATE_LIMITED = "rate_limited"
REASON_OVERLOADED = "overloaded"
REASON_HTTP_ERROR = "http_error"
REASON_TIMEOUT = "timeout"
REASON_NETWORK_ERROR = "network_error"
REASON_MALFORMED_RESPONSE = "malformed_response"
REASON_MODEL_MISMATCH = "model_mismatch"
REASON_REPLAY_MISSING = "replay_missing"
REASON_BUDGET_EXHAUSTED = "budget_exhausted"

_RETRYABLE_REASONS = frozenset({REASON_RATE_LIMITED, REASON_OVERLOADED, REASON_TIMEOUT, REASON_NETWORK_ERROR})
# A failure that every further call would repeat. Callers stop calling.
TERMINAL_REASONS = frozenset({REASON_CREDENTIAL_MISSING, REASON_AUTH_FAILED})


@dataclass(frozen=True)
class JevQuestion:
    """One atomic closed question. ``options`` are ordered levels for a Score
    and labelled options for a Choice; a Noul has none."""

    question_id: str
    question_type: str
    instructions: str
    options: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.question_type not in QUESTION_TYPES:
            raise ValueError(f"unsupported Jev question type: {self.question_type}")
        if self.question_type == "noul" and self.options:
            raise ValueError("a Noul question has no options")
        if self.question_type != "noul" and len(self.options) < 2:
            raise ValueError("a Score or Choice question needs at least two options")

    @property
    def labels(self) -> tuple[str, ...]:
        """The keys the response must carry probabilities for."""
        if self.question_type == "score":
            return tuple(str(index) for index in range(len(self.options)))
        return tuple(label for label, _ in self.options)

    def wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.question_type, "instructions": self.instructions}
        if self.question_type == "score":
            payload["criteria"] = [description for _, description in self.options]
        elif self.question_type == "choice":
            payload["criteria"] = dict(self.options)
        return payload


@dataclass(frozen=True)
class JevAnswer:
    """A validated answer. A Noul is stored as a ``yes``/``no`` distribution."""

    question_id: str
    question_type: str
    probabilities: Mapping[str, float]
    confidence: float | None = None


@dataclass(frozen=True)
class JevAssessment:
    """The outcome of one request: validated answers, or a reason it is unavailable."""

    status: str
    adapter_kind: str
    requested_model: str
    reason_code: str = ""
    returned_model: str = ""
    answers: Mapping[str, JevAnswer] = field(default_factory=dict)
    input_tokens: int = 0
    latency_ms: int = 0

    @property
    def available(self) -> bool:
        return self.status == "ok"


class JevAdapter(Protocol):
    kind: str

    def assess(self, state: Mapping[str, Any], questions: Sequence[JevQuestion]) -> JevAssessment: ...


def unavailable(adapter_kind: str, reason_code: str, *, latency_ms: int = 0) -> JevAssessment:
    return JevAssessment(
        status="unavailable",
        adapter_kind=adapter_kind,
        requested_model=JEV_PINNED_MODEL,
        reason_code=reason_code,
        latency_ms=latency_ms,
    )


def request_payload(state: Mapping[str, Any], questions: Sequence[JevQuestion]) -> dict[str, Any]:
    return {
        "model": JEV_PINNED_MODEL,
        "state": dict(state),
        "questions": {question.question_id: question.wire() for question in questions},
    }


def request_digest(state: Mapping[str, Any], questions: Sequence[JevQuestion]) -> str:
    """Identify one exact request: the model, the state, and the question wording."""
    canonical = json.dumps(request_payload(state, questions), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _probability(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if number != number or number < -_PROBABILITY_TOLERANCE or number > 1 + _PROBABILITY_TOLERANCE:
        return None
    return min(1.0, max(0.0, number))


def _validated_answer(question: JevQuestion, raw: Any) -> JevAnswer | None:
    if not isinstance(raw, Mapping):
        return None
    if question.question_type == "noul":
        yes = _probability(raw.get("noul"))
        if yes is None:
            return None
        return JevAnswer(question.question_id, "noul", {"yes": yes, "no": 1.0 - yes})
    raw_probabilities = raw.get("probabilities")
    if not isinstance(raw_probabilities, Mapping) or set(raw_probabilities) != set(question.labels):
        return None
    probabilities: dict[str, float] = {}
    for label in question.labels:
        probability = _probability(raw_probabilities[label])
        if probability is None:
            return None
        probabilities[label] = probability
    total = sum(probabilities.values())
    if abs(total - 1.0) > _PROBABILITY_TOLERANCE:
        return None
    probabilities = {label: value / total for label, value in probabilities.items()}
    confidence = _probability(raw.get("confidence")) if raw.get("confidence") is not None else None
    return JevAnswer(question.question_id, question.question_type, probabilities, confidence)


def validate_response(
    payload: Any,
    questions: Sequence[JevQuestion],
    *,
    adapter_kind: str,
    latency_ms: int = 0,
) -> JevAssessment:
    """Turn a vendor response into an assessment, or reject it whole.

    A response is used only when it names the pinned model and answers every
    question with a well-formed distribution over exactly the asked labels.
    """
    if not isinstance(payload, Mapping):
        return unavailable(adapter_kind, REASON_MALFORMED_RESPONSE, latency_ms=latency_ms)
    returned_model = payload.get("model")
    if returned_model != JEV_PINNED_MODEL:
        return unavailable(adapter_kind, REASON_MODEL_MISMATCH, latency_ms=latency_ms)
    raw_answers = payload.get("answers")
    if not isinstance(raw_answers, Mapping):
        return unavailable(adapter_kind, REASON_MALFORMED_RESPONSE, latency_ms=latency_ms)
    answers: dict[str, JevAnswer] = {}
    for question in questions:
        answer = _validated_answer(question, raw_answers.get(question.question_id))
        if answer is None:
            return unavailable(adapter_kind, REASON_MALFORMED_RESPONSE, latency_ms=latency_ms)
        answers[question.question_id] = answer
    usage = payload.get("usage")
    tokens = usage.get("input_tokens") if isinstance(usage, Mapping) else 0
    input_tokens = tokens if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0 else 0
    return JevAssessment(
        status="ok",
        adapter_kind=adapter_kind,
        requested_model=JEV_PINNED_MODEL,
        returned_model=JEV_PINNED_MODEL,
        answers=answers,
        input_tokens=input_tokens,
        latency_ms=latency_ms,
    )


def load_api_key(
    environ: Mapping[str, str] | None = None,
    key_file: Path | None = None,
) -> str | None:
    """Return the TypeSafe key from the environment or the operator's key file."""
    env = os.environ if environ is None else environ
    key = str(env.get(JEV_API_KEY_ENV, "")).strip()
    if key:
        return key
    path = JEV_API_KEY_FILE if key_file is None else key_file
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        name, separator, value = line.partition("=")
        if separator and name.strip() == JEV_API_KEY_ENV and value.strip():
            return value.strip()
    return None


class JevTransportError(Exception):
    """A transport failure reduced to a safe reason code. It carries no
    response body, header, or key."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _http_reason(status: int) -> str:
    if status in (401, 403):
        return REASON_AUTH_FAILED
    if status in (400, 422):
        return REASON_INVALID_REQUEST
    if status == 429:
        return REASON_RATE_LIMITED
    if status in (500, 502, 503, 529):
        return REASON_OVERLOADED
    return REASON_HTTP_ERROR


def _urllib_transport(api_key: str, body: bytes, timeout: float) -> Any:
    request = urllib.request.Request(
        JEV_ENDPOINT,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise JevTransportError(_http_reason(exc.code))
    except TimeoutError:
        raise JevTransportError(REASON_TIMEOUT)
    except urllib.error.URLError as exc:
        raise JevTransportError(REASON_TIMEOUT if isinstance(exc.reason, TimeoutError) else REASON_NETWORK_ERROR)
    except (OSError, http.client.HTTPException):
        # A truncated or malformed HTTP exchange (for example IncompleteRead)
        # is not an OSError; it is still one failed, retryable call.
        raise JevTransportError(REASON_NETWORK_ERROR)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise JevTransportError(REASON_MALFORMED_RESPONSE)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise JevTransportError(REASON_MALFORMED_RESPONSE)


class LiveJevAdapter:
    """Call the TypeSafe endpoint with a short timeout and bounded retries."""

    kind = "live"

    def __init__(
        self,
        api_key: str | None,
        *,
        transport: Callable[[str, bytes, float], Any] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api_key = api_key
        self._transport = transport or _urllib_transport
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max(1, max_attempts)
        self._sleep = sleep
        self._monotonic = monotonic

    def assess(self, state: Mapping[str, Any], questions: Sequence[JevQuestion]) -> JevAssessment:
        if not self._api_key:
            return unavailable(self.kind, REASON_CREDENTIAL_MISSING)
        body = json.dumps(request_payload(state, questions), ensure_ascii=False).encode("utf-8")
        reason = REASON_NETWORK_ERROR
        latency_ms = 0
        for attempt in range(self._max_attempts):
            started = self._monotonic()
            try:
                payload = self._transport(self._api_key, body, self._timeout_seconds)
            except JevTransportError as exc:
                reason = exc.reason_code
                latency_ms = round((self._monotonic() - started) * 1000)
                if reason not in _RETRYABLE_REASONS or attempt + 1 >= self._max_attempts:
                    break
                self._sleep(_BACKOFF_SECONDS * 2**attempt)
                continue
            except Exception:
                # Nothing a transport raises may escape: one failed call must
                # never cost the other candidates their scores.
                return unavailable(
                    self.kind, REASON_NETWORK_ERROR, latency_ms=round((self._monotonic() - started) * 1000)
                )
            latency_ms = round((self._monotonic() - started) * 1000)
            return validate_response(payload, questions, adapter_kind=self.kind, latency_ms=latency_ms)
        return unavailable(self.kind, reason, latency_ms=latency_ms)


class ReplayJevAdapter:
    """Serve recorded vendor responses by request digest, without a network.

    A request with no recording is ``replay_missing``; replay never reaches
    the live service.
    """

    kind = "replay"

    def __init__(self, responses: Mapping[str, Any]) -> None:
        self._responses = dict(responses)

    @classmethod
    def from_file(cls, path: Path) -> ReplayJevAdapter:
        data = json.loads(path.read_text(encoding="utf-8"))
        responses = data.get("responses") if isinstance(data, Mapping) else None
        if not isinstance(responses, Mapping):
            raise ValueError("a Jev replay file needs a 'responses' object keyed by request digest")
        return cls(responses)

    def assess(self, state: Mapping[str, Any], questions: Sequence[JevQuestion]) -> JevAssessment:
        recorded = self._responses.get(request_digest(state, questions))
        if recorded is None:
            return unavailable(self.kind, REASON_REPLAY_MISSING)
        if isinstance(recorded, Mapping) and isinstance(recorded.get("unavailable"), str):
            return unavailable(self.kind, recorded["unavailable"])
        latency = recorded.get("latency_ms", 0) if isinstance(recorded, Mapping) else 0
        return validate_response(
            recorded,
            questions,
            adapter_kind=self.kind,
            latency_ms=latency if isinstance(latency, int) and not isinstance(latency, bool) else 0,
        )
