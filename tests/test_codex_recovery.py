"""Tests for Codex recovery and resumability (issue #267)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autoscout.codex_recovery import MAX_CAPTURE_RETRIES
from autoscout.codex_recovery import CodexCliExtractionAgent
from autoscout.codex_recovery import CodexResumeAdapter
from autoscout.codex_recovery import ExtractionAttempt
from autoscout.codex_recovery import ExtractionStatus
from autoscout.codex_recovery import ProductionCodexAdapter
from autoscout.codex_recovery import ProductionCodexCaptureAdapter
from autoscout.codex_recovery import QuarantineResult
from autoscout.codex_recovery import QuarantineStatus
from autoscout.codex_recovery import _artifact_exact_list_metadata_failure_status
from autoscout.codex_recovery import _quarantine_artifact
from autoscout.codex_recovery import _read_resume_marker
from autoscout.codex_recovery import _write_resume_marker
from autoscout.codex_recovery import build_codex_extraction_prompt
from autoscout.helper_metadata import HELPER_METADATA_FIELDS


class _UnavailableAgent:
    def extract(self, item, capture_path, artifact_path):
        return ExtractionAttempt(ExtractionStatus.UNAVAILABLE, False, diagnostic="test_agent_disabled")


def _production_adapter(output_dir: Path) -> ProductionCodexAdapter:
    return ProductionCodexAdapter(agent=_UnavailableAgent())


def _write_worklist(output_dir: Path, items: list[dict]) -> None:
    for index, item in enumerate(items):
        capture_file = item.setdefault("capture_file", f"capture-{index}.json")
        capture_path = output_dir / capture_file
        if not capture_path.exists():
            capture_path.write_text(
                json.dumps(
                    {
                        "source_url": item.get("source_url", ""),
                        "html": '<a href="https://example.test/book">Book</a>',
                    }
                ),
                encoding="utf-8",
            )
    worklist_path = output_dir / "codex-extraction-worklist.json"
    worklist_path.write_text(json.dumps({"run_date": "2026-07-13", "work_items": items}), encoding="utf-8")


def _write_valid_extraction(
    output_dir: Path,
    filename: str,
    source_name: str = "Test Source",
    source_url: str = "https://example.test/source",
) -> None:
    worklist = json.loads((output_dir / "codex-extraction-worklist.json").read_text(encoding="utf-8"))
    item = next(item for item in worklist["work_items"] if item["expected_extraction_file"] == filename)
    capture_path = output_dir / item["capture_file"]
    import hashlib

    item.setdefault("operation_id", "test-operation")
    item.setdefault("capture_sha256", hashlib.sha256(capture_path.read_bytes()).hexdigest())
    (output_dir / "codex-extraction-worklist.json").write_text(json.dumps(worklist), encoding="utf-8")
    (output_dir / filename).write_text(
        json.dumps(
            {
                "source_name": source_name,
                "source_url": source_url,
                "operation_id": item["operation_id"],
                "capture_sha256": item["capture_sha256"],
                "entries": [
                    {
                        "title": "Test Book",
                        "primary_authors": ["Test Author"],
                        "source_url": "https://example.test/book",
                        "evidence_url": "https://example.test/book",
                        "evidence_note": "Test note.",
                        "candidate_category": "popular science",
                        "signal_type": "award shortlist",
                        "signal_strength": "shortlist",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _exact_list_extraction_payload(item: dict, signal_type: str) -> dict:
    return {
        "source_name": item["source_name"],
        "source_url": item["source_url"],
        "operation_id": item["operation_id"],
        "capture_sha256": item["capture_sha256"],
        "entries": [
            {
                "title": "The Shape of Time",
                "primary_authors": ["Mira Bell"],
                "source_url": item["source_url"],
                "evidence_url": "https://example.test/book",
                "evidence_note": "Award longlist entry.",
                "candidate_category": "adult nonfiction",
                "english_title": "The Shape of Time",
                "search_translation": "Форма времени",
                "author_transliteration_note": "Мира Белл",
                "signal_type": signal_type,
                "signal_strength": "longlist",
            }
        ],
    }


def _write_exact_list_extraction(output_dir: Path, filename: str, signal_type: str) -> dict:
    _write_worklist(
        output_dir,
        [
            {
                "source_name": "Magnesia Litera",
                "source_family": "award",
                "source_url": "https://example.test/source",
                "expected_extraction_file": filename,
                "source_metadata": {
                    "family": "award",
                    "signal_family": "award",
                    "language": "en",
                    "metadata_exact_book_list": True,
                    "expected_signal_strength": "longlist",
                    "eligible_candidate_category": "adult nonfiction",
                },
            }
        ],
    )
    worklist_path = output_dir / "codex-extraction-worklist.json"
    worklist = json.loads(worklist_path.read_text(encoding="utf-8"))
    item = worklist["work_items"][0]
    capture_path = output_dir / item["capture_file"]
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    capture["source_page"] = item["source_url"]
    capture_path.write_text(json.dumps(capture), encoding="utf-8")
    item["operation_id"] = "current-operation"
    item["capture_sha256"] = hashlib.sha256(capture_path.read_bytes()).hexdigest()
    worklist_path.write_text(json.dumps(worklist), encoding="utf-8")
    (output_dir / filename).write_text(
        json.dumps(_exact_list_extraction_payload(item, signal_type)),
        encoding="utf-8",
    )
    return item


def _write_invalid_extraction(output_dir: Path, filename: str) -> None:
    (output_dir / filename).write_text(
        json.dumps(
            {
                "source_name": "Test Source",
                "source_url": "https://example.test/source",
                "entries": [
                    {
                        "title": "",
                        "primary_authors": ["Test Author"],
                        "evidence_url": "not-a-real-url",
                        "candidate_category": "popular science",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_malformed_extraction(output_dir: Path, filename: str) -> None:
    (output_dir / filename).write_text("not valid json", encoding="utf-8")


class CodexCliExtractionAgentTests(unittest.TestCase):
    def test_command_uses_isolated_read_only_non_repo_and_python_writes_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "capture.json"
            artifact = root / "artifact.json"
            capture.write_text("{}", encoding="utf-8")
            payload = {"source_name": "Source", "entries": []}

            def fake_run_codex(command, isolated, response_path, bounded_timeout):
                # Verify command invariants.
                self.assertIn("--skip-git-repo-check", command)
                self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
                self.assertNotEqual(Path(command[command.index("-C") + 1]), root)
                # Write the response (emulating what Codex would do).
                response_path.write_text(json.dumps(payload), encoding="utf-8")
                return (0, "")

            with mock.patch.object(CodexCliExtractionAgent, "_run_codex", side_effect=fake_run_codex):
                result = CodexCliExtractionAgent(timeout_seconds=1).extract({}, capture, artifact)

            self.assertEqual(result.status, ExtractionStatus.PRODUCED)
            self.assertEqual(json.loads(artifact.read_text(encoding="utf-8")), payload)

    def test_extract_with_timeout_uses_exact_command_with_the_pinned_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "capture.json"
            artifact = root / "artifact.json"
            capture.write_text("{}", encoding="utf-8")
            item = {"source_name": "Source", "source_url": "https://example.test/source"}
            commands = []
            profiles = [("fast", 180.0, "low"), ("around-400-logical-seconds", 400.0, "high")]
            agent = CodexCliExtractionAgent(timeout_seconds=400)

            def fake_run_codex(command, isolated, response_path, bounded_timeout):
                del isolated, bounded_timeout
                commands.append(list(command))
                response_path.write_text(json.dumps({"entries": []}), encoding="utf-8")
                label, _timeout, effort = profiles[len(commands) - 1]
                agent._record_effective_profile("model", f"simulated-{label}")
                agent._record_effective_profile("reasoning_effort", effort)
                return (0, f"model: simulated-{label}\nreasoning effort: {effort}\n")

            with mock.patch.object(CodexCliExtractionAgent, "_run_codex", side_effect=fake_run_codex):
                for label, timeout, _effort in profiles:
                    result = agent.extract_with_timeout(item, capture, artifact, timeout_seconds=timeout)
                    self.assertEqual(result.status, ExtractionStatus.PRODUCED)
                    profile = agent.effective_model_profile_snapshot()
                    self.assertIsNotNone(profile)
                    assert profile is not None
                    self.assertEqual(profile["model"], f"simulated-{label}")

            self.assertEqual(len(commands), 2)
            expected_prompt = build_codex_extraction_prompt(item)
            for command in commands:
                # Literal tripwire. The feature flags and the model pin are
                # written out rather than derived from their constants, so a
                # change to either has to be made here on purpose. The editor
                # decided on --disable memories in issue #414. Issue #731
                # replaced the issue #411 rule that no model or reasoning
                # override reaches the command: extraction now passes the
                # repository pin from configs/codex-model-pin.json, and a pin change
                # is a reviewed commit that updates this list. No profile
                # override reaches the command.
                self.assertEqual(
                    command,
                    [
                        "codex",
                        "exec",
                        "--sandbox",
                        "read-only",
                        "--skip-git-repo-check",
                        "--disable",
                        "memories",
                        "-m",
                        "gpt-5.6-luna",
                        "-c",
                        'model_reasoning_effort="medium"',
                        "--output-last-message",
                        command[12],
                        "-C",
                        command[14],
                        expected_prompt,
                    ],
                )
                self.assertNotIn("--model", command)
                self.assertNotIn("--profile", command)
                self.assertNotIn("--reasoning", command)
                self.assertNotIn("--enable", command)

    def test_effective_profile_redacts_credentials_and_is_bounded(self):
        agent = CodexCliExtractionAgent()
        secret = "sk-" + "a" * 80
        agent._record_effective_profile("model", secret)
        agent._record_effective_profile("reasoning_effort", "x" * 500)

        profile = agent.effective_model_profile_snapshot()
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertNotIn(secret, profile["model"])
        self.assertLessEqual(len(profile["model"]), 128)
        self.assertLessEqual(len(profile["reasoning_effort"]), 128)

    def test_progress_transport_factory_creates_and_closes_one_transport_per_run(self):
        created = []

        class Transport:
            def __init__(self):
                self.closed = False

            def drain(self, lease, now):
                del lease, now

            def close(self):
                self.closed = True

        def factory():
            transport = Transport()
            created.append(transport)
            return transport

        class Process:
            pid = 12345
            returncode = 0

            def __init__(self, command, **kwargs):
                del command, kwargs

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                del timeout
                return self.returncode

        agent = CodexCliExtractionAgent(progress_transport_factory=factory)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("autoscout.codex_recovery.subprocess.Popen", Process):
                agent._run_codex(["codex"], root, root / "response.json", 1.0)
                agent._run_codex(["codex"], root, root / "response.json", 1.0)

        self.assertEqual(len(created), 2)
        self.assertIsNot(created[0], created[1])
        self.assertTrue(all(transport.closed for transport in created))

    def test_exact_list_prompt_separates_signal_type_and_strength(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "capture.json"
            artifact = root / "artifact.json"
            capture.write_text("{}", encoding="utf-8")

            def fake_run_codex(command, isolated, response_path, bounded_timeout):
                prompt = command[-1]
                self.assertIn('Set signal_type exactly to "award"', prompt)
                self.assertIn('Set signal_strength exactly to "longlist"', prompt)
                self.assertIn("Do not combine signal_type and signal_strength", prompt)
                response_path.write_text(json.dumps({"entries": []}), encoding="utf-8")
                return (0, "")

            item = {
                "source_family": "award",
                "source_metadata": {
                    "family": "award",
                    "signal_family": "award",
                    "language": "en",
                    "metadata_exact_book_list": True,
                    "expected_signal_strength": "longlist",
                    "eligible_candidate_category": "adult nonfiction",
                },
            }
            with mock.patch.object(CodexCliExtractionAgent, "_run_codex", side_effect=fake_run_codex):
                result = CodexCliExtractionAgent(timeout_seconds=1).extract(item, capture, artifact)

            self.assertEqual(result.status, ExtractionStatus.PRODUCED)


class CodexAdapterTests(unittest.TestCase):
    def test_missing_worklist_returns_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            adapter = _production_adapter(output_dir)
            result = adapter.fulfill(output_dir)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["reason"], "missing_worklist")

    def test_empty_worklist_returns_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            _write_worklist(output_dir, [])
            adapter = _production_adapter(output_dir)
            result = adapter.fulfill(output_dir)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["reason"], "empty_worklist")

    def test_valid_extraction_artifacts_count_as_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            filename = "test-source-1befd3ea-codex-extraction.json"
            items = [
                {
                    "source_name": "Test Source",
                    "source_url": "https://example.test/source",
                    "expected_extraction_file": filename,
                }
            ]
            _write_worklist(output_dir, items)
            _write_valid_extraction(output_dir, filename)

            adapter = _production_adapter(output_dir)
            result = adapter.fulfill(output_dir)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["ready_items"], 1)
            self.assertEqual(result["total_items"], 1)

    def test_exact_list_invalid_signal_metadata_is_retried(self):
        class CorrectingAgent:
            calls = 0

            def extract(self, item, capture_path, artifact_path):
                self.calls += 1
                artifact_path.write_text(
                    json.dumps(
                        {
                            "source_name": item["source_name"],
                            "source_url": item["source_url"],
                            "operation_id": item["operation_id"],
                            "capture_sha256": item["capture_sha256"],
                            "entries": [
                                {
                                    "title": "The Shape of Time",
                                    "primary_authors": ["Mira Bell"],
                                    "source_url": item["source_url"],
                                    "evidence_url": "https://example.test/book",
                                    "evidence_note": "Award longlist entry.",
                                    "candidate_category": "adult nonfiction",
                                    "english_title": "The Shape of Time",
                                    "search_translation": "Форма времени",
                                    "author_transliteration_note": "Мира Белл",
                                    "signal_type": "award",
                                    "signal_strength": "longlist",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                return ExtractionAttempt(ExtractionStatus.PRODUCED, True, 0)

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            filename = "magnesia-codex-extraction.json"
            _write_exact_list_extraction(output_dir, filename, "award_longlist")

            agent = CorrectingAgent()
            result = ProductionCodexAdapter(agent=agent).fulfill(output_dir)

            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(result["ready_items"], 1)
            self.assertEqual(agent.calls, 1)
            artifact = json.loads((output_dir / filename).read_text(encoding="utf-8"))
            self.assertEqual(artifact["entries"][0]["signal_type"], "award")
            self.assertEqual(artifact["entries"][0]["signal_strength"], "longlist")

    def test_strict_english_chart_recovery_rejects_any_helper_field_by_presence(self):
        chart_metadata = {
            "audible_explicit_chart": {
                "source_family": "audiobook ranking",
                "signal_family": "audiobook ranking",
            },
            "amazon_charts_weekly": {
                "source_family": "bestseller/ranking",
                "signal_family": "bestseller/ranking",
            },
        }
        invalid_values = dict(zip(HELPER_METADATA_FIELDS, ("", None, {"invalid": True}), strict=True))

        for chart_field, metadata in chart_metadata.items():
            with self.subTest(chart_field=chart_field):
                item = {
                    "source_family": metadata["source_family"],
                    "source_metadata": {
                        "metadata_exact_book_list": True,
                        "language": "en",
                        chart_field: True,
                        "signal_family": metadata["signal_family"],
                        "expected_signal_strength": "top ranking",
                        "eligible_candidate_category": "adult nonfiction",
                    },
                }
                entry = {
                    "title": "The Shape of Time",
                    "signal_type": metadata["signal_family"],
                    "signal_strength": "top ranking",
                    "candidate_category": "adult nonfiction",
                }
                with tempfile.TemporaryDirectory() as tmp:
                    artifact = Path(tmp) / "extraction.json"
                    artifact.write_text(json.dumps({"entries": [entry]}), encoding="utf-8")
                    helper_free_bytes = artifact.read_bytes()

                    self.assertEqual(_artifact_exact_list_metadata_failure_status(artifact, item), "")
                    self.assertEqual(artifact.read_bytes(), helper_free_bytes)

                    for field, value in invalid_values.items():
                        with self.subTest(field=field, value=value):
                            invalid_entry = {**entry, field: value}
                            artifact.write_text(json.dumps({"entries": [invalid_entry]}), encoding="utf-8")
                            before = artifact.read_bytes()

                            self.assertEqual(
                                _artifact_exact_list_metadata_failure_status(artifact, item),
                                "invalid_helper_metadata",
                            )
                            self.assertEqual(artifact.read_bytes(), before)

    def test_persistent_exact_list_signal_mismatch_keeps_specific_diagnostic(self):
        class PersistentlyInvalidAgent:
            calls = 0

            def extract(self, item, capture_path, artifact_path):
                self.calls += 1
                artifact_path.write_text(
                    json.dumps(_exact_list_extraction_payload(item, "award_longlist")),
                    encoding="utf-8",
                )
                return ExtractionAttempt(ExtractionStatus.PRODUCED, True, 0)

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            filename = "magnesia-codex-extraction.json"
            _write_exact_list_extraction(output_dir, filename, "award_longlist")
            agent = PersistentlyInvalidAgent()

            result = ProductionCodexAdapter(
                agent=agent,
                max_attempts=2,
                per_attempt_timeout_seconds=180,
            ).fulfill(output_dir)

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["reason"], "no_extraction_produced")
            self.assertEqual(agent.calls, 2)
            self.assertEqual(
                [
                    diagnostic["status"]
                    for diagnostic in result["diagnostics"]
                    if diagnostic.get("attempt") in {1, 2}
                    and not str(diagnostic.get("status", "")).startswith("rejected_artifact_")
                ],
                ["invalid_signal_metadata", "invalid_signal_metadata"],
            )

    def test_empty_extraction_is_healthy_when_capture_identity_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            filename = "empty-extraction.json"
            _write_worklist(
                output_dir,
                [
                    {
                        "source_name": "Test Source",
                        "source_url": "https://example.test/source",
                        "expected_extraction_file": filename,
                    }
                ],
            )
            _write_valid_extraction(output_dir, filename)
            artifact = json.loads((output_dir / filename).read_text(encoding="utf-8"))
            artifact["entries"] = []
            (output_dir / filename).write_text(json.dumps(artifact), encoding="utf-8")

            result = _production_adapter(output_dir).fulfill(output_dir)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["ready_items"], 1)

    def test_missing_extraction_artifact_counted_as_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            items = [
                {
                    "source_name": "Test Source",
                    "source_url": "https://example.test/source",
                    "expected_extraction_file": "test-source-codex-extraction.json",
                }
            ]
            _write_worklist(output_dir, items)

            adapter = _production_adapter(output_dir)
            result = adapter.fulfill(output_dir)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["reason"], "no_extraction_produced")
            self.assertEqual(result["skipped_items"], 1)

    def test_invalid_extraction_counted_as_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            filename = "test-source-1befd3ea-codex-extraction.json"
            items = [
                {
                    "source_name": "Test Source",
                    "source_url": "https://example.test/source",
                    "expected_extraction_file": filename,
                }
            ]
            _write_worklist(output_dir, items)
            _write_invalid_extraction(output_dir, filename)

            adapter = _production_adapter(output_dir)
            result = adapter.fulfill(output_dir)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["reason"], "no_extraction_produced")
            self.assertEqual(result["failed_items"], 1)

    def test_malformed_json_extraction_counted_as_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            filename = "test-source-1befd3ea-codex-extraction.json"
            items = [
                {
                    "source_name": "Test Source",
                    "source_url": "https://example.test/source",
                    "expected_extraction_file": filename,
                }
            ]
            _write_worklist(output_dir, items)
            _write_malformed_extraction(output_dir, filename)

            adapter = _production_adapter(output_dir)
            result = adapter.fulfill(output_dir)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["reason"], "no_extraction_produced")
            self.assertEqual(result["failed_items"], 1)

    def test_mixed_ready_failed_skipped_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            good_file = "good-source-codex-extraction.json"
            bad_file = "bad-source-codex-extraction.json"
            items = [
                {
                    "source_name": "Good",
                    "source_url": "https://example.test/good",
                    "expected_extraction_file": good_file,
                },
                {
                    "source_name": "Bad",
                    "source_url": "https://example.test/bad",
                    "expected_extraction_file": bad_file,
                },
                {
                    "source_name": "Missing",
                    "source_url": "https://example.test/missing",
                    "expected_extraction_file": "missing-extraction.json",
                },
            ]
            _write_worklist(output_dir, items)
            _write_valid_extraction(output_dir, good_file, "Good", "https://example.test/good")
            _write_malformed_extraction(output_dir, bad_file)

            adapter = _production_adapter(output_dir)
            result = adapter.fulfill(output_dir)
            self.assertEqual(result["ready_items"], 1)
            self.assertEqual(result["failed_items"], 1)
            self.assertEqual(result["skipped_items"], 1)
            self.assertEqual(result["total_items"], 3)

    def test_forbidden_field_on_entry_is_rejected(self):
        """Forbidden copied-text fields on an individual entry must be
        rejected even when absent from the top-level artifact object."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            filename = "test-source-1befd3ea-codex-extraction.json"
            items = [
                {
                    "source_name": "Test Source",
                    "source_url": "https://example.test/source",
                    "expected_extraction_file": filename,
                }
            ]
            _write_worklist(output_dir, items)
            (output_dir / filename).write_text(
                json.dumps(
                    {
                        "source_name": "Test Source",
                        "source_url": "https://example.test/source",
                        "entries": [
                            {
                                "title": "Test Book",
                                "primary_authors": ["Test Author"],
                                "evidence_url": "https://example.test/book",
                                "evidence_note": "Test note.",
                                "candidate_category": "popular science",
                                "signal_type": "award shortlist",
                                "signal_strength": "shortlist",
                                "full_review": "copied review text that must never be ingested",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            adapter = _production_adapter(output_dir)
            result = adapter.fulfill(output_dir)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["failed_items"], 1)

    def test_evidence_url_not_on_captured_page_is_rejected(self):
        """When capture data is available, evidence URLs must be verified
        against the captured page rather than trusted on scheme alone."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            filename = "test-source-1befd3ea-codex-extraction.json"
            capture_filename = "codex-capture-test-source.json"
            items = [
                {
                    "source_name": "Test Source",
                    "source_url": "https://example.test/source",
                    "capture_file": capture_filename,
                    "expected_extraction_file": filename,
                }
            ]
            _write_worklist(output_dir, items)
            (output_dir / capture_filename).write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/source",
                        "html": '<html><body><a href="https://example.test/real-book">Real Book</a></body></html>',
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / filename).write_text(
                json.dumps(
                    {
                        "source_name": "Test Source",
                        "source_url": "https://example.test/source",
                        "entries": [
                            {
                                "title": "Invented Book",
                                "primary_authors": ["Test Author"],
                                "evidence_url": "https://example.test/invented-book",
                                "evidence_note": "Test note.",
                                "candidate_category": "popular science",
                                "signal_type": "award shortlist",
                                "signal_strength": "shortlist",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            adapter = _production_adapter(output_dir)
            result = adapter.fulfill(output_dir)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["failed_items"], 1)

    def test_injected_agent_completes_production_handoff_in_one_fulfill_call(self):
        class FakeAgent:
            calls = 0

            def extract(self, item, capture_path, artifact_path):
                self.calls += 1
                artifact_path.write_text(
                    json.dumps(
                        {
                            "source_name": item["source_name"],
                            "source_url": item["source_url"],
                            "operation_id": item["operation_id"],
                            "capture_sha256": item["capture_sha256"],
                            "entries": [
                                {
                                    "title": "Book",
                                    "primary_authors": ["Author"],
                                    "source_url": "https://example.test/book",
                                    "evidence_url": "https://example.test/book",
                                    "evidence_note": "Listed book.",
                                    "candidate_category": "popular science",
                                    "signal_type": "award shortlist",
                                    "signal_strength": "shortlist",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                return ExtractionAttempt(ExtractionStatus.PRODUCED, True, 0)

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            filename = "fresh.json"
            _write_worklist(
                output_dir,
                [
                    {
                        "source_name": "Test Source",
                        "source_url": "https://example.test/source",
                        "expected_extraction_file": filename,
                    }
                ],
            )
            worklist = json.loads((output_dir / "codex-extraction-worklist.json").read_text(encoding="utf-8"))
            item = worklist["work_items"][0]
            import hashlib

            item["operation_id"] = "current-operation"
            item["capture_sha256"] = hashlib.sha256((output_dir / item["capture_file"]).read_bytes()).hexdigest()
            (output_dir / "codex-extraction-worklist.json").write_text(json.dumps(worklist), encoding="utf-8")

            agent = FakeAgent()
            result = ProductionCodexAdapter(agent=agent).fulfill(output_dir)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(agent.calls, 1)
            self.assertTrue((output_dir / filename).exists())
            self.assertEqual(result["diagnostics"][0]["status"], "validated")

    def test_quarantine_preservation_failure_forces_intervention(self):
        """Even when one source has a valid extraction, if quarantine fails
        the rejected artifact must stay untouched on disk — never deleted, never
        overwritten by extraction — and fulfill must return 'failed' with
        reason 'quarantine_preservation_failed'. The path must be reported so
        the operator can inspect it."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            good_file = "good-extraction.json"
            bad_file = "bad-extraction.json"
            items = [
                {
                    "source_name": "Good Source",
                    "source_url": "https://example.test/good",
                    "expected_extraction_file": good_file,
                },
                {
                    "source_name": "Bad Source",
                    "source_url": "https://example.test/bad",
                    "expected_extraction_file": bad_file,
                },
            ]
            _write_worklist(output_dir, items)
            _write_invalid_extraction(output_dir, bad_file)
            _write_valid_extraction(output_dir, good_file, "Good Source", "https://example.test/good")

            original_bad_bytes = (output_dir / bad_file).read_bytes()

            adapter = ProductionCodexAdapter(
                agent=_UnavailableAgent(),
            )

            original_quarantine = _quarantine_artifact

            def quarantine_side_effect(path):
                if path.name == bad_file:
                    return QuarantineResult(
                        QuarantineStatus.PRESERVATION_FAILED,
                        path=str(path),
                        diagnostic="PermissionError",
                    )
                return original_quarantine(path)

            # Simulate quarantine returning PRESERVATION_FAILED. Never
            # mock Path.unlink — the production code must not call it.
            with mock.patch(
                "autoscout.codex_recovery._quarantine_artifact",
                side_effect=quarantine_side_effect,
            ):
                result = adapter.fulfill(output_dir)

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["reason"], "quarantine_preservation_failed")
            self.assertIn("failed_quarantine_paths", result)
            self.assertIn(str(output_dir / bad_file), result["failed_quarantine_paths"])
            # Artifact must still exist with its exact original bytes.
            self.assertTrue((output_dir / bad_file).exists())
            self.assertEqual((output_dir / bad_file).read_bytes(), original_bad_bytes)
            # No extraction attempt was made — the _UnavailableAgent
            # was never invoked, so the file was never overwritten.
            extraction_diags = [
                d for d in result.get("diagnostics", []) if d.get("status") in ("unavailable", "validated")
            ]
            self.assertFalse(extraction_diags, "Extraction was attempted after preservation failure")

    def test_capture_adapter_returns_success_for_valid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-13",
                        "sources": [
                            {
                                "name": "Test Source",
                                "family": "award",
                                "url": "https://example.test/source",
                                "access_status": "open",
                                "extraction_method": "codex-extraction-replay",
                                "check_frequency": "weekly",
                                "source_page_fixture_file": "test-page.json",
                                "extraction_response_file": "test-extraction.json",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output_dir = tmp_path / "run"
            output_dir.mkdir()

            adapter = ProductionCodexCaptureAdapter()
            result = adapter.prepare(config_path, output_dir, live=False)
            self.assertEqual(result["returncode"], 0)

    def test_max_retry_constants_are_positive(self):
        self.assertGreater(MAX_CAPTURE_RETRIES, 0)
        self.assertLessEqual(MAX_CAPTURE_RETRIES, 5)


class CodexResumeTests(unittest.TestCase):
    def test_can_resume_returns_false_without_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            adapter = CodexResumeAdapter(output_dir)
            self.assertFalse(adapter.can_resume(output_dir))

    def test_can_resume_returns_true_with_valid_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            _write_resume_marker(
                output_dir,
                {
                    "run_id": "test-run",
                    "completed_stages": ["capture"],
                    "validated_artifacts": [],
                },
            )
            _write_worklist(output_dir, [])
            adapter = CodexResumeAdapter(output_dir)
            self.assertTrue(adapter.can_resume(output_dir))

    def test_can_resume_returns_false_without_capture_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            _write_resume_marker(
                output_dir,
                {
                    "run_id": "test-run",
                    "completed_stages": [],
                    "validated_artifacts": [],
                },
            )
            adapter = CodexResumeAdapter(output_dir)
            self.assertFalse(adapter.can_resume(output_dir))

    def test_resume_worklist_marks_completed_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            filename = "test-source-codex-extraction.json"
            _write_worklist(
                output_dir,
                [
                    {
                        "source_name": "Already Done",
                        "source_url": "https://example.test/done",
                        "capture_file": "capture-done.json",
                        "expected_extraction_file": filename,
                    },
                    {
                        "source_name": "Still Pending",
                        "source_url": "https://example.test/pending",
                        "capture_file": "capture-pending.json",
                        "expected_extraction_file": "pending-extraction.json",
                    },
                ],
            )
            _write_valid_extraction(output_dir, filename, "Already Done", "https://example.test/done")
            _write_resume_marker(
                output_dir,
                {
                    "run_id": "test-run",
                    "completed_stages": ["capture"],
                    "validated_artifacts": [
                        {
                            "source_url": "https://example.test/done",
                            "expected_extraction_file": filename,
                            "capture_file": "capture-done.json",
                            "run_id": "test-run",
                        }
                    ],
                },
            )

            adapter = CodexResumeAdapter(output_dir)
            items = adapter.resume_worklist(output_dir, "test-run")

            self.assertEqual(len(items), 2)
            self.assertEqual(items[0].status, "completed")
            self.assertEqual(items[1].status, "pending")

    def test_mark_stage_complete_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            adapter = CodexResumeAdapter(output_dir)
            adapter.mark_stage_complete(output_dir, "capture", "run-1")
            adapter.mark_stage_complete(output_dir, "codex", "run-1")

            marker = _read_resume_marker(output_dir)
            self.assertIn("capture", marker["completed_stages"])
            self.assertIn("codex", marker["completed_stages"])
            self.assertEqual(marker["run_id"], "run-1")

    def test_mark_artifact_validated_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            artifact = output_dir / "test-artifact.json"
            artifact.write_bytes(b"validated bytes")
            _write_resume_marker(output_dir, {"run_id": "run-1", "completed_stages": [], "validated_artifacts": []})

            adapter = CodexResumeAdapter(output_dir)
            adapter.mark_artifact_validated(
                output_dir,
                "https://example.test/source",
                artifact.name,
                "test-capture.json",
                "run-1",
            )

            marker = _read_resume_marker(output_dir)
            validated = marker["validated_artifacts"]
            self.assertEqual(len(validated), 1)
            self.assertEqual(validated[0]["source_url"], "https://example.test/source")
            self.assertEqual(validated[0]["expected_extraction_file"], artifact.name)
            self.assertEqual(validated[0]["run_id"], "run-1")
            self.assertEqual(validated[0]["artifact_sha256"], hashlib.sha256(artifact.read_bytes()).hexdigest())

    def test_new_operation_rotates_resume_identity_but_keeps_artifact_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            prior_evidence = {"run_id": "run-1", "artifact_sha256": "abc"}
            _write_resume_marker(
                output_dir,
                {
                    "run_id": "run-1",
                    "completed_stages": ["capture", "codex"],
                    "validated_artifacts": [prior_evidence],
                },
            )

            CodexResumeAdapter(output_dir).mark_stage_complete(output_dir, "capture", "run-2")

            marker = _read_resume_marker(output_dir)
            self.assertEqual(marker["run_id"], "run-2")
            self.assertEqual(marker["completed_stages"], ["capture"])
            self.assertEqual(marker["validated_artifacts"], [prior_evidence])

    def test_validation_evidence_is_not_written_for_mismatched_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            (output_dir / "artifact.json").write_text("{}", encoding="utf-8")
            _write_resume_marker(
                output_dir, {"run_id": "run-2", "completed_stages": ["capture"], "validated_artifacts": []}
            )

            CodexResumeAdapter(output_dir).mark_artifact_validated(
                output_dir, "https://example.test/source", "artifact.json", "capture.json", "run-1"
            )

            self.assertEqual(_read_resume_marker(output_dir)["validated_artifacts"], [])
