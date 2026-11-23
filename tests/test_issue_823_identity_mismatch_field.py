"""Issue #823 — an identity_mismatch names the envelope field that was wrong.

In the 2026-10-15 weekly run, National Book Awards 2025 Nonfiction and the
Carnegie Medals list were refused as ``identity_mismatch`` on both attempts,
and the log could not say which of the four envelope fields was at fault.
The retained artifacts showed the cause: each one matched its work item in
every field except ``operation_id``, where the extractor had dropped or
inserted a character while retyping the 64-character hex value.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from autoscout.codex_recovery import QuarantineResult
from autoscout.codex_recovery import QuarantineStatus
from autoscout.codex_recovery import _observed_rejection
from autoscout.codex_recovery import _record_quarantine_diagnostic
from autoscout.codex_recovery import stamp_host_operation_id
from autoscout.observed_extraction import validate_extraction_artifact

# The work item's operation ID from the 2026-10-15 run, and the value the
# extractor wrote for National Book Awards (``53`` dropped after ``d07f043``).
OPERATION_ID = "d07f04353b0b08c88e9ec5efdd15cdb32ee08b1160cd46312b2770e4040a06eb"
MISCOPIED_OPERATION_ID = "d07f043b0b08c88e9ec5efdd15cdb32ee08b1160cd46312b2770e4040a06eb"
SOURCE_NAME = "National Book Awards 2025 Nonfiction"
SOURCE_URL = "https://www.nationalbook.org/2025-national-book-awards-longlist-for-nonfiction/"


def _capture(tmp_path: Path) -> tuple[Path, str]:
    capture_path = tmp_path / "capture.json"
    capture_path.write_text(
        json.dumps({"source_name": SOURCE_NAME, "source_url": SOURCE_URL, "html": "<p>list</p>"}),
        encoding="utf-8",
    )
    return capture_path, hashlib.sha256(capture_path.read_bytes()).hexdigest()


def _item(digest: str) -> dict:
    return {
        "source_name": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "operation_id": OPERATION_ID,
        "capture_sha256": digest,
    }


def _artifact(tmp_path: Path, envelope: dict) -> Path:
    path = tmp_path / "artifact.json"
    entry = {"title": "A Book", "primary_authors": ["An Author"]}
    path.write_text(json.dumps({**envelope, "entries": [entry]}), encoding="utf-8")
    return path


def test_observed_failure_names_operation_id_alone(tmp_path: Path) -> None:
    capture_path, digest = _capture(tmp_path)
    envelope = {**_item(digest), "operation_id": MISCOPIED_OPERATION_ID}

    observation = validate_extraction_artifact(_artifact(tmp_path, envelope), _item(digest), capture_path)

    assert observation.diagnostic_class == "identity_mismatch"
    assert observation.identity_mismatches == ({"field": "operation_id", "kind": "different_value_shorter"},)
    recorded = observation.to_dict()["identity_mismatches"]
    assert recorded == [{"field": "operation_id", "kind": "different_value_shorter"}]
    # The record describes the difference; it never carries either value.
    assert OPERATION_ID not in json.dumps(recorded)
    assert MISCOPIED_OPERATION_ID not in json.dumps(recorded)


@pytest.mark.parametrize(
    ("field", "make_value", "kind"),
    [
        ("source_name", lambda expected: None, "missing"),
        ("capture_sha256", lambda expected: expected.upper(), "case_changed"),
        ("capture_sha256", lambda expected: expected[:40], "truncated"),
        ("source_url", lambda expected: expected.rstrip("/").replace("https", "http"), "url_variant"),
        ("operation_id", lambda expected: "f" + expected[1:], "different_value_same_length"),
        ("source_name", lambda expected: f" {expected} ", "whitespace_changed"),
    ],
)
def test_each_mismatch_kind_is_named(tmp_path: Path, field: str, make_value, kind: str) -> None:
    capture_path, digest = _capture(tmp_path)
    item = _item(digest)
    envelope = dict(item)
    value = make_value(item[field])
    if value is None:
        del envelope[field]
    else:
        envelope[field] = value

    observation = validate_extraction_artifact(_artifact(tmp_path, envelope), item, capture_path)

    assert observation.diagnostic_class == "identity_mismatch"
    assert observation.identity_mismatches == ({"field": field, "kind": kind},)


def test_quarantine_record_in_workflow_log_names_the_field(tmp_path: Path) -> None:
    capture_path, digest = _capture(tmp_path)
    item = _item(digest)
    artifact = _artifact(tmp_path, {**item, "operation_id": MISCOPIED_OPERATION_ID})

    observed_class, observed_count, observed_mismatches = _observed_rejection(artifact, item, capture_path)
    diagnostic = _record_quarantine_diagnostic(
        item,
        QuarantineResult(QuarantineStatus.PRESERVED, "/tmp/quarantine/artifact.rejected-1"),
        1,
        diagnostic_class=observed_class,
        raw_extracted_count=observed_count,
        identity_mismatches=observed_mismatches,
    )

    assert diagnostic["diagnostic_class"] == "identity_mismatch"
    assert diagnostic["identity_mismatches"] == [{"field": "operation_id", "kind": "different_value_shorter"}]


def test_host_stamped_operation_id_lets_the_miscopied_list_validate(tmp_path: Path) -> None:
    """The cause: the host, not the extractor, supplies the operation ID."""
    capture_path, digest = _capture(tmp_path)
    item = _item(digest)
    payload = {**item, "operation_id": MISCOPIED_OPERATION_ID, "entries": []}

    stamp_host_operation_id(payload, item)

    assert payload["operation_id"] == OPERATION_ID
    artifact = _artifact(tmp_path, {key: value for key, value in payload.items() if key != "entries"})
    observation = validate_extraction_artifact(artifact, item, capture_path)
    assert observation.diagnostic_class != "identity_mismatch"
    assert observation.identity_mismatches == ()


def test_host_stamp_leaves_the_copied_fields_to_the_extractor() -> None:
    payload = {"source_name": "Wrong", "operation_id": "x"}

    stamp_host_operation_id(payload, {"source_name": SOURCE_NAME, "operation_id": OPERATION_ID})

    assert payload == {"source_name": "Wrong", "operation_id": OPERATION_ID}
