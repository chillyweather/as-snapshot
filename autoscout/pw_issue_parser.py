"""Pure parsing of PW page texts; review prose never crosses this boundary (#954)."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# 13 digits, so numbers in the review prose after the ISBN stay out. Digits glued
# to the 13th stay in, so an overlong token fails validation instead of being cut.
ISBN = re.compile(r"\bISBN\s+(97[89](?:[\s\-–]{0,3}\d){10}(?:-?\d)*)")
# A review ends with its publication month, or "(Self-published)" in its place.
SELF_PUBLISHED = "Self-published"
END = re.compile(r"\((Jan\.|Feb\.|Mar\.|Apr\.|May|June|July|Aug\.|Sept\.|Oct\.|Nov\.|Dec\.|Self-published)\)")
SUBSECTIONS = (
    "General Nonfiction",
    "Food & Cooking",
    "Health, Home & Hobbies",
    "History",
    "Biography & Memoir",
    "Science",
    "Religion & Spirituality",
    "Business & Economics",
    "Art & Photography",
    "Politics & Current Affairs",
    "Travel",
    "Lifestyle",
    "Comics",
)
# Headings that only a Nonfiction section prints. Comics and Lifestyle also head
# Fiction or Children's pages, so they say nothing about the section.
NONFICTION_ONLY = tuple(h for h in SUBSECTIONS if h not in ("Comics", "Lifestyle"))
# Headings of the other review sections (Fiction, then Children's & YA).
OTHER_SECTION_HEADINGS = (
    "Mysteries & Thrillers",
    "SF, Fantasy & Horror",
    "Romance & Erotica",
    "Poetry",
    "Picture Books",
    "Fiction",
    "Nonfiction",
)
# Issues of 12 and 19 Oct 2026 print no running header on any review page.
OPENER_BLOCK_PAGES = 15
# Stands in for a page the PDF reader could not extract (12 Oct 2026, p. 91).
UNREADABLE_PAGE = "\x00pw-unreadable-page"
HEADER = re.compile(r"^(?:Nonfiction|Fiction|Children[’']s(?: & YA)?|Poetry|Lifestyle) Reviews$", re.I)
BIBLIOGRAPHY = re.compile(
    r"^(?P<authors>.+?)(?<![Tt]rans)(?<![Ee]d)(?<!illus)(?<!\b[A-Z])\.\s+(?:(?:Trans|Ed)\.\s+[^.]+\.\s+)?(?P<publisher>.+?),\s*"
    r"(?P<price>\$\d+(?:\.\d{1,2})?)(?:\s+(?P<format>[^()]+?))?\s*"
    r"\((?P<pages>\d+)p\)\s*[.,]?$"
)
# A line that leaves the author list open, so the next line still belongs to it.
AUTHOR_CONTINUES = re.compile(r"(?:\band|&|,|\bby|\bthe)$|,\s*(?:trans\.|illus\.|ed\.|edited\b|translated\b)", re.I)
# A bibliography that no ISBN claimed: a price followed by a page count.
ENTRY_WITHOUT_ISBN = re.compile(r"\$\d[^$]{0,60}?\(\d+p\)")


@dataclass(frozen=True)
class ParsedIssue:
    rows: list[dict[str, Any]]
    rejections: dict[str, int]
    excluded_paid_self_published: int
    # Unreadable pages inside, or right before, a declared section. The issue
    # cannot be read whole, so its caller fails it instead of guessing entries.
    unreadable_section_pages: int = 0


def valid_isbn(value: str) -> bool:
    return (
        len(value) == 13
        and value.isdigit()
        and value.startswith(("978", "979"))
        and sum(int(n) * (1 if i % 2 == 0 else 3) for i, n in enumerate(value)) % 10 == 0
    )


def _clean_lines(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text)
    # InDesign glues the running section header to the page-number furniture.
    text = re.sub(
        r"(?m)^.*?PUBLISHERS[ ]+WEEKL[ ]*Y[^\n]*?(Nonfiction Reviews|Fiction Reviews|Children[’']s & YA Reviews)\s*$",
        r"\1",
        text,
    )
    return [
        " ".join(line.split())
        for line in text.splitlines()
        if line.strip()
        and not re.match(r"P\s*H\s*O\s*T\s*O\s*(?:©|CREDIT)", line.strip(), re.I)
        and not ("©" in line and line.strip().isupper())
    ]


# Some issues encode an fi/fl/ff ligature as plain letters and still leave the
# glyph gap after it ("Confi scation", "Aff ordability"). In a title-case title
# a lowercase word after such a fragment is the rest of the same word, unless it
# is a function word or the fragment is a word of its own.
_BARE_LIGATURE_GAP = re.compile(
    r"(?<![-\w])(\w*(?:ffi|ffl|fi|fl|ff))\s+"
    r"(?=[a-z])(?!(?:the|of|and|to|for|or|a|an|on|by|as|is|with|from)\b)(?=([a-z]+))"
)
_WORDS_ENDING_IN_A_LIGATURE = frozenset(
    "wifi hifi lofi scifi sufi amalfi gaddafi qaddafi "
    "off staff stuff cliff sheriff tariff bluff buff cuff puff huff riff stiff sniff whiff scoff fluff gruff "
    "scruff snuff duff chaff gaff quaff distaff flagstaff plaintiff bailiff mastiff pontiff midriff handcuff "
    "foodstuff dandruff playoff kickoff layoff payoff takeoff standoff spinoff liftoff cutoff runoff tradeoff "
    "jeff geoff".split()
)


def _join_bare_ligature_gap(match: re.Match[str]) -> str:
    fragment, rest = match[1], match[2]
    if fragment.lower() in _WORDS_ENDING_IN_A_LIGATURE:
        return match[0]
    # "at" and "in" follow a real ff word far more often than they finish one.
    if fragment.endswith("ff") and rest in ("at", "in"):
        return match[0]
    return fragment


def _repair_title_glyphs(title: str) -> str:
    # The PDF text layer encodes some T/W/Y/V initials and fi/ft ligatures as
    # separate glyph runs. Repair only bibliographic titles, not review prose.
    # A ligature arrives as a glyph name ("/f_i", "/f_f_i"); the gap after it is an
    # artifact only inside a word, so a space before a capital or a function word stays.
    title = re.sub(
        r"/([A-Za-z](?:_[A-Za-z])+)(\s+(?=[a-z])(?!(?:the|of|and|to|for|or)\b))?",
        lambda match: match[1].replace("_", ""),
        title,
    )
    title = _BARE_LIGATURE_GAP.sub(_join_bare_ligature_gap, title)
    title = re.sub(r"\b([TW])\s+([a-z]+)\b", r"\1\2", title)
    # V and Y also stand alone ("Henry V at Agincourt", "Generation Y in the Workplace").
    title = re.sub(
        r"\b([VY])\s+(?!(?:and|for|the|of|or|vs|is|to|at|in|on|by|as|with|from|into|versus|via|per|upon"
        r"|word|chromosome)\b)([a-z]{2,})\b",
        r"\1\2",
        title,
    )
    # A running page header ("50 PUBLISHERS WEEKL Y MA Y 11, 2026 ...", or date first) glued to a title.
    header = re.match(r".*PUBLISHERS\s+WEEKL\s?Y\s+", title)
    if header:
        title = title[header.end() :]
        # What follows the masthead is a page number or the issue date, in either page layout.
        title = re.sub(r"^(?:\d+\s+|[A-Z][A-Z &\d]*,\s+20\d\d\s+)", "", title)
    # Kerning gaps around apostrophes and punctuation ("Harmony , from").
    title = re.sub(r"(\w)\s?’\s?(s|t|re|ve|ll|d|m)\b", r"\1’\2", title)
    title = re.sub(r"\s+([,:;?!)])", r"\1", title)
    return re.sub(r"(\w)\s+\.(?=\s|$)", r"\1.", title)


def _repair_author_glyphs(author: str) -> str:
    # Two observed surname splits from the same PDF font encoding. Keep this
    # narrow: ordinary multiword surnames must retain their spaces.
    return re.sub(r"\b(?:Tiff any|Brookfi eld)\b", lambda match: match[0].replace(" ", ""), author)


def _metadata(lines: list[str]) -> dict[str, Any] | None:
    # Titles and bibliography have separate lines. Trying bounded suffixes handles
    # wrapped bibliographies without absorbing surrounding prose into the title.
    if any(re.search(r'[“”„]|^"|ADVERTISEMENT|BUY NOW|ORDER NOW', line, re.I) for line in lines):
        return None
    matches = []
    for start in range(max(1, len(lines) - 3), len(lines)):
        match = BIBLIOGRAPHY.fullmatch(" ".join(lines[start:]))
        if match:
            matches.append((start, match))
    if not matches:
        return None
    # The shortest suffix is the bibliography unless the line before it leaves
    # the author list open; a title never ends that way.
    chosen = next(
        ((start, match) for start, match in reversed(matches) if not AUTHOR_CONTINUES.search(lines[start - 1])), None
    )
    if chosen is None:
        return None
    start, match = chosen
    title = _repair_title_glyphs(" ".join(lines[:start]).strip("★ "))
    authors_text = match["authors"]
    authors_text = re.sub(r"^[Ee]dited by\s+", "", authors_text)
    # Contributor roles belong to metadata, never the primary author list.
    authors_text = re.split(r",?\s+(?:[Ee]d\.|[Ee]dited|[Tt]rans\.|[Tt]ranslated|illus\.|illustrated)", authors_text)[0]
    authors = [
        _repair_author_glyphs(part.strip()) for part in re.split(r",?\s+and\s+|,\s*", authors_text) if part.strip()
    ]
    # A one-word author means a name wrapped mid-way; the split is ambiguous.
    if not title or not authors or len(lines[:start]) > 4 or any(" " not in author for author in authors):
        return None
    return {
        "title": title,
        "primary_authors": authors,
        "publisher": match["publisher"],
        "price": match["price"],
        "format": (match["format"] or "").strip(" ,"),
        "page_count": int(match["pages"]),
        "starred": any("★" in line for line in lines[:start]),
    }


PageLayout = tuple[list[str], list[str], bool, bool]
# The 2 Nov 2026 issue opens Children's two pages before its first running header.
OPENER_LOOKAHEAD_PAGES = 2


def _page_layout(page: str) -> PageLayout:
    lines = _clean_lines(page)
    paid = any(
        # Page furniture is upper case; review prose may mention paid reviews.
        line.endswith("PAID REVIEWS")
        or re.search(r"\bBOOKLIFE\b", line)
        or re.sub(r"\s", "", line) in ("BOOKLIFE", "BookLife", "BookLifeReviews")
        for line in lines
    )
    headers = [line for line in lines if HEADER.fullmatch(line)]
    return lines, headers, paid, not paid and not headers and "Reviews" in lines


def _heading_key(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def _block_section(layout: list[PageLayout], index: int) -> str | None:
    """Nonfiction when an opener's block prints only Nonfiction subsection headings.

    The block runs from the opener to the next opener, paid page or running header,
    within a bound. Any Fiction or Children's heading leaves it unclassified. PW
    prints subsection headings in capitals, so a title-case book title such as
    "Fiction" is not evidence.
    """
    nonfiction = {_heading_key(h) for h in NONFICTION_ONLY}
    other = {_heading_key(h) for h in OTHER_SECTION_HEADINGS}
    found = False
    for offset, (lines, headers, paid, opener) in enumerate(layout[index : index + OPENER_BLOCK_PAGES]):
        if offset and (paid or opener or headers):
            break
        keys = {_heading_key(line) for line in lines if line == line.upper()}
        if keys & other:
            return None
        found = found or bool(keys & nonfiction)
    return "Nonfiction Reviews" if found else None


def _opener_section(layout: list[PageLayout], index: int) -> str | None:
    for _lines, headers, paid, opener in layout[index + 1 : index + 1 + OPENER_LOOKAHEAD_PAGES]:
        if paid or opener or len(headers) > 1:
            return None
        if headers:
            return headers[0]
    return None


def _starts_section(layout: list[PageLayout], index: int, declared: set[str], *, opener: bool = False) -> bool:
    """Whether the page at index opens or continues a declared section.

    An unreadable page is classified as if it were an opener, since it may have been one.
    """
    if index >= len(layout):
        return False
    _lines, headers, paid, is_opener = layout[index]
    opener = opener or is_opener
    if paid or len(headers) > 1:
        return False
    if headers:
        return headers[0].casefold() in declared
    section = (_opener_section(layout, index) or _block_section(layout, index)) if opener else None
    return (section or "").casefold() in declared


def _entries_without_isbn(text: str) -> Counter[str]:
    """Count bibliographies that no ISBN claimed, one per review.

    A self-published book often carries an ASIN instead; it is excluded, not missing.
    """
    parts = END.split(text)
    counts: Counter[str] = Counter()
    for piece, end in zip(parts[::2], [*parts[1::2], None], strict=True):
        if ENTRY_WITHOUT_ISBN.search(piece):
            counts["self_published" if end == SELF_PUBLISHED else "pw_isbn_missing"] += 1
    return counts


def parse_pages(
    pages: list[str],
    issue_date: str,
    *,
    sections: tuple[str, ...] = ("Nonfiction Reviews",),
    _on_review: Callable[[dict[str, Any], str], None] | None = None,
) -> ParsedIssue:
    """An explicit header, or an opener followed by one, starts a section.

    Continuation stops at the next section.

    Unknown layouts are counted as rejections. Prose is used only to locate the
    end month and optional agent, and is never returned, even on rejection.
    """
    rows: list[dict[str, Any]] = []
    rejected: Counter[str] = Counter()
    excluded = 0
    subsection = ""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    declared = {section.casefold() for section in sections}
    layout = [_page_layout(page) for page in pages]
    unreadable = 0
    for index, (page, (lines, headers, paid, opener)) in enumerate(zip(pages, layout, strict=True)):
        if page == UNREADABLE_PAGE:
            # Read as empty outside the review sections. Inside one, or right before
            # one starts, a review may have been lost or cut in half.
            if (
                current is not None
                or _starts_section(layout, index, declared, opener=True)
                or _starts_section(layout, index + 1, declared)
            ):
                unreadable += 1
            continue
        if paid:
            excluded += max(1, len(ISBN.findall(page)))
        if paid or len(headers) > 1 or (headers and headers[0].casefold() not in declared):
            current = None
            continue
        if opener:
            # A section opener prints only the generic "Reviews" logo. It takes
            # the section of the next explicitly headed page, within a bound, and
            # stays unclassified when another opener or paid page comes first.
            if not _starts_section(layout, index, declared):
                current = None
                continue
            current = []
            blocks.append(current)
        elif headers and current is None:
            current = []
            blocks.append(current)
        if current is None:
            continue
        for line in lines:
            if (
                HEADER.fullmatch(line)
                or (opener and line == "Reviews")
                or re.fullmatch(r"\d+|.*PUBLISHERS WEEKLY.*|publishersweekly\.com.*", line)
            ):
                continue
            heading = next(
                (h for h in SUBSECTIONS if re.sub(r"[^a-z]", "", h.lower()) == re.sub(r"[^a-z]", "", line.lower())),
                None,
            )
            current.append(heading or line)
    for lines in blocks:
        subsection = ""
        text = "\n".join(lines)
        cursor = 0
        isbn_matches = list(ISBN.finditer(text))
        for index, isbn_match in enumerate(isbn_matches):
            before = text[cursor : isbn_match.start()].strip()
            # A page may open with the end of the preceding review.
            preceding_ends = list(END.finditer(before))
            if preceding_ends:
                unclaimed = _entries_without_isbn(before[: preceding_ends[-1].end()])
                excluded += unclaimed.pop("self_published", 0)
                rejected.update(unclaimed)
                before = before[preceding_ends[-1].end() :].strip()
            metadata_lines = before.splitlines()
            for heading in SUBSECTIONS:
                if heading in metadata_lines:
                    subsection = heading
                    metadata_lines = metadata_lines[metadata_lines.index(heading) + 1 :]
            next_start = isbn_matches[index + 1].start() if index + 1 < len(isbn_matches) else len(text)
            tail = text[isbn_match.end() : next_start]
            end = END.search(tail)
            cursor = isbn_match.end() + (end.end() if end else len(tail))
            if (end and end[1] == SELF_PUBLISHED) or re.search(r"\bself[- ]published,", before, re.I):
                excluded += 1
                continue
            isbn = re.sub(r"\D", "", isbn_match[1])
            if not valid_isbn(isbn):
                rejected["pw_isbn_invalid"] += 1
                continue
            metadata = _metadata(metadata_lines)
            if metadata is None:
                rejected["pw_author_line_missing_or_unreadable"] += 1
                continue
            if end is None:
                rejected["pw_entry_unreadable"] += 1
                continue
            agent = re.search(r"Agent:\s*([^,]{1,100}),\s*(.{1,150}?)\.\s*$", tail[: end.start()].strip(), re.S)
            # A second sentence before the month is not agency metadata; keep the book, drop the agent.
            if agent and re.search(r"\.\s", agent[2]):
                agent = None
            row = {
                **metadata,
                "isbn13": isbn,
                "subsection": subsection,
                "agent": " ".join(agent[1].split()) if agent else "",
                "agency": " ".join(agent[2].split()) if agent else "",
                "publication_month": end[1],
                "issue_date": issue_date,
            }
            rows.append(row)
            if _on_review is not None:
                # Only the explicit local-description caller receives prose.
                # The regular PW pass has no callback and retains metadata only.
                review = tail[: end.start()]
                if agent is not None:
                    review = review[: agent.start()]
                _on_review(row, " ".join(review.split()))
        unclaimed = _entries_without_isbn(text[cursor:])
        excluded += unclaimed.pop("self_published", 0)
        rejected.update(unclaimed)
    return ParsedIssue(rows, {k: v for k, v in sorted(rejected.items()) if v}, excluded, unreadable)
