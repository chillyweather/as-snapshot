"""Ranks are grounding-only wrappers; ISBN-backed author typos stay advisory (#869)."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from autoscout.grounded_description import OUTCOME_ATTEMPTED_NO_DESCRIPTION
from autoscout.grounded_description import OUTCOME_SINGLE_SOURCE
from autoscout.grounded_description import REASON_AUTHOR_MISMATCH
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import StageArtifact
from autoscout.grounded_description import SynthesisResult
from autoscout.grounded_description import _author_components
from autoscout.grounded_description import _author_keys
from autoscout.grounded_description import _author_subject_terms
from autoscout.grounded_description import _headline_names_target_author
from autoscout.grounded_description import evaluate_grounding_gate
from autoscout.grounded_description import read_stage_artifact
from autoscout.grounded_description import validate_stage_artifact
from autoscout.grounded_description import write_stage_artifact
from autoscout.identity import normalized_identity_key

RANKS = (
    "captain capt colonel col major maj general gen lieutenant lt sergeant sgt admiral adm commander cmdr "
    "professor prof dr capitaine commandant général sergent amiral professeur docteur docteure "
    "capitão capitã coronel tenente sargento almirante comandante professora doutor doutora "
    "capitán capitana mayor teniente profesor profesora doctor doctora "
    "kapitän hauptmann oberst leutnant feldwebel kommandant doktor "
    "kapitein kolonel majoor generaal luitenant admiraal commandeur hoogleraar dokter "
    "capitano colonnello maggiore generale sergente ammiraglio professore professoressa dottore dottoressa dott"
).split()
SUBJECT = "The book examines the history of aviation and the experiences of pilots."
DESCRIPTION = (
    "Книга рассказывает об истории авиации и опыте лётчиков, раскрывая повседневные задачи и трудности полётов. "
    "Автор рассматривает развитие техники, решения экипажей и документальные свидетельства участников, "
    "помогая увидеть связь личного опыта с историей профессии."
)


def source(**changes: Any) -> FetchedSource:
    return replace(
        FetchedSource(
            url="https://one.example/book",
            source_kind="retailer",
            source_family="retailer",
            owner_id="one",
            source_language="en",
            title="The Last Flight",
            primary_authors=("W. Russell Webster",),
            subject_text=SUBJECT,
            checked_date="2026-10-27",
        ),
        **changes,
    )


@pytest.mark.parametrize("rank", sorted(set(RANKS)))
@pytest.mark.parametrize("dot", ["", "."])
def test_leading_rank_matches_without_rank_in_gate_and_evidence(rank: str, dot: str) -> None:
    author = f"{rank}{dot} W Russell Webster"
    target = DurableTarget("last-flight", "The Last Flight", (author,), {})
    assert _author_keys([author]) == frozenset({"w russell webster"})
    assert _author_subject_terms([author]) == frozenset({"w russell webster", "webster"})
    assert evaluate_grounding_gate(target, [source()]).accepted
    # The same matching-only rule applies when the page carries the rank.
    plain = replace(target, primary_authors=("W. Russell Webster",))
    assert evaluate_grounding_gate(plain, [source(primary_authors=(author,))]).accepted
    assert _headline_names_target_author(target, source(title="The Last Flight by W. Russell Webster"))
    assert evaluate_grounding_gate(
        target, [source(primary_authors=(), subject_text="W. Russell Webster flies.")]
    ).accepted


@pytest.mark.parametrize("other", ["Theresa M Webster", "Russ Webster", "Russell Webster"])
def test_rank_does_not_merge_different_people_or_expand_nicknames(other: str) -> None:
    target = DurableTarget("last-flight", "The Last Flight", ("Captain W Russell Webster",), {})
    result = evaluate_grounding_gate(target, [source(primary_authors=(other,))])
    assert not result.accepted
    assert result.reason_code == REASON_AUTHOR_MISMATCH


@pytest.mark.parametrize(
    "name", ["Major", "Captain", "Dr.", "Prof.", "Major Webster", "Dr. Webster", "John Major", "John Captain Webster"]
)
def test_whole_names_surnames_and_short_names_are_untouched(name: str) -> None:
    from autoscout.identity import normalize_author_identity

    assert _author_keys([name]) == frozenset({normalize_author_identity(name)})


@pytest.mark.parametrize("name", ["Webster, Jr.", "Webster PhD", "M. Webster"])
def test_short_name_guard_does_not_change_existing_nonrank_wrapper_handling(name: str) -> None:
    assert _author_keys([name]) == frozenset({"webster"})


def test_multi_author_split_and_durable_identity_remain_unchanged() -> None:
    authors = ["Captain W Russell Webster & Theresa M Webster"]
    assert _author_components(authors[0]) == ["Captain W Russell Webster", "Theresa M Webster"]
    assert _author_keys(authors) == frozenset({"w russell webster", "theresa m webster"})
    record = {"title": "The Last Flight", "primary_authors": ["Captain W Russell Webster"]}
    assert normalized_identity_key(record) == "the last flight|captain w russell webster"
    assert record["primary_authors"] == ["Captain W Russell Webster"]


@pytest.mark.parametrize("author", ["Captain Prof. W Russell Webster", "Webster, Captain W Russell"])
def test_rank_matching_preserves_existing_display_wrappers(author: str) -> None:
    assert _author_keys([author]) == frozenset({"w russell webster"})


def test_author_prefixed_title_uses_matching_only_rank_stripping() -> None:
    target = DurableTarget("last-flight", "The Last Flight", ("Captain W Russell Webster",), {})
    assert evaluate_grounding_gate(target, [source(title="W. Russell Webster: The Last Flight")]).accepted


class Pages:
    def __init__(self, sources: list[FetchedSource]) -> None:
        self.sources = {item.url: item for item in sources}

    def discover(self, candidate: Any, **kwargs: Any) -> list[DiscoveryResult]:
        return [
            DiscoveryResult(item.url, item.source_kind, item.source_family, item.owner_id)
            for item in self.sources.values()
        ]

    def fetch(self, result: DiscoveryResult, **kwargs: Any) -> FetchedSource:
        return self.sources[result.url]

    def synthesize(self, candidate: Any, sources: Any, **kwargs: Any) -> SynthesisResult:
        return SynthesisResult(DESCRIPTION)


def run_stage(record: dict[str, Any], sources: list[FetchedSource]) -> StageArtifact:
    pages = Pages(sources)
    return GroundedDescriptionStage(discovery=pages, fetch=pages, synthesizer=pages, monotonic=lambda: 0.0).run(
        [record], operation_id="issue-869", operation_date="2026-10-27", force_candidates=True
    )


def typo_candidate() -> dict[str, Any]:
    record: dict[str, Any] = {
        "candidate_id": "candidate-869",
        "title": "Como se tornar sobrenatural",
        "primary_authors": ["Joe Dizpenza"],
        "isbn": "9786550470517",
    }
    record["identity_key"] = normalized_identity_key(record)
    return record


@pytest.mark.parametrize("conflicting_peer", [False, True])
def test_typo_advisory_round_trips_for_accepted_and_refused_candidates_without_state_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflicting_peer: bool
) -> None:
    record = typo_candidate()
    before = copy.deepcopy(record)
    proven = source(title=record["title"], primary_authors=("Joe Dispenza",), isbn=record["isbn"])
    sources = [proven]
    if conflicting_peer:
        sources.append(replace(proven, url="https://two.example/book", owner_id="two", isbn=""))
    target = DurableTarget("candidate-869", record["title"], tuple(record["primary_authors"]), record)
    gate_before = evaluate_grounding_gate(target, sources)
    assert gate_before.accepted is (not conflicting_peer)
    artifact = run_stage(record, sources)
    raw = artifact.to_dict()
    outcome = raw["outcomes"][0]
    assert outcome["candidate_id"] == "candidate-869"
    assert outcome["identity_key"] == "como se tornar sobrenatural|joe dizpenza"
    assert outcome["likely_author_typos"] == [
        {"stored_authors": ["Joe Dizpenza"], "printed_authors": ["Joe Dispenza"], "url": proven.url}
    ]
    assert outcome["outcome"] == (OUTCOME_ATTEMPTED_NO_DESCRIPTION if conflicting_peer else OUTCOME_SINGLE_SOURCE)
    if conflicting_peer:
        assert outcome["reason_code"] == gate_before.reason_code
    assert evaluate_grounding_gate(target, sources) == gate_before
    assert record == before
    assert normalized_identity_key(record) == before["identity_key"]
    assert validate_stage_artifact(raw)
    path = tmp_path / "grounded-description-stage.json"
    write_stage_artifact(path, artifact)
    loaded = read_stage_artifact(path)
    assert loaded is not None
    assert loaded.to_dict() == json.loads(path.read_text()) == raw
    assert validate_stage_artifact(loaded)
    # The entire stage result (including gate refusals/provenance) is identical
    # when advisory collection is disabled, apart from the report itself.
    monkeypatch.setattr("autoscout.grounded_description._likely_author_typos", lambda *_: ())
    without_advisory = run_stage(record, sources).to_dict()
    del raw["outcomes"][0]["likely_author_typos"]
    assert without_advisory == raw
    assert record == before


def test_rank_matched_stage_keeps_candidate_id_key_author_display_history_and_aliases() -> None:
    record = {
        "candidate_id": "candidate-rank-869",
        "identity_key": "the last flight|captain w russell webster",
        "title": "The Last Flight",
        "primary_authors": ["Captain W Russell Webster"],
    }
    history = [copy.deepcopy(record)]
    aliases = {"aliases": {}, "candidates": {"candidate-rank-869": {}}}
    before = copy.deepcopy((record, history, aliases))
    pages = Pages([source()])
    artifact = GroundedDescriptionStage(discovery=pages, fetch=pages, synthesizer=pages, monotonic=lambda: 0.0).run(
        [record],
        history=history,
        aliases=aliases,
        operation_id="issue-869",
        operation_date="2026-10-27",
        force_candidates=True,
    )
    outcome = artifact.to_dict()["outcomes"][0]
    assert outcome["outcome"] == OUTCOME_SINGLE_SOURCE
    assert outcome["candidate_id"] == "candidate-rank-869"
    assert outcome["identity_key"] == "the last flight|captain w russell webster"
    assert "likely_author_typos" not in outcome
    assert (record, history, aliases) == before
    assert normalized_identity_key(record) == "the last flight|captain w russell webster"


def test_advisory_retains_each_isbn_proven_page_and_its_printed_authors() -> None:
    record = typo_candidate()
    first = source(title=record["title"], primary_authors=("Joe Dispenza",), isbn=record["isbn"])
    second = replace(first, url="https://two.example/book", owner_id="two", primary_authors=("Dr. Joe Dispenza",))
    outcome = run_stage(record, [first, second]).to_dict()["outcomes"][0]
    assert outcome["likely_author_typos"] == [
        {"stored_authors": ["Joe Dizpenza"], "printed_authors": list(page.primary_authors), "url": page.url}
        for page in [first, second]
    ]


@pytest.mark.parametrize(
    "value",
    [
        None,
        "typo",
        [{}],
        [{"url": "https://one.example/book", "stored_authors": [], "printed_authors": ["Joe Dispenza"]}],
    ],
)
def test_artifact_validator_rejects_malformed_typo_evidence(value: Any) -> None:
    record = typo_candidate()
    raw = run_stage(
        record, [source(title=record["title"], primary_authors=("Joe Dispenza",), isbn=record["isbn"])]
    ).to_dict()
    raw["outcomes"][0]["likely_author_typos"] = value
    assert not validate_stage_artifact(raw)


@pytest.mark.parametrize(
    "case",
    ["no-isbn", "different-isbn", "discovery-only-isbn", "no-printed-authors", "reviewer-byline", "matching-peer"],
)
def test_typo_advisory_requires_isbn_proof_printed_authors_and_no_author_match(case: str) -> None:
    record = typo_candidate()
    proven = source(title=record["title"], primary_authors=("Joe Dispenza",), isbn=record["isbn"])
    sources = [proven]
    if case == "no-isbn":
        sources = [replace(proven, isbn="")]
    elif case == "different-isbn":
        sources = [replace(proven, isbn="9780306406157")]
    elif case == "discovery-only-isbn":
        record["isbn_discovery_only"] = True
    elif case == "no-printed-authors":
        sources = [replace(proven, primary_authors=())]
    elif case == "reviewer-byline":
        sources = [replace(proven, states_book_author=False)]
    else:
        sources.append(
            replace(proven, url="https://two.example/book", owner_id="two", primary_authors=("Joe Dizpenza",))
        )
    outcome = run_stage(record, sources).to_dict()["outcomes"][0]
    assert "likely_author_typos" not in outcome


@pytest.mark.parametrize("stored", ["Dr. Webster PhD", "Dr. Webster, Jr."])
def test_short_rank_guard_keeps_credential_and_suffix_normalization(stored: str) -> None:
    from autoscout.grounded_description import _author_keys

    assert _author_keys((stored,)) == _author_keys(("Dr. Webster",))


def test_reviewer_byline_matching_the_stored_author_does_not_suppress_the_advisory() -> None:
    record = typo_candidate()
    proven = source(title=record["title"], primary_authors=("Joe Dispenza",), isbn=record["isbn"])
    review = replace(
        proven,
        url="https://review.example/book",
        owner_id="review",
        primary_authors=tuple(record["primary_authors"]),
        states_book_author=False,
        isbn="",
    )
    outcome = run_stage(record, [proven, review]).to_dict()["outcomes"][0]
    assert [item["printed_authors"] for item in outcome["likely_author_typos"]] == [["Joe Dispenza"]]
