"""Parse one Publishers Marketplace "Today's Deals" email into deal rows (Issue #943).

The parser is a pure function from the raw bytes of one message to its
identity, its deal rows and its rejections. It reads the ``text/plain`` part:
that part carries the real dealmaker profile links, while the HTML part wraps
every link in a tracking redirect.

The plain text is a sequence of blocks separated by blank lines. A block that
is a bold ``**Heading**`` names a section, a country or a category; every
other block inside a section is one deal paragraph, optionally followed by
contact lines. A deal paragraph has a fixed order:

    <credentials> <Author>'s TITLE IN CAPITALS, <logline>, to <Editor> <link>
    at <Publisher> <link>, <deal terms>, by <Agent> <link> at <Agency> <link>
    (<territory>). Rights also to ...

Every field comes from that text; nothing is guessed. A paragraph that does
not follow the shape is a rejection with a named reason code, never a row.

The rights-lead fields (Issue #948) come from the same text: the deal size
band and deal type from the deal terms after the to-clause, the publication
season or month and year from "for publication in ...", the territories
already sold from the parenthesised countries of the "Rights also to ..."
tail, the contact addresses from the contact lines after the paragraph, and
the author's earlier books from the capitalised titles that carry a retail
link in the credentials before the author's name. An element the deal does
not state stays empty.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import itertools
import re
import unicodedata
from dataclasses import dataclass
from dataclasses import field
from email.message import EmailMessage
from email.utils import parseaddr
from email.utils import parsedate_to_datetime
from typing import Any

NEWSLETTER_DAILY_DEALS = "daily_deals"

# The top-level sections of the email, in the email's own words. A heading
# outside this list is a category of the current section, or a country under
# International rights.
TOP_LEVEL_SECTIONS = frozenset(
    {"Fiction", "Non-fiction", "Poetry", "Children's", "Audio rights", "International rights"}
)
INTERNATIONAL_SECTION = "International rights"
# Under International rights, these headings divide one country's deals.
INTERNATIONAL_SUBSECTIONS = frozenset({"Fiction", "Non-fiction", "Poetry", "Children's"})
# The deals end at the account footer.
END_HEADINGS = frozenset({"Manage Your Information"})

# PM's deal size bands, smallest first (Issue #948).
DEAL_SIZE_BANDS = ("nice", "very nice", "good", "significant", "major")
# Deal types, in display order.
DEAL_TYPE_AUCTION = "auction"
DEAL_TYPE_PRE_EMPT = "pre-empt"
DEAL_TYPE_EXCLUSIVE_SUBMISSION = "exclusive submission"
DEAL_TYPE_MULTI_BOOK = "multi-book"
DEAL_TYPES = (DEAL_TYPE_AUCTION, DEAL_TYPE_PRE_EMPT, DEAL_TYPE_EXCLUSIVE_SUBMISSION, DEAL_TYPE_MULTI_BOOK)
PUBLICATION_SEASONS = ("spring", "summer", "fall", "winter")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_NUMBER_WORDS = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

REJECT_UNTITLED = "untitled_deal"
REJECT_NO_TITLE = "title_not_found"
REJECT_NO_AUTHORS = "authors_not_found"
REJECT_NO_PUBLISHER = "publisher_not_found"
REJECT_NO_PLAIN_TEXT = "no_plain_text_part"
REJECT_UNREADABLE_TEXT = "unreadable_plain_text"

_PROFILE_URL = "https://www.publishersmarketplace.com/dealmakers/detail.cgi?id="
_PROFILE_LINK = re.compile(r"\s*https?://www\.publishersmarketplace\.com/dealmakers/detail\.cgi\?id=(\d+)")
# Any other link (bookshop.org for earlier books, for example). A link can be
# glued to the punctuation that follows it: "WASHINGTON https://...: REFLECTIONS".
_OTHER_LINK = re.compile(r"\s*https?://[^\s]+?(?=[:;,.)]?(?:\s|$))")
_HEADING_BLOCK = re.compile(r"^\*\*(?P<heading>[^*]+?)\*\*\s*(?P<rest>.*)$", re.S)
_CONTACT_LINE = re.compile(r"^\S+@\S+\s+mailto:\S+")
_CONTACT_ADDRESS = re.compile(r"^(?:mailto:)?([^@\s]+@[^@\s]+\.[^@\s]+?)[.,;:]?$")
# A retail link (an earlier book's bookshop page) stands as this mark while the earlier books are read.
_LINK_MARK = "\u27ea\u27eb"
_SIZE_TERM = re.compile(r"\bin an?\s+(?P<size>very nice|nice|good|significant|major)\b(?:\s+[\w-]+){0,2}?\s+deal\b")
_BOOK_COUNT_TERM = re.compile(r"\b(?P<count>two|three|four|five|six|seven|eight|nine|ten|[2-9]|1[0-9])-book\b", re.I)
# A multi-book deal, with or without its count ("a three-book deal", "a multi-book deal").
_MULTI_BOOK_TERM = re.compile(
    r"\b(?:multi|multiple|two|three|four|five|six|seven|eight|nine|ten|[2-9]|1[0-9])-book\b", re.I
)
_AUCTION_TERM = re.compile(r"\bat auction\b", re.I)
_PRE_EMPT_TERM = re.compile(r"\bpre-?empt(?:ive)?\b", re.I)
_EXCLUSIVE_TERM = re.compile(r"\bexclusive submission\b", re.I)
_PUBLICATION_TERM = re.compile(
    r"\bfor publication in\s+(?:(?P<season>spring|summer|fall|autumn|winter|"
    + "|".join(_MONTHS)
    + r")\s+)?(?P<year>(?:19|20)\d\d)\b",
    re.I,
)
# A bookshop page: only a title linked to one of these is an earlier book. A link to a
# newsroom or an organisation in the credentials is not.
_RETAIL_HOSTS = re.compile(
    r"^(?:[\w-]+\.)*(?:bookshop\.org(?:\.uk)?|amazon\.[a-z.]+|barnesandnoble\.com|indiebound\.org|indigo\.ca)$",
    re.I,
)
_MARK = "⟦{}⟧"  # ⟦id⟧ stands for one dealmaker profile link.
_MARK_PATTERN = re.compile("⟦(\\d+)⟧")
_POSSESSIVE = re.compile(r"(?:['’]s|(?<=s)['’])(?=\s)")
_DESCRIPTOR_WORD = re.compile(r"^[a-z][a-z-]*$")
# Name words and initials are matched letter by letter, so that "José" and "Núñez" count.
_INITIALS = re.compile(r"^(?:[^\W\d_]\.)+$")
_WORD_JOINERS = "'’-"
_PARTICLES = frozenset({"de", "De", "van", "Van", "von", "der", "da", "du", "del", "la", "le", "bin", "ibn"})
# Role words that open a paragraph with a capital ("Novelist Juan Manuel García Márquez").
# They are dropped only at the start of the name run: as a surname ("Peter Singer") they stay.
_ROLE_WORDS = frozenset(
    {
        "Activist",
        "Actor",
        "Actress",
        "Ambassador",
        "Anthropologist",
        "Archaeologist",
        "Architect",
        "Artist",
        "Astronomer",
        "Athlete",
        "Attorney",
        "Author",
        "Biologist",
        "Chef",
        "Coach",
        "Columnist",
        "Comedian",
        "Correspondent",
        "Creator",
        "Critic",
        "Curator",
        "Designer",
        "Diplomat",
        "Director",
        "Economist",
        "Editor",
        "Entrepreneur",
        "Essayist",
        "Filmmaker",
        "Founder",
        "Historian",
        "Host",
        "Illustrator",
        "Journalist",
        "Lawyer",
        "Memoirist",
        "Musician",
        "Neuroscientist",
        "Novelist",
        "Nurse",
        "Philosopher",
        "Photographer",
        "Physician",
        "Physicist",
        "Playwright",
        "Podcaster",
        "Poet",
        "Producer",
        "Professor",
        "Psychiatrist",
        "Psychologist",
        "Reporter",
        "Researcher",
        "Scientist",
        "Screenwriter",
        "Senator",
        "Singer",
        "Sociologist",
        "Surgeon",
        "Teacher",
        "Translator",
        "Writer",
    }
)
_HONORIFICS = frozenset({"Dr.", "Mr.", "Mrs.", "Ms.", "Mx.", "Prof."})
_DEGREES = re.compile(r"(?:,\s*(?:MD|PhD|MSC|MSc|DPhil|RN|MPH|JD|MBA|DO|PsyD|EdD|DNP|II|III|IV|Jr\.|Sr\.))+$")
# Words before the name that mark the preceding capitalised words as an
# organisation ("Senior editor at Harbor Review Iris Mendel").
_ORGANISATION_LEAD = frozenset({"at", "of", "for", "from", "with", "behind", "the", "blog"})
# One linked party of a "to"/"by" clause. A person's name never holds a comma
# or crosses the words "to" and "by".
_PARTY_TEXT = r"(?:(?!\b(?:to|by)\s)[^,()\u27e6\u27e7])+?\s*\u27e6\d+\u27e7"
_PARTY = re.compile(_PARTY_TEXT)
# Further people follow "and" or a comma, never a deal term (", in a pre-empt", ", at auction").
_PARTY_SEPARATOR = r"(?:\s*,\s*and\s+|\s+and\s+|\s*&\s*|\s*,\s+(?!(?:by|to|in|at|for|with|on|and)\b))"
# A company name may hold commas ("Park, Fine & Brower Literary Management"); it ends at its link
# and never crosses into the next clause (", by Cora Vale ⟦3⟧").
_COMPANY_TEXT = r"(?:(?!,\s*(?:by|to)\s)[^()\u27e6\u27e7])+?\s*\u27e6\d+\u27e7"
_CLAUSE = rf"(?P<people>{_PARTY_TEXT}(?:{_PARTY_SEPARATOR}{_PARTY_TEXT})*)(?:\s+(?:while\s+)?at\s+(?P<company>{_COMPANY_TEXT}))?"
_TO_CLAUSE = re.compile(r"(?:^|(?<=[\s,]))to\s+" + _CLAUSE)
_BY_CLAUSE = re.compile(r"(?:^|(?<=[\s,]))by\s+" + _CLAUSE)
# An agent the email names without profile links: "by Amy A. Collins at Talcott Notch (world)".
# An unlinked company ends at a parenthesis, a comma or the end of the deal, never at a
# period inside its name ("A.M. Heath", "St. Martin's").
_UNLINKED_COMPANY_TEXT = r"[A-Z][^,()\u27e6\u27e7]*?(?=\s*[(,]|\.?\s*$)"
_UNLINKED_BY_CLAUSE = re.compile(
    rf"(?:^|(?<=[\s,]))by\s+(?P<people>[A-Z][^,()\u27e6\u27e7]*?)\s+at\s+(?P<company>{_UNLINKED_COMPANY_TEXT})"
)
# A linked editor at a publisher without a link: "to Ben Arbor ⟦1⟧ at Northlight Books (world)".
_UNLINKED_COMPANY_AFTER = re.compile(rf"\s+(?:while\s+)?at\s+(?P<company>{_UNLINKED_COMPANY_TEXT})")
_UNLINKED_PERSON_AT = re.compile(r"\s+(?:while\s+)?at\s+")
_SPLIT_AUTHORS = re.compile(r"\s*(?:,\s*and\s+|\s+and\s+|\s*&\s*|,\s+)\s*")


class PmDealEmailError(Exception):
    """A message that is not a readable PM deals email."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class Dealmaker:
    """A person or a company named in a deal, with its PM profile link when the email carries one."""

    name: str
    profile_url: str = ""

    def as_json(self) -> dict[str, str]:
        return {"name": self.name, "profile_url": self.profile_url}


