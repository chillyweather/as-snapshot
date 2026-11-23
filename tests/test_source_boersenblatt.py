"""Börsenblatt missing-author ISBN replay regression for onboarding #775.

Synthetic minimal markup uses the accepted source selectors and retained DNB
bibliographic proof; no live capture or network access is needed.
"""

import json

import pytest

from autoscout.replay_fixtures import build_replay_fixture_pair
from autoscout.source_page_identity import ranking_mention_is_grounded
from autoscout.source_page_identity import ranking_rows

METADATA = {
    "adult_nonfiction_exclusions": ["Kinderbuch", "Malbuch", "Schulbuch"],
    "eligible_candidate_category": "adult nonfiction",
    "ranking_author_evidence_json": '[{"provider":"dnb","isbn":"9783462055306","title":"Jenseits der Brandmauer","author":"Monchi","catalog_author":"Gorkow, Jan","evidence_url":"https://d-nb.info/1395508348","request_url":"https://services.dnb.de/sru/dnb?version=1.1&operation=searchRetrieve&query=isbn%3D9783462055306&recordSchema=MARC21-xml&maximumRecords=2","record_xml":"<ns0:record xmlns:ns0=\\"http://www.loc.gov/MARC21/slim\\"><ns0:controlfield tag=\\"001\\">1395508348</ns0:controlfield>\\n    <ns0:datafield tag=\\"020\\" ind1=\\" \\" ind2=\\" \\"><ns0:subfield code=\\"a\\">9783462055306</ns0:subfield>\\n      </ns0:datafield><ns0:datafield tag=\\"020\\" ind1=\\" \\" ind2=\\" \\"><ns0:subfield code=\\"a\\">3462055305</ns0:subfield>\\n      </ns0:datafield><ns0:datafield tag=\\"100\\" ind1=\\"1\\" ind2=\\" \\"><ns0:subfield code=\\"a\\">Gorkow, Jan</ns0:subfield>\\n      <ns0:subfield code=\\"4\\">aut</ns0:subfield>\\n      </ns0:datafield><ns0:datafield tag=\\"245\\" ind1=\\"1\\" ind2=\\"0\\"><ns0:subfield code=\\"a\\">Jenseits der Brandmauer</ns0:subfield>\\n      <ns0:subfield code=\\"c\\">Monchi</ns0:subfield>\\n    </ns0:datafield></ns0:record>","record_sha256":"1b480dc9fee63b26484042ae5812b0402261725234982056b75c7ad3c5a0a62e"}]',
    "ranking_author_selector": ".publication-teaser__authors",
    "ranking_category_marker": "Sachbuch Bestseller",
    "ranking_evidence_url": "source_page",
    "ranking_expected_raw_row_count": 25,
    "ranking_identifier_pattern": "(?P<identifier>97[89][0-9]{10})",
    "ranking_identifier_selector": ".publication-teaser__isbn",
    "ranking_rank_selector": ".position-line__current-position",
    "ranking_rank_source": "selector",
    "ranking_reporting_date_selector": ".meta-date",
    "ranking_reporting_period_date_format": "%d.%m.%Y",
    "ranking_reporting_period_max_age_days": 14,
    "ranking_reporting_period_pattern": "(?P<period>[0-9]{2}\\.[0-9]{2}\\.[0-9]{4}) [0-9]{2}:[0-9]{2} Uhr",
    "ranking_row_selector": ".teaser-group-vertical__items > .teaser-group-vertical__item",
    "ranking_title_selector": ".publication-teaser__title",
}

URL = "https://www.boersenblatt.net/news/bestseller/sachbuch"


def test_missing_author_stays_missing_in_replay_and_requires_isbn_evidence():
    source = dict(METADATA, ranking_expected_raw_row_count=1)
    html = '<h1>Wochencharts Sachbuch Bestseller</h1><div class="meta-date">28.09.2026 15:25 Uhr</div><div class="teaser-group-vertical__items"><div class="teaser-group-vertical__item"><span class="position-line__current-position">1</span><h4 class="publication-teaser__title">Jenseits der Brandmauer</h4><span class="publication-teaser__isbn">9783462055306</span></div></div>'
    evidence = dict(
        source_name="Börsenblatt Sachbuch Bestseller",
        source_url=URL,
        source_page=URL,
        resolved_url=URL,
        collection_mode="source-candidate-probe",
        html=html,
        head_status=200,
        get_status=200,
        reference_date="2026-10-06",
        contract_family="ranking-table",
        contract_metadata=source,
    )
    helper = dict(
        version=1,
        source_url=URL,
        language="de",
        entries=[
            dict(
                title="Jenseits der Brandmauer",
                author="Monchi",
                evidence_url=URL,
                english_title="Beyond the Firewall",
                search_translation="По ту сторону политического барьера",
                author_transliteration_note="Монхи",
            )
        ],
    )
    pair = build_replay_fixture_pair(
        URL,
        "boersenblatt-author-proof",
        evidence,
        signal_family="bestseller/ranking",
        signal_strength="top ranking",
        candidate_category="adult nonfiction",
        language="de",
        helper_metadata=helper,
    )
    page = json.loads(pair.page_bytes)
    assert ranking_rows(page["html"], source)[0].author == "Monchi"
    without_proof = dict(source)
    without_proof.pop("ranking_author_evidence_json")
    assert ranking_rows(page["html"], without_proof)[0].author == ""
    assert json.loads(pair.extraction_bytes)["mentions"][0]["primary_authors"] == ["Monchi"]


