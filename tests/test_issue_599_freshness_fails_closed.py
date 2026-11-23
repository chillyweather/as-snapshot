"""Issue #599 - ranking freshness must not fail open on an unreadable date.

Every staleness comparison in :func:`ranking_page_identity` sits behind
``reference_date is not None``. A page whose reference date was absent, null,
or malformed was therefore never aged, so
``ranking_reporting_period_max_age_days`` read like a guarantee and enforced
nothing. That is the mechanism behind #597, where the Audible.de chart rested
on evidence frozen at 2026-08-04 and looked current from every artifact an
operator reads.

The three outcomes must stay distinguishable. A page that states a date the
system cannot read, a capture that states no date at all, and a page whose
period is genuinely too old are different facts and get different reasons.

Driven from a real live-portfolio source rather than a synthetic page, so the
guarantee is checked against a capture the weekly run actually replays.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from autoscout.config import load_validated_config
from autoscout.source_page_identity import ranking_page_identity
from autoscout.source_page_identity import validate_page_shape

ROOT = Path(__file__).resolve().parents[1]
PILOT_CONFIG = ROOT / "configs" / "current-weekly-pilot.json"
# The real capture from #597: committed with no reference_date at all, which is
# why its evidence could not age. The source is retired; the artifact is kept
# because it is the one that produced the fault.
ISSUE_597_CAPTURE = ROOT / "data" / "current-weekly-pilot" / "audible-de-biografien-erinnerungen-page.json"

UNREADABLE_FRAGMENT = "cannot read"
NO_DATE_FRAGMENT = "requires a reference date"
STALE_FRAGMENT = "is not current"


def _dated_ranking_source() -> dict[str, Any]:
    """Return a live ranking source that declares a maximum reporting-period age."""
    config = load_validated_config(PILOT_CONFIG)
    for source in config["sources"]:
        if source.get("ranking_reporting_period_max_age_days") and source.get("source_page_fixture_file"):
            resolved = dict(source)
            resolved["source_page_fixture_file"] = str(
                (PILOT_CONFIG.parent / source["source_page_fixture_file"]).resolve()
            )
            return resolved
    raise AssertionError("no live ranking source declares ranking_reporting_period_max_age_days")


def _capture(source: dict[str, Any]) -> dict[str, Any]:
    return json.loads(Path(source["source_page_fixture_file"]).read_text(encoding="utf-8"))


def test_the_committed_capture_is_still_accepted_as_current() -> None:
    """The fix must not refuse a page that carries a readable, current date.

    Stated first, because a freshness gate that refuses everything is not a
    fix, and the rest of this file is about refusals.
    """
    source = _dated_ranking_source()
    assert not validate_page_shape(source, _capture(source))


@pytest.mark.parametrize(
    ("label", "value"),
    (
        ("malformed month and day", "2026-13-45"),
        ("not a date at all", "last week"),
        ("partial", "2026-08"),
        ("empty-ish", "   x   "),
    ),
)
def test_a_stated_date_the_system_cannot_read_is_refused(label: str, value: str) -> None:
    source = _dated_ranking_source()
    page = {**_capture(source), "reference_date": value}

    error = validate_page_shape(source, page)

    assert UNREADABLE_FRAGMENT in error, f"{label}: {error!r}"
    assert "reference_date" in error


@pytest.mark.parametrize("mutation", ("absent", "null"))
def test_a_capture_with_no_date_cannot_satisfy_a_declared_maximum_age(mutation: str) -> None:
    source = _dated_ranking_source()
    page = _capture(source)
    if mutation == "absent":
        page.pop("reference_date", None)
        page.pop("run_date", None)
    else:
        page["reference_date"] = None
        page.pop("run_date", None)

    error = validate_page_shape(source, page)

    assert NO_DATE_FRAGMENT in error, error


def test_the_three_outcomes_have_three_distinct_reasons() -> None:
    """A wrong date, a missing date, and a stale page must not read the same."""
    source = _dated_ranking_source()
    base = _capture(source)

    unreadable = validate_page_shape(source, {**base, "reference_date": "not-a-date"})
    missing = validate_page_shape(source, {k: v for k, v in base.items() if k not in {"reference_date", "run_date"}})
    # The committed period is a recent week, so a reference date a year later
    # is genuinely past any declared maximum age.
    stale = validate_page_shape(source, {**base, "reference_date": "2027-08-22"})

    assert UNREADABLE_FRAGMENT in unreadable
    assert NO_DATE_FRAGMENT in missing
    assert STALE_FRAGMENT in stale
    assert len({unreadable, missing, stale}) == 3


def test_a_declared_maximum_age_is_now_enforced_on_every_route() -> None:
    """The bound must mean what it says, whatever the capture carries.

    Before #599 the same page validated as current with an unreadable date and
    was refused with a readable one, which is the definition of a bound that
    does not bind.
    """
    source = _dated_ranking_source()
    base = _capture(source)

    for label, page in (
        ("readable and far past the bound", {**base, "reference_date": "2027-08-22"}),
        ("unreadable", {**base, "reference_date": "2027-13-45"}),
        ("absent", {k: v for k, v in base.items() if k not in {"reference_date", "run_date"}}),
    ):
        assert validate_page_shape(source, page), f"{label}: a stale or unusable page must never validate"


def test_the_issue_597_capture_can_no_longer_pass_a_dated_freshness_contract() -> None:
    """The real artifact behind #597, under a dated ranking contract.

    The capture is committed with no reference_date, so before #599 it could
    satisfy a declared maximum age for ever. It is checked here under the
    contract it lacked, which is what let it escape every freshness gate.
    """
    assert ISSUE_597_CAPTURE.exists(), "the #597 capture is kept as the evidence behind this issue"
    page = json.loads(ISSUE_597_CAPTURE.read_text(encoding="utf-8"))
    assert page.get("reference_date") is None, "the capture's missing date is the point of this test"

    source = _dated_ranking_source()
    borrowed = copy.deepcopy(page)
    borrowed["source_page"] = source["url"]
    borrowed["resolved_url"] = source["url"]

    assert validate_page_shape(source, borrowed), "a capture with no reference date must not satisfy a dated contract"


def test_the_underlying_comparison_still_needs_a_reference_date() -> None:
    """Name the fail-open precisely, so a future change cannot reintroduce it.

    ``ranking_page_identity`` keeps its ``reference_date is not None`` guard,
    because callers with no freshness bound legitimately pass ``None``. What
    changed is that a caller which does have a bound can no longer reach it
    with ``None``. This pins the underlying behaviour so the guarantee is
    understood to live in the caller, not here.
    """
    source = _dated_ranking_source()
    html = str(_capture(source)["html"])

    assert ranking_page_identity(html, source, reference_date=date(2027, 8, 22)) is None
    assert ranking_page_identity(html, source, reference_date=None) is not None
