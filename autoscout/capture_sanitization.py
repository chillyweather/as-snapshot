"""Shared bounded HTML sanitization for retained source captures."""

from __future__ import annotations

import re

_CAPTURED_HTML_SANITIZE_PATTERNS: list[tuple[str, str]] = [
    (r'"token":"[a-f0-9]{32}"', '"token":"REDACTED"'),
    (r'"token":"[A-Za-z0-9+/=]{40,}"', '"token":"REDACTED"'),
]


def _redact_html_attribute(tag: str, attribute_name: str) -> str:
    pattern = rf"(\b{re.escape(attribute_name)}\s*=\s*)([\"'])([^\"']*)(\2)"
    return re.sub(pattern, r"\1\2REDACTED\4", tag, count=1, flags=re.IGNORECASE)


def _sanitize_meta_tag(match: re.Match[str]) -> str:
    tag = match.group(0)
    name_match = re.search(r"\bname\s*=\s*([\"'])([^\"']*)\1", tag, flags=re.IGNORECASE)
    if name_match and "csrf" in name_match.group(2).casefold():
        return _redact_html_attribute(tag, "content")
    return tag


def _sanitize_input_tag(match: re.Match[str]) -> str:
    tag = match.group(0)
    field_names = re.findall(
        r"\b(?:name|id)\s*=\s*([\"'])([^\"']*)\1",
        tag,
        flags=re.IGNORECASE,
    )
    if any(re.search(r"csrf|authenticity|token", field_name, flags=re.IGNORECASE) for _, field_name in field_names):
        return _redact_html_attribute(tag, "value")
    return tag


def sanitize_captured_html(markup: str) -> str:
    """Redact security and analytics values while preserving visible metadata."""
    for pattern, replacement in _CAPTURED_HTML_SANITIZE_PATTERNS:
        markup = re.sub(pattern, replacement, markup)
    markup = re.sub(r"<meta\b[^>]*>", _sanitize_meta_tag, markup, flags=re.IGNORECASE)
    markup = re.sub(r"<input\b[^>]*>", _sanitize_input_tag, markup, flags=re.IGNORECASE)
    markup = re.sub(
        r"(\bwindow\.Mntl\.csrfInit\(\s*)([\"'])[^\"']*\2",
        lambda match: f"{match.group(1)}{match.group(2)}REDACTED{match.group(2)}",
        markup,
        flags=re.IGNORECASE,
    )
    markup = re.sub(
        r"(\bsetTargeting\(\s*)([\"'])grsession\2(\s*,\s*)([\"'])[^\"']*\4",
        lambda match: f"{match.group(1)}{match.group(2)}grsession{match.group(2)}"
        f"{match.group(3)}{match.group(4)}REDACTED{match.group(4)}",
        markup,
        flags=re.IGNORECASE,
    )
    markup = re.sub(r"([?&]apiKey=)[^&\"'\s]+", r"\1REDACTED", markup, flags=re.IGNORECASE)
    markup = re.sub(
        r"(\bconfig\.apiKey\s*=\s*)([\"'])([^\"']*)(\2)",
        r"\1\2REDACTED\4",
        markup,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"(\bconfig\.apiKey\s*=\s*)(?![\"'])[^,;\s]+",
        r"\1REDACTED",
        markup,
        flags=re.IGNORECASE,
    )
