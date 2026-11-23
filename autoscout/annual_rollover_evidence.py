"""Official-page evidence for the annual edition audit (Issue #680).

The audit's calendar can only say that a newer edition *may* have been
published. Whether it was is read from the pages the source itself declares:
the configured ``url``, the ``next_edition_url`` rendered for the next year,
and the ``landing_url``. This module holds those page observations as plain
data, plans which pages a bounded live check may request, performs that check
with the collector's own bounded fetch, and reads and writes the observations
as JSON captures so that a check can be replayed deterministically.

Nothing here classifies a source; ``annual_source_audit`` does, from the
observations alone. Nothing here writes anywhere unless the caller passes an
explicit capture directory, which must not be a weekly, config or state
directory.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from datetime import date
from datetime import datetime
from pathlib import Path
from typing import Any

from autoscout.annual_edition import URL_STABLE
from autoscout.annual_edition import AnnualEdition
from autoscout.annual_edition import parse_annual_edition
from autoscout.annual_edition import url_stability_kind
from autoscout.source_page_identity import normalize_visible_text
from autoscout.source_page_identity import parse_heading_texts
from autoscout.source_page_identity import visible_page_text

# Which declared page an observation is of.
ROLE_CONFIGURED = "configured"
ROLE_NEXT_EDITION = "next-edition"
ROLE_LANDING = "landing"
ROLES: tuple[str, ...] = (ROLE_CONFIGURED, ROLE_NEXT_EDITION, ROLE_LANDING)

# The most requests a live check may make for one source: the configured page,
# plus one page that can show the next edition.
MAX_REQUESTS_PER_SOURCE = 2

CAPTURE_VERSION = 1


@dataclass(frozen=True)
class PageObservation:
    """One declared page as it was fetched, or why it could not be."""

    role: str
    requested_url: str
    status: int | None
    resolved_url: str
    fetched_at: str
    body: str = ""
    error: str = ""

    @property
    def answered(self) -> bool:
        """The page answered HTTP 200 with a body and no fetch error."""
        return self.status == 200 and not self.error

    @property
    def stayed_on_requested_url(self) -> bool:
        """The response resolved to the requested page, not a redirect elsewhere."""
        from autoscout.source_collectors.agent_fetch import _same_requested_url

        return bool(self.resolved_url) and _same_requested_url(self.requested_url, self.resolved_url)

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest() if self.body else ""

    def shows_text(self, marker: str) -> bool:
        """Whether ``marker`` is part of the page's visible text (the title counts)."""
        return bool(marker) and normalize_visible_text(marker) in visible_page_text(self.body)

    def shows_heading(self, marker: str) -> bool:
        """Whether ``marker`` is exactly one of the page's visible headings.

        Stage markers are matched per heading, as ``forbidden_visible_markers``
        are: prose that names a future stage ("Shortlist announcement
        1 October") must not read as that stage being published.
        """
        wanted = normalize_visible_text(marker)
        return bool(wanted) and any(
            wanted == normalize_visible_text(heading) for heading in parse_heading_texts(self.body)
        )


@dataclass(frozen=True)
class AnnualEditionEvidence:
    """The observations of one source's declared pages."""

    observations: tuple[PageObservation, ...] = ()

    def get(self, role: str) -> PageObservation | None:
        return next((observation for observation in self.observations if observation.role == role), None)


@dataclass(frozen=True)
class EvidenceRequest:
    """One page a live check is allowed to request."""

    role: str
    url: str


def next_edition_role(source: Mapping[str, Any], declared: AnnualEdition) -> str:
    """The role of the page that can show the next edition.

    A stable URL shows it on the configured page itself; a year-specific URL
    with a declared pattern shows it at the next year's URL; any other
    year-specific source can only announce it on its landing page.
    """
    if url_stability_kind(source) == URL_STABLE:
        return ROLE_CONFIGURED
    return ROLE_NEXT_EDITION if declared.next_edition_url else ROLE_LANDING


def expected_request_url(source: Mapping[str, Any], declared: AnnualEdition, role: str) -> str:
    """The URL the current declaration names for ``role``; the one a live check requests."""
    if role == ROLE_CONFIGURED:
        return str(source.get("url", "")).strip()
    if role == ROLE_NEXT_EDITION:
        return declared.next_edition_url_for(declared.edition_year + 1)
    return declared.landing_url


