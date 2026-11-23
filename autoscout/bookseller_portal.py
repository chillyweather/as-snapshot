"""«Idioma original» and the page count from the booksellers' portal (issue #1140).

todostuslibros.com is the portal of CEGAL, the Spanish booksellers'
confederation. Its book page states the original language of a Spanish
edition («Idioma original») and its page count («Nº páginas») for many more
recent books than the Spanish National Library holds.

CEGAL gave written permission on 2026-11-19, under conditions this module
enforces (the operator documentation records them in full):

* A book page is reached directly by its URL, built from the ISBN alone
  (:func:`book_page_url`). The site's own search is never used.
* Only «Idioma original» and the page count are read and kept, with the
  page URL as their source. No other page text is kept, logged or passed on.
* At most one request per second (:data:`MIN_REQUEST_INTERVAL_SECONDS` is
  wider than that).
* A few dozen lookups a week and one first load of a few hundred titles:
  the ledger (:class:`PortalLedger`) caps the weekly lookups at
  :data:`WEEKLY_CEILING` over the last :data:`WEEKLY_WINDOW_DAYS` days and the
  first load at :data:`FIRST_LOAD_CEILING` lookups in total.
* Each ISBN is asked once; its answer, "no page" included, is kept in the
  ledger and never asked again.
* The weekly-config switch :data:`SWITCH` turns the portal off.

The book page is ``/libros/<slug>_<ISBN-13>``. The site does not check the
slug, so any non-empty slug reaches the page (an empty one answers 404), and
a book the portal does not hold answers 404. The page's canonical link names
the book's ISBN, which is checked against the ISBN asked.

«Idioma original: Castellano» proves nothing: the portal labels some
translations that way. :meth:`PortalAnswer.original_language` never returns
Spanish, nor any language of the book's own lists.
"""

from __future__ import annotations

import html
import re
import time
import unicodedata
from collections.abc import Callable
from collections.abc import Collection
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from datetime import timedelta
from pathlib import Path
from typing import Any
from typing import Protocol

from autoscout.isbn_completion import isbn13
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.original_edition import OPEN_LIBRARY_LANGUAGE

PORTAL_KEY = "todostuslibros"
PORTAL_NAME = "Todos tus libros (CEGAL)"
PORTAL_HOST = "www.todostuslibros.com"
# Any non-empty slug reaches the page; ``libro`` is a fixed placeholder.
BOOK_PAGE_URL = "https://www.todostuslibros.com/libros/libro_{isbn}"
_CANONICAL_PREFIX = "https://www.todostuslibros.com/libros/"

# The weekly-config switch. Off unless the config sets it to ``true``.
SWITCH = "todostuslibros_lookups"

# The ledger, beside Candidate History in the state directory.
LEDGER_FILENAME = "todostuslibros-lookups.json"
LEDGER_VERSION = 1

MODE_WEEKLY = "weekly"
MODE_FIRST_LOAD = "first_load"
MODES = (MODE_WEEKLY, MODE_FIRST_LOAD)
# "A few dozen lookups a week", counted over the last seven days.
WEEKLY_CEILING = 50
WEEKLY_WINDOW_DAYS = 7
# "One first load of a few hundred titles", counted over all time.
FIRST_LOAD_CEILING = 500

# CEGAL allows one request per second; the spacing keeps a margin.
MIN_REQUEST_INTERVAL_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 2_000_000
# A portal that failed this many times in a row is not asked again in the pass.
FAILURE_LIMIT = 3

_LABEL_MAX_LENGTH = 40
_MAX_PAGE_COUNT = 20_000

# The portal's Spanish language names → ISO 639-1 codes Open Library knows.
_LANGUAGE_CODES: dict[str, str] = {
    "castellano": "es",
    "espanol": "es",
    "ingles": "en",
    "frances": "fr",
    "aleman": "de",
    "italiano": "it",
    "portugues": "pt",
    "catalan": "ca",
    "neerlandes": "nl",
    "holandes": "nl",
    "noruego": "no",
    "polaco": "pl",
    "checo": "cs",
    "sueco": "sv",
    "danes": "da",
    "fines": "fi",
    "finlandes": "fi",
    "ruso": "ru",
    "japones": "ja",
    "chino": "zh",
    "coreano": "ko",
    "hebreo": "he",
    "arabe": "ar",
    "hungaro": "hu",
}
# Spanish is never an original language the portal proves (issue #1140).
_PROVES_NOTHING = frozenset({"es"})

