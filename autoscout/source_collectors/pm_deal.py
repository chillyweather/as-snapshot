"""Deterministic collector for the Publishers Marketplace deals source (Issue #945).

The collector reads the result of the PM deals pass and requests nothing: it
opens no mailbox and never visits PM's website. The live variant binds the
result to the run date; the replay variant binds a recorded result to its own
collection date. Neither writes anything: the seen-deal index moves only when
the weekly pipeline consumes the recorded mentions (`record_reported_deals`),
so a source health check never marks a deal as reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from autoscout import pm_deal_capture
from autoscout.source_collectors.base import CollectorResult
from autoscout.source_collectors.base import _collector_error


@dataclass(frozen=True)
class PmDealCollector:
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
            pm_deal_capture.validate_declaration(source)
            collection = pm_deal_capture.read_collection(pm_deal_capture.collection_path(config_path, source))
            seen_path = pm_deal_capture.seen_items_path(config_path, source)
            seen = pm_deal_capture.read_seen_items(seen_path)
            if live or collection is None:
                reference = date.fromisoformat(run_date)
            else:
                # A recorded result is a pinned replay: it is judged by its own date, never the clock.
                reference = pm_deal_capture.collected_date(collection)
            bundle = pm_deal_capture.capture(source, reference, collection, seen)
            rows = pm_deal_capture.qualifying_rows(bundle, source)
            mentions = [pm_deal_capture.mention(row, source, run_date) for row in rows]
            for entry in mentions:
                violation = pm_deal_capture.mention_violation(entry, bundle, source)
                if violation:
                    raise pm_deal_capture.PmDealCaptureError("pm_mention_violation", violation)
        except pm_deal_capture.PmDealCaptureError as exc:
            raise _collector_error(
                f"PM deals capture refused: {exc}",
                debug_metadata={"collector_kind": "pm-deal-email", "safe_failure_status": exc.reason_code},
            ) from exc
        dropped = pm_deal_capture.dropped_row_counts(bundle, source)
        # A deal outside the declared categories, or one an earlier run reported, is not a found row:
        # only a declared deal the run cannot report counts against the source.
        lost = dropped.get(pm_deal_capture.DROP_NO_EVIDENCE_URL, 0)
        return CollectorResult(
            mentions=mentions,
            raw_mention_count=len(mentions) + lost,
            valid_mention_count=len(mentions),
            skipped_item_count=lost,
            debug_metadata={
                "collector_kind": "pm-deal-email",
                "pm_collected_date": bundle["collected_date"],
                # A pass stopped at its declared message cap is not a shortfall: the next pass
                # continues from its watermark, as a Kirkus pass at its cap does (#816).
                "pm_result_status": bundle["status"],
                "pm_stop_reason": bundle["stop_reason"],
                "pm_rows_in_result": len(bundle["rows"]),
                "pm_qualifying_rows": len(rows),
                "pm_dropped_rows": dropped,
                # A declared deal the run cannot report is a shortfall the weekly summary names.
                **(
                    {
                        "incomplete_ranking": True,
                        "incomplete_ranking_warning": (
                            f"incomplete collection: {lost} PM deal{'s' if lost != 1 else ''} "
                            "in a declared category carried no dealmaker profile link"
                        ),
                        "raw_row_count": len(mentions),
                        "expected_raw_row_count": len(mentions) + lost,
                    }
                    if lost
                    else {}
                ),
            },
        )
