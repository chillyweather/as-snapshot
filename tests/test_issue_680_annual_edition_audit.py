"""Issue #680: the annual edition audit, judged against declared release windows.

An annual source carries its edition in its identity, and refreshing it is a
hand operation nobody is reminded to do. In run `4090c59a` (2026-09-13) three
active annual sources were still pinned to 2025 and two of them reported
`collected` — a year-old award list served as a current weekly signal, which
the run record cannot distinguish from a fresh one.

The calendar year alone cannot say when a newer edition can exist: a 2025
award list in September 2026 is usually correct. Each annual source therefore
declares when its editions are published, and the audit reports one of #680's
six verdicts. Offline it can conclude only `current` or `unable-to-determine`;
whether a newer edition is *published* is a question for the declared
official pages, so the audit asks for that check and never asserts staleness.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.annual_source_audit import CURRENT
from autoscout.annual_source_audit import NOT_YET_RELEASED
from autoscout.annual_source_audit import REASON_EDITION_YEAR_AHEAD
from autoscout.annual_source_audit import REASON_METADATA_INVALID
from autoscout.annual_source_audit import REASON_METADATA_MISSING
from autoscout.annual_source_audit import REASON_NEWER_EDITION_EXPECTED
from autoscout.annual_source_audit import REASON_NO_NEWER_EDITION_EXPECTED
from autoscout.annual_source_audit import ROLLOVER_AVAILABLE
from autoscout.annual_source_audit import STABLE_URL_NEW_EDITION
from autoscout.annual_source_audit import STAGE_REVIEW_REQUIRED
from autoscout.annual_source_audit import UNABLE_TO_DETERMINE
from autoscout.annual_source_audit import VERDICTS
from autoscout.annual_source_audit import AnnualEditionEvidence
from autoscout.annual_source_audit import audit_annual_source
from autoscout.annual_source_audit import audit_annual_sources
from autoscout.annual_source_audit import declared_edition_year
from autoscout.annual_source_audit import is_annual_source

PORTFOLIO = Path(__file__).resolve().parents[1] / "configs" / "current-weekly-portfolio.json"
TODAY = date(2026, 11, 13)


def _annual(*, edition_year: int = 2026, opens: str = "09-01", closes: str = "09-30", **overrides: Any) -> dict:
    source: dict[str, Any] = {
        "name": f"A Prize {edition_year}",
        "url": f"https://example.test/prize/{edition_year}",
        "family": "award",
        "active": True,
        "check_frequency": "annual",
        "refresh_cadence": "annual",
        "url_stability": "year-specific",
        # Required by validate_config; the command validates before auditing so
        # it can tell an unreadable config from a clean one.
        "language": "en",
        "annual_edition": {
            "edition_year": edition_year,
            "landing_url": "https://example.test/prize/",
            "release_window": {"opens": opens, "closes": closes},
            "stages": ["longlist", "shortlist", "winner"],
            "tracked_stage": "shortlist",
        },
    }
    source.update(overrides)
    return source


def _portfolio_sources() -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = json.loads(PORTFOLIO.read_text(encoding="utf-8"))["sources"]
    return sources


def _portfolio_source(name: str) -> dict[str, Any]:
    return next(source for source in _portfolio_sources() if source["name"] == name)


# ── The vocabulary ──────────────────────────────────────────────────────────


def test_the_audit_speaks_the_six_verdicts_of_the_issue() -> None:
    """#680 acceptance criterion 2 names these six and no others."""
    assert VERDICTS == (
        CURRENT,
        NOT_YET_RELEASED,
        ROLLOVER_AVAILABLE,
        STABLE_URL_NEW_EDITION,
        STAGE_REVIEW_REQUIRED,
        UNABLE_TO_DETERMINE,
    )
    assert set(VERDICTS) == {
        "current",
        "not-yet-released",
        "rollover-available",
        "stable-url-new-edition",
        "stage-review-required",
        "unable-to-determine",
    }


