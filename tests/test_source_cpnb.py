"""CPNB nonfiction replay regression for onboarding #776."""

import json
from html import escape

from autoscout.replay_fixtures import build_replay_fixture_pair
from autoscout.source_page_identity import ranking_rows

URL = "https://www.debestseller60.nl/non-fictie"
METADATA = {
    "adult_nonfiction_exclusions": ["Kinderboek", "Schoolboek", "Kleurboek"],
    "eligible_candidate_category": "adult nonfiction",
    "ranking_author_selector": ".card__author",
    "ranking_category_marker": "Top Non-Fictie",
    "ranking_evidence_url": "source_page",
    "ranking_expected_raw_row_count": 20,
    "ranking_head_404_get_fallback": True,
    "ranking_identifier_pattern": "ISBN\\s+(?P<identifier>97[89][0-9]{10})",
    "ranking_rank_selector": ".card__position",
    "ranking_rank_source": "selector",
    "ranking_reporting_date_selector": ".board__week",
    "ranking_reporting_period_date_format": "iso-publication-week-wednesday",
    "ranking_reporting_period_max_age_days": 14,
    "ranking_reporting_period_pattern": "(?P<period>Week [0-9]{1,2} - [0-9]{4})",
    "ranking_row_selector": ".card",
    "ranking_title_selector": ".card__title",
}
ENTRIES = [
    {"rank": "1", "title": "Zwanenzang", "author": "Charles Spencer", "identifier": "9789048874002"},
    {"rank": "2", "title": "De babyboomerbiljoenen", "author": "Jona van Loenen", "identifier": "9789000405725"},
    {
        "rank": "3",
        "title": "Het Belachelijk Fijne Boek voor Bange Dagen",
        "author": "Marieke de Goeij",
        "identifier": "9789043943499",
    },
    {
        "rank": "4",
        "title": "De ongelooflijke geschiedenis",
        "author": "Beatrice de Graaf, Stefan Paas & David Boogerd",
        "identifier": "9789044662511",
    },
    {"rank": "5", "title": "Dit wordt toch niks", "author": "Michel van Egmond", "identifier": "9789048879397"},
    {"rank": "6", "title": "Zwemmen in het geld", "author": "Milou Brand", "identifier": "9789493434356"},
    {
        "rank": "7",
        "title": "Ali Niknam",
        "author": "Stijn Bronzwaer, Joris Kooiman & Merijn Rengers",
        "identifier": "9789083629605",
    },
    {"rank": "8", "title": "Met de trein door Europa", "author": "Inge van de Hulsbeek", "identifier": "9789043944243"},
    {"rank": "9", "title": "Je geld of je gegevens", "author": "Anja Shortland", "identifier": "9789083628011"},
    {
        "rank": "10",
        "title": "Atlas van het dialect in Nederland",
        "author": "Kristel Doreleijers, Alexia Kerkhof, Sterre Leufkens & Marc van Oostendorp",
        "identifier": "9789401495219",
    },
    {"rank": "11", "title": "Rare jongens, die Tukkers", "author": "Linda Vermeeren", "identifier": "9789000401451"},
    {
        "rank": "12",
        "title": "Als het thuis niet veilig is",
        "author": "Marloes Alferink & Alwin Grijseels",
        "identifier": "9789021345345",
    },
    {"rank": "13", "title": "Project RUST", "author": "Suzanna Jansen", "identifier": "9789026372865"},
    {"rank": "14", "title": "Protocols", "author": "Andrew D. Huberman", "identifier": "9789043933520"},
    {"rank": "15", "title": "Eindtijdfascisme", "author": "Astra Taylor & Naomi Klein", "identifier": "9789044553826"},
    {"rank": "16", "title": "Hoofd boven water", "author": "Winnifred Noorlander", "identifier": "9789043945134"},
    {"rank": "17", "title": "Dit is AI", "author": "Ilyaz Nasrullah", "identifier": "9789057126314"},
    {"rank": "18", "title": "Wat psychisch lijden is", "author": "Jim van Os", "identifier": "9789079967346"},
    {"rank": "19", "title": "The Let Them Theory", "author": "Mel Robbins", "identifier": "9789043937962"},
    {"rank": "20", "title": "Verder dan vrijheid", "author": "Tamar Valkenier", "identifier": "9789043942164"},
]
HELPER = {
    "version": 1,
    "source_url": "https://www.debestseller60.nl/non-fictie",
    "language": "nl",
    "entries": [
        {
            "title": "Zwanenzang",
            "author": "Charles Spencer",
            "evidence_url": "https://www.debestseller60.nl/non-fictie",
            "english_title": "Swan Song",
            "search_translation": "Лебединая песня",
            "author_transliteration_note": "Чарльз Спенсер",
        },
        {
            "title": "De babyboomerbiljoenen",
            "author": "Jona van Loenen",
            "evidence_url": "https://www.debestseller60.nl/non-fictie",
            "english_title": "The Baby Boomer Billions",
            "search_translation": "Миллиарды поколения бэби-бумеров",
            "author_transliteration_note": "Йона ван Лунен",
        },
        {
            "title": "Het Belachelijk Fijne Boek voor Bange Dagen",
            "author": "Marieke de Goeij",
            "evidence_url": "https://www.debestseller60.nl/non-fictie",
            "english_title": "A Ridiculously Nice Book for Frightening Days",
            "search_translation": "Невероятно добрая книга для тревожных дней",
            "author_transliteration_note": "Марике де Гуй",
        },
    ],
}


def test_cpnb_replay_preserves_twenty_ranked_isbn_identities_and_week():
    html = '<h1 class="board__title">Top Non-Fictie</h1><div class="board__week">Week 40 - 2026</div>'
    for row in ENTRIES:
        html += (
            '<div class="card">'
            f'<span class="card__position">{row["rank"]}</span>'
            f'<h2 class="card__title">{escape(row["title"])}</h2>'
            f'<span class="card__author">{escape(row["author"])}</span>'
            f"<span>ISBN {row['identifier']}</span></div>"
        )
    evidence = dict(
        source_name="CPNB Top Non-Fictie",
        source_url=URL,
        source_page=URL,
        resolved_url=URL,
        collection_mode="source-candidate-probe",
        html=html,
        head_status=404,
        get_status=200,
        reference_date="2026-10-06",
        contract_family="ranking-table",
        contract_metadata=METADATA,
        visible_title_selector=".board__title",
    )
    pair = build_replay_fixture_pair(
        URL,
        "cpnb-non-fictie",
        evidence,
        signal_family="bestseller/ranking",
        signal_strength="top ranking",
        candidate_category="adult nonfiction",
        language="nl",
        helper_metadata=HELPER,
    )
    page = json.loads(pair.page_bytes)
    rows = ranking_rows(page["html"], METADATA)
    assert len(rows) == 20
    assert [row.rank for row in rows] == [str(rank) for rank in range(1, 21)]
    assert [(row.title, row.author, row.identifier) for row in rows] == [
        (row["title"], row["author"], row["identifier"]) for row in ENTRIES
    ]
    assert "Week 40 - 2026" in page["html"]
    mentions = json.loads(pair.extraction_bytes)["mentions"]
    assert len(mentions) == 3
    assert [mention["title"] for mention in mentions] == [row["title"] for row in ENTRIES[:3]]
