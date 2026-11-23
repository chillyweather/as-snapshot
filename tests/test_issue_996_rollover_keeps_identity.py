"""A source rolled over to a new edition keeps the books it already brought (Issue #996).

An annual source is rolled over by adding a source under the new edition's
name and retiring the old one, so a book the 2025 page brought can come back
from a source with a different name. A Candidate identity is the book's title
and authors, never the source that cited it: the book keeps its identity, its
Candidate ID and the editor's decision, and a decided book does not come back
as new.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any

from autoscout.candidate_identity import candidate_id_for
from autoscout.cli.weekly import run_mvp

OLD_SOURCE = "Goodreads Choice Awards 2025 Nonfiction"
NEW_SOURCE = "Goodreads Choice Awards 2026 Nonfiction"
REJECTED_KEY = "rejected book|alex example"
INTERESTED_KEY = "interested book|robin example"


def _history_record(identity_key: str, title: str, author: str, status: str) -> dict[str, Any]:
    """A Candidate History record as a run on the 2025 page left it."""
    signal = {
        "source_name": OLD_SOURCE,
        "source_family": "reader-interest",
        "signal_type": "reader-interest",
        "signal_strength": "reader choice winner",
        "source_url": "https://example.test/choiceawards/nonfiction-2025",
        "evidence_url": "https://example.test/book/show/1",
    }
    return {
        "identity_key": identity_key,
        "candidate_id": candidate_id_for(identity_key),
        "title": title,
        "primary_authors": [author],
        "editor_status": status,
        "editor_note": "Decided on the 2025 page.",
        "independent_signal_families": ["reader-interest"],
        "primary_signal": signal,
        "primary_signal_strength": "reader choice winner",
        "source_evidence": [signal],
        "russian_edition_status": "",
        "visible_risk_status": "",
    }


def _nominee(title: str, author: str) -> dict[str, Any]:
    return {
        "title": title,
        "primary_authors": [author],
        "source_url": "https://example.test/choiceawards/nonfiction-2026",
        "signal_strength": "reader favorite",
        "evidence_note": "Opening-round nominee on the 2026 page.",
    }


def _run(tmp_path: Path) -> Path:
    config_path = tmp_path / "sources.json"
    config_path.write_text(
        json.dumps(
            {
                "run_date": "2026-11-13",
                "candidate_history": [
                    _history_record(REJECTED_KEY, "Rejected Book", "Alex Example", "reject"),
                    _history_record(INTERESTED_KEY, "Interested Book", "Robin Example", "interested"),
                ],
                "sources": [
                    {
                        "name": NEW_SOURCE,
                        "family": "reader-interest",
                        "url": "https://example.test/choiceawards/nonfiction-2026",
                        "access_status": "open",
                        "extraction_method": "fixture",
                        "check_frequency": "annual",
                        "language": "en",
                        "notes": "The rolled-over source.",
                        "fixture_candidates": [
                            _nominee("Rejected Book", "Alex Example"),
                            _nominee("Interested Book", "Robin Example"),
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "runs"
    with redirect_stderr(io.StringIO()):
        run_mvp(config_path, output_dir)
    return output_dir


def test_a_rejected_book_seen_again_from_the_rolled_over_source_stays_hidden(tmp_path: Path) -> None:
    output_dir = _run(tmp_path)

    suppressed = json.loads((output_dir / "suppressed-candidates.json").read_text(encoding="utf-8"))
    assert [(row["identity_key"], row["editor_status"]) for row in suppressed] == [(REJECTED_KEY, "reject")]
    candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
    assert REJECTED_KEY not in {candidate["identity_key"] for candidate in candidates}


def test_a_decided_book_keeps_its_identity_and_is_not_new(tmp_path: Path) -> None:
    output_dir = _run(tmp_path)

    candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
    [candidate] = [candidate for candidate in candidates if candidate["title"] == "Interested Book"]
    assert candidate["identity_key"] == INTERESTED_KEY
    assert candidate["candidate_id"] == candidate_id_for(INTERESTED_KEY)
    assert candidate["update_delta"] == "unchanged"
    assert candidate["primary_signal"]["source_name"] == NEW_SOURCE


def test_history_keeps_one_record_per_book_with_its_decision(tmp_path: Path) -> None:
    output_dir = _run(tmp_path)

    history = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
    assert sorted((record["identity_key"], record["candidate_id"], record["editor_status"]) for record in history) == [
        (INTERESTED_KEY, candidate_id_for(INTERESTED_KEY), "interested"),
        (REJECTED_KEY, candidate_id_for(REJECTED_KEY), "reject"),
    ]