@dataclass(frozen=True)
class Publication:
    """The expected publication the deal states: a year, with a season or a month when it names one.

    ``season`` is one of PUBLICATION_SEASONS or ""; ``month`` is 1 to 12, or 0 when the deal
    names none. A deal names at most one of the two.
    """

    year: int
    season: str = ""
    month: int = 0

    def as_json(self) -> dict[str, Any]:
        return {"year": self.year, "season": self.season, "month": self.month}


@dataclass(frozen=True)
class DealRow:
    section: str
    country: str
    category: str
    title: str
    authors: tuple[str, ...]
    editors: tuple[Dealmaker, ...]
    publisher: Dealmaker
    agents: tuple[Dealmaker, ...]
    agency: Dealmaker | None
    territory: str
    logline: str
    deal_size: str = ""
    deal_types: tuple[str, ...] = ()
    book_count: int = 0
    publication: Publication | None = None
    rights_also: tuple[str, ...] = ()
    contacts: tuple[str, ...] = ()
    earlier_books: tuple[str, ...] = ()

    @property
    def deal_key(self) -> str:
        """The row's identity across emails: the email carries no per-deal link."""
        return deal_key(self.title, self.authors, self.publisher.name)

    def as_json(self) -> dict[str, Any]:
        return {
            "deal_key": self.deal_key,
            "section": self.section,
            "country": self.country,
            "category": self.category,
            "title": self.title,
            "authors": list(self.authors),
            "editors": [editor.as_json() for editor in self.editors],
            "publisher": self.publisher.as_json(),
            "agents": [agent.as_json() for agent in self.agents],
            "agency": self.agency.as_json() if self.agency is not None else None,
            "territory": self.territory,
            "logline": self.logline,
            "deal_size": self.deal_size,
            "deal_types": list(self.deal_types),
            "book_count": self.book_count,
            "publication": self.publication.as_json() if self.publication is not None else None,
            "rights_also": list(self.rights_also),
            "contacts": list(self.contacts),
            "earlier_books": list(self.earlier_books),
        }


