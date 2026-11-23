"""Stage local PW PDFs as bibliographic rows without changing weekly state.

The downloaded PDFs are read one at a time and discarded. Neither page text
nor review prose is written to the result or printed.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import zipfile
from collections import Counter
from collections.abc import Callable
from datetime import date
from datetime import datetime
from pathlib import Path
from typing import Any

from autoscout.json_io import write_json
from autoscout.pw_issue_fetch import pdf_page_texts
from autoscout.pw_issue_parser import parse_pages

_TITLE_DATE = re.compile(r"(?<!\d)(?P<year>\d{2})_(?P<month>\d{2})_(?P<day>\d{2})(?!\d)")
_SUPPLEMENT_TITLE_DATE = re.compile(r"_(?P<month>\d{2})_(?P<day>\d{2})_(?P<year>2\d)(?!\d)")
_COVER_DATE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},?\s+20\d{2}\b",
    re.I,
)


def _read_pdf(path: Path) -> tuple[str, list[str]]:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ValueError("pw_pdf_reader_missing") from None
    logger = logging.getLogger("pypdf")
    handlers, propagate = logger.handlers[:], logger.propagate
    logger.handlers, logger.propagate = [logging.NullHandler()], False
    try:
        reader = PdfReader(path)
        title = str(reader.metadata.title or "") if reader.metadata else ""
        return title, pdf_page_texts(path)
    finally:
        logger.handlers, logger.propagate = handlers, propagate


def _issue_date(title: str, pages: list[str]) -> date | None:
    # Regular PW PDFs usually have an InDesign date in the document title.
    match = _TITLE_DATE.search(title)
    if match:
        try:
            if int(match["year"]) >= 20:
                return date(2000 + int(match["year"]), int(match["month"]), int(match["day"]))
        except ValueError:
            return None
    # The Winter Institute title is printed as month_day_year, even on the
    # full February 9 issue; do not mistake its leading month for a year.
    match = _SUPPLEMENT_TITLE_DATE.search(title)
    if match:
        try:
            return date(2000 + int(match["year"]), int(match["month"]), int(match["day"]))
        except ValueError:
            return None
    # A few covers have generic metadata. The last full date on their cover is
    # the issue date; earlier dates are often the publication dates of adverts.
    cover = " ".join((pages[0] if pages else "").split())
    matches = _COVER_DATE.findall(cover)
    if matches:
        try:
            return datetime.strptime(matches[-1].replace(",", ""), "%B %d %Y").date()
        except ValueError:
            return None
    return None


def stage_archives(
    archives: list[Path], year: int, output: Path, *, read_pdf: Callable[[Path], tuple[str, list[str]]] = _read_pdf
) -> dict[str, Any]:
    """Extract each issue into an isolated, Git-ignored result for later review.

    A PDF without a 2026 date or a Nonfiction Reviews section is excluded. An
    unreadable page inside that section rejects its whole issue.
    """
    if not archives or not 2000 <= year <= 2100 or output.exists():
        raise ValueError("pw_backfill_input_invalid_or_output_exists")
    rows: dict[str, dict[str, Any]] = {}
    issues: dict[str, dict[str, Any]] = {}
    skipped: Counter[str] = Counter()
    rejections: Counter[str] = Counter()
    pdfs = 0
    for archive in archives:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                if member.is_dir() or not member.filename.lower().endswith(".pdf"):
                    continue
                pdfs += 1
                if member.file_size > 500_000_000:
                    skipped["pw_pdf_too_large"] += 1
                    continue
                try:
                    with tempfile.TemporaryDirectory(prefix="autoscout-pw-local-") as folder:
                        local = Path(folder) / "issue.pdf"
                        with bundle.open(member) as source, local.open("wb") as target:
                            shutil.copyfileobj(source, target)
                        title, pages = read_pdf(local)
                    issue = _issue_date(title, pages)
                    if issue is None:
                        skipped["pw_issue_date_unknown"] += 1
                        continue
                    if issue.year != year:
                        skipped["pw_other_year"] += 1
                        continue
                    parsed = parse_pages(pages, issue.isoformat())
                    if parsed.unreadable_section_pages:
                        skipped["pw_page_unreadable"] += 1
                        continue
                    if not parsed.rows:
                        skipped["pw_zero_nonfiction_rows"] += 1
                        continue
                    if issue.isoformat() in issues:
                        skipped["pw_duplicate_issue_date"] += 1
                        continue
                    issues[issue.isoformat()] = {
                        "issue_date": issue.isoformat(),
                        "issue_id": "local-pdf",
                        "row_count": len(parsed.rows),
                        "rejections": parsed.rejections,
                    }
                    rejections.update(parsed.rejections)
                    for row in parsed.rows:
                        isbn = row["isbn13"]
                        if isbn not in rows or rows[isbn]["issue_date"] <= row["issue_date"]:
                            rows[isbn] = row
                except (OSError, ValueError, zipfile.BadZipFile):
                    skipped["pw_pdf_parse_failed"] += 1
    result = {
        "schema_version": 1,
        "year": year,
        "pdfs_inspected": pdfs,
        "issues": [issues[key] for key in sorted(issues)],
        "rows": sorted(rows.values(), key=lambda row: row["isbn13"]),
        "skipped": dict(sorted(skipped.items())),
        "rejections": dict(sorted(rejections.items())),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, result)
    return {key: value for key, value in result.items() if key != "rows"} | {"row_count": len(rows)}


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Stage local PW PDFs for a later weekly run")
    parser.add_argument("--archive", type=Path, action="append", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        summary = stage_archives(args.archive, args.year, args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(json.dumps({"status": "failed", "reason_code": str(exc)}))
        return 2
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
