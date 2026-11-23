"""PM deals become rights/deal Scouting Candidates in the weekly review (Issue #945).

Every deal here is invented (tests/pm_deal_fixtures.py). The result file is
written by the real PM deals pass run against a fake mailbox, so the capture
reads exactly the shape the pass leaves behind.
"""

from __future__ import annotations

import json
from datetime import UTC
from datetime import date
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from autoscout import pm_deal_capture
from autoscout.cli.weekly import run_mvp
from autoscout.codex_workflow import finalize_codex_run
from autoscout.codex_workflow import prepare_codex_extraction
from autoscout.config import load_validated_config
from autoscout.json_io import read_json
from autoscout.pm_mailbox import MessageHeader
from autoscout.pm_precollection import run_pass
from autoscout.review_rendering import render_review
from autoscout.russian_workflow import source_problem_message
from autoscout.source_check import check_sources
from autoscout.source_collectors import COLLECTOR_REGISTRY
from autoscout.source_collectors import CollectorRunError
from autoscout.source_gap_discovery import discover_source_gaps
from autoscout.source_rules import is_skipped_source
from autoscout.weekly_workflow import _source_problems_from_capture
from tests.pm_deal_fixtures import DAILY_DEALS_SUBJECT
from tests.pm_deal_fixtures import PM_SENDER
from tests.pm_deal_fixtures import message
from tests.pm_deal_fixtures import profile

ROOT = Path(__file__).resolve().parents[1]
RUN_DATE = "2026-11-04"
PASS_TIME = datetime(2026, 11, 4, 6, 0, tzinfo=UTC)
PM_NAME = "Publishers Marketplace Deals"
# The invented loglines of the synthetic email: none may leave the Git-ignored pass result.
LOGLINES = (
    "a novel about two sisters who inherit a failing orchard",
    "a narrative history of the merchants who tied distant ports together",
    "arguing that the slowest tools shape the most",
    "a memoir of a farming childhood",
    "a collection about crossings",
)
CATEGORIES = ["History", "Science/Technology", "Politics/Current Affairs"]


class _Mailbox:
    uidvalidity = 1

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def headers_since(self, since: date, senders: tuple[str, ...]) -> list[MessageHeader]:
        return [MessageHeader(uid=1, sender=PM_SENDER, subject=DAILY_DEALS_SUBJECT, date=PASS_TIME)]

    def fetch(self, uid: int) -> bytes:
        return self._raw

    def close(self) -> None:
        pass


def _collect(tmp_path: Path, raw: bytes | None = None, *, now: datetime = PASS_TIME, allowance: Any = None) -> Path:
    """Run the real PM deals pass over one synthetic email; return the result file."""
    pass_config = tmp_path / "pm-deal-email.json"
    pass_config.write_text(
        json.dumps(
            {
                "name": PM_NAME,
                "contract_family": "pm-deal-email",
                "mailbox": {
                    "host": "imap.example.com",
                    "port": 993,
                    "folder": "PM",
                    "user_env": "PM_USER",
                    "password_env": "PM_PASSWORD",
                },
                "senders": [PM_SENDER],
                "subjects": {"daily_deals": DAILY_DEALS_SUBJECT},
                "max_message_age_days": 14,
                "max_messages_per_pass": 30,
                "collection_file": "pm/collection.json",
                "attempt_file": "pm/attempt.json",
                "watermark_file": "pm/watermark.json",
            }
        ),
        encoding="utf-8",
    )
    mailbox = _Mailbox(raw if raw is not None else message())
    run_pass(
        pass_config,
        now=now,
        opener=lambda *_args: mailbox,
        environ={"PM_USER": "user@example.com", "PM_PASSWORD": "synthetic"},
        key_file=tmp_path / "no-key-file",
        **({"allowance": allowance} if allowance is not None else {}),
    )
    return tmp_path / "pm" / "collection.json"


