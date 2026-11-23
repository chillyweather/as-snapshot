"""The PM row as a rights lead (Issue #948).

The parser reads the deal size band, deal type, publication, already-sold
territories, contact addresses and earlier titles from the deal paragraph; the
review shows them, and an optional minimum size band filters deals at capture.
Every name, title, address and logline is invented; no PM text is copied.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from autoscout import pm_deal_capture
from autoscout.cli.weekly import run_mvp
from autoscout.pm_deal_email import DealRow
from autoscout.pm_deal_email import Publication
from autoscout.pm_deal_email import parse_paragraph
from autoscout.pm_deal_email import parse_text
from autoscout.pm_precollection import last_pass_status
from autoscout.pm_precollection import run_pass
from autoscout.review_presentation import rights_deal_line
from autoscout.review_rendering import render_review
from autoscout.source_collectors import CollectorRunError
from tests.pm_deal_fixtures import DAILY_DEALS_SUBJECT
from tests.pm_deal_fixtures import deal_text
from tests.pm_deal_fixtures import message
from tests.pm_deal_fixtures import profile
from tests.test_pm_deal_capture import RUN_DATE
from tests.test_pm_deal_capture import _collect
from tests.test_pm_deal_capture import _live
from tests.test_pm_deal_capture import _pm_source
from tests.test_pm_deal_capture import _write_config
from tests.test_pm_precollection import FakeMailbox

TO = f"to Ben Arbor {profile(101)} at Northlight Books {profile(102)}"
BY = f"by Cora Vale {profile(103)} at Vale Literary {profile(104)}"


def _row(paragraph: str, contacts: tuple[str, ...] = ()) -> DealRow:
    outcome = parse_paragraph(paragraph, section="Non-fiction", country="", category="History", contacts=contacts)
    assert isinstance(outcome, DealRow), outcome
    return outcome


def _deal(terms: str = "", tail: str = "", *, credentials: str = "", logline: str = "a synthetic logline") -> str:
    middle = f", {terms}" if terms else ""
    return f"{credentials}Ada Quill's THE SALT ROADS, {logline}, {TO}{middle}, {BY} (world English).{tail}"


# -- Parser seam ---------------------------------------------------------------


def test_a_deal_without_optional_elements_leaves_every_rights_lead_field_empty() -> None:
    row = _row(_deal())

    assert (row.deal_size, row.deal_types, row.book_count, row.publication) == ("", (), 0, None)
    assert (row.rights_also, row.contacts, row.earlier_books) == ((), (), ())
    assert row.territory == "world English"
    assert [editor.name for editor in row.editors] == ["Ben Arbor"]


@pytest.mark.parametrize("band", ["nice", "very nice", "good", "significant", "major"])
def test_each_disclosed_size_band_is_read(band: str) -> None:
    article = "an" if band[0] in "aeiou" else "a"
    assert _row(_deal(f"in {article} {band} deal")).deal_size == band


@pytest.mark.parametrize(
    ("terms", "types", "book_count"),
    [
        ("at auction", ("auction",), 0),
        ("in a pre-empt", ("pre-empt",), 0),
        ("in an exclusive submission", ("exclusive submission",), 0),
        ("in a two-book deal", ("multi-book",), 2),
        ("in a nice three-book deal, at auction", ("auction", "multi-book"), 3),
        ("in a nice multi-book deal", ("multi-book",), 0),
    ],
)
def test_deal_types_and_book_count_are_read(terms: str, types: tuple[str, ...], book_count: int) -> None:
    row = _row(_deal(terms))

    assert row.deal_types == types
    assert row.book_count == book_count


def test_a_size_band_with_a_multi_book_deal_is_read_once() -> None:
    row = _row(_deal("in a very nice two-book deal"))

    assert (row.deal_size, row.deal_types, row.book_count) == ("very nice", ("multi-book",), 2)


def test_a_size_word_that_is_not_a_pm_band_is_not_guessed() -> None:
    assert _row(_deal("in a six-figure deal")).deal_size == ""


@pytest.mark.parametrize(
    ("terms", "publication"),
    [
        ("for publication in fall 2027", Publication(year=2027, season="fall")),
        ("for publication in autumn 2027", Publication(year=2027, season="fall")),
        ("for publication in spring 2028", Publication(year=2028, season="spring")),
        ("for publication in February 2027", Publication(year=2027, month=2)),
        ("for publication in 2029", Publication(year=2029)),
        ("in a nice deal, for publication in winter 2028", Publication(year=2028, season="winter")),
    ],
)
def test_the_publication_is_stored_as_season_or_month_and_year(terms: str, publication: Publication) -> None:
    assert _row(_deal(terms)).publication == publication


def test_a_publication_qualifier_the_parser_does_not_know_is_left_empty() -> None:
    assert _row(_deal("for publication in early 2028")).publication is None


def test_the_already_sold_territories_come_from_the_rights_also_tail() -> None:
    tail = (
        f" Rights also to Kit Hale {profile(207)} at Harbor UK {profile(208)} (UK), at auction;"
        f" and to Vento {profile(209)} (Italy); and to Ostrov {profile(210)} (Russia); and to Vento (Italy)."
    )
    row = _row(_deal(tail=tail))

    assert row.rights_also == ("UK", "Italy", "Russia")
    # An auction in another territory is not the main deal's type.
    assert row.deal_types == ()


def test_deal_words_in_the_logline_are_not_deal_terms() -> None:
    row = _row(_deal(logline="a history of what was sold at auction in a major deal for publication in 1850"))

    assert (row.deal_size, row.deal_types, row.publication) == ("", (), None)


def test_contact_lines_after_the_paragraph_become_contact_addresses() -> None:
    text = f"""**Non-fiction**

