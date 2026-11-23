"""Regression coverage for issue #533 author apostrophe folding.

Identity matching folded an apostrophe away and author subject evidence did
not, so a name written with a straight apostrophe and the same name written
with a curly one named the same person on one path and two different people on
the other.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import _author_keys
from autoscout.grounded_description import _author_subject_terms
from autoscout.grounded_description import _source_has_target_author_evidence
from autoscout.identity import normalize_author_identity

# The verbatim primary-author values of the committed Candidate History.
#
# Issue #533 asks for the assertion to run over the real history file. Issue
# #475 forbids a test from constructing a path into the moving weekly run
# alias, and that guard wins, so this is a committed snapshot of the real
# values rather than a live read. ./scripts/refresh-issue-533-author-corpus is
# the synchronisation contract: run it when the author set changes and commit
# the result.
AUTHOR_CORPUS = Path(__file__).resolve().parent / "fixtures/issue-533-author-corpus/primary-authors.json"

# Both apostrophe forms are already committed to Candidate History.
STRAIGHT_APOSTROPHE = "'"
CURLY_APOSTROPHE = "’"
CORPUS_NAMES = ("James O'Hanlon", "Susan O’Malley", "M. Bertrand d’ASTORG")


def _authorless_source(subject_text: str) -> FetchedSource:
    """A parsed source with no extracted authors, so subject evidence decides."""
    return FetchedSource(
        url="https://store.example.test/book",
        source_kind="publisher",
        source_family="publisher",
        owner_id="publisher-supplied-copy",
        source_language="en",
        title="It Will Be More Beautiful Than You Could Ever Imagine",
        primary_authors=(),
        subject_text=subject_text,
        checked_date="2026-09-01",
    )


def _target(author: str) -> DurableTarget:
    return DurableTarget(
        candidate_id="issue-533-target",
        title="It Will Be More Beautiful Than You Could Ever Imagine",
        primary_authors=(author,),
        record={"title": "It Will Be More Beautiful Than You Could Ever Imagine", "primary_authors": [author]},
    )


def _corpus_author_names() -> list[str]:
    return list(json.loads(AUTHOR_CORPUS.read_text(encoding="utf-8"))["primary_authors"])


@pytest.mark.parametrize("record_form", [STRAIGHT_APOSTROPHE, CURLY_APOSTROPHE])
@pytest.mark.parametrize("page_form", [STRAIGHT_APOSTROPHE, CURLY_APOSTROPHE])
def test_apostrophe_forms_match_in_both_directions(record_form: str, page_form: str) -> None:
    """Whichever form the record holds, whichever the page publishes, they match."""
    target = _target(f"Susan O{record_form}Malley")
    source = _authorless_source(f"A posthumous collection of work by Susan O{page_form}Malley.")

    assert _source_has_target_author_evidence(target, source)


@pytest.mark.parametrize("name", CORPUS_NAMES)
def test_real_corpus_names_fold_the_same_on_both_paths(name: str) -> None:
    """The three committed names that carry an apostrophe, straight and curly."""
    straight = name.replace(CURLY_APOSTROPHE, STRAIGHT_APOSTROPHE)
    curly = name.replace(STRAIGHT_APOSTROPHE, CURLY_APOSTROPHE)

    assert _author_keys([straight]) == _author_keys([curly])
    assert _author_subject_terms([straight]) == _author_subject_terms([curly])


def test_every_committed_author_agrees_on_punctuation_across_both_paths() -> None:
    """Neither path may care which apostrophe form a committed record happens to hold."""
    names = _corpus_author_names()
    assert len(names) > 500, "the committed corpus should hold the full author set"
    apostrophe_names = [name for name in names if CURLY_APOSTROPHE in name or STRAIGHT_APOSTROPHE in name]
    assert apostrophe_names, "the committed corpus should hold apostrophe-bearing names"

    disagreeing = []
    for name in names:
        straight = name.replace(CURLY_APOSTROPHE, STRAIGHT_APOSTROPHE)
        curly = name.replace(STRAIGHT_APOSTROPHE, CURLY_APOSTROPHE)
        if _author_keys([straight]) != _author_keys([curly]):
            disagreeing.append(("identity", name))
        if _author_subject_terms([straight]) != _author_subject_terms([curly]):
            disagreeing.append(("subject", name))
        # The two paths may differ by the accent strip, and by nothing else.
        if not _author_keys([name]) <= {normalize_author_identity(term) for term in _author_subject_terms([name])}:
            disagreeing.append(("cross-path", name))

    assert disagreeing == []


def test_the_accent_rule_did_not_change() -> None:
    """Identity strips accents. The subject-evidence path must not start doing that."""
    assert _author_keys(["Hélène Giannecchini"]) == frozenset({"helene giannecchini"})
    assert "hélène giannecchini" in _author_subject_terms(["Hélène Giannecchini"])
    assert "helene giannecchini" not in _author_subject_terms(["Hélène Giannecchini"])


def test_folding_is_punctuation_and_case_only() -> None:
    """No stemming, transliteration, or fuzzy widening was introduced."""
    target = _target("Susan O'Malley")

    assert not _source_has_target_author_evidence(target, _authorless_source("Work by Susan O'Mally."))
    assert not _source_has_target_author_evidence(target, _authorless_source("Work by Susan Omalley."))


@pytest.mark.parametrize("record_form", [STRAIGHT_APOSTROPHE, CURLY_APOSTROPHE])
def test_a_different_surname_is_not_evidence_for_an_apostrophe_name(record_form: str) -> None:
    """The prefix of a surname is part of the surname, not punctuation to drop.

    Folding before selecting the surname would leave ``malley`` as the surname
    term, and a page about an unrelated Malley would then ground the candidate.
    """
    target = _target(f"Susan O{record_form}Malley")

    assert not _source_has_target_author_evidence(target, _authorless_source("A study by John Malley."))
    assert not _source_has_target_author_evidence(target, _authorless_source("Edited by Hugo Malley."))
    assert not _source_has_target_author_evidence(target, _authorless_source("A study by John Astorg."))


@pytest.mark.parametrize("name", ["Susan O'Malley", "Susan O’Malley", "M. Bertrand d’ASTORG"])
def test_the_surname_term_keeps_its_prefix(name: str) -> None:
    """The folded surname term is the whole surname, prefix included."""
    terms = _author_subject_terms([name])

    assert "malley" not in terms
    assert "astorg" not in terms
    assert any(" " in term for term in terms)
