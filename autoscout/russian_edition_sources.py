"""Query builders and response parsers of the Russian-edition completion pass (#1181).

Pure and offline: nothing here fetches a page, calls a model or touches
state. Each source answers through one parser that returns either its
results or ``None`` for an answer that cannot be read (changed markup, a
block page, garbage), so the pass can count a failure.

- **LiveLib** is a locator only (:func:`livelib_search_url`,
  :func:`parse_livelib_search`): its search page gives a Russian title, a
  Cyrillic author name and a book page address. Its book pages sit behind a
  DDoS-Guard check and are never fetched.
- **Wikidata** gives the author's name in Cyrillic in one request
  (:func:`wikidata_search_url`, :func:`parse_wikidata_search`): the search
  answers in Russian, so each hit carries the English name it matched and
  its Russian label when there is one.
- **The Russian State Library** is asked by the Cyrillic author, and the
  Russian title when LiveLib located one, through the existing live search
  of :mod:`autoscout.russian_editions` (:func:`rsl_author_query`);
  :func:`rsl_entries` turns its metadata-only results into
  :class:`EditionEntry` records.
- **LitRes** is asked by the Cyrillic author through its public search
  interface (:func:`litres_search_url`, :func:`parse_litres_search`). Its
  dates are the original work's, so a LitRes entry has no year; the ISBN
  and publisher of a confirmed LitRes edition come from its detail
  (:func:`litres_art_url`, :func:`parse_litres_art`).

Every edition, whatever its source, is one :class:`EditionEntry`: metadata
only, never a description, annotation, review or sample.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from urllib.parse import urlencode

SOURCE_LIVELIB = "livelib"
SOURCE_RSL = "rsl"
SOURCE_LITRES = "litres"
SOURCE_WIKIDATA = "wikidata"
SOURCES = (SOURCE_LIVELIB, SOURCE_WIKIDATA, SOURCE_RSL, SOURCE_LITRES)
# The two sources whose entries can confirm an edition.
CATALOGUE_SOURCES = (SOURCE_RSL, SOURCE_LITRES)

LIVELIB_ROOT = "https://www.livelib.ru"
LITRES_ROOT = "https://www.litres.ru"
LITRES_SEARCH_URL = "https://api.litres.ru/foundation/api/search"
LITRES_ART_URL = "https://api.litres.ru/foundation/api/arts/"
WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
LITRES_SEARCH_LIMIT = 20
WIKIDATA_SEARCH_LIMIT = 5
# The State Library's bibliographic line is kept this long for this pass, so
# a long title does not cut off the publisher, year and ISBN.
RSL_PUBLICATION_LIMIT = 600

# Words of a page that refuses a client instead of answering it.
BLOCK_PAGE_MARKERS = (
    "ddos-guard",
    "captcha",
    "you are making too many requests",
    "too many requests",
    "доступ запрещ",
    "слишком много запросов",
)

_CYRILLIC = re.compile(r"[\u0400-\u04FF]")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class EditionEntry:
    """One edition a source listed: metadata only.

    ``year`` is the edition's year when the source states one (never for
    LitRes, whose dates are the original work's). ``states_translation`` is
    true when the record itself says the book was translated (a State
    Library imprint). ``url`` is the record or book page on the source, and
    ``source_id`` the source's own id of the edition (LitRes).
    ``catalogue_title`` is the title as the catalogue gave it, bracketed notes
    included (State Library), so an abridgement note is still seen after
    ``title`` was cleaned.
    """

    source: str
    title: str
    authors: tuple[str, ...]
    language: str
    url: str
    publisher: str = ""
    year: int | None = None
    isbn: str = ""
    states_translation: bool = False
    source_id: str = ""
    catalogue_title: str = ""


def has_cyrillic(text: str) -> bool:
    return bool(_CYRILLIC.search(str(text or "")))


def is_block_page(text: str) -> bool:
    """True when ``text`` is a bot check, a captcha or a rate-limit page."""
    lowered = str(text or "").casefold()
    return any(marker in lowered for marker in BLOCK_PAGE_MARKERS)


def _clean(text: Any) -> str:
    return _SPACES.sub(" ", html_lib.unescape(re.sub(r"<[^>]+>", " ", str(text or "")))).strip()


# ── LiveLib ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LiveLibResult:
    title: str
    author: str
    path: str

    @property
    def url(self) -> str:
        return LIVELIB_ROOT + self.path


def livelib_search_url(title: str) -> str:
    """The LiveLib search page for one title; no author, which made results worse."""
    return f"{LIVELIB_ROOT}/find/books/{quote(title.strip(), safe='')}"


_LIVELIB_BLOCK = re.compile(r'<div class="object-wrapper object-edition[^"]*"[^>]*data-link="([^"]+)"')
_LIVELIB_TITLE = re.compile(r'<a href="[^"]*" class="title">(.*?)</a>', re.DOTALL)
_LIVELIB_AUTHOR = re.compile(r'<a class="description" href="/author/[^"]*"[^>]*>(.*?)</a>', re.DOTALL)


def parse_livelib_search(markup: str) -> list[LiveLibResult] | None:
    """Every result block of a LiveLib search page, or ``None`` when the page cannot be read.

    A page that says nothing was found answers an empty list. A block page,
    a page with neither results nor that message, and a page whose result
    blocks yield no result (changed markup inside the block) answer ``None``.
    """
    if not isinstance(markup, str) or is_block_page(markup):
        return None
    starts = list(_LIVELIB_BLOCK.finditer(markup))
    if not starts:
        return [] if 'class="not-found-text"' in markup else None
    results: list[LiveLibResult] = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(markup)
        block = markup[start.start() : end]
        title = _LIVELIB_TITLE.search(block)
        author = _LIVELIB_AUTHOR.search(block)
        path = html_lib.unescape(start.group(1).strip())
        if title is None or not path.startswith("/book/"):
            continue
        results.append(
            LiveLibResult(title=_clean(title.group(1)), author=_clean(author.group(1)) if author else "", path=path)
        )
    return results or None


# ── Wikidata ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WikidataHit:
    """One search hit: the English name it matched, and the label shown in Russian with its language."""

    match_text: str
    label: str
    label_language: str


def wikidata_search_url(author: str) -> str:
    """One search for the author's English name, answered in Russian (``uselang=ru``)."""
    query = {
        "action": "wbsearchentities",
        "search": author.strip(),
        "language": "en",
        "uselang": "ru",
        "type": "item",
        "limit": WIKIDATA_SEARCH_LIMIT,
        "format": "json",
    }
    return f"{WIKIDATA_API_URL}?{urlencode(query)}"


