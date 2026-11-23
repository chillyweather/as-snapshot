"""Local PW backfill uses invented page text, never licensed issue fixtures."""

import json
import zipfile
from pathlib import Path

import pytest

from autoscout.pw_local_backfill import _issue_date
from autoscout.pw_local_backfill import stage_archives

PAGE = """Nonfiction Reviews
General Nonfiction
An Invented Coast
Ada Example. Imaginary Press, $28 hardcover (240p)
ISBN 978-0-306-40615-7
Invented review prose that must not be retained.
(Nov.)"""


def test_issue_date_uses_indesign_metadata_and_cover_fallback():
    assert _issue_date("26_08_31_AD Cover.indd", ["SEPTEMBER 7, 2026"]).isoformat() == "2026-08-31"
    assert _issue_date("PW_WISup_02_09_26_Cover.indd", ["FEBRUARY 9, 2026"]).isoformat() == "2026-02-09"
    assert _issue_date("Untitled", ["ON SALE JANUARY 2, 2026 AUGUST 3, 2026"]).isoformat() == "2026-08-03"
    assert _issue_date("Untitled", ["No date"]) is None


def test_stage_isolated_rows_and_counts(tmp_path: Path):
    archive = tmp_path / "issues.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("issue-a.pdf", b"first")
        bundle.writestr("issue-b.pdf", b"second")
        bundle.writestr("spanish.pdf", b"third")
    outcomes = {
        b"first": ("26_11_02_AD Cover.indd", [PAGE]),
        b"second": ("Untitled", ["NOVEMBER 9, 2026\n" + PAGE]),
        b"third": ("Untitled", ["Sin fecha"]),
    }

    def read_pdf(path: Path):
        return outcomes[path.read_bytes()]

    output = tmp_path / "staged.json"
    summary = stage_archives([archive], 2026, output, read_pdf=read_pdf)
    stored = json.loads(output.read_text())
    assert summary["pdfs_inspected"] == 3
    assert summary["row_count"] == 1  # the newer review wins by ISBN
    assert [issue["issue_date"] for issue in stored["issues"]] == ["2026-11-02", "2026-11-09"]
    assert stored["rows"][0]["issue_date"] == "2026-11-09"
    assert stored["skipped"] == {"pw_issue_date_unknown": 1}
    assert "Invented review prose" not in output.read_text()
    with pytest.raises(ValueError, match="output_exists"):
        stage_archives([archive], 2026, output, read_pdf=read_pdf)
