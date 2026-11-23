"""Deterministic collector for the Publishers Weekly issue reviews source (Issue #955).

The collector reads the result of the PW issue pass and requests nothing: it
opens no mailbox and never visits PW's website. The live variant binds the
result to the run date; the replay variant binds a recorded result to its
newest issue date. Neither writes anything: the seen-review index moves only
when the weekly pipeline consumes the recorded mentions (`record_reported`),
so a source health check never marks a review as reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from autoscout import pw_issue_capture
from autoscout.source_collectors.base import CollectorResult
from autoscout.source_collectors.base import _collector_error


@dataclass(frozen=True)
class PwIssueCollector:
    variant: str

    def collect(
        self,
        config_path: Path,
        output_dir: Path,
        source: dict[str, Any],
        run_date: str,
    ) -> CollectorResult:
        live = self.variant == "live"
        try:
            pw_issue_capture.validate_declaration(source)
            collection = pw_issue_capture.read_collection(pw_issue_capture.collection_path(config_path, source))
            descriptions = pw_issue_capture.read_descriptions(
                pw_issue_capture.descriptions_path(config_path, source), collection
            )
            seen = pw_issue_capture.read_seen_items(pw_issue_capture.seen_items_path(config_path, source))
            if live or collection is None:
                reference = date.fromisoformat(run_date)
            else:
                # A recorded result is a pinned replay: it is judged by its own date, never the clock.
                reference = pw_issue_capture.newest_issue_date(collection)
            bundle = pw_issue_capture.capture(source, reference, collection, seen)
            rows = pw_issue_capture.qualifying_rows(bundle, source)
            mentions = [
                pw_issue_capture.mention(row, source, run_date, descriptions.get(row["isbn13"])) for row in rows
            ]
            for entry in mentions:
                violation = pw_issue_capture.mention_violation(entry, bundle, source, descriptions)
                if violation:
                    raise pw_issue_capture.PwIssueCaptureError("pw_mention_violation", violation)
        except pw_issue_capture.PwIssueCaptureError as exc:
            raise _collector_error(
                f"PW issue capture refused: {exc}",
                debug_metadata={"collector_kind": pw_issue_capture.FAMILY, "safe_failure_status": exc.reason_code},
            ) from exc
        missing = bundle["missing_issues"]
        collected = len(bundle["window_issues"])
        return CollectorResult(
            mentions=mentions,
            raw_mention_count=len(mentions),
            valid_mention_count=len(mentions),
            skipped_item_count=0,
            debug_metadata={
                "collector_kind": pw_issue_capture.FAMILY,
                "pw_newest_issue_date": bundle["newest_issue_date"],
                "pw_rows_in_result": len(bundle["rows"]),
                "pw_qualifying_rows": len(rows),
                "pw_prepared_descriptions": sum(1 for row in rows if row["isbn13"] in descriptions),
                "pw_dropped_rows": pw_issue_capture.dropped_row_counts(bundle, source),
                "pw_missing_issues": missing,
                # An issue of the age window the pass could not read is named in the weekly summary,
                # counted in issues: its reviews are not in this run.
                **(
                    {
                        "incomplete_ranking": True,
                        "incomplete_ranking_warning": (
                            f"incomplete collection: PW issue{'s' if len(missing) != 1 else ''} "
                            f"{', '.join(missing)} could not be read"
                        ),
                        "raw_row_count": collected,
                        "expected_raw_row_count": collected + len(missing),
                    }
                    if missing
                    else {}
                ),
            },
        )
