"""Offline coverage for issue #699: enrichment budget goes to new rows first.

Only a new Scouting Candidate reaches the editor as something to act on, so
enrichment eligibility is ordered new candidates first, updated next, and
previously-seen candidates only with budget left over. The change is a
reordering, not a filter: nothing is dropped, and within each band the
existing fairness rules — fresh work before repairs, repairs
fewest-attempts-first then oldest attempt — are untouched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from state_store_support import read_state
from state_store_support import seed_state

from autoscout.description_repair import REPAIR_ATTEMPTS_FIELD
from autoscout.description_repair import record_repair_attempt
from autoscout.enrichment_lifecycle import BAND_NEW
from autoscout.enrichment_lifecycle import BAND_ORDER
from autoscout.enrichment_lifecycle import BAND_PREVIOUSLY_SEEN
from autoscout.enrichment_lifecycle import BAND_UPDATED
from autoscout.enrichment_lifecycle import COVERAGE_MEASURED
from autoscout.enrichment_lifecycle import COVERAGE_UNKNOWN
from autoscout.enrichment_lifecycle import AttemptCoverage
from autoscout.enrichment_lifecycle import band_rank
from autoscout.enrichment_lifecycle import lifecycle_band
from autoscout.enrichment_lifecycle import lifecycle_band_rank
from autoscout.enrichment_lifecycle import lifecycle_yield
from autoscout.enrichment_lifecycle import order_by_lifecycle_band
from autoscout.enrichment_lifecycle import resolve_attempt_coverage
from autoscout.grounded_description import OUTCOME_ATTEMPTED_NO_DESCRIPTION
from autoscout.grounded_description import REASON_FEWER_THAN_TWO
from autoscout.grounded_description import GroundedDescriptionQueue
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import GroundedDescriptionStageService
from autoscout.grounded_description import SynthesisResult
from autoscout.json_io import write_json

OPERATION_DATE = "2026-09-14"
SUBJECT = "memory migration and public life"
THIN_TEXT = "Книга о памяти и общественной жизни."
RICH_TEXT = (
    "Книга о памяти, обществе и общественной жизни маленького города. "
    "Автор соединяет репортаж с историческим контекстом и показывает, "
    "как прошлое формирует настоящее. "
    "Редакционный крючок — тихий повседневный ракурс вместо громких обобщений."
)


# ── Unit: band derivation ─────────────────────────────────────────────


def test_bands_are_ordered_new_then_updated_then_previously_seen() -> None:
    assert BAND_ORDER == (BAND_NEW, BAND_UPDATED, BAND_PREVIOUSLY_SEEN)
    assert band_rank(BAND_NEW) < band_rank(BAND_UPDATED) < band_rank(BAND_PREVIOUSLY_SEEN)


def test_new_delta_is_the_new_band() -> None:
    assert lifecycle_band({"update_delta": "new"}) == BAND_NEW


def test_upgraded_and_changed_are_the_updated_band() -> None:
    assert lifecycle_band({"update_delta": "upgraded"}) == BAND_UPDATED
    assert lifecycle_band({"update_delta": "changed"}) == BAND_UPDATED


def test_unchanged_is_previously_seen() -> None:
    """A re-seen candidate with no material change is nothing to act on."""
    assert lifecycle_band({"update_delta": "unchanged"}) == BAND_PREVIOUSLY_SEEN


def test_missing_or_unknown_delta_is_previously_seen() -> None:
    """A history-only record carries no delta and must not jump the queue."""
    assert lifecycle_band({}) == BAND_PREVIOUSLY_SEEN
    assert lifecycle_band({"update_delta": ""}) == BAND_PREVIOUSLY_SEEN
    assert lifecycle_band({"update_delta": None}) == BAND_PREVIOUSLY_SEEN
    assert lifecycle_band({"update_delta": "resurrected"}) == BAND_PREVIOUSLY_SEEN


def test_delta_label_is_read_case_insensitively() -> None:
    assert lifecycle_band({"update_delta": " New "}) == BAND_NEW


def test_band_rank_sorts_unknown_band_names_last() -> None:
    assert band_rank("nonsense") > band_rank(BAND_PREVIOUSLY_SEEN)


# ── Unit: ordering ────────────────────────────────────────────────────


def _item(name: str, delta: str) -> dict[str, Any]:
    return {"candidate_id": name, "update_delta": delta}


def test_ordering_puts_new_first_then_updated_then_previously_seen() -> None:
    items = [
        _item("old", "unchanged"),
        _item("changed", "changed"),
        _item("fresh", "new"),
        _item("absent", ""),
        _item("upgraded", "upgraded"),
    ]

    ordered = [item["candidate_id"] for item in order_by_lifecycle_band(items)]

    assert ordered == ["fresh", "changed", "upgraded", "old", "absent"]


def test_ordering_is_stable_within_a_band() -> None:
    """Each band keeps the caller's order, so existing fairness survives."""
    items = [_item(f"row-{index}", "new") for index in range(6)]

    assert order_by_lifecycle_band(items) == items


