"""Tests for issue #379: the editor row cap must not drop rows silently.

Acceptance tests at the public weekly-workflow seam.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autoscout.weekly_workflow import deterministic_adapters
from autoscout.weekly_workflow import run_weekly_workflow

_ENTRY_TITLES = ["Alpha Book", "Beta Book", "Gamma Book", "Delta Book", "Epsilon Book"]
# The built-in seed-calibration band caps at 12 when a config sets no band.
_SEED_BAND_MAX = 12


def _titles(count: int) -> list[str]:
    if count <= len(_ENTRY_TITLES):
        return _ENTRY_TITLES[:count]
    return _ENTRY_TITLES + [f"Extra Book {index}" for index in range(count - len(_ENTRY_TITLES))]


def _entries(titles: list[str]) -> list[dict[str, object]]:
    return [
        {
            "title": title,
            "primary_authors": [f"Author {index}"],
            "source_url": f"https://example.test/book-{index}",
            "evidence_url": f"https://example.test/book-{index}",
            "evidence_note": "Listed book.",
            # A description lets the Visible Risk screen conclude, so a clean
            # run stays free of the inert-screen warning (issue #714).
            "short_description": "A popular science book about everyday physics.",
            "candidate_category": "popular science",
            "signal_type": "award shortlist",
            "signal_strength": "shortlist",
        }
        for index, title in enumerate(titles)
    ]


def _write_config(
    tmp_path: Path,
    *,
    pilot_row_max: int | None,
    pilot_mode: bool = True,
    readiness_purpose: str = "recurring-weekly",
) -> Path:
    """Write a run config. A pilot_row_max of None omits the band entirely, which
    is how configs/recorded-media-trial.json inherits the built-in seed band."""
    config_path = tmp_path / "config.json"
    band = {"pilot_row_min": 1, "pilot_row_max": pilot_row_max} if pilot_row_max is not None else {}
    config_path.write_text(
        json.dumps(
            {
                "run_date": "2026-07-13",
                "readiness_purpose": readiness_purpose,
                "editor_row_target": 1,
                **band,
                "pilot_mode": pilot_mode,
                "sources": [
                    {
                        "name": "Test Source",
                        "family": "award",
                        "url": "https://example.test/source",
                        "access_status": "open",
                        "extraction_method": "codex-extraction-replay",
                        "check_frequency": "weekly",
                        "language": "en",
                        "source_page_fixture_file": "test-capture.json",
                        "extraction_response_file": "test-source-codex-extraction.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return config_path


def _write_capture(tmp_path: Path, titles: list[str]) -> None:
    links = " ".join(
        f'<a href="https://example.test/book-{index}">Link</a> by Author {index}' for index in range(len(titles))
    )
    (tmp_path / "test-capture.json").write_text(
        json.dumps({"source_url": "https://example.test/source", "html": f"<html><body>{links}</body></html>"}),
        encoding="utf-8",
    )


def _write_extraction(output_dir: Path, titles: list[str]) -> None:
    worklist = json.loads((output_dir / "codex-extraction-worklist.json").read_text(encoding="utf-8"))
    item = worklist["work_items"][0]
    expected_file = str(item.get("expected_extraction_file", "")) or "test-source-codex-extraction.json"
    (output_dir / expected_file).write_text(
        json.dumps(
            {
                "source_name": "Test Source",
                "source_url": "https://example.test/source",
                "operation_id": item["operation_id"],
                "capture_sha256": item["capture_sha256"],
                "entries": _entries(titles),
            }
        ),
        encoding="utf-8",
    )


def _run(
    tmp_path: Path,
    *,
    pilot_row_max: int | None,
    pilot_mode: bool = True,
    readiness_purpose: str = "recurring-weekly",
    entry_count: int = len(_ENTRY_TITLES),
) -> tuple[dict, dict]:
    """Run the workflow and return the run summary and the pilot status artifact."""
    titles = _titles(entry_count)
    config_path = _write_config(
        tmp_path,
        pilot_row_max=pilot_row_max,
        pilot_mode=pilot_mode,
        readiness_purpose=readiness_purpose,
    )
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture(tmp_path, titles)
    adapters = deterministic_adapters()
    adapters.capture.prepare(config_path, output_dir, live=False)
    _write_extraction(output_dir, titles)
    run_weekly_workflow(
        config_path=config_path,
        output_dir=output_dir,
        state_dir=tmp_path / "state",
        adapters=adapters,
    )
    status_path = output_dir / "pilot-status.json"
    return (
        json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8")),
        json.loads(status_path.read_text(encoding="utf-8")) if status_path.is_file() else {},
    )


class EditorRowCapReportingTests(unittest.TestCase):
    def test_truncated_rows_are_counted_in_the_run_summary(self):
        """A cap that discards ranked candidates must say how many it discarded."""
        with tempfile.TemporaryDirectory() as tmp:
            summary, _status = _run(Path(tmp), pilot_row_max=2)
            self.assertEqual(summary["editor_facing_count"], 2)
            self.assertEqual(summary["row_cap_dropped_count"], len(_ENTRY_TITLES) - 2)
            # The two counts share one unit: together they account for the whole pool.
            self.assertEqual(
                summary["editor_facing_count"] + summary["row_cap_dropped_count"],
                len(_ENTRY_TITLES),
            )

    def test_a_real_drop_reaches_the_operator_facing_readiness_artifact(self):
        """The drop must not be visible only to whoever opens run-summary.json."""
        with tempfile.TemporaryDirectory() as tmp:
            summary, status = _run(Path(tmp), pilot_row_max=2)
            expected = f"{len(_ENTRY_TITLES) - 2} eligible editor-facing rows discarded by the pilot_row_max cap"
            self.assertTrue(
                any(expected in warning for warning in summary["major_warnings"]),
                summary["major_warnings"],
            )
            self.assertTrue(
                any(expected in caveat for caveat in status["caveats"]),
                status["caveats"],
            )

    def test_no_drop_is_reported_as_zero_when_the_cap_is_not_reached(self):
        """A cap above the pool discards nothing, so the count is an explicit zero."""
        with tempfile.TemporaryDirectory() as tmp:
            summary, status = _run(Path(tmp), pilot_row_max=50)
            self.assertEqual(summary["editor_facing_count"], len(_ENTRY_TITLES))
            self.assertEqual(summary["row_cap_dropped_count"], 0)
            self.assertFalse([w for w in summary["major_warnings"] if "pilot_row_max cap" in w])
            self.assertFalse([c for c in status.get("caveats", []) if "pilot_row_max cap" in c])

    def test_non_pilot_runs_never_truncate(self):
        """Outside pilot mode there is no cap, so nothing is dropped."""
        with tempfile.TemporaryDirectory() as tmp:
            summary, _status = _run(Path(tmp), pilot_row_max=2, pilot_mode=False)
            self.assertEqual(summary["editor_facing_count"], len(_ENTRY_TITLES))
            self.assertEqual(summary["row_cap_dropped_count"], 0)

    def test_the_built_in_seed_band_truncates_without_warning(self):
        """A config that omits the band inherits the 8-12 seed-calibration sampling.

        Truncation there is intended, as in configs/recorded-media-trial.json, so it
        must not raise a warning, must not add a caveat, and must not block
        readiness. Only a configured ceiling is a finding.
        """
        overflowing_pool = _SEED_BAND_MAX + 3
        with tempfile.TemporaryDirectory() as tmp:
            summary, status = _run(
                Path(tmp),
                pilot_row_max=None,
                readiness_purpose="seed-calibration",
                entry_count=overflowing_pool,
            )
            # The band really binds: the run truncates to the built-in maximum.
            self.assertEqual(summary["editor_facing_count"], _SEED_BAND_MAX)
            self.assertEqual(summary["row_cap_dropped_count"], overflowing_pool - _SEED_BAND_MAX)
            # An intended sampling band is not a finding, so no prose and no block.
            self.assertFalse([w for w in summary["major_warnings"] if "pilot_row_max cap" in w])
            self.assertFalse([c for c in status.get("caveats", []) if "pilot_row_max cap" in c])
            self.assertTrue(status["ready_for_editor"], status.get("caveats"))

    def test_a_configured_ceiling_that_binds_blocks_seed_calibration_readiness(self):
        """Seed-calibration readiness fails on any caveat, including this one.

        The two weekly configs default to seed-calibration, so this path decides
        whether a truncating run still reports itself ready to share.
        """
        with tempfile.TemporaryDirectory() as tmp:
            summary, status = _run(Path(tmp), pilot_row_max=2, readiness_purpose="seed-calibration")
            self.assertEqual(summary["row_cap_dropped_count"], len(_ENTRY_TITLES) - 2)
            self.assertTrue([c for c in status["caveats"] if "pilot_row_max cap" in c])
            self.assertFalse(status["ready_for_editor"])
            # The number is reported, not only the prose.
            self.assertEqual(
                status["readiness_basis"]["row_cap_dropped_count"],
                len(_ENTRY_TITLES) - 2,
            )


if __name__ == "__main__":
    unittest.main()
