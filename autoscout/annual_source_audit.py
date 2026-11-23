"""Read-only edition audit for annual Configured Sources (Issue #680).

An annual source carries its edition in its identity: National Book Awards
**2026**, PW Best Books **2026**, Cundill History Prize **2026**. When the
award publishes its next edition, that identity has to be refreshed by hand,
and nothing reminded anyone to do it. In run ``4090c59a`` (2026-09-13) three
active annual sources were still pinned to 2025, and two of them reported
``collected``: a year-old award list served as a current weekly signal,
indistinguishable in the run record from a fresh one.

The calendar year alone cannot say when that happens. A 2025 award list in
September 2026 is usually correct, because most nonfiction awards announce
late in the year; the same list in January 2027 usually is not. Each annual
source therefore declares when its editions are published (``annual_edition``,
see ``autoscout.annual_edition``), and this audit compares that release window
with a date. Without evidence (the weekly run's path) it can say only:

- ``current``: no edition newer than the configured one can have been
  published yet, because the next release window has not opened.
- ``unable-to-determine``: the metadata is missing or malformed, the
  configured edition is ahead of any window that has opened, or a newer
  edition's window has opened and only the declared official pages can say
  whether it was published.

Given evidence (``autoscout.annual_rollover_evidence``: observations of the
configured page, the next edition's year URL, or the landing page), it
reaches all six verdicts of #680, in a fixed order:

1. metadata problems, as above;
2. once the next edition's window has opened: ``rollover-available`` or
   ``stable-url-new-edition`` only when a page that answered 200, stayed on
   the requested URL and shows the declared ``edition_marker`` for the newer
   year; ``not-yet-released`` when such a page shows no newer edition;
   ``unable-to-determine`` when no marker is declared or the page failed;
3. before that window: ``unable-to-determine`` when a stable page already
   shows the next edition's marker (never a rollover before its window), or
   when a declared edition marker is missing from the configured page;
   ``stage-review-required`` when the (confirmed) configured page shows a
   declared stage later than the tracked one; else ``current``.

An observation counts only when it is of the URL the current declaration
names for its role; a capture of another URL is ``evidence-unavailable``.

HTTP 200 alone never yields ``current``, ``rollover-available`` or
``stable-url-new-edition``.

Read-only by construction: it takes parsed sources, a date and optional
observations, and returns verdicts. It never fetches a page, never writes
configuration, and never applies anything. Applying a rollover stays an
editor-confirmed source change under ``docs/source-onboarding-runbook.md``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from typing import Any

from autoscout.annual_edition import ANNUAL_EDITION_FIELD
from autoscout.annual_edition import URL_STABLE
from autoscout.annual_edition import AnnualEdition
from autoscout.annual_edition import is_annual_source
from autoscout.annual_edition import parse_annual_edition
from autoscout.annual_edition import url_stability_kind
from autoscout.annual_rollover_evidence import ROLE_CONFIGURED
from autoscout.annual_rollover_evidence import ROLE_LANDING
from autoscout.annual_rollover_evidence import ROLE_NEXT_EDITION
from autoscout.annual_rollover_evidence import AnnualEditionEvidence
from autoscout.annual_rollover_evidence import PageObservation
from autoscout.annual_rollover_evidence import declared_observation
from autoscout.annual_rollover_evidence import next_edition_role
from autoscout.source_rules import is_active_source

__all__ = ["AnnualEditionEvidence", "is_annual_source"]

# The six verdicts of #680's acceptance criteria.
CURRENT = "current"
NOT_YET_RELEASED = "not-yet-released"
ROLLOVER_AVAILABLE = "rollover-available"
STABLE_URL_NEW_EDITION = "stable-url-new-edition"
STAGE_REVIEW_REQUIRED = "stage-review-required"
UNABLE_TO_DETERMINE = "unable-to-determine"
VERDICTS: tuple[str, ...] = (
    CURRENT,
    NOT_YET_RELEASED,
    ROLLOVER_AVAILABLE,
    STABLE_URL_NEW_EDITION,
    STAGE_REVIEW_REQUIRED,
    UNABLE_TO_DETERMINE,
)

# Why a verdict was reached, machine-readable.
REASON_NO_NEWER_EDITION_EXPECTED = "no-newer-edition-expected"
REASON_METADATA_MISSING = "annual-metadata-missing"
REASON_METADATA_INVALID = "annual-metadata-invalid"
REASON_EDITION_YEAR_AHEAD = "edition-year-ahead"
REASON_NEWER_EDITION_EXPECTED = "newer-edition-expected-evidence-required"
# Reasons only an evidence check reaches.
REASON_CONFIGURED_EDITION_CONFIRMED = "configured-edition-confirmed"
REASON_CONFIGURED_EDITION_NOT_CONFIRMED = "configured-edition-not-confirmed"
REASON_NEWER_EDITION_PUBLISHED = "newer-edition-published"
REASON_NEWER_EDITION_NOT_PUBLISHED = "newer-edition-not-published"
REASON_NEWER_STAGE_PUBLISHED = "newer-stage-published"
REASON_EDITION_MARKER_UNDECLARED = "edition-marker-undeclared"
REASON_EVIDENCE_UNAVAILABLE = "evidence-unavailable"
REASON_MARKER_BEFORE_WINDOW = "newer-edition-marker-before-window"

# Every verdict but these asks the operator to look at the source.
_SETTLED_VERDICTS = frozenset({CURRENT, NOT_YET_RELEASED})

# Top-level fields an annual source declares beside ``annual_edition``.
_ANNUAL_METADATA_FIELDS = ("url_stability", "refresh_cadence")

# The weekly run records this advisory under this stage name; run-health reads
# it back by the same name. Defined here so the producer and the reader cannot
# drift apart.
ANNUAL_AUDIT_STAGE_NAME = "annual-source-audit"

# A four-digit year in the plausible range for an edition identity. The digit
# lookarounds reject a longer run like 20250, but they cannot reject a group
# that separators isolate *inside* an identifier: the "2026" in the ISBN
# 978-0-2026-0000-0 is bounded by hyphens, not digits. Identifier spans are
# therefore masked before this pattern ever runs.
_YEAR_PATTERN = re.compile(r"(?<!\d)(20[0-4]\d)(?!\d)")

# A run of digits and separators carrying enough digits to be a book
# identifier rather than a date or an edition: ISBN-10 is 10, ISBN-13 is 13.
# Four-digit years, two-digit ranks, and a full ISO date (8 digits) stay below
# the bound, so a source dated /2026-09-13/ keeps its year.
_IDENTIFIER_RUN = re.compile(r"\d[\d\-‐-―]*\d")
_IDENTIFIER_MIN_DIGITS = 9


def _mask_identifier_spans(text: str) -> str:
    """Blank out book-identifier runs so their digits cannot read as a year.

    An ISBN is the case that matters: ``978-0-2026-0000-0`` contains a
    hyphen-isolated ``2026`` that every digit-boundary rule accepts. Reading
    it would report a current edition for a source pinned years earlier, or
    mask an older year declared in the name — the audit asserting an edition
    that nothing published.
    """

    def blank(match: re.Match[str]) -> str:
        run = match.group(0)
        digits = sum(character.isdigit() for character in run)
        return " " * len(run) if digits >= _IDENTIFIER_MIN_DIGITS else run

    return _IDENTIFIER_RUN.sub(blank, text)


def _edition_years(text: str) -> list[int]:
    """Return the plausible edition years in one identity string."""
    return [int(match) for match in _YEAR_PATTERN.findall(_mask_identifier_spans(str(text)))]


@dataclass(frozen=True)
class MarkerCheck:
    """One declared marker looked for on one observed page."""

    role: str
    kind: str  # "edition" (visible text, title included) or "stage" (an exact heading)
    marker: str
    found: bool


@dataclass(frozen=True)
class AnnualSourceVerdict:
    """One audited annual source and what its declaration says on one date."""

    source_name: str
    verdict: str
    reason: str
    edition_year: int | None
    year_source: str
    tracked_stage: str
    release_window: str
    url: str
    landing_url: str
    today: date
    detail: str
    # Set only by an evidence check.
    observed_edition_year: int | None = None
    observed_stage: str = ""
    proposed_url: str = ""
    marker_checks: tuple[MarkerCheck, ...] = ()
    evidence: AnnualEditionEvidence | None = None

    @property
    def needs_attention(self) -> bool:
        """Whether an operator has something to check for this source."""
        return self.verdict not in _SETTLED_VERDICTS


def declared_edition_year(source: Mapping[str, Any]) -> tuple[int | None, str]:
    """Return the edition year a source declares and where it was found.

    ``annual_edition.edition_year`` comes first: it is the declaration the
    audit judges. An award-list source's ``award_year`` (Issue #577) comes
    next; configuration refuses the two when they disagree.

    Without either, the year is read from the name before the URL, because
    the name is the durable identity an operator reads in the run record, and
    #679 showed the two can disagree: a source named for 2024 served the 2026
    edition from a stable landing page. When both carry a year the newest
    wins, so a stale display name beside a refreshed URL is not reported as
    behind twice over.
    """
    declared = source.get(ANNUAL_EDITION_FIELD)
    if isinstance(declared, Mapping):
        edition_year = declared.get("edition_year")
        if isinstance(edition_year, int) and not isinstance(edition_year, bool):
            return edition_year, ANNUAL_EDITION_FIELD
    award_year = source.get("award_year")
    if isinstance(award_year, int) and not isinstance(award_year, bool):
        return award_year, "award_year"
    name_years = _edition_years(source.get("name", ""))
    url_years = _edition_years(source.get("url", ""))

    if not name_years and not url_years:
        return None, ""
    if name_years and url_years:
        newest = max(max(name_years), max(url_years))
        both = (
            "name+url" if max(name_years) == max(url_years) else ("name" if max(name_years) > max(url_years) else "url")
        )
        return newest, both
    if name_years:
        return max(name_years), "name"
    return max(url_years), "url"


def _metadata_problem(source: Mapping[str, Any]) -> tuple[str, str, AnnualEdition | None]:
    """Return (reason, detail, declaration); the reason is empty when usable."""
    missing = [field for field in _ANNUAL_METADATA_FIELDS if not str(source.get(field, "")).strip()]
    try:
        declared = parse_annual_edition(source)
    except ValueError as exc:
        return REASON_METADATA_INVALID, f"annual metadata is malformed: {exc}", None
    if declared is None:
        missing.append(ANNUAL_EDITION_FIELD)
    if missing:
        return (
            REASON_METADATA_MISSING,
            f"annual metadata missing: {', '.join(missing)}; the audit cannot judge the configured edition",
            declared,
        )
    return "", "", declared


def _calendar_verdict(declared: AnnualEdition, today: date) -> tuple[str, str, str]:
    """Return (verdict, reason, detail) from the release window alone."""
    configured = declared.edition_year
    expected = declared.newest_released_edition(today)
    window = declared.release_window
    if configured > expected:
        return (
            UNABLE_TO_DETERMINE,
            REASON_EDITION_YEAR_AHEAD,
            f"edition {configured} is configured but its release window opens on "
            f"{window.opens_on(configured).isoformat()}; confirm the edition year is not mistyped",
        )
    if configured == expected:
        return (
            CURRENT,
            REASON_NO_NEWER_EDITION_EXPECTED,
            f"edition {configured} is the newest whose release window has opened; "
            f"edition {configured + 1} opens on {window.opens_on(configured + 1).isoformat()}",
        )
    behind = expected - configured
    return (
        UNABLE_TO_DETERMINE,
        REASON_NEWER_EDITION_EXPECTED,
        f"edition {configured} is configured, but the release window of edition {expected} opened on "
        f"{window.opens_on(expected).isoformat()} ({behind} edition{'s' if behind > 1 else ''} newer); "
        f"check {declared.landing_url} for a published edition before any rollover",
    )


@dataclass(frozen=True)
class _Finding:
    verdict: str
    reason: str
    detail: str
    observed_edition_year: int | None = None
    observed_stage: str = ""
    proposed_url: str = ""


@dataclass
class _MarkerLog:
    """Looks markers up on observed pages and records every lookup."""

    checks: list[MarkerCheck] = field(default_factory=list)

    def text(self, observation: PageObservation, marker: str) -> bool:
        found = observation.shows_text(marker)
        self.checks.append(MarkerCheck(observation.role, "edition", marker, found))
        return found

    def heading(self, observation: PageObservation, marker: str) -> bool:
        found = observation.shows_heading(marker)
        self.checks.append(MarkerCheck(observation.role, "stage", marker, found))
        return found

    def newest_stage(self, declared: AnnualEdition, observation: PageObservation) -> str:
        """The latest declared stage whose marker is a heading on the page, in ``stages`` order."""
        shown = ""
        for stage, marker in declared.stage_markers:
            if self.heading(observation, marker):
                shown = stage
        return shown


def _unavailable(role: str, observation: PageObservation | None) -> _Finding:
    if observation is None:
        problem = f"no {role} page was observed"
    else:
        failure = observation.error or f"HTTP {observation.status}"
        problem = f"the {role} page {observation.requested_url} did not answer HTTP 200 ({failure})"
    return _Finding(
        UNABLE_TO_DETERMINE, REASON_EVIDENCE_UNAVAILABLE, f"{problem}; the edition cannot be judged without it"
    )


def _not_yet_released(declared: AnnualEdition, today: date, observation: PageObservation, why: str) -> _Finding:
    next_year = declared.edition_year + 1
    window = declared.release_window
    detail = f"the release window of edition {next_year} has been open since {window.opens_on(next_year).isoformat()}"
    if today > window.closes_on(next_year):
        detail += f" and closed on {window.closes_on(next_year).isoformat()}, so the edition is overdue"
    return _Finding(
        NOT_YET_RELEASED,
        REASON_NEWER_EDITION_NOT_PUBLISHED,
        f"{detail}; {observation.role} page {observation.requested_url}: {why}",
    )


def _newer_edition_finding(
    source: Mapping[str, Any], declared: AnnualEdition, today: date, evidence: AnnualEditionEvidence, log: _MarkerLog
) -> _Finding:
    """Judge a source once the window of the edition after it has opened.

    Only a page that answered 200, stayed on the requested URL where that
    matters, and shows the declared edition marker for the newer year proves
    a published edition. A 200 alone proves nothing.
    """
    configured = declared.edition_year
    if not declared.edition_marker:
        return _Finding(
            UNABLE_TO_DETERMINE,
            REASON_EDITION_MARKER_UNDECLARED,
            f"the release window of edition {configured + 1} opened on "
            f"{declared.release_window.opens_on(configured + 1).isoformat()}; with no edition_marker declared, "
            "no page can prove that edition was published",
        )
    role = next_edition_role(source, declared)
    observation, mismatch = declared_observation(source, declared, evidence, role)
    if mismatch:
        return _Finding(UNABLE_TO_DETERMINE, REASON_EVIDENCE_UNAVAILABLE, mismatch)
    if observation is None or not observation.answered:
        return _unavailable(role, observation)
    years: Sequence[int] = range(declared.newest_released_edition(today), configured, -1)
    if role == ROLE_NEXT_EDITION:
        years = (configured + 1,)
        if not observation.stayed_on_requested_url:
            why = f"redirected to {observation.resolved_url}, and a redirect is not a published edition"
            return _not_yet_released(declared, today, observation, why)
    elif role == ROLE_CONFIGURED and not observation.stayed_on_requested_url:
        return _Finding(
            UNABLE_TO_DETERMINE,
            REASON_EVIDENCE_UNAVAILABLE,
            f"the configured page {observation.requested_url} resolved to {observation.resolved_url}",
        )
    found = next((year for year in years if log.text(observation, declared.edition_marker_for(year))), None)
    if found is None:
        shown = ", ".join(repr(declared.edition_marker_for(year)) for year in years)
        return _not_yet_released(declared, today, observation, f"does not show {shown}")
    marker = declared.edition_marker_for(found)
    stage = log.newest_stage(declared, observation) if role != ROLE_LANDING else ""
    if role == ROLE_CONFIGURED:
        verdict, proposed_url = STABLE_URL_NEW_EDITION, str(source.get("url", "")).strip()
        where = "the configured stable page"
    else:
        verdict, proposed_url = ROLLOVER_AVAILABLE, observation.requested_url if role == ROLE_NEXT_EDITION else ""
        where = f"the {role} page {observation.requested_url}"
    return _Finding(
        verdict,
        REASON_NEWER_EDITION_PUBLISHED,
        f"edition {configured} is configured; {where} shows {marker!r}",
        observed_edition_year=found,
        observed_stage=stage,
        proposed_url=proposed_url,
    )


def _configured_edition_finding(
    source: Mapping[str, Any], declared: AnnualEdition, evidence: AnnualEditionEvidence, log: _MarkerLog
) -> _Finding:
    """Judge a source whose configured edition is the newest one expected."""
    configured = declared.edition_year
    observation, mismatch = declared_observation(source, declared, evidence, ROLE_CONFIGURED)
    if mismatch:
        return _Finding(UNABLE_TO_DETERMINE, REASON_EVIDENCE_UNAVAILABLE, mismatch)
    if observation is None or not observation.answered:
        return _unavailable(ROLE_CONFIGURED, observation)
    if not observation.stayed_on_requested_url:
        return _Finding(
            UNABLE_TO_DETERMINE,
            REASON_CONFIGURED_EDITION_NOT_CONFIRMED,
            f"the configured page {observation.requested_url} resolved to {observation.resolved_url}",
        )
    next_marker = declared.edition_marker_for(configured + 1)
    if url_stability_kind(source) == URL_STABLE and declared.edition_marker and log.text(observation, next_marker):
        return _Finding(
            UNABLE_TO_DETERMINE,
            REASON_MARKER_BEFORE_WINDOW,
            f"the configured stable page already shows {next_marker!r}, but the release window of edition "
            f"{configured + 1} opens on {declared.release_window.opens_on(configured + 1).isoformat()}; "
            "check the window and the marker before any change",
            observed_edition_year=configured + 1,
        )
    # A later stage counts only on a page confirmed to be the configured
    # edition: last year's page headed "The shortlist" is no reason to move
    # this year's longlist source to a shortlist.
    marker = declared.edition_marker_for(configured)
    if declared.edition_marker and not log.text(observation, marker):
        return _Finding(
            UNABLE_TO_DETERMINE,
            REASON_CONFIGURED_EDITION_NOT_CONFIRMED,
            f"the configured page does not show {marker!r}",
        )
    stage = log.newest_stage(declared, observation)
    if stage and declared.stages.index(stage) > declared.stages.index(declared.tracked_stage):
        return _Finding(
            STAGE_REVIEW_REQUIRED,
            REASON_NEWER_STAGE_PUBLISHED,
            f"the configured page shows the {stage} stage of edition {configured}, later than the tracked "
            f"{declared.tracked_stage}; whether to move to it is the operator's decision",
            observed_stage=stage,
        )
    if not declared.edition_marker:
        return _Finding(
            CURRENT,
            REASON_NO_NEWER_EDITION_EXPECTED,
            f"edition {configured} is the newest whose release window has opened; no edition_marker is "
            "declared, so the page cannot confirm the edition",
        )
    return _Finding(
        CURRENT,
        REASON_CONFIGURED_EDITION_CONFIRMED,
        f"edition {configured} is the newest whose release window has opened, and the configured page shows {marker!r}",
    )


def _evidence_finding(
    source: Mapping[str, Any], declared: AnnualEdition, today: date, evidence: AnnualEditionEvidence
) -> tuple[_Finding, tuple[MarkerCheck, ...]]:
    log = _MarkerLog()
    if declared.newest_released_edition(today) > declared.edition_year:
        finding = _newer_edition_finding(source, declared, today, evidence, log)
    else:
        finding = _configured_edition_finding(source, declared, evidence, log)
    return finding, tuple(log.checks)


def audit_annual_source(
    source: Mapping[str, Any], *, today: date, evidence: AnnualEditionEvidence | None = None
) -> AnnualSourceVerdict:
    """Classify one annual source on ``today``. Pure: no page, no clock, no write.

    Without ``evidence`` only the release window speaks, so the verdict is
    ``current`` or ``unable-to-determine`` (the weekly run's path). With
    evidence, the observed official pages decide between all six verdicts.
    """
    year, year_source = declared_edition_year(source)
    reason, detail, declared = _metadata_problem(source)
    finding = _Finding(UNABLE_TO_DETERMINE, reason, detail)
    checks: tuple[MarkerCheck, ...] = ()
    if declared is not None and not reason:
        finding = _Finding(*_calendar_verdict(declared, today))
        if evidence is not None and finding.reason != REASON_EDITION_YEAR_AHEAD:
            finding, checks = _evidence_finding(source, declared, today, evidence)
    return AnnualSourceVerdict(
        source_name=str(source.get("name", "")).strip(),
        verdict=finding.verdict,
        reason=finding.reason,
        edition_year=year,
        year_source=year_source,
        tracked_stage=declared.tracked_stage if declared else "",
        release_window=declared.release_window.describe() if declared else "",
        url=str(source.get("url", "")).strip(),
        landing_url=declared.landing_url if declared else "",
        today=today,
        detail=finding.detail,
        observed_edition_year=finding.observed_edition_year,
        observed_stage=finding.observed_stage,
        proposed_url=finding.proposed_url,
        marker_checks=checks,
        evidence=evidence,
    )


def audited_sources(sources: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The active annual sources, in configuration order.

    Inactive sources are skipped: a source the operator turned off is not
    feeding the weekly run, so a stale year on it is not a finding to act on.
    """
    return [source for source in sources if is_active_source(dict(source)) and is_annual_source(source)]


def audit_annual_sources(
    sources: Sequence[Mapping[str, Any]],
    *,
    today: date,
    evidence_by_source: Mapping[str, AnnualEditionEvidence] | None = None,
) -> list[AnnualSourceVerdict]:
    """Audit every active annual source, in configuration order.

    With ``evidence_by_source`` every source is judged on evidence; a source
    with no captured pages is judged on empty evidence, never silently on the
    calendar alone.
    """
    return [
        audit_annual_source(
            source,
            today=today,
            evidence=(
                None
                if evidence_by_source is None
                else evidence_by_source.get(str(source.get("name", "")).strip(), AnnualEditionEvidence())
            ),
        )
        for source in audited_sources(sources)
    ]


def rollover_checks_required(verdicts: Sequence[AnnualSourceVerdict]) -> list[AnnualSourceVerdict]:
    """Return only the verdicts that ask the operator to do something."""
    return [verdict for verdict in verdicts if verdict.needs_attention]


def audit_annual_sources_command(config_path: Any, *, today: date | None = None, **options: Any) -> int:
    """Print the annual edition audit for one config. Read-only.

    The command, its evidence modes and its proposals live in
    ``autoscout.annual_rollover_command``; see ``run_audit_command`` there.
    """
    from autoscout.annual_rollover_command import run_audit_command

    return run_audit_command(config_path, today=today, **options)


def annual_audit_stage_result(config_path: Any, *, today: date | None = None) -> dict[str, Any]:
    """Audit one config and return a JSON-safe result for a run artifact.

    Written for the weekly run, which must never fail because a maintenance
    advisory could not be computed. Every failure path returns a status of
    ``unknown`` carrying the reason; nothing raises, and nothing here can
    stop a run that is otherwise healthy.

    Only the sources that need a check are listed. A run artifact that
    repeated every verdict every week would bury the few that matter.
    """
    from pathlib import Path

    from autoscout.config import read_json
    from autoscout.config import resolve_weekly_portfolio_or_raise

    audit_date = today if today is not None else date.today()
    stamp = {"current_year": audit_date.year, "audit_date": audit_date.isoformat()}

    try:
        raw = read_json(Path(config_path), default=None)
        if not isinstance(raw, dict):
            return {"status": "unknown", "reason": "config is not a JSON object", **stamp}
        config = resolve_weekly_portfolio_or_raise(raw, Path(config_path))
        sources = config.get("sources")
        if not isinstance(sources, list):
            return {"status": "unknown", "reason": "config 'sources' is not a list", **stamp}
        verdicts = audit_annual_sources(sources, today=audit_date)
    except Exception as exc:  # An advisory must never break a run: catch everything.
        return {"status": "unknown", "reason": f"{type(exc).__name__}: {exc}", **stamp}

    findings = rollover_checks_required(verdicts)
    return {
        "status": "attention_required" if findings else "ok",
        **stamp,
        "annual_source_count": len(verdicts),
        "rollover_checks_required": len(findings),
        "sources": [
            {
                "source_name": finding.source_name,
                "verdict": finding.verdict,
                "reason": finding.reason,
                "edition_year": finding.edition_year,
                "url": finding.url,
            }
            for finding in findings
        ],
    }
