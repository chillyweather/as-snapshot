"""Shared bounds and the shared bounded writer for file-backed source captures."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any

from autoscout.http_decoding import MAX_RESPONSE_BYTES
from autoscout.ranking_detail_route import RankingDetailRoute
from autoscout.ranking_detail_route import ranking_detail_scope
from autoscout.ranking_evidence import RankingCaptureError
from autoscout.ranking_evidence import RankingPageEvidence

# This factor separates response-body bounds from serialized JSON bytes and
# covers JSON escaping, indentation, metadata, and bounded aggregation.
CAPTURE_SERIALIZATION_HEADROOM_FACTOR = 1.5
DEFAULT_CAPTURE_MAX_RESPONSE_BYTES = 750_000


class CaptureKind(StrEnum):
    """The kind of file-backed source capture a writer produces."""

    RANKING = "ranking"
    EXTRACTION = "extraction"


class CaptureWriteError(Exception):
    """A bounded source capture could not be serialized or written.

    This is the expected, recoverable capture-domain failure, deliberately
    independent of :class:`ValueError`: it covers both serialization and
    filesystem write failures, so it must not couple into value-validation
    error policies elsewhere.  Diagnostics derived from it are bounded and
    free of filesystem paths.
    """


def capture_serialization_limit(max_response_bytes: int) -> int:
    """Return the UTF-8 byte limit for one serialized capture."""
    return math.ceil(max_response_bytes * CAPTURE_SERIALIZATION_HEADROOM_FACTOR)


def write_bounded_capture(
    capture: Mapping[str, Any],
    *,
    max_response_bytes: int,
    kind: CaptureKind,
    destination: Path,
) -> None:
    """Serialize, bound-check, then write one capture file to disk.

    The writer JSON-serializes the capture, enforces the UTF-8 aggregate
    serialization bound, creates the destination's parent directory, and
    writes the file.  Expected JSON serialization and filesystem failures
    normalize into :class:`CaptureWriteError`; diagnostics never include
    absolute paths.  Nothing is written and no parent directory is created
    when serialization or the bound check fails.
    """
    try:
        serialized = json.dumps(capture, indent=2, ensure_ascii=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise CaptureWriteError(f"{kind.value} capture could not be serialized: {type(exc).__name__}") from exc
    limit = capture_serialization_limit(max_response_bytes)
    if len(serialized.encode("utf-8")) > limit:
        raise CaptureWriteError(f"{kind.value} capture exceeds the {limit}-byte limit after serialization")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(serialized, encoding="utf-8")
    except OSError as exc:
        # OSError.strerror is the operating-system message without the
        # offending path, so the diagnostic stays bounded and path-free.
        raise CaptureWriteError(
            f"{kind.value} capture could not be written: {exc.strerror or type(exc).__name__}"
        ) from exc


def retain_ranking_detail_page(detail: Mapping[str, Any], route: RankingDetailRoute) -> dict[str, str]:
    """Retain only one detail route scope under the shared detail bound."""
    scope = ranking_detail_scope(str(detail.get("html", "")), route.scope_selector)
    if scope.failure_reason:
        raise RankingCaptureError(f"ranking detail scope capture failed: {scope.failure_reason}")
    retained_detail = RankingPageEvidence(rows_html=scope.html).sanitized(
        rows_max_bytes=MAX_RESPONSE_BYTES,
        identity_max_bytes=MAX_RESPONSE_BYTES,
    )
    return {
        "source_page": str(detail.get("source_page", "")),
        "detail_url": str(detail.get("detail_url", detail.get("resolved_url", ""))),
        "resolved_url": str(detail.get("resolved_url", "")),
        "html": retained_detail.rows_html,
    }


def retain_ranking_detail_pages(
    details: Sequence[Mapping[str, Any]], route: RankingDetailRoute
) -> list[dict[str, str]]:
    """Retain all detail pages under the shared detail bound.

    A malformed item fails closed as a ranking evidence defect rather than
    being filtered, so a silently dropped detail page can never shrink the
    retained evidence.  The raised error stays inside the capture-domain
    error family, so callers report it with a bounded, path-free diagnostic.
    """
    retained: list[dict[str, str]] = []
    for index, detail in enumerate(details):
        if not isinstance(detail, Mapping):
            raise RankingCaptureError(f"ranking detail page {index} is not a mapping")
        retained.append(retain_ranking_detail_page(detail, route))
    return retained