@dataclass(frozen=True)
class Rejection:
    """A deal paragraph the parser could not read. It carries no deal text."""

    section: str
    country: str
    category: str
    reason_code: str

    def as_json(self) -> dict[str, str]:
        return {
            "section": self.section,
            "country": self.country,
            "category": self.category,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class ParsedEmail:
    message_id: str
    message_date: str
    newsletter_kind: str
    rows: tuple[DealRow, ...] = field(default_factory=tuple)
    rejections: tuple[Rejection, ...] = field(default_factory=tuple)


def deal_key(title: str, authors: tuple[str, ...] | list[str], publisher: str) -> str:
    """A digest of the normalised title, authors and publisher."""
    parts = [_fold(title), "|".join(sorted(_fold(author) for author in authors)), _fold(publisher)]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:24]


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^\w]+", " ", stripped.casefold()).split())


def parse_message(raw: bytes, *, newsletter_kind: str = NEWSLETTER_DAILY_DEALS) -> ParsedEmail:
    """Parse the raw bytes of one deals email."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(message, EmailMessage)
    text = _plain_text(message)
    if text is None:
        raise PmDealEmailError(REJECT_NO_PLAIN_TEXT, "the message has no text/plain part")
    rows, rejections = parse_text(text)
    return ParsedEmail(
        message_id=str(message.get("Message-ID", "")).strip(),
        message_date=_message_date(message),
        newsletter_kind=newsletter_kind,
        rows=tuple(rows),
        rejections=tuple(rejections),
    )


def message_sender(raw_header_value: str) -> str:
    """The bare, lower-case address of a From header."""
    return parseaddr(raw_header_value)[1].strip().casefold()


def _plain_text(message: EmailMessage) -> str | None:
    part = message.get_body(preferencelist=("plain",))
    if part is None:
        return None
    try:
        content = part.get_content()
    except (LookupError, UnicodeError, ValueError):
        # An unknown or wrong charset: this message is unreadable, the pass goes on.
        raise PmDealEmailError(REJECT_UNREADABLE_TEXT, "the text/plain part cannot be decoded") from None
    return content if isinstance(content, str) else None


def _message_date(message: EmailMessage) -> str:
    value = message.get("Date")
    if not value:
        return ""
    try:
        return parsedate_to_datetime(str(value)).isoformat()
    except (TypeError, ValueError):
        return ""


def parse_text(text: str) -> tuple[list[DealRow], list[Rejection]]:
    """Parse the plain-text body of a deals email."""
    rows: list[DealRow] = []
    rejections: list[Rejection] = []
    section = country = category = ""
    for block in _blocks(text):
        heading_match = _HEADING_BLOCK.match(block)
        if heading_match is not None:
            heading = " ".join(heading_match.group("heading").split())
            if heading in END_HEADINGS:
                break
            if heading in TOP_LEVEL_SECTIONS and not (
                section == INTERNATIONAL_SECTION and country and heading in INTERNATIONAL_SUBSECTIONS
            ):
                section, country, category = heading, "", ""
            elif section == INTERNATIONAL_SECTION:
                if heading in INTERNATIONAL_SUBSECTIONS:
                    category = heading
                else:
                    country, category = heading, ""
            elif section:
                category = heading
            # A heading can share its block with the first deal ("**Poetry** Author of ...").
            block = heading_match.group("rest").strip()
            if not block:
                continue
        if not section:
            continue  # The preamble before the first section carries no deals.
        paragraph = _deal_paragraph(block)
        if not paragraph:
            continue
        outcome = parse_paragraph(
            paragraph, section=section, country=country, category=category, contacts=_contact_addresses(block)
        )
        (rows if isinstance(outcome, DealRow) else rejections).append(outcome)  # type: ignore[arg-type]
    return rows, rejections


def _blocks(text: str) -> list[str]:
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    return [block.strip() for block in re.split(r"\n\s*\n", normalised) if block.strip()]


def _deal_paragraph(block: str) -> str:
    """The deal text of a block, without its contact lines."""
    lines = [line.strip() for line in block.split("\n")]
    return " ".join(line for line in lines if line and not _CONTACT_LINE.match(line))


def _contact_addresses(block: str) -> tuple[str, ...]:
    """The addresses of a block's contact lines, in order, once each."""
    addresses: list[str] = []
    for line in block.split("\n"):
        line = line.strip()
        if not _CONTACT_LINE.match(line):
            continue
        match = _CONTACT_ADDRESS.match(line.split()[0])
        if match is not None and match.group(1) not in addresses:
            addresses.append(match.group(1))
    return tuple(addresses)