def _pm_source(**overrides: Any) -> dict[str, Any]:
    return {
        "name": PM_NAME,
        "family": "rights/deal",
        "url": pm_deal_capture.SOURCE_URL,
        "access_status": "paid",
        "check_frequency": "weekly",
        "language": "en",
        "signal_family": "rights/deal",
        "expected_signal_strength": "rights/deal mention",
        "lifecycle_status": "probation",
        "contract_family": "pm-deal-email",
        "extraction_method": "pm-deal-email-live",
        "pm_collection_file": "pm/collection.json",
        "pm_max_collection_age_days": 8,
        "pm_category_labels": CATEGORIES,
        "seen_guids_file": "state/pm-seen-items.json",
        **overrides,
    }


def _write_config(tmp_path: Path, *sources: dict[str, Any]) -> Path:
    config_path = tmp_path / "live.json"
    config_path.write_text(json.dumps({"run_date": RUN_DATE, "sources": list(sources)}), encoding="utf-8")
    return config_path


def _live(tmp_path: Path, run_date: str = RUN_DATE, **overrides: Any) -> Any:
    source = _pm_source(**overrides)
    return COLLECTOR_REGISTRY["pm-deal-email-live"].collect(tmp_path / "live.json", tmp_path, source, run_date)


def _bundle(tmp_path: Path) -> dict[str, Any]:
    collection = pm_deal_capture.read_collection(_collect(tmp_path))
    return pm_deal_capture.capture(_pm_source(), date.fromisoformat(RUN_DATE), collection)


# -- Capture and replay seam --------------------------------------------------


def test_a_current_result_yields_one_mention_per_qualifying_nonfiction_deal(tmp_path: Path) -> None:
    _collect(tmp_path)
    config_path = _write_config(tmp_path, _pm_source())

    assert prepare_codex_extraction(config_path, tmp_path / "out", effective_date=RUN_DATE) == 0

    worklist = read_json(tmp_path / "out" / "codex-extraction-worklist.json")
    assert worklist["work_items"] == []
    [precollected] = worklist["precollected_sources"]
    mentions = read_json(tmp_path / "out" / precollected["recorded_mentions_file"])["mentions"]
    # Fiction, Poetry and the International rights deal yield nothing; so does an undeclared category.
    assert [(m["title"], m["primary_authors"], m["publisher"]) for m in mentions] == [
        ("THE SALT ROADS: HOW TRADE MADE THE SEA", ["Iris Mendel"], "Meridian Press"),
        ("THE QUIET MACHINE", ["Paul Ostrow", "Mina Kaur Sethi"], "Cobalt"),
    ]
    assert {m["signal_type"] for m in mentions} == {"rights/deal"}
    assert {m["publication_status"] for m in mentions} == {"forthcoming"}
    [run] = read_json(tmp_path / "out" / "source-runs.json")
    assert run["status"] == "precollected"
    assert run["debug_metadata"]["pm_dropped_rows"] == {"international_rights": 1, "not_nonfiction_section": 2}


def test_a_category_outside_the_declared_list_yields_no_mention(tmp_path: Path) -> None:
    _collect(tmp_path)
    result = _live(tmp_path, pm_category_labels=["History"])

    assert [m["title"] for m in result.mentions] == ["THE SALT ROADS: HOW TRADE MADE THE SEA"]
    assert result.debug_metadata["pm_dropped_rows"]["undeclared_category"] == 1


def test_the_evidence_link_is_the_agent_profile_then_the_agency_then_the_publisher(tmp_path: Path) -> None:
    _collect(tmp_path)
    by_title = {m["title"]: m for m in _live(tmp_path).mentions}

    # The first agent's profile; the agent of the second deal has no link, nor has the agency.
    assert by_title["THE SALT ROADS: HOW TRADE MADE THE SEA"]["evidence_url"] == profile(204)
    assert by_title["THE QUIET MACHINE"]["evidence_url"] == profile(302)
    row = {"agents": [("A", "")], "agency": ("B", profile(9)), "publisher": ("C", profile(8))}
    assert pm_deal_capture.evidence_url(row) == profile(9)
    assert pm_deal_capture.evidence_url({**row, "agency": None, "publisher": ("C", "")}) == ""


