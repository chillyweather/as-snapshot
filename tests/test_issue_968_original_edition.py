"""Original edition core: translator credit, Open Library confirmation, record (#968)."""

from __future__ import annotations

from urllib.parse import parse_qs
from urllib.parse import urlsplit

from autoscout.original_edition import IS_TRANSLATION_UNKNOWN
from autoscout.original_edition import IS_TRANSLATION_YES
from autoscout.original_edition import OPEN_LIBRARY_LANGUAGE
from autoscout.original_edition import ORIGINAL_EDITION_KEYS
from autoscout.original_edition import PROPOSAL_RESPONSE_SCHEMA
from autoscout.original_edition import STATUS_CONFIRMED
from autoscout.original_edition import STATUS_NOT_CHECKED
from autoscout.original_edition import STATUS_NOT_FOUND
from autoscout.original_edition import build_original_edition
from autoscout.original_edition import confirm_original
from autoscout.original_edition import open_library_search_url
from autoscout.original_edition import translator_credit
from autoscout.original_edition import validate_original_edition
from autoscout.original_edition import validate_proposal_response
from autoscout.scouting_candidates import ScoutingCandidate

# Real Open Library search documents observed on 2026-11-07.
ATHILL = {
    "key": "/works/OL1W",
    "title": "Somewhere towards the end",
    "author_name": ["Diana Athill"],
    "first_publish_year": 2008,
    "language": ["eng", "cat"],
}
DIAMANDIS = {
    "key": "/works/OL45397701W",
    "title": "We Are As Gods",
    "author_name": ["Peter H. Diamandis"],
}
MALLABY = {
    "key": "/works/OL45068494W",
    "title": "The Infinity Machine",
    "author_name": ["Sebastian Mallaby"],
    "first_publish_year": 2026,
}
SERVICE = [
    {
        "key": "/works/OL2W",
        "title": "Stalin",
        "author_name": ["Robert Service"],
        "first_publish_year": 2004,
        "language": ["eng", "chi", "und"],
    },
    {
        "key": "/works/OL3W",
        "title": "Stalin",
        "author_name": ["Robert Service", "Susana Beatriz Cella"],
        "first_publish_year": 2002,
    },
]
JACKSON = [
    {
        "key": "/works/OL8112287W",
        "title": "Moon Walk",
        "author_name": ["Michael Jackson"],
        "first_publish_year": 1988,
    }
]


def _proposal(title: str, language: str = "en") -> dict[str, str]:
    return {"original_title": title, "original_language": language}


def _candidate(title: str, authors: list[str], year: int = 2026) -> dict[str, object]:
    return {"title": title, "primary_authors": authors, "publication_year": year}


def _confirmation(
    proposal_title: str, language: str, candidate: dict[str, object], docs: list[dict[str, object]]
) -> dict[str, object]:
    return confirm_original(_proposal(proposal_title, language), candidate, docs)


# ── Translator credit ────────────────────────────────────────────────────


def test_nuevoslibros_line_names_the_translator() -> None:
    credit = translator_credit(
        "Ahora que se acerca el final Diana Athill Traducción de Regina López Muñoz Libros del Asteroide"
    )

    assert credit is not None
    assert credit.marker == "Traducción de"
    assert credit.translator == "Regina López Muñoz"
    assert credit.stated_original_title == ""


def test_table_label_takes_the_name_from_the_next_cell() -> None:
    markup = (
        "<table><tr><td>Traductor</td><td>Alexandre Casanovas López</td></tr></table>"
        '<script>var labels = {"traductor":"Traductor","tituloOriginal":"Título original:"};</script>'
    )

    credit = translator_credit(markup)

    assert credit is not None
    assert credit.marker == "Traductor"
    assert credit.translator == "Alexandre Casanovas López"


def test_hidden_script_label_is_not_a_credit() -> None:
    assert translator_credit('<script>var labels = {"traductor":"Traductor"};</script>') is None


def test_prose_mention_of_traduccion_is_not_a_credit() -> None:
    assert translator_credit("Traducción, navegación, simulación") is None


