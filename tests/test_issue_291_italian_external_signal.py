"""Source-specific pinned coverage for Issue #291 (Premio Strega Saggistica)."""

import json
from pathlib import Path
from typing import Any

from autoscout.config import load_validated_config
from autoscout.source_page_identity import link_anchor_pairs

ROOT = Path(__file__).resolve().parents[1]
SOURCE_NAME = "Premio Strega Saggistica 2026 Cinquina"
SOURCE_URL = "https://premiostrega.it/PSS/i-cinque-libri-candidati-al-premio-strega-saggistica-2edizione/"
CINQUINA_BOOKS = (
    (
        "Alfabit. L’italiano digitale dagli SMS all’IA",
        "Giuseppe Antonelli",
        "https://premiostrega.it/PSS/libro/alfabit-litaliano-digitale-dagli-sms-allia/",
    ),
    (
        "L’omicidio di Piersanti Mattarella. L’Italia nel mirino: Palermo, Ustica, Bologna. 1979-1980",
        "Miguel Gotor",
        "https://premiostrega.it/PSS/libro/lomicidio-di-piersanti-mattarella/",
    ),
    (
        "La peste. Indagine sulla destra in Germania",
        "Tonia Mastrobuoni",
        "https://premiostrega.it/PSS/libro/la-peste-indagine-sulla-destra-in-germania/",
    ),
    (
        "Vertigine. Storie di chi si affida alla scienza e di chi impara a farlo",
        "Beatrice Mautino",
        "https://premiostrega.it/PSS/libro/vertigine-storie-di-chi-si-affida-alla-scienza-e-di-chi-impara-a-farlo/",
    ),
    ("Oriente. Una storia", "Alessandro Vanoli", "https://premiostrega.it/PSS/libro/oriente-una-storia/"),
)


def _source() -> dict[str, Any]:
    config = load_validated_config(ROOT / "configs/current-weekly-pilot.json")
    return dict(next(value for value in config["sources"] if value["name"] == SOURCE_NAME))


def _fixtures() -> tuple[dict[str, Any], dict[str, Any]]:
    source = _source()
    page = json.loads((ROOT / "configs" / source["source_page_fixture_file"]).read_text(encoding="utf-8"))
    extraction = json.loads((ROOT / "configs" / source["extraction_response_file"]).read_text(encoding="utf-8"))
    return page, extraction


def test_portfolio_metadata_fields() -> None:
    source = _source()
    expected = {
        "family": "award",
        "signal_family": "award",
        "language": "it",
        "country": "Italy",
        "market": "Italy",
        "refresh_cadence": "annual",
        "url_stability": "year-specific",
        "check_frequency": "annual",
        "access_status": "open",
        "active": True,
        "metadata_exact_book_list": True,
        "expected_signal_strength": "shortlist",
        "eligible_candidate_category": "adult nonfiction",
        "lifecycle_status": "established",
        "url": SOURCE_URL,
    }
    for field, value in expected.items():
        assert source.get(field) == value


def test_pinned_cinquina_books_and_links() -> None:
    page, extraction = _fixtures()
    pairs = tuple(link_anchor_pairs(page["html"], page["source_page"]))
    mentions = tuple((m["title"], m["primary_authors"][0], m["evidence_url"]) for m in extraction["mentions"])
    assert pairs == tuple((url, title) for title, _author, url in CINQUINA_BOOKS)
    assert mentions == CINQUINA_BOOKS


def test_expected_signal_strength_is_shortlist_not_winner() -> None:
    assert _source()["expected_signal_strength"] == "shortlist"
    _, extraction = _fixtures()
    assert all(m["signal_strength"] == "shortlist" and m["signal_strength"] != "winner" for m in extraction["mentions"])
