"""Bounded DNB ISBN author checks, with replayable bibliographic evidence.

This is a separate network operation. Ranking probes consume its retained
evidence offline and keep their existing HEAD/GET request budget.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler
from urllib.request import Request
from urllib.request import build_opener

MAX_BYTES = 100_000
MAX_RECORDS = 5
MARC = "{http://www.loc.gov/MARC21/slim}"
SRU = "{http://www.loc.gov/zing/srw/}"


def _text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def valid_isbn(value: str) -> str:
    compact = re.sub(r"[-\s]", "", value)
    if not re.fullmatch(r"97[89][0-9]{10}", compact):
        raise ValueError("ISBN must be an ISBN-13")
    total = sum(int(digit) * (1 if index % 2 == 0 else 3) for index, digit in enumerate(compact))
    if total % 10:
        raise ValueError("ISBN checksum is invalid")
    return compact


def query_url(isbn: str) -> str:
    return "https://services.dnb.de/sru/dnb?" + urlencode(
        {
            "version": "1.1",
            "operation": "searchRetrieve",
            "query": f"isbn={valid_isbn(isbn)}",
            "recordSchema": "MARC21-xml",
            "maximumRecords": "2",
        }
    )


def _xml(value: str) -> ET.Element:
    if len(value.encode("utf-8")) > MAX_BYTES or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", value, re.IGNORECASE):
        raise ValueError("catalog XML exceeds its bound or contains a document/entity declaration")
    try:
        return ET.fromstring(value)
    except ET.ParseError as exc:
        raise ValueError("catalog XML is malformed") from exc


def _fields(record: ET.Element, tag: str, code: str) -> list[str]:
    return [
        " ".join((element.text or "").split())
        for element in record.findall(f"{MARC}datafield[@tag='{tag}']/{MARC}subfield[@code='{code}']")
    ]


def _author(record: ET.Element, isbn: str, title: str) -> tuple[str, str, str]:
    if record.tag != f"{MARC}record":
        raise ValueError("catalog evidence is not a MARC bibliographic record")
    identifiers = {re.sub(r"[-\s]", "", value) for value in _fields(record, "020", "a")}
    if isbn not in identifiers:
        raise ValueError("catalog record ISBN does not match the ranking ISBN")
    titles = _fields(record, "245", "a")
    if len(titles) != 1 or _text(titles[0]) != _text(title):
        raise ValueError("catalog record title does not match the ranking title")
    people = record.findall(f"{MARC}datafield[@tag='100']") + record.findall(f"{MARC}datafield[@tag='700']")
    authors = [
        person for person in people if any(e.text == "aut" for e in person.findall(f"{MARC}subfield[@code='4']"))
    ]
    # A responsibility statement may also contain editors or translators.
    # This initial route accepts only one explicit author and a simple name.
    statements = _fields(record, "245", "c")
    if len(authors) != 1 or len(statements) != 1:
        raise ValueError("catalog record does not establish one unambiguous author")
    author = statements[0]
    words = author.split()
    if not 1 <= len(words) <= 5 or any(
        not re.fullmatch(r"[^\W\d_](?:[^\W\d_]|['.’-])*", word, re.UNICODE) for word in words
    ):
        raise ValueError("catalog author statement is not a simple author name")
    if any(not (word[0].isupper() or word in {"de", "di", "van", "von", "der", "zu"}) for word in words):
        raise ValueError("catalog author statement is ambiguous")
    catalog_names = [e.text or "" for e in authors[0].findall(f"{MARC}subfield[@code='a']")]
    record_id = record.findtext(f"{MARC}controlfield[@tag='001']", "")
    if len(catalog_names) != 1 or not re.fullmatch(r"[0-9]{8,12}[0-9X]?", record_id):
        raise ValueError("catalog author authority or record identity is missing")
    return author, catalog_names[0], record_id


def evidence_from_response(body: str, isbn: str, title: str) -> dict[str, str]:
    isbn = valid_isbn(isbn)
    root = _xml(body)
    records = root.findall(f"{SRU}records/{SRU}record/{SRU}recordData/{MARC}record")
    if root.findtext(f"{SRU}numberOfRecords") != "1" or len(records) != 1:
        raise ValueError("ISBN lookup must return exactly one catalog record")
    author, catalog_author, record_id = _author(records[0], isbn, title)
    # Retain only metadata needed to reproduce the match, never blurbs,
    # subjects, descriptions, unrelated editions, or content links.
    minimal = ET.Element(f"{MARC}record")
    for element in records[0]:
        if element.tag == f"{MARC}controlfield" and element.get("tag") == "001":
            minimal.append(element)
        elif element.tag == f"{MARC}datafield" and element.get("tag") in {"020", "100", "700", "245"}:
            projected = ET.SubElement(minimal, element.tag, element.attrib)
            for subfield in element:
                allowed = {"245": {"a", "c"}, "020": {"a"}, "100": {"a", "4"}, "700": {"a", "4"}}
                if subfield.get("code") in allowed[element.get("tag", "")]:
                    projected.append(subfield)
    captured = ET.tostring(minimal, encoding="unicode")
    return {
        "provider": "dnb",
        "isbn": isbn,
        "title": title,
        "author": author,
        "catalog_author": catalog_author,
        "evidence_url": f"https://d-nb.info/{record_id}",
        "request_url": query_url(isbn),
        "record_xml": captured,
        "record_sha256": hashlib.sha256(captured.encode()).hexdigest(),
    }


def verified_author(source: Mapping[str, Any], isbn: str, title: str) -> str:
    """Read optional retained checks without network or filesystem access."""
    raw = source.get("ranking_author_evidence_json", "")
    if "ranking_author_evidence_json" not in source:
        return ""
    if not isinstance(raw, str) or not raw.strip() or len(raw.encode()) > MAX_BYTES:
        raise ValueError("ISBN author evidence must be bounded JSON text")
    try:
        records = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("ISBN author evidence JSON is malformed") from exc
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_RECORDS:
        raise ValueError("ISBN author evidence must contain one to five records")
    seen: set[str] = set()
    matched = ""
    for record in records:
        if not isinstance(record, dict) or any(not isinstance(value, str) for value in record.values()):
            raise ValueError("ISBN author evidence record must contain text fields")
        expected_fields = {
            "provider",
            "isbn",
            "title",
            "author",
            "catalog_author",
            "evidence_url",
            "request_url",
            "record_xml",
            "record_sha256",
        }
        if set(record) != expected_fields or record.get("provider") != "dnb":
            raise ValueError("ISBN author evidence has an unsupported record shape")
        record_isbn = valid_isbn(record["isbn"])
        if record_isbn in seen:
            raise ValueError("ISBN author evidence contains duplicate ISBNs")
        seen.add(record_isbn)
        xml = record["record_xml"]
        if hashlib.sha256(xml.encode()).hexdigest() != record["record_sha256"]:
            raise ValueError("ISBN author evidence digest does not match")
        author, catalog_author, record_id = _author(_xml(xml), record_isbn, record["title"])
        if (
            record["request_url"] != query_url(record_isbn)
            or record["evidence_url"] != f"https://d-nb.info/{record_id}"
            or record["author"] != author
            or record["catalog_author"] != catalog_author
        ):
            raise ValueError("ISBN author evidence metadata does not match its retained record")
        if record_isbn == isbn:
            if _text(record["title"]) != _text(title):
                raise ValueError("ISBN author evidence title does not match the ranking row")
            matched = author
    return matched


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def lookup_author(isbn: str, title: str, *, opener: Any = None) -> dict[str, str]:
    """One bounded request, no retries or redirects, no secondary requests."""
    url = query_url(isbn)
    open_request = opener or build_opener(_NoRedirect()).open
    request = Request(
        url, headers={"User-Agent": "AutoScout/0.1 ISBN author verification", "Accept": "application/xml"}
    )
    with open_request(request, timeout=15) as response:
        if response.status != 200 or response.geturl() != url:
            raise ValueError("ISBN catalog request failed or redirected")
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("ISBN catalog response exceeds the capture limit")
    return evidence_from_response(body.decode("utf-8"), isbn, title)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify one missing author by ISBN in DNB; print replayable metadata evidence."
    )
    parser.add_argument("--isbn", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        evidence = lookup_author(args.isbn, args.title)
        payload = json.dumps([evidence], ensure_ascii=False, indent=2) + "\n"
        if args.output:
            # New evidence is never written into an existing artifact.
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as output:
                output.write(payload)
        print(payload, end="")
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "unverified", "reason": str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
