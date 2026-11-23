"""PW issue reviews become Scouting Candidates in the weekly review (Issue #955).

Every review here is invented. The result file holds rows the real PW parser
produced from synthetic page text, so the capture reads exactly the shape the
PW issue pass leaves behind.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from state_store_support import read_state

from autoscout import pw_issue_capture
from autoscout.cli.weekly import run_mvp
from autoscout.codex_workflow import finalize_codex_run
from autoscout.codex_workflow import prepare_codex_extraction
from autoscout.config import load_validated_config
from autoscout.json_io import read_json
from autoscout.pw_issue_parser import parse_pages
from autoscout.review_rendering import render_review
from autoscout.russian_workflow import source_problem_message
from autoscout.source_check import check_sources
from autoscout.source_collectors import COLLECTOR_REGISTRY
from autoscout.source_collectors import CollectorRunError
from autoscout.source_rules import is_skipped_source
from autoscout.weekly_workflow import _source_problems_from_capture

ROOT = Path(__file__).resolve().parents[1]
RUN_DATE = "2026-11-04"
ISSUE_DATE = "2026-11-02"
PW_NAME = "Publishers Weekly Nonfiction Reviews"
# Invented review prose: the pass never keeps it, and none may reach state or the review.
PROSE = (
    "Entirely invented prose about tidal harbours for testing only.",
    "Invented prose about a kitchen that never existed.",
    "Invented prose about the slow history of nothing.",
)
PREPARED_DESCRIPTION = (
    "Книга исследует историю вымышленных приливных гаваней и показывает, как местные жители наблюдали "
    "за изменениями берега на протяжении многих лет. Автор сопоставляет свидетельства разных эпох и "
    "объясняет, какие вопросы о жизни прибрежных сообществ помогают поставить эти записи."
)
PAGES = [
    f"""Nonfiction Reviews
History
★ The Salt Harbours
Ada Example. Imaginary Press, $28 hardcover (240p)
ISBN 978-0-306-40615-7
{PROSE[0]}
Agent: Bea Agent, Example Literary. (Jan.)
Food & Cooking
The Made-Up Kitchen
Cal Sample. Fictive Books, $35 (256p)
ISBN 978-1-4028-9462-6
{PROSE[1]} (Dec.)""",
    f"""Nonfiction Reviews