def parse_paragraph(
    paragraph: str, *, section: str, country: str, category: str, contacts: tuple[str, ...] = ()
) -> DealRow | Rejection:
    """Parse one deal paragraph into a row, or a rejection with a named reason."""

    def reject(reason_code: str) -> Rejection:
        return Rejection(section=section, country=country, category=category, reason_code=reason_code)

    links: list[str] = []

    def mark(match: re.Match[str]) -> str:
        links.append(match.group(1))
        return " " + _MARK.format(len(links) - 1)

    text = _PROFILE_LINK.sub(mark, paragraph)
    # The retail links stay as marks in one copy, where they show which capitals are earlier books.
    linked = " ".join(_OTHER_LINK.sub(_retail_mark, text).split())
    text = " ".join(_OTHER_LINK.sub("", text).split())
    # "Rights also to ..." lists the territories already sold; it is not the main deal.
    main, *rights_tail = re.split(r"\.\s+Rights also to\s", text, maxsplit=1)

    found = _find_title(main)
    if found is None:
        return reject(REJECT_UNTITLED if re.search(r"['’]s\s+untitled\b", main) else REJECT_NO_TITLE)
    name_end, title, after_title = found
    authors = _authors(main[:name_end])
    if not authors:
        return reject(REJECT_NO_AUTHORS)

    rest = main[after_title:]
    to_match = _TO_CLAUSE.search(rest)
    if to_match is None:
        return reject(REJECT_NO_PUBLISHER)
    to_end = to_match.end()
    unlinked_publisher = None if to_match.group("company") else _UNLINKED_COMPANY_AFTER.match(rest, to_end)
    if unlinked_publisher is not None:
        editors = _people(to_match, links)
        publisher: Dealmaker | None = Dealmaker(name=unlinked_publisher.group("company").strip())
        to_end = unlinked_publisher.end()
    else:
        editors, publisher = _parties(to_match, links, lone_party_is_company=True)
    if publisher is None:
        return reject(REJECT_NO_PUBLISHER)
    logline = rest[: to_match.start()].strip(" ,")

    tail = rest[to_end:]
    agents: tuple[Dealmaker, ...] = ()
    agency: Dealmaker | None = None
    by_match = _BY_CLAUSE.search(tail)
    if by_match is not None:
        agents, agency = _parties(by_match, links, lone_party_is_company=False)
    else:
        unlinked = _UNLINKED_BY_CLAUSE.search(tail)
        if unlinked is not None:
            agents = tuple(
                Dealmaker(name=name.strip()) for name in _SPLIT_AUTHORS.split(unlinked.group("people")) if name.strip()
            )
            agency = Dealmaker(name=unlinked.group("company").strip())
    # The first parenthesis after the publisher is the territory: "(NA)", "(world English)",
    # or, under International rights, the buying country "(Brazil)".
    territory_match = re.search(r"\(([^()]+)\)", tail)
    # The deal terms follow the to-clause; the logline before it never counts.
    terms = _MARK_PATTERN.sub("", rest[to_match.start() :])
    return DealRow(
        section=section,
        country=country,
        category=category,
        title=title,
        authors=authors,
        editors=editors,
        publisher=publisher,
        agents=agents,
        agency=agency,
        territory=territory_match.group(1).strip() if territory_match else "",
        logline=_MARK_PATTERN.sub("", logline).strip(),
        deal_size=_deal_size(terms),
        deal_types=_deal_types(terms),
        book_count=_book_count(terms),
        publication=_publication(terms),
        rights_also=_rights_also(rights_tail[0] if rights_tail else ""),
        contacts=contacts,
        earlier_books=_earlier_books(linked, title),
    )


