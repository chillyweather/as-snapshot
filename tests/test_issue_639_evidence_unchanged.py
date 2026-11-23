"""Offline coverage for issue #639: skip re-synthesis when cited evidence is unchanged.

The adjudication stage runs every week over the same Candidate History. A
candidate whose retained evidence digests still match the freshly fetched
pages reports the existing ``evidence_unchanged`` outcome: the agent is not
called and the text is not changed. A changed page, or a page that can no
longer be fetched, makes the candidate eligible again. This follows the rule
issues #548 and #549 settled for the deterministic stage, reusing the same
outcome name and count, and it keeps that rule's two escapes: the editor can
name a candidate to force a fresh look, and retained evidence goes stale on
the same re-verification cadence, so nothing is skipped for ever. Every test
is offline: a fake transport serves small HTML pages to the real
``BoundedHTTPFetcher``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from adjudication_test_support import AUTHOR
from adjudication_test_support import OPERATION_DATE
from adjudication_test_support import SUBJECT
from adjudication_test_support import VALID_TEXT
from adjudication_test_support import citation as _citation
from adjudication_test_support import fetcher as _fetcher
from adjudication_test_support import page as _page
from adjudication_test_support import record as _record
from adjudication_test_support import response as _response
from adjudication_test_support import workspace as _workspace
from state_store_support import read_state
from state_store_support import state_snapshot

from autoscout.description_adjudication import ADJUDICATION_PREVIEW_ARTIFACT_NAME
from autoscout.description_adjudication import ADJUDICATION_PROVENANCE
from autoscout.description_adjudication import ReplayDescriptionAdjudicationAdapter
from autoscout.description_adjudication import _adjudication_evidence_unchanged
from autoscout.description_adjudication import adjudicate_descriptions
from autoscout.description_adjudication import adjudication_evidence_digest
from autoscout.generate_descriptions import GROUNDED_REVERIFY_AGE_DAYS
from autoscout.grounded_description import MAX_FETCHES_PER_CANDIDATE
from autoscout.grounded_description import OUTCOME_EVIDENCE_UNCHANGED
from autoscout.grounded_description import REASON_CODES
from autoscout.grounded_description import REASON_EVIDENCE_UNCHANGED
from autoscout.grounded_description import SourceClassifier
from autoscout.grounded_description import canonicalize_url
from autoscout.grounded_description import reason_label
from autoscout.json_io import read_json

IDENTITY_KEY = "work one|alex author"

CHANGED_SUBJECT = "A completely rewritten account of memory and public life."

PUBLISHER_URL = "https://publisher-example.test/work-one"
CATALOGUE_URL = "https://catalog-example.test/work-one"
PRESS_URL = "https://press-example.test/work-one"
REVIEW_URL = "https://review-example.test/work-one"


def _citations() -> list[dict[str, Any]]:
    return [
        _citation(PUBLISHER_URL, "publisher-example.test", "publisher"),
        _citation(CATALOGUE_URL, "catalog-example.test", "library_catalogue"),
    ]


def _applied_response(identity_key: str) -> dict[str, Any]:
    return _response(identity_key, _citations())


def _unresolved_response(identity_key: str) -> dict[str, Any]:
    return _response(
        identity_key,
        _citations(),
        resolved=False,
        unresolved_reason="pages disagree on the work identity",
    )


def _pages() -> dict[str, str | None]:
    return {canonicalize_url(PUBLISHER_URL): _page(), canonicalize_url(CATALOGUE_URL): _page()}


def _adjudicate(
    state_dir: Path,
    output_dir: Path,
    response_body: dict[str, Any],
    pages: dict[str, str | None],
    **kwargs: Any,
) -> tuple[ReplayDescriptionAdjudicationAdapter, dict[str, Any]]:
    adapter = ReplayDescriptionAdjudicationAdapter({IDENTITY_KEY: response_body})
    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=adapter,
        fetcher=_fetcher(pages),
        apply=True,
        **({"effective_date": OPERATION_DATE} | kwargs),
    )
    return adapter, result


def test_second_run_reports_evidence_unchanged_without_calling_the_agent(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path, [_record("candidate-001", IDENTITY_KEY)])
    first_adapter, first = _adjudicate(state_dir, output_dir, _unresolved_response("a"), _pages())

    assert first_adapter.calls == [IDENTITY_KEY]
    assert first["fallback"] == 1
    retained = read_state(state_dir, "candidate_history")[0]["description_provenance"]
    assert retained["evidence_basis"] == [canonicalize_url(PUBLISHER_URL), canonicalize_url(CATALOGUE_URL)]
    assert retained["evidence_digests"] == [
        adjudication_evidence_digest(SUBJECT),
        adjudication_evidence_digest(SUBJECT),
    ]
    history_after_first = state_snapshot(state_dir)

    second_adapter, second = _adjudicate(state_dir, output_dir, _unresolved_response("b"), _pages())

    assert second_adapter.calls == []
    assert second["evidence_unchanged"] == 1
    assert second["applied"] == 0
    preview = read_json(output_dir / ADJUDICATION_PREVIEW_ARTIFACT_NAME)
    assert preview["counts"]["evidence_unchanged"] == 1
    (row,) = preview["preview"]
    assert row["decision"] == OUTCOME_EVIDENCE_UNCHANGED == "evidence_unchanged"
    assert row["decision_reason"] == REASON_EVIDENCE_UNCHANGED
    # The text and its provenance survive the second run byte for byte.
    assert state_snapshot(state_dir) == history_after_first
    assert read_state(state_dir, "candidate_history")[0]["content_description"] == retained["content_description"]


def test_changed_cited_page_makes_the_candidate_eligible_again(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path, [_record("candidate-001", IDENTITY_KEY)])
    _adjudicate(state_dir, output_dir, _unresolved_response("a"), _pages())

    changed_pages: dict[str, str | None] = {
        canonicalize_url(PUBLISHER_URL): _page(subject=CHANGED_SUBJECT),
        canonicalize_url(CATALOGUE_URL): _page(),
    }
    second_adapter, second = _adjudicate(state_dir, output_dir, _unresolved_response("b"), changed_pages)

    assert second_adapter.calls == [IDENTITY_KEY]
    assert second["evidence_unchanged"] == 0
    retained = read_state(state_dir, "candidate_history")[0]["description_provenance"]
    assert retained["evidence_digests"] == [
        adjudication_evidence_digest(CHANGED_SUBJECT),
        adjudication_evidence_digest(SUBJECT),
    ]


def test_unfetchable_cited_page_makes_the_candidate_eligible_again(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path, [_record("candidate-001", IDENTITY_KEY)])
    _adjudicate(state_dir, output_dir, _unresolved_response("a"), _pages())

    dead_pages: dict[str, str | None] = {
        canonicalize_url(PUBLISHER_URL): None,
        canonicalize_url(CATALOGUE_URL): _page(),
    }
    second_adapter, second = _adjudicate(state_dir, output_dir, _unresolved_response("b"), dead_pages)

    assert second_adapter.calls == [IDENTITY_KEY]
    assert second["evidence_unchanged"] == 0


def test_evidence_older_than_the_reverify_cadence_is_adjudicated_again(tmp_path: Path) -> None:
    # The deterministic stage re-verifies a grounded candidate about monthly
    # rather than trusting one stored comparison for ever. Adjudication
    # follows the same cadence, so a candidate the agent once rejected is
    # reconsidered after the stage and its policy file have moved on.
    state_dir, output_dir = _workspace(tmp_path, [_record("candidate-001", IDENTITY_KEY)])
    _adjudicate(state_dir, output_dir, _unresolved_response("a"), _pages(), effective_date="2026-09-14")

    fresh_adapter, fresh = _adjudicate(
        state_dir, output_dir, _unresolved_response("b"), _pages(), effective_date="2026-10-01"
    )
    assert fresh_adapter.calls == []
    assert fresh["evidence_unchanged"] == 1

    stale_adapter, stale = _adjudicate(
        state_dir, output_dir, _unresolved_response("c"), _pages(), effective_date="2026-10-15"
    )
    # 2026-10-01 is 17 days after the first run and still fresh; 2026-10-15
    # is 31 days after it and past the cadence.
    assert GROUNDED_REVERIFY_AGE_DAYS == 30
    assert stale_adapter.calls == [IDENTITY_KEY]
    assert stale["evidence_unchanged"] == 0


def test_named_candidate_is_adjudicated_again_even_when_unchanged(tmp_path: Path) -> None:
    # Naming a candidate forces a fresh look, exactly as ``_force_synthesis``
    # does in the deterministic stage, so an editor is never told to wait.
    state_dir, output_dir = _workspace(tmp_path, [_record("candidate-001", IDENTITY_KEY)])
    _adjudicate(state_dir, output_dir, _unresolved_response("a"), _pages())

    named_adapter, named = _adjudicate(state_dir, output_dir, _unresolved_response("b"), _pages(), only=[IDENTITY_KEY])

    assert named_adapter.calls == [IDENTITY_KEY]
    assert named["evidence_unchanged"] == 0


def test_more_retained_pages_than_the_fetch_budget_stays_eligible(tmp_path: Path) -> None:
    # The gate exists to save the run work, so it never spends more fetches
    # on one candidate than the stage itself may spend.
    assert MAX_FETCHES_PER_CANDIDATE == 3
    citations = [
        _citation(PUBLISHER_URL, "publisher-example.test", "library_catalogue"),
        _citation(CATALOGUE_URL, "catalog-example.test", "library_catalogue"),
        _citation(PRESS_URL, "press-example.test", "press"),
        _citation(REVIEW_URL, "review-example.test", "trade_review"),
    ]
    pages: dict[str, str | None] = {
        canonicalize_url(url): _page() for url in (PUBLISHER_URL, CATALOGUE_URL, PRESS_URL, REVIEW_URL)
    }
    state_dir, output_dir = _workspace(tmp_path, [_record("candidate-001", IDENTITY_KEY)])
    unresolved = _response(
        IDENTITY_KEY, citations, resolved=False, unresolved_reason="pages disagree on the work identity"
    )
    _adjudicate(state_dir, output_dir, unresolved, pages)
    retained = read_state(state_dir, "candidate_history")[0]["description_provenance"]
    assert len(retained["evidence_digests"]) == 4

    second_adapter, second = _adjudicate(state_dir, output_dir, unresolved, pages)

    assert second_adapter.calls == [IDENTITY_KEY]
    assert second["evidence_unchanged"] == 0


def test_the_gate_never_skips_a_record_that_kept_a_valid_description() -> None:
    # A record with a valid description reaches the work list only when a
    # repair is due. A pending repair must run, so the gate stands in front
    # of no such record, whatever its digests say.
    record = _record("candidate-001", IDENTITY_KEY, description=VALID_TEXT)
    record["description_provenance"] = {
        "identity_key": IDENTITY_KEY,
        "content_description": VALID_TEXT,
        "provenance": ADJUDICATION_PROVENANCE,
        "applied_date": OPERATION_DATE,
        "evidence_basis": [canonicalize_url(PUBLISHER_URL)],
        "evidence_digests": [adjudication_evidence_digest(SUBJECT)],
    }
    unchanged, urls = _adjudication_evidence_unchanged(
        record,
        fetcher=_fetcher({canonicalize_url(PUBLISHER_URL): _page()}),
        classifier=SourceClassifier(),
        checked_date=OPERATION_DATE,
    )

    assert (unchanged, urls) == (False, [])


def test_existing_outcome_and_count_are_reused_with_no_new_outcome_name(tmp_path: Path) -> None:
    assert OUTCOME_EVIDENCE_UNCHANGED == "evidence_unchanged"
    assert REASON_EVIDENCE_UNCHANGED in REASON_CODES
    assert reason_label(REASON_EVIDENCE_UNCHANGED) == "Описание уже подтверждено теми же источниками"

    state_dir, output_dir = _workspace(tmp_path, [_record("candidate-001", IDENTITY_KEY)])
    _adjudicate(state_dir, output_dir, _unresolved_response("a"), _pages())
    _adapter, result = _adjudicate(state_dir, output_dir, _unresolved_response("b"), _pages())

    assert result["evidence_unchanged"] == 1
    preview = read_json(output_dir / ADJUDICATION_PREVIEW_ARTIFACT_NAME)
    assert preview["counts"]["evidence_unchanged"] == 1
    (row,) = preview["preview"]
    assert row["decision"] == "evidence_unchanged"


def test_record_without_retained_digests_stays_eligible(tmp_path: Path) -> None:
    legacy = _record("candidate-001", IDENTITY_KEY)
    legacy["content_description"] = "Описание требует дополнительной проверки"
    legacy["description_provenance"] = {
        "identity_key": IDENTITY_KEY,
        "content_description": "Описание требует дополнительной проверки",
        "provenance": "fallback",
        "applied_date": "2026-09-01",
        "evidence_basis": [],
    }
    state_dir, output_dir = _workspace(tmp_path, [legacy])

    adapter, _result = _adjudicate(state_dir, output_dir, _unresolved_response("a"), _pages())

    assert adapter.calls == [IDENTITY_KEY]


def test_approved_description_survives_the_next_run_unchanged(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path, [_record("candidate-001", IDENTITY_KEY)])
    _first_adapter, first = _adjudicate(state_dir, output_dir, _applied_response("a"), _pages())
    assert first["applied"] == 1
    history_after_first = state_snapshot(state_dir)

    second_adapter, second = _adjudicate(state_dir, output_dir, _applied_response("b"), _pages())

    # An applied description never re-enters the work list, so the agent is
    # not called and the text survives without the gate being consulted.
    assert second_adapter.calls == []
    assert state_snapshot(state_dir) == history_after_first
    assert read_state(state_dir, "candidate_history")[0]["content_description"] == VALID_TEXT
    assert second["applied"] == 0
    assert second["evidence_unchanged"] == 0


def test_two_runs_from_one_state_write_byte_identical_artifacts(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path, [_record("candidate-001", IDENTITY_KEY)])
    kwargs: dict[str, Any] = {
        "effective_date": OPERATION_DATE,
        "adapter": ReplayDescriptionAdjudicationAdapter({IDENTITY_KEY: _unresolved_response("a")}),
        "fetcher": _fetcher(_pages()),
    }

    adjudicate_descriptions(state_dir, output_dir, **kwargs)
    first_preview = (output_dir / ADJUDICATION_PREVIEW_ARTIFACT_NAME).read_bytes()
    adjudicate_descriptions(state_dir, output_dir, **kwargs)
    second_preview = (output_dir / ADJUDICATION_PREVIEW_ARTIFACT_NAME).read_bytes()

    assert first_preview == second_preview
    assert AUTHOR in read_state(state_dir, "candidate_history")[0]["primary_authors"]