def test_evidence_verdicts_are_not_produced_offline() -> None:
    """Only an official page can say an edition was published or a stage
    moved on; without evidence the audit reaches neither, on any day of the
    year, for any real source."""
    days = [date(2026, month, day) for month in range(1, 13) for day in (1, 15)]
    offline = {
        audit_annual_source(source, today=day).verdict
        for source in _portfolio_sources()
        if is_annual_source(source) and source.get("active") is not False
        for day in days
    }

    assert offline <= {CURRENT, UNABLE_TO_DETERMINE}


def test_empty_evidence_is_never_read_as_a_clean_page() -> None:
    """Evidence that holds no observation is not the offline path: the audit
    says it could not judge the source rather than falling back to the calendar."""
    verdict = audit_annual_source(_annual(), today=TODAY, evidence=AnnualEditionEvidence())

    assert verdict.verdict == UNABLE_TO_DETERMINE


# ── Release windows ─────────────────────────────────────────────────────────


def test_an_edition_whose_successor_window_has_not_opened_is_current() -> None:
    """A 2026 edition in February 2027 is current when editions publish in September."""
    verdict = audit_annual_source(_annual(edition_year=2026), today=date(2027, 2, 1))

    assert (verdict.verdict, verdict.reason) == (CURRENT, REASON_NO_NEWER_EDITION_EXPECTED)
    assert not verdict.needs_attention
    assert "2027-09-01" in verdict.detail


def test_a_window_that_has_not_opened_prevents_a_premature_flag() -> None:
    """The #680 case: a 2025 list in October 2026 is current while the 2026
    edition's window opens in November. The old year-only audit flagged it."""
    source = _annual(edition_year=2025, opens="11-01", closes="11-30")

    assert audit_annual_source(source, today=date(2026, 10, 31)).verdict == CURRENT
    assert audit_annual_source(source, today=date(2026, 11, 1)).verdict == UNABLE_TO_DETERMINE


def test_an_opened_window_for_a_newer_edition_asks_for_an_evidence_check() -> None:
    """The case that asks the operator to act. It is not `rollover-available`:
    the calendar says a newer edition may exist, not that it does."""
    verdict = audit_annual_source(_annual(edition_year=2025), today=TODAY)

    assert verdict.verdict == UNABLE_TO_DETERMINE
    assert verdict.reason == REASON_NEWER_EDITION_EXPECTED
    assert verdict.needs_attention
    assert "edition 2026" in verdict.detail
    assert "2026-09-01" in verdict.detail
    assert "https://example.test/prize/" in verdict.detail
    assert "stale" not in verdict.detail.lower()


def test_a_multi_year_gap_names_the_newest_expected_edition() -> None:
    verdict = audit_annual_source(_annual(edition_year=2023), today=TODAY)

    assert verdict.reason == REASON_NEWER_EDITION_EXPECTED
    assert "edition 2026" in verdict.detail
    assert "3 editions newer" in verdict.detail


def test_an_edition_ahead_of_every_opened_window_is_reported() -> None:
    """A typo in an edition identity is worth surfacing, not silently passing."""
    verdict = audit_annual_source(_annual(edition_year=2027), today=TODAY)

    assert (verdict.verdict, verdict.reason) == (UNABLE_TO_DETERMINE, REASON_EDITION_YEAR_AHEAD)
    assert "2027-09-01" in verdict.detail


def test_the_window_opens_on_its_first_day() -> None:
    source = _annual(edition_year=2025)

    assert audit_annual_source(source, today=date(2026, 8, 31)).verdict == CURRENT
    assert audit_annual_source(source, today=date(2026, 9, 1)).reason == REASON_NEWER_EDITION_EXPECTED


def test_a_calendar_year_offset_shifts_the_window() -> None:
    """The Andrew Carnegie Medals label as 2026 the edition whose longlist was
    published in October 2025."""
    source = _annual(edition_year=2026, opens="10-01", closes="10-31")
    source["annual_edition"]["release_window"]["calendar_year_offset"] = -1

    assert audit_annual_source(source, today=date(2025, 10, 1)).verdict == CURRENT
    assert audit_annual_source(source, today=date(2025, 9, 30)).reason == REASON_EDITION_YEAR_AHEAD
    assert audit_annual_source(source, today=date(2026, 9, 30)).verdict == CURRENT
    assert audit_annual_source(source, today=date(2026, 10, 1)).reason == REASON_NEWER_EDITION_EXPECTED