def test_stated_original_title_counts_as_a_credit() -> None:
    credit = translator_credit("Título original: The Psychology of Money")

    assert credit is not None
    assert credit.translator == ""
    assert credit.marker == "Título original"
    assert credit.stated_original_title == "The Psychology of Money"


def test_translator_and_stated_original_title_are_both_reported() -> None:
    markup = "<p>Traducción de Regina López Muñoz</p><p>Título original: Somewhere Towards the End</p>"

    credit = translator_credit(markup)

    assert credit is not None
    assert credit.translator == "Regina López Muñoz"
    assert credit.stated_original_title == "Somewhere Towards the End"


def test_entity_encoded_marker_and_name_are_read() -> None:
    credit = translator_credit("<p>Traducci&oacute;n de Regina L&oacute;pez Mu&ntilde;oz</p>")

    assert credit is not None
    assert credit.translator == "Regina López Muñoz"


def test_translation_marker_needs_a_capitalised_name() -> None:
    assert translator_credit("Traducción de la edición española") is None


def test_marker_does_not_match_inside_a_longer_word() -> None:
    assert translator_credit("Übersetzungen Anna Schmidt") is None

    credit = translator_credit("Traductores Ana Pérez y Luis Gil")

    assert credit is not None
    assert credit.marker == "Traductores"
    assert credit.translator.startswith("Ana Pérez")


def test_stated_original_title_in_a_separate_cell() -> None:
    markup = "<table><tr><td>Título original:</td><td>We Are as Gods</td></tr></table>"

    credit = translator_credit(markup)

    assert credit is not None
    assert credit.marker == "Título original"
    assert credit.stated_original_title == "We Are as Gods"


def test_empty_translator_cell_does_not_take_the_next_row_label() -> None:
    markup = "<table><tr><td>Traductor</td><td></td></tr><tr><td>Autor</td><td>Diana Athill</td></tr></table>"

    assert translator_credit(markup) is None


def test_empty_stated_title_cell_does_not_take_the_next_row_label() -> None:
    markup = "<table><tr><td>Título original</td><td></td></tr><tr><td>ISBN</td><td>9788491054321</td></tr></table>"

    assert translator_credit(markup) is None


def test_table_label_reads_the_value_from_its_own_row_only() -> None:
    markup = (
        "<table><tr><td>Traductor</td><td>Alexandre Casanovas López</td></tr>"
        "<tr><td>Autor</td><td>Diana Athill</td></tr></table>"
    )

    credit = translator_credit(markup)

    assert credit is not None
    assert credit.translator == "Alexandre Casanovas López"


def test_definition_list_stated_title_takes_the_following_dd() -> None:
    credit = translator_credit("<dl><dt>Título original</dt><dd>We Are as Gods</dd></dl>")

    assert credit is not None
    assert credit.marker == "Título original"
    assert credit.stated_original_title == "We Are as Gods"


def test_empty_definition_list_dd_does_not_take_the_next_dt() -> None:
    markup = "<dl><dt>Traductor</dt><dd></dd><dt>Autor</dt><dd>X</dd></dl>"

    assert translator_credit(markup) is None


def test_div_label_does_not_take_a_following_marker_line_as_a_name() -> None:
    credit = translator_credit("<div>Traductor:</div><div>Título original: Foo</div>")

    assert credit is not None
    assert credit.translator == ""
    assert credit.stated_original_title == "Foo"


def test_div_label_still_takes_the_following_line_as_a_name() -> None:
    credit = translator_credit("<div>Traductor:</div><div>Ana Pérez</div>")

    assert credit is not None
    assert credit.translator == "Ana Pérez"


def test_stated_original_title_marker_alone_is_not_a_credit() -> None:
    assert translator_credit("Título original:") is None


def test_traduccion_del_idioma_names_the_translator() -> None:
    credit = translator_credit("Traducción del inglés de Regina López Muñoz")

    assert credit is not None
    assert credit.marker == "Traducción del inglés de"
    assert credit.translator == "Regina López Muñoz"