_CANONICAL = re.compile(r"""<link\s+rel=["']canonical["']\s+href=["']([^"']+)["']""", re.IGNORECASE)
_BUTTON_ISBN = re.compile(r"""<button\b[^>]*\bisbn=["']([0-9Xx-]{10,17})["']""", re.IGNORECASE)
_ORIGINAL_LANGUAGE = re.compile(
    r"<dt>\s*Idioma\s+original\s*:?\s*</dt>\s*<dd>\s*([^<]{1,80}?)\s*</dd>",
    re.IGNORECASE,
)
_PAGE_COUNT = re.compile(
    r"<dt>\s*N\s*(?:º|°|o\.?|&ordm;)\s*p(?:á|a|&aacute;)ginas\s*:?\s*</dt>\s*<dd>\s*(\d{1,6})\s*</dd>",
    re.IGNORECASE,
)
_LABEL = re.compile(r"^[^\W\d_]+(?:[ -][^\W\d_]+)*$")


class PortalLedgerError(ValueError):
    """The ledger cannot be read or written; no lookup is made without it."""


class PortalDeadline(Exception):
    """The pass deadline would pass while waiting for the next request."""


def enabled(config: Any) -> bool:
    """True only when the weekly config sets the switch to ``true``."""
    return isinstance(config, Mapping) and config.get(SWITCH) is True


def config_enabled(config_path: Path | None) -> bool:
    """The switch, read from the weekly config file; a missing or unreadable config is off."""
    if config_path is None:
        return False
    try:
        config = read_json(Path(config_path))
    except (OSError, ValueError):
        return False
    return enabled(config)


def book_page_url(isbn: str) -> str:
    """The book page of an ISBN, or empty when it is no valid ISBN."""
    normalized = isbn13(isbn)
    return BOOK_PAGE_URL.format(isbn=normalized) if normalized else ""


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold().strip()


def language_code(label: str) -> str:
    """The ISO 639-1 code of a portal language name, or empty for one Open Library does not know."""
    code = _LANGUAGE_CODES.get(_fold(label), "")
    return code if code in OPEN_LIBRARY_LANGUAGE else ""


# ── The page ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PortalPage:
    """The two facts a book page states, and the page's URL."""

    page_url: str
    original_language_label: str = ""
    page_count: int | None = None


def _isbn_digits(value: str) -> str:
    return re.sub(r"[^0-9Xx]", "", value)


def _clean_label(raw: str) -> str:
    label = " ".join(html.unescape(raw).split())
    if len(label) > _LABEL_MAX_LENGTH or not _LABEL.match(label):
        return ""
    return label


def parse_book_page(body: str, isbn: str) -> PortalPage | None:
    """The facts of the book page of ``isbn``, or ``None`` when ``body`` is not that book's page.

    Only the canonical link (or the page's own ISBN button), «Idioma
    original» and «Nº páginas» are read. A field the page does not have is
    left empty.
    """
    wanted = isbn13(isbn)
    if not wanted or not isinstance(body, str):
        return None
    page_url = ""
    canonical = _CANONICAL.search(body)
    if canonical is not None:
        url = html.unescape(canonical.group(1)).strip()
        if url.startswith(_CANONICAL_PREFIX) and "_" in url:
            if isbn13(_isbn_digits(url.rsplit("_", 1)[1])) != wanted:
                return None
            page_url = url
    if not page_url:
        button = _BUTTON_ISBN.search(body)
        if button is None or isbn13(_isbn_digits(button.group(1))) != wanted:
            return None
        page_url = BOOK_PAGE_URL.format(isbn=wanted)
    language = _ORIGINAL_LANGUAGE.search(body)
    pages = _PAGE_COUNT.search(body)
    page_count = int(pages.group(1)) if pages is not None else None
    if page_count is not None and not 0 < page_count <= _MAX_PAGE_COUNT:
        page_count = None
    return PortalPage(
        page_url=page_url,
        original_language_label=_clean_label(language.group(1)) if language is not None else "",
        page_count=page_count,
    )


# ── The answer kept per ISBN ─────────────────────────────────────────────