def test_a_positive_offset_covers_an_award_announced_the_year_after_its_label() -> None:
    source = _annual(edition_year=2025, opens="01-15", closes="02-15")
    source["annual_edition"]["release_window"]["calendar_year_offset"] = 1

    assert audit_annual_source(source, today=date(2026, 12, 31)).verdict == CURRENT
    assert audit_annual_source(source, today=date(2027, 1, 15)).reason == REASON_NEWER_EDITION_EXPECTED


# ── Missing or malformed metadata ───────────────────────────────────────────


def test_missing_annual_metadata_is_unable_to_determine_and_names_what_is_missing() -> None:
    """Deutscher Sachbuchpreis read its edition from a stable landing page
    with no year in its identity (#679). Without a declaration the audit
    cannot confirm the edition it serves and must say so."""
    source = _annual()
    del source["annual_edition"], source["url_stability"]
    verdict = audit_annual_source(source, today=TODAY)

    assert (verdict.verdict, verdict.reason) == (UNABLE_TO_DETERMINE, REASON_METADATA_MISSING)
    assert verdict.needs_attention
    assert "url_stability" in verdict.detail
    assert "annual_edition" in verdict.detail
    assert "refresh_cadence" not in verdict.detail


def test_a_stable_landing_page_without_metadata_is_never_passed_as_current() -> None:
    verdict = audit_annual_source(
        {
            "name": "Deutscher Sachbuchpreis",
            "url": "https://www.deutscher-sachbuchpreis.de/",
            "check_frequency": "annual",
        },
        today=TODAY,
    )

    assert verdict.reason == REASON_METADATA_MISSING
    assert verdict.edition_year is None


def test_malformed_metadata_reached_without_validation_is_reported_not_raised() -> None:
    """The weekly advisory audits a config it did not validate."""
    source = _annual()
    source["annual_edition"]["release_window"] = {"opens": "13-01", "closes": "09-30"}
    verdict = audit_annual_source(source, today=TODAY)

    assert (verdict.verdict, verdict.reason) == (UNABLE_TO_DETERMINE, REASON_METADATA_INVALID)


# ── What it reads the year from ─────────────────────────────────────────────


def test_the_declared_edition_year_outranks_award_year_and_identity() -> None:
    source = _annual(edition_year=2026, name="A Prize 2024", award_year=2026)

    assert declared_edition_year(source) == (2026, "annual_edition")
    del source["annual_edition"]
    assert declared_edition_year(source) == (2026, "award_year")


def test_the_year_is_read_from_the_name_or_the_url() -> None:
    assert declared_edition_year({"name": "A Prize 2025", "url": "https://example.test/prize"}) == (2025, "name")
    assert declared_edition_year({"name": "A Prize", "url": "https://example.test/p/2025"}) == (2025, "url")
    assert declared_edition_year({"name": "A Prize", "url": "https://example.test/p"}) == (None, "")


def test_a_stale_display_name_beside_a_refreshed_url_takes_the_newer_year() -> None:
    """#679's exact shape: the source was named for 2024 while its page served
    the 2026 edition. The refreshed half is the one that reflects reality."""
    assert declared_edition_year({"name": "A Prize 2024", "url": "https://example.test/p/2026"}) == (2026, "url")


def test_digits_that_are_not_edition_years_are_not_read_as_years() -> None:
    """An ISBN, a rank, or a numeric path segment is not an edition year.

    An ISBN can carry a *separator*-isolated year, as `978-0-2026-0000-0`
    does, which every digit lookaround accepts. Identifier runs are masked
    before years are read.
    """
    for url in (
        "https://example.test/ranking-nielsen/semanal/9",
        "https://example.test/libros/algo_978-84-08-32009-8",
        "https://example.test/top/20250",
        "https://example.test/list/1999",
        # Separator-isolated years inside identifiers:
        "https://example.test/book/978-0-2026-0000-0",
        "https://example.test/book/9780202600000",
        "https://example.test/book/0-306-40615-2",
    ):
        assert declared_edition_year({"name": "A Prize", "url": url}) == (None, ""), url


