"""Bounded, separate captures for ranking rows and ranking identity."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from autoscout.capture_sanitization import sanitize_captured_html


class RankingCaptureError(ValueError):
    """A ranking capture cannot be retained within its evidence bounds."""


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


def _require_text(value: Any, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RankingCaptureError(f"ranking capture {field} must be text")
    return value


@dataclass(frozen=True)
class RankingPageEvidence:
    """Rows and identity HTML kept as independent bounded evidence."""

    rows_html: str = ""
    identity_html: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RankingPageEvidence:
        """Read the two ranking HTML fields from a page mapping."""
        return cls(
            rows_html=_require_text(value.get("html", ""), "html"),
            identity_html=_require_text(value.get("identity_html", ""), "identity_html"),
        )

    def to_mapping(self, value: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Return a page mapping with the two HTML fields replaced."""
        result = dict(value or {})
        result["html"] = self.rows_html
        if self.identity_html:
            result["identity_html"] = self.identity_html
        else:
            result.pop("identity_html", None)
        return result

    def sanitized(self, *, rows_max_bytes: int, identity_max_bytes: int) -> RankingPageEvidence:
        """Sanitize each capture and refuse an oversized retained result."""
        if rows_max_bytes < 1 or identity_max_bytes < 1:
            raise ValueError("ranking capture bounds must be positive")
        rows = sanitize_captured_html(self.rows_html)
        identity = sanitize_captured_html(self.identity_html) if self.identity_html else ""
        if _utf8_size(rows) > rows_max_bytes:
            raise RankingCaptureError(
                f"ranking rows capture exceeds the {rows_max_bytes}-byte limit after sanitization"
            )
        if _utf8_size(identity) > identity_max_bytes:
            raise RankingCaptureError(
                f"ranking identity capture exceeds the {identity_max_bytes}-byte limit after sanitization"
            )
        return RankingPageEvidence(rows_html=rows, identity_html=identity)
