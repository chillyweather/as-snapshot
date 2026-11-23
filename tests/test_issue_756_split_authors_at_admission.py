"""Issue #756: a spaced-slash author value is admitted as separate people.

#767 taught the description stage to read ``Hevrøy, Hans Ola / Ngan, John``
as two people. The Source Mention itself still carried one fused value, so
the identity key, the author display and every later stage saw one person
(``evig fysen|hans ola ngan john hevrøy``). Admission now splits it, each name
kept as printed.

Candidate History already holds records with the fused form. They are not
rewritten here (that is an identity correction and needs the editor's
confirmation), but a new sighting of the same book must still find them, so
the identity matcher reads a stored spaced slash as separate people too.
"""

from __future__ import annotations

import pytest

from autoscout.candidate_identity import resolve_history_match
from autoscout.identity import mentions_are_identity_match
from autoscout.identity import normalized_contributor_set
from autoscout.identity import normalized_identity_key
from autoscout.identity import split_author_list
from autoscout.source_mentions import SourceMention

SOURCE = {
    "name": "Bokhandlerforeningen Boklista - Generell litteratur voksne",
    "family": "bestseller/ranking",
    "url": "https://bokhandlerforeningen.no/boklista",
    "notes": "Issue #756 fixture source.",
}


@pytest.mark.parametrize(
    ("value", "people"),
    [
        # Author cells of the 2026-10-05 Bokhandlerforeningen capture, as printed.
        ("Hevrøy, Hans Ola / Ngan, John", ["Hevrøy, Hans Ola", "Ngan, John"]),
        ("Ragnar Hovland / Steffen Kverneland", ["Ragnar Hovland", "Steffen Kverneland"]),
        (
            "Mashadi, Vita / Mashadi, Wanda / Jenssen, Tonje",
            ["Mashadi, Vita", "Mashadi, Wanda", "Jenssen, Tonje"],
        ),
        # A slash without spaces on both sides is part of a name.
        ("AC/DC", ["AC/DC"]),
        ("Hans/Ola Hevrøy", ["Hans/Ola Hevrøy"]),
        # A single inverted name keeps its printed order.
        ("Hevrøy, Hans Ola", ["Hevrøy, Hans Ola"]),
        ("", []),
    ],
)
def test_split_author_list(value: str, people: list[str]) -> None:
    assert split_author_list(value) == people


def _collector_mention(*authors: str) -> dict:
    return {
        "title": "Evig fysen",
        "primary_authors": list(authors),
        "source_url": "https://bokhandlerforeningen.no/boklista",
        "signal_type": "bestseller/ranking",
        "signal_strength": "top ranking",
        "evidence_note": "Rank 3 in the bestseller list.",
        "isbn": "9788234016134",
    }


def test_a_fused_value_is_admitted_as_separate_people_in_their_printed_form() -> None:
    mention = SourceMention.from_collector(
        SOURCE, _collector_mention("Hevrøy, Hans Ola / Ngan, John"), "2026-10-16"
    ).to_dict()

    assert mention["primary_authors"] == ["Hevrøy, Hans Ola", "Ngan, John"]
    assert normalized_identity_key(mention) == "evig fysen|hans ola hevrøy & john ngan"


def test_every_admission_path_splits() -> None:
    candidate = {
        "title": "Husdraumar",
        "primary_authors": ["Marit Eikemo / Marita Aarekol"],
        "source_url": "https://bokhandlerforeningen.no/boklista",
    }
    from_candidate = SourceMention.from_candidate(SOURCE, candidate, "2026-10-16").to_dict()

    assert from_candidate["primary_authors"] == ["Marit Eikemo", "Marita Aarekol"]


def test_a_mention_without_a_spaced_slash_is_unchanged() -> None:
    raw = _collector_mention("Øystein Olsen", "Eivind Thomassen")

    mention = SourceMention.from_collector(SOURCE, raw, "2026-10-16").to_dict()

    assert mention["primary_authors"] == ["Øystein Olsen", "Eivind Thomassen"]


def test_the_contributor_set_reads_a_stored_spaced_slash_as_separate_people() -> None:
    stored = {"primary_authors": ["Joan Salvat-Papasseit / Frederic J. Porta"]}

    assert normalized_contributor_set(stored) == {"joan salvat papasseit", "frederic j porta"}


def test_a_new_sighting_without_isbn_still_matches_its_fused_history_record() -> None:
    """*Soldat d'avantguarda* (TodosTusLibros, no ISBN) is stored fused.

    Without the matcher reading the stored slash, the split re-sighting would
    share no author with it and become a second candidate for the same book.
    """
    history_record = {
        "identity_key": "soldat d avantguarda|joan salvat papasseit frederic j porta",
        "title": "Soldat d'avantguarda",
        "primary_authors": ["Joan Salvat-Papasseit / Frederic J. Porta"],
    }
    new_sighting = {
        "identity_key": "soldat d avantguarda|joan salvat papasseit & frederic j porta",
        "title": "Soldat d'avantguarda",
        "primary_authors": ["Joan Salvat-Papasseit", "Frederic J. Porta"],
    }

    assert mentions_are_identity_match(new_sighting, history_record)
    match, _candidate_id = resolve_history_match(new_sighting, [history_record], {})
    assert match is history_record


@pytest.mark.parametrize("hash_seed", ["0", "6", "42"])
def test_a_history_merge_keeps_every_coauthor_of_a_fused_value(hash_seed: str) -> None:
    """Codex review of #756: the migration union split only at ``&`` and took
    one element of a contributor set that a spaced slash now makes larger, so
    a coauthor could vanish depending on set order. Run under several hash
    seeds, because set order is what varied."""
    import json
    import os
    import subprocess
    import sys

    script = (
        "import json\n"
        "from autoscout.candidate_history_migration import _union_primary_authors\n"
        "members = [{'primary_authors': ['Alice']}, {'primary_authors': ['Alice / Bob']}]\n"
        "print(json.dumps(_union_primary_authors(members, 0)))\n"
    )
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    output = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True)

    assert json.loads(output.stdout) == ["Alice", "Bob"]