def test_traducao_do_idioma_names_the_translator() -> None:
    credit = translator_credit("Tradução do inglês de Maria Silva")

    assert credit is not None
    assert credit.marker == "Tradução do inglês de"
    assert credit.translator == "Maria Silva"


def test_traduzione_dall_idioma_names_the_translator() -> None:
    credit = translator_credit("Traduzione dall'inglese di Paolo Rossi")

    assert credit is not None
    assert credit.marker == "Traduzione dall'inglese di"
    assert credit.translator == "Paolo Rossi"


def test_vertaald_uit_het_idioma_names_the_translator() -> None:
    credit = translator_credit("Vertaald uit het Engels door Jan de Vries")

    assert credit is not None
    assert credit.marker == "Vertaald uit het Engels door"
    assert credit.translator.startswith("Jan")


def test_oversatt_fra_idioma_names_the_translator() -> None:
    credit = translator_credit("Oversatt fra engelsk av Kari Nordmann")

    assert credit is not None
    assert credit.marker == "Oversatt fra engelsk av"
    assert credit.translator == "Kari Nordmann"


# ── Open Library confirmation ────────────────────────────────────────────


def test_athill_confirms_with_an_english_language_and_year() -> None:
    record = _confirmation(
        "Somewhere Towards the End",
        "en",
        _candidate("Ahora que se acerca el final", ["Diana Athill"]),
        [ATHILL],
    )

    assert record == {
        "status": STATUS_CONFIRMED,
        "original_title": "Somewhere towards the end",
        "original_language": "en",
        "language_confirmed": True,
        "original_first_published_year": 2008,
        "confirmation_url": "https://openlibrary.org/works/OL1W",
    }


def test_diamandis_middle_initial_matches_and_missing_language_is_unconfirmed() -> None:
    record = _confirmation(
        "We Are as Gods",
        "en",
        _candidate("Somos como dioses", ["Peter Diamandis", "Philip Kotler"]),
        [DIAMANDIS],
    )

    assert record["status"] == STATUS_CONFIRMED
    assert record["original_title"] == "We Are As Gods"
    assert record["language_confirmed"] is False
    assert record["original_first_published_year"] is None
    assert record["confirmation_url"] == "https://openlibrary.org/works/OL45397701W"


def test_mallaby_confirms_in_the_candidate_year() -> None:
    record = _confirmation(
        "The Infinity Machine",
        "en",
        _candidate("La máquina infinita", ["Sebastian Mallaby"]),
        [MALLABY],
    )

    assert record["status"] == STATUS_CONFIRMED
    assert record["original_title"] == "The Infinity Machine"
    assert record["original_first_published_year"] == 2026
    assert record["confirmation_url"] == "https://openlibrary.org/works/OL45068494W"


def test_service_prefers_the_confirmed_language_over_an_earlier_unconfirmed_year() -> None:
    record = _confirmation("Stalin", "en", _candidate("Stalin", ["Robert Service"]), SERVICE)

    assert record["status"] == STATUS_CONFIRMED
    # 2002 has no language list (it is a Spanish translation); 2004 lists
    # "eng", so the confirmed-language work wins even though it is later.
    assert record["confirmation_url"] == "https://openlibrary.org/works/OL2W"
    assert record["original_first_published_year"] == 2004
    assert record["language_confirmed"] is True


def test_jackson_moonwalk_confirms() -> None:
    record = _confirmation("Moon Walk", "en", _candidate("Moonwalk", ["Michael Jackson"]), JACKSON)

    assert record["status"] == STATUS_CONFIRMED
    assert record["original_title"] == "Moon Walk"
    assert record["original_first_published_year"] == 1988
    assert record["confirmation_url"] == "https://openlibrary.org/works/OL8112287W"


def test_a_spanish_only_document_never_confirms_an_english_proposal() -> None:
    docs = [dict(ATHILL, language=["spa"])]

    record = _confirmation(
        "Somewhere Towards the End", "en", _candidate("Ahora que se acerca el final", ["Diana Athill"]), docs
    )

    assert record["status"] == STATUS_NOT_FOUND
    assert record["original_title"] == ""
    assert record["confirmation_url"] == ""