def _json(text: Any) -> Any:
    try:
        return json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return None


def _hit(item: Mapping[str, Any]) -> WikidataHit:
    match = item.get("match")
    display = item.get("display")
    label = display.get("label") if isinstance(display, Mapping) else None
    return WikidataHit(
        match_text=str(match.get("text", "") if isinstance(match, Mapping) else "").strip(),
        label=str(label.get("value", "") if isinstance(label, Mapping) else "").strip(),
        label_language=str(label.get("language", "") if isinstance(label, Mapping) else "").strip(),
    )


def parse_wikidata_search(text: str) -> list[WikidataHit] | None:
    """The hits of a ``wbsearchentities`` answer in its order, or ``None`` when unreadable."""
    data = _json(text)
    if not isinstance(data, Mapping) or not isinstance(data.get("search"), list):
        return None
    return [_hit(item) for item in data["search"] if isinstance(item, Mapping)]


def same_latin_name(left: str, right: str) -> bool:
    """True when two Latin names have the same letters word for word, ignoring case and punctuation."""

    def key(name: str) -> str:
        return " ".join(re.findall(r"[a-z]+", name.casefold()))

    return bool(key(left)) and key(left) == key(right)


# ── Russian State Library ────────────────────────────────────────────────


def rsl_author_query(cyrillic_name: str, title: str = "") -> str:
    """The State Library's query: ``author:(<Surname> <Given name>)``, with ``AND title:(<title>)`` when given.

    The catalogue answers only its first ten records, so for a prolific
    author the edition is found by its title.
    """
    words = cyrillic_name.split()
    author = f"author:({cyrillic_name.strip()})" if len(words) < 2 else f"author:({words[-1]} {words[0]})"
    title = re.sub(r"[()]", " ", title)
    title = _SPACES.sub(" ", title).strip()
    return f"{author} AND title:({title})" if title else author