Science
Notes on Nothing
Dana Placeholder and Eli Standin. Hypothetical House, $30 (288p)
ISBN 978-0-596-52068-7
{PROSE[2]} (Feb.)""",
]


def _collect(tmp_path: Path, issue_date: str = ISSUE_DATE) -> Path:
    """Write a pass result holding the rows the real parser reads from the synthetic issue."""
    parsed = parse_pages(PAGES, issue_date)
    assert len(parsed.rows) == 3 and not parsed.rejections
    path = tmp_path / "pw" / "collection.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rows": parsed.rows,
                "issues": [{"issue_date": issue_date, "issue_id": "1002"}],
                "skipped_issues": [],
                "retry_issues": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def _pw_source(**overrides: Any) -> dict[str, Any]:
    return {
        "name": PW_NAME,
        "family": "media/curated list",
        "url": pw_issue_capture.SOURCE_URL,
        "access_status": "paid",
        "check_frequency": "weekly",
        "language": "en",
        "signal_family": "media/curated list",
        "expected_signal_strength": "minor mention",
        "lifecycle_status": "probation",
        "contract_family": "pw-issue-pdf",
        "extraction_method": "pw-issue-pdf-live",
        "pw_collection_file": "pw/collection.json",
        "pw_max_issue_age_days": 10,
        "pw_excluded_subsections": ["Food & Cooking"],
        "seen_guids_file": "state/pw-seen-items.json",
        **overrides,
    }


def _write_config(tmp_path: Path, *sources: dict[str, Any]) -> Path:
    config_path = tmp_path / "live.json"
    config_path.write_text(json.dumps({"run_date": RUN_DATE, "sources": list(sources)}), encoding="utf-8")
    return config_path


def _live(tmp_path: Path, run_date: str = RUN_DATE, **overrides: Any) -> Any:
    return COLLECTOR_REGISTRY["pw-issue-pdf-live"].collect(
        tmp_path / "live.json", tmp_path, _pw_source(**overrides), run_date
    )


def _bundle(tmp_path: Path) -> dict[str, Any]:
    collection = pw_issue_capture.read_collection(_collect(tmp_path))
    return pw_issue_capture.capture(_pw_source(), date.fromisoformat(RUN_DATE), collection)


# -- Capture and replay seam --------------------------------------------------


def test_a_current_result_yields_one_mention_per_qualifying_review(tmp_path: Path) -> None:
    _collect(tmp_path)
    config_path = _write_config(tmp_path, _pw_source())

    assert prepare_codex_extraction(config_path, tmp_path / "out", effective_date=RUN_DATE) == 0

    worklist = read_json(tmp_path / "out" / "codex-extraction-worklist.json")
    assert worklist["work_items"] == []
    [precollected] = worklist["precollected_sources"]
    mentions = read_json(tmp_path / "out" / precollected["recorded_mentions_file"])["mentions"]
    # The Food & Cooking review is in the declared exclusion list.
    assert [(m["title"], m["primary_authors"], m["publisher"], m["isbn"]) for m in mentions] == [
        ("The Salt Harbours", ["Ada Example"], "Imaginary Press", "9780306406157"),
        ("Notes on Nothing", ["Dana Placeholder", "Eli Standin"], "Hypothetical House", "9780596520687"),
    ]
    salt, notes = mentions
    assert salt["evidence_url"] == "https://www.publishersweekly.com/9780306406157"
    assert salt["source_week"] == ISSUE_DATE
    # A January publication reviewed in November is next year's.
    assert (salt["publication_year"], notes["publication_year"]) == (2027, 2027)
    assert salt["evidence_note"] == (
        "Publishers Weekly ★ starred review (Nonfiction, History), issue of 2026-11-02; "
        "publication Jan 2027; agent Bea Agent at Example Literary."
    )
    assert notes["evidence_note"] == (
        "Publishers Weekly review (Nonfiction, Science), issue of 2026-11-02; publication Feb 2027."
    )
    [run] = read_json(tmp_path / "out" / "source-runs.json")
    assert run["status"] == "precollected"
    assert run["debug_metadata"]["pw_dropped_rows"] == {"excluded_subsection": 1}


def test_the_exclusion_list_is_declared_and_editable(tmp_path: Path) -> None:
    _collect(tmp_path)

    assert len(_live(tmp_path, pw_excluded_subsections=[]).mentions) == 3
    assert [m["title"] for m in _live(tmp_path, pw_excluded_subsections=["Food & Cooking", "Science"]).mentions] == [
        "The Salt Harbours"
    ]
    with pytest.raises(CollectorRunError, match="pw_declaration"):
        _live(tmp_path, pw_excluded_subsections=["Cookery"])


@pytest.mark.parametrize(
    ("collection", "reason"),
    [(None, "pw_collection_missing"), ("stale", "pw_stale_collection")],
)
def test_a_missing_or_stale_result_is_refused_by_name_and_the_run_continues(
    tmp_path: Path, collection: str | None, reason: str
) -> None:
    if collection == "stale":
        _collect(tmp_path, issue_date="2026-10-19")
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
    config_path = _write_config(tmp_path, _pw_source(), other)

    assert prepare_codex_extraction(config_path, tmp_path / "out", effective_date=RUN_DATE) == 0

    worklist = read_json(tmp_path / "out" / "codex-extraction-worklist.json")
    source_runs = read_json(tmp_path / "out" / "source-runs.json")
    runs = {run["source_name"]: run for run in source_runs}
    assert runs[PW_NAME]["status"] == "failed"
    assert runs[PW_NAME]["debug_metadata"]["safe_failure_status"] == reason
    assert runs["Fixture List"]["status"] == "precollected"
    assert [entry["name"] for entry in worklist["precollected_sources"]] == ["Fixture List"]
    # The weekly completion summary names PW.
    assert PW_NAME in source_problem_message(_source_problems_from_capture({"source_runs": source_runs}))


def test_a_digital_edition_that_arrives_before_its_cover_date_is_current(tmp_path: Path) -> None:
    _collect(tmp_path, issue_date="2026-11-09")

    assert len(_live(tmp_path).mentions) == 2


def test_the_replay_variant_reads_a_recorded_result_by_its_newest_issue(tmp_path: Path) -> None:
    _collect(tmp_path)
    source = _pw_source(extraction_method="pw-issue-pdf-replay")

    result = COLLECTOR_REGISTRY["pw-issue-pdf-replay"].collect(tmp_path / "live.json", tmp_path, source, "2027-03-01")

    assert len(result.mentions) == 2
    assert not (tmp_path / "state" / "pw-seen-items.json").exists()


# -- Mention validation --------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "The Salt Harbour"),
        ("primary_authors", ["Ada Exampel"]),
        ("publisher", "Imaginary Books"),
        ("isbn", "9781402894626"),
        ("evidence_url", "https://www.publishersweekly.com/9781402894626"),
        ("evidence_note", "A model's summary of the review."),
    ],
)
def test_a_mention_whose_field_differs_from_the_captured_row_is_refused(tmp_path: Path, field: str, value: Any) -> None:
    bundle = _bundle(tmp_path)
    salt = pw_issue_capture.mention(pw_issue_capture.qualifying_rows(bundle, _pw_source())[0], _pw_source(), RUN_DATE)

    assert pw_issue_capture.mention_violation(salt, bundle, _pw_source()) == ""
    assert "does not agree" in pw_issue_capture.mention_violation({**salt, field: value}, bundle, _pw_source())


def test_a_mention_carrying_review_text_is_refused(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    entry = pw_issue_capture.mention(pw_issue_capture.qualifying_rows(bundle, _pw_source())[0], _pw_source(), RUN_DATE)

    violation = pw_issue_capture.mention_violation({**entry, "content_description": PROSE[0]}, bundle, _pw_source())

    # A PW description is allowed only when it matches the local prepared
    # artifact for this exact book; unapproved review prose still fails.
    assert "does not agree with a qualifying captured review row" in violation


def test_a_result_row_with_a_bad_isbn_is_a_named_failure(tmp_path: Path) -> None:
    path = _collect(tmp_path)
    result = read_json(path)
    result["rows"][0]["isbn13"] = "9780306406158"
    path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(CollectorRunError, match="pw_collection_state"):
        _live(tmp_path)


# -- Seen reviews --------------------------------------------------------------


def _weekly_capture(tmp_path: Path, run_date: str, *, pipeline: bool = True) -> list[dict[str, Any]]:
    """The weekly run's PW steps: capture, finalize, then the pipeline. Returns the recorded mentions."""
    config_path = tmp_path / "live.json"
    if not config_path.exists():
        _write_config(tmp_path, _pw_source())
    out = tmp_path / f"out-{run_date}-{len(list(tmp_path.glob('out-*')))}"
    assert prepare_codex_extraction(config_path, out, effective_date=run_date) == 0
    [precollected] = read_json(out / "codex-extraction-worklist.json")["precollected_sources"]
    assert finalize_codex_run(out, config_path, effective_date=run_date) == 0
    if pipeline:
        run_mvp(out / "codex-replay-config.json", out, out)
    return read_json(out / precollected["recorded_mentions_file"])["mentions"]


