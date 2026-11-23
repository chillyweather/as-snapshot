"""Issue #854: comma-listed authors are split and credentials stripped at admission."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoscout.author_admission import NO_PERSON_AUTHOR_DROP_REASON
from autoscout.author_admission import admitted_author_people
from autoscout.candidate_identity import resolve_history_match
from autoscout.config import load_validated_config
from autoscout.history_identity_replay import replay_history
from autoscout.identity import mentions_are_identity_match
from autoscout.identity import normalize_author_display
from autoscout.identity import normalized_identity_key
from autoscout.source_collectors import agent_extraction_mentions_from_response
from autoscout.source_collectors import agent_mentions
from autoscout.source_mentions import with_split_author_lists

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "issue-852-bookpage-2026-09-20"


@pytest.mark.parametrize(
    ("raw", "people"),
    [
        # The four Crucial Conversations displays seen across sources.
        (
            "Joseph Grenny, Kerry Patterson, Ron McMillan, and others",
            ["Joseph Grenny", "Kerry Patterson", "Ron McMillan"],
        ),
        ("Joseph Grenny, Kerry Patterson, Ron McMillan", ["Joseph Grenny", "Kerry Patterson", "Ron McMillan"]),
        ("Joseph Grenny, Kerry Patterson, and Ron McMillan", ["Joseph Grenny", "Kerry Patterson", "Ron McMillan"]),
        ("Joseph Grenny et al.", ["Joseph Grenny"]),
        # Corporate co-credit stays as given; the tail is dropped.
        ("Barack Obama, Higher Ground, et al", ["Barack Obama", "Higher Ground"]),
        # Credentials are stripped; a credential alone is nobody.
        ("Robin Blackstone, MD", ["Robin Blackstone"]),
        ("MD", []),
        ("M.D.", []),
        ("Dr. med. Sheila de Liz", ["Sheila de Liz"]),
        # Every German medical prefix form the normalizer accepts.
        ("Dr. med Sheila de Liz", ["Sheila de Liz"]),
        ("Dr med Sheila de Liz", ["Sheila de Liz"]),
        ("Dr med. Sheila de Liz", ["Sheila de Liz"]),
        ("Dr.med Sheila de Liz", ["Sheila de Liz"]),
        ("Dr.med. Sheila de Liz", ["Sheila de Liz"]),
        ("Dr. Medina Smith", ["Medina Smith"]),
        ("Andrew D. Huberman Ph.D.", ["Andrew D. Huberman"]),
        ("Prof. Dr. Ulrike Herrmann", ["Ulrike Herrmann"]),
        # One comma and no tail is the surname-first display.
        ("Bildner, Jim", ["Bildner, Jim"]),
        ("Smith, John et al.", ["Smith, John"]),
        ("Smith, John, MD", ["Smith, John"]),
        # A generational suffix stays with its name.
        ("Connick, Harry, Jr.", ["Connick, Harry, Jr."]),
        ("Ford, Henry, III", ["Ford, Henry, III"]),
        # A suffix in the middle ends a person; it is never a person itself.
        ("John Doe, Jr., Jane Roe", ["John Doe, Jr.", "Jane Roe"]),
        ("Robin Blackstone, MD, Jane Roe", ["Robin Blackstone", "Jane Roe"]),
        # Other contributor tails.
        ("Jane Doe u. a.", ["Jane Doe"]),
        ("María López y otros", ["María López"]),
        ("Mario Rossi e altri", ["Mario Rossi"]),
        ("Иван Петров и др.", ["Иван Петров"]),
        # Spaced slash (#756) still separates people, and names are not reordered.
        ("Hevrøy, Hans Ola / Ngan, John", ["Hevrøy, Hans Ola", "Ngan, John"]),
        ("Drew Barrymore", ["Drew Barrymore"]),
    ],
)
def test_admitted_people(raw: str, people: list[str]) -> None:
    assert admitted_author_people(raw) == people


def test_bildner_still_normalizes_given_name_first() -> None:
    assert normalize_author_display(admitted_author_people("Bildner, Jim")[0]) == "Jim Bildner"


def _key(title: str, authors: list[str]) -> str:
    return normalized_identity_key(with_split_author_lists({"title": title, "primary_authors": authors}))


def test_issue_849_inputs_reach_the_existing_identity_keys() -> None:
    assert (
        _key("Crucial Conversations (Third Edition)", ["Joseph Grenny, Kerry Patterson, Ron McMillan, and others"])
        == "crucial conversations third edition|joseph grenny & kerry patterson & ron mcmillan"
    )
    assert _key("American Health - Who Gets Paid", ["Robin Blackstone", "MD"]) == (
        "american health who gets paid|robin blackstone"
    )
    assert _key("Woman on Fire", ["Dr. med. Sheila de Liz"]) == "woman on fire|sheila de liz"
    assert _key("A Great Book with Barack Obama", ["Barack Obama, Higher Ground, et al"]) == (
        "a great book with barack obama|barack obama & higher ground"
    )


def test_unchanged_authors_return_the_same_mention() -> None:
    mention = {"title": "Book", "primary_authors": ["Ann Author"]}
    assert with_split_author_lists(mention) is mention


def test_replay_reports_records_whose_key_moves() -> None:
    history = [
        {"identity_key": "book|ann author", "title": "Book", "primary_authors": ["Ann Author"]},
        {
            "identity_key": "list|bob two cid three and others ann one",
            "title": "List",
            "primary_authors": ["Ann One, Bob Two, Cid Three, and others"],
        },
        {"identity_key": "bare|nobody"},
    ]
    report = replay_history(history)
    assert report["record_count"] == 3
    assert report["replayed_count"] == 2
    assert report["not_replayable"] == ["bare|nobody"]
    assert [(item["stored_identity_key"], item["replayed_identity_key"]) for item in report["moved"]] == [
        ("list|bob two cid three and others ann one", "list|ann one & bob two & cid three")
    ]


def test_german_medical_prefix_forms_keep_the_identity_key() -> None:
    for display in ("Dr. med Sheila de Liz", "Dr med Sheila de Liz", "Dr.med Sheila de Liz"):
        assert _key("Woman on Fire", [display]) == "woman on fire|sheila de liz"
        # The key the normalizer gave these forms before admission changed.
        assert normalized_identity_key({"title": "Woman on Fire", "primary_authors": [display]}) == (
            "woman on fire|sheila de liz"
        )


def test_a_suffix_is_never_a_shared_contributor() -> None:
    first = with_split_author_lists({"title": "Same", "primary_authors": ["John Doe, Jr., Jane Roe"]})
    second = with_split_author_lists({"title": "Same", "primary_authors": ["Bob Smith, Jr., Tim Jones"]})
    assert not mentions_are_identity_match(first, second)


def test_an_extracted_row_naming_only_a_credential_is_dropped_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = FIXTURES / "codex-capture-bookpage-nonfiction-6c3244f3.json"
    extraction = FIXTURES / "bookpage-nonfiction-6c3244f3-codex-extraction.json"
    config = load_validated_config(ROOT / "configs/current-weekly-pilot.json")
    source = next(source for source in config["sources"] if source["name"] == "BookPage Nonfiction")
    source = {**source, "source_page_fixture_file": str(capture), "extraction_response_file": str(extraction)}
    page = json.loads(capture.read_text(encoding="utf-8"))
    entries = json.loads(extraction.read_text(encoding="utf-8"))["entries"]
    credential_only = {**entries[1], "primary_authors": ["MD"]}

    mentions, dropped = agent_extraction_mentions_from_response(
        page, {"mentions": [credential_only, entries[2]]}, source, "2026-09-20", collector_kind="codex-extraction"
    )
    assert [drop["reason"] for drop in dropped] == [NO_PERSON_AUTHOR_DROP_REASON]
    assert [mention["title"] for mention in mentions] == [entries[2]["title"]]

    monkeypatch.setattr(agent_mentions, "mention_author_exempt", lambda _source: True)
    mentions, dropped = agent_extraction_mentions_from_response(
        page, {"mentions": [credential_only]}, source, "2026-09-20", collector_kind="codex-extraction"
    )
    assert dropped == []
    assert [with_split_author_lists(mention)["primary_authors"] for mention in mentions] == [[]]


def test_an_identical_sighting_without_isbn_or_alias_finds_its_fused_history_record() -> None:
    fused = {
        "identity_key": "crucial conversations third edition|kerry patterson ron mcmillan and others joseph grenny",
        "candidate_id": "candidate-000000000000000000000001",
        "title": "Crucial Conversations (Third Edition)",
        "primary_authors": ["Joseph Grenny, Kerry Patterson, Ron McMillan, and others"],
        "editor_status": "reject",
    }
    sighting = with_split_author_lists(
        {
            "title": "Crucial Conversations (Third Edition)",
            "primary_authors": ["Joseph Grenny, Kerry Patterson, Ron McMillan, and others"],
        }
    )
    sighting["identity_key"] = normalized_identity_key(sighting)
    assert sighting["identity_key"] != fused["identity_key"]

    record, candidate_id = resolve_history_match(sighting, [fused], {"version": 1, "aliases": {}, "candidates": {}})
    assert record is fused
    assert candidate_id == fused["candidate_id"]


def test_a_fused_record_beside_its_clean_twin_resolves_without_ambiguity() -> None:
    clean = {
        "identity_key": "a great book with barack obama|barack obama",
        "title": "A Great Book with Barack Obama",
        "primary_authors": ["Barack Obama"],
    }
    fused = {
        "identity_key": "a great book with barack obama|higher ground et al barack obama",
        "title": "A Great Book with Barack Obama",
        "primary_authors": ["Barack Obama, Higher Ground, et al"],
    }
    sighting = with_split_author_lists(
        {"title": "A Great Book with Barack Obama", "primary_authors": ["Barack Obama, Higher Ground, et al"]}
    )
    sighting["identity_key"] = normalized_identity_key(sighting)
    table = {"version": 1, "aliases": {}, "candidates": {}}

    record, _candidate_id = resolve_history_match(sighting, [clean, fused], table)
    assert record is fused
    plain = {"title": "A Great Book with Barack Obama", "primary_authors": ["Barack Obama"]}
    plain["identity_key"] = normalized_identity_key(plain)
    assert resolve_history_match(plain, [clean, fused], table)[0] is clean


def test_a_fused_sighting_beats_the_metadata_fallback_of_its_clean_twin() -> None:
    clean = {
        "identity_key": "a great book with barack obama|barack obama",
        "candidate_id": "candidate-000000000000000000000001",
        "title": "A Great Book with Barack Obama",
        "primary_authors": ["Barack Obama"],
    }
    fused = {
        "identity_key": "a great book with barack obama|higher ground et al barack obama",
        "candidate_id": "candidate-000000000000000000000002",
        "title": "A Great Book with Barack Obama",
        "primary_authors": ["Barack Obama, Higher Ground, et al"],
    }
    table = {
        "version": 1,
        "aliases": {clean["identity_key"]: clean["candidate_id"], fused["identity_key"]: fused["candidate_id"]},
        "candidates": {
            record["candidate_id"]: {"title": record["title"], "primary_authors": record["primary_authors"]}
            for record in (clean, fused)
        },
    }
    sighting = with_split_author_lists(
        {"title": "A Great Book with Barack Obama", "primary_authors": ["Barack Obama, Higher Ground, et al"]}
    )
    sighting["identity_key"] = normalized_identity_key(sighting)

    assert resolve_history_match(sighting, [clean, fused], table) == (fused, fused["candidate_id"])


def test_a_title_only_sighting_finds_a_record_stored_with_a_credential_as_author() -> None:
    stored = {"identity_key": "book|md", "title": "Book", "primary_authors": ["MD"]}
    sighting = with_split_author_lists({"title": "Book", "primary_authors": ["MD"]})
    sighting["identity_key"] = normalized_identity_key(sighting)
    assert sighting["identity_key"] == "book|"

    record, _candidate_id = resolve_history_match(sighting, [stored], {"version": 1, "aliases": {}, "candidates": {}})
    assert record is stored


EMPTY_TABLE = {"version": 1, "aliases": {}, "candidates": {}}
FUSED_CRUCIAL = "Joseph Grenny, Kerry Patterson, Ron McMillan, and others"


def _sighting(title: str, authors: list[str]) -> dict:
    sighting = with_split_author_lists({"title": title, "primary_authors": authors})
    sighting["identity_key"] = normalized_identity_key(sighting)
    return sighting


def test_a_narrower_sighting_finds_a_record_stored_with_a_fused_author_list() -> None:
    fused = {
        "identity_key": "crucial conversations third edition|kerry patterson ron mcmillan and others joseph grenny",
        "title": "Crucial Conversations (Third Edition)",
        "primary_authors": [FUSED_CRUCIAL],
    }
    sighting = _sighting("Crucial Conversations (Third Edition)", ["Joseph Grenny"])
    assert resolve_history_match(sighting, [fused], EMPTY_TABLE)[0] is fused


def test_two_records_admitting_to_one_key_are_ambiguous_not_a_new_identity() -> None:
    first = {"identity_key": "list|one", "title": "List", "primary_authors": ["Ann One, Bob Two, Cid Three"]}
    second = {
        "identity_key": "list|two",
        "title": "List",
        "primary_authors": ["Ann One, Bob Two, Cid Three, and others"],
    }
    with pytest.raises(ValueError, match="ambiguous candidate history"):
        resolve_history_match(_sighting("List", ["Ann One, Bob Two, Cid Three"]), [first, second], EMPTY_TABLE)

    candidate_id = "candidate-000000000000000000000009"
    table = {
        "version": 1,
        "aliases": {"list|one": candidate_id, "list|two": candidate_id},
        "candidates": {candidate_id: {"preferred_history_key": "list|two"}},
    }
    assert resolve_history_match(_sighting("List", ["Ann One, Bob Two, Cid Three"]), [first, second], table) == (
        second,
        candidate_id,
    )


def test_a_readmitted_match_keeps_the_preferred_history_record() -> None:
    candidate_id = "candidate-000000000000000000000003"
    preferred = {"identity_key": "book|ann one", "title": "Book", "primary_authors": ["Ann One"]}
    fused = {
        "identity_key": "book|bob two cid three ann one",
        "title": "Book",
        "primary_authors": ["Ann One, Bob Two, Cid Three"],
    }
    table = {
        "version": 1,
        "aliases": {"book|ann one": candidate_id, "book|bob two cid three ann one": candidate_id},
        "candidates": {candidate_id: {"preferred_history_key": "book|ann one"}},
    }
    sighting = _sighting("Book", ["Ann One, Bob Two, Cid Three"])
    assert sighting["identity_key"] == "book|ann one & bob two & cid three"
    assert resolve_history_match(sighting, [preferred, fused], table) == (
        preferred,
        candidate_id,
    )