**History**

{_deal()}
cora@example.com mailto:cora@example.com Rights:
rights@example.com mailto:rights@example.com
cora@example.com mailto:cora@example.com

{_deal().replace("Ada Quill", "Zed Orr")}

**Manage Your Information**
"""
    rows, rejections = parse_text(text)

    assert not rejections
    assert [row.contacts for row in rows] == [("cora@example.com", "rights@example.com"), ()]


def test_earlier_books_are_the_linked_titles_of_the_credentials() -> None:
    credentials = (
        "NYU historian and author of PAPER LANTERNS https://bookshop.org/a/1/9780000000001 and "
        "WASHINGTON https://bookshop.org/a/1/9780000000002: REFLECTIONS ON A CITY "
        "https://bookshop.org/a/1/9780000000002 "
    )
    row = _row(_deal(credentials=credentials))

    assert row.earlier_books == ("PAPER LANTERNS", "WASHINGTON: REFLECTIONS ON A CITY")
    assert row.authors == ("Ada Quill",)
    assert row.title == "THE SALT ROADS"


def test_an_initial_after_an_earlier_book_belongs_to_the_author() -> None:
    paragraph = (
        "Author of PAPER LANTERNS https://bookshop.org/a/1/9780000000001 J. Ada Quill's THE SALT ROADS, "
        f"a synthetic logline, {TO}, {BY} (world)."
    )

    assert _row(paragraph).earlier_books == ("PAPER LANTERNS",)


def test_capitals_without_a_retail_link_are_not_earlier_books() -> None:
    assert _row(_deal(credentials="NPR host and author of THE OLD HOUSE ")).earlier_books == ()


def test_a_linked_organisation_in_the_credentials_is_not_an_earlier_book() -> None:
    credentials = "NPR https://www.npr.example/hosts host and author of PAPER LANTERNS https://www.amazon.com/dp/1 "

    assert _row(_deal(credentials=credentials)).earlier_books == ("PAPER LANTERNS",)


def test_standalone_punctuation_inside_an_earlier_title_is_kept() -> None:
    credentials = (
        "Author of SALT & PEPPER https://bookshop.org/a/1/1 and NORTH / SOUTH - EAST https://bookshop.org/a/1/2 "
    )

    assert _row(_deal(credentials=credentials)).earlier_books == ("SALT & PEPPER", "NORTH / SOUTH - EAST")


def test_an_earlier_title_with_accented_capitals_is_kept_whole() -> None:
    credentials = "Author of CAFÉ SOCIETY https://bookshop.org/a/1/9780000000003 and ÉTÉ https://bookshop.org/a/1/4 "

    assert _row(_deal(credentials=credentials)).earlier_books == ("CAFÉ SOCIETY", "ÉTÉ")


# -- Capture: older results and the minimum size band --------------------------


def _v1_result(path: Path) -> None:
    """Rewrite a pass result as version 1: the rows lose the rights-lead fields."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = 1
    for row in data["rows"]:
        for field in ("deal_size", "deal_types", "book_count", "publication", "rights_also", "contacts"):
            row.pop(field)
        row.pop("earlier_books")
    path.write_text(json.dumps(data), encoding="utf-8")


def test_a_version_1_result_stays_readable_with_empty_rights_lead_fields(tmp_path: Path) -> None:
    _v1_result(_collect(tmp_path))

    mentions = _live(tmp_path).mentions

    salt_roads = next(m for m in mentions if m["title"].startswith("THE SALT ROADS"))
    deal = salt_roads["rights_deal"]
    assert (deal["deal_size"], deal["publication"], deal["rights_also"], deal["contacts"]) == ("", None, [], [])
    assert deal["editors"] == ["Jon Pike", "Lena Moss"]