_RSL_IMPRINT = re.compile(r"^\s*[^:]+?\s:\s(?P<publisher>.+?),\s*(?:cop\.\s*|печ\.\s*|©\s*)?(?P<year>\d{4})\b")
_ISBN = re.compile(r"ISBN\s*([0-9][0-9Xx\-]{8,16}[0-9Xx])")
# Imprint words that state a translation.
TRANSLATION_STATEMENTS = ("перевод с", "пер. с", "перевели с")


# The State Library's words for Russian, listed: «русинский» (Rusyn) and
# «белорусский» are other languages, so no prefix or stem is matched.
RUSSIAN_CLUES = frozenset(
    {
        "русский",
        "русский язык",
        "на русском языке",
        "рус.",
        "рус",
        "rus",
        "ru",
        "russian",
    }
)


def states_russian(clue: str) -> bool:
    """True when a State Library language clue names Russian; any other stated language is not Russian.

    A clue listing several languages («русский; английский») names Russian
    when one of its parts is in :data:`RUSSIAN_CLUES`.
    """
    text = _SPACES.sub(" ", str(clue or "").casefold().replace("ё", "е")).strip()
    return any(part in RUSSIAN_CLUES for part in re.split(r"\s*[,;/]\s*", text))


def _rsl_imprint(publication: str) -> tuple[str, int | None]:
    for segment in re.split(r"\.\s+-\s+", publication):
        match = _RSL_IMPRINT.match(segment)
        if match:
            publisher = match.group("publisher").split(" : ")[0].strip(" .;")
            return publisher, int(match.group("year"))
    return "", None


def _rsl_title(result_title: str) -> str:
    """The title proper of a State Library record: no statement of responsibility, bracket notes or age mark."""
    title = result_title.split(" / ", 1)[0]
    # A parallel title ("= The 48 laws of power") is the original's, not the edition's.
    title = re.sub(r"\s=\s[^:\[]*", " ", title)
    title = re.sub(r"(?<!\w)\d{1,2}\+", "", re.sub(r"\[[^\]]*\]", "", title))
    parts = [part.strip(" :") for part in re.split(r"\s+:(?:\s+|$)|^\s*:\s*", title) if part.strip(" :")]
    return ": ".join(parts)


def rsl_entries(results: Sequence[Mapping[str, Any]]) -> list[EditionEntry]:
    """State Library results, as :func:`autoscout.russian_editions.parse_rsl_live_response` gives them."""
    entries: list[EditionEntry] = []
    for result in results:
        title = _rsl_title(str(result.get("result_title", "")))
        url = str(result.get("record_url", "")).strip()
        if not title or not url:
            continue
        publication = str(result.get("publication_data", ""))
        publisher, year = _rsl_imprint(publication)
        isbn = _ISBN.search(publication)
        clue = str(result.get("language_clue", "") or "").strip().casefold()
        russian = (not clue or states_russian(clue)) and has_cyrillic(title)
        entries.append(
            EditionEntry(
                source=SOURCE_RSL,
                title=title,
                authors=(str(result.get("result_author", "")).strip(),),
                language="ru" if russian else clue,
                url=url,
                publisher=publisher,
                year=year,
                isbn=isbn.group(1) if isbn else "",
                states_translation=any(words in publication.casefold() for words in TRANSLATION_STATEMENTS),
                catalogue_title=str(result.get("result_title", "")).strip(),
            )
        )
    return entries


class RslEntries(list[EditionEntry]):
    """The editions of one State Library answer, and whether the answer was partial.

    The catalogue answers only its first ten records: an answer is
    ``partial`` when its ``TotalHits`` exceeds the records read, so an edition
    missing from it may still be in the catalogue.
    """

    def __init__(self, entries: Sequence[EditionEntry] = (), *, partial: bool = False) -> None:
        super().__init__(entries)
        self.partial = partial