def test_prepared_local_description_reaches_history_before_search(tmp_path: Path) -> None:
    collection = _collect(tmp_path)
    row = read_json(collection)["rows"][0]
    prepared = tmp_path / "pw" / "descriptions.json"
    prepared.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "year": 2026,
                "descriptions": {
                    row["isbn13"]: {
                        "isbn13": row["isbn13"],
                        "title": row["title"],
                        "primary_authors": row["primary_authors"],
                        "issue_date": row["issue_date"],
                        "review_sha256": "a" * 64,
                        "content_description": PREPARED_DESCRIPTION,
                        "model": "local-test",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    _write_config(tmp_path, _pw_source(pw_descriptions_file="pw/descriptions.json"))

    mentions = _weekly_capture(tmp_path, RUN_DATE)
    described = next(item for item in mentions if item["isbn"] == row["isbn13"])
    assert described["content_description"] == PREPARED_DESCRIPTION
    assert described["description_provenance"] == "grounded"
    out = next(tmp_path.glob("out-*"))
    history = read_state(out, "candidate_history")
    record = next(item for item in history if item.get("isbn") == row["isbn13"])
    assert record["content_description"] == PREPARED_DESCRIPTION
    assert record["description_provenance"]["provenance"] == "grounded"
    assert not any(prose in json.dumps(record) for prose in PROSE)


def test_prepared_description_with_wrong_book_identity_is_refused(tmp_path: Path) -> None:
    collection = _collect(tmp_path)
    row = read_json(collection)["rows"][0]
    prepared = tmp_path / "pw" / "descriptions.json"
    prepared.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "year": 2026,
                "descriptions": {
                    row["isbn13"]: {
                        "isbn13": row["isbn13"],
                        "title": "A Different Book",
                        "primary_authors": row["primary_authors"],
                        "issue_date": row["issue_date"],
                        "review_sha256": "a" * 64,
                        "content_description": PREPARED_DESCRIPTION,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CollectorRunError, match="pw_description_state"):
        _live(tmp_path, pw_descriptions_file="pw/descriptions.json")


def test_a_review_reported_by_an_earlier_run_is_not_new_again(tmp_path: Path) -> None:
    _collect(tmp_path)

    first = _weekly_capture(tmp_path, RUN_DATE)
    same_day = _weekly_capture(tmp_path, RUN_DATE)
    next_run = _weekly_capture(tmp_path, "2026-11-05")

    assert len(first) == 2
    # A second run on the same date does not lose what the first one reported.
    assert len(same_day) == 2
    assert next_run == []
    seen = read_json(tmp_path / "state" / "pw-seen-items.json")
    assert sorted(entry["guid"] for entry in seen["seen_guids"]) == ["9780306406157", "9780596520687"]
    assert {entry["first_seen_date"] for entry in seen["seen_guids"]} == {RUN_DATE}


def test_capture_finalize_and_the_source_health_check_mark_no_review_as_seen(tmp_path: Path) -> None:
    _collect(tmp_path)
    config_path = _write_config(tmp_path, _pw_source())

    assert len(_weekly_capture(tmp_path, RUN_DATE, pipeline=False)) == 2
    check_sources(config_path, live=True, effective_date=RUN_DATE)

    assert not (tmp_path / "state" / "pw-seen-items.json").exists()
    assert len(_weekly_capture(tmp_path, "2026-11-05")) == 2


def test_a_malformed_seen_index_is_a_named_failure(tmp_path: Path) -> None:
    _collect(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "pw-seen-items.json").write_text('{"seen_guids": [{"guid": "x"}]}', encoding="utf-8")

    with pytest.raises(CollectorRunError, match="pw_seen_state"):
        _live(tmp_path)


def test_a_pipeline_reading_the_pass_result_directly_marks_its_reviews_as_seen(tmp_path: Path) -> None:
    _collect(tmp_path)
    run_mvp(_write_config(tmp_path, _pw_source()), tmp_path / "out", tmp_path / "out")

    seen = read_json(tmp_path / "state" / "pw-seen-items.json")
    assert len(seen["seen_guids"]) == 2


# -- The weekly review ---------------------------------------------------------


def test_a_pw_review_merges_with_kirkus_and_no_review_text_reaches_state_or_the_review(tmp_path: Path) -> None:
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
                "title": "The Salt Harbours",
                "primary_authors": ["Ada Example"],
                "isbn": "9780306406157",
                "source_url": "https://example.test/kirkus/salt-harbours",
                "signal_strength": "minor mention",
                "candidate_category": "adult nonfiction",
                "evidence_note": "Kirkus nonfiction listing row.",
            }
        ],
    }
    config_path = _write_config(tmp_path, _pw_source(extraction_method="pw-issue-pdf-replay"), kirkus)
    out = tmp_path / "out"

    run_mvp(config_path, out, out)
    render_review(out, out, effective_date=RUN_DATE)

    candidates = {c["title"]: c for c in read_json(out / "scouting-candidates.json")}
    assert set(candidates) == {"The Salt Harbours", "Notes on Nothing"}
    salt = candidates["The Salt Harbours"]
    assert {e["source_name"] for e in salt["source_evidence"]} == {PW_NAME, "Kirkus Fixture"}
    html = (out / "weekly-review.html").read_text(encoding="utf-8")
    # The editor sees the star, the agent and the link to the PW review.
    assert "https://www.publishersweekly.com/9780306406157" in html
    assert "Publishers Weekly ★ starred review (Nonfiction, History)" in html
    assert "agent Bea Agent at Example Literary" in html
    # No review prose reaches canonical state, the review or the editor CSV.
    for path in out.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert not [prose for prose in PROSE if prose in text], path.name