def test_a_different_author_with_the_same_surname_prefix_does_not_confirm() -> None:
    docs = [
        {
            "key": "/works/OL5W",
            "title": "Stalin",
            "author_name": ["Robert Greene"],
            "first_publish_year": 2004,
        }
    ]

    record = _confirmation("Stalin", "en", _candidate("Stalin", ["Robert Service"]), docs)

    assert record["status"] == STATUS_NOT_FOUND


def test_a_document_year_after_the_candidate_year_does_not_confirm() -> None:
    docs = [dict(MALLABY, first_publish_year=2027)]

    record = _confirmation("The Infinity Machine", "en", _candidate("La máquina infinita", ["Sebastian Mallaby"]), docs)

    assert record["status"] == STATUS_NOT_FOUND


def test_empty_documents_are_not_found() -> None:
    record = _confirmation("Stalin", "en", _candidate("Stalin", ["Robert Service"]), [])

    assert record["status"] == STATUS_NOT_FOUND


def test_corona_nostra_is_not_found() -> None:
    record = _confirmation("Corona Nostra", "es", _candidate("Corona Nostra", ["Nieves Concostrina"]), [])

    assert record["status"] == STATUS_NOT_FOUND
    assert set(record) == {
        "status",
        "original_title",
        "original_language",
        "language_confirmed",
        "original_first_published_year",
        "confirmation_url",
    }


def test_malformed_documents_never_raise() -> None:
    docs = [
        None,
        {},
        {"title": None},
        {"title": "Stalin", "author_name": "Robert Service"},
        {"key": "OL9W", "title": "Stalin", "author_name": ["Robert Service"]},
    ]

    record = confirm_original(_proposal("Stalin"), _candidate("Stalin", ["Robert Service"]), docs)

    assert record["status"] == STATUS_NOT_FOUND


def test_search_url_uses_the_main_title_and_the_surname() -> None:
    url = open_library_search_url("Somewhere Towards the End: A Memoir", "Athill, Diana")

    parts = urlsplit(url)
    parameters = parse_qs(parts.query)
    assert parts.scheme == "https"
    assert parts.netloc == "openlibrary.org"
    assert parts.path == "/search.json"
    assert parameters["title"] == ["somewhere towards the end"]
    assert parameters["author"] == ["Athill"]
    assert parameters["fields"] == ["key,title,author_name,first_publish_year,language"]
    assert parameters["limit"] == ["10"]


def test_open_library_language_codes_cover_the_required_languages() -> None:
    expected = {
        "en": "eng",
        "es": "spa",
        "pt": "por",
        "it": "ita",
        "de": "ger",
        "fr": "fre",
        "nl": "dut",
        "ca": "cat",
        "no": "nor",
        "pl": "pol",
        "cs": "cze",
        "sv": "swe",
        "da": "dan",
        "fi": "fin",
        "ru": "rus",
        "ja": "jpn",
        "zh": "chi",
        "ko": "kor",
        "he": "heb",
        "ar": "ara",
        "hu": "hun",
    }

    for language, code in expected.items():
        assert OPEN_LIBRARY_LANGUAGE[language] == code


# ── The record ───────────────────────────────────────────────────────────


def _confirmed_record() -> dict[str, object]:
    confirmation = _confirmation(
        "Somewhere Towards the End",
        "en",
        _candidate("Ahora que se acerca el final", ["Diana Athill"]),
        [ATHILL],
    )
    return build_original_edition(credit_url="https://nuevoslibros.test/athill", confirmation=confirmation)


def test_builder_sets_the_translation_credit_and_confirmation() -> None:
    record = _confirmed_record()

    assert list(record) == list(ORIGINAL_EDITION_KEYS)
    assert record["is_translation"] == IS_TRANSLATION_YES
    assert record["translation_evidence_url"] == "https://nuevoslibros.test/athill"
    assert record["status"] == STATUS_CONFIRMED
    assert record["original_title"] == "Somewhere towards the end"
    assert validate_original_edition(record)


