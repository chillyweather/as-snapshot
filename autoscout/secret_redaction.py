"""Recognize and redact credential-shaped strings before they are persisted.

This module is deliberately narrow and pattern-based (issue #287): it exists
to catch recognizable credential *formats* — Figma personal access tokens,
GitHub tokens, OpenAI-style keys, AWS access key IDs, Slack tokens, bearer
tokens, and generic ``key=``/``secret=``-style assignments — regardless of
whether the generic key-name sanitizer elsewhere in the codebase (which
drops dict keys/values containing words like "token" or "secret") happens to
recognize the surrounding context.

Patterns are kept conservative on purpose: a bare 64-character sha256 hex
digest (e.g. ``capture_sha256``) or a normal URL must never be redacted.
Only hex/base64-looking blobs that follow a credential-style ``key=``,
``token=``, ``secret=``, or ``password=`` prefix are treated as secrets.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from collections.abc import Sequence
from re import Match
from re import Pattern
from typing import Any

# Each entry is (kind, compiled_pattern, group). ``group`` is 0 when the
# entire match is the secret; otherwise it names the capture group holding
# just the secret value, so surrounding non-secret context (a "Bearer "
# prefix, a "token=" assignment, quoting) is preserved in the output.
_PATTERN_SPECS: tuple[tuple[str, Pattern[str], int], ...] = (
    ("figma_pat", re.compile(r"figd_[A-Za-z0-9_-]{20,}"), 0),
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), 0),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,255}"), 0),
    # Real OpenAI-style keys are "sk-" (optionally "sk-proj-"/"sk-admin-"/
    # "sk-svcacct-") followed by a long run WITHOUT hyphens. The lookbehind
    # and the hyphen-free body keep hyphenated prose/URL slugs such as
    # ".../high-risk-by-author-name" from matching.
    ("openai_key", re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-|admin-|svcacct-)?[A-Za-z0-9_]{20,}"), 0),
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}"), 0),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), 0),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+([A-Za-z0-9\-_.=]{20,})"), 1),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:token|api[_-]?key|secret[_-]?key|access[_-]?key|secret|password|passwd|pwd)\b"
            r"['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9+/=_\-]{16,})['\"]?"
        ),
        1,
    ),
)


def _replace_match(kind: str, group: int) -> Any:
    def _replacer(match: Match[str]) -> str:
        full = match.group(0)
        if group == 0:
            return f"[REDACTED:{kind}]"
        start, end = match.span(group)
        match_start = match.start(0)
        rel_start = start - match_start
        rel_end = end - match_start
        return full[:rel_start] + f"[REDACTED:{kind}]" + full[rel_end:]

    return _replacer


def redact_secrets(text: str) -> str:
    """Replace recognizable credential shapes in ``text`` with markers.

    Non-secret surrounding context (assignment key names, "Bearer " prefix,
    quoting) is preserved so failure diagnostics remain useful.
    """
    if not isinstance(text, str) or not text:
        return text
    result = text
    for kind, pattern, group in _PATTERN_SPECS:
        result = pattern.sub(_replace_match(kind, group), result)
    return result


def contains_secret(text: str) -> bool:
    """Return True if ``text`` contains any recognizable credential shape."""
    if not isinstance(text, str) or not text:
        return False
    return any(pattern.search(text) for _, pattern, _ in _PATTERN_SPECS)


def redact_structure(value: Any) -> Any:
    """Recursively redact strings nested inside dicts/lists/tuples.

    Non-string, non-container leaves (numbers, bools, None) pass through
    unchanged. The input's shape (dict/list/tuple) is preserved.
    """
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, Mapping):
        return {key: redact_structure(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(redact_structure(item) for item in value)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [redact_structure(item) for item in value]
    return value
