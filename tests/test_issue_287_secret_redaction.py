"""Tests for issue #287: credential-shaped secrets must never survive into
persisted diagnostics or canonical run artifacts.

Only synthetic, obviously-fake values are used below (e.g. ``figd_`` +
``FAKE...`` suffixes). No realistic-looking real credential appears in this
file.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autoscout.codex_recovery import CodexCliExtractionAgent
from autoscout.codex_recovery import ExtractionStatus
from autoscout.secret_redaction import contains_secret
from autoscout.secret_redaction import redact_secrets
from autoscout.secret_redaction import redact_structure
from autoscout.weekly_workflow import WorkflowOutcome
from autoscout.weekly_workflow import deterministic_adapters
from autoscout.weekly_workflow import run_weekly_workflow

# Synthetic, obviously-fake token fixtures. None of these are real credentials.
FAKE_FIGMA_PAT = "figd_" + ("FAKE0000" * 5) + "UNIQSUFFIXTAIL99"
FAKE_GITHUB_TOKEN = "ghp_" + ("FAKE1234" * 5)
FAKE_GITHUB_PAT = "github_pat_" + ("FAKE1234" * 4)
FAKE_OPENAI_KEY = "sk-" + ("FAKE1234" * 4)
FAKE_AWS_KEY = "AKIA" + "FAKE0000FAKE0000"
FAKE_SLACK_TOKEN = "xoxb-" + "FAKE0001112223"
FAKE_BEARER_VALUE = "FAKE" + ("BEARERTOKEN0000" * 2)


class RedactSecretsPatternTests(unittest.TestCase):
    def test_figma_pat_redacted_with_context_preserved(self):
        text = f"figma config note: {FAKE_FIGMA_PAT} was pasted by mistake"
        result = redact_secrets(text)
        self.assertNotIn(FAKE_FIGMA_PAT, result)
        self.assertNotIn("figd_", result)
        self.assertIn("[REDACTED:figma_pat]", result)
        self.assertTrue(result.startswith("figma config note: "))
        self.assertTrue(result.endswith(" was pasted by mistake"))

    def test_github_token_redacted(self):
        text = f"auth failed using {FAKE_GITHUB_TOKEN} for the push"
        result = redact_secrets(text)
        self.assertNotIn(FAKE_GITHUB_TOKEN, result)
        self.assertIn("[REDACTED:github_token]", result)
        self.assertTrue(result.startswith("auth failed using "))

    def test_github_pat_redacted(self):
        text = f"stored credential {FAKE_GITHUB_PAT} in config"
        result = redact_secrets(text)
        self.assertNotIn(FAKE_GITHUB_PAT, result)
        self.assertIn("[REDACTED:github_pat]", result)

    def test_openai_key_redacted(self):
        text = f"model call failed: {FAKE_OPENAI_KEY} rejected"
        result = redact_secrets(text)
        self.assertNotIn(FAKE_OPENAI_KEY, result)
        self.assertIn("[REDACTED:openai_key]", result)

    def test_aws_access_key_id_redacted(self):
        text = f"aws error for key id {FAKE_AWS_KEY} during upload"
        result = redact_secrets(text)
        self.assertNotIn(FAKE_AWS_KEY, result)
        self.assertIn("[REDACTED:aws_access_key_id]", result)

    def test_slack_token_redacted(self):
        text = f"slack webhook used {FAKE_SLACK_TOKEN} unexpectedly"
        result = redact_secrets(text)
        self.assertNotIn(FAKE_SLACK_TOKEN, result)
        self.assertIn("[REDACTED:slack_token]", result)

    def test_bearer_token_redacted_prefix_preserved(self):
        text = f"Authorization: Bearer {FAKE_BEARER_VALUE} sent to upstream"
        result = redact_secrets(text)
        self.assertNotIn(FAKE_BEARER_VALUE, result)
        self.assertIn("Authorization: Bearer [REDACTED:bearer_token]", result)
        self.assertTrue(result.endswith(" sent to upstream"))

    def test_credential_assignment_style_redacted_key_preserved(self):
        text = "config dump: token=FAKEVALUEFAKEVALUE0000 while debugging"
        result = redact_secrets(text)
        self.assertNotIn("FAKEVALUEFAKEVALUE0000", result)
        self.assertIn("token=[REDACTED:credential_assignment]", result)

    def test_quoted_credential_assignment_preserves_quotes_and_key(self):
        text = '{"api_key": "FAKEVALUEFAKEVALUE0000"}'
        result = redact_secrets(text)
        self.assertNotIn("FAKEVALUEFAKEVALUE0000", result)
        self.assertIn('"api_key": "[REDACTED:credential_assignment]"', result)


class RedactSecretsFalsePositiveTests(unittest.TestCase):
    def test_bare_sha256_hex_digest_not_redacted(self):
        digest = hashlib.sha256(b"autoscout-issue-287-fixture").hexdigest()
        text = f'{{"capture_sha256": "{digest}"}}'
        self.assertEqual(redact_secrets(text), text)
        self.assertFalse(contains_secret(text))

    def test_normal_url_not_redacted(self):
        text = "see https://example.test/nonfiction/bestsellers?ref=abcdef1234567890 for details"
        self.assertEqual(redact_secrets(text), text)
        self.assertFalse(contains_secret(text))

    def test_prose_mentioning_key_without_assignment_not_redacted(self):
        text = "the key insight from this source is that sales spiked in March"
        self.assertEqual(redact_secrets(text), text)
        self.assertFalse(contains_secret(text))

    def test_hyphenated_url_slug_containing_sk_not_redacted(self):
        # Regression: a real candidate URL whose book-title slug embeds
        # "sk-" ("high-risk-by-...") was flagged as an openai_key,
        # which would make the fail-closed artifact scan block publication.
        text = "https://www.penguinrandomhouse.com/books/676238/high-risk-by-chavi-eve-karkowsky-md/"
        self.assertEqual(redact_secrets(text), text)
        self.assertFalse(contains_secret(text))

    def test_openai_style_key_still_redacted_after_slug_fix(self):
        for fake in ("sk-" + "FAKE1234" * 4, "sk-proj-" + "FAKE1234" * 4):
            result = redact_secrets(f"error: {fake} rejected")
            self.assertNotIn(fake, result)
            self.assertIn("[REDACTED:openai_key]", result)


class ContainsSecretTests(unittest.TestCase):
    def test_true_when_pattern_present(self):
        self.assertTrue(contains_secret(f"leaked: {FAKE_FIGMA_PAT}"))

    def test_false_for_plain_text(self):
        self.assertFalse(contains_secret("nothing sensitive here at all"))

    def test_false_for_empty_or_non_string(self):
        self.assertFalse(contains_secret(""))
        self.assertFalse(contains_secret(None))  # type: ignore[arg-type]


class RedactStructureTests(unittest.TestCase):
    def test_nested_dict_list_tuple_redacted_shape_preserved(self):
        structure = {
            "diagnostics": [
                {"source_name": "Test Source", "diagnostic": f"stderr: {FAKE_FIGMA_PAT}"},
                ("attempt", 1, FAKE_GITHUB_TOKEN),
            ],
            "count": 3,
            "ok": True,
            "missing": None,
            "capture_sha256": hashlib.sha256(b"x").hexdigest(),
        }
        result = redact_structure(structure)

        self.assertIsInstance(result, dict)
        self.assertIsInstance(result["diagnostics"], list)
        self.assertIsInstance(result["diagnostics"][1], tuple)
        self.assertEqual(result["count"], 3)
        self.assertIs(result["ok"], True)
        self.assertIsNone(result["missing"])
        # sha256 digest untouched.
        self.assertEqual(result["capture_sha256"], structure["capture_sha256"])
        # secrets gone from both the dict-nested and tuple-nested strings.
        serialized = json.dumps(result, default=str)
        self.assertNotIn(FAKE_FIGMA_PAT, serialized)
        self.assertNotIn(FAKE_GITHUB_TOKEN, serialized)
        self.assertIn("[REDACTED:figma_pat]", serialized)
        self.assertIn("[REDACTED:github_token]", serialized)


class CodexStderrRedactedBeforeTruncationTests(unittest.TestCase):
    def test_token_straddling_truncation_boundary_leaves_no_fragment(self):
        # Build stderr so a naive stderr[-500:] truncation (redact AFTER
        # truncate, the buggy order) would only capture a trailing fragment
        # of the token -- still a recoverable partial secret. Redacting
        # BEFORE truncating (the fix) must remove the whole token first.
        noise_before = "A" * 600
        noise_after = "B" * 50
        raw_stderr = noise_before + FAKE_FIGMA_PAT + noise_after
        self.assertGreater(len(raw_stderr), 500)

        # Sanity-check the premise: naive truncation alone would leak a
        # recoverable fragment of the real token.
        naively_truncated = raw_stderr.strip()[-500:]
        self.assertIn("UNIQSUFFIXTAIL99", naively_truncated)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "capture.json"
            artifact = root / "artifact.json"
            capture.write_text("{}", encoding="utf-8")

            def fake_run_codex(command, isolated, response_path, bounded_timeout):
                return (1, raw_stderr)

            with mock.patch.object(CodexCliExtractionAgent, "_run_codex", side_effect=fake_run_codex):
                result = CodexCliExtractionAgent(timeout_seconds=1).extract({}, capture, artifact)

        self.assertEqual(result.status, ExtractionStatus.COMMAND_FAILED)
        diagnostic = result.diagnostic
        self.assertNotIn(FAKE_FIGMA_PAT, diagnostic)
        self.assertNotIn("figd_", diagnostic)
        self.assertNotIn("UNIQSUFFIXTAIL99", diagnostic)
        # No sliding-window fragment of the token body (beyond the bare
        # "figd_" prefix length) survives anywhere in the diagnostic.
        token_body = FAKE_FIGMA_PAT[len("figd_") :]
        for start in range(0, len(token_body) - 10):
            fragment = token_body[start : start + 10]
            self.assertNotIn(fragment, diagnostic)
        self.assertIn("[REDACTED:figma_pat]", diagnostic)


class WeeklyWorkflowSecretScanTests(unittest.TestCase):
    """Fail-closed scan: a synthetic credential in a canonical artifact must
    block publication with a sanitized diagnostic."""

    def _write_config(self, tmp_path: Path) -> Path:
        config_path = tmp_path / "config.json"
        config = {
            "run_date": "2026-07-13",
            "readiness_purpose": "recurring-weekly",
            "editor_row_target": 100,
            "pilot_row_min": 100,
            "pilot_row_max": 100,
            "pilot_mode": True,
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
                    "extraction_response_file": "test-extraction.json",
                }
            ],
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")
        return config_path

    def _write_capture_fixture(self, tmp_path: Path) -> None:
        (tmp_path / "test-capture.json").write_text(
            json.dumps(
                {
                    "source_url": "https://example.test/source",
                    "html": '<html><body><a href="https://example.test/book">Test</a></body></html>',
                }
            ),
            encoding="utf-8",
        )

    def _write_extraction_fixture(self, output_dir: Path, *, planted_secret: str = "") -> None:
        worklist = json.loads((output_dir / "codex-extraction-worklist.json").read_text(encoding="utf-8"))
        item = worklist["work_items"][0]
        payload: dict = {
            "source_name": "Test Source",
            "source_url": "https://example.test/source",
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
                    "signal_strength": "shortlist",
                }
            ],
        }
        if planted_secret:
            payload["operator_debug_note"] = planted_secret
        result = output_dir / str(item["expected_extraction_file"])
        result.write_text(json.dumps(payload), encoding="utf-8")

    def test_planted_credential_in_canonical_artifact_blocks_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_config(tmp_path)
            output_dir = tmp_path / "run"
            output_dir.mkdir()
            self._write_capture_fixture(tmp_path)

            adapters = deterministic_adapters()
            adapters.capture.prepare(config_path, output_dir, live=False)
            worklist = json.loads((output_dir / "codex-extraction-worklist.json").read_text(encoding="utf-8"))
            expected_extraction_file = str(worklist["work_items"][0]["expected_extraction_file"])
            self._write_extraction_fixture(output_dir, planted_secret=f"leaked during debug: {FAKE_FIGMA_PAT}")

            result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

            self.assertEqual(result.outcome, WorkflowOutcome.INTERVENTION_REQUIRED)
            self.assertEqual(result.failed_stage, "artifact-secret-scan")

            log_text = json.dumps(result.log)
            self.assertNotIn(FAKE_FIGMA_PAT, log_text)
            self.assertNotIn("UNIQSUFFIXTAIL99", log_text)
            self.assertIn(expected_extraction_file, log_text)

            log_path = output_dir / "workflow-log.json"
            if log_path.exists():
                persisted = log_path.read_text(encoding="utf-8")
                self.assertNotIn(FAKE_FIGMA_PAT, persisted)
                self.assertNotIn("UNIQSUFFIXTAIL99", persisted)

    def test_clean_artifacts_do_not_trigger_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_config(tmp_path)
            output_dir = tmp_path / "run"
            output_dir.mkdir()
            self._write_capture_fixture(tmp_path)

            adapters = deterministic_adapters()
            adapters.capture.prepare(config_path, output_dir, live=False)
            self._write_extraction_fixture(output_dir)

            result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

            self.assertNotEqual(result.failed_stage, "artifact-secret-scan")


if __name__ == "__main__":
    unittest.main()
