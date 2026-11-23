"""The PM deals email parser: message bytes into deal rows and rejections (Issue #943)."""

from __future__ import annotations

import pytest

from autoscout.pm_deal_email import REJECT_NO_AUTHORS
from autoscout.pm_deal_email import REJECT_NO_PLAIN_TEXT
from autoscout.pm_deal_email import REJECT_NO_PUBLISHER
from autoscout.pm_deal_email import REJECT_NO_TITLE
from autoscout.pm_deal_email import REJECT_UNREADABLE_TEXT
from autoscout.pm_deal_email import REJECT_UNTITLED
from autoscout.pm_deal_email import DealRow
from autoscout.pm_deal_email import PmDealEmailError
from autoscout.pm_deal_email import deal_key
from autoscout.pm_deal_email import parse_message
from autoscout.pm_deal_email import parse_paragraph
from tests.pm_deal_fixtures import message
from tests.pm_deal_fixtures import profile


def _rows_by_title() -> dict[str, DealRow]:
    return {row.title: row for row in parse_message(message()).rows}


def test_message_identity_and_counts() -> None:
    parsed = parse_message(message())

    assert parsed.message_id == "<synthetic-1@example.com>"
    assert parsed.message_date == "2026-11-03T14:33:03+00:00"
    assert parsed.newsletter_kind == "daily_deals"
    assert sorted(row.title for row in parsed.rows) == [
        "NIGHT FERRY",
        "THE GLASS ORCHARD",
        "THE LONG WINTER",
        "THE QUIET MACHINE",
        "THE SALT ROADS: HOW TRADE MADE THE SEA",
    ]
    assert sorted(rejection.reason_code for rejection in parsed.rejections) == sorted(
        [REJECT_UNTITLED, REJECT_NO_TITLE, REJECT_NO_PUBLISHER]
    )


def test_a_nonfiction_deal_carries_every_core_field() -> None:
    row = _rows_by_title()["THE SALT ROADS: HOW TRADE MADE THE SEA"]

    assert (row.section, row.country, row.category) == ("Non-fiction", "", "History")
    # The organisation in the credentials ("Harbor Review") is not part of the name.
    assert row.authors == ("Iris Mendel",)
    assert [editor.as_json() for editor in row.editors] == [
        {"name": "Jon Pike", "profile_url": profile(201)},
        {"name": "Lena Moss", "profile_url": profile(202)},
    ]
    assert row.publisher.as_json() == {"name": "Meridian Press", "profile_url": profile(203)}
    assert [agent.name for agent in row.agents] == ["Omar Reyes", "Tess Lowe"]
    # A company name may hold a comma; it ends at its profile link.
    assert row.agency is not None
    assert row.agency.as_json() == {"name": "Park, Lane & Reyes Literary", "profile_url": profile(206)}
    # The territory is the main deal's, not that of the rights already sold.
    assert row.territory == "world English"
    assert row.logline == "a narrative history of the merchants who tied distant ports together"


def test_a_fiction_deal_drops_earlier_book_links_and_credentials() -> None:
    row = _rows_by_title()["THE GLASS ORCHARD"]

    assert (row.section, row.category) == ("Fiction", "Literary")
    assert row.authors == ("Ada Quill",)
    assert row.publisher.name == "Northlight Books"
    assert [agent.name for agent in row.agents] == ["Cora Vale"]
    assert row.territory == "NA"
    assert "bookshop" not in row.logline


def test_two_authors_an_honorific_and_an_agent_without_profile_links() -> None:
    row = _rows_by_title()["THE QUIET MACHINE"]

    assert row.authors == ("Paul Ostrow", "Mina Kaur Sethi")
    assert row.agents[0].as_json() == {"name": "Sam Wynn", "profile_url": ""}
    assert row.agency is not None and row.agency.name == "Wynn Agency"
    assert row.territory == "NA"


def test_a_heading_that_shares_its_block_with_the_first_deal() -> None:
    row = _rows_by_title()["NIGHT FERRY"]

    assert (row.section, row.category) == ("Poetry", "")
    assert row.authors == ("Gil Marr",)


def test_an_international_deal_records_its_country_and_publisher_without_an_editor() -> None:
    row = _rows_by_title()["THE LONG WINTER"]

    assert (row.section, row.country, row.category) == ("International rights", "Latvia", "Non-fiction")
    assert row.editors == ()
    assert row.publisher.name == "Ziedi"
    assert row.territory == "Latvia"
    assert row.agency is not None and row.agency.name == "Baltic Rights"


def test_rejections_name_their_reason_and_carry_no_deal_text() -> None:
    rejections = parse_message(message()).rejections
    by_reason = {rejection.reason_code: rejection for rejection in rejections}

    assert by_reason[REJECT_UNTITLED].as_json() == {
        "section": "Fiction",
        "country": "",
        "category": "General/Other",
        "reason_code": REJECT_UNTITLED,
    }
    assert by_reason[REJECT_NO_PUBLISHER].category == "Science/Technology"
    for rejection in rejections:
        assert set(rejection.as_json()) == {"section", "country", "category", "reason_code"}


