"""Issue #386 — a refused artifact is reported as refused, with its reason.

On the 2026-08-05 live weekly run, six sources produced complete artifacts
holding 124 grounded mentions between them. Every artifact carried a single
top-level key, ``mentions``, and none of the four identity fields the validator
requires, so all six were refused as ``identity_mismatch`` and their mentions
were discarded.

Two reporting faults hid that from the operator. The quarantine record wrote
``diagnostic: ""``, so the class the validator had already computed never
reached the workflow log. And ``source-runs.json`` said the extraction had
produced no artifact, which is the opposite of what happened and points the
reader at capture instead of at the artifact.

The cause was a contract that contradicted itself: ``extraction_contract``
declared ``required_output_shape`` as a bare ``{"mentions": [...]}`` while the
Codex prompt asked in prose for the identity envelope. An extractor that
believed the machine-readable shape was refused for obeying it.
"""

from __future__ import annotations

import json
from pathlib import Path

from autoscout.codex_recovery import EXTRACTION_OUTCOMES_FILE
from autoscout.codex_recovery import QuarantineResult
from autoscout.codex_recovery import QuarantineStatus
from autoscout.codex_recovery import _record_quarantine_diagnostic
from autoscout.codex_workflow import finalize_codex_run
from autoscout.observed_extraction import validate_extraction_artifact
from autoscout.source_collection import SourceCollection
from autoscout.source_collectors.contract import extraction_contract
from autoscout.source_rules import EXTRACTION_ARTIFACT_MISSING
from autoscout.source_rules import EXTRACTION_ARTIFACT_REJECTED
from autoscout.source_rules import withheld_reason_message

OPERATION_ID = "f4245698f3e9e7e2358e3b27a47121cd1f8cacf6d2ce01c0531af81fcc22a9ab"
SOURCE_NAME = "Audible Bestselling Science & Engineering Audiobooks"
SOURCE_URL = "https://www.audible.com/charts/best/science-engineering-audiobooks/18580540011"


def _capture(tmp_path: Path) -> tuple[Path, str]:
    """Write a capture and return its path with its digest."""
    import hashlib

    capture_path = tmp_path / "codex-capture-audible-science.json"
    capture_path.write_text(
        json.dumps(
            {
                "source_name": SOURCE_NAME,
                "source_url": SOURCE_URL,
                "html": f'<a href="{SOURCE_URL}">Biological War</a>',
            }
        ),
        encoding="utf-8",
    )
    return capture_path, hashlib.sha256(capture_path.read_bytes()).hexdigest()


def _work_item(capture_digest: str) -> dict:
    return {
        "source_name": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "operation_id": OPERATION_ID,
        "capture_sha256": capture_digest,
        "capture_file": "codex-capture-audible-science.json",
        "expected_extraction_file": "audible-science-codex-extraction.json",
        "source_family": "audiobook ranking",
    }


# ── The refusal itself ────────────────────────────────────────────────────


