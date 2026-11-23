"""The ``source-proposal reconsider`` command (Issue #578).

The proposal and the ledger are synthetic.  No test here makes a network
request.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.__main__ import main

URL = "https://example.test/2026-book-awards-shortlist/"
PROPOSAL_ID = "source-example-test-2026-book-awards-shortlist"
REJECTION = "no book-level title links"


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _case(tmp_path: Path, *, outcome: str = "accept", probe_date: str = "2026-10-11") -> list[str]:
    state_dir = tmp_path / "state"
    seed_state(
        state_dir,
        source_proposals=[
            {
                "id": PROPOSAL_ID,
                "source_url": URL,
                "normalized_source": URL.rstrip("/"),
                "proposed_by": "agent",
                "proposal_date": "2026-07-18",
                "reason": "evaluated and rejected",
                "supporting_evidence": [],
                "suggested_signal_family": "award",
                "source_name": "Synthetic Book Awards 2026",
                "status": "rejected",
                "decision_reason": REJECTION,
            }
        ],
    )
    accepted = outcome == "accept"
    ledger = tmp_path / "ledger.json"
    _write(
        ledger,
        {
            "version": 3,
            "total_entry_count": 1,
            "accept_count": int(accepted),
            "reject_count": int(not accepted),
            "defer_count": 0,
            "entries": [
                {
                    "url": URL,
                    "normalized_url": URL.rstrip("/"),
                    "name": "Synthetic Book Awards 2026",
                    "signal_family": "award",
                    "proposing_context": "unspecified",
                    "probe_date": probe_date,
                    "outcome": outcome,
                    "reason_code": "ok" if accepted else "award_row_shape",
                    "reason": "bounded award rows accepted" if accepted else "row refused",
                    "head_status": 200,
                    "get_status": 200,
                    "resolved_url": URL,
                    "http_evidence": {
                        "get": {"body_bytes": 100, "resolved_url": URL, "status": 200, "truncated": False},
                        "head": {"resolved_url": URL, "status": 200},
                    },
                    "detected_heading": "2026 Book Awards Shortlist Announcement",
                    "bounded_entry_count": 12 if accepted else 0,
                    "raw_row_count": 12 if accepted else 0,
                    "contract_family": "award-list",
                    "samples": [],
                }
            ],
        },
    )
    return [
        "source-proposal",
        "reconsider",
        "--id",
        PROPOSAL_ID,
        "--state-dir",
        str(state_dir),
        "--ledger",
        str(ledger),
        "--reason",
        "The award-list contract reads unlinked rows.",
        "--reconsidered-by",
        "operator",
    ]


def _proposal(tmp_path: Path) -> dict[str, Any]:
    return read_state(tmp_path / "state", "source_proposals")[0]


def test_the_preview_shows_the_old_rejection_and_writes_nothing(tmp_path: Path, capsys: Any) -> None:
    args = _case(tmp_path)
    before = state_snapshot(tmp_path / "state")

    assert main(args) == 3
    preview = json.loads(capsys.readouterr().out)

    assert preview["confirmation_required"] is True
    assert preview["existing_decision_reason"] == REJECTION
    assert preview["fresh_outcome"] == "accept"
    assert preview["fresh_bounded_entry_count"] == 12
    assert state_snapshot(tmp_path / "state") == before


def test_the_confirmed_call_reopens_the_proposal_and_keeps_the_rejection(tmp_path: Path, capsys: Any) -> None:
    args = _case(tmp_path)
    assert main(args) == 3
    token = json.loads(capsys.readouterr().out)["confirmation_token"]

    assert main([*args, "--confirmation-token", token, "--confirmed-by-editor"]) == 0

    proposal = _proposal(tmp_path)
    assert proposal["status"] == "proposed"
    assert proposal["proposal_date"] == "2026-10-11"
    assert proposal["prior_decisions"] == [
        {
            "status": "rejected",
            "proposal_date": "2026-07-18",
            "decision_reason": REJECTION,
            "reason": "evaluated and rejected",
        }
    ]
    assert proposal["supporting_evidence"][0]["contract_family"] == "award-list"


@pytest.mark.parametrize("token", ["", "0" * 64])
def test_a_missing_or_foreign_token_reopens_nothing(tmp_path: Path, capsys: Any, token: str) -> None:
    args = _case(tmp_path)

    assert main([*args, "--confirmation-token", token, "--confirmed-by-editor"]) == 1

    assert "reason_code=confirmation_token_mismatch" in capsys.readouterr().out
    assert _proposal(tmp_path)["status"] == "rejected"


def test_a_token_for_another_reason_reopens_nothing(tmp_path: Path, capsys: Any) -> None:
    args = _case(tmp_path)
    assert main(args) == 3
    token = json.loads(capsys.readouterr().out)["confirmation_token"]
    changed = [*args[: args.index("--reason") + 1], "Another reason.", *args[args.index("--reason") + 2 :]]

    assert main([*changed, "--confirmation-token", token, "--confirmed-by-editor"]) == 1
    assert _proposal(tmp_path)["status"] == "rejected"


@pytest.mark.parametrize(
    ("overrides", "reason_code"),
    [
        ({"outcome": "reject"}, "evidence_not_accepted"),
        ({"probe_date": "2026-07-18"}, "evidence_stale"),
    ],
)
def test_evidence_that_is_not_fresh_and_accepted_is_refused(
    tmp_path: Path, capsys: Any, overrides: dict[str, str], reason_code: str
) -> None:
    assert main(_case(tmp_path, **overrides)) == 1

    assert f"reason_code={reason_code}" in capsys.readouterr().out
    assert _proposal(tmp_path)["status"] == "rejected"


def test_a_proposal_with_no_ledger_entry_is_refused(tmp_path: Path, capsys: Any) -> None:
    args = _case(tmp_path)
    ledger = Path(args[args.index("--ledger") + 1])
    _write(
        ledger,
        {"version": 3, "total_entry_count": 0, "accept_count": 0, "reject_count": 0, "defer_count": 0, "entries": []},
    )

    assert main(args) == 1
    assert "reason_code=evidence_not_accepted" in capsys.readouterr().out


def test_a_ledger_entry_for_another_query_of_the_same_path_is_not_the_evidence(tmp_path: Path, capsys: Any) -> None:
    args = _case(tmp_path)
    ledger = Path(args[args.index("--ledger") + 1])
    data = json.loads(ledger.read_text(encoding="utf-8"))
    entry = data["entries"][0]
    other = f"{URL}?year=2025"
    entry.update(url=other, normalized_url=f"{URL.rstrip('/')}?year=2025", resolved_url=other)
    entry["http_evidence"]["get"]["resolved_url"] = other
    entry["http_evidence"]["head"]["resolved_url"] = other
    _write(ledger, data)

    assert main(args) == 1
    assert "reason_code=evidence_not_accepted" in capsys.readouterr().out
    assert _proposal(tmp_path)["status"] == "rejected"