def declared_observation(
    source: Mapping[str, Any], declared: AnnualEdition, evidence: AnnualEditionEvidence, role: str
) -> tuple[PageObservation | None, str]:
    """Return the observation of ``role`` and, when it is of another URL, why it is not evidence.

    A capture names the source, not its declaration: after ``url`` or
    ``next_edition_url`` changes, an older capture is of a page the source no
    longer declares, and it says nothing about this source.
    """
    observation = evidence.get(role)
    expected = expected_request_url(source, declared, role)
    if observation is None or observation.requested_url.strip() == expected:
        return observation, ""
    return observation, (
        f"the {role} page was captured at {observation.requested_url}, but the declaration names {expected}; "
        "that capture is not evidence for this source"
    )


def plan_evidence_requests(source: Mapping[str, Any], *, today: date) -> tuple[EvidenceRequest, ...]:
    """Return the pages a bounded live check may request for one source.

    Only URLs the source declares are planned. The configured page is always
    requested; the page that can show the next edition is requested only when
    that edition's release window has opened, so a window that has not opened
    suppresses the request entirely. A source whose metadata cannot be parsed
    is not requested at all.
    """
    try:
        declared = parse_annual_edition(source)
        role = next_edition_role(source, declared) if declared is not None else ""
    except ValueError:
        return ()
    if declared is None:
        return ()
    url = expected_request_url(source, declared, ROLE_CONFIGURED)
    requests = [EvidenceRequest(ROLE_CONFIGURED, url)] if url else []
    if declared.newest_released_edition(today) > declared.edition_year and role != ROLE_CONFIGURED:
        target = expected_request_url(source, declared, role)
        if target and target != url:
            requests.append(EvidenceRequest(role, target))
    return tuple(requests[:MAX_REQUESTS_PER_SOURCE])


PageFetcher = Callable[[EvidenceRequest], PageObservation]


def gather_evidence(source: Mapping[str, Any], *, today: date, fetch: PageFetcher) -> tuple[AnnualEditionEvidence, int]:
    """Fetch the planned pages in order, once each; return the evidence and the request count.

    The fetcher records a failure as an observation and never raises; an
    exception that escapes it anyway is recorded the same way.
    """
    observations = []
    planned = plan_evidence_requests(source, today=today)
    for request in planned:
        try:
            observations.append(fetch(request))
        except Exception as exc:  # A failed fetch is evidence, never an abort.
            observations.append(_failed(request, f"{type(exc).__name__}: {exc}"))
    return AnnualEditionEvidence(tuple(observations)), len(planned)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _failed(request: EvidenceRequest, error: str, status: int | None = None) -> PageObservation:
    return PageObservation(request.role, request.url, status, "", _now(), "", error)


def live_page_fetcher(source: Mapping[str, Any]) -> PageFetcher:
    """Return a fetcher that uses the collector's own bounded GET.

    Same User-Agent, size limit and timeout as live collection; one plain GET
    per request with no retry. HTTP redirects are followed by the transport
    and recorded through the resolved URL; links in the body are never
    followed.
    """
    from autoscout.ranking_request import DEFAULT_RANKING_REQUEST
    from autoscout.source_collectors.agent_fetch import _fetch_bounded_page
    from autoscout.source_collectors.base import CollectorRunError

    def fetch(request: EvidenceRequest) -> PageObservation:
        try:
            html, _decoded, resolved = _fetch_bounded_page(
                dict(source),
                request.url,
                ranking_request=DEFAULT_RANKING_REQUEST,
                unwrap_response=False,
                strict_status=True,
            )
        except CollectorRunError as exc:
            status_text = str(exc.debug_metadata.get("http_status", ""))
            return _failed(request, str(exc), int(status_text) if status_text.isdigit() else None)
        except Exception as exc:
            return _failed(request, f"{type(exc).__name__}: {exc}")
        return PageObservation(request.role, request.url, 200, str(resolved or request.url), _now(), html)

    return fetch


# ── Captures ────────────────────────────────────────────────────────────────


class EvidenceCaptureError(ValueError):
    """An evidence capture cannot be read or written."""


def capture_file_name(source_name: str) -> str:
    """A stable, filesystem-safe capture name for one source."""
    slug = re.sub(r"[^a-z0-9]+", "-", source_name.casefold()).strip("-")[:60] or "source"
    digest = hashlib.sha256(source_name.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}.json"


def evidence_to_json(source_name: str, evidence: AnnualEditionEvidence) -> dict[str, Any]:
    return {
        "version": CAPTURE_VERSION,
        "source_name": source_name,
        "observations": [
            {
                "role": observation.role,
                "requested_url": observation.requested_url,
                "status": observation.status,
                "resolved_url": observation.resolved_url,
                "fetched_at": observation.fetched_at,
                "body": observation.body,
                "error": observation.error,
            }
            for observation in evidence.observations
        ],
    }