def test_ordering_drops_nothing() -> None:
    items = [_item("a", "new"), _item("b", "unchanged"), _item("c", "changed")]

    assert sorted(item["candidate_id"] for item in order_by_lifecycle_band(items)) == ["a", "b", "c"]


def test_lifecycle_band_rank_matches_band_rank() -> None:
    for delta in ("new", "upgraded", "changed", "unchanged", ""):
        record = {"update_delta": delta}
        assert lifecycle_band_rank(record) == band_rank(lifecycle_band(record))


# ── Unit: yield reporting ─────────────────────────────────────────────


def _entry(band: str, *, attempted: bool, described: bool = False, budget: bool = False) -> dict[str, Any]:
    return {"band": band, "attempted": attempted, "described": described, "not_attempted_budget": budget}


def test_yield_reports_new_and_updated_separately_from_all_rows() -> None:
    summary = lifecycle_yield(
        [
            _entry(BAND_NEW, attempted=True, described=True),
            _entry(BAND_NEW, attempted=True, described=True),
            _entry(BAND_UPDATED, attempted=True, described=False),
            _entry(BAND_PREVIOUSLY_SEEN, attempted=True, described=False),
            _entry(BAND_PREVIOUSLY_SEEN, attempted=True, described=False),
        ]
    )

    assert summary["bands"][BAND_NEW]["yield"] == 1.0
    assert summary["new_and_updated"] == {
        "eligible": 3,
        "attempted": 3,
        "described": 2,
        "not_attempted_budget": 0,
        "yield": round(2 / 3, 4),
    }
    # The all-rows total stays comparable with earlier runs.
    assert summary["all_rows"]["attempted"] == 5
    assert summary["all_rows"]["described"] == 2
    assert summary["all_rows"]["yield"] == 0.4


def test_yield_is_none_when_nothing_was_attempted() -> None:
    """Zero would read as "attempted and failed", which is a different fact."""
    summary = lifecycle_yield([_entry(BAND_NEW, attempted=False, budget=True)])

    assert summary["bands"][BAND_NEW]["yield"] is None
    assert summary["bands"][BAND_NEW]["not_attempted_budget"] == 1
    assert summary["all_rows"]["yield"] is None


def test_yield_reports_every_band_even_when_empty() -> None:
    summary = lifecycle_yield([])

    assert set(summary["bands"]) == set(BAND_ORDER)
    for values in summary["bands"].values():
        assert values == {
            "eligible": 0,
            "attempted": 0,
            "described": 0,
            "not_attempted_budget": 0,
            "yield": None,
        }


def test_yield_files_an_unknown_band_under_previously_seen() -> None:
    summary = lifecycle_yield([_entry("nonsense", attempted=True, described=True)])

    assert summary["bands"][BAND_PREVIOUSLY_SEEN]["described"] == 1


def test_measured_coverage_is_the_default() -> None:
    assert lifecycle_yield([])["attempt_coverage"] == COVERAGE_MEASURED


def test_coverage_vocabulary_is_closed() -> None:
    assert {member.value for member in AttemptCoverage} == {"measured", "unknown"}


def test_an_unrecognised_coverage_label_fails_closed_to_unknown() -> None:
    """A typo must never be able to claim the attempt figures were observed.

    "unknown" only withholds evidence; a wrong "measured" manufactures it,
    so anything not spelled exactly right resolves to unknown.
    """
    for label in ("mesured", "Measured", " measured ", "", "true", None, 1):
        assert resolve_attempt_coverage(label) is AttemptCoverage.UNKNOWN, label
        assert lifecycle_yield([], attempt_coverage=label)["attempt_coverage"] == COVERAGE_UNKNOWN  # type: ignore[arg-type]


