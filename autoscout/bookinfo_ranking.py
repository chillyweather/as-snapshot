"""Explicit BookInfo JSON ranking evidence, shared by live capture and replay.

Dates are attested by supplier options plus the exact request, never by a
fabricated HTML heading. No catalogue search or metadata enrichment occurs.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from collections.abc import Mapping
from datetime import date
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any
from typing import NoReturn
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from autoscout.http_decoding import MAX_RESPONSE_BYTES

if TYPE_CHECKING:
    from autoscout.source_feasibility import FeasibilityVerdict

FAMILY = "bookinfo-weekly-json"
SOURCE_URL = "https://www.bookinfo.com.br/maisvendidos"
ORIGIN = "https://www.bookinfo.com.br"


class BookInfoError(ValueError):
    """A named refusal that callers can report without repairing evidence."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def _fail(code: str, message: str) -> NoReturn:
    raise BookInfoError(code, message)


def validate_declaration(source: Mapping[str, Any]) -> None:
    if source.get("url") != SOURCE_URL:
        _fail("bookinfo_declaration", "BookInfo requires its canonical publisher URL")
    if source.get("bookinfo_period_evidence") != "week-options-and-request":
        _fail("bookinfo_declaration", "BookInfo requires explicit week-options-and-request period policy")
    if type(source.get("bookinfo_max_age_days", 14)) is not int or source.get("bookinfo_max_age_days", 14) != 14:
        _fail("bookinfo_declaration", "BookInfo period-start maximum age must be 14 days")
    exclusions = source.get("adult_nonfiction_exclusions", [])
    if not isinstance(exclusions, list) or any(not isinstance(x, str) or not x.strip() for x in exclusions):
        _fail("bookinfo_declaration", "BookInfo exclusions must be a list of nonblank title terms")


def option_urls(reference: date) -> list[str]:
    months = {(reference.year, reference.month)}
    earlier = reference - timedelta(days=14)
    months.add((earlier.year, earlier.month))
    return [
        ORIGIN + "/api/bi/filtro/semana?" + urlencode({"mes": f"{m:02}/{y}"}) for y, m in sorted(months, reverse=True)
    ]


def ranking_url(start: date) -> str:
    return (
        ORIGIN
        + "/api/bi/sintetico-premium?"
        + urlencode(
            {
                "agrupamento": "livro",
                "ano": str(start.year),
                "mes": f"{start.month:02}/{start.year}",
                "data_inicio_semana": start.isoformat(),
                "id_categoria": "3",
            }
        )
    )


