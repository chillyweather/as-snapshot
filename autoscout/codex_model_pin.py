"""The pinned Codex model for source extraction (Issue #731).

Extraction used to run on whatever model the operator's global
``~/.codex/config.toml`` named.  On 2026-09-26 that file was switched to a
new model one minute before the weekly run, nothing noticed, and twelve
healthy captures came back with no entries.  Extraction output depends on
the model, so the model is part of AutoScout's configuration, not the
operator's desktop settings.

The pin lives in ``configs/codex-model-pin.json``, so a model
change is a reviewed commit.  Every extraction command and the Stage 0 probe
pass it explicitly with ``-m`` and ``-c model_reasoning_effort=...``; the
operator's global default is reported beside it and never used.

A missing or malformed pin fails closed: extraction must not silently fall
back to the global default, because that fallback is the defect this module
removes.

The ``description`` entry pins every other Codex call of the weekly run the
same way: description enrichment, description adjudication, the PM logline
descriptions and original-edition completion.  They also ran on the global
default until 2026-11-10, when the first run on the run server found
``gpt-5.5`` there, a model a ChatGPT account cannot use: every call of those
stages failed in three seconds and no description was written.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoscout.repository_root import repository_root

DEFAULT_PIN_PATH = repository_root() / "configs" / "codex-model-pin.json"

EXTRACTION_PIN_KEY = "extraction"
DESCRIPTION_PIN_KEY = "description"
# Description adjudication searches the web, which only the Codex CLI backend
# offers (Issue #1149).  It runs on the description pin while that pin is on
# the Codex CLI; once the description pin names another backend, this optional
# entry names the Codex model adjudication keeps using.
ADJUDICATION_PIN_KEY = "adjudication"

# The backend that runs a pinned model (Issue #1018).  The top-level key of
# the pin file applies to every entry; a ``backend`` key inside one entry
# overrides it for that entry alone (Issue #1149), so one stage can move while
# the others stay.  The Codex CLI is the default when neither is present.
BACKEND_KEY = "backend"
CODEX_CLI_BACKEND = "codex-cli"
# Direct HTTPS calls to the Claude API with the key in
# ~/.config/autoscout/claude.env (Issue #1149).
CLAUDE_API_BACKEND = "claude-api"
DEFAULT_BACKEND = CODEX_CLI_BACKEND
KNOWN_BACKENDS = (CODEX_CLI_BACKEND, CLAUDE_API_BACKEND)
# The Claude API takes its depth as ``output_config.effort``, from this set.
CLAUDE_API_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# A model slug is a short token.  It must never start with "-", so it can
# only ever be read as the value of ``-m`` and never as another flag.
_MODEL_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REASONING_EFFORT_SHAPE = re.compile(r"^[a-z]{1,32}$")


class ModelPinError(ValueError):
    """The pin file is missing or does not declare a usable model."""


@dataclass(frozen=True)
class CodexModelPin:
    """One pinned model and reasoning effort for a Codex-backed stage."""

    model: str
    reasoning_effort: str
    source: str = ""
    backend: str = DEFAULT_BACKEND

    def as_profile(self) -> dict[str, str]:
        """Return the pin in the ``effective_model_profile`` vocabulary."""
        return {"model": self.model, "reasoning_effort": self.reasoning_effort}


def parse_model_pin(document: Any, *, key: str = EXTRACTION_PIN_KEY, source: str = "") -> CodexModelPin:
    """Return the pin declared under *key*, or raise ``ModelPinError``."""
    if not isinstance(document, dict):
        raise ModelPinError("codex model pin must be a JSON object")
    entry = document.get(key)
    if not isinstance(entry, dict):
        raise ModelPinError(f"codex model pin has no {key!r} entry")
    model = str(entry.get("model", "")).strip()
    if not _MODEL_SHAPE.match(model):
        raise ModelPinError(f"codex model pin {key!r} has an invalid model: {model!r}")
    effort = str(entry.get("reasoning_effort", "")).strip().casefold()
    if not _REASONING_EFFORT_SHAPE.match(effort):
        raise ModelPinError(f"codex model pin {key!r} has an invalid reasoning_effort: {effort!r}")
    backend = document.get(BACKEND_KEY, DEFAULT_BACKEND)
    if backend not in KNOWN_BACKENDS:
        raise ModelPinError(f"codex model pin names an unknown {BACKEND_KEY!r}: {str(backend)[:64]!r}")
    if BACKEND_KEY in entry:
        backend = entry[BACKEND_KEY]
        if backend not in KNOWN_BACKENDS:
            raise ModelPinError(f"codex model pin {key!r} names an unknown {BACKEND_KEY!r}: {str(backend)[:64]!r}")
    if backend == CLAUDE_API_BACKEND and effort not in CLAUDE_API_EFFORTS:
        raise ModelPinError(
            f"codex model pin {key!r} has a reasoning_effort the {CLAUDE_API_BACKEND} backend does not take: {effort!r}"
        )
    return CodexModelPin(model=model, reasoning_effort=effort, source=source, backend=backend)


def _read_pin_document(path: Path | None) -> tuple[Any, str]:
    pin_path = Path(path) if path is not None else DEFAULT_PIN_PATH
    try:
        return json.loads(pin_path.read_text(encoding="utf-8")), pin_path.name
    except FileNotFoundError as exc:
        raise ModelPinError(f"codex model pin file not found: {pin_path.name}") from exc
    except (OSError, ValueError) as exc:
        raise ModelPinError(f"codex model pin file is unreadable: {pin_path.name}: {exc}") from exc


def load_model_pin(path: Path | None = None, *, key: str = EXTRACTION_PIN_KEY) -> CodexModelPin:
    """Read the pin file and return the pin declared under *key*."""
    document, source = _read_pin_document(path)
    return parse_model_pin(document, key=key, source=source)


def load_extraction_model_pin(path: Path | None = None) -> CodexModelPin:
    """Return the pinned extraction model."""
    return load_model_pin(path, key=EXTRACTION_PIN_KEY)


def load_description_model_pin(path: Path | None = None) -> CodexModelPin:
    """Return the pinned model of the description stages and original-edition completion."""
    return load_model_pin(path, key=DESCRIPTION_PIN_KEY)


def adjudication_model_pin(document: Any, *, source: str = "") -> CodexModelPin:
    """Return the Codex pin description adjudication runs on, or raise ``ModelPinError``.

    Adjudication needs web search, which only the Codex CLI backend offers.
    An ``adjudication`` entry wins and must be on the Codex CLI; without one,
    adjudication runs on the description pin, which must then be on the
    Codex CLI too.  A description pin on another backend without an
    ``adjudication`` entry fails closed: no Codex model is guessed.
    """
    if isinstance(document, dict) and ADJUDICATION_PIN_KEY in document:
        pin = parse_model_pin(document, key=ADJUDICATION_PIN_KEY, source=source)
        if pin.backend != CODEX_CLI_BACKEND:
            raise ModelPinError(
                f"codex model pin {ADJUDICATION_PIN_KEY!r} must be on {CODEX_CLI_BACKEND!r}: adjudication searches the web"
            )
        return pin
    pin = parse_model_pin(document, key=DESCRIPTION_PIN_KEY, source=source)
    if pin.backend != CODEX_CLI_BACKEND:
        raise ModelPinError(
            f"codex model pin {DESCRIPTION_PIN_KEY!r} is on {pin.backend!r} and adjudication searches the web, "
            f"which only {CODEX_CLI_BACKEND!r} offers: add an {ADJUDICATION_PIN_KEY!r} entry naming a Codex model"
        )
    return pin


def load_adjudication_model_pin(path: Path | None = None) -> CodexModelPin:
    """Read the pin file and return the Codex pin description adjudication runs on."""
    document, source = _read_pin_document(path)
    return adjudication_model_pin(document, source=source)


# ``codex exec`` prints its resolved settings as a header on stderr.
_HEADER_MODEL = re.compile(r"^\s*model:\s*(.+?)\s*$", re.MULTILINE)
_HEADER_EFFORT = re.compile(r"^\s*reasoning effort:\s*(.+?)\s*$", re.MULTILINE)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def header_profile(stderr: str) -> dict[str, str]:
    """Return the model and effort ``codex exec`` reported, when present."""
    text = _ANSI.sub("", str(stderr or ""))
    profile: dict[str, str] = {}
    model = _HEADER_MODEL.search(text)
    if model:
        profile["model"] = model.group(1).strip()[:128]
    effort = _HEADER_EFFORT.search(text)
    if effort:
        profile["reasoning_effort"] = effort.group(1).strip().casefold()[:32]
    return profile


# Phrases the API uses when a model cannot be served at all.  The CLI's
# "Model metadata for ... not found" warning is deliberately absent: it also
# appears for a servable model the local cache has not seen yet.
_UNAVAILABLE_MARKERS = (
    "model is not supported",
    "model_not_found",
    "does not exist",
    "unknown model",
)


def model_unavailable_in(stderr: str) -> bool:
    """Return whether *stderr* says the requested model cannot be served."""
    lowered = _ANSI.sub("", str(stderr or "")).casefold()
    return any(marker in lowered for marker in _UNAVAILABLE_MARKERS)


# What the CLI prints when the account has no quota left for now.
_USAGE_LIMIT_MARKERS = ("usage limit", "usage_limit_exceeded")


def usage_limit_in(stderr: str) -> bool:
    """Return whether *stderr* says the account has hit its usage limit."""
    lowered = _ANSI.sub("", str(stderr or "")).casefold()
    return any(marker in lowered for marker in _USAGE_LIMIT_MARKERS)


__all__ = [
    "ADJUDICATION_PIN_KEY",
    "BACKEND_KEY",
    "CLAUDE_API_BACKEND",
    "CLAUDE_API_EFFORTS",
    "CODEX_CLI_BACKEND",
    "DEFAULT_BACKEND",
    "DEFAULT_PIN_PATH",
    "DESCRIPTION_PIN_KEY",
    "KNOWN_BACKENDS",
    "CodexModelPin",
    "ModelPinError",
    "adjudication_model_pin",
    "header_profile",
    "load_adjudication_model_pin",
    "load_description_model_pin",
    "load_extraction_model_pin",
    "load_model_pin",
    "model_unavailable_in",
    "parse_model_pin",
    "usage_limit_in",
]