@dataclass(frozen=True)
class PortalAnswer:
    """The portal's answer for one ISBN; ``has_page`` false means the portal has no page."""

    isbn: str
    looked_up: str
    has_page: bool
    page_url: str = ""
    original_language_label: str = ""
    page_count: int | None = None

    def original_language(self, list_languages: Collection[str]) -> str:
        """The ISO 639-1 original language the page proves, or empty.

        Spanish («Castellano») proves nothing, and neither does a language of
        the book's own lists.
        """
        if not self.has_page:
            return ""
        code = language_code(self.original_language_label)
        if not code or code in _PROVES_NOTHING:
            return ""
        if code in {str(language).strip().casefold() for language in list_languages}:
            return ""
        return code

    def to_json(self) -> dict[str, Any]:
        return {
            "looked_up": self.looked_up,
            "has_page": self.has_page,
            "page_url": self.page_url,
            "original_language": self.original_language_label,
            "page_count": self.page_count,
        }


def _answer_from_json(isbn: str, value: Any) -> PortalAnswer:
    if not isinstance(value, Mapping) or set(value) != {
        "looked_up",
        "has_page",
        "page_url",
        "original_language",
        "page_count",
    }:
        raise PortalLedgerError("malformed answer")
    looked_up, has_page, page_url = value["looked_up"], value["has_page"], value["page_url"]
    label, page_count = value["original_language"], value["page_count"]
    if not isinstance(looked_up, str) or not isinstance(has_page, bool) or not isinstance(page_url, str):
        raise PortalLedgerError("malformed answer")
    if not isinstance(label, str) or (label and _clean_label(label) != label):
        raise PortalLedgerError("malformed answer")
    if page_count is not None and (
        not isinstance(page_count, int) or isinstance(page_count, bool) or not 0 < page_count <= _MAX_PAGE_COUNT
    ):
        raise PortalLedgerError("malformed answer")
    _iso_date(looked_up)
    if has_page and not page_url.startswith(_CANONICAL_PREFIX):
        raise PortalLedgerError("malformed answer")
    if not has_page and (page_url or label or page_count is not None):
        raise PortalLedgerError("malformed answer")
    return PortalAnswer(
        isbn=isbn,
        looked_up=looked_up,
        has_page=has_page,
        page_url=page_url,
        original_language_label=label,
        page_count=page_count,
    )


def _iso_date(value: Any) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise PortalLedgerError("malformed date") from None


# ── The ledger ───────────────────────────────────────────────────────────


@dataclass
class _LedgerState:
    answers: dict[str, PortalAnswer]
    requests: list[dict[str, str]]


