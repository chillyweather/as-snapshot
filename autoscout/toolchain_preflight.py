"""Stage 0 toolchain preflight: Codex and discovery adapter readiness.

Codex readiness is fail-closed because extraction is the core of the run.
Discovery adapter readiness is warn-only because the stage is offline-safe.

Every check reports a three-state status (pass/fail or pass/degraded/unknown)
rather than a bool, so "could not check" is never confused with "healthy".

The adapter probe uses GET /health and spends no Brave quota.
The Codex probe is one minimal invocation; it costs one Codex request
when it succeeds and fails fast at 400 with no token cost when the
model is unservable. The cost is documented in the result.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from autoscout.codex_features import EXTRACTION_DISABLED_FEATURES
from autoscout.codex_features import verify_extraction_feature_policy
from autoscout.codex_model_pin import CLAUDE_API_BACKEND
from autoscout.codex_model_pin import CODEX_CLI_BACKEND
from autoscout.codex_model_pin import CodexModelPin
from autoscout.codex_model_pin import ModelPinError
from autoscout.codex_model_pin import adjudication_model_pin
from autoscout.codex_model_pin import header_profile
from autoscout.codex_model_pin import load_adjudication_model_pin
from autoscout.codex_model_pin import load_description_model_pin
from autoscout.codex_model_pin import load_extraction_model_pin
from autoscout.codex_model_pin import model_unavailable_in
from autoscout.grounded_description import PREFLIGHT_ADAPTER_KEY_MISSING
from autoscout.grounded_description import PREFLIGHT_ADAPTER_NO_RESPONSE
from autoscout.grounded_description import PREFLIGHT_ADAPTER_NOT_RUNNING
from autoscout.grounded_description import PREFLIGHT_ADAPTER_UNAVAILABLE
from autoscout.grounded_description import PREFLIGHT_ADAPTER_VENDOR_ERROR
from autoscout.grounded_description import PREFLIGHT_ENDPOINT_NOT_CONFIGURED
from autoscout.grounded_description import PREFLIGHT_HEALTHY
from autoscout.grounded_description import PreflightResult
from autoscout.model_client import CLAUDE_KEY_FILE_DISPLAY
from autoscout.model_client import CLAUDE_KEY_NAME
from autoscout.model_client import ModelCallResult
from autoscout.model_client import ModelCallStatus
from autoscout.model_client import ModelFailure
from autoscout.model_client import ModelRequest
from autoscout.model_client import model_client_for

STAGE_ZERO_NAME = "toolchain-preflight"

# Codex-specific reason codes reuse the same vocabulary style but are
# distinct because no grounded_description constant exists for Codex.
CODEX_HEALTHY = "codex_healthy"
CODEX_MODEL_UNSUPPORTED = "codex_model_unsupported"
CODEX_NOT_AVAILABLE = "codex_not_available"
CODEX_CHECK_UNKNOWN = "codex_check_unknown"
# The pinned extraction model (Issue #731).
CODEX_MODEL_PIN_INVALID = "codex_model_pin_invalid"
CODEX_PINNED_MODEL_UNAVAILABLE = "codex_pinned_model_unavailable"
CODEX_MODEL_PIN_NOT_APPLIED = "codex_model_pin_not_applied"
# A pin on the Claude API backend (Issue #1149).
CLAUDE_API_KEY_UNUSABLE = "claude_api_key_unusable"
CLAUDE_API_CHECK_UNKNOWN = "claude_api_check_unknown"
# The API, or the client before sending, refuses the pin's request itself:
# a retry cannot help, the pin entry must change.
CLAUDE_API_REQUEST_REJECTED = "claude_api_request_rejected"
_PERMANENT_CLAUDE_REJECTIONS = frozenset(
    {ModelFailure.BAD_REQUEST, ModelFailure.UNSUPPORTED_REQUEST, ModelFailure.SCHEMA_UNSUPPORTED}
)
# The Claude probe waits for adaptive thinking on a one-line answer.
CLAUDE_PROBE_MIN_TIMEOUT_SECONDS = 30.0

PIN_FILE_NAME = "configs/codex-model-pin.json"
PIN_REPLACEMENT_ACTION = (
    "Qualify a replacement model with ./scripts/qualify-model --model <name> "
    f"and update the extraction pin in {PIN_FILE_NAME} with its PASS report."
)

DISCOVERY_CHECK_UNKNOWN = "discovery_check_unknown"


def _read_codex_config_string(key: str) -> str:
    """Return one top-level string setting from ~/.codex/config.toml, or "".

    Reads CODEX_HOME when set, otherwise ~/.codex.
    Tries tomllib first, falls back to regex.
    Never raises, never logs the file content.
    """
    home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    config_path = home / "config.toml"
    try:
        data = config_path.read_bytes()
    except OSError:
        return ""
    # Try tomllib (Python 3.11+)
    try:
        import tomllib

        payload = tomllib.loads(data.decode("utf-8"))
        value = str(payload.get(key, "")).strip()
        if value:
            return value[:200]
    except Exception:
        pass
    # Fallback regex
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return ""
    pattern = rf'^\s*{re.escape(key)}\s*=\s*["\']([^"\']+)["\']'
    match = re.search(pattern, text, flags=re.MULTILINE)
    if match:
        return match.group(1).strip()[:200]
    return ""


def _read_codex_model() -> str:
    """Return the resolved model from the Codex config, or ""."""
    return _read_codex_config_string("model")


def _read_codex_reasoning_effort() -> str:
    """Return the resolved reasoning effort from the Codex config, or ""."""
    return _read_codex_config_string("model_reasoning_effort")


def _codex_cli_version(*, timeout_seconds: float = 2.0) -> tuple[str, str]:
    """Return (version_string, error). Version is like "codex-cli 0.148.0"."""
    try:
        completed = subprocess.run(
            ["codex", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=max(1.0, float(timeout_seconds)),
        )
    except FileNotFoundError as exc:
        return "", f"Codex CLI not found: {exc}"
    except subprocess.TimeoutExpired:
        return "", "codex --version timed out"
    except OSError as exc:
        return "", f"codex --version failed: {exc}"
    output = (completed.stdout + "\n" + completed.stderr).strip()
    if completed.returncode != 0 and not output:
        return "", f"codex --version exited {completed.returncode}"
    # Typical output: "codex-cli 0.148.0"
    # Be tolerant: first line that looks like version.
    for line in output.splitlines():
        line = line.strip()
        if "codex" in line.lower() and re.search(r"\d+\.\d+\.\d+", line):
            return line[:200], ""
    # Fallback: return raw output if no parse
    if output:
        return output.splitlines()[0].strip()[:200], ""
    return "", "codex --version produced no output"


def _probe_codex_minimal_exec(
    *,
    timeout_seconds: float = 10.0,
    pin: CodexModelPin | None = None,
    disabled_features: tuple[str, ...] = EXTRACTION_DISABLED_FEATURES,
) -> tuple[int, str, str]:
    """Run one minimal Codex invocation. Return (returncode, stdout, stderr).

    The extraction feature flags are passed only while Codex runs extraction.
    """
    model_pin = pin if pin is not None else load_extraction_model_pin()
    with tempfile.TemporaryDirectory(prefix="autoscout-stage-zero-codex-") as tmp:
        isolated = Path(tmp)
        response_path = isolated / "response.json"
        prompt = "Return OK"
        request = ModelRequest(
            prompt=prompt,
            response_path=response_path,
            # The pinned model too, so a retired pin stops the run here
            # (Issue #731).
            model=model_pin.model,
            reasoning_effort=model_pin.reasoning_effort,
            working_dir=isolated,
            # The probe runs the command extraction runs, feature flags
            # included, so a flag that breaks invocation is found here and
            # not 29 sources later (Issue #414).
            disabled_features=disabled_features,
        )
        result = model_client_for(model_pin).run(request, timeout=max(1.0, float(timeout_seconds)))
        if result.status is ModelCallStatus.TIMEOUT:
            return 124, "", "codex exec timed out"
        if result.status is ModelCallStatus.NOT_STARTED:
            if isinstance(result.spawn_error, FileNotFoundError):
                return 127, "", f"Codex CLI not found: {result.spawn_error}"
            return 127, "", f"codex exec failed: {result.spawn_error}"
        return int(result.returncode or 0), result.stdout, result.stderr


def _probe_claude_minimal_call(pin: CodexModelPin, *, timeout_seconds: float) -> ModelCallResult:
    """Ask a Claude API pin for a one-line JSON answer: the key file is read and the model answers."""
    with tempfile.TemporaryDirectory(prefix="autoscout-stage-zero-claude-") as tmp:
        request = ModelRequest(
            prompt='Return the JSON object {"ok": true}.',
            response_path=Path(tmp) / "response.json",
            model=pin.model,
            reasoning_effort=pin.reasoning_effort,
            max_output_tokens=2048,
            purpose="stage_zero_probe",
        )
        return model_client_for(pin).run(request, timeout=max(CLAUDE_PROBE_MIN_TIMEOUT_SECONDS, float(timeout_seconds)))


def _claude_pin_checks(
    pins: list[tuple[str, CodexModelPin]], probe: Any, timeout_seconds: float
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, str]]]:
    """Probe every pin on the Claude API backend once per model and effort.

    Returns a failing result, an inconclusive one, and the evidence of each
    probe.  A missing or rejected key and an unavailable model fail Stage 0;
    a timeout, a rate limit or exhausted credit is inconclusive, as a Codex
    probe that ends the same way is.
    """
    evidence: list[dict[str, str]] = []
    inconclusive: dict[str, Any] | None = None
    seen: set[tuple[str, str]] = set()
    for role, pin in pins:
        if pin.backend != CLAUDE_API_BACKEND or (pin.model, pin.reasoning_effort) in seen:
            continue
        seen.add((pin.model, pin.reasoning_effort))
        try:
            result = (
                probe(pin) if probe is not None else _probe_claude_minimal_call(pin, timeout_seconds=timeout_seconds)
            )
        except Exception as exc:
            result = ModelCallResult(ModelCallStatus.NOT_STARTED, stderr=type(exc).__name__)
        diagnostic = str(result.stderr or result.status.value)[:300]
        evidence.append(
            {
                "role": role,
                "backend": pin.backend,
                "model": pin.model,
                "status": result.status.value,
                "failure": result.failure.value,
            }
        )
        base = {"model": pin.model, "cli_version": "", "detail": {"claude_api": evidence}}
        if result.failure in (ModelFailure.KEY_MISSING, ModelFailure.KEY_REJECTED):
            return (
                {
                    "status": "fail",
                    "reason_code": CLAUDE_API_KEY_UNUSABLE,
                    "message": (
                        f"The {role} pin runs on the Claude API, and its key cannot be used: {diagnostic}. "
                        f"Put a working {CLAUDE_KEY_NAME} in {CLAUDE_KEY_FILE_DISPLAY}."
                    ),
                    **base,
                },
                None,
                evidence,
            )
        if result.failure is ModelFailure.MODEL_UNAVAILABLE:
            return (
                {
                    "status": "fail",
                    "reason_code": CODEX_PINNED_MODEL_UNAVAILABLE,
                    "message": (
                        f"The pinned {role} model '{pin.model}' is not available on the Claude API: {diagnostic}. "
                        f"Name a model this key can use in the {role} pin in {PIN_FILE_NAME}."
                    ),
                    **base,
                },
                None,
                evidence,
            )
        if result.failure in _PERMANENT_CLAUDE_REJECTIONS:
            return (
                {
                    "status": "fail",
                    "reason_code": CLAUDE_API_REQUEST_REJECTED,
                    "message": (
                        f"The Claude API rejects the {role} pin's request (model '{pin.model}', "
                        f"effort '{pin.reasoning_effort}'): {diagnostic}. "
                        f"Fix the {role} pin in {PIN_FILE_NAME}."
                    ),
                    **base,
                },
                None,
                evidence,
            )
        if result.status is not ModelCallStatus.ANSWERED and inconclusive is None:
            inconclusive = {
                "status": "unknown",
                "reason_code": CLAUDE_API_CHECK_UNKNOWN,
                "message": (
                    f"Could not verify the {role} model '{pin.model}' on the Claude API: {diagnostic}. "
                    "Check the API credit and retry."
                ),
                **base,
            }
    return None, inconclusive, evidence


def _codex_probe_verdict(
    role: str,
    model: str,
    effort: str,
    returncode: int,
    stderr: str,
    cli_version: str,
    pin_detail: dict[str, Any],
) -> dict[str, Any] | None:
    """Judge one Codex probe of the *role* pin; ``None`` when it shows the pin works.

    Every distinct Codex pin goes through the same checks: a CLI too old for
    the model, a model that cannot be served, a model or effort the CLI did
    not apply, and a non-zero exit.
    """
    combined_stderr = stderr.strip()
    lowered = combined_stderr.lower()

    # Detect model unsupported: 400 with "requires a newer version of Codex"
    if "requires a newer version of codex" in lowered or "model requires a newer version" in lowered:
        return {
            "status": "fail",
            "reason_code": CODEX_MODEL_UNSUPPORTED,
            "message": (
                f"The '{model}' model requires a newer version of Codex (installed {cli_version}). "
                "Please upgrade to the latest app or CLI and try again."
            ),
            "model": model,
            "cli_version": cli_version,
            "detail": {"stderr": combined_stderr[:500]},
        }

    # A retired or unknown pinned model stops the run before capture, with
    # the one action that fixes it (Issue #731).
    if model_unavailable_in(combined_stderr):
        action = (
            PIN_REPLACEMENT_ACTION
            if role == "extraction"
            else f"Name a model this account can use in the {role} pin in {PIN_FILE_NAME}."
        )
        return {
            "status": "fail",
            "reason_code": CODEX_PINNED_MODEL_UNAVAILABLE,
            "message": f"The pinned {role} model '{model}' is not available (CLI {cli_version}). " + action,
            "model": model,
            "cli_version": cli_version,
            "detail": {**pin_detail, "stderr": combined_stderr[:500], "returncode": returncode},
        }

    # The CLI prints the model and effort it resolved. When it resolved
    # something other than the pin, the flags did not take effect, and the
    # run cannot claim the pinned model (Issue #731).
    reported = header_profile(combined_stderr)
    expected = {"model": model, "reasoning_effort": effort}
    mismatched = sorted(key for key, value in reported.items() if key in expected and value != expected[key])
    if mismatched:
        return {
            "status": "fail",
            "reason_code": CODEX_MODEL_PIN_NOT_APPLIED,
            "message": (
                f"Codex did not apply the pinned {role} {', '.join(mismatched)} "
                f"(expected {expected}, reported {reported}, CLI {cli_version}). "
                "Check that the installed Codex CLI accepts '-m' and '-c model_reasoning_effort'."
            ),
            "model": model,
            "cli_version": cli_version,
            "detail": {**pin_detail, "reported_profile": reported},
        }

    if returncode != 0:
        # Distinguish inconclusive vs definite failure.
        # If stderr mentions auth/configuration/permission, it's a definite failure but not model unsupported.
        # For now, treat any non-zero with stderr as unknown unless it clearly indicates missing CLI.
        if "not found" in lowered or "no such file" in lowered:
            return {
                "status": "fail",
                "reason_code": CODEX_NOT_AVAILABLE,
                "message": (
                    f"Codex CLI not available (model '{model}', CLI {cli_version}): {combined_stderr[:300]}. "
                    "Install Codex CLI and verify with 'codex --version'."
                ),
                "model": model,
                "cli_version": cli_version,
                "detail": {"stderr": combined_stderr[:500], "returncode": returncode},
            }
        # Timeout is unknown, not fail, because we could not determine
        if "timed out" in lowered or returncode == 124:
            return {
                "status": "unknown",
                "reason_code": CODEX_CHECK_UNKNOWN,
                "message": (
                    f"Could not verify Codex toolchain (model '{model}', CLI {cli_version}): Codex probe timed out. "
                    "Check Codex CLI responsiveness with 'codex --version' and retry."
                ),
                "model": model,
                "cli_version": cli_version,
                "detail": {"stderr": combined_stderr[:500]},
            }
        return {
            "status": "unknown",
            "reason_code": CODEX_CHECK_UNKNOWN,
            "message": (
                f"Could not verify Codex toolchain (model '{model}', CLI {cli_version}): {combined_stderr[:300] or f'exit {returncode}'}. "
                f"Verify Codex CLI with 'codex --version' and the {role} pin in {PIN_FILE_NAME}."
            ),
            "model": model,
            "cli_version": cli_version,
            "detail": {"stderr": combined_stderr[:500], "returncode": returncode},
        }
    return None


def _reported_pin(pin: CodexModelPin) -> dict[str, str]:
    """Return a pin as Stage 0 reports it; the backend is named when it is not the default."""
    backend = {"backend": pin.backend} if pin.backend != CODEX_CLI_BACKEND else {}
    return {**pin.as_profile(), **backend, "source": pin.source}


def probe_codex_toolchain(
    *,
    codex_version_fn: Any | None = None,
    model_fn: Any | None = None,
    exec_fn: Any | None = None,
    features_fn: Any | None = None,
    timeout_seconds: float = 10.0,
    pin_fn: Any | None = None,
    global_model_fn: Any | None = None,
    description_pin_fn: Any | None = None,
    description_exec_fn: Any | None = None,
    adjudication_pin_fn: Any | None = None,
    claude_probe_fn: Any | None = None,
) -> dict[str, Any]:
    """Probe Codex toolchain readiness.

    Returns a dict with:
        status: "pass" | "fail" | "unknown"
        reason_code: bounded code
        message: fix-naming human message
        model: the model extraction will use (the repository pin)
        cli_version: version string
        detail: extra bounded detail, including the pin and the operator's
            global default model beside it (Issue #731)

    Injected fns are for deterministic tests. ``pin_fn`` returns the
    extraction pin, ``model_fn`` overrides the pinned model name, and
    ``global_model_fn`` reads the operator's global default, which is
    reported only. When None, the real subprocess/config probes run. The real probe costs one minimal
    Codex invocation (fails fast at 400 with no tokens when the model
    is unservable, otherwise minimal tokens for the OK response).

    The description pin is checked beside the extraction pin: it must be
    readable, and when it names another model or effort that model gets a
    minimal invocation of its own (``description_pin_fn`` and
    ``description_exec_fn`` inject both in tests). Until 2026-11-10 only
    the extraction model was probed, so a run whose description stages
    could not reach their model still passed Stage 0.

    A pin on the Claude API backend (Issue #1149) is checked first: its key
    file must hold a key the API accepts, and its model must answer one
    minimal request (``claude_probe_fn`` injects that probe in tests).  The
    Codex checks then run on the pins still on the Codex CLI: description
    adjudication always is, through the ``adjudication`` entry once the
    description pin has moved (``adjudication_pin_fn`` injects it).
    """
    # Resolve the pinned extraction model (Issue #731). A pin that cannot be
    # read fails closed: extraction must not fall back to the global default.
    try:
        pin = pin_fn() if pin_fn is not None else load_extraction_model_pin()
    except ModelPinError as exc:
        return {
            "status": "fail",
            "reason_code": CODEX_MODEL_PIN_INVALID,
            "message": f"The Codex extraction model pin is unusable: {str(exc)[:200]}. Fix {PIN_FILE_NAME}.",
            "model": "",
            "cli_version": "",
            "detail": {"pin_error": str(exc)[:200]},
        }
    try:
        description_pin = description_pin_fn() if description_pin_fn is not None else load_description_model_pin()
    except ModelPinError as exc:
        return {
            "status": "fail",
            "reason_code": CODEX_MODEL_PIN_INVALID,
            "message": f"The Codex description model pin is unusable: {str(exc)[:200]}. Fix {PIN_FILE_NAME}.",
            "model": "",
            "cli_version": "",
            "detail": {"pin_error": str(exc)[:200]},
        }
    # The extraction feature policy and flags belong to Codex only while it runs extraction.
    codex_extracts = pin.backend == CODEX_CLI_BACKEND
    probe_features = EXTRACTION_DISABLED_FEATURES if codex_extracts else ()
    claude_failure, claude_unknown, claude_evidence = _claude_pin_checks(
        [("extraction", pin), ("description", description_pin)], claude_probe_fn, timeout_seconds
    )
    if claude_failure is not None:
        return claude_failure
    # The Codex checks below cover the pins still on the Codex CLI, and the
    # pin adjudication actually runs on, resolved as the adapter resolves it.
    try:
        if adjudication_pin_fn is not None:
            adjudication_pin = adjudication_pin_fn()
        elif description_pin_fn is None:
            adjudication_pin = load_adjudication_model_pin()
        else:
            # An injected description pin stands for a pin file without an
            # ``adjudication`` entry.
            adjudication_pin = adjudication_model_pin(
                {
                    "description": {
                        "model": description_pin.model,
                        "reasoning_effort": description_pin.reasoning_effort,
                        "backend": description_pin.backend,
                    }
                },
                source=description_pin.source,
            )
    except ModelPinError as exc:
        return {
            "status": "fail",
            "reason_code": CODEX_MODEL_PIN_INVALID,
            "message": f"The Codex adjudication model pin is unusable: {str(exc)[:300]}. Fix {PIN_FILE_NAME}.",
            "model": "",
            "cli_version": "",
            "detail": {"pin_error": str(exc)[:300]},
        }
    codex_roles: list[tuple[str, CodexModelPin]] = []
    for role, candidate in (("extraction", pin), ("description", description_pin), ("adjudication", adjudication_pin)):
        same = any(
            (known.model, known.reasoning_effort) == (candidate.model, candidate.reasoning_effort)
            for _role, known in codex_roles
        )
        if candidate.backend == CODEX_CLI_BACKEND and not same:
            codex_roles.append((role, candidate))
    extraction_pin = pin
    # The first distinct Codex pin gets the full probe; its role names it in every diagnostic.
    primary_role, pin = codex_roles[0]
    try:
        global_default_model = str(global_model_fn() if global_model_fn is not None else _read_codex_model()).strip()[
            :200
        ]
    except Exception:
        global_default_model = ""
    pin_detail = {
        "model_pin": _reported_pin(extraction_pin),
        **(
            {"codex_probe_pin": {"role": primary_role, **pin.as_profile(), "source": pin.source}}
            if primary_role != "extraction"
            else {}
        ),
        # The configured pins, each with its own backend; adjudication is
        # reported beside the description pin whenever it is another pin.
        "description_model_pin": _reported_pin(description_pin),
        **(
            {"adjudication_model_pin": _reported_pin(adjudication_pin)}
            if _reported_pin(adjudication_pin) != _reported_pin(description_pin)
            else {}
        ),
        "global_default_model": global_default_model,
        # A different global default is expected and harmless: the pin wins.
        "global_default_matches_pin": global_default_model == extraction_pin.model,
    }

    # Resolve model
    try:
        model = str(model_fn() if model_fn is not None else pin.model).strip()[:200]
    except Exception:
        model = ""
        model_error = "could not read Codex model"
        return {
            "status": "unknown",
            "reason_code": CODEX_CHECK_UNKNOWN,
            "message": (
                f"Could not determine Codex model: {model_error}. "
                f"Check the {primary_role} pin in {PIN_FILE_NAME} and run 'codex --version'."
            ),
            "model": "",
            "cli_version": "",
            "detail": {"model_error": model_error[:200]},
        }

    # Resolve CLI version
    try:
        if codex_version_fn is not None:
            version_result = codex_version_fn()
            if isinstance(version_result, tuple) and len(version_result) == 2:
                cli_version, version_error = version_result
                cli_version = str(cli_version).strip()[:200]
                version_error = str(version_error).strip()[:200]
            else:
                cli_version = str(version_result).strip()[:200]
                version_error = ""
        else:
            cli_version, version_error = _codex_cli_version(timeout_seconds=2.0)
    except Exception as exc:
        return {
            "status": "unknown",
            "reason_code": CODEX_CHECK_UNKNOWN,
            "message": (
                f"Could not determine Codex CLI version: {exc}. Install Codex CLI and verify with 'codex --version'."
            ),
            "model": model,
            "cli_version": "",
            "detail": {"version_error": str(exc)[:200]},
        }

    if version_error or not cli_version:
        # Could not determine version -> unknown, not pass
        return {
            "status": "unknown",
            "reason_code": CODEX_CHECK_UNKNOWN,
            "message": (
                f"Could not determine Codex CLI version{': ' + version_error if version_error else ''}. "
                "Install Codex CLI and verify with 'codex --version'." + (f" Model is '{model}'." if model else "")
            ),
            "model": model,
            "cli_version": cli_version,
            "detail": {"version_error": version_error[:500] if version_error else "no version output"},
        }

    if not model:
        return {
            "status": "unknown",
            "reason_code": CODEX_CHECK_UNKNOWN,
            "message": (
                f"Could not determine configured Codex model (CLI {cli_version}). "
                f"Check the {primary_role} pin in {PIN_FILE_NAME} and verify with 'codex --version'."
            ),
            "model": "",
            "cli_version": cli_version,
            "detail": {"cli_version": cli_version},
        }

    # Feature policy gate. It runs before the exec probe because it is
    # local and free, and because a policy that cannot be satisfied must not
    # spend a Codex request (Issue #414).
    if codex_extracts:
        feature_policy = verify_extraction_feature_policy(
            features_fn=features_fn,
            timeout_seconds=min(5.0, max(1.0, float(timeout_seconds))),
            cli_version=cli_version,
        )
    else:
        feature_policy = {"status": "pass", "detail": {"not_checked": "extraction does not run on the Codex CLI"}}
    feature_detail = dict(feature_policy.get("detail", {}) or {})
    if str(feature_policy.get("status", "")) != "pass":
        return {
            "status": str(feature_policy.get("status", "unknown")),
            "reason_code": str(feature_policy.get("reason_code", CODEX_CHECK_UNKNOWN)),
            "message": str(feature_policy.get("message", "")),
            "model": model,
            "cli_version": cli_version,
            "detail": {"feature_policy": feature_detail},
        }

    # Minimal exec probe
    try:
        if exec_fn is not None:
            exec_result = exec_fn()
            if isinstance(exec_result, tuple) and len(exec_result) == 3:
                returncode, _stdout, stderr = exec_result
                stderr = str(stderr)[:2000]
            elif isinstance(exec_result, dict):
                # Allow dict injection with keys returncode/stderr etc
                returncode = int(exec_result.get("returncode", 0))
                stderr = str(exec_result.get("stderr", ""))[:2000]
            else:
                returncode = 0
                stderr = ""
        else:
            probe_pin = pin if model == pin.model else CodexModelPin(model, pin.reasoning_effort, pin.source)
            returncode, _stdout, stderr = _probe_codex_minimal_exec(
                timeout_seconds=timeout_seconds, pin=probe_pin, disabled_features=probe_features
            )
            stderr = str(stderr)[:2000]
    except Exception as exc:
        return {
            "status": "unknown",
            "reason_code": CODEX_CHECK_UNKNOWN,
            "message": (
                f"Could not check Codex toolchain (model '{model}', CLI {cli_version}): {exc}. "
                "Verify Codex CLI installation with 'codex --version' and model in ~/.codex/config.toml."
            ),
            "model": model,
            "cli_version": cli_version,
            "detail": {"exec_error": str(exc)[:500]},
        }

    reported = header_profile(stderr.strip())
    verdict = _codex_probe_verdict(
        primary_role, model, pin.reasoning_effort, returncode, stderr, cli_version, pin_detail
    )
    if verdict is not None:
        return verdict

    # The description stages and adjudication run on their own pins. The
    # probe above already covers a pin that names the same model and effort.
    # On the all-Codex configuration main has always had (no pin on the
    # Claude API, adjudication on the description pin), a distinct
    # description pin is judged as before #1149: only a model that cannot be
    # served fails it. Every pin of the new configurations gets the full checks.
    judged_as_before = CLAUDE_API_BACKEND not in (extraction_pin.backend, description_pin.backend) and (
        _reported_pin(adjudication_pin) == _reported_pin(description_pin)
    )
    for role, extra_pin in codex_roles[1:]:
        if extra_pin.as_profile() == {"model": model, "reasoning_effort": pin.reasoning_effort}:
            continue
        try:
            if description_exec_fn is not None:
                extra_code, _out, extra_stderr = description_exec_fn()
            elif exec_fn is None:
                extra_code, _out, extra_stderr = _probe_codex_minimal_exec(
                    timeout_seconds=timeout_seconds, pin=extra_pin, disabled_features=probe_features
                )
            else:
                extra_code, extra_stderr = 0, ""
        except Exception:
            extra_code, extra_stderr = 0, ""
        if judged_as_before:
            if model_unavailable_in(str(extra_stderr)[:2000]):
                return {
                    "status": "fail",
                    "reason_code": CODEX_PINNED_MODEL_UNAVAILABLE,
                    "message": (
                        f"The pinned {role} model '{extra_pin.model}' is not available (CLI {cli_version}). "
                        f"Name a model this account can use in the {role} pin in {PIN_FILE_NAME}."
                    ),
                    "model": model,
                    "cli_version": cli_version,
                    "detail": {**pin_detail, "stderr": str(extra_stderr).strip()[:500]},
                }
            continue
        verdict = _codex_probe_verdict(
            role,
            extra_pin.model,
            extra_pin.reasoning_effort,
            int(extra_code or 0),
            str(extra_stderr)[:2000],
            cli_version,
            pin_detail,
        )
        if verdict is not None:
            return verdict

    if claude_evidence:
        pin_detail["claude_api"] = claude_evidence
    if claude_unknown is not None:
        return {**claude_unknown, "cli_version": cli_version, "detail": {**pin_detail, "reported_profile": reported}}

    # Success
    return {
        "status": "pass",
        "reason_code": CODEX_HEALTHY,
        "message": f"Codex toolchain is healthy (model '{model}', CLI {cli_version})",
        "model": model,
        "cli_version": cli_version,
        # The feature policy evidence is retained on a healthy run too. A
        # run must be able to prove what extraction had switched off, not
        # only that nothing failed (Issue #414). The pin evidence likewise
        # shows which model ran and what the global default was (#731).
        "detail": {
            "feature_policy": feature_detail,
            **pin_detail,
            "reported_profile": reported,
            "pin_applied": "verified" if reported.get("model") == model else "unverified",
        },
    }


def probe_discovery_adapter(
    enrichment_adapter: Any | None,
    *,
    preflight_fn: Any | None = None,
) -> dict[str, Any]:
    """Probe discovery adapter readiness without spending Brave quota.

    Uses GET /health via the adapter's preflight. Never calls /search.
    Returns dict with:
        status: "pass" | "degraded" | "unknown"
        reason_code: one of PREFLIGHT_* or DISCOVERY_CHECK_UNKNOWN
        message: fix-naming message
        endpoint: endpoint string when known
    """
    # If no adapter at all, treat as endpoint not configured (degraded, not unknown)
    if enrichment_adapter is None and preflight_fn is None:
        return {
            "status": "degraded",
            "reason_code": PREFLIGHT_ENDPOINT_NOT_CONFIGURED,
            "message": (
                "AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT is not set. "
                "Set it to the loopback adapter URL, e.g., http://127.0.0.1:8899/search?q={query}, "
                "and ensure the grounded-search-adapter is running with AUTOSCOUT_SEARCH_API_KEY set."
            ),
            "endpoint": "",
        }

    # Determine preflight result
    result: Any = None
    try:
        if preflight_fn is not None:
            result = preflight_fn()
        elif enrichment_adapter is not None:
            # Prefer the service preflight seam
            preflight = getattr(enrichment_adapter, "preflight", None)
            if callable(preflight):
                result = preflight()
            else:
                return {
                    "status": "unknown",
                    "reason_code": DISCOVERY_CHECK_UNKNOWN,
                    "message": (
                        "Could not check discovery adapter: preflight seam unavailable. "
                        "Check enrichment adapter configuration."
                    ),
                    "endpoint": "",
                }
        else:
            return {
                "status": "unknown",
                "reason_code": DISCOVERY_CHECK_UNKNOWN,
                "message": "Could not check discovery adapter: no adapter and no preflight function.",
                "endpoint": "",
            }
    except Exception as exc:
        return {
            "status": "unknown",
            "reason_code": DISCOVERY_CHECK_UNKNOWN,
            "message": (
                f"Could not check discovery adapter: {exc}. "
                "Verify AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT and that the adapter is running with AUTOSCOUT_SEARCH_API_KEY set."
            ),
            "endpoint": "",
            "detail": {"error": str(exc)[:500]},
        }

    # Normalize result to PreflightResult-like
    reason_code = ""
    message = ""
    ok = False

    if isinstance(result, PreflightResult):
        ok = bool(result.ok)
        reason_code = str(result.reason_code or "").strip()
        message = str(result.message or "").strip()
    elif isinstance(result, tuple) and len(result) == 2:
        ok, raw_reason = result
        ok = bool(ok)
        reason_code = str(raw_reason or "").strip() or (PREFLIGHT_HEALTHY if ok else PREFLIGHT_ADAPTER_NO_RESPONSE)
        message = "discovery adapter is healthy" if ok else "discovery adapter did not answer preflight"
    elif isinstance(result, dict):
        # Allow dict injection for tests
        ok = bool(result.get("ok", False))
        reason_code = str(result.get("reason_code", "")).strip()
        message = str(result.get("message", "")).strip()
    elif isinstance(result, bool):
        ok = result
        reason_code = PREFLIGHT_HEALTHY if ok else PREFLIGHT_ADAPTER_NO_RESPONSE
        message = "discovery adapter is healthy" if ok else "discovery adapter did not answer preflight"
    else:
        return {
            "status": "unknown",
            "reason_code": DISCOVERY_CHECK_UNKNOWN,
            "message": (
                f"Could not check discovery adapter: unexpected preflight result {type(result).__name__}. "
                "Verify AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT."
            ),
            "endpoint": "",
        }

    # Map to three-state status
    if ok and reason_code == PREFLIGHT_HEALTHY:
        return {
            "status": "pass",
            "reason_code": PREFLIGHT_HEALTHY,
            "message": message or "discovery adapter is healthy",
            "endpoint": os.environ.get("AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT", "").strip(),
        }

    # Degraded states: endpoint not configured, key missing, not running, no response, unavailable, vendor error
    degraded_codes = {
        PREFLIGHT_ENDPOINT_NOT_CONFIGURED,
        PREFLIGHT_ADAPTER_KEY_MISSING,
        PREFLIGHT_ADAPTER_NOT_RUNNING,
        PREFLIGHT_ADAPTER_NO_RESPONSE,
        PREFLIGHT_ADAPTER_UNAVAILABLE,
        PREFLIGHT_ADAPTER_VENDOR_ERROR,
    }

    if reason_code in degraded_codes:
        # Ensure message names the fix; enrich generic messages.
        if reason_code == PREFLIGHT_ENDPOINT_NOT_CONFIGURED:
            fix_message = (
                "AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT is not set. "
                "Set it to http://127.0.0.1:8899/search?q={query} and run the grounded-search-adapter with AUTOSCOUT_SEARCH_API_KEY set."
            )
            message = fix_message
        elif reason_code == PREFLIGHT_ADAPTER_KEY_MISSING:
            # Already fix-naming, keep as is
            message = message or "AUTOSCOUT_SEARCH_API_KEY is not set"
        elif reason_code in {PREFLIGHT_ADAPTER_NOT_RUNNING, PREFLIGHT_ADAPTER_NO_RESPONSE}:
            message = (
                message
                or "discovery adapter is not running. Start it with 'python -m autoscout.grounded_search_adapter' and set AUTOSCOUT_SEARCH_API_KEY."
            )
        return {
            "status": "degraded",
            "reason_code": reason_code,
            "message": message,
            "endpoint": os.environ.get("AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT", "").strip(),
        }

    # Unknown code -> unknown status
    return {
        "status": "unknown",
        "reason_code": reason_code or DISCOVERY_CHECK_UNKNOWN,
        "message": message
        or (
            f"Could not determine discovery adapter health (reason {reason_code}). "
            "Verify AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT and adapter status with GET /health."
        ),
        "endpoint": os.environ.get("AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT", "").strip(),
    }


def build_stage_zero_result(
    codex_result: dict[str, Any],
    discovery_result: dict[str, Any],
) -> dict[str, Any]:
    """Combine codex and discovery checks into a Stage 0 result."""
    codex_status = str(codex_result.get("status", "unknown"))
    discovery_status = str(discovery_result.get("status", "unknown"))

    if codex_status == "fail":
        overall = "fail"
    elif codex_status == "unknown" or discovery_status in {"unknown", "degraded"}:
        # Unknown trumps degraded for visibility, but both are not pass
        if codex_status == "unknown" or discovery_status == "unknown":
            overall = "unknown"
        else:
            overall = "degraded"
    elif discovery_status == "degraded":
        overall = "degraded"
    else:
        overall = "pass"

    return {
        "stage": STAGE_ZERO_NAME,
        "overall_status": overall,
        "codex": dict(codex_result),
        "discovery": dict(discovery_result),
    }


__all__ = [
    "CODEX_CHECK_UNKNOWN",
    "CODEX_HEALTHY",
    "CODEX_MODEL_PIN_INVALID",
    "CODEX_MODEL_PIN_NOT_APPLIED",
    "CODEX_MODEL_UNSUPPORTED",
    "CODEX_NOT_AVAILABLE",
    "CODEX_PINNED_MODEL_UNAVAILABLE",
    "DISCOVERY_CHECK_UNKNOWN",
    "STAGE_ZERO_NAME",
    "build_stage_zero_result",
    "probe_codex_toolchain",
    "probe_discovery_adapter",
]