def test_an_isbn_never_masks_a_real_edition_year_beside_it() -> None:
    """Masking identifiers must not eat the year the source actually declares."""
    assert declared_edition_year({"name": "A Prize", "url": "https://example.test/2025/book/978-0-2026-0000-0"}) == (
        2025,
        "url",
    )


def test_a_dated_url_keeps_its_year() -> None:
    """An ISO date is eight digits, below the identifier bound."""
    assert declared_edition_year({"name": "A Prize", "url": "https://example.test/news/2026-09-13/longlist"}) == (
        2026,
        "url",
    )


# ── Which sources are audited ───────────────────────────────────────────────


def test_both_cadence_spellings_declare_an_annual_source() -> None:
    """`refresh_cadence` is a validated alias of `check_frequency` and both
    appear across the portfolio; reading one alone skips real annual sources."""
    assert is_annual_source({"check_frequency": "annual"}) is True
    assert is_annual_source({"refresh_cadence": "annual"}) is True
    assert is_annual_source({"check_frequency": "weekly"}) is False
    assert is_annual_source({}) is False


def test_only_active_annual_sources_are_audited() -> None:
    sources = [
        _annual(name="Active Annual 2025"),
        _annual(name="Inactive Annual 2025", active=False),
        _annual(name="Weekly Chart 2025", check_frequency="weekly", refresh_cadence="weekly"),
    ]

    audited = audit_annual_sources(sources, today=TODAY)

    assert [verdict.source_name for verdict in audited] == ["Active Annual 2025"]


# ── The audit is read-only ──────────────────────────────────────────────────


def test_auditing_does_not_mutate_the_sources_it_reads() -> None:
    """#680's hard constraint: the audit never rewrites configuration."""
    sources = [_annual(edition_year=2025), _annual(edition_year=2026)]
    before = json.dumps(sources, sort_keys=True)

    audit_annual_sources(sources, today=TODAY)

    assert json.dumps(sources, sort_keys=True) == before


# ── Against the real portfolio ──────────────────────────────────────────────


def test_the_real_portfolio_on_2026_11_13() -> None:
    """Pinned against live config on a fixed date.

    National Book Awards (#826), PW Best Books and Goodreads Choice Awards
    (#996) were rolled over to 2026 by hand, so none of them is due. The one
    finding is the Andrew Carnegie Medals: the 2027 longlist would have been
    published in October 2026, and only its official page can say whether it
    was (ALA put the medals on hiatus after the 2026 edition).
    """
    verdicts = audit_annual_sources(_portfolio_sources(), today=TODAY)
    flagged = {v.source_name: v.reason for v in verdicts if v.needs_attention}

    assert len(verdicts) == 15
    assert flagged == {"Andrew Carnegie Medals for Excellence - Nonfiction 2026": REASON_NEWER_EDITION_EXPECTED}


def test_the_real_portfolio_passes_its_stable_landing_pages_as_current() -> None:
    """Deutscher Sachbuchpreis and Magnesia Litera are deliberately configured
    at a landing page. With a declared edition neither needs a rollover."""
    by_name = {v.source_name: v.verdict for v in audit_annual_sources(_portfolio_sources(), today=TODAY)}

    assert by_name["Deutscher Sachbuchpreis"] == CURRENT
    assert by_name["Magnesia Litera 2026 — Naučná literatura a Publicistika"] == CURRENT


def test_the_retired_goodreads_2025_edition_would_have_been_flagged_only_once_its_window_opened() -> None:
    """Historical tripwire from the real configuration (#996).

    The 2025 edition stayed configured until the 2026 nominees were published
    on 10 November 2026. With the real Goodreads release window, that edition
    is current on 1 October 2026 and asks for a check on 13 November 2026.
    """
    source = copy.deepcopy(_portfolio_source("Goodreads Choice Awards 2026 Nonfiction"))
    retired = _portfolio_source("Goodreads Choice Awards 2025 Nonfiction")
    source.update(name=retired["name"], url=retired["url"])
    source["annual_edition"]["edition_year"] = 2025

    assert audit_annual_source(source, today=date(2026, 10, 1)).verdict == CURRENT
    late = audit_annual_source(source, today=TODAY)
    assert (late.verdict, late.reason) == (UNABLE_TO_DETERMINE, REASON_NEWER_EDITION_EXPECTED)