def _deal_size(terms: str) -> str:
    match = _SIZE_TERM.search(terms)
    return match.group("size") if match else ""


def _book_count(terms: str) -> int:
    match = _BOOK_COUNT_TERM.search(terms)
    if match is None:
        return 0
    count = match.group("count").casefold()
    return _NUMBER_WORDS.get(count) or int(count)


def _deal_types(terms: str) -> tuple[str, ...]:
    found = {
        DEAL_TYPE_AUCTION: _AUCTION_TERM.search(terms),
        DEAL_TYPE_PRE_EMPT: _PRE_EMPT_TERM.search(terms),
        DEAL_TYPE_EXCLUSIVE_SUBMISSION: _EXCLUSIVE_TERM.search(terms),
        DEAL_TYPE_MULTI_BOOK: _MULTI_BOOK_TERM.search(terms),
    }
    return tuple(kind for kind in DEAL_TYPES if found[kind])


def _publication(terms: str) -> Publication | None:
    match = _PUBLICATION_TERM.search(terms)
    if match is None:
        return None
    year = int(match.group("year"))
    word = (match.group("season") or "").casefold()
    if not word:
        return Publication(year=year)
    if word in {"autumn", "fall"}:
        return Publication(year=year, season="fall")
    if word in PUBLICATION_SEASONS:
        return Publication(year=year, season=word)
    return Publication(year=year, month=[month.casefold() for month in _MONTHS].index(word) + 1)