def test_the_preamble_and_the_footer_hold_no_deals() -> None:
    parsed = parse_message(message())

    assert all(row.section for row in parsed.rows)
    assert not any("synthetic footer" in row.logline or "reminder" in row.logline for row in parsed.rows)


def test_a_paragraph_without_author_names_is_rejected() -> None:
    paragraph = f"An anonymous author's THE HIDDEN YEAR, a memoir, to Ben Arbor {profile(1)} at Northlight {profile(2)} (world)."

    outcome = parse_paragraph(paragraph, section="Non-fiction", country="", category="Narrative")

    assert not isinstance(outcome, DealRow)
    assert outcome.reason_code == REJECT_NO_AUTHORS


@pytest.mark.parametrize(
    ("before", "expected"),
    [
        ("Ada Quill", ("Ada Quill",)),
        ("Maya R. Linden", ("Maya R. Linden",)),
        ("J.R. Lowe", ("J.R. Lowe",)),
        ("Author of SOME EARLIER BOOK Caroline Harper Vance", ("Caroline Harper Vance",)),
        ("Professor of history at the Lake Institute of Ideas Nora Ellis, PhD", ("Nora Ellis",)),
        ("Board-certified physician Dr. Lena Ortiz", ("Lena Ortiz",)),
        ("Founder of the blog The Reader's Table Tom Ash", ("Tom Ash",)),
        ("Mira Patel and Owen Grey", ("Mira Patel", "Owen Grey")),
        ("Ana Núñez", ("Ana Núñez",)),
        ("Award-winning chef José Smith", ("José Smith",)),
        ("Zoë Brontë-Ôrsted", ("Zoë Brontë-Ôrsted",)),
        ("Juan Manuel García Márquez", ("Juan Manuel García Márquez",)),
        ("Novelist Juan Manuel García Márquez", ("Juan Manuel García Márquez",)),
        ("Bestselling Novelist Juan García", ("Juan García",)),
        # A role word at the end of a name is a surname.
        ("Peter Singer", ("Peter Singer",)),
        ("Philosopher Mara Nurse", ("Mara Nurse",)),
        ("Ana Singer and Owen Grey", ("Ana Singer", "Owen Grey")),
    ],
)
def test_author_names_are_the_trailing_name_words(before: str, expected: tuple[str, ...]) -> None:
    paragraph = f"{before}'s A TITLE, a logline, to Ben Arbor {profile(1)} at Northlight {profile(2)} (world)."

    outcome = parse_paragraph(paragraph, section="Non-fiction", country="", category="History")

    assert isinstance(outcome, DealRow)
    assert outcome.authors == expected


def test_a_title_stops_before_a_mixed_case_word() -> None:
    paragraph = (
        f"Ada Quill's THE LONG GAME, AI-driven essays, to Ben Arbor {profile(1)} at Northlight {profile(2)} (world)."
    )

    outcome = parse_paragraph(paragraph, section="Non-fiction", country="", category="History")

    assert isinstance(outcome, DealRow)
    assert outcome.title == "THE LONG GAME"


@pytest.mark.parametrize(
    ("after_possessive", "title", "logline"),
    [
        ("A TITLE, US history through maps", "A TITLE", "US history through maps"),
        ("EAT, SLEEP, REPEAT, a guide to rest", "EAT, SLEEP, REPEAT", "a guide to rest"),
        ("IN VEIN and book two in a duology", "IN VEIN", "and book two in a duology"),
    ],
)
def test_the_title_ends_where_the_logline_starts(after_possessive: str, title: str, logline: str) -> None:
    paragraph = f"Ada Quill's {after_possessive}, to Ben Arbor {profile(1)} at Northlight {profile(2)} (world)."

    outcome = parse_paragraph(paragraph, section="Non-fiction", country="", category="History")

    assert isinstance(outcome, DealRow)
    assert (outcome.title, outcome.logline) == (title, logline)


def test_deal_terms_are_not_parties() -> None:
    paragraph = (
        f"Ada Quill's THE LONG GAME, a logline, to Ben Arbor {profile(1)} at Northlight {profile(2)}, "
        f"in a pre-empt, at auction, by Cora Vale {profile(3)} at Vale Literary {profile(4)} (NA)."
    )

    outcome = parse_paragraph(paragraph, section="Non-fiction", country="", category="History")

    assert isinstance(outcome, DealRow)
    assert [editor.name for editor in outcome.editors] == ["Ben Arbor"]
    assert [agent.name for agent in outcome.agents] == ["Cora Vale"]
    assert outcome.territory == "NA"