def _observation_from_json(item: Any, label: str) -> PageObservation:
    if not isinstance(item, Mapping):
        raise EvidenceCaptureError(f"{label}: an observation must be an object")
    role = item.get("role")
    if role not in ROLES:
        raise EvidenceCaptureError(f"{label}: role must be one of {', '.join(ROLES)}")
    status = item.get("status")
    if status is not None and (not isinstance(status, int) or isinstance(status, bool)):
        raise EvidenceCaptureError(f"{label}: status must be an integer or null")
    text_fields = {}
    for field in ("requested_url", "resolved_url", "fetched_at", "body", "error"):
        value = item.get(field, "")
        if not isinstance(value, str):
            raise EvidenceCaptureError(f"{label}: {field} must be text")
        text_fields[field] = value
    if not text_fields["requested_url"]:
        raise EvidenceCaptureError(f"{label}: requested_url is required")
    return PageObservation(role=str(role), status=status, **text_fields)


def evidence_from_json(document: Any, label: str) -> tuple[str, AnnualEditionEvidence]:
    """Parse one capture document into (source name, evidence)."""
    if not isinstance(document, Mapping) or document.get("version") != CAPTURE_VERSION:
        raise EvidenceCaptureError(f"{label}: not an annual evidence capture of version {CAPTURE_VERSION}")
    source_name = document.get("source_name")
    observations = document.get("observations")
    if not isinstance(source_name, str) or not source_name.strip() or not isinstance(observations, list):
        raise EvidenceCaptureError(f"{label}: source_name and observations are required")
    parsed = tuple(_observation_from_json(item, label) for item in observations)
    if len({observation.role for observation in parsed}) != len(parsed):
        raise EvidenceCaptureError(f"{label}: a role is observed more than once")
    return source_name.strip(), AnnualEditionEvidence(parsed)


def read_evidence_dir(directory: Path) -> dict[str, AnnualEditionEvidence]:
    """Read every capture in ``directory``, keyed by source name."""
    if not directory.is_dir():
        raise EvidenceCaptureError(f"evidence directory not found: {directory}")
    evidence: dict[str, AnnualEditionEvidence] = {}
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError as exc:
        raise EvidenceCaptureError(f"evidence directory cannot be listed: {directory} ({exc})") from exc
    for path in paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise EvidenceCaptureError(f"{path.name}: unreadable capture ({exc})") from exc
        source_name, parsed = evidence_from_json(document, path.name)
        if source_name in evidence:
            raise EvidenceCaptureError(f"{path.name}: a second capture for {source_name!r}")
        evidence[source_name] = parsed
    return evidence


def validated_capture_dir(directory: Path) -> Path:
    """Refuse a capture directory inside configs/, runs/ or any weekly/state directory."""
    from autoscout.safe_artifacts import SafeArtifactError
    from autoscout.safe_artifacts import resolve_storage_destination

    try:
        return resolve_storage_destination(directory, "directory", "evidence directory").path
    except SafeArtifactError as exc:
        raise EvidenceCaptureError(str(exc)) from exc
    except OSError as exc:
        raise EvidenceCaptureError(f"evidence directory cannot be checked: {directory} ({exc})") from exc


def write_evidence_capture(directory: Path, source_name: str, evidence: AnnualEditionEvidence) -> Path:
    """Write one source's capture into a directory already validated by ``validated_capture_dir``.

    The capture file itself is validated too, so an existing symlink or hard
    link at its name is refused rather than written through, and it is
    published by an atomic staged replace, never written in place. Every
    failure, a refused link or a filesystem error, is an ``EvidenceCaptureError``.
    """
    from autoscout.safe_artifacts import SafeArtifactError
    from autoscout.safe_artifacts import publish_single
    from autoscout.safe_artifacts import resolve_storage_destination

    path = directory / capture_file_name(source_name)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        resolve_storage_destination(path, "file", "evidence capture")
        publish_single(path, json.dumps(evidence_to_json(source_name, evidence), ensure_ascii=False, indent=2) + "\n")
    except SafeArtifactError as exc:
        raise EvidenceCaptureError(f"{path.name}: {exc}") from exc
    except OSError as exc:
        raise EvidenceCaptureError(f"{path.name} could not be written: {exc}") from exc
    return path
