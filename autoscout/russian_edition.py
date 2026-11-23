"""The pure core of the Russian-edition completion pass (#1181).

Given what the sources listed for a book and which listed title a model
named as the same work, this module decides what is recorded, offline:

1. :func:`names_correspond` compares two author names loosely across
   scripts (Хайдт/Haidt, Бёркман/Burkeman), on a consonant skeleton of the
   surname. When both names carry a given name, the given names must
   correspond too: across scripts on the same skeleton (Джон is John, never
   Robert), within Cyrillic by their first letter (Роберт Грин is not Джон
   Грин).
   :func:`wikidata_russian_name` picks the author's Russian label from a
   Wikidata search answered in Russian.
2. :func:`is_retelling` recognises summaries, abridgements and "key ideas"
   books; the marker words are data per language
   (:data:`RETELLING_MARKERS`).
3. :func:`numbered_titles` merges the editions of every source into
   numbered titles, one per normalised Russian title, and
   :func:`eligible_titles` keeps only the editions that can pass the guards:
   the list the model sees.
4. :func:`decide` applies the guards that do not depend on the model and
   builds the ``russian_edition`` record: ``confirmed`` (a State Library or
   LitRes edition), ``unconfirmed`` (LiveLib located it, no catalogue
   confirmed it) or ``no_find``.
5. :func:`validate_russian_edition` checks a stored record;
   :data:`MATCH_RESPONSE_SCHEMA` and :func:`validate_match_response`
   describe and check the model's batched answer.

Nothing here fetches, calls a model or touches state. The record is a
learned field of its own: it never writes or reads the editorial
``russian_edition_status`` or the ``russian_edition_verification`` record.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from autoscout.russian_edition_sources import CATALOGUE_SOURCES
from autoscout.russian_edition_sources import SOURCE_LITRES
from autoscout.russian_edition_sources import SOURCE_LIVELIB
from autoscout.russian_edition_sources import SOURCE_RSL
from autoscout.russian_edition_sources import EditionEntry
from autoscout.russian_edition_sources import WikidataHit
from autoscout.russian_edition_sources import has_cyrillic
from autoscout.russian_edition_sources import same_latin_name

TARGET_LANGUAGE = "ru"

RUSSIAN_EDITION_FIELD = "russian_edition"
LOOKUP_DATE_FIELD = "russian_edition_lookup_date"
AUTHOR_NAME_FIELD = "russian_author_name"
# The date of the pass's last attempt, completed or not: it orders the queue,
# so a book that could not be completed does not come first every night. It
# never makes a book settled.
ATTEMPT_DATE_FIELD = "russian_edition_attempt_date"
# The date of the last LiveLib search actually made for the book (answered,
# blocked or failed; never one skipped at the ceiling or answered from the
# cache). It orders the queue, so the LiveLib ceiling goes to the books that
# waited longest for one. It never makes a book settled.
LIVELIB_SEARCH_DATE_FIELD = "russian_edition_livelib_search_date"
LEARNED_FIELDS = (
    RUSSIAN_EDITION_FIELD,
    LOOKUP_DATE_FIELD,
    AUTHOR_NAME_FIELD,
    ATTEMPT_DATE_FIELD,
    LIVELIB_SEARCH_DATE_FIELD,
)

STATUS_CONFIRMED = "confirmed"
STATUS_UNCONFIRMED = "unconfirmed"
STATUS_NO_FIND = "no_find"
STATUSES = (STATUS_CONFIRMED, STATUS_UNCONFIRMED, STATUS_NO_FIND)

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"

CONFIRMED_KEYS = (
    "status",
    "russian_title",
    "russian_author",
    "publisher",
    "year",
    "isbn",
    "evidence_url",
    "sources",
    "confidence",
    "checked_on",
)
UNCONFIRMED_KEYS = ("status", "russian_title", "russian_author", "evidence_url", "checked_on")
NO_FIND_KEYS = ("status", "checked_on")

# Hosts whose record or book pages may be a confirmed edition's evidence,
# and the host of an unconfirmed one.
EVIDENCE_HOSTS = {SOURCE_RSL: ("search.rsl.ru",), SOURCE_LITRES: ("www.litres.ru", "litres.ru")}
LOCATOR_HOSTS = ("www.livelib.ru", "livelib.ru")

# Title words that mark a summary, an abridgement or a "key ideas" retelling,
# per target language; English markers apply to every language.
RETELLING_MARKERS: dict[str, tuple[str, ...]] = {
    "ru": (
        "саммари",
        "краткая версия",
        "ключевые идеи",
        "краткое содержание",
        "краткое изложение",
        "кратко",
        "инфографика по книге",
        "сокращённое издание",
        "сокращенное издание",
        "в сокращении",
        "сокращённый перевод",
        "краткий пересказ",
        "пересказ",
        "адаптированное издание",
    ),
    "en": ("summary", "workbook"),
}

# ── Names ────────────────────────────────────────────────────────────────

_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "phd", "md", "dr"})
# Cyrillic letters as Latin consonants; vowels, й, ь, ъ and в fall away,
# as w and v do on the Latin side (Russian renders W with either letter).
_CYRILLIC_SKELETON = {
    "б": "b",
    "г": "g",
    "д": "d",
    "ж": "zh",
    "з": "s",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
}
_LATIN_DIGRAPHS = (("ph", "f"), ("th", "t"), ("kh", "h"), ("ck", "k"), ("qu", "k"), ("x", "ks"), ("j", "dzh"))
_LATIN_VOWELS = frozenset("aeiouyvw")


def _fold(text: str) -> str:
    """Casefolded, with the Cyrillic yo as ye and Latin accents removed."""
    text = str(text or "").casefold().replace("ё", "е")
    return "".join(
        char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char) or char == "̆"
    ).replace("й", "й")


def _words(text: str) -> list[str]:
    words = re.findall(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*", _fold(text))
    while words and words[-1] in _NAME_SUFFIXES:
        words.pop()
    return words


def _name_parts(name: str) -> tuple[list[str], str]:
    """The given names and the surname; "Surname, Given" is read as such."""
    head, comma, tail = str(name or "").partition(",")
    given = _words(tail) if comma else []
    if given:
        surnames = _words(head)
        return given, surnames[-1] if surnames else ""
    words = _words(head if comma else name)
    return words[:-1], words[-1] if words else ""


def surname(name: str) -> str:
    """The surname of a name: its last word that is not a suffix such as Jr., or the part before a comma."""
    return _name_parts(name)[1]


def _collapse(letters: str) -> str:
    return re.sub(r"(.)\1+", r"\1", letters)


def word_skeleton(word: str) -> str:
    """The consonant skeleton of one name word, comparable across Cyrillic and Latin.

    A Latin "ch" before r or l, or in "chael", is a k (Christopher/Кристофер,
    Michael/Майкл).
    """
    word = _fold(word)
    if has_cyrillic(word):
        return _collapse("".join(_CYRILLIC_SKELETON.get(char, "") for char in word))
    latin = re.sub(r"[^a-z]", "", word).replace("z", "s")
    latin = re.sub(r"ch(?=[rl]|ael)", "k", latin).replace("ch", "C")
    latin = re.sub(r"c(?=[eiy])", "s", latin).replace("c", "k").replace("C", "ch")
    for digraph, replacement in _LATIN_DIGRAPHS:
        latin = latin.replace(digraph, replacement)
    return _collapse("".join(char for char in latin if char not in _LATIN_VOWELS))


def name_skeleton(name: str) -> str:
    """The consonant skeleton of a name's surname, comparable across Cyrillic and Latin."""
    return word_skeleton(surname(name))