# -- Configuration -------------------------------------------------------------


def test_the_paid_pw_source_is_collected_while_other_paid_sources_stay_skipped() -> None:
    assert is_skipped_source({"access_status": "paid", "contract_family": "pw-issue-pdf"}) is False
    assert is_skipped_source({"access_status": "paid", "contract_family": "kirkus-coming-soon"}) is True
    assert is_skipped_source({"access_status": "blocked", "contract_family": "pw-issue-pdf"}) is True


def test_the_weekly_configs_declare_pw_as_an_established_source_reading_the_pass_result() -> None:
    live = load_validated_config(ROOT / "configs/current-weekly-pilot-live.json")
    replay = load_validated_config(ROOT / "configs/current-weekly-pilot.json")
    live_source = next(source for source in live["sources"] if source["name"] == PW_NAME)
    replay_source = next(source for source in replay["sources"] if source["name"] == PW_NAME)

    for source in (live_source, replay_source):
        assert source["access_status"] == "paid"
        assert source["lifecycle_status"] == "established"
        assert source["contract_family"] == "pw-issue-pdf"
        assert source["pw_excluded_subsections"] == ["Food & Cooking"]
    assert live_source["extraction_method"] == "pw-issue-pdf-live"
    assert replay_source["extraction_method"] == "pw-issue-pdf-replay"
    # The weekly run reads the result the blessed pass writes.
    pass_config = ROOT / "configs/pw-issue-pdf.json"
    declared = read_json(pass_config)
    assert (ROOT / "configs" / live_source["pw_collection_file"]).resolve() == (
        pass_config.parent / declared["collection_file"]
    ).resolve()
    assert declared["name"] == PW_NAME