def test_artifact_with_entries_but_no_envelope_is_refused_on_identity(tmp_path: Path) -> None:
    """The observed failure mode: grounded mentions, no identity envelope."""
    capture_path, digest = _capture(tmp_path)
    artifact = tmp_path / "audible-science-codex-extraction.json"
    artifact.write_text(
        json.dumps(
            {
                "mentions": [
                    {
                        "title": "Biological War",
                        "primary_authors": ["Annie Jacobsen"],
                        "source_url": SOURCE_URL,
                        "evidence_url": SOURCE_URL,
                        "signal_type": "audiobook ranking",
                        "signal_strength": "minor mention",
                        "evidence_note": "Audible chart entry",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    observation = validate_extraction_artifact(artifact, _work_item(digest), capture_path)

    assert observation.valid is False
    assert observation.diagnostic_class == "identity_mismatch"
    # The entries were read and counted before the identity check refused them,
    # which is what makes the refusal a loss rather than an empty result.
    assert observation.raw_extracted_count == 1


# ── Step 1a: the class reaches the workflow log ───────────────────────────


def test_quarantine_diagnostic_names_the_validation_class() -> None:
    diagnostic = _record_quarantine_diagnostic(
        {"source_name": SOURCE_NAME},
        QuarantineResult(QuarantineStatus.PRESERVED, "/tmp/quarantine/artifact.rejected-1"),
        1,
        diagnostic_class="identity_mismatch",
        raw_extracted_count=20,
    )

    assert diagnostic["status"] == "rejected_artifact_preserved"
    assert diagnostic["diagnostic_class"] == "identity_mismatch"
    assert diagnostic["raw_extracted_count"] == 20
    # The regression: an empty diagnostic told the operator nothing at all.
    assert diagnostic["diagnostic"] == "identity_mismatch"


def test_preservation_error_outranks_the_validation_class() -> None:
    """A quarantine that failed describes itself first: the bytes may be gone."""
    diagnostic = _record_quarantine_diagnostic(
        {"source_name": SOURCE_NAME},
        QuarantineResult(QuarantineStatus.PRESERVATION_FAILED, "", diagnostic="OSError"),
        1,
        diagnostic_class="identity_mismatch",
    )

    assert diagnostic["diagnostic"] == "OSError"
    assert diagnostic["diagnostic_class"] == "identity_mismatch"


def test_quarantine_diagnostic_omits_absent_counts() -> None:
    diagnostic = _record_quarantine_diagnostic(
        {"source_name": SOURCE_NAME},
        QuarantineResult(QuarantineStatus.PRESERVED, "/tmp/a.rejected-1"),
    )

    assert "raw_extracted_count" not in diagnostic
    assert "diagnostic_class" not in diagnostic
    assert diagnostic["diagnostic"] == ""


# ── Step 1b: a refusal is not reported as an absence ──────────────────────


def _worklist(tmp_path: Path, digest: str) -> None:
    (tmp_path / "codex-extraction-worklist.json").write_text(
        json.dumps({"run_date": "2026-08-05", "work_items": [_work_item(digest)], "precollected_sources": []}),
        encoding="utf-8",
    )


def _replay_source(tmp_path: Path) -> dict:
    config = json.loads((tmp_path / "codex-replay-config.json").read_text(encoding="utf-8"))
    return config["sources"][0]


def test_refused_artifact_is_reported_as_rejected_not_absent(tmp_path: Path, capsys) -> None:
    _capture(tmp_path)
    _worklist(tmp_path, "digest")
    (tmp_path / EXTRACTION_OUTCOMES_FILE).write_text(
        json.dumps(
            {
                "outcomes": [
                    {
                        "source_name": SOURCE_NAME,
                        "outcome": "rejected",
                        "diagnostic_class": "identity_mismatch",
                        "raw_extracted_count": 20,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert finalize_codex_run(tmp_path) == 0

    source = _replay_source(tmp_path)
    assert source["inactive_reason"] == EXTRACTION_ARTIFACT_REJECTED
    assert source["withheld_detail"] == "identity_mismatch; 20 extracted mentions discarded"
    message = withheld_reason_message(source)
    assert "failed validation" in message
    assert "identity_mismatch" in message
    assert "20 extracted mentions discarded" in message
    # The exact wording that misled the operator must not appear.
    assert "produced no artifact" not in message

    out = capsys.readouterr().out
    assert "rejected_sources=1" in out
    # A refusal is not a pending save. Re-saving the same bytes under the same
    # name earns the same refusal, so the operator must not be sent there.
    assert "pending_sources=0" in out
    assert "pending_artifact=" not in out
    assert f"rejected_artifact={SOURCE_NAME} (identity_mismatch; 20 extracted mentions discarded)" in out
    assert "autoscout-rejected-artifacts" in out


def test_a_withheld_precollected_source_prints_no_empty_save_list(tmp_path: Path, capsys) -> None:
    """A precollected source is pending but names no extraction artifact."""
    _capture(tmp_path)
    (tmp_path / "codex-extraction-worklist.json").write_text(
        json.dumps(
            {
                "run_date": "2026-08-05",
                "work_items": [],
                "precollected_sources": [
                    {
                        "name": "Nonfiction.fr Critiques",
                        "family": "media/curated list",
                        "url": "https://example.test/nonfiction-fr",
                        "language": "en",
                        "recorded_mentions_file": "never-written.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert finalize_codex_run(tmp_path) == 0

    out = capsys.readouterr().out
    assert "pending_sources=1" in out
    assert "Save each pending Codex extraction artifact" not in out


def test_absent_and_rejected_sources_are_listed_separately(tmp_path: Path, capsys) -> None:
    """Each ending gets the next step that actually fits it."""
    _capture(tmp_path)
    absent_item = _work_item("digest") | {
        "source_name": "Never Arrived",
        "expected_extraction_file": "never-arrived-codex-extraction.json",
    }
    (tmp_path / "codex-extraction-worklist.json").write_text(
        json.dumps(
            {
                "run_date": "2026-08-05",
                "work_items": [_work_item("digest"), absent_item],
                "precollected_sources": [],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / EXTRACTION_OUTCOMES_FILE).write_text(
        json.dumps(
            {
                "outcomes": [
                    {"source_name": SOURCE_NAME, "outcome": "rejected", "diagnostic_class": "identity_mismatch"},
                    {"source_name": "Never Arrived", "outcome": "absent"},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert finalize_codex_run(tmp_path) == 0

    out = capsys.readouterr().out
    assert "pending_sources=1" in out
    assert "rejected_sources=1" in out
    assert "pending_artifact=never-arrived-codex-extraction.json (Never Arrived)" in out
    # The refused source must appear only under the rejected heading.
    assert f"pending_artifact=audible-science-codex-extraction.json ({SOURCE_NAME})" not in out
    assert f"rejected_artifact={SOURCE_NAME}" in out


def test_truly_absent_artifact_still_reports_as_absent(tmp_path: Path) -> None:
    """The rejected reason must not swallow the case it was split away from."""
    _capture(tmp_path)
    _worklist(tmp_path, "digest")
    (tmp_path / EXTRACTION_OUTCOMES_FILE).write_text(
        json.dumps({"outcomes": [{"source_name": SOURCE_NAME, "outcome": "absent"}]}),
        encoding="utf-8",
    )

    assert finalize_codex_run(tmp_path) == 0

    source = _replay_source(tmp_path)
    assert source["inactive_reason"] == EXTRACTION_ARTIFACT_MISSING
    assert "withheld_detail" not in source
    assert "produced no artifact" in withheld_reason_message(source)


def test_missing_outcomes_record_degrades_to_the_absent_reason(tmp_path: Path) -> None:
    """A run whose extraction predates the record must still finalize."""
    _capture(tmp_path)
    _worklist(tmp_path, "digest")

    assert finalize_codex_run(tmp_path) == 0
    assert _replay_source(tmp_path)["inactive_reason"] == EXTRACTION_ARTIFACT_MISSING


def test_rejected_source_reaches_the_failed_count(tmp_path: Path) -> None:
    """The reason must travel the whole chain, not stop at the replay config."""
    sources = [
        {
            "name": SOURCE_NAME,
            "family": "audiobook ranking",
            "url": SOURCE_URL,
            "language": "en",
            "active": False,
            "inactive_reason": EXTRACTION_ARTIFACT_REJECTED,
            "withheld_detail": "identity_mismatch; 20 extracted mentions discarded",
        }
    ]

    result = SourceCollection(config_path=tmp_path / "config.json", output_dir=tmp_path, run_date="2026-08-05").collect(
        sources
    )

    assert result.failed_source_count == 1
    run = result.source_runs[0]
    assert run["status"] == "failed"
    assert "failed validation" in run["reason"]
    assert "identity_mismatch" in run["reason"]
    assert "produced no artifact" not in run["reason"]


# ── Step 2: the contract states the envelope ──────────────────────────────

CAPTURE_BOUND_SOURCE = {
    "name": SOURCE_NAME,
    "family": "audiobook ranking",
    "url": SOURCE_URL,
    "language": "en",
    "audible_explicit_chart": True,
}


def test_capture_bound_contract_requires_the_identity_envelope() -> None:
    contract = extraction_contract(CAPTURE_BOUND_SOURCE, capture_file="codex-capture-audible-science.json")

    shape = contract["required_output_shape"]
    for field in ("source_name", "source_url", "operation_id", "capture_sha256", "entries"):
        assert field in shape, f"capture-bound contract must declare {field}"
    assert contract["required_top_level_fields"] == [
        "source_name",
        "source_url",
        "operation_id",
        "capture_sha256",
        "entries",
    ]
    # The bare shape is what the six refused sources produced.
    assert "mentions" not in shape


def test_capture_bound_contract_shape_satisfies_the_validator(tmp_path: Path) -> None:
    """The contract and the validator must agree, which is the whole point.

    An extraction that returns exactly the declared top-level shape must not be
    refused on identity. This is the check that would have failed before the
    fix, when the contract declared a shape the validator rejected (#375).
    """
    capture_path, digest = _capture(tmp_path)
    item = _work_item(digest)
    contract = extraction_contract(CAPTURE_BOUND_SOURCE, capture_file=item["capture_file"])

    artifact = tmp_path / item["expected_extraction_file"]
    artifact.write_text(
        json.dumps(
            {field: item[field] for field in contract["required_top_level_fields"] if field != "entries"}
            | {
                "entries": [
                    {
                        "title": "Biological War",
                        "primary_authors": ["Annie Jacobsen"],
                        "source_url": SOURCE_URL,
                        "evidence_url": SOURCE_URL,
                        "signal_type": "audiobook ranking",
                        "signal_strength": "minor mention",
                        "evidence_note": "Audible chart entry",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    observation = validate_extraction_artifact(artifact, item, capture_path)

    assert observation.diagnostic_class != "identity_mismatch"


def test_inline_agent_contract_keeps_the_bare_mentions_shape() -> None:
    """No capture means nothing to bind, and the inline parser reads mentions."""
    contract = extraction_contract(CAPTURE_BOUND_SOURCE)

    assert "mentions" in contract["required_output_shape"]
    assert "required_top_level_fields" not in contract
