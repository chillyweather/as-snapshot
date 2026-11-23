"""Structured edition metadata for an annual Configured Source (Issue #680).

An annual source is pinned to one edition of a prize or a list, and the
calendar alone cannot say whether that edition is still the newest one: a
2025 award list in September 2026 is usually correct, and the same list in
January 2027 usually is not. What tells them apart is when the award
publishes. Each annual source therefore declares, in one ``annual_edition``
object, which edition it is configured for, where a new edition is
announced, when an edition is first published, and which stage of the
award cycle the source tracks.

This module parses and validates that object. It reads nothing but the
source mapping: no page, no network, no clock. The audit in
``annual_source_audit`` turns the parsed declaration and a date into a
verdict.

The object is optional for configuration loading: a source without it still
loads and still collects, and the audit reports it as missing metadata. An
object that is present but malformed is a configuration error, because a
half-declared window would let the audit pass a source it cannot judge.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlparse

ANNUAL_EDITION_FIELD = "annual_edition"
ANNUAL_CADENCE = "annual"

# The stages an annual award or list publishes, named once for every source.
# ``list`` is a single-stage list such as PW Best Books; ``semifinalists`` is
# Prêmio Jabuti's ten-book stage before its five finalists.
ANNUAL_STAGES: tuple[str, ...] = (
    "longlist",
    "semifinalists",
    "shortlist",
    "finalists",
    "nominees",
    "winner",
    "list",
)

YEAR_PLACEHOLDER = "{year}"

# How an annual source's URL behaves across editions, read from the leading
# word of its free-text ``url_stability``. A stable URL carries each new
# edition itself (the edition is read on the configured page); a
# year-specific URL is replaced by another URL for the next edition. The audit
# must never guess which one a source is, so an annual declaration whose
# ``url_stability`` starts with neither word is refused.
URL_STABLE = "stable"
URL_YEAR_SPECIFIC = "year-specific"
_URL_STABILITY_KINDS = (URL_STABLE, URL_YEAR_SPECIFIC)

# How far an edition label may sit from the calendar year of its release.
# The Andrew Carnegie Medals label as 2026 the edition whose longlist was
# published in October 2025 (-1); nothing in the portfolio needs more.
_OFFSET_RANGE = range(-1, 2)

_REQUIRED_KEYS = ("edition_year", "landing_url", "release_window", "stages", "tracked_stage")
_OPTIONAL_KEYS = ("edition_marker", "next_edition_url", "stage_markers")
_WINDOW_KEYS = ("opens", "closes", "calendar_year_offset")
_MONTH_DAY = re.compile(r"(\d{2})-(\d{2})")
# Month-day pairs are checked against a common year, so 02-29 is refused: a
# window that opens on a day most years lack cannot be compared every year.
_COMMON_YEAR = 2001


@dataclass(frozen=True)
class ReleaseWindow:
    """When the edition labelled Y first publishes its first stage.

    ``opens`` and ``closes`` are (month, day) pairs in calendar year
    ``Y + calendar_year_offset``.
    """

    opens: tuple[int, int]
    closes: tuple[int, int]
    calendar_year_offset: int = 0

    def opens_on(self, edition_year: int) -> date:
        """The first day an edition labelled ``edition_year`` may be published."""
        return date(edition_year + self.calendar_year_offset, *self.opens)

    def closes_on(self, edition_year: int) -> date:
        """The last day an edition labelled ``edition_year`` is expected to be first published."""
        return date(edition_year + self.calendar_year_offset, *self.closes)

    def describe(self) -> str:
        """The window as written in configuration, with any year offset."""
        text = f"{self.opens[0]:02d}-{self.opens[1]:02d}..{self.closes[0]:02d}-{self.closes[1]:02d}"
        if self.calendar_year_offset:
            text += f" (edition year {self.calendar_year_offset:+d})"
        return text


@dataclass(frozen=True)
class AnnualEdition:
    """One annual source's validated edition declaration."""

    edition_year: int
    landing_url: str
    release_window: ReleaseWindow
    stages: tuple[str, ...]
    tracked_stage: str
    edition_marker: str = ""
    next_edition_url: str = ""
    stage_markers: tuple[tuple[str, str], ...] = ()

    def newest_released_edition(self, today: date) -> int:
        """The newest edition year whose release window has opened by ``today``."""
        year = today.year - self.release_window.calendar_year_offset
        if self.release_window.opens_on(year) > today:
            year -= 1
        return year

    def edition_marker_for(self, year: int) -> str:
        """The declared edition marker rendered for ``year``; empty when none is declared."""
        return self.edition_marker.replace(YEAR_PLACEHOLDER, str(year))

    def next_edition_url_for(self, year: int) -> str:
        """The declared year-specific URL rendered for ``year``; empty when none is declared."""
        return self.next_edition_url.replace(YEAR_PLACEHOLDER, str(year))


