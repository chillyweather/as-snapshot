"""Tests for ``autoscout.config`` — typed config validation at load time."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from autoscout.cli.weekly import run_mvp as _cli_run_mvp
from autoscout.codex_workflow import prepare_codex_extraction
from autoscout.config import load_validated_config
from autoscout.config import resolve_weekly_portfolio
from autoscout.config import resolve_weekly_portfolio_or_raise
from autoscout.config import validate_config

# ── Fixture paths ─────────────────────────────────────────────────────────

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "configs"
_VALID_FIXTURE = _FIXTURE_DIR / "weekly-mvp-fixture.json"
_VALID_PILOT = _FIXTURE_DIR / "current-weekly-pilot.json"
_VALID_LIVE = _FIXTURE_DIR / "current-weekly-live.json"
_VALID_PILOT_LIVE = _FIXTURE_DIR / "current-weekly-pilot-live.json"


def _undated_ranking_source(**overrides: object) -> dict[str, object]:
    return {
        "name": "Synthetic undated ranking",
        "family": "bestseller/ranking",
        "contract_family": "ranking-table",
        "url": "https://example.test/weekly-ranking",
        "language": "es",
        "metadata_ranking_table": True,
        "ranking_row_selector": ".row",
        "ranking_rank_source": "selector",
        "ranking_rank_selector": ".rank",
        "ranking_title_selector": ".title",
        "ranking_author_selector": ".author",
        "ranking_identifier_pattern": r"(?P<identifier>9780306406157)",
        "ranking_category_marker": "más vendidos",
        "ranking_reporting_period_undated": True,
        "ranking_expected_raw_row_count": 2,
        "ranking_evidence_url": "source_page",
        "adult_nonfiction_exclusions": ["libro de actividades"],
        **overrides,
    }


class _IPCResult:
    __slots__ = ("returncode", "stderr")

    def __init__(self, returncode: int, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr


def _run_mvp(config_path: Path, output_dir: Path) -> _IPCResult:
    """Run MVP pipeline in-process, returning a result-like object."""
    captured = io.StringIO()
    try:
        with redirect_stderr(captured):
            _cli_run_mvp(config_path, output_dir)
        return _IPCResult(0, captured.getvalue())
    except SystemExit as e:
        return _IPCResult(e.code if e.code is not None else 1, captured.getvalue())
    except Exception as e:
        return _IPCResult(1, captured.getvalue() + "\n" + str(e))


def _load(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return resolve_weekly_portfolio(raw, path)


# ── Valid config tests ────────────────────────────────────────────────────


class ValidConfigTests(unittest.TestCase):
    def test_fixture_config_passes_validation(self):
        errors = validate_config(_load(_VALID_FIXTURE))
        self.assertEqual(errors, [])

    def test_pilot_config_passes_validation(self):
        errors = validate_config(_load(_VALID_PILOT))
        self.assertEqual(errors, [])

    def test_live_config_passes_validation(self):
        errors = validate_config(_load(_VALID_LIVE))
        self.assertEqual(errors, [])

    def test_pilot_live_config_passes_validation(self):
        errors = validate_config(_load(_VALID_PILOT_LIVE))
        self.assertEqual(errors, [])

    def test_minimal_valid_config_passes_validation(self):
        errors = validate_config({"run_date": "2026-01-01", "sources": []})
        self.assertEqual(errors, [])

    def test_undated_ranking_source_passes_validate_and_load(self):
        config = {"run_date": "2026-01-01", "sources": [_undated_ranking_source()]}
        self.assertEqual(validate_config(config), [])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "undated-ranking.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            loaded = load_validated_config(path)
        self.assertTrue(loaded["sources"][0]["ranking_reporting_period_undated"])

    def test_ranking_source_without_freshness_declaration_fails(self):
        source = _undated_ranking_source()
        source.pop("ranking_reporting_period_undated")
        errors = validate_config({"run_date": "2026-01-01", "sources": [source]})
        self.assertTrue(any("ranking-table requires ranking_reporting_period_pattern" in error for error in errors))

    def test_ranking_source_rejects_contradictory_freshness_declarations(self):
        source = _undated_ranking_source(ranking_reporting_period_pattern=r"(?P<period>\d{4})")
        errors = validate_config({"run_date": "2026-01-01", "sources": [source]})
        self.assertTrue(any("cannot be combined with dated reporting metadata" in error for error in errors))

    def test_full_valid_config_with_all_known_keys_passes_validation(self):
        config = {
            "run_date": "2026-01-01",
            "sources": [
                {
                    "name": "Test Source",
                    "family": "award",
                    "url": "https://example.com",
                    "active": True,
                    "access_status": "open",
                    "extraction_method": "fixture",
                    "check_frequency": "weekly",
                    "language": "en",
                    "notes": "Test.",
                    "fixture_candidates": [],
                    "enable_live": False,
                    "extraction_backend": "real",
                    "enable_real_backend": False,
                    "credential_env": "TEST_KEY",
                    "max_model_input_chars": 120000,
                    "model_timeout_seconds": 30,
                    "model_max_output_tokens": 1200,
                    "source_page_fixture_file": "page.json",
                    "extraction_response_file": "extract.json",
                    "live_response_file": "live.json",
                    "capture_live_response_file": "cap-live.json",
                    "capture_extraction_response_file": "cap-ext.json",
                    "recorded_mentions_file": "rec.json",
                    "recorded_mentions_files": [],
                    "collector_response_file": "col.json",
                    "live_extraction_response_file": "le.json",
                    "codex_artifact_dir": ".",
                    "timeout_seconds": 10,
                    "russian_edition_status_source": "manual",
                    "visible_risk_status_source": "manual",
                }
            ],
            "pilot_mode": True,
            "editor_row_target": 100,
            "pilot_row_min": 100,
            "pilot_row_max": 100,
            "pilot_row_target_min": 50,
            "pilot_row_target_max": 150,
            "report_qa": {"reviewer": "Test", "status": "ok"},
            "candidate_history": [],
            "excluded_candidate_history": [],
            "returned_editor_rows": [],
            "russian_edition_checks": [
                {
                    "retailer": "Ozon",
                    "url": "https://www.ozon.ru",
                    "fixture_results": [],
                    "enable_live": True,
                    "name": "ozon-check",
                    "fixture_error": "",
                    "fixture_live_results": [],
                    "live_response_files": [],
                    "live_response_file": "",
                    "recorded_results": [],
                    "recorded_results_file": "",
                    "recorded_results_files": [],
                    "capture_recorded_results_file": "",
                    "timeout_seconds": 10,
                    "search_path": "/search",
                    "query_param": "q",
                    "family": "russian-retailer",
                    "source_family": "russian-retailer",
                    "live_blocked_reason": "",
                    "safe_debug_status": "",
                }
            ],
        }
        errors = validate_config(config)
        self.assertEqual(errors, [])


# ── Top-level validation errors ───────────────────────────────────────────


class TopLevelValidationErrorsTests(unittest.TestCase):
    def test_missing_run_date_is_not_required_defaults_to_today(self):
        errors = validate_config({"sources": []})
        self.assertEqual(errors, [])

    def test_missing_sources_reports_error(self):
        errors = validate_config({"run_date": "2026-01-01"})
        self.assertEqual(len(errors), 1)
        self.assertIn("missing required key 'sources'", errors[0])

    def test_unknown_top_level_key_reports_error(self):
        errors = validate_config({"run_date": "2026-01-01", "sources": [], "typo_field": 1})
        self.assertEqual(len(errors), 1)
        self.assertIn("unknown key 'typo_field'", errors[0])

    def test_wrong_type_for_run_date_reports_error(self):
        errors = validate_config({"run_date": 20260101, "sources": []})
        self.assertEqual(len(errors), 1)
        self.assertIn("'run_date' must be str", errors[0])

    def test_wrong_type_for_pilot_mode_reports_error(self):
        errors = validate_config({"run_date": "2026-01-01", "sources": [], "pilot_mode": "yes"})
        self.assertEqual(len(errors), 1)
        self.assertIn("'pilot_mode' must be bool", errors[0])

    def test_wrong_type_for_sources_reports_error(self):
        errors = validate_config({"run_date": "2026-01-01", "sources": "not-a-list"})
        self.assertEqual(len(errors), 1)
        self.assertIn("'sources' must be list", errors[0])

    def test_null_sources_reports_error(self):
        errors = validate_config({"run_date": "2026-01-01", "sources": None})
        self.assertEqual(len(errors), 1)
        self.assertIn("'sources' must be list", errors[0])

    def test_non_object_top_level_reports_error_without_crashing(self):
        errors = validate_config([])
        self.assertEqual(errors, ["top-level config must be a JSON object"])


# ── Source validation errors ──────────────────────────────────────────────


class SourceValidationErrorsTests(unittest.TestCase):
    def test_missing_name_reports_error_with_index(self):
        errors = validate_config(
            {"run_date": "2026-01-01", "sources": [{"url": "https://example.com", "language": "en"}]}
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("missing required key 'name'", errors[0])

    def test_non_object_source_reports_error_without_crashing(self):
        errors = validate_config({"run_date": "2026-01-01", "sources": ["not-an-object"]})
        self.assertEqual(errors, ["source[0]: must be a JSON object"])

    def test_missing_url_not_required_accepts_source_without_url(self):
        errors = validate_config({"run_date": "2026-01-01", "sources": [{"name": "test", "language": "en"}]})
        self.assertEqual(errors, [])

    def test_helper_metadata_file_must_be_declared_with_digest(self):
        errors = validate_config(
            {
                "sources": [{"name": "test", "language": "en", "helper_metadata_sha256": "a" * 64}],
            }
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("helper_metadata_file", errors[0])
        self.assertIn("helper_metadata_sha256", errors[0])

    def test_helper_metadata_digest_must_be_declared_with_file(self):
        errors = validate_config(
            {
                "sources": [{"name": "test", "language": "en", "helper_metadata_file": "helper.json"}],
            }
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("helper_metadata_file", errors[0])
        self.assertIn("helper_metadata_sha256", errors[0])

    def test_helper_metadata_digest_rejects_uppercase_and_malformed_values(self):
        for digest in ("A" * 64, "a" * 63, "not-a-digest"):
            with self.subTest(digest=digest):
                errors = validate_config(
                    {
                        "sources": [
                            {
                                "name": "test",
                                "language": "en",
                                "helper_metadata_file": "helper.json",
                                "helper_metadata_sha256": digest,
                            }
                        ]
                    }
                )
                self.assertEqual(len(errors), 1)
                self.assertIn("helper_metadata_sha256", errors[0])
                self.assertIn("64 lowercase hexadecimal", errors[0])

    def test_helper_metadata_empty_declarations_fail_with_field_names(self):
        errors = validate_config(
            {
                "sources": [
                    {
                        "name": "test",
                        "language": "en",
                        "helper_metadata_file": "",
                        "helper_metadata_sha256": "",
                    }
                ]
            }
        )
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("helper_metadata_file" in error for error in errors))
        self.assertTrue(any("helper_metadata_sha256" in error for error in errors))

    def test_helper_metadata_file_and_digest_valid_pair_passes(self):
        errors = validate_config(
            {
                "sources": [
                    {
                        "name": "test",
                        "language": "en",
                        "helper_metadata_file": "helper.json",
                        "helper_metadata_sha256": "a" * 64,
                    }
                ]
            }
        )
        self.assertEqual(errors, [])

    def test_unknown_source_key_reports_error_with_source_name(self):
        config = _load(_VALID_FIXTURE)
        for source in config["sources"]:
            source.setdefault("language", "en")
        config["sources"][0]["bad_field"] = 123
        errors = validate_config(config)
        self.assertEqual(len(errors), 1)
        self.assertIn("unknown key 'bad_field'", errors[0])
        self.assertIn("Fixture Awards", errors[0])

    def test_wrong_type_for_active_reports_error(self):
        config = _load(_VALID_FIXTURE)
        for source in config["sources"]:
            source.setdefault("language", "en")
        config["sources"][0]["active"] = "yes"
        # Only trigger error if "yes" evaluates differently... in Python "yes" is truthy
        # but the type check is isinstance(value, bool), and "yes" is str not bool.
        errors = validate_config(config)
        self.assertEqual(len(errors), 1)
        self.assertIn("'active' must be bool", errors[0])
        self.assertIn("Fixture Awards", errors[0])

    def test_multiple_source_errors_all_reported(self):
        errors = validate_config(
            {
                "run_date": "2026-01-01",
                "sources": [
                    {"name": "A", "language": "en", "bad_1": 1},
                    {"url": "https://example.com", "language": "en", "bad_2": 2},
                ],
            }
        )
        self.assertEqual(len(errors), 3)  # A: bad_1; source[1]: missing name + bad_2
        self.assertTrue(any("bad_1" in e for e in errors))
        self.assertTrue(any("bad_2" in e for e in errors))
        self.assertTrue(any("missing required key 'name'" in e for e in errors))

    def test_floating_int_fields_accept_either_type(self):
        errors = validate_config(
            {
                "run_date": "2026-01-01",
                "sources": [
                    {
                        "name": "test",
                        "url": "https://example.com",
                        "language": "en",
                        "max_model_input_chars": 120000,
                        "model_timeout_seconds": 30.5,
                        "model_max_output_tokens": 1200.0,
                    }
                ],
            }
        )
        self.assertEqual(errors, [])


# ── Russian check validation errors ───────────────────────────────────────


class RussianCheckValidationErrorsTests(unittest.TestCase):
    def test_unknown_check_key_reports_error_with_check_name(self):
        config = _load(_VALID_FIXTURE)
        for source in config["sources"]:
            source.setdefault("language", "en")
        config["russian_edition_checks"][0]["bad_field"] = "x"
        errors = validate_config(config)
        self.assertEqual(len(errors), 1)
        self.assertIn("unknown key 'bad_field'", errors[0])
        self.assertIn("Ozon", errors[0])

    def test_check_without_retailer_uses_fallback_label(self):
        errors = validate_config(
            {
                "run_date": "2026-01-01",
                "sources": [],
                "russian_edition_checks": [
                    {"url": "https://example.com", "bad_key": 1},
                ],
            }
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("russian-check[0]", errors[0])

    def test_non_object_check_reports_error_without_crashing(self):
        errors = validate_config({"run_date": "2026-01-01", "sources": [], "russian_edition_checks": ["not-an-object"]})
        self.assertEqual(errors, ["russian-check[0]: must be a JSON object"])

    def test_multiple_check_errors_all_reported(self):
        errors = validate_config(
            {
                "run_date": "2026-01-01",
                "sources": [],
                "russian_edition_checks": [
                    {"retailer": "A", "bad_a": 1},
                    {"retailer": "B", "bad_b": 2},
                ],
            }
        )
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("bad_a" in e for e in errors))
        self.assertTrue(any("bad_b" in e for e in errors))


# ── CLI-level validation exit behaviour ───────────────────────────────────


class CLIValidationExitTests(unittest.TestCase):
    def test_invalid_config_run_mvp_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bad_config = tmp_path / "bad-config.json"
            bad_config.write_text(
                json.dumps({"sources": [{"name": "A", "bad_key": 1}]}),  # unknown source key
                encoding="utf-8",
            )
            output_dir = tmp_path / "out"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "autoscout",
                    "run-mvp",
                    "--config",
                    str(bad_config),
                    "--output-dir",
                    str(output_dir),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("validation failed", result.stderr)

    def test_invalid_config_prepare_codex_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bad_config = tmp_path / "bad-config.json"
            bad_config.write_text(
                json.dumps({"run_date": "2026-01-01", "sources": [{"name": "A", "bad_key": 1}]}),
                encoding="utf-8",
            )
            output_dir = tmp_path / "out"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "autoscout",
                    "prepare-codex-extraction",
                    "--config",
                    str(bad_config),
                    "--output-dir",
                    str(output_dir),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("validation failed", result.stderr)

    def test_valid_config_run_mvp_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "out"
            result = _run_mvp(_VALID_FIXTURE, output_dir)
            self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")
            self.assertTrue((output_dir / "run-summary.json").exists())

    def test_valid_config_prepare_codex_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "out"
            prepare_codex_extraction(_VALID_PILOT, output_dir)
            self.assertTrue((output_dir / "codex-extraction-worklist.json").exists())


# ── Artifact parity ───────────────────────────────────────────────────────


class ConfigArtifactParityTests(unittest.TestCase):
    def test_fixture_config_produces_expected_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "out"
            result = _run_mvp(_VALID_FIXTURE, output_dir)
            self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")

            expected_artifacts = [
                "editor-facing.csv",
                "run-summary.json",
                "source-mentions.json",
                "scouting-candidates.json",
                "candidate-history.json",
                "proposed-corrections.json",
                "memo-queue.json",
                "source-proposals.json",
                "suppressed-candidates.json",
                "internal-exclusions.json",
                "russian-edition-checks.json",
                "source-runs.json",
                "report-qa.json",
                "russian-review-queue.csv",
            ]
            for name in expected_artifacts:
                self.assertTrue(
                    (output_dir / name).exists(),
                    f"Missing artifact: {name}",
                )

            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertIn("editor_facing_count", summary)
            self.assertIn("major_warnings", summary)

    def test_pilot_config_produces_expected_pilot_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "out"
            result = _run_mvp(_VALID_PILOT, output_dir)
            self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")
            self.assertTrue((output_dir / "pilot-status.json").exists())
            self.assertTrue((output_dir / "editor-review-guide.md").exists())


# ── Portfolio resolution tests ────────────────────────────────────────────


class PortfolioResolutionTests(unittest.TestCase):
    """Tests for resolve_weekly_portfolio - merging portfolio + overlays."""

    def test_raising_resolver_keeps_resolution_errors_out_of_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            with self.assertRaisesRegex(ValueError, "weekly portfolio file not found"):
                resolve_weekly_portfolio_or_raise(
                    {"weekly_portfolio": "missing.json", "sources": []},
                    config_path,
                )

    def test_wrapper_propagates_portfolio_json_decode_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            portfolio_path = tmp_path / "portfolio.json"
            portfolio_path.write_text("not-json", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                resolve_weekly_portfolio(
                    {"weekly_portfolio": portfolio_path.name, "sources": []},
                    tmp_path / "config.json",
                )

    def test_wrapper_propagates_portfolio_io_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            portfolio_path = tmp_path / "portfolio.json"
            portfolio_path.write_text(json.dumps({"sources": []}), encoding="utf-8")
            with patch("autoscout.config.read_json", side_effect=OSError("read failed")):
                with self.assertRaisesRegex(OSError, "read failed"):
                    resolve_weekly_portfolio(
                        {"weekly_portfolio": portfolio_path.name, "sources": []},
                        tmp_path / "config.json",
                    )

    def test_config_without_portfolio_passes_through_unchanged(self):
        config = {"run_date": "2026-01-01", "sources": [{"name": "A", "url": "https://a.test"}]}
        result = resolve_weekly_portfolio(config, Path("/fake/config.json"))
        self.assertEqual(result, config)

    def test_empty_overlay_preserves_all_portfolio_sources(self):
        portfolio_sources = [
            {
                "name": "Src A",
                "family": "award",
                "url": "https://a.test",
                "active": True,
                "access_status": "open",
                "check_frequency": "weekly",
                "language": "en",
                "notes": "Note A",
            },
            {
                "name": "Src B",
                "family": "media",
                "url": "https://b.test",
                "active": True,
                "access_status": "open",
                "check_frequency": "weekly",
                "language": "en",
                "notes": "Note B",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            portfolio_path = tmp_path / "portfolio.json"
            portfolio_path.write_text(json.dumps({"sources": portfolio_sources}), encoding="utf-8")
            config_path = tmp_path / "config.json"
            config = {
                "run_date": "2026-01-01",
                "weekly_portfolio": "portfolio.json",
                "sources": [{}, {}],
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")

            raw = json.loads(config_path.read_text(encoding="utf-8"))
            result = resolve_weekly_portfolio(raw, config_path)

            self.assertEqual(len(result["sources"]), 2)
            self.assertEqual(result["sources"][0]["name"], "Src A")
            self.assertEqual(result["sources"][1]["name"], "Src B")
            self.assertNotIn("weekly_portfolio", result)

    def test_overlay_merges_extraction_fields_on_top_of_portfolio(self):
        portfolio_sources = [
            {
                "name": "Src A",
                "family": "award",
                "url": "https://a.test",
                "active": True,
                "access_status": "open",
                "check_frequency": "weekly",
                "language": "en",
                "notes": "Base notes.",
            },
        ]
        overlays = [
            {
                "name": "Src A",
                "extraction_method": "codex-extraction-replay",
                "source_page_fixture_file": "page.json",
                "extraction_response_file": "extract.json",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            portfolio_path = tmp_path / "portfolio.json"
            portfolio_path.write_text(json.dumps({"sources": portfolio_sources}), encoding="utf-8")
            config_path = tmp_path / "config.json"
            config = {
                "run_date": "2026-01-01",
                "weekly_portfolio": "portfolio.json",
                "sources": overlays,
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")

            raw = json.loads(config_path.read_text(encoding="utf-8"))
            result = resolve_weekly_portfolio(raw, config_path)

            resolved = result["sources"][0]
            self.assertEqual(resolved["name"], "Src A")
            self.assertEqual(resolved["notes"], "Base notes.")
            self.assertEqual(resolved["extraction_method"], "codex-extraction-replay")
            self.assertEqual(resolved["source_page_fixture_file"], "page.json")
            self.assertEqual(resolved["extraction_response_file"], "extract.json")

    def test_overlay_can_intentionally_replace_portfolio_identity_with_explicit_target(self):
        portfolio_sources = [
            {
                "name": "Original",
                "family": "award",
                "url": "https://original.test",
                "active": True,
                "access_status": "open",
                "check_frequency": "weekly",
                "language": "en",
                "notes": "Original notes.",
            },
        ]
        overlays = [
            {
                "portfolio_name": "Original",
                "name": "Overridden",
                "url": "https://overridden.test",
                "notes": "Overridden notes.",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            portfolio_path = tmp_path / "portfolio.json"
            portfolio_path.write_text(json.dumps({"sources": portfolio_sources}), encoding="utf-8")
            config_path = tmp_path / "config.json"
            config = {
                "run_date": "2026-01-01",
                "weekly_portfolio": "portfolio.json",
                "sources": overlays,
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")

            raw = json.loads(config_path.read_text(encoding="utf-8"))
            result = resolve_weekly_portfolio(raw, config_path)

            resolved = result["sources"][0]
            self.assertEqual(resolved["name"], "Overridden")
            self.assertEqual(resolved["url"], "https://overridden.test")
            self.assertEqual(resolved["notes"], "Overridden notes.")
            self.assertEqual(resolved["family"], "award")  # unchanged from portfolio

    def test_unknown_overlay_name_exits_instead_of_falling_back_to_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            portfolio_path = tmp_path / "portfolio.json"
            portfolio_path.write_text(json.dumps({"sources": [{"name": "Src A"}]}), encoding="utf-8")
            config_path = tmp_path / "config.json"
            raw = {
                "run_date": "2026-01-01",
                "weekly_portfolio": "portfolio.json",
                "sources": [{"name": "Src B", "extraction_method": "fixture"}],
            }

            with self.assertRaises(SystemExit):
                resolve_weekly_portfolio(raw, config_path)

    def test_missing_portfolio_file_exits_with_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            config = {
                "run_date": "2026-01-01",
                "weekly_portfolio": "nonexistent.json",
                "sources": [{}],
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; from pathlib import Path; from autoscout.config import load_validated_config; load_validated_config(Path(sys.argv[1]))",
                    str(config_path),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_weekly_portfolio_validates_resolved_config(self):
        """Validation runs on the resolved config, so overlay sources don't need 'name'."""
        portfolio_sources = [
            {
                "name": "Valid",
                "family": "award",
                "url": "https://valid.test",
                "active": True,
                "access_status": "open",
                "check_frequency": "weekly",
                "language": "en",
                "notes": "OK.",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            portfolio_path = tmp_path / "portfolio.json"
            portfolio_path.write_text(json.dumps({"sources": portfolio_sources}), encoding="utf-8")
            config_path = tmp_path / "config.json"
            # Overlay names its portfolio source; validation runs after resolution.
            config = {
                "run_date": "2026-01-01",
                "weekly_portfolio": "portfolio.json",
                "sources": [{"name": "Valid", "extraction_method": "fixture"}],
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")

            raw = json.loads(config_path.read_text(encoding="utf-8"))
            resolved = resolve_weekly_portfolio(raw, config_path)
            errors = validate_config(resolved)
            self.assertEqual(errors, [])

    def test_weekly_pilot_config_resolves_to_same_sources_as_expanded(self):
        """Artifact parity: resolved overlay-based config equals the original expanded form."""
        import json as _json

        pilot_path = _FIXTURE_DIR / "current-weekly-pilot.json"
        portfolio_path = _FIXTURE_DIR / "current-weekly-portfolio.json"

        raw = _json.loads(pilot_path.read_text(encoding="utf-8"))
        resolved = resolve_weekly_portfolio(raw, pilot_path)

        portfolio = _json.loads(portfolio_path.read_text(encoding="utf-8"))
        overlay_sources = raw["sources"]

        self.assertEqual(len(resolved["sources"]), len(portfolio["sources"]))
        for i, (portfolio_src, overlay, resolved_src) in enumerate(
            zip(portfolio["sources"], overlay_sources, resolved["sources"], strict=True)
        ):
            expected_overlay = dict(overlay)
            expected_overlay.pop("portfolio_name", None)
            expected = {**portfolio_src, **expected_overlay}
            self.assertEqual(resolved_src, expected, f"Source [{i}] mismatch")

    def test_weekly_live_config_resolves_to_same_sources_as_expanded(self):
        """Artifact parity for the live config."""
        import json as _json

        live_path = _FIXTURE_DIR / "current-weekly-live.json"
        portfolio_path = _FIXTURE_DIR / "current-weekly-portfolio.json"

        raw = _json.loads(live_path.read_text(encoding="utf-8"))
        resolved = resolve_weekly_portfolio(raw, live_path)

        portfolio = _json.loads(portfolio_path.read_text(encoding="utf-8"))
        overlay_sources = raw["sources"]

        self.assertEqual(len(resolved["sources"]), len(portfolio["sources"]))
        for i, (portfolio_src, overlay, resolved_src) in enumerate(
            zip(portfolio["sources"], overlay_sources, resolved["sources"], strict=True)
        ):
            expected_overlay = dict(overlay)
            expected_overlay.pop("portfolio_name", None)
            expected = {**portfolio_src, **expected_overlay}
            self.assertEqual(resolved_src, expected, f"Source [{i}] mismatch")

    def test_weekly_pilot_live_config_resolves_to_same_sources_as_expanded(self):
        """Artifact parity for the pilot-live config."""
        import json as _json

        pilot_live_path = _FIXTURE_DIR / "current-weekly-pilot-live.json"
        portfolio_path = _FIXTURE_DIR / "current-weekly-portfolio.json"

        raw = _json.loads(pilot_live_path.read_text(encoding="utf-8"))
        resolved = resolve_weekly_portfolio(raw, pilot_live_path)

        portfolio = _json.loads(portfolio_path.read_text(encoding="utf-8"))
        overlay_sources = raw["sources"]

        self.assertEqual(len(resolved["sources"]), len(portfolio["sources"]))
        for i, (portfolio_src, overlay, resolved_src) in enumerate(
            zip(portfolio["sources"], overlay_sources, resolved["sources"], strict=True)
        ):
            expected_overlay = dict(overlay)
            expected_overlay.pop("portfolio_name", None)
            expected = {**portfolio_src, **expected_overlay}
            self.assertEqual(resolved_src, expected, f"Source [{i}] mismatch")


def test_exact_book_list_requires_companion_fields():
    valid_source = {
        "name": "Valid Exact List",
        "family": "award",
        "url": "https://example.test/list",
        "language": "en",
        "metadata_exact_book_list": True,
        "expected_signal_strength": "shortlist",
        "eligible_candidate_category": "adult nonfiction",
        "expected_page_heading": "Annual shortlist",
        "signal_family": "award",
    }
    assert validate_config({"run_date": "2026-07-17", "sources": [valid_source]}) == []
    for field in ("url", "expected_page_heading", "expected_signal_strength", "eligible_candidate_category"):
        errors = validate_config({"run_date": "2026-07-17", "sources": [{**valid_source, field: ""}]})
        assert len(errors) == 1 and field in errors[0]
    errors = validate_config(
        {"run_date": "2026-07-17", "sources": [{**valid_source, "signal_family": "", "family": ""}]}
    )
    assert len(errors) == 1 and "signal_family" in errors[0]


def test_exact_book_list_blank_strings_rejected():
    source = {
        "name": "Blank Fields",
        "language": "en",
        "metadata_exact_book_list": True,
        "url": "   ",
        "expected_signal_strength": "\t",
        "eligible_candidate_category": "  ",
        "expected_page_heading": "",
        "signal_family": "  ",
    }
    errors = validate_config({"run_date": "2026-07-17", "sources": [source]})
    assert len(errors) == 1
    for field in (
        "url",
        "expected_page_heading",
        "expected_signal_strength",
        "eligible_candidate_category",
        "signal_family",
    ):
        assert field in errors[0].casefold()


def test_exact_book_list_accepts_signal_family_or_family():
    source = {
        "name": "OK",
        "family": "award",
        "url": "https://example.test/",
        "language": "en",
        "metadata_exact_book_list": True,
        "expected_signal_strength": "shortlist",
        "eligible_candidate_category": "adult nonfiction",
        "expected_page_heading": "Heading",
    }
    assert validate_config({"run_date": "2026-07-17", "sources": [source]}) == []


def test_entry_section_heading_is_an_exact_book_list_declaration():
    source = {
        "name": "Sectioned",
        "family": "award",
        "url": "https://example.test/",
        "language": "en",
        "metadata_exact_book_list": True,
        "expected_signal_strength": "shortlist",
        "eligible_candidate_category": "adult nonfiction",
        "expected_page_heading": "Heading",
        "entry_section_heading": "The shortlist",
    }
    assert validate_config({"run_date": "2026-07-17", "sources": [source]}) == []
    errors = validate_config({"run_date": "2026-07-17", "sources": [{**source, "entry_section_heading": "  "}]})
    assert len(errors) == 1 and "'entry_section_heading' must be nonblank" in errors[0]
    not_exact_list = {key: value for key, value in source.items() if key != "metadata_exact_book_list"}
    errors = validate_config({"run_date": "2026-07-17", "sources": [not_exact_list]})
    assert len(errors) == 1 and "applies only to an exact-book-list source" in errors[0]


def test_probation_evidence_window_weeks_rejects_bool():
    source = {
        "name": "Bad",
        "url": "https://example.test/",
        "language": "en",
        "probation_evidence_window_weeks": True,
    }
    errors = validate_config({"run_date": "2026-07-17", "sources": [source]})
    assert len(errors) >= 1
    assert any("probation_evidence_window_weeks" in e for e in errors)


def test_probation_evidence_window_weeks_rejects_zero():
    source = {
        "name": "Bad",
        "url": "https://example.test/",
        "language": "en",
        "probation_evidence_window_weeks": 0,
    }
    errors = validate_config({"run_date": "2026-07-17", "sources": [source]})
    assert len(errors) >= 1
    assert any("probation_evidence_window_weeks" in e for e in errors)


def test_probation_evidence_window_weeks_rejects_negative():
    source = {
        "name": "Bad",
        "url": "https://example.test/",
        "language": "en",
        "probation_evidence_window_weeks": -1,
    }
    errors = validate_config({"run_date": "2026-07-17", "sources": [source]})
    assert len(errors) >= 1
    assert any("probation_evidence_window_weeks" in e for e in errors)


def test_probation_evidence_window_weeks_accepts_positive_int():
    source = {
        "name": "Good",
        "url": "https://example.test/",
        "language": "en",
        "probation_evidence_window_weeks": 6,
    }
    errors = validate_config({"run_date": "2026-07-17", "sources": [source]})
    assert errors == []


def test_probation_started_at_rejects_invalid_date():
    source = {
        "name": "Bad",
        "url": "https://example.test/",
        "language": "en",
        "probation_started_at": "not-a-date",
    }
    errors = validate_config({"run_date": "2026-07-17", "sources": [source]})
    assert len(errors) >= 1
    assert any("probation_started_at" in e for e in errors)


def test_probation_started_at_accepts_valid_iso_date():
    source = {
        "name": "Good",
        "url": "https://example.test/",
        "language": "en",
        "probation_started_at": "2026-07-01",
    }
    errors = validate_config({"run_date": "2026-07-17", "sources": [source]})
    assert errors == []


def test_probation_started_at_rejects_blank():
    source = {
        "name": "Bad",
        "url": "https://example.test/",
        "language": "en",
        "probation_started_at": "",
    }
    errors = validate_config({"run_date": "2026-07-17", "sources": [source]})
    assert len(errors) >= 1
    assert any("probation_started_at" in e for e in errors)


def _audible_chart_source(language: str, marker: str) -> dict:
    """Minimal valid audible_explicit_chart source with the given locale marker."""
    return {
        "name": "Audible Chart",
        "family": "audiobook ranking",
        "url": "https://www.audible.example/charts/test/1",
        "audible_explicit_chart": True,
        "expected_page_heading": "Top Chart",
        "max_response_bytes": 1500000,
        "expected_signal_strength": "top ranking",
        "language": language,
        "expected_range_marker": marker,
    }


def test_audible_chart_range_marker_is_locale_exact():
    german_with_english_marker = validate_config(
        {"run_date": "2026-07-17", "sources": [_audible_chart_source("de", "1 - 20 of 100 results")]}
    )
    assert len(german_with_english_marker) == 1
    assert "exact expected_range_marker" in german_with_english_marker[0]
    english_with_german_marker = validate_config(
        {"run_date": "2026-07-17", "sources": [_audible_chart_source("en", "1 - 20 von 100 Ergebnissen")]}
    )
    assert len(english_with_german_marker) == 1
    assert "exact expected_range_marker" in english_with_german_marker[0]
    for language, marker in (
        ("de", "1 - 20 von 100 Ergebnissen"),
        ("en", "1 - 20 of 100 results"),
    ):
        errors = validate_config({"run_date": "2026-07-17", "sources": [_audible_chart_source(language, marker)]})
        assert errors == []


if __name__ == "__main__":
    unittest.main()
