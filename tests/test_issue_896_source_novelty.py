"""Issue #896: a candidate source's sampled entries are matched against Candidate History."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from state_store_support import seed_state
from state_store_support import state_databases

from autoscout.__main__ import main
from autoscout.source_novelty import MAX_SAMPLE_ENTRIES
from autoscout.source_novelty import SampleError
from autoscout.source_novelty import load_sample
from autoscout.source_novelty import novelty_report

RUN_DATE = date(2026, 11, 1)

HISTORY: list[dict[str, Any]] = [
    {
        "candidate_id": "candidate-a",
        "identity_key": "the painter s fire|zara anishanslin",
        "title": "The Painter's Fire",
        "english_title": "",
        "primary_authors": ["Zara Anishanslin"],
        "isbn": "",
        "editor_status": "interested",
    },
    {
        "candidate_id": "candidate-b",
        "identity_key": "der lange schatten|anna müller",
        "title": "Der lange Schatten",
        "english_title": "The Long Shadow",
        "primary_authors": ["Anna Müller"],
        "isbn": "",
        "editor_status": "reject",
    },
    {
        "candidate_id": "candidate-c",
        "identity_key": "quiet numbers|ben ortiz",
        "title": "Quiet Numbers",
        "english_title": "",
        "primary_authors": ["Ben Ortiz"],
        "isbn": "978-0-00-000000-2",
        "editor_status": "",
    },
]


def _entry(title: str, authors: list[str], **fields: Any) -> dict[str, Any]:
    return {"title": title, "authors": authors, "nonfiction": True, "publication_year": 2026, **fields}


def _sample(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 1,
        "source_name": "Candidate",
        "url": "https://example.test/list",
        "language": "es",
        "entries": entries,
    }


def _classes(entries: list[dict[str, Any]]) -> list[str]:
    return [row["class"] for row in novelty_report(_sample(entries), HISTORY, RUN_DATE)["entries"]]


def test_each_entry_gets_one_class_in_the_closed_order() -> None:
    entries = [
        _entry("", ["Somebody"]),
        _entry("Libro nuevo", ["Lucía Prieto"], isbn="9780000000101"),
        _entry("Libro nuevo", ["Lucía Prieto"], publication_year=2001),
        _entry("The Painter's Fire", ["Zara Anishanslin"], nonfiction=False, publication_year=2001),
        _entry("Una novela", ["Clara Ruiz"], nonfiction=False),
        _entry("La sombra larga", ["Lucía Prieto"], translated_from="de", publication_year=2001),
        _entry("Viejo libro", ["Lucía Prieto"], publication_year=2024),
        _entry("Otro libro", ["Ben Ortiz"]),
    ]
    assert _classes(entries) == [
        "invalid",
        "new",
        "duplicate",
        "known",
        "out_of_scope",
        "translation",
        "stale",
        "known_author",
    ]


def test_last_year_is_not_stale_and_a_missing_year_is_kept() -> None:
    assert _classes([_entry("Libro A", ["Lucía Prieto"], publication_year=2025)]) == ["new"]
    assert _classes([_entry("Libro B", ["Lucía Prieto"], publication_year=None)]) == ["new"]


def test_a_translated_title_matches_through_the_original_or_the_english_title() -> None:
    by_original = _entry("El fuego del pintor", ["Zara Anishanslin"], original_title="The Painter's Fire")
    by_english = _entry("The Long Shadow", ["Anna Müller"])
    assert _classes([by_original, by_english]) == ["known", "known"]


def test_an_isbn_matches_a_different_title() -> None:
    assert _classes([_entry("Números tranquilos", ["B. Ortiz"], isbn="9780000000002")]) == ["known"]


def test_a_legacy_record_is_matched_by_its_identity_key() -> None:
    history = [{"candidate_id": "candidate-l", "identity_key": "how flowers made our world|david george haskell"}]
    report = novelty_report(
        _sample([_entry("How Flowers Made Our World", ["David George Haskell"])]), history, RUN_DATE
    )
    assert report["entries"][0]["class"] == "known"


def test_a_discovery_only_isbn_proves_no_identity() -> None:
    history = [{**HISTORY[2], "isbn_discovery_only": True}]
    report = novelty_report(
        _sample([_entry("Números tranquilos", ["Lucía Prieto"], isbn="9780000000002")]), history, RUN_DATE
    )
    assert report["entries"][0]["class"] == "new"


def test_repeated_works_count_once() -> None:
    entries = [_entry("Libro nuevo", ["Lucía Prieto"]) for _ in range(5)]
    entries.append(_entry("Otro título", ["L. Prieto"], isbn="9780000000101"))
    entries.append(_entry("Edición de bolsillo", ["Somebody Else"], isbn="978-0-00-000010-1"))
    report = novelty_report(_sample(entries), HISTORY, RUN_DATE)
    assert report["counts"]["new"] == 2
    assert report["counts"]["duplicate"] == 5
    assert (report["valid_entry_count"], report["verdict"]) == (2, "weak")


def test_a_duplicate_keeps_the_isbn_it_adds() -> None:
    entries = [
        _entry("Libro nuevo", ["Lucía Prieto"]),
        _entry("Libro nuevo", ["Lucía Prieto"], isbn="9780000000101"),
        _entry("Edición de bolsillo", ["Somebody Else"], isbn="9780000000101"),
    ]
    assert _classes(entries) == ["new", "duplicate", "duplicate"]


@pytest.mark.parametrize("display", ["Zara Anishanslin et al.", "Zara Anishanslin, Bob Jones, Carol Davis"])
def test_a_copied_author_display_is_admitted_into_people(display: str) -> None:
    assert _classes([_entry("The Painter's Fire", [display])]) == ["known"]


def test_the_same_title_by_another_author_is_not_known() -> None:
    assert _classes([_entry("Quiet Numbers", ["Lucía Prieto"])]) == ["new"]


def test_an_original_title_does_not_stand_in_for_the_title() -> None:
    assert _classes([_entry(" ", ["Lucía Prieto"], original_title="A New Book")]) == ["invalid"]


def test_a_placeholder_author_names_nobody() -> None:
    assert _classes([_entry("Libro", ["Unknown"])]) == ["invalid"]


def test_known_entries_report_the_editor_decisions_they_carry() -> None:
    report = novelty_report(
        _sample(
            [
                _entry("The Painter's Fire", ["Zara Anishanslin"]),
                _entry("Der lange Schatten", ["Anna Müller"]),
                _entry("Quiet Numbers", ["Ben Ortiz"]),
            ]
        ),
        HISTORY,
        RUN_DATE,
    )
    assert report["known_editor_decisions"] == {"positive": 1, "already_known": 0, "reject": 1, "undecided": 1}
    assert report["entries"][0]["matches"] == [
        {"candidate_id": "candidate-a", "title": "The Painter's Fire", "editor_status": "interested"}
    ]


@pytest.mark.parametrize(
    ("new", "other", "verdict"),
    [
        (5, 11, "promising"),  # 5 of 16 is 31%
        (5, 12, "weak"),  # 5 of 17 is 29%
        (4, 0, "weak"),
        (0, 3, "no_novelty"),
    ],
)
def test_the_verdict_needs_enough_new_entries_and_a_large_enough_share(new: int, other: int, verdict: str) -> None:
    entries = [_entry(f"Libro nuevo {index}", [f"Autora Nueva{index}"]) for index in range(new)]
    entries += [_entry(f"Novela {index}", ["Clara Ruiz"], nonfiction=False) for index in range(other)]
    entries.append(_entry("", ["Nobody"]))  # invalid entries are outside the share
    report = novelty_report(_sample(entries), HISTORY, RUN_DATE)
    assert report["verdict"] == verdict
    assert report["valid_entry_count"] == new + other


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"version": 2}, "version"),
        ({"extra": True}, "unknown keys"),
        ({"url": " "}, "url"),
        ({"entries": []}, "nonempty"),
        ({"entries": [{"title": "T", "authors": ["A"]}]}, "nonfiction"),
        ({"entries": [{"title": "T", "authors": "A", "nonfiction": True}]}, "authors"),
        ({"entries": [{"title": "T", "authors": ["A"], "nonfiction": True, "publication_year": "2026"}]}, "year"),
        ({"entries": [{"title": "T", "authors": ["A"], "nonfiction": True, "rank": 1}]}, "unknown keys"),
        ({"entries": [_entry("T", ["A"])] * (MAX_SAMPLE_ENTRIES + 1)}, "at most"),
    ],
)
def test_a_malformed_sample_is_refused(tmp_path: Path, change: dict[str, Any], message: str) -> None:
    path = tmp_path / "sample.json"
    path.write_text(json.dumps({**_sample([_entry("T", ["A"])]), **change}), encoding="utf-8")
    with pytest.raises(SampleError, match=message):
        load_sample(path)


def _state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    seed_state(state_dir, candidate_history=HISTORY)
    return state_dir


def _snapshot(root: Path) -> dict[str, bytes]:
    files = {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}
    return {**files, **state_databases()}


def test_the_command_prints_a_deterministic_report_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = _state_dir(tmp_path)
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps(_sample([_entry("Libro nuevo", ["Lucía Prieto"])])), encoding="utf-8")
    before = _snapshot(tmp_path)
    argv = ["source-novelty", "--sample", str(sample), "--state-dir", str(state_dir), "--run-date", "2026-11-01"]

    assert main(argv) == 0
    first = capsys.readouterr().out
    assert main(argv) == 0
    assert capsys.readouterr().out == first
    assert _snapshot(tmp_path) == before
    report = json.loads(first)
    assert (report["status"], report["verdict"], report["counts"]["new"]) == ("success", "weak", 1)


@pytest.mark.parametrize("problem", ["missing_state_dir", "missing_history", "bad_date", "bad_sample"])
def test_input_errors_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str], problem: str) -> None:
    state_dir = _state_dir(tmp_path)
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps(_sample([_entry("T", ["A"])])), encoding="utf-8")
    argv = ["source-novelty", "--sample", str(sample), "--state-dir", str(state_dir), "--run-date", "2026-11-01"]
    if problem == "missing_state_dir":
        argv[4] = str(tmp_path / "absent")
    elif problem == "missing_history":
        seed_state(state_dir, candidate_history=[])
    elif problem == "bad_date":
        argv[6] = "01.11.2026"
    else:
        sample.write_text("{", encoding="utf-8")
    assert main(argv) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "input_error"
