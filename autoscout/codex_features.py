"""The Codex feature policy for extraction work items (issue #414).

An extraction work item must ground its output in the retained Source
Capture.  The installed Codex CLI carries a `memories` feature that injects
a memory-folder instruction into every task, and that folder holds notes
from earlier operations about the same Configured Sources.  An
Agent-Extracted Mention that came from such a note, rather than from the
captured page, cannot be told apart in the evidence.

The editor decided that extraction disables the feature and that the run
proves it.  The proof is local and free: `codex features list` reports the
effective value of every feature under the flags it is given, so asking the
CLI with the extraction flags applied states what extraction will see.

Two things fail closed here.  A feature name the CLI does not know is
rejected by the CLI itself, and a feature that is still enabled under its
own disable flag means the flag did not take effect.  Either way the run
cannot claim that memory is off, so it must not start.

The prompt instruction from issue #411 stays.  It is advisory, this is not,
and extraction correctness must not depend on either alone.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any

# The features extraction disables, one --disable flag each.
EXTRACTION_DISABLED_FEATURES: tuple[str, ...] = ("memories",)

CODEX_FEATURES_HEALTHY = "codex_features_healthy"
CODEX_FEATURE_REJECTED = "codex_feature_rejected"
CODEX_FEATURE_STILL_ENABLED = "codex_feature_still_enabled"
CODEX_FEATURES_CHECK_UNKNOWN = "codex_features_check_unknown"

# `codex features list` prints one feature per line as name, state, and
# effective value, separated by runs of spaces. The state itself can hold a
# single space, as in "under development", so the split is on two or more.
_FIELD_SPLIT = re.compile(r"\s{2,}")

_FALSE_VALUES = frozenset({"false", "off", "disabled"})

# Bounds on what a probe result can put into a run artifact.
_MAX_FEATURE_NAME_CHARS = 120
_MAX_FEATURE_STATE_CHARS = 60
_MAX_FEATURE_VALUE_CHARS = 20
_MAX_MESSAGE_OUTPUT_CHARS = 200
_MAX_DETAIL_OUTPUT_CHARS = 500


def extraction_feature_flags() -> list[str]:
    """Return the CLI flags that disable every feature in the policy."""
    flags: list[str] = []
    for name in EXTRACTION_DISABLED_FEATURES:
        flags.extend(["--disable", name])
    return flags


def _rename_remedy() -> str:
    """Return the one instruction for a feature the CLI no longer knows."""
    return (
        "Check 'codex features list' and update EXTRACTION_DISABLED_FEATURES in "
        "autoscout/codex_features.py to the current name."
    )


def parse_codex_feature_states(output: str) -> dict[str, dict[str, str]]:
    """Return ``{name: {"state": ..., "value": ...}}`` from probe output.

    Unreadable lines are skipped rather than guessed at.  An empty result
    means the output could not be read, which the caller reports as an
    unknown check instead of a pass.
    """
    found: dict[str, dict[str, str]] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fields = _FIELD_SPLIT.split(line)
        if len(fields) < 3:
            continue
        name = fields[0].strip()
        if not name or " " in name:
            continue
        found[name[:_MAX_FEATURE_NAME_CHARS]] = {
            "state": fields[1].strip()[:_MAX_FEATURE_STATE_CHARS],
            "value": fields[2].strip().lower()[:_MAX_FEATURE_VALUE_CHARS],
        }
    return found


def _probe_codex_features(
    flags: list[str],
    *,
    timeout_seconds: float = 5.0,
) -> tuple[int, str, str]:
    """Ask the CLI for its feature table under *flags*.

    Costs no model request: `features list` is local.
    """
    command = ["codex", *flags, "features", "list"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=max(1.0, float(timeout_seconds)),
        )
    except FileNotFoundError as exc:
        return 127, "", f"Codex CLI not found: {exc}"
    except subprocess.TimeoutExpired:
        return 124, "", "codex features list timed out"
    except OSError as exc:
        return 127, "", f"codex features list failed: {exc}"
    return completed.returncode, completed.stdout, completed.stderr


def verify_extraction_feature_policy(
    *,
    features_fn: Any | None = None,
    timeout_seconds: float = 5.0,
    cli_version: str = "",
) -> dict[str, Any]:
    """Prove that the extraction flags turn every policy feature off.

    Returns a three-state result, so "could not check" is never confused
    with "memory is off":

    - ``pass``: the CLI accepted every flag and reports every policy
      feature as disabled.  The detail states the effective values, which
      is the evidence the run retains.
    - ``fail``: the CLI rejected a feature name, or a feature is still
      enabled under its own disable flag.
    - ``unknown``: the probe could not run or its output could not be read.
    """
    features = EXTRACTION_DISABLED_FEATURES
    flags = extraction_feature_flags()
    installed = f" (CLI {cli_version})" if cli_version else ""
    try:
        if features_fn is not None:
            returncode, stdout, stderr = features_fn(list(flags))
            returncode, stdout, stderr = int(returncode), str(stdout), str(stderr)
        else:
            returncode, stdout, stderr = _probe_codex_features(flags, timeout_seconds=timeout_seconds)
    except Exception as exc:
        return {
            "status": "unknown",
            "reason_code": CODEX_FEATURES_CHECK_UNKNOWN,
            "message": (
                f"Could not check the Codex feature policy: {exc}. Verify with 'codex {' '.join(flags)} features list'."
            ),
            "detail": {"disabled_features": list(features), "probe_error": str(exc)[:_MAX_MESSAGE_OUTPUT_CHARS]},
        }

    combined = f"{stdout}\n{stderr}".strip()
    if returncode != 0:
        lowered = combined.lower()
        if "unknown feature" in lowered:
            return {
                "status": "fail",
                "reason_code": CODEX_FEATURE_REJECTED,
                "message": (
                    f"The installed Codex CLI{installed} rejected a feature AutoScout disables for "
                    f"extraction: {combined[:_MAX_MESSAGE_OUTPUT_CHARS]}. "
                    f"The feature may have been renamed or removed. {_rename_remedy()}"
                ),
                "detail": {
                    "disabled_features": list(features),
                    "stderr": combined[:_MAX_DETAIL_OUTPUT_CHARS],
                    "returncode": returncode,
                },
            }
        if "timed out" in lowered or returncode == 124:
            return {
                "status": "unknown",
                "reason_code": CODEX_FEATURES_CHECK_UNKNOWN,
                "message": (
                    "Could not check the Codex feature policy: the probe timed out. "
                    f"Retry 'codex {' '.join(flags)} features list'."
                ),
                "detail": {"disabled_features": list(features), "stderr": combined[:_MAX_DETAIL_OUTPUT_CHARS]},
            }
        return {
            "status": "unknown",
            "reason_code": CODEX_FEATURES_CHECK_UNKNOWN,
            "message": (
                f"Could not check the Codex feature policy: {combined[:200] or f'exit {returncode}'}. "
                f"Verify with 'codex {' '.join(flags)} features list'."
            ),
            "detail": {
                "disabled_features": list(features),
                "stderr": combined[:_MAX_DETAIL_OUTPUT_CHARS],
                "returncode": returncode,
            },
        }

    states = parse_codex_feature_states(stdout)
    if not states:
        return {
            "status": "unknown",
            "reason_code": CODEX_FEATURES_CHECK_UNKNOWN,
            "message": (
                "Could not read the Codex feature table, so extraction cannot prove that memory is off. "
                f"Check the output of 'codex {' '.join(flags)} features list'."
            ),
            "detail": {
                "disabled_features": list(features),
                "output_sample": stdout.strip()[:_MAX_MESSAGE_OUTPUT_CHARS],
            },
        }

    effective: dict[str, str] = {}
    feature_states: dict[str, str] = {}
    missing: list[str] = []
    enabled: list[str] = []
    for name in features:
        entry = states.get(name)
        if entry is None:
            missing.append(name)
            continue
        effective[name] = entry["value"]
        feature_states[name] = entry["state"]
        if entry["value"] not in _FALSE_VALUES:
            enabled.append(name)

    detail = {
        "disabled_features": list(features),
        "effective_values": effective,
        "feature_states": feature_states,
    }
    if missing:
        return {
            "status": "fail",
            "reason_code": CODEX_FEATURE_REJECTED,
            "message": (
                f"The installed Codex CLI{installed} does not list {', '.join(missing)}, "
                f"so extraction cannot prove that the feature is off. {_rename_remedy()}"
            ),
            "detail": {**detail, "missing_features": missing},
        }
    if enabled:
        still_on = ", ".join(f"{name}={effective[name]}" for name in enabled)
        return {
            "status": "fail",
            "reason_code": CODEX_FEATURE_STILL_ENABLED,
            "message": (
                f"A Codex feature stayed enabled under its own disable flag: {still_on}. "
                "An extraction item could then read guidance from earlier operations. "
                f"Reproduce with 'codex {' '.join(flags)} features list' and report the CLI behaviour."
            ),
            "detail": {**detail, "still_enabled": enabled},
        }
    return {
        "status": "pass",
        "reason_code": CODEX_FEATURES_HEALTHY,
        "message": f"Extraction disables {', '.join(features)} and the Codex CLI confirms it",
        "detail": detail,
    }


__all__ = [
    "CODEX_FEATURES_CHECK_UNKNOWN",
    "CODEX_FEATURES_HEALTHY",
    "CODEX_FEATURE_REJECTED",
    "CODEX_FEATURE_STILL_ENABLED",
    "EXTRACTION_DISABLED_FEATURES",
    "extraction_feature_flags",
    "parse_codex_feature_states",
    "verify_extraction_feature_policy",
]