@pytest.mark.parametrize(
    ("collection", "reason"),
    [(None, "pm_collection_missing"), ("stale", "pm_stale_collection")],
)
def test_a_missing_or_stale_result_is_refused_by_name_and_the_run_continues(
    tmp_path: Path, collection: str | None, reason: str
) -> None:
    if collection == "stale":
        _collect(tmp_path, now=datetime(2026, 10, 20, 6, 0, tzinfo=UTC))
    other = {
        "name": "Fixture List",
        "family": "media/curated list",
        "url": "https://www.nonfiction.fr/rss-critiques.xml",
        "access_status": "open",
        "check_frequency": "weekly",
        "language": "fr",
        "extraction_method": "nonfiction-fr-rss-replay",
        "rss_fixture_file": str(ROOT / "tests/fixtures/nonfiction-fr/rss-mixed.xml"),
        "review_pages_fixture_file": str(ROOT / "tests/fixtures/nonfiction-fr/review-pages-mixed.json"),
    }
    config_path = _write_config(tmp_path, _pm_source(), other)

    assert prepare_codex_extraction(config_path, tmp_path / "out", effective_date=RUN_DATE) == 0

    worklist = read_json(tmp_path / "out" / "codex-extraction-worklist.json")
    source_runs = read_json(tmp_path / "out" / "source-runs.json")
    runs = {run["source_name"]: run for run in source_runs}
    assert runs[PM_NAME]["status"] == "failed"
    assert reason in runs[PM_NAME]["reason"]
    assert runs[PM_NAME]["debug_metadata"]["safe_failure_status"] == reason
    assert runs["Fixture List"]["status"] == "precollected"
    assert [entry["name"] for entry in worklist["precollected_sources"]] == ["Fixture List"]
    # The weekly completion summary names PM.
    assert PM_NAME in source_problem_message(_source_problems_from_capture({"source_runs": source_runs}))


def test_the_replay_variant_reads_a_recorded_result_by_its_own_date(tmp_path: Path) -> None:
    _collect(tmp_path)
    source = _pm_source(extraction_method="pm-deal-email-replay")

    result = COLLECTOR_REGISTRY["pm-deal-email-replay"].collect(tmp_path / "live.json", tmp_path, source, "2027-03-01")

    assert len(result.mentions) == 2
    assert not (tmp_path / "state" / "pm-seen-items.json").exists()


# -- Mention validation --------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "THE SALT ROAD"),
        ("primary_authors", ["Iris Mendell"]),
        ("publisher", "Meridian Books"),
        ("evidence_url", profile(205)),
        ("evidence_note", "A model's summary of the deal."),
        ("signal_type", "media/curated list"),
    ],
)
def test_a_mention_whose_field_differs_from_the_captured_row_is_refused(tmp_path: Path, field: str, value: Any) -> None:
    bundle = _bundle(tmp_path)
    [salt_roads, _quiet] = [
        pm_deal_capture.mention(row, _pm_source(), RUN_DATE)
        for row in pm_deal_capture.qualifying_rows(bundle, _pm_source())
    ]

    assert pm_deal_capture.mention_violation(salt_roads, bundle, _pm_source()) == ""
    assert "does not agree" in pm_deal_capture.mention_violation({**salt_roads, field: value}, bundle, _pm_source())