def _observation(url: str, response: Mapping[str, Any]) -> dict[str, Any]:
    body = response.get("body")
    if response.get("status") != 200:
        _fail("bookinfo_http", "BookInfo request did not return HTTP 200")
    if response.get("resolved_url") != url:
        _fail("bookinfo_wrong_target", "BookInfo request resolved to a different URL")
    if response.get("truncated") or not isinstance(body, str) or len(body.encode("utf-8")) > MAX_RESPONSE_BYTES:
        _fail("bookinfo_response_bounds", "BookInfo response is missing, truncated or oversized")
    return {
        "url": url,
        "resolved_url": url,
        "status": 200,
        "body": body,
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


def _read(observation: Mapping[str, Any], url: str) -> Any:
    if not isinstance(observation, Mapping):
        _fail("bookinfo_evidence_identity", "BookInfo observation must be an object")
    checked = _observation(url, observation)
    if observation.get("url") != url or observation.get("sha256") != checked["sha256"]:
        _fail("bookinfo_evidence_identity", "BookInfo request identity or response digest does not agree")
    try:
        return json.loads(checked["body"])
    except (ValueError, RecursionError):
        _fail("bookinfo_json", "BookInfo response is not valid JSON")


def _selected_week(options: list[Any], reference: date) -> date:
    starts: list[date] = []
    for month_rows in options:
        if not isinstance(month_rows, list) or len(month_rows) > 6:
            _fail("bookinfo_week_options", "BookInfo month options must be a bounded array")
        for row in month_rows:
            try:
                raw = row["data_inicio_semana"]
                timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                local = timestamp.astimezone(ZoneInfo("America/Sao_Paulo"))
                start = local.date()
                if timestamp.tzinfo is None or local.hour or local.minute or local.second or local.microsecond:
                    raise ValueError("week timestamp is not local midnight")
            except (TypeError, ValueError, KeyError, AttributeError):
                _fail("bookinfo_week_options", "BookInfo week timestamp is invalid")
            if start.weekday() != 0 or start > reference or start in starts:
                _fail("bookinfo_week_options", "BookInfo week is duplicate, future or not Monday")
            starts.append(start)
    eligible = [x for x in starts if (reference - x).days <= 14 and x + timedelta(days=6) < reference]
    if not eligible:
        _fail("bookinfo_stale", "BookInfo has no completed week within the 14-day start-age limit")
    return max(eligible)


def _validated_options(observations: Any, reference: date) -> list[Any]:
    urls = option_urls(reference)
    if not isinstance(observations, list) or len(observations) != len(urls):
        _fail("bookinfo_evidence_identity", "BookInfo requires exactly the declared month observations")
    options = [_read(x, u) for x, u in zip(observations, urls, strict=True)]
    for values, url in zip(options, urls, strict=True):
        if isinstance(values, list):
            # Each endpoint must attest its own month, including timezone conversion.
            from urllib.parse import parse_qs
            from urllib.parse import urlsplit

            expected = parse_qs(urlsplit(url).query)["mes"][0]
            for value in values:
                try:
                    ts = datetime.fromisoformat(value["data_inicio_semana"].replace("Z", "+00:00"))
                    month = ts.astimezone(ZoneInfo("America/Sao_Paulo")).strftime("%m/%Y")
                except (KeyError, TypeError, ValueError, AttributeError):
                    _fail("bookinfo_week_options", "BookInfo week timestamp is invalid")
                if month != expected:
                    _fail("bookinfo_week_options", "BookInfo options disagree with requested month")
    return options


def validate_bundle(bundle: Mapping[str, Any], source: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Revalidate raw observations; derived rows never substitute for evidence."""
    validate_declaration(source)
    if not isinstance(bundle, Mapping):
        _fail("bookinfo_evidence_identity", "BookInfo bundle must be an object")
    try:
        reference = date.fromisoformat(bundle["reference_date"])
        observations = bundle["options"]
        ranking = bundle["ranking"]
        if (
            type(bundle["version"]) is not int
            or bundle["version"] != 1
            or bundle["source_url"] != SOURCE_URL
            or reference.isoformat() != bundle["reference_date"]
        ):
            raise ValueError("wrong bundle identity")
    except (KeyError, TypeError, ValueError):
        _fail("bookinfo_evidence_identity", "BookInfo bundle identity or reference date is invalid")
    options = _validated_options(observations, reference)
    start = _selected_week(options, reference)
    if (
        bundle.get("period_start") != start.isoformat()
        or bundle.get("period_end") != (start + timedelta(days=6)).isoformat()
    ):
        _fail("bookinfo_evidence_identity", "BookInfo selected period does not agree with options")
    data = _read(ranking, ranking_url(start))
    if not isinstance(data, list) or len(data) != 20:
        _fail("bookinfo_row_count", "BookInfo must return exactly 20 raw rows")
    rows = []
    seen: set[str] = set()
    previous: int | None = None
    for rank, row in enumerate(data, 1):
        allowed = {"sku", "titulo", "autor", "editora", "volume", "categoria", "id_categoria"}
        if (
            not isinstance(row, dict)
            or set(row) - allowed
            or any(
                not isinstance(row.get(k), str) or not row[k].strip()
                for k in ("sku", "titulo", "autor", "editora", "volume")
            )
        ):
            _fail("bookinfo_row_shape", "BookInfo row requires title, author, publisher, ISBN and volume strings")
        isbn = row["sku"]
        if (
            not re.fullmatch(r"97[89][0-9]{10}", isbn)
            or sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(isbn)) % 10
        ):
            _fail("bookinfo_isbn", "BookInfo row has an invalid ISBN-13")
        if isbn in seen:
            _fail("bookinfo_isbn", "BookInfo ranking has duplicate ISBNs")
        seen.add(isbn)
        if row.get("id_categoria") != "3" or row.get("categoria") != "Não Ficção":
            _fail("bookinfo_category", "BookInfo row is not the declared nonfiction category")
        if not re.fullmatch(r"0|[1-9][0-9]{0,11}", row["volume"]):
            _fail("bookinfo_volume", "BookInfo volume must be a canonical bounded nonnegative integer")
        volume = int(row["volume"])
        if previous is not None and volume > previous:
            _fail("bookinfo_volume", "BookInfo rows are not in nonincreasing sales order")
        previous = volume
        rows.append(
            {
                "title": row["titulo"].strip(),
                "author": row["autor"].strip(),
                "publisher": row["editora"].strip(),
                "isbn": isbn,
                "current_rank": rank,
                "volume": volume,
                "source_week": start.isoformat(),
                "evidence_url": ranking_url(start),
            }
        )
    return rows


def qualifying_rows(rows: list[dict[str, Any]], source: Mapping[str, Any]) -> list[dict[str, Any]]:
    from autoscout.source_page_identity import normalize_visible_text

    terms = [normalize_visible_text(x) for x in source.get("adult_nonfiction_exclusions", [])]
    return [r for r in rows if not any(term in normalize_visible_text(r["title"]) for term in terms)]


def capture(source: Mapping[str, Any], reference: date, fetch: Callable[[str], Mapping[str, Any]]) -> dict[str, Any]:
    """At most two month GETs plus one exact ranking GET; never retry."""
    validate_declaration(source)
    observations = [_observation(url, fetch(url)) for url in option_urls(reference)]
    start = _selected_week(_validated_options(observations, reference), reference)
    url = ranking_url(start)
    bundle = {
        "version": 1,
        "source_url": SOURCE_URL,
        "reference_date": reference.isoformat(),
        "period_start": start.isoformat(),
        "period_end": (start + timedelta(days=6)).isoformat(),
        "options": observations,
        "ranking": _observation(url, fetch(url)),
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
        "bookinfo_evidence": bundle,
        "text": json.dumps(rows, ensure_ascii=False),
        "contract_family": FAMILY,
    }


def evidence_url_grounded(page: Mapping[str, Any], evidence_url: str) -> bool:
    """True when the URL is the ranking request a retained capture selected and observed."""
    bundle = page.get("bookinfo_evidence")
    if page.get("contract_family") != FAMILY or not isinstance(bundle, Mapping):
        return False
    ranking = bundle.get("ranking")
    try:
        start = date.fromisoformat(bundle["period_start"])
    except (KeyError, TypeError, ValueError):
        return False
    if not isinstance(ranking, Mapping):
        return False
    return evidence_url == ranking_url(start) == ranking.get("url")


def mention_violation(entry: Mapping[str, Any], page: Mapping[str, Any], source: Mapping[str, Any]) -> str:
    try:
        rows = qualifying_rows(validate_bundle(page.get("bookinfo_evidence", {}), source), source)
    except BookInfoError as exc:
        return str(exc)
    # The ranking carries no publication year, so a supplied one is not source evidence.
    if entry.get("publication_year") not in (None, ""):
        return "BookInfo mention carries a publication year the JSON does not state"
    for row in rows:
        if (
            entry.get("publisher") in (None, "", row["publisher"])
            and entry.get("title") == row["title"]
            and entry.get("primary_authors") == [row["author"]]
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
    return "BookInfo mention does not agree with a qualifying captured JSON row"


def feasibility(evidence: Mapping[str, Any], source: Mapping[str, Any]) -> FeasibilityVerdict:
    from autoscout.source_feasibility import EntrySample
    from autoscout.source_feasibility import FeasibilityVerdict

    try:
        bundle = evidence.get("bookinfo_evidence", {})
        rows = qualifying_rows(validate_bundle(bundle, source), source)
        if evidence.get("source_page") != SOURCE_URL or evidence.get("reference_date") != bundle["reference_date"]:
            _fail("bookinfo_evidence_identity", "BookInfo capture identity disagrees with its bundle")
        if not rows:
            _fail("bookinfo_no_qualifying_rows", "BookInfo has no qualifying rows after exclusions")
    except BookInfoError as exc:
        return FeasibilityVerdict(False, str(exc), reason_code=exc.reason_code, contract_family=FAMILY)
    return FeasibilityVerdict(
        True,
        "bounded supplier JSON ranking accepted",
        reason_code="ok",
        contract_family=FAMILY,
        detected_heading="BookInfo Não Ficção",
        bounded_entry_count=len(rows),
        raw_row_count=20,
        ranking_category="Não Ficção",
        reporting_period=bundle["period_start"],
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