def url_stability_kind(source: Mapping[str, Any]) -> str:
    """Return ``stable`` or ``year-specific`` from the source's ``url_stability``.

    Raises ``ValueError`` when the declared text starts with neither word.
    """
    text = str(source.get("url_stability", "")).strip().casefold()
    for kind in _URL_STABILITY_KINDS:
        if text.startswith(kind):
            return kind
    raise ValueError(f"url_stability must begin with {URL_STABLE!r} or {URL_YEAR_SPECIFIC!r} on an annual source")


def is_annual_source(source: Mapping[str, Any]) -> bool:
    """Say whether a source declares an annual cadence.

    Either field may carry it: ``refresh_cadence`` is a validated alias of
    ``check_frequency`` (``config.py``), and both spellings appear across the
    portfolio, so reading one alone would silently skip real annual sources.
    """
    return any(
        str(source.get(field, "")).strip().lower() == ANNUAL_CADENCE for field in ("check_frequency", "refresh_cadence")
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _month_day(value: Any, label: str) -> tuple[int, int]:
    match = _MONTH_DAY.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError(f"{label} must be MM-DD text")
    month, day = int(match.group(1)), int(match.group(2))
    try:
        date(_COMMON_YEAR, month, day)
    except ValueError:
        raise ValueError(f"{label} {value!r} is not a calendar day in every year") from None
    return month, day


def _https_url(value: Any, label: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{label} must be an https URL")
    return text


def _year_template(value: Any, label: str) -> str:
    """A nonblank text whose only placeholder is ``{year}``."""
    text = value.strip() if isinstance(value, str) else ""
    if YEAR_PLACEHOLDER not in text:
        raise ValueError(f"{label} must be nonblank text containing {YEAR_PLACEHOLDER}")
    if "{" in text.replace(YEAR_PLACEHOLDER, "") or "}" in text.replace(YEAR_PLACEHOLDER, ""):
        raise ValueError(f"{label} may contain no placeholder other than {YEAR_PLACEHOLDER}")
    return text


def _release_window(value: Any) -> ReleaseWindow:
    label = f"{ANNUAL_EDITION_FIELD}.release_window"
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    unknown = sorted(set(value) - set(_WINDOW_KEYS))
    if unknown:
        raise ValueError(f"{label} has unknown key(s): {', '.join(unknown)}")
    opens = _month_day(value.get("opens"), f"{label}.opens")
    closes = _month_day(value.get("closes"), f"{label}.closes")
    if opens > closes:
        raise ValueError(f"{label}.opens must not be after {label}.closes")
    offset = value.get("calendar_year_offset", 0)
    if not _is_int(offset) or offset not in _OFFSET_RANGE:
        raise ValueError(f"{label}.calendar_year_offset must be an integer from -1 to 1")
    return ReleaseWindow(opens=opens, closes=closes, calendar_year_offset=offset)


def _stages(value: Any) -> tuple[str, ...]:
    label = f"{ANNUAL_EDITION_FIELD}.stages"
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty list")
    if any(stage not in ANNUAL_STAGES for stage in value):
        raise ValueError(f"{label} may contain only: {', '.join(ANNUAL_STAGES)}")
    if len(set(value)) != len(value):
        raise ValueError(f"{label} must not repeat a stage")
    return tuple(value)


def _stage_markers(value: Any, stages: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    label = f"{ANNUAL_EDITION_FIELD}.stage_markers"
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{label} must be a non-empty object")
    for stage, marker in value.items():
        if stage not in stages:
            raise ValueError(f"{label} key {stage!r} is not one of the declared stages")
        if not isinstance(marker, str) or not marker.strip():
            raise ValueError(f"{label}.{stage} must be nonblank text")
    return tuple((stage, value[stage]) for stage in stages if stage in value)


def parse_annual_edition(source: Mapping[str, Any]) -> AnnualEdition | None:
    """Return the source's edition declaration, ``None`` when it declares none.

    Raises ``ValueError`` naming the first problem in a malformed declaration.
    """
    if ANNUAL_EDITION_FIELD not in source:
        return None
    raw = source[ANNUAL_EDITION_FIELD]
    if not isinstance(raw, Mapping):
        raise ValueError(f"{ANNUAL_EDITION_FIELD} must be an object")
    unknown = sorted(set(raw) - set(_REQUIRED_KEYS) - set(_OPTIONAL_KEYS))
    if unknown:
        raise ValueError(f"{ANNUAL_EDITION_FIELD} has unknown key(s): {', '.join(unknown)}")
    missing = [key for key in _REQUIRED_KEYS if key not in raw]
    if missing:
        raise ValueError(f"{ANNUAL_EDITION_FIELD} is missing: {', '.join(missing)}")

    edition_year = raw["edition_year"]
    if not _is_int(edition_year) or not 2000 <= edition_year <= 2099:
        raise ValueError(f"{ANNUAL_EDITION_FIELD}.edition_year must be a four-digit year")
    award_year = source.get("award_year")
    if _is_int(award_year) and award_year != edition_year:
        raise ValueError(f"{ANNUAL_EDITION_FIELD}.edition_year {edition_year} disagrees with award_year {award_year}")
    if str(source.get("url_stability", "")).strip():
        url_stability_kind(source)
    stages = _stages(raw["stages"])
    tracked_stage = raw["tracked_stage"]
    if tracked_stage not in stages:
        raise ValueError(f"{ANNUAL_EDITION_FIELD}.tracked_stage must be one of the declared stages")

    return AnnualEdition(
        edition_year=edition_year,
        landing_url=_https_url(raw["landing_url"], f"{ANNUAL_EDITION_FIELD}.landing_url"),
        release_window=_release_window(raw["release_window"]),
        stages=stages,
        tracked_stage=tracked_stage,
        edition_marker=(
            _year_template(raw["edition_marker"], f"{ANNUAL_EDITION_FIELD}.edition_marker")
            if "edition_marker" in raw
            else ""
        ),
        next_edition_url=(
            _https_url(
                _year_template(raw["next_edition_url"], f"{ANNUAL_EDITION_FIELD}.next_edition_url"),
                f"{ANNUAL_EDITION_FIELD}.next_edition_url",
            )
            if "next_edition_url" in raw
            else ""
        ),
        stage_markers=_stage_markers(raw["stage_markers"], stages) if "stage_markers" in raw else (),
    )


def validate_annual_edition_declaration(source: Mapping[str, Any]) -> str:
    """Return an error naming a malformed ``annual_edition`` object, or ``""``.

    An absent object is not an error here: configuration must stay loadable,
    and the annual audit reports the missing declaration instead.
    """
    try:
        declared = parse_annual_edition(source)
    except ValueError as exc:
        return str(exc)
    if declared is not None and not is_annual_source(source):
        return f"{ANNUAL_EDITION_FIELD} requires an annual check_frequency or refresh_cadence"
    return ""