def test_a_mention_carrying_deal_text_is_refused(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    entry = pm_deal_capture.mention(pm_deal_capture.qualifying_rows(bundle, _pm_source())[0], _pm_source(), RUN_DATE)

    violation = pm_deal_capture.mention_violation({**entry, "logline": LOGLINES[1]}, bundle, _pm_source())

    assert "fields the deal rows do not support: logline" in violation


# -- Seen deals ----------------------------------------------------------------


def _weekly_capture(tmp_path: Path, run_date: str, *, pipeline: bool = True) -> list[dict[str, Any]]:
    """The weekly run's PM steps: capture, finalize, then the pipeline. Returns the recorded mentions."""
    config_path = tmp_path / "live.json"
    if not config_path.exists():
        _write_config(tmp_path, _pm_source())
    out = tmp_path / f"out-{run_date}-{len(list(tmp_path.glob('out-*')))}"
    assert prepare_codex_extraction(config_path, out, effective_date=run_date) == 0
    [precollected] = read_json(out / "codex-extraction-worklist.json")["precollected_sources"]
    assert finalize_codex_run(out, config_path, effective_date=run_date) == 0
    if pipeline:
        run_mvp(out / "codex-replay-config.json", out, out)
    return read_json(out / precollected["recorded_mentions_file"])["mentions"]


def test_a_deal_reported_by_an_earlier_run_is_not_new_again(tmp_path: Path) -> None:
    _collect(tmp_path)

    first = _weekly_capture(tmp_path, RUN_DATE)
    same_day = _weekly_capture(tmp_path, RUN_DATE)
    next_run = _weekly_capture(tmp_path, "2026-11-05")

    assert len(first) == 2
    # A second run on the same date does not lose what the first one reported.
    assert len(same_day) == 2
    assert next_run == []
    seen = read_json(tmp_path / "state" / "pm-seen-items.json")
    assert len(seen["seen_guids"]) == 2
    assert {entry["first_seen_date"] for entry in seen["seen_guids"]} == {RUN_DATE}


def test_capture_finalize_and_the_source_health_check_mark_no_deal_as_seen(tmp_path: Path) -> None:
    _collect(tmp_path)
    config_path = _write_config(tmp_path, _pm_source())

    # A run that stops before its pipeline consumes the deals reports none of them.
    assert len(_weekly_capture(tmp_path, RUN_DATE, pipeline=False)) == 2
    check_sources(config_path, live=True, effective_date=RUN_DATE)

    assert not (tmp_path / "state" / "pm-seen-items.json").exists()
    # A weekly run on a later date still reports both deals.
    assert len(_weekly_capture(tmp_path, "2026-11-05")) == 2


def test_a_malformed_seen_index_is_a_named_failure(tmp_path: Path) -> None:
    _collect(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "pm-seen-items.json").write_text('{"seen_guids": [{"guid": "x"}]}', encoding="utf-8")

    with pytest.raises(CollectorRunError, match="pm_seen_state"):
        _live(tmp_path)


# -- The weekly review ---------------------------------------------------------


def test_a_pm_deal_merges_with_kirkus_and_no_logline_reaches_state_or_the_review(tmp_path: Path) -> None:
    _collect(tmp_path)
    kirkus = {
        "name": "Kirkus Fixture",
        "family": "media/curated list",
        "url": "https://example.test/kirkus",
        "access_status": "open",
        "extraction_method": "fixture",
        "check_frequency": "weekly",
        "language": "en",
        "fixture_candidates": [
            {
                "title": "THE SALT ROADS: HOW TRADE MADE THE SEA",
                "primary_authors": ["Iris Mendel"],
                "source_url": "https://example.test/kirkus/salt-roads",
                "signal_strength": "minor mention",
                "candidate_category": "adult nonfiction",
                "evidence_note": "Kirkus nonfiction listing row.",
            }
        ],
    }
    config_path = _write_config(tmp_path, _pm_source(extraction_method="pm-deal-email-replay"), kirkus)
    out = tmp_path / "out"

    run_mvp(config_path, out, out)
    render_review(out, out, effective_date=RUN_DATE)

    candidates = {c["title"]: c for c in read_json(out / "scouting-candidates.json")}
    assert set(candidates) == {"THE SALT ROADS: HOW TRADE MADE THE SEA", "THE QUIET MACHINE"}
    salt_roads = candidates["THE SALT ROADS: HOW TRADE MADE THE SEA"]
    assert {e["source_name"] for e in salt_roads["source_evidence"]} == {PM_NAME, "Kirkus Fixture"}
    review = (out / "weekly-review.md").read_text(encoding="utf-8")
    html = (out / "weekly-review.html").read_text(encoding="utf-8")
    # The line reads as a rights lead since #948: editor, contact, already-sold territories, terms, season.
    deal_line = (
        "Meridian Press (редактор: Jon Pike, Lena Moss); агент: Omar Reyes, Tess Lowe (Park, Lane & Reyes Literary); "
        "контакт: omar@example.com, rights@example.com; территория: world English; права уже проданы: UK, Italy; "
        "сделка: significant ($251–499 тыс.); публикация: осень 2027; категория PM: History"
    )
    assert "Сделка о правах" in review and deal_line in review
    assert deal_line.replace("&", "&amp;") in html
    assert (
        "Cobalt (редактор: Ruth Fell); агент: Sam Wynn (Wynn Agency); территория: NA; категория PM: Science/Technology"
        in review
    )
    # No retained logline string reaches canonical state, the review or the editor CSV.
    for path in out.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert not [logline for logline in LOGLINES if logline in text], path.name


def test_a_run_without_a_rights_deal_keeps_its_review_table_shape(tmp_path: Path) -> None:
    source = {
        "name": "Fixture List",
        "family": "media/curated list",
        "url": "https://example.test/list",
        "access_status": "open",
        "extraction_method": "fixture",
        "check_frequency": "weekly",
        "language": "en",
        "fixture_candidates": [
            {
                "title": "A Plain Book",
                "primary_authors": ["Una Plain"],
                "source_url": "https://example.test/list/1",
                "signal_strength": "minor mention",
                "candidate_category": "adult nonfiction",
                "evidence_note": "Listed.",
            }
        ],
    }
    out = tmp_path / "out"
    run_mvp(_write_config(tmp_path, source), out, out)
    render_review(out, out, effective_date=RUN_DATE)

    assert "Сделка о правах" not in (out / "weekly-review.md").read_text(encoding="utf-8")
    assert "Rights deal" not in (out / "editor-facing.csv").read_text(encoding="utf-8").splitlines()[0]


# -- Configuration -------------------------------------------------------------


def test_the_paid_pm_source_is_collected_while_other_paid_sources_stay_skipped() -> None:
    assert is_skipped_source({"access_status": "paid"}) is True
    assert is_skipped_source({"access_status": "paid", "contract_family": "kirkus-coming-soon"}) is True
    assert is_skipped_source({"access_status": "paid", "contract_family": "pm-deal-email"}) is False
    assert is_skipped_source({"access_status": "blocked", "contract_family": "pm-deal-email"}) is True


def test_the_weekly_configs_declare_pm_as_an_established_rights_deal_source() -> None:
    live = load_validated_config(ROOT / "configs/current-weekly-pilot-live.json")
    replay = load_validated_config(ROOT / "configs/current-weekly-pilot.json")
    live_source = next(source for source in live["sources"] if source["name"] == PM_NAME)
    replay_source = next(source for source in replay["sources"] if source["name"] == PM_NAME)

    for source in (live_source, replay_source):
        assert source["family"] == source["signal_family"] == "rights/deal"
        assert source["access_status"] == "paid"
        assert source["lifecycle_status"] == "established"
        assert source["contract_family"] == "pm-deal-email"
        assert source["pm_category_labels"]
    assert live_source["extraction_method"] == "pm-deal-email-live"
    assert replay_source["extraction_method"] == "pm-deal-email-replay"
    # The weekly run reads the result the blessed pass writes.
    pass_config = ROOT / "configs/pm-deal-email.json"
    declared = read_json(pass_config)
    assert (ROOT / "configs" / live_source["pm_collection_file"]).resolve() == (
        pass_config.parent / declared["collection_file"]
    ).resolve()
    assert declared["name"] == PM_NAME


def test_the_committed_replay_fixture_yields_mentions_without_live_collection() -> None:
    config_path = ROOT / "configs/current-weekly-pilot.json"
    source = next(source for source in load_validated_config(config_path)["sources"] if source["name"] == PM_NAME)

    result = COLLECTOR_REGISTRY["pm-deal-email-replay"].collect(config_path, ROOT, source, RUN_DATE)

    assert [m["title"] for m in result.mentions] == ["THE SALT ROADS: HOW TRADE MADE THE SEA", "THE QUIET MACHINE"]
    for mention in result.mentions:
        assert (
            pm_deal_capture.mention_violation(
                mention,
                pm_deal_capture.capture(
                    source,
                    date.fromisoformat(result.debug_metadata["pm_collected_date"]),
                    pm_deal_capture.read_collection(pm_deal_capture.collection_path(config_path, source)),
                ),
                source,
            )
            == ""
        )


def test_source_gap_discovery_no_longer_reports_rights_deal_as_missing() -> None:
    portfolio = read_json(ROOT / "configs/current-weekly-portfolio.json")

    coverage = discover_source_gaps(portfolio, signal_family="rights/deal")["family_coverage"]["rights/deal"]

    assert coverage["active_source_count"] >= 1
    assert "no active source" not in coverage["weakness_reasons"]
    assert PM_NAME in [source["name"] for source in coverage["sources"] if source["active"]]


def test_the_finalized_replay_config_keeps_pm_collectable(tmp_path: Path) -> None:
    _collect(tmp_path)
    config_path = _write_config(tmp_path, _pm_source())
    out = tmp_path / "out"

    assert prepare_codex_extraction(config_path, out, effective_date=RUN_DATE) == 0
    assert finalize_codex_run(out, config_path, effective_date=RUN_DATE) == 0

    [replay_source] = read_json(out / "codex-replay-config.json")["sources"]
    assert replay_source["extraction_method"] == "recorded"
    assert replay_source["contract_family"] == "pm-deal-email"
    # The paid PM source is not skipped when the extraction stage replays its mentions.
    assert is_skipped_source(replay_source) is False


def test_a_declared_deal_without_a_profile_link_is_named_in_the_summary(tmp_path: Path) -> None:
    result_path = _collect(tmp_path)
    result = read_json(result_path)
    for row in result["rows"]:
        if row["title"] == "THE QUIET MACHINE":
            row["publisher"]["profile_url"] = ""
    result_path.write_text(json.dumps(result), encoding="utf-8")
    config_path = _write_config(tmp_path, _pm_source())

    assert prepare_codex_extraction(config_path, tmp_path / "out", effective_date=RUN_DATE) == 0

    [run] = read_json(tmp_path / "out" / "source-runs.json")
    assert run["debug_metadata"]["pm_dropped_rows"]["no_profile_link"] == 1
    message = source_problem_message(_source_problems_from_capture({"source_runs": [run]}))
    assert f"{PM_NAME} (1 из 2)" in message


def test_a_deal_without_a_profile_link_is_still_named_after_the_extraction_replay(tmp_path: Path) -> None:
    result_path = _collect(tmp_path)
    result = read_json(result_path)
    for row in result["rows"]:
        if row["title"] == "THE QUIET MACHINE":
            row["publisher"]["profile_url"] = ""
    result_path.write_text(json.dumps(result), encoding="utf-8")
    config_path = _write_config(tmp_path, _pm_source())
    out = tmp_path / "out"
    assert prepare_codex_extraction(config_path, out, effective_date=RUN_DATE) == 0
    assert finalize_codex_run(out, config_path, effective_date=RUN_DATE) == 0

    # The extraction stage replays the recorded PM mentions into a new source-run record.
    replayed = tmp_path / "replayed"
    run_mvp(out / "codex-replay-config.json", replayed, replayed)

    [run] = [run for run in read_json(replayed / "source-runs.json") if run["source_name"] == PM_NAME]
    assert run["debug_metadata"]["incomplete_ranking"] is True
    assert run["debug_metadata"]["pm_dropped_rows"]["no_profile_link"] == 1
    assert (run["raw_mention_count"], run["valid_mention_count"]) == (2, 1)
    message = source_problem_message(_source_problems_from_capture({"source_runs": [run]}))
    assert f"{PM_NAME} (1 из 2)" in message
    render_review(replayed, replayed, effective_date=RUN_DATE)
    review = (replayed / "weekly-review.md").read_text(encoding="utf-8")
    assert f"**⚠ {PM_NAME}** — неполный охват рейтинга: 1 из 2 строк" in review


def test_a_pipeline_reading_the_pass_result_directly_marks_its_deals_as_seen(tmp_path: Path) -> None:
    _collect(tmp_path)
    config_path = _write_config(tmp_path, _pm_source())
    out = tmp_path / "out"

    run_mvp(config_path, out, out)

    seen = read_json(tmp_path / "state" / "pm-seen-items.json")
    assert len(seen["seen_guids"]) == 2
    # A run on a later date does not report the same deals again.
    assert _live(tmp_path, "2026-11-05").mentions == []
