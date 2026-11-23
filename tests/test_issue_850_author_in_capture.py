"""Issue #850: an extracted author must be named somewhere in the capture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autoscout.author_grounding import AUTHOR_NOT_IN_CAPTURE_DROP_REASON
from autoscout.author_grounding import CaptureAuthorText
from autoscout.config import load_validated_config
from autoscout.source_collectors import agent_extraction_mentions_from_response

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
# The committed 2026-09-26 BookPage run that accepted "The Cartoonist Cooperative".
RUN_0926 = FIXTURES / "issue-850-bookpage-2026-09-26"
# The committed 2026-09-20 BookPage capture; it never prints Lena Andrews.
RUN_0920 = FIXTURES / "issue-852-bookpage-2026-09-20"
CAPTURE_NAME = "codex-capture-bookpage-nonfiction-6c3244f3.json"
EXTRACTION_NAME = "bookpage-nonfiction-6c3244f3-codex-extraction.json"


def _bookpage_source() -> dict[str, Any]:
    config = load_validated_config(ROOT / "configs/current-weekly-pilot.json")
    return next(source for source in config["sources"] if source["name"] == "BookPage Nonfiction")


def _replay(run_dir: Path, entries: list[dict[str, Any]], run_date: str) -> tuple[list[dict], list[dict]]:
    page = json.loads((run_dir / CAPTURE_NAME).read_text(encoding="utf-8"))
    return agent_extraction_mentions_from_response(
        page, {"mentions": entries}, _bookpage_source(), run_date, collector_kind="codex-extraction"
    )


def _entries(run_dir: Path) -> list[dict[str, Any]]:
    return json.loads((run_dir / EXTRACTION_NAME).read_text(encoding="utf-8"))["entries"]


def test_the_invented_cooperative_drops_and_every_author_on_the_page_stays() -> None:
    entries = [entry for entry in _entries(RUN_0926) if any(str(a).strip() for a in entry["primary_authors"])]
    mentions, dropped = _replay(RUN_0926, entries, "2026-09-26")

    assert [(drop["title"], drop["reason"]) for drop in dropped] == [
        ("I Won’t Pretend These Missiles Are Stars", AUTHOR_NOT_IN_CAPTURE_DROP_REASON)
    ]
    assert len(mentions) == len(entries) - 1

    # The author the page does name, through its review URL slug, is admitted.
    missiles = next(entry for entry in entries if entry["title"].startswith("I Won’t Pretend"))
    mentions, dropped = _replay(
        RUN_0926, [{**missiles, "primary_authors": ["The Cartoonist Collective"]}], "2026-09-26"
    )
    assert dropped == []
    assert [mention["primary_authors"] for mention in mentions] == [["The Cartoonist Collective"]]


def test_an_author_supplied_from_outside_the_page_drops() -> None:
    # Lena Andrews is the real author of Valiant Women, but BookPage never
    # prints the name; the row drops rather than trust the extractor.
    valiant = next(entry for entry in _entries(RUN_0920) if entry["title"] == "Valiant Women")
    mentions, dropped = _replay(RUN_0920, [{**valiant, "primary_authors": ["Lena Andrews"]}], "2026-09-20")
    assert mentions == []
    assert [drop["reason"] for drop in dropped] == [AUTHOR_NOT_IN_CAPTURE_DROP_REASON]


@pytest.mark.parametrize(
    ("page", "authors", "evidence_url", "missing"),
    [
        # Printed in visible text, in any name order, accents and case ignored.
        ({"html": "<p>by Jon Ronson</p>"}, ["Jon Ronson"], "", []),
        ({"html": "<p>Ronson, Jon</p>"}, ["Jon Ronson"], "", []),
        ({"html": "<p>RONSON Jon</p>"}, ["Ronson, Jon"], "", []),
        ({"html": "<p>Honoree Fanonne Jeffers</p>"}, ["Honorée Fanonne Jeffers"], "", []),
        # Only in an attribute, or only in the evidence URL slug.
        ({"html": '<img alt="Maria Ressa">'}, ["Maria Ressa"], "", []),
        (
            {"html": ""},
            ["The Cartoonist Collective"],
            "https://www.bookpage.com/reviews/missiles-the-cartoonist-collective/",
            [],
        ),
        # Only in embedded script data, with JSON escapes.
        (
            {"html": '<script>{"author":"Ma\\u0142gorzata Niemczy\\u0144ska"}</script>'},
            ["Małgorzata Niemczyńska"],
            "",
            [],
        ),
        # Only in another retained field, such as a structured text body.
        ({"html": "", "text": "1,Book,Zadie Smith"}, ["Zadie Smith"], "", []),
        # A credential is stripped before the check; each listed person must appear.
        ({"html": "<p>Robin Blackstone</p>"}, ["Robin Blackstone, MD"], "", []),
        ({"html": "<p>Naomi Klein</p>"}, ["Naomi Klein", "Astra Taylor"], "", ["Astra Taylor"]),
        # A name split by inline formatting, one broken by a newline in a
        # retained field, and a multi-word surname printed first.
        ({"html": "<p>by Jane <em>Author</em></p>"}, ["Jane Author"], "", []),
        ({"html": "", "text": "Jon\nRonson"}, ["Jon Ronson"], "", []),
        ({"html": "<p>García Márquez, Gabriel</p>"}, ["Gabriel García Márquez"], "", []),
        # No phrase spans two separate capture fields.
        ({"html": "<p>Ann</p>", "text": "Lee"}, ["Ann Lee"], "", ["Ann Lee"]),
        # A paraphrase of the slug, and a name whose tokens appear only apart.
        (
            {"html": ""},
            ["The Cartoonist Cooperative"],
            "https://www.bookpage.com/reviews/missiles-the-cartoonist-collective/",
            ["The Cartoonist Cooperative"],
        ),
        ({"html": "<p>John wrote with Green</p>"}, ["John Green"], "", ["John Green"]),
        # A whole-word match: "Ann Lee" is not inside "Joann Leeds".
        ({"html": "<p>Joann Leeds</p>"}, ["Ann Lee"], "", ["Ann Lee"]),
    ],
)
def test_capture_author_text(page: dict[str, Any], authors: list[str], evidence_url: str, missing: list[str]) -> None:
    assert CaptureAuthorText(page).missing_people(authors, evidence_url) == missing


def test_a_line_break_inside_a_name_reads_as_a_space() -> None:
    assert CaptureAuthorText({"html": "<p>Jon<br>Ronson</p>"}).missing_people(["Jon Ronson"], "") == []


def test_an_isbn_catalog_record_vouches_for_an_author_the_ranking_page_omits() -> None:
    # Börsenblatt rows can omit the author; the source's retained DNB record
    # for the row's ISBN names him, and the ranking contract accepts that.
    from autoscout.isbn_author_verification import verified_author
    from tests.test_source_boersenblatt import METADATA

    config = load_validated_config(ROOT / "configs/current-weekly-pilot.json")
    source = next(source for source in config["sources"] if source["name"] == "Börsenblatt Sachbuch Bestseller")
    vouched = verified_author({**METADATA, **source}, "9783462055306", "Jenseits der Brandmauer")
    assert vouched == "Monchi"

    page = {"html": '<h4 class="publication-teaser__title">Jenseits der Brandmauer</h4><span>9783462055306</span>'}
    assert CaptureAuthorText(page).missing_people(["Monchi"], "") == ["Monchi"]
    assert CaptureAuthorText(page).missing_people(["Monchi"], "", vouched_authors=(vouched,)) == []
