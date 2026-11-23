"""Offline coverage for issue #635: description adjudication contract and command.

Adjudication decides whether a public web page really describes a book.
This ticket builds the typed adapter seam, a deterministic replay, the
``adjudicate-descriptions`` command, and a preview artifact. Nothing here
touches the network or changes Candidate History.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.__main__ import main
from autoscout.description_adjudication import ADJUDICATION_RESPONSE_SCHEMA
from autoscout.description_adjudication import MAX_ADJUDICATION_REASON_LENGTH
from autoscout.description_adjudication import ReplayDescriptionAdjudicationAdapter
from autoscout.description_adjudication import adjudicate_descriptions
from autoscout.description_adjudication import validate_adjudication_response
from autoscout.grounded_description import BoundedHTTPError
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.json_io import read_json

OPERATION_DATE = "2026-09-14"


class _UnreachableTransport:
    """Fail every request instantly so contract tests stay offline.

    Issue #636 added code verification to the preview path, which
    re-fetches cited pages. These contract tests pin the preview shape,
    not verification, so every fetch fails closed without a network call.
    """

    def request(self, url: str, **_kwargs: Any) -> Any:
        raise BoundedHTTPError("offline contract test never fetches")


def _offline_fetcher() -> BoundedHTTPFetcher:
    return BoundedHTTPFetcher(transport=_UnreachableTransport())  # type: ignore[arg-type]


def _record(candidate_id: str, identity_key: str, title: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "identity_key": identity_key,
        "title": title,
        "primary_authors": ["Alex Author"],
        "content_description": "",
    }


def _citation(url: str, host: str, *, describes: bool = True) -> dict[str, Any]:
    return {
        "url": url,
        "host": host,
        "surface_kind": "publisher",
        "printed_title": "Work One",
        "printed_authors": ["Alex Author"],
        "describes_this_work": describes,
        "identity_note": "",
    }


def _response(identity_key: str) -> dict[str, Any]:
    return {
        "identity_key": identity_key,
        "resolved": True,
        "unresolved_reason": "",
        "confidence": "high",
        "content_description": "Книга о памяти, обществе и общественной жизни.",
        "citations": [
            _citation("https://publisher.example.test/work-one", "publisher.example.test"),
            _citation("https://catalog.example.test/work-one", "catalog.example.test"),
        ],
    }


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir()
    output_dir.mkdir()
    seed_state(
        state_dir,
        candidate_history=[
            _record("candidate-001", "work one|alex author", "Work One"),
            _record("candidate-002", "work two|alex author", "Work Two"),
        ],
    )
    return state_dir, output_dir


def test_schema_lists_every_property_key_as_required() -> None:
    assert set(ADJUDICATION_RESPONSE_SCHEMA["properties"]) == set(ADJUDICATION_RESPONSE_SCHEMA["required"])
    items = ADJUDICATION_RESPONSE_SCHEMA["properties"]["citations"]["items"]
    assert set(items["properties"]) == set(items["required"])


def test_well_formed_replay_response_reaches_preview(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    adapter = ReplayDescriptionAdjudicationAdapter({"work one|alex author": _response("work one|alex author")})

    result = adjudicate_descriptions(
        state_dir, output_dir, effective_date=OPERATION_DATE, adapter=adapter, fetcher=_offline_fetcher()
    )

    assert result["status"] == "ok"
    assert sorted(adapter.calls) == ["work one|alex author", "work two|alex author"]
    preview = read_json(output_dir / "description-adjudication-preview.json")
    rows = {row["identity_key"]: row for row in preview["preview"]}
    first = rows["work one|alex author"]
    assert first["candidate_id"] == "candidate-001"
    assert first["reason"] == ""
    assert first["response"]["resolved"] is True
    assert first["response"]["confidence"] == "high"
    assert len(first["response"]["citations"]) == 2
    # The replay has no verdict for the second candidate, which is still a
    # well-formed unresolved response rather than a rejection.
    second = rows["work two|alex author"]
    assert second["candidate_id"] == "candidate-002"
    assert second["reason"] == ""
    assert second["response"]["resolved"] is False


def test_malformed_replay_response_is_rejected_with_bounded_reason(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    malformed: dict[str, Any] = {
        "identity_key": "work one|alex author",
        "resolved": "yes",
        "bogus": "unexpected",
    }
    adapter = ReplayDescriptionAdjudicationAdapter(
        {
            "work one|alex author": malformed,
            "work two|alex author": _response("work two|alex author"),
        }
    )

    result = adjudicate_descriptions(
        state_dir, output_dir, effective_date=OPERATION_DATE, adapter=adapter, fetcher=_offline_fetcher()
    )

    # One bad response does not stop the command: the other candidate is
    # still previewed as valid.
    assert result["status"] == "ok"
    assert result["valid"] == 1
    assert result["rejected"] == 1
    preview = read_json(output_dir / "description-adjudication-preview.json")
    rows = {row["identity_key"]: row for row in preview["preview"]}
    rejected = rows["work one|alex author"]
    assert rejected["candidate_id"] == "candidate-001"
    assert rejected["response"] == {}
    assert rejected["reason"]
    assert len(rejected["reason"]) <= MAX_ADJUDICATION_REASON_LENGTH
    assert rows["work two|alex author"]["reason"] == ""


def test_consecutive_runs_write_byte_identical_previews(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    before = state_snapshot(state_dir)
    adapter = ReplayDescriptionAdjudicationAdapter({"work one|alex author": _response("work one|alex author")})

    adjudicate_descriptions(
        state_dir, output_dir, effective_date=OPERATION_DATE, adapter=adapter, fetcher=_offline_fetcher()
    )
    first = (output_dir / "description-adjudication-preview.json").read_bytes()
    adjudicate_descriptions(
        state_dir, output_dir, effective_date=OPERATION_DATE, adapter=adapter, fetcher=_offline_fetcher()
    )
    second = (output_dir / "description-adjudication-preview.json").read_bytes()

    assert first == second
    # The command writes a preview only: history is untouched.
    assert state_snapshot(state_dir) == before


def test_preview_rows_sort_by_candidate_id(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)

    adjudicate_descriptions(state_dir, output_dir, effective_date=OPERATION_DATE)

    preview = read_json(output_dir / "description-adjudication-preview.json")
    assert [row["candidate_id"] for row in preview["preview"]] == ["candidate-001", "candidate-002"]


def test_limit_and_named_selection_shape(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)

    limited = adjudicate_descriptions(state_dir, output_dir, limit=1, effective_date=OPERATION_DATE)
    assert limited["considered"] == 1

    selected = adjudicate_descriptions(
        state_dir,
        output_dir,
        only=["work two|alex author"],
        effective_date=OPERATION_DATE,
    )
    assert selected["considered"] == 1
    preview = read_json(output_dir / "description-adjudication-preview.json")
    assert [row["candidate_id"] for row in preview["preview"]] == ["candidate-002"]

    with pytest.raises(ValueError, match="cannot be combined"):
        adjudicate_descriptions(state_dir, output_dir, limit=1, only=["work two|alex author"])


def test_named_selection_of_candidate_without_description_work_previews_nothing(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    history = read_state(state_dir, "candidate_history")
    history[0]["content_description"] = "Книга о памяти, обществе и общественной жизни."
    seed_state(state_dir, candidate_history=history)

    selected = adjudicate_descriptions(
        state_dir,
        output_dir,
        only=["work one|alex author"],
        effective_date=OPERATION_DATE,
    )

    # Selection reuses the backfill helper, so a named candidate with a valid
    # description has no work item: nothing is adjudicated. The explicit
    # request is still reported as a skipped preview row (issue #637).
    assert selected["considered"] == 0
    assert selected["skipped"] == 1
    preview = read_json(output_dir / "description-adjudication-preview.json")
    (row,) = preview["preview"]
    assert row["candidate_id"] == "candidate-001"
    assert row["decision"] == "skipped"
    assert row["application"]["skipped"] == 1


def test_cli_writes_preview_to_custom_path(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    custom = output_dir / "custom" / "preview.json"

    assert (
        main(
            [
                "adjudicate-descriptions",
                "--state-dir",
                str(state_dir),
                "--output-dir",
                str(output_dir),
                "--run-date",
                OPERATION_DATE,
                "--preview-file",
                str(custom),
            ]
        )
        == 0
    )
    preview = read_json(custom)
    assert preview["artifact"] == "adjudicate-descriptions"
    assert preview["operation_date"] == OPERATION_DATE
    assert preview["counts"]["previewed"] == 2


def test_validator_rejects_each_contract_violation() -> None:
    base = _response("work one|alex author")

    assert validate_adjudication_response(base) == (True, "")

    missing = dict(base)
    del missing["citations"]
    assert validate_adjudication_response(missing)[0] is False

    extra = dict(base)
    extra["optional_field"] = ""
    assert validate_adjudication_response(extra)[0] is False

    bad_confidence = dict(base)
    bad_confidence["confidence"] = "certain"
    assert validate_adjudication_response(bad_confidence)[0] is False

    bad_citation = dict(base)
    bad_citation["citations"] = [dict(_citation("https://x.example.test/a", "x.example.test"))]
    del bad_citation["citations"][0]["identity_note"]
    assert validate_adjudication_response(bad_citation)[0] is False

    assert validate_adjudication_response("not-an-object")[0] is False
