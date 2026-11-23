"""Issue #916: the duplicate report also finds contained titles, translated editions, and author disagreements.

Grouping by exact normalized title missed "I'm Glad I'm Still Alive" beside
"Glad I'm Still Alive" (one PEOPLE headline, one book) and never showed a
same-title record whose author is wrong. Same-title records with no shared
surname stay out of the merge suggestions (#855): many are two books.
"""

from __future__ import annotations

from typing import Any

from autoscout.probable_duplicates import POSSIBLE_EDITIONS
from autoscout.probable_duplicates import PROBABLE_DUPLICATE
from autoscout.probable_duplicates import probable_duplicate_pairs

ALIASES: dict[str, Any] = {"version": 1, "aliases": {}, "candidates": {}}


def _record(title: str, authors: list[str], **extra: Any) -> dict[str, Any]:
    key_title = title.lower().replace("’", " ").replace("'", " ")
    return {
        "identity_key": f"{' '.join(key_title.split())}|{' & '.join(a.lower() for a in authors)}",
        "title": title,
        "primary_authors": authors,
        **extra,
    }


def _report(*records: dict[str, Any]) -> dict[str, Any]:
    return probable_duplicate_pairs(list(records), ALIASES)


def _keys(pair: dict[str, Any]) -> set[str]:
    return {record["identity_key"] for record in pair["records"]}


def test_a_title_contained_in_another_by_the_same_author_is_a_probable_duplicate() -> None:
    glad = _record("Glad I'm Still Alive", ["Christopher Lloyd"])
    im_glad = _record("I’m Glad I’m Still Alive", ["Christopher Lloyd"])

    (pair,) = _report(glad, im_glad)["pairs"]

    assert pair["kind"] == PROBABLE_DUPLICATE
    assert "title_containment" in pair["signals"] and "shared_surname" in pair["signals"]
    assert _keys(pair) == {glad["identity_key"], im_glad["identity_key"]}
    assert pair["normalized_title"] == "glad i m still alive"


def test_a_subtitle_suffix_or_prefix_counts_but_a_short_or_middle_title_does_not() -> None:
    full = _record("Träume aus Feuer: Der Alchemist von der Pfaueninsel", ["Florian Illies"])
    short = _record("Träume aus Feuer", ["Florian Illies"])
    two_tokens = _record("The Body", ["Bessel van der Kolk"])
    longer = _record("The Body Keeps the Score", ["Bessel van der Kolk"])
    middle = _record("Keeps the Score of Things", ["Bessel van der Kolk"])

    pairs = _report(full, short, two_tokens, longer, middle)["pairs"]

    assert [_keys(pair) for pair in pairs] == [{full["identity_key"], short["identity_key"]}]


def test_a_contained_title_by_another_author_is_not_paired() -> None:
    report = _report(_record("The Long Way Home", ["Ann Writer"]), _record("The Long Way Home Again", ["Bo Other"]))
    assert report["pairs"] == [] and report["author_disagreements"] == []


def _published(title: str, english: str, authors: list[str], **provenance_changes: Any) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "kind": "sourced_published",
        "mapping": {
            "original_title": title,
            "original_authors": authors,
            "published_title": english,
            "published_authors": authors,
        },
        "research_sources": ["https://example.test/publisher"],
    }
    provenance.update(provenance_changes)
    return _record(
        title,
        authors,
        english_title=english,
        english_title_type="sourced_published",
        english_title_provenance=provenance,
    )


def test_a_sourced_published_english_title_links_a_translated_edition() -> None:
    original = _record("The Infinity Machine", ["Sebastian Mallaby"])
    spanish = _published("La máquina infinita", "The Infinity Machine", ["Sebastian Mallaby"])

    (pair,) = _report(original, spanish)["pairs"]

    assert pair["kind"] == POSSIBLE_EDITIONS and "translated_title" in pair["signals"]


def test_a_published_title_without_complete_evidence_links_nothing() -> None:
    original = _record("The Infinity Machine", ["Sebastian Mallaby"])
    no_sources = _published("La máquina infinita", "The Infinity Machine", ["Sebastian Mallaby"], research_sources=[])
    no_provenance = {**no_sources, "english_title_provenance": None}
    for spanish in (no_sources, no_provenance):
        assert _report(original, spanish)["pairs"] == []


def test_a_published_title_equal_to_its_own_title_links_no_other_work() -> None:
    # Review of #916: the published title must match the other record's title, not its own.
    first = _published("First Work Of Many", "First Work Of Many", ["Ann Writer"])
    second = _record("Second Book Entirely", ["Ann Writer"])
    assert _report(first, second)["pairs"] == []


def test_a_generated_english_rendering_is_not_identity_evidence() -> None:
    original = _record("The Infinity Machine", ["Sebastian Mallaby"])
    spanish = _record(
        "La máquina infinita",
        ["Sebastian Mallaby"],
        english_title="The Infinity Machine",
        english_title_type="generated_rendering",
    )
    assert _report(original, spanish)["pairs"] == []


def test_same_title_without_a_shared_surname_is_listed_apart_and_never_suggested() -> None:
    passmore = _record("Black Arms to Hold You Up", ["Ben Passmore"])
    sugrue = _record("Black Arms to Hold You Up", ["Thomas J. Sugrue"])

    report = _report(passmore, sugrue)

    assert report["pairs"] == [] and report["pair_count"] == 0
    (disagreement,) = report["author_disagreements"]
    assert report["author_disagreement_count"] == 1
    assert disagreement["signals"] == ["no_shared_surname"]
    assert _keys(disagreement) == {passmore["identity_key"], sugrue["identity_key"]}
    assert "correction_draft" not in disagreement


def test_author_disagreements_do_not_change_the_skip_counts() -> None:
    first = _record("Elon Musk", ["Walter Isaacson"], candidate_id="candidate-a")
    second = _record("Elon Musk", ["Ashlee Vance"], candidate_id="candidate-b")
    dismissals = [{"candidate_ids": ["candidate-a", "candidate-b"]}]

    report = probable_duplicate_pairs([first, second], ALIASES, dismissals)

    assert report["author_disagreements"] == []
    assert report["counts"]["skipped_dismissed"] == 0