class PortalLedger:
    """Every portal request and every answer, kept in the state directory so the ceilings hold across runs.

    Each request is reserved before it is sent: :meth:`reserve` rereads the
    file under a lock, refuses a request past its ceiling, and writes the
    request down first, so a crash, a failed request or a second process
    can never let the count slip. A missing file is an empty ledger; a
    malformed one raises :class:`PortalLedgerError`, and no lookup is made.
    Weekly requests older than the window are dropped when the ledger is
    written; first-load requests are kept, because their ceiling is a total.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @classmethod
    def in_state_dir(cls, state_dir: Path) -> PortalLedger:
        return cls(Path(state_dir) / LEDGER_FILENAME)

    def _read(self) -> _LedgerState:
        if not self.path.exists():
            return _LedgerState(answers={}, requests=[])
        try:
            raw = read_json(self.path)
        except (OSError, ValueError) as exc:
            raise PortalLedgerError("unreadable ledger") from exc
        if (
            not isinstance(raw, Mapping)
            or raw.get("version") != LEDGER_VERSION
            or not isinstance(raw.get("answers"), Mapping)
            or not isinstance(raw.get("requests"), list)
        ):
            raise PortalLedgerError("malformed ledger")
        answers: dict[str, PortalAnswer] = {}
        for isbn, value in raw["answers"].items():
            if not isinstance(isbn, str) or isbn13(isbn) != isbn:
                raise PortalLedgerError("malformed isbn")
            answers[isbn] = _answer_from_json(isbn, value)
        requests: list[dict[str, str]] = []
        for entry in raw["requests"]:
            if (
                not isinstance(entry, Mapping)
                or set(entry) != {"date", "mode", "isbn"}
                or entry["mode"] not in MODES
                or not isinstance(entry["isbn"], str)
            ):
                raise PortalLedgerError("malformed request")
            _iso_date(entry["date"])
            requests.append({"date": str(entry["date"]), "mode": str(entry["mode"]), "isbn": str(entry["isbn"])})
        return _LedgerState(answers=answers, requests=requests)

    def _write(self, state: _LedgerState, today: date) -> None:
        cutoff = today - timedelta(days=WEEKLY_WINDOW_DAYS)
        requests = [
            entry
            for entry in state.requests
            if entry["mode"] == MODE_FIRST_LOAD or date.fromisoformat(entry["date"]) > cutoff
        ]
        payload = {
            "version": LEDGER_VERSION,
            "answers": {isbn: state.answers[isbn].to_json() for isbn in sorted(state.answers)},
            "requests": requests,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_json(self.path, payload)
        except OSError as exc:
            raise PortalLedgerError("ledger not written") from exc

    @contextmanager
    def _locked(self) -> Iterator[None]:
        from autoscout.safe_artifacts import bundle_write_lock

        with bundle_write_lock(self.path):
            yield

    def answers(self) -> dict[str, PortalAnswer]:
        """Every kept answer by ISBN-13."""
        return dict(self._read().answers)

    def answer(self, isbn: str) -> PortalAnswer | None:
        return self._read().answers.get(isbn13(isbn))

    def used(self, mode: str, today: date) -> int:
        """Requests counted against the ceiling of ``mode`` on ``today``."""
        return _used(self._read().requests, mode, today)

    def remaining(self, mode: str, today: date) -> int:
        return max(0, ceiling(mode) - self.used(mode, today))

    def reserve(self, isbn: str, mode: str, today: date) -> bool:
        """Write down one request of ``mode`` for ``isbn``; False when its ceiling is reached."""
        if mode not in MODES:
            raise ValueError(f"unknown portal mode: {mode}")
        with self._locked():
            state = self._read()
            if _used(state.requests, mode, today) >= ceiling(mode):
                return False
            state.requests.append({"date": today.isoformat(), "mode": mode, "isbn": isbn13(isbn)})
            self._write(state, today)
            return True

    def record(self, answer: PortalAnswer, today: date) -> None:
        """Keep the answer for its ISBN."""
        with self._locked():
            state = self._read()
            state.answers[answer.isbn] = answer
            self._write(state, today)


def ceiling(mode: str) -> int:
    return FIRST_LOAD_CEILING if mode == MODE_FIRST_LOAD else WEEKLY_CEILING


def _used(requests: list[dict[str, str]], mode: str, today: date) -> int:
    if mode == MODE_FIRST_LOAD:
        return sum(1 for entry in requests if entry["mode"] == MODE_FIRST_LOAD)
    cutoff = today - timedelta(days=WEEKLY_WINDOW_DAYS)
    return sum(
        1 for entry in requests if entry["mode"] == MODE_WEEKLY and cutoff < date.fromisoformat(entry["date"]) <= today
    )


# ── Fetching ─────────────────────────────────────────────────────────────


class PortalFetch(Protocol):
    def __call__(self, url: str, *, timeout: float) -> tuple[int, str]:
        """GET ``url``: the HTTP status and the body (status 0 for no answer at all)."""
        ...


class LivePortalFetch:
    """GET a book page with the bounded standard-library transport, never following another host.

    The body is returned to the parser only; nothing here keeps or logs it.
    """

    def __init__(self, transport: Any = None) -> None:
        from autoscout.grounded_description import StandardLibraryHTTPTransport

        self._transport = transport or StandardLibraryHTTPTransport(max_redirects=1)

    def __call__(self, url: str, *, timeout: float) -> tuple[int, str]:
        from urllib.error import HTTPError

        try:
            response = self._transport.request(
                url,
                timeout_seconds=max(0.1, timeout),
                max_response_bytes=MAX_RESPONSE_BYTES,
                allowed_hosts={PORTAL_HOST},
            )
        except HTTPError as exc:
            status = int(exc.code or 0)
            exc.close()
            return status, ""
        except Exception:
            return 0, ""
        if getattr(response, "truncated", False):
            return 0, ""
        return int(getattr(response, "status", 0)), bytes(response.body).decode("utf-8", errors="replace")


@dataclass
class PortalStats:
    """What the portal did in one pass; counts only, never page text."""

    requests: int = 0
    pages: int = 0
    no_page: int = 0
    cached: int = 0
    failed: int = 0
    unavailable: int = 0
    ceiling_reached: int = 0
    not_asked: int = 0
    ledger_unavailable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "pages": self.pages,
            "no_page": self.no_page,
            "cached": self.cached,
            "failed": self.failed,
            "unavailable": self.unavailable,
            "ceiling_reached": self.ceiling_reached,
            "not_asked": self.not_asked,
            "ledger_unavailable": self.ledger_unavailable,
        }


class PortalLookup:
    """Answers per ISBN for one pass: the kept answer, else one paced, counted request.

    ``fetch=None`` makes a lookup that only reads kept answers (a preview).
    ``today`` dates the requests for the ceilings; it is the real date, not
    a run date, because the ceilings are about real requests.
    """

    def __init__(
        self,
        ledger: PortalLedger,
        fetch: PortalFetch | None,
        *,
        mode: str,
        today: date,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        interval_seconds: float = MIN_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown portal mode: {mode}")
        self.ledger = ledger
        self.mode = mode
        self.today = today
        self.stats = PortalStats()
        self._fetch = fetch
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._interval = max(1.0, float(interval_seconds))
        self._last_request: float | None = None
        self._failures = 0
        self._ceiling_reached = False
        self._answers: dict[str, PortalAnswer] | None = None

    def _kept(self) -> dict[str, PortalAnswer] | None:
        if self._answers is None and not self.stats.ledger_unavailable:
            try:
                self._answers = self.ledger.answers()
            except PortalLedgerError:
                self.stats.ledger_unavailable = True
        return self._answers

    def kept(self, isbn: str) -> PortalAnswer | None:
        """The kept answer for ``isbn``, without any request."""
        answers = self._kept()
        return answers.get(isbn13(isbn)) if answers is not None else None

    def answer(self, isbn: str, deadline: float = float("inf")) -> PortalAnswer | None:
        """The portal's answer for ``isbn``: kept, else asked once; ``None`` when there is none.

        Raises :class:`PortalDeadline` when the request spacing would run past
        ``deadline``.
        """
        normalized = isbn13(isbn)
        answers = self._kept()
        if not normalized or answers is None:
            self.stats.not_asked += 1
            return None
        if normalized in answers:
            self.stats.cached += 1
            return answers[normalized]
        if self._fetch is None:
            self.stats.not_asked += 1
            return None
        if self._failures >= FAILURE_LIMIT:
            self.stats.unavailable += 1
            return None
        if self._ceiling_reached:
            self.stats.ceiling_reached += 1
            return None
        self._pace(deadline)
        try:
            if not self.ledger.reserve(normalized, self.mode, self.today):
                self._ceiling_reached = True
                self.stats.ceiling_reached += 1
                return None
        except PortalLedgerError:
            self.stats.ledger_unavailable = True
            self._answers = None
            return None
        self.stats.requests += 1
        self._last_request = self._monotonic()
        timeout = min(REQUEST_TIMEOUT_SECONDS, max(0.1, deadline - self._monotonic()))
        status, body = self._fetch(book_page_url(normalized), timeout=timeout)
        answer: PortalAnswer | None = None
        if status == 404:
            answer = PortalAnswer(isbn=normalized, looked_up=self.today.isoformat(), has_page=False)
        elif status == 200:
            page = parse_book_page(body, normalized)
            if page is not None:
                answer = PortalAnswer(
                    isbn=normalized,
                    looked_up=self.today.isoformat(),
                    has_page=True,
                    page_url=page.page_url,
                    original_language_label=page.original_language_label,
                    page_count=page.page_count,
                )
        del body
        if answer is None:
            self._failures += 1
            self.stats.failed += 1
            return None
        self._failures = 0
        try:
            self.ledger.record(answer, self.today)
        except PortalLedgerError:
            self.stats.ledger_unavailable = True
            self._answers = None
            return None
        answers[normalized] = answer
        if answer.has_page:
            self.stats.pages += 1
        else:
            self.stats.no_page += 1
        return answer

    def _pace(self, deadline: float) -> None:
        if self._last_request is None:
            return
        now = self._monotonic()
        wait = self._last_request + self._interval - now
        if wait > 0:
            if now + wait >= deadline:
                raise PortalDeadline
            self._sleeper(wait)


__all__ = [
    "BOOK_PAGE_URL",
    "FIRST_LOAD_CEILING",
    "LEDGER_FILENAME",
    "MIN_REQUEST_INTERVAL_SECONDS",
    "MODE_FIRST_LOAD",
    "MODE_WEEKLY",
    "PORTAL_KEY",
    "SWITCH",
    "WEEKLY_CEILING",
    "WEEKLY_WINDOW_DAYS",
    "LivePortalFetch",
    "PortalAnswer",
    "PortalDeadline",
    "PortalLedger",
    "PortalLedgerError",
    "PortalLookup",
    "PortalPage",
    "PortalStats",
    "book_page_url",
    "config_enabled",
    "enabled",
    "language_code",
    "parse_book_page",
]
