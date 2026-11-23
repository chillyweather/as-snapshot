"""Topic term forms (#966) and exception terms (#967).

A word ending in ``*`` matches the inflected forms of a Russian word, a term in
double quotes matches adjacent words in order, and exception terms keep a
matching candidate outside a topic entry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.editorial_taste_profile import TasteProfileError
from autoscout.editorial_taste_profile import _candidate_matches_soft_preference
from autoscout.editorial_taste_profile import add_profile_entry
from autoscout.editorial_taste_profile import editorial_taste_profile_effects
from autoscout.editorial_taste_profile import hide_candidates_by_rules
from autoscout.editorial_taste_profile import list_profile_entries
from autoscout.editorial_taste_profile import matching_metadata_field
from autoscout.editorial_taste_profile import normalize_topic_term
from autoscout.editorial_taste_profile import save_taste_profile
from autoscout.editorial_taste_profile import set_profile_entry_terms
from autoscout.editorial_taste_profile import topic_term_error
from autoscout.editorial_taste_profile import validate_taste_profile
from autoscout.scouting_candidates import ScoutingCandidate

# Russian descriptions of books the editor rejected by hand because the
# exact-word terms of taste-0012 missed them (#966).
MISSED_LGBT_DESCRIPTIONS = {
    "A audácia dos invertidos": "История ЛГБТИ+ движения в Бразилии и стратегий выживания.",
    "La batalla por el cuerpo": "Полемика о том, как трансгендерные права меняют медицину.",
    "Finding Renée Richards": "Биография первой трансгендерной теннисистки.",
    "The Intermediaries": "Портрет веймарского Берлина и людей, которых называли гомосексуальным меньшинством.",
    "William Blake and the Sea Monsters of Love": "Прочтение Блейка через оптику квирного желания.",
}
LGBT_TERMS = ["квир*", "лгбт*", "гомосексуал*", "трансгендер*"]

COOKBOOK_TERMS = ["рецепт*", "кулинарная книга", "cookbook*"]
COOKBOOK_EXCEPTIONS = ['"истори* кухн*"', '"food history"', "рецептур*"]


def _candidate(title: str, description: str = "", **data: object) -> ScoutingCandidate:
    return ScoutingCandidate(
        {
            "title": title,
            "primary_authors": ["Fixture Author"],
            "priority_label": "C",
            "content_description": description,
            "short_description": "",
            "candidate_category": "",
            "english_title": "",
            "english_annotation": "",
            "source_evidence": [],
            **data,
        }
    )


def _entry(
    ident: str = "taste-0001",
    kind: str = "hard_rule",
    matching_terms: list[str] | None = None,
    exception_terms: list[str] | None = None,
) -> dict:
    entry: dict = {
        "id": ident,
        "kind": kind,
        "scope": "topic",
        "subject": "fixture topic",
        "verbatim_quote": f"Quote for {ident}.",
        "provenance": "issue 966/967 fixture",
        "created_at": "2026-11-05",
        "created_by": "test",
        "status": "active",
        "matching_terms": matching_terms if matching_terms is not None else ["fixture"],
    }
    if kind == "soft_preference":
        entry["direction"] = "negative"
    if exception_terms is not None:
        entry["exception_terms"] = exception_terms
    return entry


def _hidden_titles(candidates: list[ScoutingCandidate], *entries: dict) -> set[str]:
    _visible, hidden = hide_candidates_by_rules(candidates, {"entries": list(entries)})
    return {record["title"] for record in hidden}


def _write_latest(state_dir: Path, candidates: list[ScoutingCandidate]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    seed_state(state_dir, latest_scouting_candidates=[candidate.data for candidate in candidates])


def _profile_bytes(state_dir: Path) -> bytes:
    return state_snapshot(state_dir)


# ── #966: prefix terms ──


def test_prefix_terms_hide_the_books_the_exact_terms_missed() -> None:
    candidates = [_candidate(title, text) for title, text in MISSED_LGBT_DESCRIPTIONS.items()]

    assert _hidden_titles(candidates, _entry(matching_terms=["квир", "лгбт", "гомосексуальность"])) == set()
    assert _hidden_titles(candidates, _entry(matching_terms=LGBT_TERMS)) == set(MISSED_LGBT_DESCRIPTIONS)


def test_a_plain_term_still_matches_whole_words_only() -> None:
    entry = _entry(matching_terms=["fiction"])

    assert _hidden_titles([_candidate("A", "", candidate_category="fictional worlds")], entry) == set()
    assert _hidden_titles([_candidate("B", "", candidate_category="fiction")], entry) == {"B"}
    assert _hidden_titles([_candidate("C", "кулинарной книги")], _entry(matching_terms=["кулинарная"])) == set()


def test_a_prefix_term_does_not_match_a_word_that_only_contains_its_stem() -> None:
    entry = _entry(matching_terms=["квир*"])

    assert _hidden_titles([_candidate("A", "антиквирный")], entry) == set()
    assert _hidden_titles([_candidate("B", "квирного")], entry) == {"B"}


def test_a_russian_prefix_term_still_skips_english_metadata() -> None:
    candidate = _candidate("Book", "", english_title="квирный")

    assert matching_metadata_field(candidate, "квир*") == ""


@pytest.mark.parametrize(
    ("term", "error"),
    [
        ("тран*", ""),
        ("тра*", "prefix_stem_too_short"),
        ("ре*цепт", "prefix_marker_must_end_a_word"),
        ("рецепт**", "prefix_marker_must_end_a_word"),
        ("квир *", "prefix_marker_must_end_a_word"),
        ("*", "prefix_marker_must_end_a_word"),
        ("+", "term_has_no_words"),
        ('"истори* кухн*"', ""),
        ('"истори* кухн*', "phrase_quotes_must_wrap_the_whole_term"),
        ('история "кухни"', "phrase_quotes_must_wrap_the_whole_term"),
        ('""', "term_has_no_words"),
    ],
)
def test_topic_term_forms_are_validated(term: str, error: str) -> None:
    assert topic_term_error(term) == error


def test_normalisation_keeps_markers_and_is_idempotent() -> None:
    assert normalize_topic_term("Трансгендер*") == "трансгендер*"
    assert normalize_topic_term("ЛГБТ+*") == "лгбт*"
    assert normalize_topic_term('"История* Кухни"') == '"история* кухни"'
    for term in ("квир-тематика", "трансгендер*", '"истори* кухн*"'):
        assert normalize_topic_term(normalize_topic_term(term)) == normalize_topic_term(term)


def test_a_stored_malformed_term_fails_profile_validation() -> None:
    with pytest.raises(TasteProfileError, match="prefix_stem_too_short"):
        validate_taste_profile({"entries": [_entry(matching_terms=["тра*"])]})
    with pytest.raises(TasteProfileError, match="exception_terms"):
        validate_taste_profile({"entries": [_entry(exception_terms=["тра*"])]})


def test_add_rejects_a_malformed_term_without_mutation(tmp_path: Path, capsys) -> None:
    save_taste_profile(tmp_path, {"entries": []})
    before = _profile_bytes(tmp_path)

    code = add_profile_entry(
        tmp_path,
        "hard-rule",
        "topic",
        "трансгендерная тематика",
        "нет",
        "fixture",
        "test",
        matching_terms=["тра*"],
        confirmed_by_editor=True,
    )

    assert code == 2
    out = capsys.readouterr().out
    assert "error=invalid_matching_term" in out
    assert "reason=prefix_stem_too_short" in out
    assert _profile_bytes(tmp_path) == before


def test_set_terms_rejects_a_malformed_term_without_mutation(tmp_path: Path, capsys) -> None:
    legacy = _entry()
    del legacy["matching_terms"]
    save_taste_profile(tmp_path, {"entries": [legacy]})
    before = _profile_bytes(tmp_path)

    code = set_profile_entry_terms(tmp_path, "taste-0001", ["ре*цепт"], confirmed_by_editor=True)

    assert code == 2
    assert "reason=prefix_marker_must_end_a_word" in capsys.readouterr().out
    assert _profile_bytes(tmp_path) == before


def test_the_preview_reports_matches_found_through_a_prefix(tmp_path: Path, capsys) -> None:
    _write_latest(tmp_path, [_candidate("Transcendent", "Мемуары трансгендерной женщины.")])

    code = add_profile_entry(
        tmp_path,
        "hard-rule",
        "topic",
        "трансгендерная тематика",
        "нет",
        "fixture",
        "test",
        matching_terms=["трансгендер*"],
    )

    assert code == 3
    out = capsys.readouterr().out
    assert "current_matches=1" in out
    assert "term “трансгендер*” in description" in out
    assert not (tmp_path / "editorial-profile.json").exists()


# ── #967: phrases and exception terms ──


def test_a_quoted_phrase_needs_adjacent_words_in_order() -> None:
    entry = _entry(matching_terms=['"истори* кухн*"'])
    candidates = [
        _candidate("Adjacent", "Большая история кухни Мексики."),
        _candidate("Scattered", "Истории о семье и домашней кухне."),
        _candidate("Reversed", "Кухня и её история."),
    ]

    assert _hidden_titles(candidates, entry) == {"Adjacent"}


def test_an_exception_keeps_food_history_visible_while_cookbooks_hide() -> None:
    rule = _entry(matching_terms=COOKBOOK_TERMS, exception_terms=COOKBOOK_EXCEPTIONS)
    candidates = [
        _candidate("Simply Ina", "Сборник из 85 рецептов для повседневных обедов."),
        _candidate("Plant Bowls", "", english_annotation="A vegan cookbook for weeknights."),
        _candidate("Tacos", "Большая история кухни Мексики с историческими рецептами."),
        _candidate("Bad Food", "Корпорации влияют на выбор через рекламу и рецептуры продуктов."),
        _candidate("Spice Routes", "", english_annotation="A food history told through old recipes."),
        _candidate("Cooking Con Omi", "Кулинарная книга с историями о домашней кухне и рецептами."),
    ]

    assert _hidden_titles(candidates, rule) == {"Simply Ina", "Plant Bowls", "Cooking Con Omi"}


def test_an_exception_applies_to_soft_preferences_too() -> None:
    pref = _entry(kind="soft_preference", matching_terms=COOKBOOK_TERMS, exception_terms=COOKBOOK_EXCEPTIONS)

    assert _candidate_matches_soft_preference(_candidate("Simply Ina", "85 рецептов"), pref)
    assert not _candidate_matches_soft_preference(_candidate("Tacos", "история кухни и рецепты"), pref)


def test_exception_terms_need_matching_terms_on_a_topic_entry() -> None:
    entry = _entry(exception_terms=["рецептур*"])
    del entry["matching_terms"]

    with pytest.raises(TasteProfileError, match="exception_terms is only allowed on topic entries with matching_terms"):
        validate_taste_profile({"entries": [entry]})


def test_add_rejects_exception_terms_outside_topic_scope(tmp_path: Path, capsys) -> None:
    code = add_profile_entry(
        tmp_path, "hard-rule", "author", "Ina Garten", "нет", "fixture", "test", exception_terms=["рецептур*"]
    )

    assert code == 2
    assert "error=exception_terms_only_allowed_for_topic_entries" in capsys.readouterr().out


def test_add_rejects_an_exception_that_repeats_a_matching_term(tmp_path: Path, capsys) -> None:
    code = add_profile_entry(
        tmp_path,
        "hard-rule",
        "topic",
        "кулинарные книги",
        "нет",
        "fixture",
        "test",
        matching_terms=["рецепт*"],
        exception_terms=["Рецепт*"],
    )

    assert code == 2
    assert "error=exception_term_repeats_matching_term" in capsys.readouterr().out


def test_preview_lists_both_hidden_books_and_books_kept_by_an_exception(tmp_path: Path, capsys) -> None:
    _write_latest(
        tmp_path,
        [
            _candidate("Simply Ina", "Сборник из 85 рецептов."),
            _candidate("Tacos", "Большая история кухни Мексики с рецептами."),
            _candidate("Unrelated", "Книга о мостах."),
        ],
    )

    code = add_profile_entry(
        tmp_path,
        "hard-rule",
        "topic",
        "[editor note withheld]",
        "Кулинарные книги хотелось бы исключить совсем.",
        "fixture",
        "test",
        matching_terms=COOKBOOK_TERMS,
        exception_terms=COOKBOOK_EXCEPTIONS,
    )

    assert code == 3
    lines = capsys.readouterr().out.splitlines()
    assert 'exception_terms=["\\"истори* кухн*\\"","\\"food history\\"","рецептур*"]' in lines
    assert "current_matches=1" in lines
    assert "kept_by_exception=1" in lines
    kept = [json.loads(line.split("=", 1)[1]) for line in lines if line.startswith("kept_by_exception_match=")]
    assert [row["title"] for row in kept] == ["Tacos"]
    assert kept[0]["exception"] == 'exception term “"истори* кухн*"” in description'
    assert not (tmp_path / "editorial-profile.json").exists()


def test_preview_without_exceptions_prints_no_exception_lines(tmp_path: Path, capsys) -> None:
    _write_latest(tmp_path, [_candidate("Simply Ina", "Сборник из 85 рецептов.")])

    add_profile_entry(tmp_path, "hard-rule", "topic", "рецепты", "нет", "fixture", "test", matching_terms=["рецепт*"])

    out = capsys.readouterr().out
    assert "exception_terms=" not in out
    assert "kept_by_exception" not in out


def test_confirmed_add_stores_exception_terms_and_list_shows_them(tmp_path: Path, capsys) -> None:
    code = add_profile_entry(
        tmp_path,
        "hard-rule",
        "topic",
        "кулинарные книги",
        "Кулинарные книги хотелось бы исключить совсем.",
        "fixture",
        "test",
        matching_terms=COOKBOOK_TERMS,
        exception_terms=[*COOKBOOK_EXCEPTIONS, "рецептур*"],
        confirmed_by_editor=True,
    )

    assert code == 0
    stored = read_state(tmp_path, "editorial_profile")["entries"][0]
    assert stored["matching_terms"] == COOKBOOK_TERMS
    assert stored["exception_terms"] == COOKBOOK_EXCEPTIONS
    capsys.readouterr()
    list_profile_entries(tmp_path)
    assert "Exception terms:" in capsys.readouterr().out


def test_entries_that_differ_only_in_exceptions_are_not_exact_duplicates(tmp_path: Path, capsys) -> None:
    save_taste_profile(tmp_path, {"entries": [_entry(matching_terms=["рецепт*"])]})
    existing = _entry(matching_terms=["рецепт*"])

    code = add_profile_entry(
        tmp_path,
        "hard-rule",
        "topic",
        existing["subject"],
        "нет",
        "fixture",
        "test",
        matching_terms=["рецепт*"],
        exception_terms=["рецептур*"],
    )

    out = capsys.readouterr().out
    assert "reason=exact_duplicate_exists_no_new_effect_needed" not in out
    assert code == 3
    assert "reason=probable_overlap_unresolved" in out


def test_a_prefix_term_and_its_plain_word_are_different_behaviour(tmp_path: Path, capsys) -> None:
    save_taste_profile(tmp_path, {"entries": [_entry(matching_terms=["рецепт"])]})

    add_profile_entry(
        tmp_path, "hard-rule", "topic", "fixture topic", "нет", "fixture", "test", matching_terms=["рецепт*"]
    )

    assert "reason=exact_duplicate_exists_no_new_effect_needed" not in capsys.readouterr().out


def test_effects_report_exception_terms_only_for_entries_that_have_them() -> None:
    profile = {
        "entries": [
            _entry("taste-0001", matching_terms=["рецепт*"], exception_terms=["рецептур*"]),
            _entry("taste-0002", matching_terms=["квир*"]),
        ]
    }
    candidates = [_candidate("Bad Food", "рецептуры"), _candidate("Simply Ina", "рецепты")]
    _visible, hidden = hide_candidates_by_rules(candidates, profile)

    effects = {effect["id"]: effect for effect in editorial_taste_profile_effects(candidates, profile, hidden, {})}

    assert effects["taste-0001"]["exception_terms"] == ["рецептур*"]
    assert effects["taste-0001"]["match_count"] == 1
    assert "exception_terms" not in effects["taste-0002"]