def _given_skeletons_correspond(left: str, right: str) -> bool:
    """True when two given-name skeletons are one name in two spellings.

    The same skeleton, or one consonant dropped after the first
    (Stephen/Стивен).
    """
    if left == right:
        return True
    short, long = sorted((left, right), key=len)
    if len(short) < 2 or len(long) - len(short) != 1 or short[0] != long[0]:
        return False
    return any(long[:index] + long[index + 1 :] == short for index in range(1, len(long)))


def given_names_correspond(left: str, right: str) -> bool:
    """True unless both names carry a given name and the first given names are not one name.

    Within Cyrillic, the given names share their first letter. Across
    scripts, or between two Latin names, their skeletons correspond
    (:func:`_given_skeletons_correspond`); a Latin initial corresponds to a
    given name whose skeleton it begins. A given name without consonants
    (Ия, Ea) says nothing, and the surname decides.
    """
    left_given, right_given = _name_parts(left)[0], _name_parts(right)[0]
    if not (left_given and right_given):
        return True
    left_word, right_word = left_given[0], right_given[0]
    if has_cyrillic(left_word) and has_cyrillic(right_word):
        return left_word[0] == right_word[0]
    left_key, right_key = word_skeleton(left_word), word_skeleton(right_word)
    if not (left_key and right_key):
        return True
    if len(left_word) == 1:
        return right_key.startswith(left_key)
    if len(right_word) == 1:
        return left_key.startswith(right_key)
    return _given_skeletons_correspond(left_key, right_key)