def _rights_also(tail: str) -> tuple[str, ...]:
    """The territories already sold: each parenthesis of the "Rights also to ..." tail, once."""
    territories: list[str] = []
    for group in re.findall(r"\(([^()]+)\)", _MARK_PATTERN.sub("", tail)):
        territory = " ".join(group.split())
        if territory and territory not in territories:
            territories.append(territory)
    return tuple(territories)


def _retail_mark(match: re.Match[str]) -> str:
    host = re.sub(r"^\s*https?://", "", match.group(0)).split("/", 1)[0].split(":", 1)[0]
    return _LINK_MARK if _RETAIL_HOSTS.match(host) else ""


def _is_title_word(word: str) -> bool:
    """A word in capitals: letters of any script, digits and punctuation, but no lower-case letter."""
    return any(char.isalnum() for char in word) and not any(char.islower() for char in word)


def _extend_title(run: list[str], word: str) -> list[str]:
    """The title run with *word* added, or an empty run when *word* ends it.

    Punctuation standing alone ("&", "-", "/") continues a run that has begun.
    """
    if _is_title_word(word):
        return [*run, word]
    if run and word and not any(char.isalnum() for char in word):
        return [*run, word]
    return []


def _earlier_books(linked: str, title: str) -> tuple[str, ...]:
    """The titles in the credentials before the author's name that carry a retail link.

    A title is the run of capitalised words that ends at its link, with a subtitle
    glued to the link ("WASHINGTON https://...: REFLECTIONS"). Only a linked title
    counts: capitals without a link may be an acronym or an organisation, and an
    earlier book is never guessed.
    """
    main = re.split(r"\.\s+Rights also to\s", linked, maxsplit=1)[0]
    possessive = re.search(r"(?:['’]s|(?<=s)['’])\s+(?:[a-z][a-z-]*\s+){0,4}?" + re.escape(title.split()[0]), main)
    if possessive is None:
        return ()
    tokens = main[: possessive.start()].split()
    books: list[str] = []
    run: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if _LINK_MARK not in token:
            run = _extend_title(run, token)
            continue
        before, after = token.split(_LINK_MARK, 1)
        if before:
            run = _extend_title(run, before)
        words, run = run, []
        if words and after.startswith(":"):
            subtitle = [after[1:]] if _is_title_word(after[1:]) else []
            while index < len(tokens):
                head, mark, _rest = tokens[index].partition(_LINK_MARK)
                if not _extend_title(subtitle or ["start"], head):
                    break
                subtitle.append(head)
                index += 1
                if mark:
                    break
            else:
                mark = ""
            if not mark:
                # A subtitle with no link of its own ends before the author's initials.
                while subtitle and _INITIALS.match(subtitle[-1]):
                    subtitle.pop()
            if subtitle:
                words = [*words[:-1], words[-1] + ":", *subtitle]
        book = " ".join(" ".join(words).replace(_LINK_MARK, "").split())
        book = re.sub(r"[\s,;:&/\-–—]+$", "", book)  # Punctuation that ended the run is no part of it.
        if sum(char.isupper() for char in book) >= 2 and book != title and book not in books:
            books.append(book)
    return tuple(books)


