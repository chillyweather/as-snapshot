"""Regression tests for declaring a ranking table through the operator paths.

``metadata_ranking_table`` is a boolean, but it lives in ``RANKING_TABLE_FIELDS``
next to string selectors. Two consumers iterate that tuple and assumed every
member is a string, so the correct value was rejected and the wrong one was
accepted. A non-English ranking table must also declare its language, because an
absent language reads as English and would silently drop the translation rule.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from autoscout.cli.onboarding import add_ranking_table_arguments
from autoscout.config import validate_config
from autoscout.source_metadata import RANKING_DETAIL_CONFIG_FIELDS
from autoscout.source_metadata import RANKING_TABLE_FIELD_SPECS
from autoscout.source_metadata import SOURCE_SCAFFOLD_REQUIRED_METADATA_FIELDS
from autoscout.source_scaffold import PROBATION_LIFECYCLE_STATUS
from autoscout.source_scaffold import ScaffoldError
from autoscout.source_scaffold import build_probation_replay_contract_metadata
from autoscout.source_scaffold import normalize_operator_metadata

LIFECYCLE_NOTE = "Issue #562: active and reversible probation pending source metrics."


def _operator_metadata(**overrides: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {field: "x" for field in SOURCE_SCAFFOLD_REQUIRED_METADATA_FIELDS}
    metadata["lifecycle_note"] = LIFECYCLE_NOTE
    metadata["contract_family"] = "exact-book-list"
    metadata.update(overrides)
    return metadata


def test_detail_route_fields_are_derived_from_the_canonical_schema() -> None:
    expected_fields = {
        "ranking_detail_link_selector",
        "ranking_detail_url_pattern",
        "ranking_detail_scope_selector",
        "ranking_detail_fields",
        "ranking_detail_identifier_selector",
        "ranking_detail_identifier_attribute",
        "ranking_detail_identifier_pattern",
        "ranking_detail_category_selector",
        "ranking_detail_category_pattern",
        "ranking_detail_category_allowlist",
        "ranking_detail_max_pages",
        # Issue #1163: the bounded number of detail pages a capture may lose.
        "ranking_detail_max_failed_pages",
    }
    expected_kinds = {
        "ranking_detail_link_selector": "string",
        "ranking_detail_url_pattern": "string",
        "ranking_detail_scope_selector": "string",
        "ranking_detail_fields": "list",
        "ranking_detail_identifier_selector": "string",
        "ranking_detail_identifier_attribute": "string",
        "ranking_detail_identifier_pattern": "string",
        "ranking_detail_category_selector": "string",
        "ranking_detail_category_pattern": "string",
        "ranking_detail_category_allowlist": "list",
        "ranking_detail_max_pages": "int",
        "ranking_detail_max_failed_pages": "int",
    }
    specs = {spec.name: spec for spec in RANKING_TABLE_FIELD_SPECS if spec.detail_route}
    assert set(RANKING_DETAIL_CONFIG_FIELDS) == expected_fields
    assert set(specs) == expected_fields
    assert {name: spec.value_kind for name, spec in specs.items()} == expected_kinds


def test_operator_normalization_does_not_claim_replay_lifecycle_state() -> None:
    result = normalize_operator_metadata(_operator_metadata(metadata_ranking_table=True))
    assert result["metadata_ranking_table"] is True
    assert "lifecycle_status" not in result


def test_probation_replay_contract_metadata_declares_its_artifact_lifecycle() -> None:
    normalized = normalize_operator_metadata(_operator_metadata(metadata_ranking_table=True))
    replay_metadata = build_probation_replay_contract_metadata(normalized)
    assert replay_metadata["lifecycle_status"] == PROBATION_LIFECYCLE_STATUS


def test_scaffold_ignores_unknown_metadata_extensions_until_canonical_validation() -> None:
    result = normalize_operator_metadata(_operator_metadata(unrelated_extension="kept by a caller"))
    assert "unrelated_extension" not in result
    result = normalize_operator_metadata(_operator_metadata(ranking_detail_not_declared="x"))
    assert "ranking_detail_not_declared" not in result


def test_scaffold_rejects_a_stringly_typed_ranking_table_declaration() -> None:
    with pytest.raises(ScaffoldError, match="metadata_ranking_table must be a boolean"):
        normalize_operator_metadata(_operator_metadata(metadata_ranking_table="true"))


def test_add_source_cli_declares_the_ranking_table_flag_as_a_boolean() -> None:
    parser = argparse.ArgumentParser()
    add_ranking_table_arguments(parser)
    assert parser.parse_args(["--metadata-ranking-table"]).metadata_ranking_table is True
    # An omitted flag stays absent so it never injects a false declaration.
    assert parser.parse_args([]).metadata_ranking_table is None


def _ranking_source(**overrides: Any) -> dict[str, Any]:
    source: dict[str, Any] = {
        "name": "Synthetic ranking table",
        "family": "bestseller/ranking",
        "url": "https://example.test/ranking",
        "active": True,
        "access_status": "open",
        "check_frequency": "weekly",
        "contract_family": "ranking-table",
        "metadata_ranking_table": True,
        "ranking_evidence_url": "source_page",
        "ranking_row_selector": ".row",
        "ranking_rank_source": "document_order",
        "ranking_title_selector": ".title",
        "ranking_author_selector": ".author",
        "ranking_category_marker": "Nonfiction",
        "ranking_reporting_period_pattern": r"(?P<period>[0-9]{4}-[0-9]{2}-[0-9]{2})",
        "ranking_reporting_period_date_format": "%Y-%m-%d",
        "ranking_reporting_period_max_age_days": 14,
        "ranking_identifier_pattern": r"(?P<identifier>97[89][0-9-]{10,20})",
        "ranking_expected_raw_row_count": 20,
        "adult_nonfiction_exclusions": ["activity book"],
        "expected_page_heading": "Synthetic ranking",
        "visible_title_selector": ".heading",
        "expected_signal_strength": "top ranking",
        "eligible_candidate_category": "adult nonfiction",
    }
    source.update(overrides)
    return source


def test_ranking_table_source_must_declare_its_language() -> None:
    """An absent language reads as English and would drop the translation rule."""
    errors = validate_config({"sources": [_ranking_source()]})
    assert any("requires a nonblank language" in error for error in errors)


def test_ranking_table_source_with_a_declared_language_validates() -> None:
    assert validate_config({"sources": [_ranking_source(language="en")]}) == []


@pytest.mark.parametrize("subcommand", ["add-source", "scaffold-source"])
def test_every_operator_parser_declares_the_flag_as_a_boolean(subcommand: str) -> None:
    """Both operator parsers must use the shared helper, not their own loop.

    ``add-source`` was converted first and ``scaffold-source`` kept an inline
    copy, so the flag stayed string-typed on one path. A boolean option carries
    no metavar in the usage text; a string-typed one does.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "autoscout", subcommand, "--help"],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert "--metadata-ranking-table" in completed.stdout
    assert "--metadata-ranking-table METADATA_RANKING_TABLE" not in completed.stdout


def test_a_flag_only_ranking_table_must_also_declare_its_language() -> None:
    """The language gate resolves the family the way the prompt builders do.

    A source that sets ``metadata_ranking_table`` without a ranking row selector
    is not a ``ranking_contract`` for the declaration checks, but it is still a
    ranking table to ``contract_family``, so the rule governs it and the
    declaration it reads must be required.
    """
    flag_only = {
        "name": "Flag-only ranking table",
        "family": "bestseller/ranking",
        "url": "https://example.test/flag-only",
        "active": True,
        "access_status": "open",
        "check_frequency": "weekly",
        "metadata_ranking_table": True,
        "expected_table_headers": ["Rank"],
        "expected_visible_markers": ["Nonfiction"],
    }
    errors = validate_config({"sources": [flag_only]})
    assert any("requires a nonblank language" in error for error in errors)
    assert not any(
        "requires a nonblank language" in error
        for error in validate_config({"sources": [{**flag_only, "language": "pt"}]})
    )