def names_correspond(left: str, right: str) -> bool:
    """True when the surnames share a non-empty consonant skeleton and the given names correspond.

    The given names matter only when both names carry one
    (:func:`given_names_correspond`); otherwise the surname decides.
    """
    left_key, right_key = name_skeleton(left), name_skeleton(right)
    if not left_key or left_key != right_key:
        return False
    return given_names_correspond(left, right)


def cyrillic_name(name: Any) -> str:
    """``name`` cleaned when it is a Cyrillic personal name of at most five words, else empty."""
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    if not text or not has_cyrillic(text) or len(text.split()) > 5 or len(text) > 80:
        return ""
    return text


def wikidata_russian_name(hits: Sequence[WikidataHit], author: str) -> str:
    """The Russian label of the first hit that is the author's own entry, or empty.

    A hit qualifies when the English name it matched is the author's name,
    its label is in Russian, and the label is a Cyrillic personal name whose
    surname corresponds to the author's. A talk or a book named after the
    author fails the name comparison or the personal-name limit.
    """
    for hit in hits:
        if not same_latin_name(hit.match_text, author) or hit.label_language != TARGET_LANGUAGE:
            continue
        name = cyrillic_name(hit.label)
        if name and names_correspond(name, author):
            return name
    return ""


# ── Titles ───────────────────────────────────────────────────────────────


def russian_main_title(title: str) -> str:
    """A Russian title without its subtitle: the text before the first colon, full stop or dash."""
    head = re.split(r"\s*:\s|\.\s|\s[—–]\s", str(title or "").strip(), maxsplit=1)[0]
    return head.strip(" .:")


def normalized_title(title: str) -> str:
    """A Russian title reduced for comparison: no bracket notes, parallel title or age mark."""
    text = re.sub(r"\[[^\]]*\]", " ", str(title or ""))
    text = re.split(r"\s=\s", text, maxsplit=1)[0]
    text = re.sub(r"\b\d{1,2}\+", " ", text)
    return " ".join(re.findall(r"[^\W_]+", _fold(text)))