def test_the_exact_measured_label_resolves_to_measured() -> None:
    assert resolve_attempt_coverage("measured") is AttemptCoverage.MEASURED
    assert resolve_attempt_coverage(AttemptCoverage.MEASURED) is AttemptCoverage.MEASURED


def test_reported_coverage_is_a_plain_string_for_the_artifact() -> None:
    """The report is serialized JSON, so the value must stay a plain str."""
    reported = lifecycle_yield([])["attempt_coverage"]

    assert type(reported) is str
    assert json.dumps({"attempt_coverage": reported}) == '{"attempt_coverage": "measured"}'


def test_unknown_coverage_reports_eligibility_but_no_attempt_counts() -> None:
    """A stage that never reported real outcomes must claim no attempts."""
    summary = lifecycle_yield(
        [
            _entry(BAND_NEW, attempted=True, described=True),
            _entry(BAND_PREVIOUSLY_SEEN, attempted=True),
        ],
        attempt_coverage=COVERAGE_UNKNOWN,
    )

    assert summary["attempt_coverage"] == COVERAGE_UNKNOWN
    # Eligibility is planned before the stage runs, so it survives.
    assert summary["bands"][BAND_NEW]["eligible"] == 1
    assert summary["all_rows"]["eligible"] == 2
    for block in (summary["all_rows"], summary["new_and_updated"], *summary["bands"].values()):
        assert block["attempted"] is None
        assert block["described"] is None
        assert block["not_attempted_budget"] is None
        assert block["yield"] is None


# ── Queue ordering ────────────────────────────────────────────────────


def _queue_candidate(candidate_id: str, delta: str, *, priority: str = "D", first_seen: str = "2026-09-14") -> dict:
    return {
        "candidate_id": candidate_id,
        "identity_key": f"{candidate_id}|test author",
        "title": candidate_id,
        "primary_authors": ["Test Author"],
        "update_delta": delta,
        "priority_label": priority,
        "first_seen_date": first_seen,
    }


def _worklist_ids(candidates: list[dict[str, Any]], **kwargs: Any) -> list[str]:
    queue = GroundedDescriptionQueue()
    ordered = queue.worklist(
        candidates,
        operation_id="issue-699",
        operation_date=OPERATION_DATE,
        eligible_candidate_ids={str(item["candidate_id"]) for item in candidates},
        **kwargs,
    )
    return [entry.candidate_id for _candidate, entry in ordered]


def test_queue_admits_new_before_previously_seen_despite_older_queue_age() -> None:
    """The stage budget consumes the worklist in order, so order is the fix.

    The previously-seen row wins on every pre-#699 key — better priority
    label and an older first-seen date — and must still sort behind the
    new one.
    """
    candidates = [
        _queue_candidate("candidate-old", "unchanged", priority="A", first_seen="2026-06-01"),
        _queue_candidate("candidate-new", "new", priority="D", first_seen="2026-09-14"),
    ]

    assert _worklist_ids(candidates) == ["candidate-new", "candidate-old"]


def test_queue_orders_all_three_bands(tmp_path: Path) -> None:
    candidates = [
        _queue_candidate("candidate-seen", "unchanged"),
        _queue_candidate("candidate-updated", "changed"),
        _queue_candidate("candidate-new", "new"),
        _queue_candidate("candidate-absent", ""),
    ]

    ordered = _worklist_ids(candidates)

    assert ordered[0] == "candidate-new"
    assert ordered[1] == "candidate-updated"
    assert set(ordered[2:]) == {"candidate-seen", "candidate-absent"}


def test_queue_keeps_queue_age_order_within_a_band() -> None:
    """Banding adds a key; it does not replace the existing queue sort."""
    candidates = [
        _queue_candidate("candidate-younger", "new", priority="D", first_seen="2026-09-14"),
        _queue_candidate("candidate-older", "new", priority="D", first_seen="2026-06-01"),
        _queue_candidate("candidate-priority", "new", priority="A", first_seen="2026-09-14"),
    ]

    assert _worklist_ids(candidates) == [
        "candidate-priority",
        "candidate-older",
        "candidate-younger",
    ]


