"""National-library records by ISBN for the original-edition pass (issue #1139).

A national library's catalogue states outright whether an edition is a
translation, from which language and, often, under which original title.
This module holds the pure, offline part of that lookup: one
:class:`LibraryCatalogue` per library, each naming the list languages it
serves, the search URL for an ISBN and the parser of its answer.

* :data:`DNB` — the German National Library's SRU interface, MARC 21:
  field 041 (indicator 1 ``1`` or subfield ``h``) states a translation and
  its original language, 240 or a 246 labelled as the original holds the
  original title, 300 the extent.
* :data:`BNF` — the French National Library's SRU interface, UNIMARC:
  field 101 (indicator 1 ``1``, subfield ``c``) states a translation and its
  original language, 454 the original title, 215 the extent.
* :data:`BNE` — the Spanish National Library's SRU interface, MARC 21
  (issue #1147): field 041 (indicator 1 ``1`` or subfield ``h``) states a
  translation and its original language, a uniform title in 240 with a
  language subfield ``l`` states a translation without naming the original
  language, 240 holds the original title, 300 the extent. A 240 without
  ``l`` is the uniform title of a work in its own language (a Spanish
  classic, a law) and states nothing.

Another library is one more :class:`LibraryCatalogue` in
:data:`ALL_LIBRARY_CATALOGUES`; nothing else needs to change. A library whose
key is in :data:`PAUSED_LIBRARY_KEYS` is left out of
:data:`LIBRARY_CATALOGUES`, the libraries the pass asks. A library's
``isbn_prefixes`` and ``min_request_interval_seconds`` (its robots crawl
delay) are honoured by the pass that asks it.

A record without a translation statement is not a "no": it only means the
library does not say, and the caller treats it as no answer. Nothing here
fetches a page or touches state.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from collections.abc import Collection
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from urllib.parse import quote
from urllib.parse import urlsplit

from autoscout.isbn_completion import isbn13
from autoscout.original_edition import OPEN_LIBRARY_LANGUAGE

# MARC 21 and UNIMARC both use the ISO 639-2 bibliographic codes Open
# Library stores; a few terminology codes are accepted as well.
_LANGUAGE_FROM_MARC: dict[str, str] = {
    **{code: iso for iso, code in OPEN_LIBRARY_LANGUAGE.items()},
    "deu": "de",
    "fra": "fr",
    "nld": "nl",
    "ces": "cs",
    "zho": "zh",
}
_TITLE_MAX_LENGTH = 300
# "501 p.", "526 Seiten", "XII, 340 S.", "320 pages", "519 páginas", "300 págs.".
_PAGES = re.compile(
    r"(\d{1,5})\s*(?:p\b|p\.|pages?\b|S\.|Seiten\b|páginas?\b|págs?\.)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MarcField:
    tag: str
    ind1: str
    ind2: str
    subfields: tuple[tuple[str, str], ...]

    def values(self, code: str) -> list[str]:
        return [value for subfield, value in self.subfields if subfield == code and value.strip()]


@dataclass(frozen=True)
class MarcRecord:
    """One MARC record of an SRU answer, in either MARC 21 or UNIMARC."""

    record_id: str
    controlfields: dict[str, str] = field(default_factory=dict)
    datafields: tuple[MarcField, ...] = ()

    def fields(self, tag: str) -> list[MarcField]:
        return [datafield for datafield in self.datafields if datafield.tag == tag]


@dataclass(frozen=True)
class LibraryRecord:
    """What one library record states about an edition.

    ``states_translation`` is true only when the record says so; false means
    the record is silent, never that the book is not a translation.
    ``original_language`` is an ISO 639-1 code known to Open Library, or empty.
    """

    record_url: str
    states_translation: bool
    original_language: str = ""
    original_title: str = ""
    page_count: int | None = None


@dataclass(frozen=True)
class LibraryCatalogue:
    """One library that can be asked about an edition by its ISBN.

    ``key`` names the library in reports; ``name`` is its full name.
    ``isbn_prefixes`` limits the ISBN-13s worth asking (empty: every ISBN);
    ``min_request_interval_seconds`` is the spacing between two requests the
    library's robots file asks for.
    """

    key: str
    name: str
    list_languages: frozenset[str]
    search_url: Callable[[str], str]
    parse: Callable[[str], list[LibraryRecord] | None]
    isbn_prefixes: tuple[str, ...] = ()
    min_request_interval_seconds: float = 0.0

    def serves(self, source_languages: Collection[str]) -> bool:
        """True when the book was found on a list in one of this library's languages."""
        return any(language.strip().casefold() in self.list_languages for language in source_languages)

    def holds(self, isbn: str) -> bool:
        """True when the library is worth asking about this ISBN-13."""
        return not self.isbn_prefixes or isbn.startswith(self.isbn_prefixes)