def is_retelling(entry: EditionEntry, language: str = TARGET_LANGUAGE) -> bool:
    """True for a summary, abridgement or "key ideas" book.

    A marker word in the title decides, or in the title as the catalogue
    gave it, bracketed notes included (a State Library «[сокращенное
    издание]»); so does an entry whose credited
    authors do not include the real one while the title names the real
    author's surname (a summary publisher credited as author). The second
    check needs the real name, so the caller passes it through
    :func:`retelling_by_credit`.
    """
    markers = [_fold(marker) for marker in (*RETELLING_MARKERS.get(language, ()), *RETELLING_MARKERS["en"])]
    for text in (entry.title, entry.catalogue_title):
        words = " ".join(re.findall(r"[^\W_]+", _fold(text)))
        if any(re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", words) for marker in markers):
            return True
    return False


def retelling_by_credit(entry: EditionEntry, author_name: str) -> bool:
    """True when the title names the real author but no credited author is them."""
    real = surname(author_name)
    if not real or any(names_correspond(author, author_name) for author in entry.authors):
        return False
    return real in re.findall(r"[^\W_]+", _fold(entry.title)) or any(
        names_correspond(word, author_name) for word in entry.title.split() if has_cyrillic(word)
    )


# ── The numbered list ────────────────────────────────────────────────────


@dataclass
class TitleGroup:
    """One Russian title the model sees, with every listed edition of it."""

    key: str
    entries: list[EditionEntry] = field(default_factory=list)

    @property
    def title(self) -> str:
        catalogue = [entry for entry in self.entries if entry.source in CATALOGUE_SOURCES]
        return (catalogue or self.entries)[0].title

    @property
    def sources(self) -> list[str]:
        return sorted({entry.source for entry in self.entries})


def numbered_titles(entries: Sequence[EditionEntry]) -> list[TitleGroup]:
    """Editions merged per normalised Russian title, in the order they were first listed."""
    groups: dict[str, TitleGroup] = {}
    for entry in entries:
        key = normalized_title(entry.title)
        if not key:
            continue
        groups.setdefault(key, TitleGroup(key)).entries.append(entry)
    return list(groups.values())


def model_entry(number: int, group: TitleGroup) -> dict[str, Any]:
    """One numbered title for the model: title, publisher, year and sources only."""
    years = [entry.year for entry in group.entries if entry.year is not None]
    publisher = next((entry.publisher for entry in group.entries if entry.publisher), "")
    sources = ["livelib (locator only)" if source == SOURCE_LIVELIB else source for source in group.sources]
    return {
        "number": number,
        "title": group.title,
        "publisher": publisher,
        "year": min(years) if years else None,
        "sources": sources,
    }


# ── The decision ─────────────────────────────────────────────────────────


def _predates(entry: EditionEntry, first_year: int | None) -> bool:
    return first_year is not None and entry.year is not None and entry.year < first_year


def russian_script_ok(entry: EditionEntry) -> bool:
    """True when the title's script does not rule the entry out.

    LitRes states the edition's language, so its Russian entry may carry a
    Latin title («Nexus»); the State Library's language clue and LiveLib
    are not reliable enough for that, so their titles must be Cyrillic.
    """
    return entry.source == SOURCE_LITRES or has_cyrillic(entry.title)


def credits_author(entry: EditionEntry, author_name: str, latin_author: str) -> bool:
    """True when a credited author of ``entry`` is both the Cyrillic ``author_name`` and the book's ``latin_author``."""
    return any(
        names_correspond(author, author_name) and names_correspond(author, latin_author) for author in entry.authors
    )


def _passes_guards(entry: EditionEntry, author_name: str, latin_author: str, first_year: int | None) -> bool:
    if entry.language != TARGET_LANGUAGE or not russian_script_ok(entry):
        return False
    if _predates(entry, first_year):
        return False
    if not credits_author(entry, author_name, latin_author):
        return False
    return not is_retelling(entry) and not retelling_by_credit(entry, author_name)


def eligible_titles(
    entries: Sequence[EditionEntry], *, author_name: str, latin_author: str, first_year: int | None
) -> list[TitleGroup]:
    """The numbered titles the model may see: only editions that can pass the guards of :func:`decide`.

    An entry that is not Russian, is a retelling (by a marker or by its
    credit), is not credited to the book's author, or is dated before the
    original is left out; a title whose every entry is left out is not
    listed. A title the author already published before the original is
    another work and is left out whole, so an undated edition of it is never
    listed on its own.
    """
    groups: list[TitleGroup] = []
    for group in numbered_titles(entries):
        if any(
            _passes_guards(entry, author_name, latin_author, None) and _predates(entry, first_year)
            for entry in group.entries
        ):
            continue
        passing = [entry for entry in group.entries if _passes_guards(entry, author_name, latin_author, first_year)]
        if passing:
            groups.append(TitleGroup(group.key, passing))
    return groups


def _evidence(entries: Sequence[EditionEntry]) -> EditionEntry:
    """The record that best proves the edition: a State Library record stating a translation, earliest first."""

    def rank(entry: EditionEntry) -> tuple[int, int]:
        order = 0 if entry.source == SOURCE_RSL and entry.states_translation else 1 if entry.source == SOURCE_RSL else 2
        return order, entry.year if entry.year is not None else 9999

    return min(entries, key=rank)


def decide(
    group: TitleGroup | None,
    *,
    author_name: str,
    latin_author: str,
    first_year: int | None,
    today: date,
) -> dict[str, Any]:
    """The record for one book, given the title group the model named (``None`` for none).

    ``author_name`` is the Cyrillic spelling searched with and
    ``latin_author`` the book's own author: an edition's credited author
    must correspond to both.

    A catalogue edition of the named title that passes every guard confirms
    it; a LiveLib result alone that passes them is ``unconfirmed``; anything
    else is ``no_find``. A title the author already published in Russian
    before the original appeared is another work, even when an undated
    edition of it (LitRes gives no edition year) would pass on its own.
    """
    if group is None or any(
        _passes_guards(entry, author_name, latin_author, None) and _predates(entry, first_year)
        for entry in group.entries
    ):
        return build_no_find(today)
    # The group holds every listed edition with the named title's normalised form.
    passing = [entry for entry in group.entries if _passes_guards(entry, author_name, latin_author, first_year)]
    catalogue = [entry for entry in passing if entry.source in CATALOGUE_SOURCES]
    if catalogue:
        evidence = _evidence(catalogue)
        sources = sorted({entry.source for entry in passing})
        both = {SOURCE_RSL, SOURCE_LITRES} <= set(sources)
        stated = any(entry.source == SOURCE_RSL and entry.states_translation for entry in catalogue)
        return build_confirmed(
            evidence,
            russian_author=author_name,
            sources=sources,
            confidence=CONFIDENCE_HIGH if both or stated else CONFIDENCE_MEDIUM,
            today=today,
        )
    located = next((entry for entry in passing if entry.source == SOURCE_LIVELIB), None)
    if located is not None:
        return build_unconfirmed(located, today=today)
    return build_no_find(today)


# ── The record ───────────────────────────────────────────────────────────


def build_confirmed(
    entry: EditionEntry, *, russian_author: str, sources: Sequence[str], confidence: str, today: date
) -> dict[str, Any]:
    return {
        "status": STATUS_CONFIRMED,
        "russian_title": entry.title,
        "russian_author": russian_author,
        "publisher": entry.publisher,
        "year": entry.year,
        "isbn": entry.isbn,
        "evidence_url": entry.url,
        "sources": list(sources),
        "confidence": confidence,
        "checked_on": today.isoformat(),
    }


def build_unconfirmed(entry: EditionEntry, *, today: date) -> dict[str, Any]:
    return {
        "status": STATUS_UNCONFIRMED,
        "russian_title": entry.title,
        "russian_author": entry.authors[0] if entry.authors else "",
        "evidence_url": entry.url,
        "checked_on": today.isoformat(),
    }


def build_no_find(today: date) -> dict[str, Any]:
    return {"status": STATUS_NO_FIND, "checked_on": today.isoformat()}


def _url_on(url: Any, hosts: Collection[str]) -> bool:
    # An address with whitespace or a control character in it is not a usable
    # address, and a line break in it would split a review row (#1181).
    if not isinstance(url, str) or not url or any(_unusable_url_character(char) for char in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and (parts.hostname or "").casefold() in hosts


def _unusable_url_character(char: str) -> bool:
    return char.isspace() or unicodedata.category(char) == "Cc"


def _is_iso_date(value: Any) -> bool:
    try:
        date.fromisoformat(str(value))
    except ValueError:
        return False
    return isinstance(value, str)


def _text(value: Any, *, required: bool) -> bool:
    return isinstance(value, str) and len(value) <= 400 and (bool(value.strip()) or not required)


def _valid_confirmed(value: Mapping[str, Any]) -> bool:
    year = value["year"]
    sources = value["sources"]
    if year is not None and (not isinstance(year, int) or isinstance(year, bool)):
        return False
    if (
        not isinstance(sources, list)
        or not sources
        or any(source not in (*CATALOGUE_SOURCES, SOURCE_LIVELIB) for source in sources)
    ):
        return False
    hosts = tuple(host for source in CATALOGUE_SOURCES if source in sources for host in EVIDENCE_HOSTS[source])
    # A LitRes edition states its language, so its Russian title may be Latin.
    latin_allowed = _url_on(value["evidence_url"], EVIDENCE_HOSTS[SOURCE_LITRES])
    return bool(
        _text(value["russian_title"], required=True)
        and (has_cyrillic(value["russian_title"]) or latin_allowed)
        and _text(value["russian_author"], required=True)
        and _text(value["publisher"], required=False)
        and _text(value["isbn"], required=False)
        and value["confidence"] in (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM)
        and _url_on(value["evidence_url"], hosts)
    )


def validate_russian_edition(value: Any) -> bool:
    """True when ``value`` is a complete, internally consistent ``russian_edition`` record."""
    if not isinstance(value, dict) or not _is_iso_date(value.get("checked_on")):
        return False
    status = value.get("status")
    if status == STATUS_CONFIRMED:
        return set(value) == set(CONFIRMED_KEYS) and _valid_confirmed(value)
    if status == STATUS_UNCONFIRMED:
        return bool(
            set(value) == set(UNCONFIRMED_KEYS)
            and _text(value["russian_title"], required=True)
            and has_cyrillic(value["russian_title"])
            and _text(value["russian_author"], required=True)
            and _url_on(value["evidence_url"], LOCATOR_HOSTS)
        )
    return status == STATUS_NO_FIND and set(value) == set(NO_FIND_KEYS)


def is_confirmed(value: Any) -> bool:
    return validate_russian_edition(value) and value["status"] == STATUS_CONFIRMED


# ── Model answer contract ────────────────────────────────────────────────

MATCH_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "number": {"type": ["integer", "null"]},
                },
                "required": ["candidate_id", "number"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["matches"],
    "additionalProperties": False,
}
_MATCH_KEYS = frozenset({"candidate_id", "number"})


def validate_match_response(payload: Any, counts: Mapping[str, int]) -> dict[str, int | None] | None:
    """The model's answer per candidate id, or ``None`` on a wrong shape.

    ``counts`` maps each requested candidate id to how many titles it was
    shown. A number outside 1..count is a malformed answer for that book and
    is dropped, so the book is asked again; ``null`` means none of them.
    Unknown ids are ignored; a duplicate id is a malformed response.
    """
    if not isinstance(payload, Mapping) or set(payload) != {"matches"} or not isinstance(payload["matches"], list):
        return None
    result: dict[str, int | None] = {}
    seen: set[str] = set()
    for item in payload["matches"]:
        if not isinstance(item, Mapping) or set(item) != _MATCH_KEYS:
            return None
        candidate_id, number = item["candidate_id"], item["number"]
        if not isinstance(candidate_id, str) or candidate_id in seen:
            return None
        if number is not None and (not isinstance(number, int) or isinstance(number, bool)):
            return None
        seen.add(candidate_id)
        if candidate_id not in counts:
            continue
        if number is None or 1 <= number <= counts[candidate_id]:
            result[candidate_id] = number
    return result
