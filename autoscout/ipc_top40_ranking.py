"""Explicit Independent Press Top 40 CSV ranking evidence, shared by live capture and replay.

The publisher page names one current nonfiction file; the file's hosting URL
attests when it was published. The CSV states no sales week, so none is
fabricated. No catalogue search or metadata enrichment occurs.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable
from collections.abc import Mapping
from datetime import UTC
from datetime import date
from datetime import datetime
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from typing import Any
from typing import NoReturn

from autoscout.http_decoding import MAX_RESPONSE_BYTES

if TYPE_CHECKING:
    from autoscout.source_feasibility import FeasibilityVerdict

FAMILY = "ipc-top40-csv"
SOURCE_URL = "https://www.indiepubs.org/top40"
ORIGIN = "https://www.indiepubs.org"
EVIDENCE_FIELD = "ipc_top40_evidence"
LINK_LABEL = "NONFICTION EXCEL"
HEADER = ["Ranking", "Title", "Publisher", "ISBN", "Author"]
ROW_COUNT = 40
MAX_AGE_DAYS = 14
# The publisher page is read in slices and not kept, so its size costs no memory
# beyond its largest single token, and no source declares a bound for it.  This
# ceiling only stops a response that never ends; the page was 2.6 MB in November
# 2026, nearly all site-builder markup in many tags of a few kilobytes each.
PAGE_CEILING_BYTES = 50_000_000

_FILE_PATH_RE = re.compile(r"/s/independentpresstop40nonfiction-[a-z0-9]{1,16}\.csv")
# The publisher's own hosted-file path: site id, asset id, upload time in epoch milliseconds.
_HOSTED_FILE_RE = re.compile(
    r"https://static1\.squarespace\.com/static/61d85d41a24345648f560d14/t/[0-9a-f]{24}/"
    r"(?P<uploaded_ms>[0-9]{13})/independentpresstop40nonfiction\.csv"
)


# Contributor roles the file marks in parentheses; only an editor is a primary author.
_ROLE_RE = re.compile(r"(?P<name>.+?)\s*\((?P<role>[^()]+)\)")
_EDITOR_ROLES = frozenset({"ed.", "ed", "eds.", "eds"})
_CREDENTIAL_RE = re.compile(r"(?:M\.?D|Ph\.?D|R\.?N|Jr|Sr|II|III|IV)\.?", re.IGNORECASE)


def primary_authors(display: str) -> list[str]:
    """Split the file's comma-joined author display into individual primary authors.

    Translators, illustrators and other marked roles are dropped, as are bare
    credentials. A display that yields no name is kept whole rather than emptied.
    """
    authors: list[str] = []
    for part in (segment.strip() for segment in display.split(",")):
        if not part or _CREDENTIAL_RE.fullmatch(part):
            continue
        role = _ROLE_RE.fullmatch(part)
        if role is None:
            authors.append(part)
        elif role["role"].strip().casefold() in _EDITOR_ROLES:
            authors.append(role["name"].strip())
    return authors or [display.strip()]


class IpcTop40Error(ValueError):
    """A named refusal that callers can report without repairing evidence."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def _fail(code: str, message: str) -> NoReturn:
    raise IpcTop40Error(code, message)


def validate_declaration(source: Mapping[str, Any]) -> None:
    if source.get("url") != SOURCE_URL:
        _fail("ipc_top40_declaration", "Independent Press Top 40 requires its canonical publisher URL")
    if source.get("ipc_top40_period_evidence") != "hosted-file-upload-date":
        _fail("ipc_top40_declaration", "Independent Press Top 40 requires explicit hosted-file-upload-date policy")
    max_age = source.get("ipc_top40_max_age_days", MAX_AGE_DAYS)
    if type(max_age) is not int or max_age != MAX_AGE_DAYS:
        _fail("ipc_top40_declaration", "Independent Press Top 40 file maximum age must be 14 days")
    exclusions = source.get("adult_nonfiction_exclusions", [])
    if not isinstance(exclusions, list) or any(not isinstance(x, str) or not x.strip() for x in exclusions):
        _fail("ipc_top40_declaration", "Independent Press Top 40 exclusions must be a list of nonblank title terms")