def test_builder_without_a_credit_or_confirmation_is_unknown_and_not_checked() -> None:
    record = build_original_edition(credit_url="", confirmation=None)

    assert record["is_translation"] == IS_TRANSLATION_UNKNOWN
    assert record["status"] == STATUS_NOT_CHECKED
    assert record["original_title"] == ""
    assert record["confirmation_url"] == ""
    assert validate_original_edition(record)


def test_validator_rejects_a_missing_or_extra_key() -> None:
    record = _confirmed_record()

    missing = dict(record)
    missing.pop("confirmation_url")
    extra = dict(record, unexpected="value")

    assert not validate_original_edition(missing)
    assert not validate_original_edition(extra)
    assert not validate_original_edition("not a dict")


def test_validator_rejects_an_unknown_translation_value() -> None:
    assert not validate_original_edition(dict(_confirmed_record(), is_translation="no"))


def test_validator_rejects_a_confirmed_record_without_a_title_or_library_url() -> None:
    record = _confirmed_record()

    assert not validate_original_edition(dict(record, original_title=""))
    assert not validate_original_edition(dict(record, confirmation_url=""))
    assert not validate_original_edition(dict(record, confirmation_url="https://example.test/works/OL1W"))
    assert not validate_original_edition(dict(record, confirmation_url="https://openlibrary.org.evil.test/x"))


def test_validator_rejects_a_confirmed_record_with_an_unknown_language() -> None:
    assert not validate_original_edition(dict(_confirmed_record(), original_language="xx"))


def test_validator_requires_a_confirmed_record_to_be_a_translation() -> None:
    assert not validate_original_edition(dict(_confirmed_record(), is_translation=IS_TRANSLATION_UNKNOWN))


def test_validator_rejects_a_not_found_record_that_names_an_original() -> None:
    record = build_original_edition(credit_url="", confirmation=None)

    assert not validate_original_edition(dict(record, original_title="Somewhere towards the end"))
    assert not validate_original_edition(dict(record, confirmation_url="https://openlibrary.org/works/OL1W"))


def test_builder_output_validates_for_every_supported_shape() -> None:
    credit_url = "https://nuevoslibros.test/athill"
    confirmed = _confirmation(
        "Somewhere Towards the End",
        "en",
        _candidate("Ahora que se acerca el final", ["Diana Athill"]),
        [ATHILL],
    )
    not_found = _confirmation("Corona Nostra", "es", _candidate("Corona Nostra", ["Nieves Concostrina"]), [])

    assert validate_original_edition(build_original_edition(credit_url=credit_url, confirmation=confirmed))
    assert validate_original_edition(build_original_edition(credit_url=credit_url, confirmation=not_found))
    assert validate_original_edition(build_original_edition(credit_url=credit_url, confirmation=None))
    assert validate_original_edition(build_original_edition(credit_url="", confirmation=None))


def test_validator_requires_a_real_evidence_url_for_a_translation() -> None:
    record = _confirmed_record()

    assert not validate_original_edition(dict(record, translation_evidence_url=""))
    assert not validate_original_edition(dict(record, translation_evidence_url="nuevoslibros.test/athill"))
    assert not validate_original_edition(dict(record, translation_evidence_url="ftp://nuevoslibros.test/athill"))
    assert not validate_original_edition(dict(record, translation_evidence_url="https://["))
    assert validate_original_edition(dict(record, translation_evidence_url="http://nuevoslibros.test/athill"))


def test_validator_requires_an_unknown_translation_to_have_no_evidence() -> None:
    record = build_original_edition(credit_url="", confirmation=None)

    assert validate_original_edition(record)
    assert not validate_original_edition(dict(record, translation_evidence_url="https://nuevoslibros.test/athill"))


def test_validator_rejects_a_not_found_record_with_confirmation_content() -> None:
    record = build_original_edition(
        credit_url="https://nuevoslibros.test/athill",
        confirmation=_confirmation("Corona Nostra", "es", _candidate("Corona Nostra", ["Nieves Concostrina"]), []),
    )

    assert validate_original_edition(record)
    assert not validate_original_edition(dict(record, original_language="es"))
    assert not validate_original_edition(dict(record, language_confirmed=True))
    assert not validate_original_edition(dict(record, original_first_published_year=2008))


