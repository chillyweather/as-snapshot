"""Issue #719: each Candidate History record's identity keys are derived once per resolution pass.

Resolving a candidate compares it with every history record. Before #719
every comparison derived the record's identity keys again, so a stage that
resolved N candidates against M records normalised titles and authors N x M
times. The observation is a counting spy on the key derivation, plus the
resolution outcomes, which must not move.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from autoscout import candidate_identity
from autoscout import grounded_description_enrichment
from autoscout.candidate_identity import KeyedHistory
from autoscout.candidate_identity import candidate_id_for
from autoscout.candidate_identity import empty_alias_table
from autoscout.candidate_identity import resolve_history_match
from autoscout.grounded_description import REASON_AMBIGUITY
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import resolve_durable_target
from autoscout.grounded_description_enrichment import _enrichment_candidates
from autoscout.grounded_description_enrichment import _LoadedInputs
from autoscout.grounded_description_enrichment import _plan_eligibility
from autoscout.identity import normalized_identity_key
from autoscout.json_io import serialize_json
from autoscout.pipeline import _reconcile_with_history
from autoscout.pipeline import apply_update_delta_labels
from autoscout.pipeline import suppress_candidates_without_material_change
from autoscout.scouting_candidates import ScoutingCandidate


def _record(title: str, authors: list[str], **fields: Any) -> dict[str, Any]:
    record = {"title": title, "primary_authors": authors}
    return {"identity_key": normalized_identity_key(record), **record, **fields}


def _aliased(records: list[dict[str, Any]], **candidates: dict[str, Any]) -> dict[str, Any]:
    table = empty_alias_table()
    for record in records:
        candidate_id = record.get("candidate_id") or candidate_id_for(record["identity_key"])
        table["aliases"][record["identity_key"]] = candidate_id
        table["candidates"].setdefault(candidate_id, {"candidate_id": candidate_id})
    for candidate_id, metadata in candidates.items():
        table["candidates"][candidate_id] = {"candidate_id": candidate_id, **metadata}
    return table


def _both_inputs(current: dict[str, Any], history: list[dict[str, Any]], table: dict[str, Any]) -> list[Any]:
    """Resolve once from the plain history list and once from a KeyedHistory."""
    return [
        resolve_history_match(dict(current), history, table),
        resolve_history_match(dict(current), KeyedHistory.of(history), table),
    ]


def test_an_exact_alias_resolves_to_its_record() -> None:
    record = _record("Atlas", ["Ann Lee"])
    table = _aliased([record])
    expected = (record, candidate_id_for(record["identity_key"]))
    assert _both_inputs(_record("Atlas", ["Ann Lee"]), [record], table) == [expected, expected]


def test_the_conservative_metadata_fallback_accepts_one_durable_id() -> None:
    record = _record("Atlas", ["Ann Lee"], candidate_id="candidate-atlas")
    table = _aliased([record], **{"candidate-atlas": {"title": "Atlas", "primary_authors": ["Ann Lee"]}})
    current = _record("Atlas", ["Ann Lee", "Bo Chen"])
    expected = (record, "candidate-atlas")
    assert _both_inputs(current, [record], table) == [expected, expected]


def test_a_conflicting_alias_still_refuses_to_resolve() -> None:
    stored = _record("Atlas", ["Ann Lee"], candidate_id="candidate-a")
    computed = _record("Atlas Revised", ["Ann Lee"], candidate_id="candidate-b")
    table = _aliased([stored, computed])
    # The stored key names one durable ID and the recomputed key another.
    current = {**_record("Atlas Revised", ["Ann Lee"]), "identity_key": stored["identity_key"]}
    for history in ([stored, computed], KeyedHistory.of([stored, computed])):
        with pytest.raises(ValueError, match="exact identity key matched multiple records"):
            resolve_history_match(dict(current), history, table)


def test_a_preferred_history_key_selects_among_same_id_records() -> None:
    first = _record("Atlas", ["Ann Lee"], candidate_id="candidate-p")
    second = _record("Atlas Revised", ["Ann Lee"], candidate_id="candidate-p")
    table = _aliased([first, second], **{"candidate-p": {"preferred_history_key": second["identity_key"]}})
    expected = (second, "candidate-p")
    assert _both_inputs(first, [first, second], table) == [expected, expected]


def test_same_id_records_without_a_preferred_key_stay_ambiguous() -> None:
    first = _record("Atlas", ["Ann Lee"], candidate_id="candidate-p")
    second = _record("Atlas Revised", ["Ann Lee"], candidate_id="candidate-p")
    table = _aliased([first, second])
    current = {**_record("Atlas Third", ["Ann Lee"]), "candidate_id": "candidate-p"}
    for history in ([first, second], KeyedHistory.of([first, second])):
        with pytest.raises(ValueError, match="ambiguous candidate history for durable candidate ID candidate-p"):
            resolve_history_match(dict(current), history, table)


def test_legacy_records_without_a_durable_id_resolve_as_before() -> None:
    legacy = _record("Atlas", ["Ann Lee"])
    table = empty_alias_table()
    expected = (legacy, candidate_id_for(legacy["identity_key"]))
    assert _both_inputs(_record("Atlas", ["Ann Lee"]), [legacy], table) == [expected, expected]
    assert _both_inputs(_record("Atlas", ["Ann Lee", "Bo Chen"]), [legacy], table) == [expected, expected]
    assert _both_inputs(_record("Other", ["Cy Park"]), [legacy], table) == [(None, None), (None, None)]


HISTORY_SIZE = 40
CANDIDATE_COUNT = 25


def _history() -> list[dict[str, Any]]:
    return [_record(f"History Book {index}", [f"Author {index}"]) for index in range(HISTORY_SIZE)]


def _candidates() -> list[dict[str, Any]]:
    # Half of the candidates are sightings of a history record; the rest are new.
    return [
        _record(f"History Book {index}" if index % 2 else f"New Book {index}", [f"Author {index}"])
        for index in range(CANDIDATE_COUNT)
    ]


@pytest.fixture
def history_key_derivations(monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict[str, Any]], Counter[int]]:
    """Return the history and a count of key derivations per history record."""
    history = _history()
    history_ids = {id(record) for record in history}
    derivations: Counter[int] = Counter()
    derive = candidate_identity._record_keys

    def counting_record_keys(record: dict[str, Any]) -> list[str]:
        if id(record) in history_ids:
            derivations[id(record)] += 1
        return derive(record)

    monkeypatch.setattr(candidate_identity, "_record_keys", counting_record_keys)
    return history, derivations


def test_one_pass_derives_each_history_record_once(
    history_key_derivations: tuple[list[dict[str, Any]], Counter[int]],
) -> None:
    history, derivations = history_key_derivations
    table = _aliased(history)
    keyed = KeyedHistory.of(history)

    resolved = [resolve_history_match(candidate, keyed, table) for candidate in _candidates()]

    assert sum(1 for record, _id in resolved if record is not None) == CANDIDATE_COUNT // 2
    assert len(derivations) == HISTORY_SIZE
    assert set(derivations.values()) == {1}


@pytest.mark.parametrize(
    "stage",
    [
        lambda candidates, history, table: apply_update_delta_labels(candidates, history, table),
        lambda candidates, history, table: suppress_candidates_without_material_change(candidates, history, table),
        lambda candidates, history, table: _reconcile_with_history(
            [ScoutingCandidate.from_mapping(candidate) for candidate in candidates], history, table
        ),
    ],
    ids=["update-delta", "suppression", "reconciliation"],
)
def test_a_pipeline_stage_derives_each_history_record_once(
    stage: Any, history_key_derivations: tuple[list[dict[str, Any]], Counter[int]]
) -> None:
    history, derivations = history_key_derivations

    stage(_candidates(), history, _aliased(history))

    assert set(derivations.values()) == {1}


@pytest.fixture
def indexed_passes(monkeypatch: pytest.MonkeyPatch) -> tuple[list[KeyedHistory], Counter[int]]:
    indexes: list[KeyedHistory] = []
    derivations: Counter[int] = Counter()
    derive = candidate_identity._record_keys
    build = KeyedHistory.of

    def counting_record_keys(record: dict[str, Any]) -> list[str]:
        derivations[id(record)] += 1
        return derive(record)

    def capture_index(cls: type[KeyedHistory], history: Any) -> KeyedHistory:
        index = build(history)
        indexes.append(index)
        return index

    monkeypatch.setattr(candidate_identity, "_record_keys", counting_record_keys)
    monkeypatch.setattr(KeyedHistory, "of", classmethod(capture_index))
    return indexes, derivations


@pytest.mark.parametrize("planning", [False, True], ids=["enrichment", "hidden-eligibility"])
def test_description_planning_derives_history_keys_once_per_pass(
    planning: bool, tmp_path: Path, indexed_passes: tuple[list[KeyedHistory], Counter[int]]
) -> None:
    history = [{**record, "candidate_id": candidate_id_for(record["identity_key"])} for record in _history()]
    current = [{**record, "candidate_id": candidate_id_for(record["identity_key"])} for record in _candidates()]
    aliases = _aliased(history)
    if planning:
        inputs = _LoadedInputs(
            output_dir=tmp_path,
            state_dir=None,
            current_candidates=current,
            history=history,
            queue_path=tmp_path / "queue.json",
            visibility=[{**record, "visibility": "hidden"} for record in history[:5]],
            aliases=aliases,
            operation_id="op",
            operation_date="2026-10-29",
        )
        candidates, _eligible, skipped, _repairs = _plan_eligibility(inputs)
        assert list(skipped.values()) == ["hidden_candidate"] * 5
    else:
        candidates = _enrichment_candidates(current, history, aliases)

    indexes, derivations = indexed_passes
    assert len(candidates) == HISTORY_SIZE + (CANDIDATE_COUNT + 1) // 2
    assert len(indexes) == (2 if planning else 1)
    for index in indexes:
        assert len(index.records) == HISTORY_SIZE
        assert {derivations[id(record)] for record in index.records} == {1}
    # Enrichment indexes its own copies, whose object identities own the cache.
    assert all(copied is not original for copied, original in zip(indexes[-1].records, history, strict=True))


def test_description_resolution_preserves_alias_fallback_and_unmatched_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exact = _record("Atlas", ["Ann Lee"], candidate_id="candidate-atlas", content_description="History Atlas")
    fallback = _record("Maps", ["Bo Chen"], candidate_id="candidate-maps", content_description="History Maps")
    history = [exact, fallback]
    aliases = _aliased(history, **{"candidate-maps": {"title": "Maps", "primary_authors": ["Bo Chen"]}})
    current = [
        _record("Atlas", ["Ann Lee"], content_description="Current Atlas"),
        _record("Maps", ["Bo Chen", "Cy Park"], content_description="Current Maps"),
        _record("Other", ["Dee Fox"], candidate_id="candidate-other", content_description="Current Other"),
    ]
    before = deepcopy((current, history, aliases))
    keyed = KeyedHistory.of([dict(record) for record in history])
    plain = [resolve_durable_target(record, history=history, aliases=aliases) for record in current]
    indexed = [
        resolve_durable_target(record, history=history, keyed_history=keyed, aliases=aliases) for record in current
    ]
    assert indexed == plain
    assert [result.target.candidate_id if result.target else None for result in indexed] == [
        "candidate-atlas",
        "candidate-maps",
        None,
    ]
    admitted = [
        {**current[0], "candidate_id": "candidate-atlas"},
        {**current[1], "candidate_id": "candidate-maps"},
        current[2],
    ]
    expected = [
        {**admitted[0], "content_description": "History Atlas"},
        {**admitted[1], "identity_key": fallback["identity_key"], "content_description": "History Maps"},
        admitted[2],
    ]
    actual = _enrichment_candidates(admitted, history, aliases)
    assert serialize_json(actual) == serialize_json(expected)

    # Replay the previous raw-list path through the same orchestration.
    def plain_match(record: dict[str, Any], index: KeyedHistory, table: dict[str, Any]) -> Any:
        return resolve_history_match(record, list(index.records), table)

    monkeypatch.setattr(grounded_description_enrichment, "resolve_history_match", plain_match)
    assert serialize_json(_enrichment_candidates(admitted, history, aliases)) == serialize_json(actual)
    assert (current, history, aliases) == before


def test_description_attempts_share_one_isolated_history_index(
    indexed_passes: tuple[list[KeyedHistory], Counter[int]],
) -> None:
    history = [{**record, "candidate_id": candidate_id_for(record["identity_key"])} for record in _history()]
    # Unkeyed history rows still constrain the known-ID guard.
    history.append({"candidate_id": "unkeyed-history-id"})
    current = [dict(record) for record in history[:3]]
    before = deepcopy(history)

    class Discovery:
        def discover(self, candidate: Any, **_kwargs: Any) -> list[DiscoveryResult]:
            return [
                DiscoveryResult(
                    f"https://review.example.test/{candidate['candidate_id']}",
                    "review",
                    "review",
                    "review",
                    title=candidate["title"],
                    primary_authors=tuple(candidate["primary_authors"]),
                )
            ]

    class Fetch:
        def fetch(self, result: DiscoveryResult, **_kwargs: Any) -> dict[str, Any]:
            return {
                "url": result.url,
                "source_kind": "review",
                "source_family": "review",
                "owner_id": "review",
                "title": result.title,
                "primary_authors": list(result.primary_authors),
                "subject_text": "An account of historical maps and their makers.",
                "checked_date": "2026-10-29",
            }

    artifact = GroundedDescriptionStage(discovery=Discovery(), fetch=Fetch(), monotonic=lambda: 0.0).run(
        current,
        history=history,
        aliases=_aliased(history[:-1]),
        force_candidates=True,
        operation_id="op",
        operation_date="2026-10-29",
    )

    assert len(artifact.outcomes) == len(current)
    assert all(outcome.research_sources for outcome in artifact.outcomes)
    indexes, derivations = indexed_passes
    assert len(indexes) == 1
    assert len(indexes[0].records) == HISTORY_SIZE
    assert {derivations[id(record)] for record in indexes[0].records} == {1}
    assert all(copied is not original for copied, original in zip(indexes[0].records, history[:-1], strict=True))
    assert history == before


@pytest.mark.parametrize("invalid_aliases", [False, True], ids=["ambiguous-history", "invalid-aliases"])
def test_indexed_durable_resolution_preserves_ambiguity_handling(invalid_aliases: bool) -> None:
    history = [
        _record("Atlas", ["Ann Lee"], candidate_id="candidate-atlas"),
        _record("Atlas Revised", ["Ann Lee"], candidate_id="candidate-atlas"),
    ]
    current = _record("Atlas Third", ["Ann Lee"], candidate_id="candidate-atlas")
    aliases = {"aliases": None, "candidates": {}} if invalid_aliases else _aliased(history)
    plain = resolve_durable_target(current, history=history, aliases=aliases)
    indexed = resolve_durable_target(current, history=history, keyed_history=KeyedHistory.of(history), aliases=aliases)
    assert indexed == plain
    assert indexed.target is None
    assert indexed.reason_code == REASON_AMBIGUITY


def test_indexed_durable_resolution_keeps_unkeyed_history_in_the_known_id_guard() -> None:
    current = _record("Other", ["Dee Fox"], candidate_id="candidate-other")
    history = [{"candidate_id": "unkeyed-history-id"}]
    plain = resolve_durable_target(current, history=history)
    indexed = resolve_durable_target(current, history=history, keyed_history=KeyedHistory.of(history))
    assert indexed == plain
    assert indexed.target is None
    assert indexed.reason_code == REASON_AMBIGUITY