def _find_title(text: str) -> tuple[int, str, int] | None:
    """The first possessive followed by a title in capitals: (name end, title, title end)."""
    for possessive in _POSSESSIVE.finditer(text):
        position = possessive.end()
        # Up to four lower-case words may sit between the name and the title ("'s adult debut TITLE").
        descriptor = re.match(r"(?:\s+[a-z][a-z-]*){0,4}?\s+(?=[^a-z\s])", text[position:])
        if descriptor is None:
            continue
        start = position + descriptor.end()
        end = start
        while end < len(text) and not text[end].islower():
            end += 1
        candidate = text[start:end]
        if not candidate:
            continue
        if end < len(text) and text[end].islower() and not candidate.rstrip().endswith(","):
            # The capitals ran past the title into the logline ("TITLE, AI-driven ...",
            # "TITLE, US history ..."): the title ends at the last comma.
            comma = candidate.rfind(", ")
            if comma > 0:
                candidate = candidate[:comma]
                end = start + comma
            elif not candidate.endswith(" "):
                continue  # The run stopped inside a word: no title boundary.
        title = candidate.strip(" ,")
        if sum(char.isupper() for char in title) < 2 or _MARK_PATTERN.search(title):
            continue
        return possessive.start(), title, start + len(candidate)
    return None


def _authors(before_possessive: str) -> tuple[str, ...]:
    """The author names at the end of the text before the possessive.

    The text is "<credentials> <names>". The names are the trailing run of
    name words. When the run follows a preposition or article, an
    organisation's capitalised words run into the names ("Senior editor at
    Harbor Review Iris Mendel"), and only the last given name and surname are
    kept.
    """
    text = _DEGREES.sub("", before_possessive.strip()).strip()
    tokens = text.split()
    index = len(tokens)
    while index > 0:
        token = tokens[index - 1]
        bare = token.rstrip(",")
        if _is_name_word(bare) or (bare in {"and", "&"} and index < len(tokens)):
            index -= 1
            continue
        break
    run = tokens[index:]
    lead = tokens[index - 1] if index > 0 else ""
    while run and run[0].rstrip(",") in {"and", "&"}:
        run = run[1:]
    # A role word in the first name is a credential when a full name follows it
    # ("Bestselling Novelist Juan García"); at the end it is a surname ("Peter Singer").
    first = list(itertools.takewhile(lambda word: word.rstrip(",") not in {"and", "&"}, run))
    for position in range(len(first) - 3, -1, -1):
        if first[position] in _ROLE_WORDS:
            lead, run = first[position], run[position + 1 :]
            break
    if not run:
        return ()
    names = [name for name in _SPLIT_AUTHORS.split(" ".join(run)) if name.strip()]
    core = len(_core_words(names[0])) if names else 0
    # Only a preposition or article before the run shows that an organisation's name runs
    # into the author's; a long name with no such lead ("Juan Manuel García Márquez") stays whole.
    if lead in _ORGANISATION_LEAD and core > 2:
        names[0] = _last_full_name(names[0])
    authors = tuple(_strip_honorific(name) for name in names)
    return tuple(author for author in authors if len(_core_words(author)) >= 1 and author)