def test_queue_keeps_repair_fairness_within_a_band() -> None:
    """Repairs stay fewest-attempts-first inside whatever band they are in."""
    candidates = [
        _queue_candidate("candidate-fresh", "new"),
        _queue_candidate("candidate-repair-failed", "new"),
        _queue_candidate("candidate-repair-untouched", "new"),
    ]
    # The planned rank is fairness order: untouched repair before the
    # previously-failed one.
    repair_rank = {"candidate-repair-untouched": 0, "candidate-repair-failed": 1}

    assert _worklist_ids(candidates, repair_rank=repair_rank) == [
        "candidate-fresh",
        "candidate-repair-untouched",
        "candidate-repair-failed",
    ]


def test_queue_puts_a_new_repair_ahead_of_a_previously_seen_fresh_row() -> None:
    """Band beats the fresh-before-repairs rule: band is the outer key."""
    candidates = [
        _queue_candidate("candidate-seen-fresh", "unchanged"),
        _queue_candidate("candidate-new-repair", "new"),
    ]

    ordered = _worklist_ids(candidates, repair_rank={"candidate-new-repair": 0})

    assert ordered == ["candidate-new-repair", "candidate-seen-fresh"]


def test_queue_filters_nothing() -> None:
    candidates = [
        _queue_candidate("candidate-new", "new"),
        _queue_candidate("candidate-seen", "unchanged"),
        _queue_candidate("candidate-updated", "upgraded"),
    ]

    assert sorted(_worklist_ids(candidates)) == [
        "candidate-new",
        "candidate-seen",
        "candidate-updated",
    ]


# ── Weekly integration against the real stage ─────────────────────────


class _Clock:
    def __init__(self, now: list[float]) -> None:
        self._now = now

    def __call__(self) -> float:
        return self._now[0]