def test_the_committed_replay_fixture_yields_mentions_without_live_collection() -> None:
    config_path = ROOT / "configs/current-weekly-pilot.json"
    source = next(source for source in load_validated_config(config_path)["sources"] if source["name"] == PW_NAME)

    result = COLLECTOR_REGISTRY["pw-issue-pdf-replay"].collect(config_path, ROOT, source, RUN_DATE)

    # The synthetic fixture's Food & Cooking row is excluded.
    assert [m["title"] for m in result.mentions] == ["An Invented Coastline", "Notes on Nothing in Particular"]


def test_the_finalized_replay_config_keeps_pw_collectable(tmp_path: Path) -> None:
    _collect(tmp_path)
    config_path = _write_config(tmp_path, _pw_source())
    out = tmp_path / "out"

    assert prepare_codex_extraction(config_path, out, effective_date=RUN_DATE) == 0
    assert finalize_codex_run(out, config_path, effective_date=RUN_DATE) == 0

    [replay_source] = read_json(out / "codex-replay-config.json")["sources"]
    assert replay_source["extraction_method"] == "recorded"
    assert replay_source["contract_family"] == "pw-issue-pdf"
    assert is_skipped_source(replay_source) is False


# -- Review findings: coverage caveats and publication dates ------------------


def test_a_failed_issue_in_the_age_window_is_named_as_incomplete_coverage(tmp_path: Path) -> None:
    path = _collect(tmp_path)
    result = read_json(path)
    # The 9 Nov issue failed after 2 Nov was collected; one older failure is outside the window.
    result["skipped_issues"] = [
        {"issue_date": "2026-11-09", "reason_code": "pw_page_unreadable"},
        {"issue_date": "2026-09-28", "reason_code": "pw_issue_date_mismatch"},
    ]
    path.write_text(json.dumps(result), encoding="utf-8")
    config_path = _write_config(tmp_path, _pw_source())
    out = tmp_path / "out"

    assert prepare_codex_extraction(config_path, out, effective_date="2026-11-10") == 0
    assert finalize_codex_run(out, config_path, effective_date="2026-11-10") == 0
    replayed = tmp_path / "replayed"
    run_mvp(out / "codex-replay-config.json", replayed, replayed)

    for runs in (read_json(out / "source-runs.json"), read_json(replayed / "source-runs.json")):
        [run] = [run for run in runs if run["source_name"] == PW_NAME]
        assert run["debug_metadata"]["pw_missing_issues"] == ["2026-11-09"]
        problems = _source_problems_from_capture({"source_runs": [run]})
        assert [problem["status"] for problem in problems] == ["incomplete"]
        assert PW_NAME in source_problem_message(problems)