# ── The command surface ─────────────────────────────────────────────────────


def _config_with(tmp_path: Path, *sources: dict[str, Any]) -> Path:
    path = tmp_path / f"config-{len(list(tmp_path.iterdir()))}.json"
    path.write_text(json.dumps({"sources": list(sources)}), encoding="utf-8")
    return path


def test_the_command_reports_findings_with_a_distinct_exit_code(capsys: Any, tmp_path: Path) -> None:
    """0 when nothing needs checking, 1 when something does, so a script can
    act on the result without parsing the output."""
    from autoscout.annual_source_audit import audit_annual_sources_command

    clean = _config_with(tmp_path, _annual(edition_year=2026))
    assert audit_annual_sources_command(clean, today=TODAY) == 0
    assert "annual_audit_status=no_rollover_checks_required" in capsys.readouterr().out

    behind = _config_with(tmp_path, _annual(edition_year=2025))
    assert audit_annual_sources_command(behind, today=TODAY) == 1
    output = capsys.readouterr().out
    assert "annual_audit_status=rollover_checks_required" in output
    assert "annual_audit_rollover_checks_required=1" in output
    assert f"verdict={UNABLE_TO_DETERMINE}; reason={REASON_NEWER_EDITION_EXPECTED}" in output
    assert "tracked_stage=shortlist; release_window=09-01..09-30" in output


def test_missing_metadata_needs_the_operator(capsys: Any, tmp_path: Path) -> None:
    from autoscout.annual_source_audit import audit_annual_sources_command

    source = _annual()
    del source["annual_edition"]

    assert audit_annual_sources_command(_config_with(tmp_path, source), today=TODAY) == 1
    assert f"reason={REASON_METADATA_MISSING}" in capsys.readouterr().out


def test_malformed_metadata_is_a_config_error_not_a_finding(capsys: Any, tmp_path: Path) -> None:
    from autoscout.annual_source_audit import audit_annual_sources_command

    source = _annual()
    source["annual_edition"]["tracked_stage"] = "grand prize"

    assert audit_annual_sources_command(_config_with(tmp_path, source), today=TODAY) == 2
    output = capsys.readouterr().out
    assert "annual_audit_status=config_error" in output
    assert "tracked_stage" in output


def test_an_unreadable_config_is_an_error_not_a_clean_audit(capsys: Any, tmp_path: Path) -> None:
    """A config that cannot be read must never print "nothing to check"."""
    from autoscout.annual_source_audit import audit_annual_sources_command

    assert audit_annual_sources_command(tmp_path / "absent.json", today=TODAY) == 2
    assert "annual_audit_status=config_error" in capsys.readouterr().out


def test_a_broken_portfolio_reference_is_a_config_error_not_a_finding(capsys: Any, tmp_path: Path) -> None:
    """Exit 2, not 1 - and not a process exit.

    `resolve_weekly_portfolio` and `load_validated_config` both call sys.exit(1)
    on a bad config, and SystemExit does not inherit from Exception, so an
    `except ValueError` around either never fires. Borrowing one would report
    a missing portfolio file with the same code as a real rollover finding.
    """
    from autoscout.annual_source_audit import audit_annual_sources_command

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"weekly_portfolio": "no-such-portfolio.json"}), encoding="utf-8")

    assert audit_annual_sources_command(config, today=TODAY) == 2
    assert "annual_audit_status=config_error" in capsys.readouterr().out


def test_the_cli_audits_on_the_given_date(capsys: Any, tmp_path: Path) -> None:
    from autoscout.__main__ import main

    config = _config_with(tmp_path, _annual(edition_year=2025))

    assert main(["audit-annual-sources", "--config", str(config), "--today", "2026-08-31"]) == 0
    assert "annual_audit_date=2026-08-31" in capsys.readouterr().out
    assert main(["audit-annual-sources", "--config", str(config), "--today", "2026-09-01"]) == 1