class _LabelledLinks(HTMLParser):
    """Collect the href of every anchor whose visible text is the download label."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.href: str | None = None
        self.parts: list[str] = []
        self.matches: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._close_anchor()
            self.href = next((value or "" for name, value in attrs if name == "href"), "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._close_anchor()

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.parts.append(data)

    def _close_anchor(self) -> None:
        if self.href is not None and " ".join("".join(self.parts).split()).casefold() == LINK_LABEL.casefold():
            self.matches.append(self.href)
        self.href = None
        self.parts = []


def _one_file_link(matches: list[str]) -> str:
    if len(matches) != 1:
        _fail(
            "ipc_top40_file_link",
            f"Independent Press Top 40 page must name exactly one {LINK_LABEL} link (found {len(matches)})",
        )
    href = matches[0].strip()
    path = href[len(ORIGIN) :] if href.startswith(ORIGIN + "/") else href
    if _FILE_PATH_RE.fullmatch(path) is None:
        _fail("ipc_top40_file_link", f"Independent Press Top 40 {LINK_LABEL} link is not the nonfiction CSV file")
    return path


class PageScan:
    """The publisher page read slice by slice: its labelled links and its digest, and nothing else.

    The page is mostly the site builder's markup and grows with it, so it is
    not held whole: each slice goes through the link parser and the digest and
    is dropped.  The parser keeps only a tag, comment or script it has not seen
    the end of, so memory follows the largest single token, not the page; the
    page ceiling bounds even that.  The whole page is still read, so a second
    labelled link is seen.
    """

    def __init__(self) -> None:
        self._links = _LabelledLinks()
        self._digest = hashlib.sha256()

    def feed(self, text: str) -> None:
        self._links.feed(text)
        self._digest.update(text.encode("utf-8"))

    def sha256(self) -> str:
        return self._digest.hexdigest()

    def file_link(self) -> str:
        self._links.close()
        return _one_file_link(self._links.matches)


def file_link(page_html: str) -> str:
    """Return the one current nonfiction file path the publisher page names."""
    scan = PageScan()
    scan.feed(page_html)
    return scan.file_link()


def _body(response: Mapping[str, Any]) -> str:
    body = response.get("body")
    if response.get("status") != 200:
        _fail("ipc_top40_http", "Independent Press Top 40 request did not return HTTP 200")
    if response.get("truncated") or not isinstance(body, str) or len(body.encode("utf-8")) > MAX_RESPONSE_BYTES:
        _fail("ipc_top40_response_bounds", "Independent Press Top 40 response is missing, truncated or oversized")
    return body


def read_whole_page(
    fetch: Callable[[str], Mapping[str, Any]],
) -> Callable[[str, Callable[[str], None]], Mapping[str, Any]]:
    """Adapt a fetch that returns the whole body to the page scan; the page ceiling still applies."""

    def scan_page(url: str, consume: Callable[[str], None]) -> Mapping[str, Any]:
        response = fetch(url)
        if response.get("status") != 200:
            return response
        body = response.get("body")
        if response.get("truncated") or not isinstance(body, str) or len(body.encode("utf-8")) > PAGE_CEILING_BYTES:
            return {"status": 200, "resolved_url": response.get("resolved_url"), "truncated": True}
        consume(body)
        return {"status": 200, "resolved_url": response.get("resolved_url"), "truncated": False}

    return scan_page


def _page_observation(scan_page: Callable[[str, Callable[[str], None]], Mapping[str, Any]]) -> dict[str, Any]:
    """Retain the selected link and the page digest, not the publisher's page prose."""
    scan = PageScan()
    response = scan_page(SOURCE_URL, scan.feed)
    if response.get("status") != 200:
        _fail("ipc_top40_http", "Independent Press Top 40 request did not return HTTP 200")
    if response.get("truncated"):
        _fail("ipc_top40_response_bounds", "Independent Press Top 40 response is missing, truncated or oversized")
    if response.get("resolved_url") != SOURCE_URL:
        _fail("ipc_top40_wrong_target", "Independent Press Top 40 page resolved to a different URL")
    return {
        "url": SOURCE_URL,
        "resolved_url": SOURCE_URL,
        "status": 200,
        "sha256": scan.sha256(),
        "link_label": LINK_LABEL,
        "link_href": scan.file_link(),
    }


def _uploaded(resolved_url: Any) -> date:
    match = _HOSTED_FILE_RE.fullmatch(resolved_url) if isinstance(resolved_url, str) else None
    if match is None:
        _fail("ipc_top40_wrong_target", "Independent Press Top 40 file did not resolve to the publisher's hosted file")
    return datetime.fromtimestamp(int(match["uploaded_ms"]) / 1000, UTC).date()