def test_validator_rejects_a_not_checked_record_with_confirmation_content() -> None:
    record = build_original_edition(credit_url="https://nuevoslibros.test/athill", confirmation=None)

    assert validate_original_edition(record)
    assert not validate_original_edition(dict(record, original_language="en"))
    assert not validate_original_edition(dict(record, language_confirmed=True))
    assert not validate_original_edition(dict(record, original_first_published_year=2008))


def test_validator_rejects_a_malformed_confirmation_url() -> None:
    assert not validate_original_edition(dict(_confirmed_record(), confirmation_url="https://["))


# ── Model proposal contract ──────────────────────────────────────────────


def test_proposal_schema_forbids_additional_properties_at_every_level() -> None:
    assert PROPOSAL_RESPONSE_SCHEMA["additionalProperties"] is False
    items = PROPOSAL_RESPONSE_SCHEMA["properties"]["proposals"]["items"]
    assert items["additionalProperties"] is False
    assert items["required"] == ["candidate_id", "original_title", "original_language"]
    assert PROPOSAL_RESPONSE_SCHEMA["required"] == ["proposals"]


def test_a_valid_batch_keeps_known_proposals_and_drops_unknown_ones() -> None:
    payload = {
        "proposals": [
            {"candidate_id": "book-a", "original_title": "We Are as Gods", "original_language": "en"},
            {"candidate_id": "book-b", "original_title": None, "original_language": None},
            {"candidate_id": "book-c", "original_title": "Corona Nostra", "original_language": "xx"},
            {"candidate_id": "book-d", "original_title": "   ", "original_language": "en"},
            {"candidate_id": "not-requested", "original_title": "Somewhere", "original_language": "en"},
        ]
    }

    assert validate_proposal_response(payload, {"book-a", "book-b", "book-c", "book-d"}) == {
        "book-a": {"original_title": "We Are as Gods", "original_language": "en"}
    }


def test_a_null_proposal_is_dropped() -> None:
    payload = {"proposals": [{"candidate_id": "book-a", "original_title": None, "original_language": None}]}

    assert validate_proposal_response(payload, {"book-a"}) == {}


def test_a_title_longer_than_300_characters_is_dropped() -> None:
    payload = {"proposals": [{"candidate_id": "book-a", "original_title": "x" * 301, "original_language": "en"}]}

    assert validate_proposal_response(payload, {"book-a"}) == {}


def test_duplicate_candidate_ids_are_a_malformed_response() -> None:
    payload = {
        "proposals": [
            {"candidate_id": "book-a", "original_title": "One", "original_language": "en"},
            {"candidate_id": "book-a", "original_title": "Two", "original_language": "en"},
        ]
    }

    assert validate_proposal_response(payload, {"book-a"}) is None


def test_a_wrong_payload_shape_is_rejected() -> None:
    assert validate_proposal_response(["not", "an", "object"], {"book-a"}) is None
    assert validate_proposal_response({"items": []}, {"book-a"}) is None
    assert validate_proposal_response({"proposals": {}}, {"book-a"}) is None
    assert validate_proposal_response({"proposals": [{"candidate_id": 1}]}, {"book-a"}) is None
    assert (
        validate_proposal_response(
            {"proposals": [{"candidate_id": "book-a", "original_title": None, "original_language": None, "x": 1}]},
            {"book-a"},
        )
        is None
    )


# ── Material Change ──────────────────────────────────────────────────────


def test_adding_an_original_edition_does_not_mark_a_material_change() -> None:
    before = {
        "identity_key": "ahora que se acerca el final|diana athill",
        "title": "Ahora que se acerca el final",
        "primary_authors": ["Diana Athill"],
        "independent_signal_families": ["media/curated list"],
        "primary_signal": {"signal_strength": "major media list"},
        "primary_signal_strength": "major media list",
        "russian_edition_status": "not found in Russian",
        "visible_risk_status": "no visible risk",
    }
    after = dict(before, original_edition=_confirmed_record())

    assert ScoutingCandidate(after).material_change_reasons(before) == []