def test_extraction_instructions_allow_only_the_explicit_isbn_author_fallback():
    from autoscout.source_collectors.contract import extraction_contract

    source = dict(METADATA, language="de", contract_family="ranking-table")
    rules = extraction_contract(source)["rules"]
    assert any("ranking_author_evidence_json" in rule and "exact ISBN and title" in rule for rule in rules)
    assert any("never replace a visible author" in rule for rule in rules)
    assert any("catalog_author" in rule for rule in rules)
    default_source = dict(source)
    default_source.pop("ranking_author_evidence_json")
    default_rules = extraction_contract(default_source)["rules"]
    assert default_rules[0] == "Use only the configured source page HTML and obey the source_metadata constraints."
    assert not any("ranking_author_evidence_json" in rule for rule in default_rules)


def test_extraction_is_told_to_read_the_week_from_the_dated_element_the_validator_reads():
    """Issue #787: the page also states the week as "KW 40 2026" in the list's
    aria-label.  The validator accepts only the date in ``.meta-date``, so the
    contract has to name that element instead of leaving the form open."""
    from autoscout.source_collectors.contract import extraction_contract

    source = dict(METADATA, language="de", contract_family="ranking-table", ranking_expected_raw_row_count=1)
    contract = extraction_contract(source)
    rules = " ".join(contract["rules"])
    assert "ranking_reporting_date_selector '.meta-date'" in rules
    assert "ranking_reporting_period_pattern captures" in rules
    assert "aria-label" in rules
    assert "'.meta-date'" in contract["required_output_shape"]["mentions"][0]["source_week"]

    html = (
        '<h1>Wochencharts Sachbuch Bestseller</h1><div class="meta-date">28.09.2026 08:27 Uhr</div>'
        '<ol aria-label="Bestseller HC Sachbuch KW 40 2026" class="teaser-group-vertical__items">'
        '<li class="teaser-group-vertical__item"><span class="position-line__current-position">1</span>'
        '<h4 class="publication-teaser__title">Jenseits der Brandmauer</h4>'
        '<span class="publication-teaser__authors">Monchi</span>'
        '<span class="publication-teaser__isbn">9783462055306</span></li></ol>'
    )

    def grounded(source_week: str) -> bool:
        return ranking_mention_is_grounded(
            html,
            title="Jenseits der Brandmauer",
            isbn="9783462055306",
            current_rank="1",
            source_week=source_week,
            primary_authors=["Monchi"],
            ranking_source=source,
        )

    assert grounded("28.09.2026")
    assert not grounded("KW 40 2026")
    assert not grounded("28.09.2026 08:27 Uhr")


ISBN_ROW_HTML = (
    '<h1>Wochencharts Sachbuch Bestseller</h1><div class="meta-date">28.09.2026 08:27 Uhr</div>'
    '<ol class="teaser-group-vertical__items">'
    '<li class="teaser-group-vertical__item"><span class="position-line__current-position">1</span>'
    '<h4 class="publication-teaser__title">Jenseits der Brandmauer</h4>'
    '<span class="publication-teaser__authors">Monchi</span>'
    '<span class="publication-teaser__isbn">ISBN 9783462055306</span></li></ol>'
)


@pytest.mark.parametrize(
    ("isbn", "grounded"),
    [
        ("ISBN 9783462055306", True),
        ("9783462055306", True),
        ("978-3-462-05530-6", True),
        ("ISBN 9783462055307", False),
        ("9783462055306 9783462014846", False),
        ("ISBN", False),
    ],
)
def test_ranking_row_isbn_is_read_as_one_valid_isbn_inside_the_value(isbn, grounded):
    """Issue #827: the page labels the ISBN, and a model that copies the label
    must not lose the row, while a value without exactly one valid ISBN still
    fails closed."""
    source = dict(METADATA, ranking_expected_raw_row_count=1)
    assert (
        ranking_mention_is_grounded(
            ISBN_ROW_HTML,
            title="Jenseits der Brandmauer",
            isbn=isbn,
            current_rank="1",
            source_week="28.09.2026",
            primary_authors=["Monchi"],
            ranking_source=source,
        )
        is grounded
    )


def test_ranking_row_isbn_contract_asks_for_the_bare_isbn():
    from autoscout.source_collectors.contract import extraction_contract

    source = dict(METADATA, language="de", contract_family="ranking-table")
    isbn_shape = extraction_contract(source)["required_output_shape"]["mentions"][0]["isbn"]
    assert "exactly as shown" not in isbn_shape
    assert "without" in isbn_shape and "ISBN" in isbn_shape


def test_accepted_ranking_mention_stores_the_bare_isbn():
    """A labelled ISBN that grounds is stored bare, so the book keeps the
    identity it had in weeks the model returned the digits alone."""
    from autoscout.source_collectors.agent_mentions import agent_extraction_mentions_from_response

    source = dict(METADATA, ranking_expected_raw_row_count=1, contract_family="ranking-table", url=URL, language="en")
    page = {"html": ISBN_ROW_HTML, "source_page": URL, "resolved_url": URL, "reference_date": "2026-09-28"}
    entry = {
        "title": "Jenseits der Brandmauer",
        "primary_authors": ["Monchi"],
        "isbn": "ISBN 9783462055306",
        "current_rank": 1,
        "source_week": "28.09.2026",
        "source_url": URL,
        "evidence_url": URL,
        "signal_type": "bestseller/ranking",
        "evidence_note": "Rank 1 in the Sachbuch table for 28.09.2026.",
        "candidate_category": "adult nonfiction",
    }
    mentions, dropped = agent_extraction_mentions_from_response(page, {"mentions": [entry]}, source, "2026-09-29")
    assert dropped == []
    assert [mention["isbn"] for mention in mentions] == ["9783462055306"]