def _file_observation(url: str, response: Mapping[str, Any]) -> dict[str, Any]:
    body = _body(response)
    _uploaded(response.get("resolved_url"))
    return {
        "url": url,
        "resolved_url": response["resolved_url"],
        "status": 200,
        "body": body,
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


def _published(file_observation: Mapping[str, Any], reference: date) -> date:
    published = _uploaded(file_observation.get("resolved_url"))
    age = (reference - published).days
    # The upload time is UTC and the run date is local, so one day ahead is not a future file.
    if age < -1:
        _fail("ipc_top40_stale", "Independent Press Top 40 file is dated after the reference date")
    if age > MAX_AGE_DAYS:
        _fail("ipc_top40_stale", "Independent Press Top 40 file is older than the 14-day age limit")
    return published


def _csv_rows(body: str) -> list[list[str]]:
    try:
        rows = list(csv.reader(io.StringIO(body.removeprefix("﻿"), newline=""), strict=True))
    except csv.Error:
        _fail("ipc_top40_csv", "Independent Press Top 40 file is not valid CSV")
    if not rows or rows[0] != HEADER:
        _fail("ipc_top40_csv", "Independent Press Top 40 file does not carry the declared header")
    return rows[1:]


def validate_bundle(bundle: Mapping[str, Any], source: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Revalidate the raw file observation; derived rows never substitute for evidence."""
    validate_declaration(source)
    if not isinstance(bundle, Mapping):
        _fail("ipc_top40_evidence_identity", "Independent Press Top 40 bundle must be an object")
    try:
        reference = date.fromisoformat(bundle["reference_date"])
        page = bundle["page"]
        observed = bundle["file"]
        if (
            type(bundle["version"]) is not int
            or bundle["version"] != 1
            or bundle["source_url"] != SOURCE_URL
            or reference.isoformat() != bundle["reference_date"]
            or not isinstance(page, Mapping)
            or not isinstance(observed, Mapping)
        ):
            raise ValueError("wrong bundle identity")
    except (KeyError, TypeError, ValueError):
        _fail("ipc_top40_evidence_identity", "Independent Press Top 40 bundle identity or reference date is invalid")
    href = page.get("link_href")
    if (
        page.get("url") != SOURCE_URL
        or page.get("resolved_url") != SOURCE_URL
        or page.get("status") != 200
        or page.get("link_label") != LINK_LABEL
        or not isinstance(href, str)
        or _FILE_PATH_RE.fullmatch(href) is None
        or not isinstance(page.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", page["sha256"]) is None
    ):
        _fail("ipc_top40_evidence_identity", "Independent Press Top 40 page observation is invalid")
    file_url = ORIGIN + href
    checked = _file_observation(file_url, observed)
    if observed.get("url") != file_url or observed.get("sha256") != checked["sha256"]:
        _fail("ipc_top40_evidence_identity", "Independent Press Top 40 file identity or response digest does not agree")
    published = _published(observed, reference)
    if bundle.get("published_date") != published.isoformat():
        _fail("ipc_top40_evidence_identity", "Independent Press Top 40 published date does not agree with the file")
    data = _csv_rows(checked["body"])
    if len(data) != ROW_COUNT:
        _fail("ipc_top40_row_count", "Independent Press Top 40 must return exactly 40 raw rows")
    rows = []
    seen: set[str] = set()
    for rank, row in enumerate(data, 1):
        if len(row) != len(HEADER) or any(not value.strip() for value in row):
            _fail(
                "ipc_top40_row_shape", "Independent Press Top 40 row requires rank, title, publisher, ISBN and author"
            )
        ranking, title, publisher, isbn, author = row
        if ranking != str(rank):
            _fail("ipc_top40_rank", "Independent Press Top 40 ranks must run 1 to 40 in file order")
        if (
            not re.fullmatch(r"97[89][0-9]{10}", isbn)
            or sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(isbn)) % 10
        ):
            _fail("ipc_top40_isbn", "Independent Press Top 40 row has an invalid ISBN-13")
        if isbn in seen:
            _fail("ipc_top40_isbn", "Independent Press Top 40 ranking has duplicate ISBNs")
        seen.add(isbn)
        rows.append(
            {
                "title": title.strip(),
                "author": author.strip(),
                "primary_authors": primary_authors(author),
                "publisher": publisher.strip(),
                "isbn": isbn,
                "current_rank": rank,
                "source_week": published.isoformat(),
                "evidence_url": file_url,
            }
        )
    return rows


def qualifying_rows(rows: list[dict[str, Any]], source: Mapping[str, Any]) -> list[dict[str, Any]]:
    from autoscout.source_page_identity import normalize_visible_text

    terms = [normalize_visible_text(x) for x in source.get("adult_nonfiction_exclusions", [])]
    return [r for r in rows if not any(term in normalize_visible_text(r["title"]) for term in terms)]


def capture(
    source: Mapping[str, Any],
    reference: date,
    fetch: Callable[[str], Mapping[str, Any]],
    scan_page: Callable[[str, Callable[[str], None]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """One publisher page GET plus one named file GET; never retry.

    *scan_page* reads the page in slices into the callback it is given and
    returns the response's status, resolved URL and whether it was cut short;
    without one, *fetch* reads the page whole.  The file always goes through
    *fetch* under the shared response bound.
    """
    validate_declaration(source)
    page = _page_observation(scan_page or read_whole_page(fetch))
    file_url = ORIGIN + page["link_href"]
    observed = _file_observation(file_url, fetch(file_url))
    bundle = {
        "version": 1,
        "source_url": SOURCE_URL,
        "reference_date": reference.isoformat(),
        "published_date": _published(observed, reference).isoformat(),
        "page": page,
        "file": observed,
    }
    validate_bundle(bundle, source)
    return bundle


def page_data(bundle: dict[str, Any], source: Mapping[str, Any]) -> dict[str, Any]:
    rows = validate_bundle(bundle, source)
    return {
        "source_page": SOURCE_URL,
        "source_url": SOURCE_URL,
        "html": "",
        "reference_date": bundle["reference_date"],
        EVIDENCE_FIELD: bundle,
        "text": json.dumps(rows, ensure_ascii=False),
        "contract_family": FAMILY,
    }


def evidence_url_grounded(page: Mapping[str, Any], evidence_url: str) -> bool:
    """True when the URL is the CSV file a retained capture names and observed."""
    bundle = page.get(EVIDENCE_FIELD)
    if page.get("contract_family") != FAMILY or not isinstance(bundle, Mapping):
        return False
    named = bundle.get("page")
    observed = bundle.get("file")
    if not isinstance(named, Mapping) or not isinstance(observed, Mapping):
        return False
    href = named.get("link_href")
    if not isinstance(href, str) or _FILE_PATH_RE.fullmatch(href) is None:
        return False
    return evidence_url == ORIGIN + href == observed.get("url")


def mention_violation(entry: Mapping[str, Any], page: Mapping[str, Any], source: Mapping[str, Any]) -> str:
    try:
        rows = qualifying_rows(validate_bundle(page.get(EVIDENCE_FIELD, {}), source), source)
    except IpcTop40Error as exc:
        return str(exc)
    # The file carries no publication year, so a supplied one is not source evidence.
    if entry.get("publication_year") not in (None, ""):
        return "Independent Press Top 40 mention carries a publication year the CSV does not state"
    for row in rows:
        if (
            entry.get("publisher") in (None, "", row["publisher"])
            and entry.get("title") == row["title"]
            and entry.get("primary_authors") == row["primary_authors"]
            and entry.get("isbn") == row["isbn"]
            and type(entry.get("current_rank")) is int
            and entry.get("current_rank") == row["current_rank"]
            and entry.get("source_week") == row["source_week"]
            and entry.get("source_url") == SOURCE_URL
            and entry.get("evidence_url") == row["evidence_url"]
            and entry.get("signal_type") == "bestseller/ranking"
            and entry.get("candidate_category") == source.get("eligible_candidate_category", "adult nonfiction")
        ):
            return ""
    return "Independent Press Top 40 mention does not agree with a qualifying captured CSV row"


def feasibility(evidence: Mapping[str, Any], source: Mapping[str, Any]) -> FeasibilityVerdict:
    from autoscout.source_feasibility import EntrySample
    from autoscout.source_feasibility import FeasibilityVerdict

    try:
        bundle = evidence.get(EVIDENCE_FIELD, {})
        rows = qualifying_rows(validate_bundle(bundle, source), source)
        if evidence.get("source_page") != SOURCE_URL or evidence.get("reference_date") != bundle["reference_date"]:
            _fail("ipc_top40_evidence_identity", "Independent Press Top 40 capture identity disagrees with its bundle")
        if not rows:
            _fail("ipc_top40_no_qualifying_rows", "Independent Press Top 40 has no qualifying rows after exclusions")
    except IpcTop40Error as exc:
        return FeasibilityVerdict(False, str(exc), reason_code=exc.reason_code, contract_family=FAMILY)
    return FeasibilityVerdict(
        True,
        "bounded publisher CSV ranking accepted",
        reason_code="ok",
        contract_family=FAMILY,
        detected_heading="Independent Press Top 40 Nonfiction",
        bounded_entry_count=len(rows),
        raw_row_count=ROW_COUNT,
        ranking_category="Nonfiction",
        reporting_period=bundle["published_date"],
        samples=tuple(
            EntrySample(
                r["title"],
                r["author"],
                r["evidence_url"],
                str(r["current_rank"]),
                r["isbn"],
                "adult nonfiction",
                r["source_week"],
            )
            for r in rows[:3]
        ),
    )