def _is_name_word(token: str) -> bool:
    if token in _PARTICLES or token in _HONORIFICS:
        return True
    if _INITIALS.match(token):
        return token[0].isupper()
    word = token[:-1] if token.endswith(".") else token
    letters = [char for char in word if char not in _WORD_JOINERS]
    # A capitalised word with at least one lower-case letter: "Núñez", "McGuire", "O'Brien".
    # An all-capitals word is a title, not a name.
    return (
        bool(letters)
        and word[0].isupper()
        and all(char.isalpha() for char in letters)
        and any(char.islower() for char in letters)
    )


def _core_words(name: str) -> list[str]:
    return [word for word in name.split() if not _INITIALS.match(word) and word not in _HONORIFICS]


def _last_full_name(name: str) -> str:
    """Surname, initials and particles, one given name, and a preceding honorific."""
    words = name.split()
    kept = [words.pop()]
    while words and (_INITIALS.match(words[-1]) or words[-1] in _PARTICLES):
        kept.insert(0, words.pop())
    if words:
        kept.insert(0, words.pop())
    if words and words[-1] in _HONORIFICS:
        kept.insert(0, words.pop())
    return " ".join(kept)


def _strip_honorific(name: str) -> str:
    words = name.strip(" ,").split()
    while words and words[0] in _HONORIFICS:
        words = words[1:]
    return " ".join(words)


def _parties(
    clause: re.Match[str], links: list[str], *, lone_party_is_company: bool
) -> tuple[tuple[Dealmaker, ...], Dealmaker | None]:
    """People and their company from "A ⟦1⟧ and B ⟦2⟧ at Company ⟦3⟧"."""
    people = list(_people(clause, links))
    company = _dealmaker(clause.group("company"), links) if clause.group("company") else None
    if company is None and len(people) == 1:
        # "to Ben Arbor at Northlight Books ⟦3⟧": only the company has a link, so the
        # person and the company arrive as one party.
        parts = _UNLINKED_PERSON_AT.split(people[0].name, maxsplit=1)
        if len(parts) == 2 and parts[0] and parts[1]:
            names = (name.strip() for name in _SPLIT_AUTHORS.split(parts[0]))
            return (
                tuple(Dealmaker(name=name) for name in names if name),
                Dealmaker(name=parts[1], profile_url=people[0].profile_url),
            )
    if company is None and lone_party_is_company and len(people) == 1:
        # "to Reservoir Books ⟦4⟧ (Spain)": a publisher with no named editor.
        return (), people[0]
    return tuple(people), company


def _people(clause: re.Match[str], links: list[str]) -> tuple[Dealmaker, ...]:
    return tuple(
        person
        for person in (_dealmaker(part, links) for part in _PARTY.findall(clause.group("people")))
        if person is not None
    )


def _dealmaker(text: str, links: list[str]) -> Dealmaker | None:
    marks = _MARK_PATTERN.findall(text)
    name = " ".join(_MARK_PATTERN.sub("", text).split()).strip(" ,")
    name = re.sub(r"^(?:and|&)\s+", "", name).strip(" ,")
    if not name:
        return None
    return Dealmaker(name=name, profile_url=_PROFILE_URL + links[int(marks[-1])] if marks else "")