class _Discovery:
    """Record pickup order and burn stage time on every candidate."""

    def __init__(self, now: list[float], pickup: list[str]) -> None:
        self._now = now
        self._pickup = pickup

    def discover(self, candidate: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        candidate_id = str(candidate.get("candidate_id", ""))
        self._pickup.append(candidate_id)
        self._now[0] += 8.0
        return [
            {
                "url": f"https://{side}.example.test/{candidate_id}",
                "source_kind": "publisher",
                "source_family": "publisher_copy",
                "owner_id": f"owner-{side}-{candidate_id}",
                "source_language": "en",
                "classification": "classified",
                "title": str(candidate.get("title", "")),
                "primary_authors": list(candidate.get("primary_authors", [])),
            }
            for side in ("one", "two")
        ]


class _Fetch:
    def fetch(self, result: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "url": result.url,
            "source_kind": "publisher",
            "source_family": "publisher_copy",
            "owner_id": result.owner_id,
            "source_language": "en",
            "title": result.title,
            "primary_authors": list(result.primary_authors),
            "subject_text": SUBJECT,
            "subject_key": SUBJECT,
            "checked_date": kwargs.get("checked_date", OPERATION_DATE),
            "classification": "classified",
        }


class _Synthesizer:
    def synthesize(self, _candidate: Any, sources: Any) -> SynthesisResult:
        return SynthesisResult(RICH_TEXT)


def _history_record(candidate_id: str, **extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "candidate_id": candidate_id,
        "identity_key": f"{candidate_id}|test author",
        "title": candidate_id,
        "primary_authors": ["Test Author"],
        "content_description": "",
    }
    record.update(extra)
    return record


def _workspace(
    tmp_path: Path,
    history: list[dict[str, Any]],
    deltas: dict[str, str],
) -> tuple[Path, Path]:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir()
    output_dir.mkdir()
    seed_state(state_dir, candidate_history=history)
    seed_state(state_dir, candidate_visibility=[])
    write_json(
        output_dir / "scouting-candidates.json",
        [
            {
                "candidate_id": record["candidate_id"],
                "identity_key": record["identity_key"],
                "title": record["title"],
                "primary_authors": ["Test Author"],
                "priority_label": record.get("priority_label", "D"),
                "first_seen_date": record.get("first_seen_date", "2026-09-14"),
                "update_delta": deltas[record["candidate_id"]],
            }
            for record in history
        ],
    )
    return state_dir, output_dir


def _service(now: list[float], pickup: list[str], *, stage_budget_seconds: float) -> GroundedDescriptionStageService:
    return GroundedDescriptionStageService(
        GroundedDescriptionStage(
            discovery=_Discovery(now, pickup),  # type: ignore[arg-type]
            fetch=_Fetch(),  # type: ignore[arg-type]
            synthesizer=_Synthesizer(),  # type: ignore[arg-type]
            monotonic=_Clock(now),  # type: ignore[arg-type]
            max_workers=1,
            stage_budget_seconds=stage_budget_seconds,
        )
    )


def test_budget_exhausts_new_candidates_before_starting_previously_seen(tmp_path: Path) -> None:
    """The acceptance criterion, measured against the real stage clock.

    Two new candidates and two previously-seen ones, with a budget that
    admits three. Both new rows must be attempted; a previously-seen row
    absorbs the exhaustion.
    """
    history = [
        _history_record("candidate-seen-one", priority_label="A", first_seen_date="2026-06-01"),
        _history_record("candidate-seen-two", priority_label="A", first_seen_date="2026-06-02"),
        _history_record("candidate-new-one", priority_label="D", first_seen_date="2026-09-14"),
        _history_record("candidate-new-two", priority_label="D", first_seen_date="2026-09-14"),
    ]
    deltas = {
        "candidate-seen-one": "unchanged",
        "candidate-seen-two": "unchanged",
        "candidate-new-one": "new",
        "candidate-new-two": "new",
    }
    state_dir, output_dir = _workspace(tmp_path, history, deltas)
    now = [0.0]
    pickup: list[str] = []

    result = _service(now, pickup, stage_budget_seconds=30.0).enrich(
        output_dir, state_dir, OPERATION_DATE, operation_id="issue-699-budget"
    )

    assert result["status"] == "ok"
    assert pickup[:2] == ["candidate-new-one", "candidate-new-two"]
    assert len(pickup) == 3
    assert pickup[2].startswith("candidate-seen-")
    lifecycle = result["description_stage_report"]["lifecycle"]
    # Every new candidate was attempted before the budget ran out.
    assert lifecycle["bands"][BAND_NEW]["eligible"] == 2
    assert lifecycle["bands"][BAND_NEW]["attempted"] == 2
    assert lifecycle["bands"][BAND_NEW]["not_attempted_budget"] == 0
    assert lifecycle["bands"][BAND_PREVIOUSLY_SEEN]["not_attempted_budget"] == 1


def test_previously_seen_rows_still_run_when_budget_allows(tmp_path: Path) -> None:
    """Reordering, not filtering: nothing is dropped from the queue."""
    history = [
        _history_record("candidate-seen"),
        _history_record("candidate-new"),
    ]
    deltas = {"candidate-seen": "unchanged", "candidate-new": "new"}
    state_dir, output_dir = _workspace(tmp_path, history, deltas)
    now = [0.0]
    pickup: list[str] = []

    result = _service(now, pickup, stage_budget_seconds=900.0).enrich(
        output_dir, state_dir, OPERATION_DATE, operation_id="issue-699-no-filter"
    )

    assert result["status"] == "ok"
    assert pickup == ["candidate-new", "candidate-seen"]
    saved = {record["candidate_id"]: record for record in read_state(state_dir, "candidate_history")}
    assert saved["candidate-seen"]["content_description"] == RICH_TEXT
    assert saved["candidate-new"]["content_description"] == RICH_TEXT
    lifecycle = result["description_stage_report"]["lifecycle"]
    assert lifecycle["all_rows"] == {
        "eligible": 2,
        "attempted": 2,
        "described": 2,
        "not_attempted_budget": 0,
        "yield": 1.0,
    }
    assert lifecycle["new_and_updated"]["described"] == 1


def test_updated_rows_run_ahead_of_previously_seen_rows(tmp_path: Path) -> None:
    history = [
        _history_record("candidate-seen"),
        _history_record("candidate-updated"),
        _history_record("candidate-new"),
    ]
    deltas = {
        "candidate-seen": "unchanged",
        "candidate-updated": "upgraded",
        "candidate-new": "new",
    }
    state_dir, output_dir = _workspace(tmp_path, history, deltas)
    now = [0.0]
    pickup: list[str] = []

    result = _service(now, pickup, stage_budget_seconds=900.0).enrich(
        output_dir, state_dir, OPERATION_DATE, operation_id="issue-699-updated"
    )

    assert result["status"] == "ok"
    assert pickup == ["candidate-new", "candidate-updated", "candidate-seen"]
    lifecycle = result["description_stage_report"]["lifecycle"]
    assert lifecycle["bands"][BAND_UPDATED]["described"] == 1
    assert lifecycle["new_and_updated"]["described"] == 2
    assert lifecycle["all_rows"]["described"] == 3


def test_new_repair_beats_previously_seen_repair_under_budget(tmp_path: Path) -> None:
    """Repair fairness holds inside a band, but band decides between bands.

    The previously-seen repair is untouched and would win the global
    fairness sort; the new repair has already failed once. Band ordering
    must still attempt the new one first, because it is the row the editor
    reads.
    """
    thin = {
        "content_description": THIN_TEXT,
        "description_provenance": {
            "provenance": "grounded",
            "content_description": THIN_TEXT,
            "applied_date": "2026-09-01",
            "evidence_basis": ["https://one.example.test/work"],
        },
    }
    failed_new = _history_record("candidate-new-repair", **thin)
    record_repair_attempt(
        failed_new,
        operation_date="2026-09-01",
        operation_id="op-old",
        outcome=OUTCOME_ATTEMPTED_NO_DESCRIPTION,
        reason_code=REASON_FEWER_THAN_TWO,
        stage="adjudication",
    )
    untouched_seen = _history_record("candidate-seen-repair", **thin)
    history = [untouched_seen, failed_new]
    deltas = {"candidate-seen-repair": "unchanged", "candidate-new-repair": "new"}
    state_dir, output_dir = _workspace(tmp_path, history, deltas)
    now = [0.0]
    pickup: list[str] = []

    result = _service(now, pickup, stage_budget_seconds=12.0).enrich(
        output_dir, state_dir, OPERATION_DATE, operation_id="issue-699-repair-band"
    )

    assert result["status"] == "ok"
    assert pickup == ["candidate-new-repair"]
    saved = {record["candidate_id"]: record for record in read_state(state_dir, "candidate_history")}
    assert saved["candidate-new-repair"]["content_description"] == RICH_TEXT
    # Deferred, not dropped: the untouched repair keeps its clean log.
    assert saved["candidate-seen-repair"]["content_description"] == THIN_TEXT
    assert len(saved["candidate-seen-repair"].get(REPAIR_ATTEMPTS_FIELD, [])) == 0
    assert result["description_stage_report"]["repair"]["deferred_budget"] == 1


def test_a_crashing_stage_reports_unknown_coverage_not_full_attempts(tmp_path: Path) -> None:
    """Review finding on #699: a stage crash must not claim every row ran.

    Recovery synthesizes one failed outcome per eligible candidate, which
    is shaped exactly like a real attempt that found nothing. Counting
    those reported "attempted: 1, not_attempted_budget: 0, yield: 0.0" for
    a stage that crashed before starting anything — the acceptance metric
    inverted. Coverage is unknown instead.
    """

    class _CrashingStage:
        stage_budget_seconds = 900.0
        candidate_envelope_seconds = 90.0
        discovery_budget_seconds = 10.0
        request_inactivity_timeout_seconds = 10.0
        max_workers = 1
        max_fetches = 3

        def run(self, _candidates: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("stage crashed before admitting any candidate")

    history = [_history_record("candidate-new"), _history_record("candidate-seen")]
    deltas = {"candidate-new": "new", "candidate-seen": "unchanged"}
    state_dir, output_dir = _workspace(tmp_path, history, deltas)

    result = GroundedDescriptionStageService(_CrashingStage()).enrich(  # type: ignore[arg-type]
        output_dir, state_dir, OPERATION_DATE, operation_id="issue-699-crash"
    )

    report = result["description_stage_report"]
    assert report["status"] == "failed"
    lifecycle = report["lifecycle"]
    assert lifecycle["attempt_coverage"] == COVERAGE_UNKNOWN
    # Eligibility was planned before the crash, so it is still reported.
    assert lifecycle["bands"][BAND_NEW]["eligible"] == 1
    assert lifecycle["all_rows"]["eligible"] == 2
    # Nothing may claim the new candidate was attempted, or that the budget
    # was not what stopped it.
    assert lifecycle["bands"][BAND_NEW]["attempted"] is None
    assert lifecycle["bands"][BAND_NEW]["not_attempted_budget"] is None
    assert lifecycle["new_and_updated"]["yield"] is None
    assert lifecycle["all_rows"]["yield"] is None


def test_a_successful_run_still_reports_measured_coverage(tmp_path: Path) -> None:
    history = [_history_record("candidate-new")]
    state_dir, output_dir = _workspace(tmp_path, history, {"candidate-new": "new"})
    now = [0.0]

    result = _service(now, [], stage_budget_seconds=900.0).enrich(
        output_dir, state_dir, OPERATION_DATE, operation_id="issue-699-measured"
    )

    lifecycle = result["description_stage_report"]["lifecycle"]
    assert lifecycle["attempt_coverage"] == COVERAGE_MEASURED
    assert lifecycle["bands"][BAND_NEW]["attempted"] == 1


# ── Residual adjudication stage ───────────────────────────────────────


def test_adjudication_orders_work_items_by_band() -> None:
    from autoscout.description_adjudication import _order_work_items_fresh_first_with_repairs

    work_items = [
        {"identity_key": "seen|author"},
        {"identity_key": "new|author"},
        {"identity_key": "updated|author"},
    ]
    bands = {"seen|author": BAND_PREVIOUSLY_SEEN, "new|author": BAND_NEW, "updated|author": BAND_UPDATED}

    ordered = _order_work_items_fresh_first_with_repairs(work_items, [], bands)

    assert [item["identity_key"] for item in ordered] == ["new|author", "updated|author", "seen|author"]


def test_adjudication_keeps_fresh_before_repairs_within_a_band() -> None:
    from autoscout.description_adjudication import _order_work_items_fresh_first_with_repairs

    work_items = [
        {"identity_key": "repair|author", "depth_repair": True},
        {"identity_key": "fresh|author"},
    ]
    bands = {"repair|author": BAND_NEW, "fresh|author": BAND_NEW}

    ordered = _order_work_items_fresh_first_with_repairs(work_items, [], bands)

    assert [item["identity_key"] for item in ordered] == ["fresh|author", "repair|author"]


def test_adjudication_puts_a_new_repair_ahead_of_previously_seen_fresh_work() -> None:
    from autoscout.description_adjudication import _order_work_items_fresh_first_with_repairs

    work_items = [
        {"identity_key": "seen-fresh|author"},
        {"identity_key": "new-repair|author", "depth_repair": True},
    ]
    bands = {"seen-fresh|author": BAND_PREVIOUSLY_SEEN, "new-repair|author": BAND_NEW}

    ordered = _order_work_items_fresh_first_with_repairs(work_items, [], bands)

    assert [item["identity_key"] for item in ordered] == ["new-repair|author", "seen-fresh|author"]


def test_adjudication_without_a_band_map_orders_exactly_as_before() -> None:
    from autoscout.description_adjudication import _order_work_items_fresh_first_with_repairs

    work_items = [
        {"identity_key": "repair|author", "depth_repair": True},
        {"identity_key": "fresh-b|author"},
        {"identity_key": "fresh-a|author"},
    ]

    ordered = _order_work_items_fresh_first_with_repairs(work_items, [])

    assert [item["identity_key"] for item in ordered] == ["fresh-b|author", "fresh-a|author", "repair|author"]


def test_adjudication_reads_bands_from_the_published_candidates(tmp_path: Path) -> None:
    from autoscout.description_adjudication import _lifecycle_bands_by_identity

    write_json(
        tmp_path / "scouting-candidates.json",
        [
            {"identity_key": "new|author", "update_delta": "new"},
            {"identity_key": "changed|author", "update_delta": "changed"},
            {"identity_key": "same|author", "update_delta": "unchanged"},
        ],
    )

    assert _lifecycle_bands_by_identity(tmp_path) == {
        "new|author": BAND_NEW,
        "changed|author": BAND_UPDATED,
        "same|author": BAND_PREVIOUSLY_SEEN,
    }


def test_adjudication_band_map_is_empty_without_a_candidates_snapshot(tmp_path: Path) -> None:
    """A missing snapshot degrades to the pre-#699 order, never a failure."""
    assert _bands(tmp_path) == {}
    write_json(tmp_path / "scouting-candidates.json", {"not": "a list"})
    assert _bands(tmp_path) == {}


def _bands(output_dir: Path) -> dict[str, str]:
    from autoscout.description_adjudication import _lifecycle_bands_by_identity

    return _lifecycle_bands_by_identity(output_dir)
