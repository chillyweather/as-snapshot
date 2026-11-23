"""Agent-extraction backends: fake, fixture and real, and the model transport behind the real one."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Protocol
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import Request

from autoscout.source_collectors.agent_model_input import frontlist_model_batches
from autoscout.source_collectors.agent_model_input import model_bounded_page_data
from autoscout.source_collectors.base import DEFAULT_MODEL_MAX_OUTPUT_TOKENS
from autoscout.source_collectors.base import _collector_error
from autoscout.source_collectors.base import clean_text
from autoscout.source_collectors.base import collectors_pkg
from autoscout.source_collectors.base import contract_family
from autoscout.source_collectors.base import page_links
from autoscout.source_collectors.base import resolve_config_path
from autoscout.source_collectors.contract import agent_extraction_prompt


class AgentExtractionBackend(Protocol):
    name: str
    mode: str

    def extract(
        self,
        config_path: Path,
        source: dict[str, Any],
        page_data: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]: ...


class AgentExtractionTransport(Protocol):
    def extract(
        self,
        *,
        api_key: str,
        backend_name: str,
        source: dict[str, Any],
        page_data: dict[str, Any],
        max_chars: int,
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> tuple[Any, dict[str, Any]]: ...


@dataclass(frozen=True)
class OpenAIChatCompletionAgentExtractionTransport:
    endpoint: str = "https://api.openai.com/v1/chat/completions"
    default_model: str = "gpt-4.1-mini"

    def extract(
        self,
        *,
        api_key: str,
        backend_name: str,
        source: dict[str, Any],
        page_data: dict[str, Any],
        max_chars: int,
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> tuple[Any, dict[str, Any]]:
        model = str(source.get("model") or source.get("model_name") or self.default_model).strip()
        if not model:
            raise _collector_error(
                "Agent extraction real backend model must be configured",
                debug_metadata=agent_backend_debug(backend_name, "real-live-unavailable", "missing_model"),
            )
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Extract metadata-only Source Mention records from the provided configured "
                        "source page. Do not browse, search the web, follow links, or copy long source text. "
                        "Return only JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": agent_extraction_prompt(source, page_data, max_chars),
                },
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": max_output_tokens,
        }
        request = Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with collectors_pkg().urlopen(request, timeout=timeout_seconds) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except TimeoutError:
            raise _collector_error(
                "Agent extraction real backend failed: model request timed out",
                debug_metadata=agent_backend_debug(backend_name, "real-live-unavailable", "transport_timeout"),
            )
        except HTTPError as exc:
            raise _collector_error(
                f"Agent extraction real backend failed: HTTP {exc.code}",
                debug_metadata={
                    **agent_backend_debug(backend_name, "real-live-unavailable", "transport_http_error"),
                    "http_status": str(exc.code),
                },
            )
        except URLError as exc:
            raise _collector_error(
                f"Agent extraction real backend failed: {exc.reason}",
                debug_metadata=agent_backend_debug(backend_name, "real-live-unavailable", "transport_unavailable"),
            )
        except json.JSONDecodeError:
            raise _collector_error(
                "Agent extraction real backend returned malformed transport JSON",
                debug_metadata=agent_backend_debug(backend_name, "real-live-unavailable", "transport_malformed"),
            )

        content = openai_chat_completion_content(response_data)
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            raise _collector_error(
                "Agent extraction real backend returned non-JSON extraction content",
                debug_metadata=agent_backend_debug(backend_name, "real-live-unavailable", "model_output_malformed"),
            )
        return data, {
            "real_extraction_transport": "openai-chat-completions",
            "model": model,
            "model_endpoint": self.endpoint,
            "prompt_contract": "source-mention-metadata-only",
        }


REAL_AGENT_EXTRACTION_TRANSPORT: AgentExtractionTransport = OpenAIChatCompletionAgentExtractionTransport()


@dataclass(frozen=True)
class FakeAgentExtractionBackend:
    name: str = "fake"
    mode: str = "fake-live"

    def extract(
        self,
        config_path: Path,
        source: dict[str, Any],
        page_data: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        return fake_agent_extraction_response(config_path, source, page_data)


@dataclass(frozen=True)
class FixtureAgentExtractionBackend:
    name: str
    mode: str = "fixture-live"

    def extract(
        self,
        config_path: Path,
        source: dict[str, Any],
        page_data: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        data, debug = configured_extraction_response(config_path, source)
        return data, {**debug, "extraction_backend": self.name}


@dataclass(frozen=True)
class RealAgentExtractionBackend:
    name: str = "real"
    mode: str = "real-live"
    transport: AgentExtractionTransport | None = None

    def extract(
        self,
        config_path: Path,
        source: dict[str, Any],
        page_data: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        if not source.get("enable_real_backend", False):
            raise _collector_error(
                "Agent extraction real backend disabled: set enable_real_backend: true",
                status="skipped",
                debug_metadata=agent_backend_debug(self.name, "real-live-unavailable", "disabled"),
            )
        credential_ref = str(
            source.get("credential_env") or source.get("api_key_env") or source.get("credentials_ref") or ""
        ).strip()
        if not credential_ref:
            raise _collector_error(
                "Agent extraction real backend missing credentials configuration",
                status="skipped",
                debug_metadata=agent_backend_debug(self.name, "real-live-unavailable", "missing_credentials"),
            )

        raw_max_chars = source.get("max_model_input_chars", 120_000)
        max_chars = _positive_setting(
            120_000 if raw_max_chars is None else raw_max_chars,
            int,
            self.name,
            "max_model_input_chars must be an integer",
            "max_model_input_chars must be positive",
            "invalid_input_limit",
        )
        model_page_data, model_input_debug = model_bounded_page_data(page_data, max_chars, source=source)
        raw_max_output_tokens = source.get("model_max_output_tokens", DEFAULT_MODEL_MAX_OUTPUT_TOKENS)
        max_output_tokens = _positive_setting(
            DEFAULT_MODEL_MAX_OUTPUT_TOKENS if raw_max_output_tokens is None else raw_max_output_tokens,
            int,
            self.name,
            "model_max_output_tokens must be an integer",
            "model_max_output_tokens must be positive",
            "invalid_output_limit",
        )
        timeout_seconds = _positive_setting(
            source.get("model_timeout_seconds", source.get("timeout_seconds", 30)),
            float,
            self.name,
            "timeout must be a number",
            "timeout must be positive",
            "invalid_timeout",
        )
        limits_debug = {
            "credential_env": credential_ref,
            "max_model_input_chars": max_chars,
            "model_max_output_tokens": max_output_tokens,
            "model_timeout_seconds": timeout_seconds,
            **model_input_debug,
        }

        response_path = source.get("real_extraction_response_file") or source.get("fake_real_extraction_response_file")
        inline_response = source.get("real_extraction_response")
        if response_path:
            path = resolve_config_path(config_path, str(response_path))
            data = json.loads(path.read_text(encoding="utf-8"))
        elif inline_response is not None:
            data = inline_response
        else:
            credential_value = os.environ.get(credential_ref, "").strip()
            if not credential_value:
                raise _collector_error(
                    f"Agent extraction real backend missing credential environment variable: {credential_ref}",
                    status="skipped",
                    debug_metadata=agent_backend_debug(self.name, "real-live-unavailable", "missing_credentials"),
                )
            transport = self.transport or collectors_pkg().REAL_AGENT_EXTRACTION_TRANSPORT
            batches = (
                frontlist_model_batches(page_data, source, max_output_tokens)
                if contract_family(source) == "nuevoslibros-frontlist"
                else [model_page_data]
            )
            batch_entries: list[Any] = []
            if not batches:
                # A NuevosLibros capture with no card in the recent window makes no model
                # call. Its empty result carries the reason, and the run reports the source
                # by name as contributing nothing (#993).
                from autoscout.nuevoslibros_frontlist import NO_RECENT_CARD_REASON

                data = {"mentions": [], "empty_reason": NO_RECENT_CARD_REASON}
                transport_debug = {"model_request_count": 0}
            for batch in batches:
                data, transport_debug = transport.extract(
                    api_key=credential_value,
                    backend_name=self.name,
                    source=source,
                    page_data=batch,
                    max_chars=max_chars,
                    max_output_tokens=max_output_tokens,
                    timeout_seconds=timeout_seconds,
                )
                if len(batches) == 1:
                    break
                status = str(data.get("status", "")).strip().lower() if isinstance(data, dict) else ""
                if status not in {"", "ok", "success", "complete"}:
                    # A refused batch answers for the whole source.
                    break
                entries = data.get("mentions", data.get("entries")) if isinstance(data, dict) else data
                if not isinstance(entries, list):
                    # A batch that names no rows cannot pass for an empty one: merging it
                    # would save the other batches' books as a complete list.
                    raise _collector_error(
                        "Agent extraction real backend batch returned no mentions list",
                        debug_metadata=agent_backend_debug(
                            self.name, "real-live-unavailable", "model_output_malformed"
                        ),
                    )
                batch_entries.extend(entries)
            else:
                if len(batches) > 1:
                    data = {"mentions": batch_entries}
                    transport_debug = {**transport_debug, "model_request_count": len(batches)}
            return data, {
                **agent_backend_debug(self.name, "real-live", "ok"),
                **limits_debug,
                **transport_debug,
            }

        # A configured response file or inline payload is a deterministic replay
        # of the real path, not a live model call. Report it as such so
        # operators are not told stale fixtures were freshly scouted.
        return data, {
            **agent_backend_debug(self.name, "real-live-replay", "ok"),
            **limits_debug,
            "real_extraction_response_file": str(response_path or ""),
            "real_extraction_transport": "deterministic",
            "prompt_contract": "source-mention-metadata-only",
        }


def _positive_setting[Number: (int, float)](
    raw: Any,
    cast: Callable[[Any], Number],
    backend_name: str,
    unparsable: str,
    not_positive: str,
    failure_status: str,
) -> Number:
    """Read one real-backend limit, refusing a value that is not a positive number."""
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        raise _collector_error(
            f"Agent extraction real backend {unparsable}",
            debug_metadata=agent_backend_debug(backend_name, "real-live-unavailable", failure_status),
        )
    if value <= 0:
        raise _collector_error(
            f"Agent extraction real backend {not_positive}",
            debug_metadata=agent_backend_debug(backend_name, "real-live-unavailable", failure_status),
        )
    return value


def agent_backend_debug(name: str, mode: str, status: str) -> dict[str, Any]:
    return {
        "extraction_backend": name,
        "backend_name": name,
        "backend_mode": mode,
        "safe_failure_status": status,
    }


def openai_chat_completion_content(response_data: Any) -> str:
    if not isinstance(response_data, dict):
        raise _collector_error(
            "Agent extraction real backend returned malformed transport JSON",
            debug_metadata=agent_backend_debug("real", "real-live-unavailable", "transport_malformed"),
        )
    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise _collector_error(
            "Agent extraction real backend returned no model choices",
            debug_metadata=agent_backend_debug("real", "real-live-unavailable", "transport_malformed"),
        )
    first = choices[0]
    if not isinstance(first, dict):
        raise _collector_error(
            "Agent extraction real backend returned malformed model choice",
            debug_metadata=agent_backend_debug("real", "real-live-unavailable", "transport_malformed"),
        )
    message = first.get("message")
    if not isinstance(message, dict):
        raise _collector_error(
            "Agent extraction real backend returned malformed model message",
            debug_metadata=agent_backend_debug("real", "real-live-unavailable", "transport_malformed"),
        )
    content = message.get("content")
    if isinstance(content, str):
        return content
    raise _collector_error(
        "Agent extraction real backend returned empty model content",
        debug_metadata=agent_backend_debug("real", "real-live-unavailable", "model_output_empty"),
    )


def live_agent_extraction_response(
    config_path: Path,
    source: dict[str, Any],
    page_data: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    backend = select_agent_extraction_backend(source)
    data, debug = backend.extract(config_path, source, page_data)
    backend_mode = str(debug.get("backend_mode") or backend.mode)
    return data, {
        **debug,
        "extraction_backend": backend.name,
        "backend_name": backend.name,
        "backend_mode": backend_mode,
    }


def select_agent_extraction_backend(source: dict[str, Any]) -> AgentExtractionBackend:
    backend = str(source.get("extraction_backend", "")).strip().lower()
    if not backend and (
        source.get("extraction_response_file")
        or source.get("collector_response_file")
        or source.get("live_extraction_response_file")
    ):
        backend = "fixture"
    if backend == "fake":
        return FakeAgentExtractionBackend()
    if backend in {"replay", "fixture"}:
        return FixtureAgentExtractionBackend(name=backend)
    if backend in {"real", "openai", "model"}:
        return RealAgentExtractionBackend(name=backend)
    raise _collector_error(
        "Agent extraction backend unavailable: configure extraction_backend for live collection",
        debug_metadata=agent_backend_debug(backend, "unavailable", "missing_backend"),
    )


def fake_agent_extraction_response(
    config_path: Path,
    source: dict[str, Any],
    page_data: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    raw_path = source.get("fake_extraction_response_file") or source.get("extraction_response_file")
    if raw_path:
        path = resolve_config_path(config_path, str(raw_path))
        return json.loads(path.read_text(encoding="utf-8")), {
            "extraction_backend": "fake",
            "fake_extraction_response_file": str(raw_path),
        }
    if "fake_extraction_response" in source:
        return source["fake_extraction_response"], {"extraction_backend": "fake"}
    links = sorted(page_links(str(page_data.get("html", "")), str(page_data.get("source_page", ""))))
    mentions = [
        {
            "title": clean_text(link.rsplit("/", 1)[-1].replace("-", " ")).title() or "Agent Extracted Book",
            "primary_authors": ["Unknown Agent Extraction Author"],
            "source_url": link,
            "evidence_url": link,
            "signal_type": source.get("family", "media/curated list"),
            "signal_strength": "minor mention",
            "evidence_note": "Fake agent extraction backend produced a metadata-only mention for tests.",
        }
        for link in links[:1]
    ]
    return {"mentions": mentions}, {"extraction_backend": "fake"}


def configured_extraction_response(config_path: Path, source: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    raw_path = (
        source.get("extraction_response_file")
        or source.get("collector_response_file")
        or source.get("live_extraction_response_file")
    )
    debug_metadata = {
        "extraction_response_file": str(source.get("extraction_response_file", "")),
        "collector_response_file": str(source.get("collector_response_file", "")),
        "live_extraction_response_file": str(source.get("live_extraction_response_file", "")),
    }
    if not raw_path:
        raise _collector_error(
            "Agent extraction backend unavailable: configure extraction_response_file for replay",
            debug_metadata={"collector_kind": "agent-extraction"},
        )
    path = resolve_config_path(config_path, str(raw_path))
    debug_metadata["extraction_response_path"] = str(raw_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "response" in data and data.get("collection_mode"):
        data = data["response"]
    return data, debug_metadata