def test_an_editor_named_while_at_the_publisher() -> None:
    paragraph = (
        f"Ada Quill's THE LONG GAME, a logline, to Ben Arbor {profile(1)} while at Northlight {profile(2)} (NA)."
    )

    outcome = parse_paragraph(paragraph, section="Fiction", country="", category="Romance")

    assert isinstance(outcome, DealRow)
    assert outcome.publisher.name == "Northlight"


@pytest.mark.parametrize("by", ["", f", by Cora Vale at Vale Literary {profile(4)}"])
def test_a_person_without_a_link_is_not_part_of_the_company(by: str) -> None:
    paragraph = f"Ada Quill's THE LONG GAME, a logline, to Ben Arbor at Northlight Books {profile(2)}{by} (NA)."

    outcome = parse_paragraph(paragraph, section="Fiction", country="", category="Romance")

    assert isinstance(outcome, DealRow)
    assert [editor.as_json() for editor in outcome.editors] == [{"name": "Ben Arbor", "profile_url": ""}]
    assert outcome.publisher.as_json() == {"name": "Northlight Books", "profile_url": profile(2)}
    if by:
        assert [agent.name for agent in outcome.agents] == ["Cora Vale"]
        assert outcome.agency is not None
        assert outcome.agency.as_json() == {"name": "Vale Literary", "profile_url": profile(4)}


def test_several_unlinked_editors_are_separate_records() -> None:
    paragraph = (
        f"Ada Quill's THE LONG GAME, a logline, to Ben Arbor and Zoe Witt at Northlight Books {profile(2)} (NA)."
    )

    outcome = parse_paragraph(paragraph, section="Fiction", country="", category="Romance")

    assert isinstance(outcome, DealRow)
    assert [editor.name for editor in outcome.editors] == ["Ben Arbor", "Zoe Witt"]
    assert outcome.publisher.name == "Northlight Books"


@pytest.mark.parametrize("after", [" (world).", f", by Cora Vale {profile(3)} at Vale Literary {profile(4)} (world)."])
def test_a_linked_editor_at_an_unlinked_publisher(after: str) -> None:
    paragraph = f"Ada Quill's THE LONG GAME, a logline, to Ben Arbor {profile(1)} at Northlight Books{after}"

    outcome = parse_paragraph(paragraph, section="Fiction", country="", category="Romance")

    assert isinstance(outcome, DealRow)
    assert [editor.as_json() for editor in outcome.editors] == [{"name": "Ben Arbor", "profile_url": profile(1)}]
    assert outcome.publisher.as_json() == {"name": "Northlight Books", "profile_url": ""}
    assert outcome.territory == "world"
    if "Cora" in after:
        assert [agent.name for agent in outcome.agents] == ["Cora Vale"]


@pytest.mark.parametrize("agency", ["A.M. Heath", "St. Brigid Agency"])
def test_an_unlinked_agency_keeps_the_periods_in_its_name(agency: str) -> None:
    paragraph = (
        f"Ada Quill's THE LONG GAME, a logline, to Ben Arbor {profile(1)} at Northlight {profile(2)}, "
        f"by Cora Vale at {agency} (world)."
    )

    outcome = parse_paragraph(paragraph, section="Fiction", country="", category="Romance")

    assert isinstance(outcome, DealRow)
    assert outcome.agency is not None and outcome.agency.name == agency
    assert [agent.name for agent in outcome.agents] == ["Cora Vale"]


def test_the_deal_key_ignores_case_accents_punctuation_and_author_order() -> None:
    assert deal_key("THE SALT ROADS", ["Iris Mendel", "Ana Núñez"], "Meridian Press") == deal_key(
        "The Salt Roads", ["Ana Nunez", "Iris Mendel"], "MERIDIAN PRESS."
    )
    assert deal_key("THE SALT ROADS", ["Iris Mendel"], "Meridian Press") != deal_key(
        "THE SALT ROADS", ["Iris Mendel"], "Cobalt"
    )


def test_a_message_without_a_plain_text_part_is_a_named_failure() -> None:
    from email.message import EmailMessage

    mail = EmailMessage()
    mail["Subject"] = "Today's Deals from PublishersMarketplace.com"
    mail.set_content("<p>only html</p>", subtype="html")

    with pytest.raises(PmDealEmailError) as caught:
        parse_message(mail.as_bytes())
    assert caught.value.reason_code == REJECT_NO_PLAIN_TEXT


def test_a_plain_text_part_in_an_unknown_charset_is_a_named_failure() -> None:
    raw = (
        b"From: daily@publisherslunch.com\r\nSubject: Today's Deals from PublishersMarketplace.com\r\n"
        b"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=x-no-such-charset\r\n\r\nSome text.\r\n"
    )

    with pytest.raises(PmDealEmailError) as caught:
        parse_message(raw)
    assert caught.value.reason_code == REJECT_UNREADABLE_TEXT