def test_the_next_pass_reads_a_version_1_result_again_and_the_doctor_still_reads_it(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    _v1_result(result)
    status = last_pass_status_for(tmp_path)
    assert status["result"]["deals_in_result"] == 5

    _collect(tmp_path)

    data = json.loads(result.read_text(encoding="utf-8"))
    assert data["schema_version"] == 2
    salt_roads = next(row for row in data["rows"] if row["title"].startswith("THE SALT ROADS"))
    assert salt_roads["rights_also"] == ["UK", "Italy"]


def test_a_pass_cut_short_while_upgrading_keeps_the_version_1_rows(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    _v1_result(result)

    _collect(tmp_path, allowance=lambda: 0)

    data = json.loads(result.read_text(encoding="utf-8"))
    assert (data["schema_version"], data["status"], len(data["rows"])) == (2, "partial", 5)
    assert not any("deal_size" in row for row in data["rows"])
    # The kept rows stay readable, and the next full pass gives them the new fields.
    assert len(_live(tmp_path).mentions) == 2
    _collect(tmp_path)
    data = json.loads(result.read_text(encoding="utf-8"))
    assert all("deal_size" in row for row in data["rows"])


def test_an_upgrade_whose_watermark_write_failed_finishes_on_the_next_pass(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    _v1_result(result)
    # The upgrade pass wrote a version 2 result but kept rows it had not yet read again,
    # and the old watermark survived because its own write failed.
    data = json.loads(result.read_text(encoding="utf-8"))
    data["schema_version"] = 2
    result.write_text(json.dumps(data), encoding="utf-8")

    _collect(tmp_path)

    data = json.loads(result.read_text(encoding="utf-8"))
    assert all("deal_size" in row for row in data["rows"])


def _pass_config(tmp_path: Path, **overrides: Any) -> Path:
    """The pass declaration `_collect` writes, with *overrides*."""
    _collect(tmp_path)
    for name in ("collection.json", "watermark.json", "attempt.json"):
        (tmp_path / "pm" / name).unlink(missing_ok=True)
    config = tmp_path / "pm-deal-email.json"
    declared = json.loads(config.read_text(encoding="utf-8"))
    config.write_text(json.dumps({**declared, **overrides}), encoding="utf-8")
    return config


def _run_pass(config: Path, mailbox: FakeMailbox) -> dict[str, Any]:
    return run_pass(
        config,
        now=datetime(2026, 11, 4, 6, 0, tzinfo=UTC),
        opener=lambda *_args: mailbox,
        environ={"PM_USER": "user@example.com", "PM_PASSWORD": "synthetic"},
        key_file=config.parent / "no-key-file",
    )


def test_an_upgrade_over_several_short_passes_advances_and_then_reads_new_mail(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    for uid in (1, 2):
        sent = datetime(2026, 11, uid, 9, 0, tzinfo=UTC)
        raw = message(deal_text(f"THE BOOK OF DAY {uid}"), sent=sent, message_id=f"<{uid}@x>")
        mailbox.add(uid, raw, subject=DAILY_DEALS_SUBJECT, sent=sent)
    config = _pass_config(tmp_path, max_messages_per_pass=1)
    for _ in range(2):
        _run_pass(config, mailbox)
    result = tmp_path / "pm" / "collection.json"
    _v1_result(result)

    # Each short pass reads the next email of the upgrade, never the first one again.
    fetched_before = len(mailbox.fetched)
    _run_pass(config, mailbox)
    _run_pass(config, mailbox)
    assert mailbox.fetched[fetched_before:] == [1, 2]
    data = json.loads(result.read_text(encoding="utf-8"))
    assert all("deal_size" in row for row in data["rows"])
    assert "upgrade_cursor_uid" not in data

    # Once the upgrade is done, a new email is read.
    sent = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    mailbox.add(
        3,
        message(deal_text("THE BOOK OF DAY 3"), sent=sent, message_id="<3@x>"),
        subject=DAILY_DEALS_SUBJECT,
        sent=sent,
    )
    _run_pass(config, mailbox)
    assert mailbox.fetched[-1] == 3


def last_pass_status_for(tmp_path: Path) -> dict[str, Any]:
    (tmp_path / "weekly.json").write_text("{}", encoding="utf-8")
    status = last_pass_status(tmp_path / "weekly.json")
    assert status is not None
    return status


def test_a_version_2_row_with_an_unknown_size_band_is_refused(tmp_path: Path) -> None:
    path = _collect(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["rows"][0]["deal_size"] = "enormous"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(CollectorRunError, match="pm_collection_state"):
        _live(tmp_path)


def _sized_email() -> bytes:
    deals = "\n\n".join(
        f"{author}'s {title}, a synthetic logline, {TO}{terms}, {BY} (world)."
        for author, title, terms in (
            ("Ada Quill", "SMALL DEAL", ", in a nice deal"),
            ("Bo Lark", "NO SIZE", ""),
            ("Cy Rowe", "BIG DEAL", ", in a significant deal"),
            ("Di Moss", "BIGGEST DEAL", ", in a major deal"),
        )
    )
    return message(f"**Non-fiction**\n\n**History**\n\n{deals}\n\n**Manage Your Information**\n")


def test_the_minimum_size_switch_is_off_by_default(tmp_path: Path) -> None:
    _collect(tmp_path, _sized_email())

    titles = {m["title"] for m in _live(tmp_path).mentions}

    assert titles == {"SMALL DEAL", "NO SIZE", "BIG DEAL", "BIGGEST DEAL"}


def test_a_minimum_size_band_filters_smaller_and_undisclosed_deals_by_name(tmp_path: Path) -> None:
    _collect(tmp_path, _sized_email())

    result = _live(tmp_path, pm_min_deal_size_band="significant")

    assert {m["title"] for m in result.mentions} == {"BIG DEAL", "BIGGEST DEAL"}
    dropped = result.debug_metadata["pm_dropped_rows"]
    assert dropped == {"below_minimum_deal_size": 1, "deal_size_not_disclosed": 1}
    # A deal left out by the declared rule is not a shortfall of the source.
    assert result.skipped_item_count == 0
    assert "incomplete_ranking" not in result.debug_metadata


def test_an_unknown_minimum_size_band_is_a_declaration_error() -> None:
    with pytest.raises(pm_deal_capture.PmDealCaptureError, match="pm_declaration"):
        pm_deal_capture.validate_declaration(_pm_source(pm_min_deal_size_band="huge"))


# -- The review row --------------------------------------------------------------


def _rights_lead_email() -> bytes:
    return message(
        f"""**Non-fiction**

**Politics/Current Affairs**

Author of PAPER LANTERNS https://bookshop.org/a/1/9780000000001 Ada Quill's THE NORTHERN LEDGER, a synthetic logline, {TO}, in a good deal, at auction, for publication in spring 2028, {BY} (world English). Rights also to Kit Hale {profile(207)} at Harbor UK {profile(208)} (UK); and to Ostrov {profile(210)} (Russia).
cora@example.com mailto:cora@example.com

**Manage Your Information**
"""
    )


def test_the_review_row_shows_the_rights_lead_in_markdown_html_and_csv(tmp_path: Path) -> None:
    _collect(tmp_path, _rights_lead_email())
    config_path = _write_config(tmp_path, _pm_source(extraction_method="pm-deal-email-replay"))
    out = tmp_path / "out"

    run_mvp(config_path, out, out)
    render_review(out, out, effective_date=RUN_DATE)

    expected = (
        "Northlight Books (редактор: Ben Arbor); агент: Cora Vale (Vale Literary); контакт: cora@example.com; "
        "территория: world English; права уже проданы: UK, Russia; русские права уже проданы; "
        "сделка: good ($100–250 тыс.), аукцион; публикация: весна 2028; "
        "категория PM: Politics/Current Affairs; ранее: PAPER LANTERNS"
    )
    assert expected in (out / "weekly-review.md").read_text(encoding="utf-8")
    assert expected in (out / "weekly-review.html").read_text(encoding="utf-8")
    with (out / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["Rights deal"] for row in rows] == [expected]
    # Retail links never reach the review.
    assert "bookshop.org" not in (out / "weekly-review.html").read_text(encoding="utf-8")


def test_the_line_names_russian_rights_only_when_russia_is_among_the_sold_territories() -> None:
    def line(sold: list[str]) -> str:
        return rights_deal_line([{"rights_deal": {"publisher": "Northlight Books", "rights_also": sold}}])

    assert "русские права уже проданы" in line(["Germany", "Russia"])
    assert "русские права уже проданы" not in line(["Germany", "Brazil"])
    assert "русские права уже проданы" in line(["Russian"])
    assert "русские права уже проданы" not in line(["world excluding Russia"])
    assert "русские права уже проданы" not in line(["world, Russia excluded"])
    assert "русские права уже проданы" not in line(["Russia not included"])
    assert "русские права уже проданы" not in line(["Europe except Russia and Ukraine"])
    assert "русские права" not in line([])


def test_the_line_renders_a_month_a_bare_year_and_a_book_count() -> None:
    def line(**deal: Any) -> str:
        return rights_deal_line([{"rights_deal": {"publisher": "Northlight Books", **deal}}])

    assert "публикация: февраль 2027" in line(publication={"year": 2027, "season": "", "month": 2})
    assert "публикация: 2029" in line(publication={"year": 2029, "season": "", "month": 0})
    assert "сделка: преэмптив, договор на 5 книг" in line(deal_types=["pre-empt", "multi-book"], book_count=5)
    assert "договор на 3 книги" in line(deal_types=["multi-book"], book_count=3)
