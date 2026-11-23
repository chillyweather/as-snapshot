"""Złota Róża 2026 nominations source-specific tests (onboarding #795).

The shared award-list contract is covered by tests/test_issue_577_award_list.py,
tests/test_issue_578_award_list_categories.py and
tests/test_issue_795_award_list_category_label.py. This file pins what is
specific to this source: the four nominee rows written author first, the bold
paragraph that labels them, the article heading that states the year, and the
lists of the same article that are not collected. It reads the committed
replay fixtures and makes no network request.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.annual_source_audit import CURRENT
from autoscout.annual_source_audit import REASON_NEWER_EDITION_EXPECTED
from autoscout.annual_source_audit import audit_annual_source
from autoscout.award_list import award_rows
from autoscout.award_list import mention_violation
from autoscout.config import load_validated_config
from autoscout.source_page_identity import validate_page_shape

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = ROOT / "configs"
SOURCE_NAME = "Nagroda Złota Róża 2026 — nominacje"
URL = "https://www.instytutksiazki.pl/aktualnosci/znamy-nominacje-do-nagrody-zlota-roza-2026/"
RULES_URL = "https://festiwalnauki.edu.pl/nagroda-zlota-roza"
LABEL = "Nominowani w 2026 roku"
LABEL_MARKUP = f"<p><strong>{LABEL}</strong></p>"

NOMINEES = [
    ("Czego się boją ptaki", "Adam Zbyryt"),
    ("Majowie. Na tropie wielkiej cywilizacji Ameryki Środkowej", "Jarosław Źrałka"),
    ("W stronę długowieczności", "Marek Postuła, Krzysztof Łoniewski"),
    ("Fleck. Ocalony przez naukę", "Anna Wacławik, Maria Ciesielska"),
]
# Lists of the same article that this source does not collect.
TAG_LIST = '<ul><li><a href="https://www.instytutksiazki.pl/aktualnosci-tagi/nowe-ksiazki/">Nowe Książki</a></li></ul>'
JURY_LIST = (
    "<p><strong>Jury nagrody</strong></p><ul>"
    "<li>Urszula Chwalba – Instytut Książki</li>"
    "<li>Andrzej Skalimowski – redaktor naczelny miesięcznika „Nowe Książki”</li></ul>"
)
# Headings of unrelated news the article page shows below the article.
RELATED_NEWS = (
    "<h2>Przeczytaj również</h2>"
    "<h4>Zgłoszenia do Stypendium im. Albrechta Lemppa za 2027 rok</h4>"
    "<h4>„Cyberpunk 2077: Bez przypadku” Rafała Kosika wyróżniony Nagrodą Galaxy w Chinach</h4>"
)


def _source(config_name: str = "current-weekly-pilot.json") -> dict[str, Any]:
    config = load_validated_config(CONFIGS / config_name)
    return next(source for source in config["sources"] if source["name"] == SOURCE_NAME)


def _fixture(field: str) -> dict[str, Any]:
    return json.loads((CONFIGS / _source()[field]).read_text(encoding="utf-8"))


def _page(html: str | None = None, **overrides: Any) -> dict[str, Any]:
    page = _fixture("source_page_fixture_file")
    if html is not None:
        page["html"] = html
    page.update(overrides)
    return page


def _mention(title: str, authors: list[str]) -> dict[str, Any]:
    return {**_fixture("extraction_response_file")["mentions"][0], "title": title, "primary_authors": authors}


def test_the_source_declares_one_labelled_category_of_a_polish_article() -> None:
    source = _source()

    assert source["url"] == URL
    assert source["language"] == "pl"
    assert source["country"] == "Poland"
    assert source["contract_family"] == "award-list"
    assert source["award_year"] == 2026
    assert source["award_year_selector"] == ".eh-heading"
    assert source["award_category_heading"] == LABEL
    assert source["award_category_label_element"] == "strong"
    assert source["award_row_format"] == "{author}, {title}, {ignored}"
    assert source["award_row_title_element"] == "em"
    assert source["award_expected_row_count"] == 4
    assert source["lifecycle_status"] == "established"
    assert source["check_frequency"] == "annual"
    assert "adult_nonfiction_exclusions" not in source


def test_the_polish_original_rule_is_recorded_with_the_rules_url() -> None:
    notes = _source()["notes"]

    assert "written in Polish" in notes
    assert RULES_URL in notes


def test_the_replay_page_holds_the_four_nominees_written_author_first() -> None:
    page = _page()
    rows = award_rows(page["html"], _source())

    assert [(row.title, row.author) for row in rows] == NOMINEES
    assert LABEL_MARKUP in page["html"]
    assert validate_page_shape(_source(), page) == ""


def test_coauthors_before_the_title_are_each_one_complete_name() -> None:
    source = _source()
    authors = ["Marek Postuła", "Krzysztof Łoniewski"]

    assert mention_violation(*[_mention("W stronę długowieczności", authors)] * 2, _page(), source) == ""
    assert "not grounded" in mention_violation(
        *[_mention("W stronę długowieczności", authors[:1])] * 2, _page(), source
    )
    # The comma between coauthors does not begin the title.
    assert "not grounded" in mention_violation(
        *[_mention("Krzysztof Łoniewski, W stronę długowieczności", authors[:1])] * 2, _page(), source
    )


def test_the_tag_list_and_the_jury_list_of_the_article_are_not_rows_of_the_source() -> None:
    source = _source()
    html = _page()["html"].replace(LABEL_MARKUP, TAG_LIST + LABEL_MARKUP).replace("</body>", JURY_LIST + "</body>")
    page = _page(html)

    assert TAG_LIST in html and JURY_LIST in html
    assert [(row.title, row.author) for row in award_rows(html, source)] == NOMINEES
    assert validate_page_shape(source, page) == ""
    assert "not grounded" in mention_violation(*[_mention("Instytut Książki", ["Urszula Chwalba"])] * 2, page, source)


def test_a_jury_list_that_loses_its_label_refuses_the_page() -> None:
    source = _source()
    unlabelled = JURY_LIST.replace("<p><strong>Jury nagrody</strong></p>", "<p>Jury nagrody:</p>")
    html = _page()["html"].replace("</body>", unlabelled + "</body>")

    assert "award row 5 does not set its title in exactly one <em> element" in validate_page_shape(source, _page(html))


def test_a_changed_row_count_or_label_fails_closed() -> None:
    source = _source()
    html = _page()["html"]
    short = html.replace("<li>Adam Zbyryt, <em>Czego się boją ptaki</em>, Wydawnictwo Poznańskie</li>\n", "")
    renamed = html.replace(LABEL_MARKUP, "<p><strong>Nominowani</strong></p>")
    as_heading = html.replace(LABEL_MARKUP, f"<h2>{LABEL}</h2>")

    assert len({html, short, renamed, as_heading}) == 4
    assert "exposes 3 rows; expected exactly 4" in validate_page_shape(source, _page(short))
    assert f"'{LABEL}' appears 0 times" in validate_page_shape(source, _page(renamed))
    assert f"'{LABEL}' appears 0 times" in validate_page_shape(source, _page(as_heading))


def test_the_award_year_is_read_from_the_article_heading_and_not_from_related_news() -> None:
    source = _source()
    html = _page()["html"]
    with_news = html.replace("</body>", RELATED_NEWS + "</body>")

    # Unrelated headings that state 2027 and 2077 do not supersede the 2026 edition; read by default, they would.
    assert with_news != html
    assert validate_page_shape(source, _page(with_news)) == ""
    assert "newer award year 2077" in validate_page_shape({**source, "award_year_selector": ""}, _page(with_news))


def test_a_stale_or_superseded_award_year_is_reported() -> None:
    source = _source()
    html = _page()["html"]
    replaced = html.replace("„Złota Róża” 2026", "„Złota Róża” 2027")

    assert replaced != html
    assert validate_page_shape(source, _page(reference_date="2027-06-01")) == ""
    # The 2027 nominations are published in September (#680 release window).
    assert audit_annual_source(source, today=date(2027, 6, 1)).verdict == CURRENT
    assert audit_annual_source(source, today=date(2027, 10, 1)).reason == REASON_NEWER_EDITION_EXPECTED
    assert "award year 2026 is stale" in validate_page_shape(source, _page(reference_date="2028-01-01"))
    # A page that moved on to the next edition is refused, never re-read as 2026.
    assert "expected page heading" in validate_page_shape(source, _page(replaced))


def test_replay_mentions_cite_the_configured_page_and_nothing_a_row_does_not_state() -> None:
    extraction = _fixture("extraction_response_file")

    assert [mention["title"] for mention in extraction["mentions"]] == [title for title, _author in NOMINEES[:3]]
    assert extraction["mentions"][2]["primary_authors"] == ["Marek Postuła", "Krzysztof Łoniewski"]
    for mention in extraction["mentions"]:
        assert mention["source_url"] == mention["evidence_url"] == URL
        assert mention["signal_strength"] == "shortlist"
        assert mention["english_title"] != mention["title"]
        assert not {"current_rank", "isbn", "source_week", "publisher"} & mention.keys()


def test_live_collection_is_opt_in_in_both_live_overlays() -> None:
    for config_name in ("current-weekly-pilot-live.json", "current-weekly-live.json"):
        source = _source(config_name)
        assert source["enable_live"] is True
        assert source["extraction_method"] == "agent-extraction-live"
        assert source["award_category_label_element"] == "strong"