def parse_rsl_author_answer(text: str, query: str, check_date: str) -> RslEntries | None:
    """The editions of a State Library answer to ``query``, or ``None`` when it cannot be read.

    The answer is read by the existing metadata-only parser of
    :mod:`autoscout.russian_editions`, which keeps a result only with its
    query and check date; a block or an unparseable answer is ``None``. So is
    an answer whose ``TotalHits`` is positive while no record could be read:
    the catalogue found something this parser could not see. An answer whose
    ``TotalHits`` exceeds the records read is marked partial
    (:class:`RslEntries`).
    """
    from autoscout.russian_editions import parse_rsl_live_response

    if not isinstance(text, str) or not text.strip():
        return None
    check = {"name": "RSL", "publication_data_limit": RSL_PUBLICATION_LIMIT, "complete_author": True}
    results, error = parse_rsl_live_response(text, query, {}, check, check_date)
    if error:
        return None
    entries = rsl_entries(results)
    total = _rsl_total_hits(text)
    if not entries and total > 0:
        return None
    return RslEntries(entries, partial=total > len(results))


def _rsl_total_hits(text: str) -> int:
    data = _json(text)
    hits = data.get("TotalHits") if isinstance(data, Mapping) else None
    return hits if isinstance(hits, int) and not isinstance(hits, bool) else 0


# ── LitRes ───────────────────────────────────────────────────────────────


def litres_search_url(cyrillic_name: str) -> str:
    query = {"q": cyrillic_name.strip(), "limit": LITRES_SEARCH_LIMIT, "types": "text_book"}
    return f"{LITRES_SEARCH_URL}?{urlencode(query)}"


def litres_art_url(art_id: str) -> str:
    """The LitRes detail of one edition, for its ISBN and publisher."""
    return f"{LITRES_ART_URL}{quote(str(art_id).strip(), safe='')}"


def _litres_entry(instance: Mapping[str, Any]) -> EditionEntry | None:
    title = str(instance.get("title", "") or "").strip()
    path = str(instance.get("url", "") or "").strip()
    persons = instance.get("persons")
    if not title or not path.startswith("/") or not isinstance(persons, list):
        return None
    authors = tuple(
        str(person.get("full_name", "")).strip()
        for person in persons
        if isinstance(person, Mapping) and person.get("role") == "author" and str(person.get("full_name", "")).strip()
    )
    return EditionEntry(
        source=SOURCE_LITRES,
        title=title,
        authors=authors,
        language=str(instance.get("language_code", "") or "").strip().casefold(),
        url=LITRES_ROOT + path,
        source_id=str(instance.get("id", "") or "").strip(),
    )


def parse_litres_search(text: str) -> list[EditionEntry] | None:
    """The text books of a LitRes search answer, or ``None`` when it cannot be read.

    An answer without text-book hits is an empty list. One with text-book
    hits of which none can be read into an entry (changed fields) is
    ``None``, so it counts as a failure.
    """
    data = _json(text)
    payload = data.get("payload") if isinstance(data, Mapping) else None
    items = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(items, list):
        return None
    entries: list[EditionEntry] = []
    hits = 0
    for item in items:
        if not isinstance(item, Mapping) or item.get("type") != "text_book":
            continue
        hits += 1
        instance = item.get("instance")
        entry = _litres_entry(instance) if isinstance(instance, Mapping) else None
        if entry is not None:
            entries.append(entry)
    return None if hits and not entries else entries


@dataclass(frozen=True)
class LitresArt:
    """What the LitRes detail of one edition adds: its ISBN and its first rightholder."""

    isbn: str
    publisher: str


def parse_litres_art(text: str) -> LitresArt | None:
    """The ISBN and publisher of a LitRes detail answer, or ``None`` when it cannot be read."""
    data = _json(text)
    payload = data.get("payload") if isinstance(data, Mapping) else None
    art = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(art, Mapping):
        return None
    rightholders = art.get("rightholders")
    publisher = ""
    if isinstance(rightholders, list):
        publisher = next(
            (
                str(item.get("name", "")).strip()
                for item in rightholders
                if isinstance(item, Mapping) and str(item.get("name", "") or "").strip()
            ),
            "",
        )
    return LitresArt(isbn=str(art.get("isbn", "") or "").strip(), publisher=publisher)