@pytest.mark.parametrize("queue", ["retry_issues", "untried_issues"])
def test_an_issue_waiting_for_a_retry_or_never_attempted_is_named_too(tmp_path: Path, queue: str) -> None:
    path = _collect(tmp_path)
    result = read_json(path)
    result[queue] = ["2026-10-26"]
    path.write_text(json.dumps(result), encoding="utf-8")

    debug = _live(tmp_path).debug_metadata

    assert debug["pw_missing_issues"] == ["2026-10-26"]
    assert (debug["raw_row_count"], debug["expected_raw_row_count"]) == (1, 2)


def test_a_complete_window_names_no_caveat(tmp_path: Path) -> None:
    _collect(tmp_path)

    debug = _live(tmp_path).debug_metadata

    assert debug["pw_missing_issues"] == []
    assert "incomplete_ranking" not in debug


@pytest.mark.parametrize(
    ("issue_date", "month", "year"),
    [
        ("2027-01-04", "Dec.", 2026),  # a late review across the year boundary
        ("2026-11-02", "Jan.", 2027),
        ("2026-11-02", "Aug.", 2026),
        ("2026-11-02", "July", 2027),
        ("2026-03-02", "Dec.", 2025),
    ],
)
def test_the_publication_year_is_placed_around_the_issue(issue_date: str, month: str, year: int) -> None:
    assert pw_issue_capture.publication_year({"issue_date": issue_date, "publication_month": month}) == year


def test_the_publication_status_follows_the_run_date(tmp_path: Path) -> None:
    row = {"issue_date": "2026-09-07", "publication_month": "Oct."}

    assert pw_issue_capture.publication_status(row, "2026-09-30") == "forthcoming"
    assert pw_issue_capture.publication_status(row, "2026-10-01") == "published"
    assert pw_issue_capture.publication_status(row, "2026-11-04") == "published"
