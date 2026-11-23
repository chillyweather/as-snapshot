"""Deterministic offline tests for the sanitized missing-required-fields aggregate.

The aggregate is the typed, content-free diagnostic payload carried by
``validation_missing_required_fields`` observations: exact examined and
rejected entry counts plus canonical per-field missing counts. It must
never store titles, author names, URLs, prose, raw output, credentials,
or tokens, and it must stay optional so existing artifacts remain valid.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from autoscout.observed_extraction import observe_extraction
from autoscout.observed_extraction import validate_extraction_artifact
from autoscout.source_collectors import CODEX_CANONICAL_MISSING_FIELDS
from autoscout.source_collectors import _validate_codex_raw_required_fields
from autoscout.source_collectors import codex_missing_field_aggregate


def _capture(tmp_path: Path) -> tuple[dict[str, str], Path]:
    capture = tmp_path / "capture.json"
    capture.write_text(
        json.dumps({"source_name": "Source", "source_url": "https://example.test/source", "html": ""}),
        encoding="utf-8",
    )
    item = {
        "source_name": "Source",
        "source_url": "https://example.test/source",
        "operation_id": "operation",
        "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
    }
    return item, capture


def _full_entry(**overrides: Any) -> dict[str, Any]:
    entry = {
        "title": "The Test Book",
        "primary_authors": ["Jane Author"],
        "signal_type": "media",
        "signal_strength": "mention",
        "evidence_note": "Listed on the source page.",
        "evidence_url": "https://example.test/evidence",
        "source_url": "https://example.test/source",
    }
    entry.update(overrides)
    return entry


def _artifact(tmp_path: Path, item: dict[str, Any], entries: list[Any]) -> Path:
    artifact = tmp_path / "extraction.json"
    artifact.write_text(
        json.dumps(
            {
                **{key: item[key] for key in ("source_name", "source_url", "operation_id", "capture_sha256")},
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )
    return artifact


# ── exact counts and canonical normalization ────────────────────────────────


def test_exact_counts_for_examined_rejected_and_missing_fields() -> None:
    entries = [
        _full_entry(),
        _full_entry(title=""),  # missing title only
        _full_entry(evidence_note="   "),  # missing evidence_note only
        _full_entry(primary_authors=[], authors=[]),  # missing primary_authors only
        "not-an-object",  # missing every required field
    ]
    valid, dropped = _validate_codex_raw_required_fields(entries)
    assert len(valid) == 1
    assert len(dropped) == 4

    aggregate = codex_missing_field_aggregate(entries)
    assert aggregate is not None
    assert aggregate.examined_entries == 5
    assert aggregate.rejected_entries == 4
    # The non-object entry counts as missing every canonical field.
    assert aggregate.missing_field_counts == {
        "title": 2,
        "signal_type": 1,
        "signal_strength": 1,
        "evidence_note": 2,
        "primary_authors": 2,
        "source_url": 1,
    }


def test_missing_field_counts_are_canonical_and_aggregate_stays_absent_when_nothing_dropped() -> None:
    entries = [
        _full_entry(primary_authors=["Jane Author"], authors=["Other Author"], source_url="https://example.test/x"),
        _full_entry(primary_authors=["Jane Author"], authors=["Other Author"], item_url="https://example.test/y"),
    ]
    aggregate = codex_missing_field_aggregate(entries)
    assert aggregate is None

    assert codex_missing_field_aggregate([]) is None
    assert codex_missing_field_aggregate([_full_entry()]) is None


def test_authors_and_url_variants_normalize_to_canonical_field_names() -> None:
    # ``authors`` satisfies the author requirement and must not count
    # under ``primary_authors``; ``item_url`` and ``evidence_url`` satisfy
    # the URL requirement and must not count under ``source_url``.
    entries = [
        _full_entry(primary_authors=[], authors=["Jane Author"], item_url="https://example.test/item"),
        _full_entry(primary_authors=[], authors=["Jane Author"], evidence_url="https://example.test/evidence"),
        _full_entry(primary_authors=["Jane Author"], source_url="https://example.test/source"),
        _full_entry(primary_authors=[], authors=[], source_url=""),  # author requirement missing only
    ]
    aggregate = codex_missing_field_aggregate(entries)
    assert aggregate is not None
    assert aggregate.examined_entries == 4
    assert aggregate.rejected_entries == 1
    assert aggregate.missing_field_counts == {"primary_authors": 1}
    assert "source_url" not in aggregate.missing_field_counts


def test_source_check_date_is_never_counted_as_missing() -> None:
    entries = [_full_entry(source_check_date="2026-08-01") for _ in range(3)]
    aggregate = codex_missing_field_aggregate(entries)
    assert aggregate is None


def test_partial_drop_counts_only_rejected_entries() -> None:
    entries = [
        _full_entry(),
        _full_entry(title=""),
        _full_entry(primary_authors=[], authors=[]),
    ]
    valid, dropped = _validate_codex_raw_required_fields(entries)
    assert len(valid) == 1
    assert len(dropped) == 2
    aggregate = codex_missing_field_aggregate(entries)
    assert aggregate is not None
    assert aggregate.examined_entries == 3
    assert aggregate.rejected_entries == 2
    assert aggregate.missing_field_counts == {"title": 1, "primary_authors": 1}


# ── no content leakage ───────────────────────────────────────────────────────


def test_aggregate_never_leaks_titles_authors_urls_or_prose(tmp_path: Path) -> None:
    item, capture = _capture(tmp_path)
    sensitive = {
        "title": "The Hidden Secret Book Title",
        "author": "Sensitive Author Name",
        "url": "https://example.test/private/evidence/12345",
        "prose": "This is a long prose evidence note that must never be stored.",
        "credential": "sk-secret-token-value",
    }
    entries = [
        {
            "title": sensitive["title"],
            "signal_type": "media",
            "signal_strength": "mention",
            "evidence_note": sensitive["prose"],
            "evidence_url": sensitive["url"],
            "secret_token": sensitive["credential"],
            "primary_authors": [],
        },
        {
            "title": sensitive["title"],
            "primary_authors": [sensitive["author"]],
            "signal_type": "media",
            "signal_strength": "mention",
            "evidence_note": sensitive["prose"],
        },
        "not-an-object",
    ]
    artifact = _artifact(tmp_path, item, entries)
    observation = validate_extraction_artifact(artifact, item, capture)

    assert observation.valid is False
    assert observation.diagnostic_class == "missing_required_fields"
    assert observation.missing_required_fields is not None
    serialized = json.dumps(observation.missing_required_fields.to_dict())
    for fragment in sensitive.values():
        assert fragment not in serialized
    assert observation.missing_required_fields.missing_field_counts == {
        "title": 1,
        "signal_type": 1,
        "signal_strength": 1,
        "evidence_note": 1,
        "primary_authors": 2,
        "source_url": 2,
    }


def test_record_serialization_stays_secret_free(tmp_path: Path) -> None:
    item, capture = _capture(tmp_path)

    class Agent:
        def extract_with_timeout(self, work_item, capture_path, artifact_path, *, timeout_seconds):
            artifact_path.write_text(
                json.dumps(
                    {
                        **{
                            key: work_item[key]
                            for key in ("source_name", "source_url", "operation_id", "capture_sha256")
                        },
                        "entries": [
                            {
                                "title": "Leaked Book Title",
                                "signal_type": "media",
                                "signal_strength": "mention",
                                "evidence_note": "Leaked prose body.",
                                "evidence_url": "https://example.test/leaked/url",
                                "primary_authors": [],
                            },
                            "not-an-object",
                        ],
                    }
                ),
                encoding="utf-8",
            )
            from autoscout.codex_recovery import ExtractionAttempt
            from autoscout.codex_recovery import ExtractionStatus

            return ExtractionAttempt(ExtractionStatus.PRODUCED, False, 0)

    observed = observe_extraction(item, capture, tmp_path / ".staging.json", timeout_seconds=600, agent=Agent())
    assert observed.diagnostic_class == "validation_missing_required_fields"
    assert observed.validation is not None
    record = observed.validation.to_dict()
    aggregate = record["validation_missing_required_fields"]
    serialized = json.dumps(record)
    for fragment in ("Leaked Book Title", "Leaked Author", "https://example.test/leaked/url", "Leaked prose body."):
        assert fragment not in serialized
    assert set(aggregate["missing_field_counts"]) <= set(CODEX_CANONICAL_MISSING_FIELDS)


# ── propagation through ValidationObservation ─────────────────


def test_propagation_from_required_field_validation_into_record(tmp_path: Path) -> None:
    item, capture = _capture(tmp_path)

    class Agent:
        def extract_with_timeout(self, work_item, capture_path, artifact_path, *, timeout_seconds):
            artifact_path.write_text(
                json.dumps(
                    {
                        **{
                            key: work_item[key]
                            for key in ("source_name", "source_url", "operation_id", "capture_sha256")
                        },
                        "entries": [_full_entry(title=""), _full_entry(primary_authors=[], authors=[])],
                    }
                ),
                encoding="utf-8",
            )
            from autoscout.codex_recovery import ExtractionAttempt
            from autoscout.codex_recovery import ExtractionStatus

            return ExtractionAttempt(ExtractionStatus.PRODUCED, False, 0)

    observed = observe_extraction(item, capture, tmp_path / ".staging.json", timeout_seconds=600, agent=Agent())

    assert observed.final_status == "invalid_output"
    assert observed.diagnostic_class == "validation_missing_required_fields"
    assert observed.validation is not None
    assert observed.validation.valid is False
    assert observed.validation.diagnostic_class == "missing_required_fields"
    assert observed.validation.missing_required_fields is not None
    assert observed.validation.missing_required_fields.examined_entries == 2
    assert observed.validation.missing_required_fields.rejected_entries == 2
    assert observed.validation.missing_required_fields.missing_field_counts == {
        "title": 1,
        "primary_authors": 1,
    }

    assert observed.validation.to_dict()["validation_missing_required_fields"] == {
        "examined_entries": 2,
        "rejected_entries": 2,
        "missing_field_counts": {"title": 1, "primary_authors": 1},
    }


def test_validated_observation_has_no_aggregate(tmp_path: Path) -> None:
    item, capture = _capture(tmp_path)

    class Agent:
        def extract_with_timeout(self, work_item, capture_path, artifact_path, *, timeout_seconds):
            artifact_path.write_text(
                json.dumps(
                    {
                        **{
                            key: work_item[key]
                            for key in ("source_name", "source_url", "operation_id", "capture_sha256")
                        },
                        "entries": [],
                    }
                ),
                encoding="utf-8",
            )
            from autoscout.codex_recovery import ExtractionAttempt
            from autoscout.codex_recovery import ExtractionStatus

            return ExtractionAttempt(ExtractionStatus.PRODUCED, False, 0)

    observed = observe_extraction(item, capture, tmp_path / ".staging.json", timeout_seconds=600, agent=Agent())

    assert observed.final_status == "validated"
    assert observed.diagnostic_class == "validated"
    assert observed.validation is not None
    assert observed.validation.valid is True
    assert observed.validation.missing_required_fields is None
    assert "validation_missing_required_fields" not in observed.validation.to_dict()


# ── unrelated validation paths carry no aggregate ────────────────────────────


def test_unrelated_validation_paths_carry_no_aggregate(tmp_path: Path) -> None:
    item, capture = _capture(tmp_path)

    cases: list[tuple[str, str | None]] = [
        ("missing_output", None),
        ("malformed_json", None),
        ("non_object_json", None),
        ("missing_entries", None),
        ("identity_mismatch", None),
        ("invalid_capture", None),
        ("forbidden_copied_text", None),
    ]
    for index, (diagnostic, _) in enumerate(cases):
        artifact = tmp_path / f"artifact-{index}.json"
        if diagnostic == "missing_output":
            artifact.unlink(missing_ok=True)
        elif diagnostic == "malformed_json":
            artifact.write_text("{not json", encoding="utf-8")
        elif diagnostic == "non_object_json":
            artifact.write_text('["a list"]', encoding="utf-8")
        elif diagnostic == "missing_entries":
            artifact.write_text(json.dumps({key: item[key] for key in ("source_name", "source_url")}), encoding="utf-8")
        elif diagnostic == "identity_mismatch":
            artifact.write_text(
                json.dumps({**{key: item[key] for key in ("source_name", "source_url")}, "entries": []}),
                encoding="utf-8",
            )
        elif diagnostic == "invalid_capture":
            broken_capture = tmp_path / f"capture-{index}.json"
            broken_capture.write_text("not json", encoding="utf-8")
            observation = validate_extraction_artifact(_artifact(tmp_path, item, []), item, broken_capture)
            assert observation.diagnostic_class == "invalid_capture"
            assert observation.missing_required_fields is None
            continue
        else:
            artifact = _artifact(
                tmp_path,
                item,
                [{"title": "T", "description": "forbidden copied text"}],
            )
        observation = validate_extraction_artifact(artifact, item, capture)
        assert observation.diagnostic_class == diagnostic
        assert observation.missing_required_fields is None
        assert "validation_missing_required_fields" not in observation.to_dict()


# ── backward compatibility and schema validation ────────────────────────────


def _observation(
    source_name: str = "Source",
    *,
    diagnostic_class: str = "validated",
    final_status: str = "validated",
    aggregate: dict[str, Any] | None = None,
    schema_version: str = "2.0",
    raw_extracted_count: int | None = None,
    validated_count: int | None = None,
) -> dict[str, Any]:
    default_raw = 2 if final_status == "invalid_output" else 0
    observation = {
        "observation_schema_version": schema_version,
        "source_name": source_name,
        "source_family": "media",
        "source_url": "https://example.test/source",
        "operation_id": "operation",
        "capture_sha256": "0" * 64,
        "work_item_identity": {
            "source_name": source_name,
            "source_url": "https://example.test/source",
            "operation_id": "operation",
            "capture_sha256": "0" * 64,
        },
        "mode": "production-shaped",
        "worker_count": 3,
        "repetition": 1,
        "round": 1,
        "attempt": 1,
        "timeout_ceiling_seconds": 181.0,
        "applied_timeout_ceiling_seconds": 181.0,
        "timeout_stage": None,
        "applied_timeout_threshold_seconds": None,
        "boundary_supplier": None,
        "worker_slot": 1,
        "submission_sequence": 1,
        "capture_bytes": 10,
        "capture_unicode_chars": 10,
        "generated_prompt_chars": 20,
        "queue_wait_seconds": 0.0,
        "subprocess_duration_seconds": 2.0,
        "validation_duration_seconds": 1.0,
        "collection_duration_seconds": 0.0,
        "total_duration_seconds": 3.0,
        "output_bytes": 10,
        "raw_extracted_count": default_raw if raw_extracted_count is None else raw_extracted_count,
        "validated_count": 0 if validated_count is None else validated_count,
        "exit_code": 0,
        "final_status": final_status,
        "diagnostic_class": diagnostic_class,
        "scheduled_before_budget_exhaustion": True,
        "started": True,
    }
    if schema_version == "1.0":
        for field_name in (
            "applied_timeout_ceiling_seconds",
            "timeout_stage",
            "applied_timeout_threshold_seconds",
            "boundary_supplier",
            "worker_slot",
            "submission_sequence",
            "collection_duration_seconds",
        ):
            observation.pop(field_name, None)
    if aggregate is not None:
        observation["validation_missing_required_fields"] = aggregate
    return observation


# ── strict schema validation for the sanitized aggregate ────────────────────
