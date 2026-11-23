"""English helper-field contract for every family (Issue #645).

German and French books reached the editor with no English title for two
compounding reasons: three German sources declared no language, and the
``generic`` (and ``audiobook-ranking``) contract family never asked for the
helper fields. The language test now lives in exactly one place
(``autoscout.source_language``), and every non-English source requests the
helpers whatever its contract family.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Signal families of the affected portfolio sources (ticket #645): three
# different signal families, one contract family (``generic``).
# Issue #679 renamed Deutscher Sachbuchpreis 2024 to the year-neutral
# Deutscher Sachbuchpreis; the signal family and helper contract are unchanged.
GENERIC_NON_ENGLISH_SOURCES = [
    {"name": "Spiegel Bestseller Sachbuch", "family": "bestseller/ranking", "language": "de"},
    {"name": "Deutscher Sachbuchpreis", "family": "award", "language": "de"},
    {"name": "Die Zeit Sachbuch-Bestenliste", "family": "media/curated list", "language": "de"},
    {"name": "Académie française Prix de l’essai", "family": "award", "language": "fr"},
]

BASE_REQUIRED_FIELDS = [
    "title",
    "primary_authors",
    "source_url",
    "evidence_url",
    "signal_type",
    "signal_strength",
    "evidence_note",
]

HELPER_REQUIRED_FIELDS = ["english_title", "search_translation", "author_transliteration_note"]


def _generic_source(name: str, family: str, language: str) -> dict[str, Any]:
    return {
        "name": name,
        "family": family,
        "url": f"https://example.test/{name.lower().replace(' ', '-')}",
        "active": True,
        "access_status": "open",
        "check_frequency": "weekly",
        "language": language,
    }


def test_language_predicate_matrix() -> None:
    from autoscout.source_language import requires_english_helper_fields

    for language in ("en", "EN", "english", "English", " en "):
        assert requires_english_helper_fields({"language": language}) is False
    for language in ("de", "fr", "it", "es", "cs", "pt", "no"):
        assert requires_english_helper_fields({"language": language}) is True


def test_language_predicate_rejects_blank_language() -> None:
    """An empty language is an error, not a default (Issue #645).

    Two branches used to read ``""`` as non-English and one as English.
    Neither reading survives: configuration validation requires a declared
    language, so a blank value reaching the predicate is a programming
    error and must fail loudly instead of silently picking a meaning.
    """
    from autoscout.source_language import requires_english_helper_fields

    for source in ({}, {"language": ""}, {"language": "   "}):
        with pytest.raises(ValueError, match="language"):
            requires_english_helper_fields(source)


def test_language_test_has_one_owner() -> None:
    """No second copy of the English-language test may exist.

    This test fails if any module outside ``autoscout/source_language.py``
    grows its own ``{"en", "english"}``-style language set, which is how
    the two disagreeing readings of an empty language came back.
    """
    hits: list[str] = []
    for path in sorted((ROOT / "autoscout").rglob("*.py")):
        if path.name == "source_language.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if '"en"' in line and '"english"' in line:
                hits.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert hits == [], "competing language tests found:\n" + "\n".join(hits)


def test_non_english_generic_sources_request_helper_fields() -> None:
    """The contract fix, proven for each affected signal family."""
    from autoscout.source_collectors import extraction_contract

    for spec in GENERIC_NON_ENGLISH_SOURCES:
        source = _generic_source(spec["name"], spec["family"], spec["language"])
        contract = extraction_contract(source)
        for field in HELPER_REQUIRED_FIELDS:
            assert field in contract["required_fields"], f"{spec['name']}: {field} not required"
            assert field in contract["required_output_shape"]["mentions"][0], (
                f"{spec['name']}: {field} missing from mention shape"
            )


def test_non_english_audiobook_ranking_requests_helper_fields() -> None:
    from autoscout.source_collectors import extraction_contract

    source = _generic_source("Audiobooks Beispiel", "audiobook ranking", "de")
    contract = extraction_contract(source)
    for field in HELPER_REQUIRED_FIELDS:
        assert field in contract["required_fields"]
        assert field in contract["required_output_shape"]["mentions"][0]


def test_english_generic_source_requests_no_helper_fields() -> None:
    """English books need no English title: the conditional stays conditional."""
    from autoscout.source_collectors import extraction_contract

    source = _generic_source("PEOPLE Books", "media/curated list", "en")
    contract = extraction_contract(source)
    assert contract["required_fields"] == BASE_REQUIRED_FIELDS


def test_english_exact_book_list_keeps_equality_form() -> None:
    """Exact-book-list English sources still ask for english_title == title."""
    from autoscout.source_collectors import extraction_contract

    source = {
        **_generic_source("Carnegie Example", "award", "en"),
        "metadata_exact_book_list": True,
        "expected_signal_strength": "finalist",
        "eligible_candidate_category": "adult nonfiction",
    }
    contract = extraction_contract(source)
    for field in HELPER_REQUIRED_FIELDS:
        assert field in contract["required_fields"]


def _valid_generic_german_entry() -> dict[str, Any]:
    return {
        "title": "Die Kraft der Demokratie",
        "primary_authors": ["Hedwig Richter"],
        "source_url": "https://www.zeit.de/sachbuch-bestenliste",
        "evidence_url": "https://www.zeit.de/sachbuch-bestenliste/die-kraft-der-demokratie",
        "signal_type": "media/curated list",
        "signal_strength": "major media list",
        "evidence_note": "Bestenliste entry for Die Kraft der Demokratie by Hedwig Richter.",
        "candidate_category": "adult nonfiction",
        "english_title": "The Power of Democracy",
        "search_translation": "Сила демократии",
        "author_transliteration_note": "Хедвиг Рихтер",
    }


def test_generic_validator_accepts_valid_non_english_helpers() -> None:
    from autoscout.source_collectors import extraction_contract_violation

    source = _generic_source("Die Zeit Sachbuch-Bestenliste", "media/curated list", "de")
    entry = _valid_generic_german_entry()
    page_data = {"source_page": entry["source_url"], "html": ""}
    assert extraction_contract_violation(entry, dict(entry), source, page_data) == ""


def test_generic_validator_rejects_missing_non_english_helpers() -> None:
    from autoscout.source_collectors import extraction_contract_violation

    source = _generic_source("Spiegel Bestseller Sachbuch", "bestseller/ranking", "de")
    entry = _valid_generic_german_entry()
    entry["source_url"] = source["url"]
    for field in HELPER_REQUIRED_FIELDS:
        broken = dict(entry)
        del broken[field]
        page_data = {"source_page": source["url"], "html": ""}
        reason = extraction_contract_violation(broken, dict(broken), source, page_data)
        assert "helper metadata" in reason, f"{field}: expected a helper violation, got {reason!r}"


def test_generic_validator_ignores_helpers_for_english_sources() -> None:
    from autoscout.source_collectors import extraction_contract_violation

    source = _generic_source("PEOPLE Books", "media/curated list", "en")
    entry = _valid_generic_german_entry()
    entry["source_url"] = source["url"]
    for field in HELPER_REQUIRED_FIELDS:
        entry.pop(field, None)
    page_data = {"source_page": source["url"], "html": ""}
    assert extraction_contract_violation(entry, dict(entry), source, page_data) == ""


def test_nonfiction_fr_produces_helpers_with_sidecar_and_defers_without() -> None:
    """The fifth affected source: the RSS collector attaches curated helpers
    when a sidecar exists, and flows helper-less with a recorded reason
    otherwise (Issue #645 deferral, docs/issue-645-nonfiction-fr-deferral.md).
    """
    import hashlib
    import json
    import tempfile

    from autoscout.source_collectors import COLLECTOR_REGISTRY

    fixture_dir = ROOT / "tests" / "fixtures" / "nonfiction-fr"
    rss_text = (fixture_dir / "rss-mixed.xml").read_bytes()
    pages = json.loads((fixture_dir / "review-pages-mixed.json").read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        rss_path = tmp_path / "rss.xml"
        rss_path.write_bytes(rss_text)
        pages_path = tmp_path / "pages.json"
        pages_path.write_text(json.dumps(pages), encoding="utf-8")
        config_path = tmp_path / "config.json"
        config_path.write_text("{}", encoding="utf-8")
        base = {
            "name": "Nonfiction.fr Critiques",
            "family": "media/curated list",
            "url": "https://www.nonfiction.fr/rss-critiques.xml",
            "active": True,
            "access_status": "open",
            "check_frequency": "weekly",
            "language": "fr",
            "extraction_method": "nonfiction-fr-rss-replay",
            "rss_fixture_file": str(rss_path),
            "review_pages_fixture_file": str(pages_path),
        }

        plain = COLLECTOR_REGISTRY["nonfiction-fr-rss-replay"].collect(
            config_path, tmp_path / "out", dict(base), "2026-07-17"
        )
        assert len(plain.mentions) == 1
        assert "english_title" not in plain.mentions[0]
        assert plain.debug_metadata["helper_attached_count"] == 0

        sidecar = {
            "version": 1,
            "source_url": "https://www.nonfiction.fr/rss-critiques.xml",
            "language": "fr",
            "entries": [
                {
                    "title": "Cartographier les futurs",
                    "author": "Camille Exemple",
                    "evidence_url": "https://www.nonfiction.fr/article-100-complete-book.htm",
                    "english_title": "Mapping Futures",
                    "search_translation": "Картографирование будущего",
                    "author_transliteration_note": "Камиль Экзампль",
                }
            ],
        }
        sidecar_path = tmp_path / "helper.json"
        sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")
        digest = hashlib.sha256(sidecar_path.read_bytes()).hexdigest()
        enriched = COLLECTOR_REGISTRY["nonfiction-fr-rss-replay"].collect(
            config_path,
            tmp_path / "out",
            {
                **base,
                "helper_metadata_file": str(sidecar_path),
                "helper_metadata_sha256": digest,
            },
            "2026-07-17",
        )
        assert len(enriched.mentions) == 1
        assert enriched.mentions[0]["english_title"] == "Mapping Futures"
        assert enriched.mentions[0]["title"] == "Cartographier les futurs"
        assert enriched.debug_metadata["helper_attached_count"] == 1


def test_config_validation_rejects_missing_language() -> None:
    from autoscout.config import validate_config

    config = {
        "run_date": "2026-09-03",
        "sources": [
            {
                "name": "Language-less Source",
                "family": "award",
                "url": "https://example.test/no-language",
                "active": True,
                "access_status": "open",
                "check_frequency": "weekly",
                "notes": "Seven words of notes for validation here.",
            }
        ],
    }
    errors = validate_config(config)
    assert any("language" in error for error in errors), errors


def test_portfolio_declares_a_language_for_every_source() -> None:
    from autoscout.config import load_validated_config

    config = load_validated_config(ROOT / "configs" / "current-weekly-portfolio.json")
    # #907: the confirmed onboarding adds one more Spanish frontlist source.
    # #945: the Publishers Marketplace deals source restores the rights/deal family.
    # #955: the Publishers Weekly issue reviews source.
    # #996: PW Best Books 2026 joins; the 2025 record stays as retired.
    # #996: Goodreads Choice Awards 2026 joins; the 2025 record stays as retired.
    assert len(config["sources"]) == 54
    missing = [source["name"] for source in config["sources"] if not str(source.get("language", "")).strip()]
    assert missing == []


def test_affected_replay_fixtures_produce_english_titles() -> None:
    """The measurement test: replay each affected extraction source and prove
    the collected mentions carry an English title."""
    from autoscout.config import load_validated_config
    from autoscout.source_collection import SourceCollection

    config_path = ROOT / "configs" / "current-weekly-pilot.json"
    config = load_validated_config(config_path)
    names = [spec["name"] for spec in GENERIC_NON_ENGLISH_SOURCES]
    # A retired source (Die Zeit, Issue #764) keeps its replay fixture, and the
    # fixture is still measured: it is collected from a copy that is switched on.
    sources = [{**source, "active": True} for source in config["sources"] if source.get("name") in names]
    assert {source["name"] for source in sources} == set(names)

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        result = SourceCollection(config_path, Path(tmp) / "runs", "2026-06-10").collect(sources)
    by_source: dict[str, list[dict[str, Any]]] = {}
    for mention in result.source_mentions:
        by_source.setdefault(mention.get("source_name", ""), []).append(mention)
    assert set(by_source) == set(names), by_source.keys()
    for name in names:
        assert by_source[name], f"{name}: no mentions collected"
        for mention in by_source[name]:
            assert str(mention.get("english_title", "")).strip(), (
                f"{name}: mention {mention.get('title')!r} has no English title"
            )
            assert str(mention.get("search_translation", "")).strip()
            assert mention["search_translation"].strip() != mention["title"].strip()
            assert str(mention.get("author_transliteration_note", "")).strip()


@pytest.mark.parametrize("title", ["MEMORIAL", "Stalin", "Moonwalk"])
def test_a_non_english_title_may_read_the_same_in_english(title: str) -> None:
    """The 2026-11-03 run lost three books whose title is the same word in English.

    The English title is a non-authoritative search aid, so equality costs no book.
    English sources still require english_title to equal the title.
    """
    from autoscout.helper_metadata import validate_helper_metadata

    entry = {
        "title": title,
        "english_title": title,
        "search_translation": "Книга",
        "author_transliteration_note": "Ирина Щербакова",
    }
    assert validate_helper_metadata(entry, title_relation=True) == ""
    assert validate_helper_metadata(entry, title_relation=False) == ""
    assert validate_helper_metadata({**entry, "english_title": "Something else"}, title_relation=False)
    assert validate_helper_metadata({**entry, "search_translation": title}, title_relation=True)