# ── SRU and MARC reading ─────────────────────────────────────────────────


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _marc_record(element: ET.Element) -> MarcRecord:
    controlfields: dict[str, str] = {}
    datafields: list[MarcField] = []
    for child in element:
        name = _local(child.tag)
        if name == "controlfield":
            controlfields.setdefault(str(child.get("tag", "")), (child.text or "").strip())
        elif name == "datafield":
            subfields = tuple(
                (str(sub.get("code", "")), " ".join((sub.text or "").split()))
                for sub in child
                if _local(sub.tag) == "subfield"
            )
            datafields.append(
                MarcField(
                    tag=str(child.get("tag", "")),
                    ind1=str(child.get("ind1", " ")),
                    ind2=str(child.get("ind2", " ")),
                    subfields=subfields,
                )
            )
    return MarcRecord(record_id=str(element.get("id", "")), controlfields=controlfields, datafields=tuple(datafields))


def sru_marc_records(body: str) -> list[MarcRecord] | None:
    """The MARC records of an SRU answer; ``None`` when it is not a usable SRU answer.

    An answer with SRU diagnostics (an error) or without a record count is
    not usable; an answer with zero records is an empty list.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    if _local(root.tag) != "searchRetrieveResponse":
        return None
    if any(_local(element.tag) == "diagnostics" and len(element) for element in root.iter()):
        return None
    if not any(_local(element.tag) == "numberOfRecords" for element in root):
        return None
    records: list[MarcRecord] = []
    for element in root.iter():
        if _local(element.tag) != "record":
            continue
        if any(_local(child.tag) in {"controlfield", "datafield"} for child in element):
            records.append(_marc_record(element))
    return records


def _language(code: str) -> str:
    return _LANGUAGE_FROM_MARC.get(code.strip().casefold(), "")


def _clean_title(value: str) -> str:
    # MARC non-filing markers around a leading article are dropped first.
    title = " ".join(value.replace("\u0098", "").replace("\u009c", "").split()).strip(" /:;,.=")
    return title[:_TITLE_MAX_LENGTH].strip()


def _page_count(extents: Sequence[str]) -> int | None:
    counts = [int(match) for extent in extents for match in _PAGES.findall(extent)]
    counts = [count for count in counts if count > 0]
    return max(counts) if counts else None


def _http_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    return value if parts.scheme in {"http", "https"} and parts.hostname else ""


# ── German National Library (MARC 21) ────────────────────────────────────

DNB_SRU_URL = "https://services.dnb.de/sru/dnb"
DNB_RECORD_URL = "https://d-nb.info/"


def dnb_search_url(isbn: str) -> str:
    return (
        f"{DNB_SRU_URL}?version=1.1&operation=searchRetrieve&query=num%3D{quote(isbn13(isbn))}&recordSchema=MARC21-xml"
    )


def _dnb_original_title(record: MarcRecord) -> str:
    for title_field in record.fields("246"):
        labels = " ".join(title_field.values("i")).casefold()
        if "original" in labels:
            titles = title_field.values("a")
            if titles:
                return _clean_title(titles[0])
    for title_field in record.fields("240"):
        titles = title_field.values("a")
        if titles:
            return _clean_title(titles[0])
    return ""


def _marc21_language_statement(record: MarcRecord) -> tuple[bool, str]:
    """``(states_translation, original_language)`` from MARC 21 field 041.

    Indicator 1 ``1`` or an original-language subfield ``h`` states a
    translation; the first ``h`` that is not a language of the text is the
    original language.
    """
    states_translation = False
    language = ""
    for language_field in record.fields("041"):
        text_languages = {_language(code) for code in language_field.values("a")}
        originals = [_language(code) for code in language_field.values("h")]
        if language_field.ind1 == "1" or originals:
            states_translation = True
        if not language:
            language = next((code for code in originals if code and code not in text_languages), "")
    return states_translation, language


def dnb_record(record: MarcRecord) -> LibraryRecord | None:
    identifier = record.controlfields.get("001", "")
    if not identifier:
        return None
    states_translation, language = _marc21_language_statement(record)
    title = _dnb_original_title(record) if states_translation else ""
    extents = [value for extent in record.fields("300") for value in extent.values("a")]
    return LibraryRecord(
        record_url=DNB_RECORD_URL + quote(identifier),
        states_translation=states_translation,
        original_language=language,
        original_title=title,
        page_count=_page_count(extents),
    )


def parse_dnb(body: str) -> list[LibraryRecord] | None:
    records = sru_marc_records(body)
    if records is None:
        return None
    return [parsed for record in records if (parsed := dnb_record(record)) is not None]


# ── French National Library (UNIMARC) ────────────────────────────────────

BNF_SRU_URL = "https://catalogue.bnf.fr/api/SRU"
BNF_CATALOGUE_URL = "https://catalogue.bnf.fr/"


def bnf_search_url(isbn: str) -> str:
    query = quote(f'bib.isbn adj "{isbn13(isbn)}"', safe="")
    return f"{BNF_SRU_URL}?version=1.2&operation=searchRetrieve&query={query}"


def _bnf_record_url(record: MarcRecord) -> str:
    url = _http_url(record.controlfields.get("003", ""))
    if url:
        return url
    if record.record_id.startswith("ark:/"):
        return BNF_CATALOGUE_URL + record.record_id
    return ""


def bnf_record(record: MarcRecord) -> LibraryRecord | None:
    url = _bnf_record_url(record)
    if not url:
        return None
    states_translation = False
    language = ""
    for language_field in record.fields("101"):
        originals = [_language(code) for code in language_field.values("c")]
        # Indicator 1: 0 the original language, 1 a translation, 2 contains
        # translations. Only a translation of the whole work counts.
        if language_field.ind1 == "1":
            states_translation = True
            text_languages = {_language(code) for code in language_field.values("a")}
            if not language:
                language = next((code for code in originals if code and code not in text_languages), "")
    title = ""
    # With an intermediate language (101 $b) the title in 454 may be the
    # intermediate edition's, not the original's, so it is not taken.
    intermediate = any(language_field.values("b") for language_field in record.fields("101"))
    if states_translation and not intermediate:
        for title_field in record.fields("454"):
            titles = title_field.values("t") or title_field.values("a")
            if titles:
                title = _clean_title(titles[0])
                break
    extents = [value for extent in record.fields("215") for value in extent.values("a")]
    return LibraryRecord(
        record_url=url,
        states_translation=states_translation,
        original_language=language,
        original_title=title,
        page_count=_page_count(extents),
    )


def parse_bnf(body: str) -> list[LibraryRecord] | None:
    records = sru_marc_records(body)
    if records is None:
        return None
    return [parsed for record in records if (parsed := bnf_record(record)) is not None]


# ── Spanish National Library (MARC 21) ───────────────────────────────────

BNE_SRU_URL = "https://catalogo.bne.es/view/sru/34BNE_INST"
BNE_RECORD_URL = "https://catalogo.bne.es/discovery/fulldisplay/alma{}/34BNE_INST:CATALOGO"
# The BNE is Spain's legal-deposit library: books with a Spanish ISBN
# (978-84, 979-13). A Latin American edition on a Spanish-language list is
# rarely there, and every request costs the robots file's crawl delay.
BNE_ISBN_PREFIXES = ("97884", "97913")
# ``Crawl-delay: 10`` in https://catalogo.bne.es/robots.txt (read 2026-11-19).
BNE_REQUEST_INTERVAL_SECONDS = 10.0


def bne_search_url(isbn: str) -> str:
    return (
        f"{BNE_SRU_URL}?version=1.2&operation=searchRetrieve&recordSchema=marcxml"
        f"&query=alma.isbn%3D{quote(isbn13(isbn))}"
    )


def bne_record(record: MarcRecord) -> LibraryRecord | None:
    identifier = record.controlfields.get("001", "")
    if not identifier.isdigit():
        return None
    states_translation, language = _marc21_language_statement(record)
    uniform_titles = record.fields("240")
    # A uniform title naming the language of this expression (240 $l) is the
    # BNE's statement of a translation when 041 is missing (its e-book records
    # carry no 041). Without $l a 240 is the uniform title of a work in its own
    # language and says nothing. A 041 that calls the text no translation
    # (indicator 1 0, no $h) is believed over a 240.
    language_fields = record.fields("041")
    original_text = bool(language_fields) and all(
        language_field.ind1 == "0" and not language_field.values("h") for language_field in language_fields
    )
    if not original_text and any(title_field.values("l") for title_field in uniform_titles):
        states_translation = True
    title = ""
    if states_translation:
        title = next(
            (_clean_title(titles[0]) for title_field in uniform_titles if (titles := title_field.values("a"))),
            "",
        )
    extents = [value for extent in record.fields("300") for value in extent.values("a")]
    return LibraryRecord(
        record_url=BNE_RECORD_URL.format(identifier),
        states_translation=states_translation,
        original_language=language,
        original_title=title,
        page_count=_page_count(extents),
    )


def parse_bne(body: str) -> list[LibraryRecord] | None:
    records = sru_marc_records(body)
    if records is None:
        return None
    return [parsed for record in records if (parsed := bne_record(record)) is not None]


# ── The libraries ────────────────────────────────────────────────────────

DNB = LibraryCatalogue(
    key="dnb",
    name="Deutsche Nationalbibliothek",
    list_languages=frozenset({"de"}),
    search_url=dnb_search_url,
    parse=parse_dnb,
)
BNF = LibraryCatalogue(
    key="bnf",
    name="Bibliothèque nationale de France",
    list_languages=frozenset({"fr"}),
    search_url=bnf_search_url,
    parse=parse_bnf,
)
BNE = LibraryCatalogue(
    key="bne",
    name="Biblioteca Nacional de España",
    list_languages=frozenset({"es"}),
    search_url=bne_search_url,
    parse=parse_bne,
    isbn_prefixes=BNE_ISBN_PREFIXES,
    min_request_interval_seconds=BNE_REQUEST_INTERVAL_SECONDS,
)
ALL_LIBRARY_CATALOGUES: tuple[LibraryCatalogue, ...] = (DNB, BNF, BNE)
# Libraries the original-edition pass does not ask. The DNB has refused every
# request since 2026-11-18 (TLS refused, then HTTP 403/429 even for its robots
# file), although its rules allow keyless use (#1145); a refused library only
# spends requests and may keep the block in place. Remove the key once it
# answers again.
PAUSED_LIBRARY_KEYS: frozenset[str] = frozenset({"dnb"})
LIBRARY_CATALOGUES: tuple[LibraryCatalogue, ...] = tuple(
    library for library in ALL_LIBRARY_CATALOGUES if library.key not in PAUSED_LIBRARY_KEYS
)


def best_record(records: Sequence[LibraryRecord]) -> LibraryRecord | None:
    """The record that answers best: one stating a translation with a language, then any statement, then any."""
    if not records:
        return None
    return min(
        enumerate(records),
        key=lambda item: (not item[1].states_translation, not item[1].original_language, item[0]),
    )[1]


__all__ = [
    "ALL_LIBRARY_CATALOGUES",
    "BNE",
    "BNF",
    "DNB",
    "LIBRARY_CATALOGUES",
    "PAUSED_LIBRARY_KEYS",
    "LibraryCatalogue",
    "LibraryRecord",
    "best_record",
    "bne_search_url",
    "bnf_search_url",
    "dnb_search_url",
    "parse_bne",
    "parse_bnf",
    "parse_dnb",
    "sru_marc_records",
]
