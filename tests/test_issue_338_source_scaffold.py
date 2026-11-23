"""Focused offline tests for Issue #338 probation source scaffolding."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest
from source_contract_battery import ExactBookListCase
from source_contract_battery import assert_exact_book_list_contract
from source_contract_battery import assert_source_onboarding_contract

from autoscout.__main__ import main
from autoscout.config import resolve_weekly_portfolio
from autoscout.replay_fixtures import build_replay_fixture_pair
from autoscout.source_metadata import SOURCE_LAYOUT_FIELDS
from autoscout.source_metadata import SOURCE_SCAFFOLD_METADATA_FIELDS
from autoscout.source_metadata import SOURCE_SCAFFOLD_OPTIONAL_METADATA_FIELDS
from autoscout.source_metadata import SOURCE_SCAFFOLD_REQUIRED_METADATA_FIELDS
from autoscout.source_proposals import normalize_source_identity
from autoscout.source_scaffold import ScaffoldError
from autoscout.source_scaffold import build_probation_replay_contract_metadata
from autoscout.source_scaffold import normalize_operator_metadata
from autoscout.source_scaffold import run_scaffold_probation_source

SOURCE_NAME = "Example Probation List"
SOURCE_URL = "https://example.test/probation-list"
SIGNAL_FAMILY = "award"
SIGNAL_STRENGTH = "shortlist"
CATEGORY = "adult nonfiction"
SLUG = "example-probation-list"
HEADING = "Example 2026 Nonfiction Shortlist"
SORT_LABEL = "Sorted by editorial priority"

METADATA = {
    "source_name": SOURCE_NAME,
    "url": SOURCE_URL,
    "family": SIGNAL_FAMILY,
    "access_status": "open",
    "check_frequency": "weekly",
    "language": "en",
    "country": "United States",
    "market": "United States",
    "expected_category": "Nonfiction",
    "signal_family": SIGNAL_FAMILY,
    "refresh_cadence": "weekly",
    "url_stability": "stable category page",
    "expected_page_heading": HEADING,
    "expected_sort_label": SORT_LABEL,
    "expected_signal_strength": SIGNAL_STRENGTH,
    "eligible_candidate_category": CATEGORY,
    "extraction_notes": "Metadata only: preserve exact original title, author, and grounded book URL.",
    "lifecycle_note": "Issue #338 probation source: active and reversible pending source metrics.",
    "notes": "Accepted Source Proposal; offline replay scaffold for probation review.",
}

ENTRIES = (
    ("The Quiet Archive", "Mara Ellison", f"{SOURCE_URL}/the-quiet-archive"),
    ("River of Ash", "Daniel Mercer", f"{SOURCE_URL}/river-of-ash"),
)
_HELPERS = {
    "The Quiet Archive": ("Тихий архив", "Мара Эллисон"),
    "River of Ash": ("Река пепла", "Дэниел Мерсер"),
}

RANKING_HEADING = "Example Nonfiction ranking 03/08/2026 a 09/08/2026"

RANKING_METADATA = {
    **METADATA,
    "expected_page_heading": RANKING_HEADING,
    "expected_sort_label": "",
    "family": "bestseller/ranking",
    "signal_family": "bestseller/ranking",
    "contract_family": "ranking-table",
    "ranking_row_selector": ".ranking-row",
    "ranking_rank_source": "document_order",
    "ranking_title_selector": ".ranking-title",
    "ranking_author_selector": ".ranking-author",
    "ranking_identifier_selector": ".ranking-isbn",
    "ranking_category_marker": "Nonfiction ranking",
    "ranking_reporting_period_pattern": r"(?P<period>\d{2}/\d{2}/\d{4} a \d{2}/\d{2}/\d{4})",
    "ranking_reporting_period_date_format": "%d/%m/%Y",
    "ranking_reporting_period_max_age_days": 14,
    "ranking_expected_raw_row_count": 20,
    "ranking_evidence_url": "source_page",
    "ranking_source_statement": "The source ranks nonfiction books by document order for 03/08/2026 a 09/08/2026.",
    "adult_nonfiction_exclusions": ["activity book"],
}


def _html() -> str:
    items = "".join(
        f'<li><a href="{url}">{title}</a><span class="author">{author}</span></li>' for title, author, url in ENTRIES
    )
    return f'<h1>{HEADING}</h1><p class="sort-label">{SORT_LABEL}</p><ul>{items}</ul>'


def _helper_metadata() -> dict[str, Any]:
    return {
        "version": 1,
        "source_url": SOURCE_URL,
        "language": METADATA["language"],
        "entries": [
            {
                "title": title,
                "author": author,
                "evidence_url": url,
                "english_title": title,
                "search_translation": _HELPERS[title][0],
                "author_transliteration_note": _HELPERS[title][1],
            }
            for title, author, url in ENTRIES
        ],
    }


def _ranking_isbn(seed: int) -> str:
    digits = f"97803064{seed:04d}"
    checksum = sum((1 if index % 2 == 0 else 3) * int(value) for index, value in enumerate(digits))
    return digits + str((10 - checksum % 10) % 10)


def _ranking_evidence() -> dict[str, Any]:
    rows = "".join(
        f'<div class="ranking-row"><span class="ranking-title">Ranking Book {rank}</span>'
        f'<span class="ranking-author">Ranking Author {rank}</span>'
        f'<span class="ranking-isbn">{_ranking_isbn(rank)}</span></div>'
        for rank in range(1, 21)
    )
    return {
        "collection_mode": "source-candidate-probe",
        "source_url": SOURCE_URL,
        "source_name": SOURCE_NAME,
        "source_page": SOURCE_URL,
        "name": SOURCE_NAME,
        "signal_family": RANKING_METADATA["signal_family"],
        "resolved_url": SOURCE_URL,
        "head_status": 200,
        "get_status": 200,
        "reference_date": "2026-08-10",
        "html": (
            f"<html><body><h1>{RANKING_HEADING}</h1>{rows}"
            f"<p>Nonfiction ranking: {RANKING_METADATA['ranking_source_statement']}</p></body></html>"
        ),
        "detected_heading": RANKING_HEADING,
        "bounded_entry_count": 20,
        "contract_family": "ranking-table",
        "contract_metadata": dict(RANKING_METADATA),
        "ranking_source_statement": RANKING_METADATA["ranking_source_statement"],
    }


def _ranking_helper_metadata() -> dict[str, Any]:
    return {
        "version": 1,
        "source_url": SOURCE_URL,
        "language": RANKING_METADATA["language"],
        "entries": [
            {
                "title": f"Ranking Book {rank}",
                "author": f"Ranking Author {rank}",
                "evidence_url": SOURCE_URL,
                "english_title": f"Ranking Book {rank}",
                "search_translation": f"Рейтинговая книга {rank}",
                "author_transliteration_note": f"Автор рейтинга {rank}",
            }
            for rank in range(1, 4)
        ],
    }


def _evidence(*, include_sort_label: bool = True, **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "collection_mode": "source-candidate-probe",
        "source_url": SOURCE_URL,
        "source_name": SOURCE_NAME,
        "source_page": SOURCE_URL,
        "name": SOURCE_NAME,
        "signal_family": SIGNAL_FAMILY,
        "resolved_url": SOURCE_URL,
        "head_status": 200,
        "get_status": 200,
        "html": _html() if include_sort_label else _html().replace(f'<p class="sort-label">{SORT_LABEL}</p>', ""),
        "detected_heading": HEADING,
        "bounded_entry_count": len(ENTRIES),
    }
    value.update(overrides)
    return value


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "evidence_path": tmp_path / "evidence.json",
        "helper_metadata_path": tmp_path / "helper-metadata.json",
        "page_fixture_path": tmp_path / "fixtures" / "page.json",
        "extraction_fixture_path": tmp_path / "fixtures" / "extraction.json",
        "proposal_file": tmp_path / "state" / "source-proposals.json",
        "portfolio_config": tmp_path / "configs" / "portfolio.json",
        "pilot_config": tmp_path / "configs" / "pilot.json",
        "pilot_live_config": tmp_path / "configs" / "pilot-live.json",
        "live_config": tmp_path / "configs" / "live.json",
    }


def _ready_case(
    tmp_path: Path,
    *,
    fixture_sort_label: str = SORT_LABEL,
    metadata: dict[str, Any] = METADATA,
    evidence: dict[str, Any] | None = None,
    helper_metadata: dict[str, Any] | None = None,
) -> dict[str, Path]:
    paths = _paths(tmp_path)
    paths["evidence_path"].parent.mkdir(parents=True, exist_ok=True)
    evidence = evidence or _evidence(include_sort_label=bool(fixture_sort_label))
    helper_metadata = helper_metadata or _helper_metadata()
    normalized_metadata = normalize_operator_metadata(metadata)
    _write_json(paths["evidence_path"], evidence)
    _write_json(paths["helper_metadata_path"], helper_metadata)
    pair = build_replay_fixture_pair(
        normalized_metadata["url"],
        SLUG,
        evidence,
        source_name=normalized_metadata["source_name"],
        signal_family=normalized_metadata["signal_family"],
        contract_family=str(normalized_metadata.get("contract_family", "")),
        contract_metadata=build_probation_replay_contract_metadata(normalized_metadata),
        signal_strength=normalized_metadata["expected_signal_strength"],
        candidate_category=normalized_metadata["eligible_candidate_category"],
        language=normalized_metadata["language"],
        expected_sort_label=fixture_sort_label,
        helper_metadata_file=paths["helper_metadata_path"],
    )
    paths["page_fixture_path"].parent.mkdir(parents=True)
    paths["page_fixture_path"].write_bytes(pair.page_bytes)
    paths["extraction_fixture_path"].write_bytes(pair.extraction_bytes)
    paths["proposal_file"].parent.mkdir(parents=True)
    _write_json(
        paths["proposal_file"],
        [
            {
                "id": "proposal-338-example",
                "source_url": str(metadata["url"]),
                "normalized_source": normalize_source_identity(str(metadata["url"])),
                "source_name": str(metadata["source_name"]),
                "status": "accepted",
            }
        ],
    )
    portfolio = {"sources": [{"name": "Existing Source", "url": "https://existing.test", "language": "en"}]}
    paths["portfolio_config"].parent.mkdir(parents=True, exist_ok=True)
    _write_json(paths["portfolio_config"], portfolio)
    for key in ("pilot_config", "pilot_live_config", "live_config"):
        _write_json(
            paths[key],
            {
                "weekly_portfolio": "portfolio.json",
                "sources": [{"name": "Existing Source"}],
                "pilot_mode": True,
            },
        )
    return paths


def _call(
    paths: dict[str, Path], metadata: dict[str, Any] | None = None, **overrides: Any
) -> tuple[dict[str, Any], int]:
    arguments: dict[str, Any] = {
        "probe_evidence": paths["evidence_path"],
        "helper_metadata_file": paths["helper_metadata_path"],
        "page_fixture": paths["page_fixture_path"],
        "extraction_fixture": paths["extraction_fixture_path"],
        "proposal_file": paths["proposal_file"],
        "proposal_id": "proposal-338-example",
        "slug": SLUG,
        "portfolio_config": paths["portfolio_config"],
        "pilot_config": paths["pilot_config"],
        "pilot_live_config": paths["pilot_live_config"],
        "live_config": paths["live_config"],
        "metadata": metadata or METADATA,
    }
    arguments.update(overrides)
    return run_scaffold_probation_source(**arguments)


def test_layout_metadata_is_normalized_and_optional_fields_stay_absent() -> None:
    metadata = dict(METADATA)
    metadata.update(
        {
            "entry_scope_selector": "  .ranking-row  ",
            "ranking_source_statement": "  Rank is assigned by the source.  ",
            "visible_title_selector": "  .page-title  ",
            "expected_reporting_period": "  03/08/2026 a 09/08/2026  ",
            "ranking_expected_path": "  /probation-list  ",
        }
    )

    normalized = normalize_operator_metadata(metadata)

    assert {field: normalized[field] for field in SOURCE_LAYOUT_FIELDS} == {
        "entry_scope_selector": ".ranking-row",
        "ranking_source_statement": "Rank is assigned by the source.",
        "visible_title_selector": ".page-title",
        "expected_reporting_period": "03/08/2026 a 09/08/2026",
        "ranking_expected_path": "/probation-list",
    }
    without_layout = normalize_operator_metadata(METADATA)
    assert not set(SOURCE_LAYOUT_FIELDS) & without_layout.keys()


@pytest.mark.parametrize("field", SOURCE_LAYOUT_FIELDS)
def test_layout_metadata_requires_a_nonblank_string(field: str) -> None:
    metadata = dict(METADATA)
    metadata[field] = " "

    with pytest.raises(ScaffoldError, match=field):
        normalize_operator_metadata(metadata)


def test_canonical_scaffold_metadata_includes_all_operator_fields() -> None:
    expected = {
        "source_name",
        "url",
        "family",
        "access_status",
        "check_frequency",
        "language",
        "country",
        "market",
        "expected_category",
        "signal_family",
        "refresh_cadence",
        "url_stability",
        "extraction_notes",
        "expected_page_heading",
        "expected_sort_label",
        "expected_signal_strength",
        "eligible_candidate_category",
        "lifecycle_note",
        "notes",
    }
    assert set(SOURCE_SCAFFOLD_METADATA_FIELDS) == expected
    assert set(SOURCE_SCAFFOLD_OPTIONAL_METADATA_FIELDS) == {"expected_sort_label"}
    assert "expected_sort_label" not in SOURCE_SCAFFOLD_REQUIRED_METADATA_FIELDS
    assert not {"active", "metadata_exact_book_list", "lifecycle_status"} & set(SOURCE_SCAFFOLD_METADATA_FIELDS)


def test_public_ranking_table_scaffold_uses_full_live_budget(tmp_path: Path) -> None:
    paths = _ready_case(
        tmp_path,
        fixture_sort_label="",
        metadata=RANKING_METADATA,
        evidence=_ranking_evidence(),
        helper_metadata=_ranking_helper_metadata(),
    )
    report, code = _call(paths, metadata=RANKING_METADATA)

    assert code == 0
    assert report["status"] == "created"
    page = json.loads(paths["page_fixture_path"].read_text(encoding="utf-8"))
    assert page["contract_metadata"]["lifecycle_status"] == "probation"
    for key in ("pilot_live_config", "live_config"):
        source = json.loads(paths[key].read_text(encoding="utf-8"))["sources"][-1]
        assert source["model_max_output_tokens"] == 8000


def test_success_writes_helper_sidecar_runtime_reference_atomically(tmp_path: Path) -> None:
    paths = _ready_case(tmp_path)
    report, code = _call(paths)
    assert code == 0
    assert report["status"] == "created"

    expected = "../fixtures/example-probation-list-helper-metadata.json"
    portfolio_source = json.loads(paths["portfolio_config"].read_text(encoding="utf-8"))["sources"][-1]
    expected_digest = hashlib.sha256(paths["helper_metadata_path"].read_bytes()).hexdigest()
    assert portfolio_source["helper_metadata_file"] == expected
    assert portfolio_source["helper_metadata_sha256"] == expected_digest
    for key in ("pilot_config", "pilot_live_config", "live_config"):
        overlay = json.loads(paths[key].read_text(encoding="utf-8"))["sources"][-1]
        assert overlay["helper_metadata_file"] == expected
        assert overlay["helper_metadata_sha256"] == expected_digest


def test_success_writes_four_config_bundle_with_explicit_metadata(tmp_path: Path) -> None:
    paths = _ready_case(tmp_path)
    proposal_before = paths["proposal_file"].read_bytes()

    report, code = _call(paths)

    assert code == 0
    assert report["status"] == "created"
    assert report["source_name"] == SOURCE_NAME
    assert paths["proposal_file"].read_bytes() == proposal_before

    portfolio = json.loads(paths["portfolio_config"].read_text(encoding="utf-8"))
    source = portfolio["sources"][-1]
    assert source["active"] is True
    assert source["metadata_exact_book_list"] is True
    assert source["lifecycle_status"] == "probation"
    for key, value in METADATA.items():
        config_key = "name" if key == "source_name" else key
        assert source[config_key] == value

    pilot = json.loads(paths["pilot_config"].read_text(encoding="utf-8"))
    pilot_source = pilot["sources"][-1]
    assert_source_onboarding_contract(
        source,
        pilot_source,
        page_fixture=paths["page_fixture_path"],
        extraction_fixture=paths["extraction_fixture_path"],
    )
    assert pilot_source["name"] == SOURCE_NAME
    assert pilot_source["extraction_method"] == "codex-extraction-replay"
    assert (paths["pilot_config"].parent / pilot_source["source_page_fixture_file"]).resolve() == paths[
        "page_fixture_path"
    ].resolve()
    assert (paths["pilot_config"].parent / pilot_source["extraction_response_file"]).resolve() == paths[
        "extraction_fixture_path"
    ].resolve()

    for key in ("pilot_live_config", "live_config"):
        live_source = json.loads(paths[key].read_text(encoding="utf-8"))["sources"][-1]
        assert live_source["name"] == SOURCE_NAME
        assert live_source["extraction_method"] == "agent-extraction-live"
        assert live_source["capture_live_response_file"] == f"{SLUG}-page-capture.json"
        assert live_source["capture_extraction_response_file"] == f"{SLUG}-extraction-capture.json"
        assert live_source["model_max_output_tokens"] == 1200
        assert "source_page_fixture_file" not in live_source
        assert "extraction_response_file" not in live_source


def test_scaffold_pair_passes_shared_exact_book_list_contract_battery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _ready_case(tmp_path)
    report, code = _call(paths)
    assert code == 0
    assert report["status"] == "created"

    configs = {
        key: json.loads(paths[key].read_text(encoding="utf-8"))
        for key in ("portfolio_config", "pilot_config", "pilot_live_config", "live_config")
    }
    sequences = [[source["name"] for source in config["sources"]] for config in configs.values()]
    assert sequences == [sequences[0]] * 4

    source = configs["portfolio_config"]["sources"][-1]
    resolved_pilot_source = {**source, **configs["pilot_config"]["sources"][-1]}
    resolved_pilot_source["source_page_fixture_file"] = str(paths["page_fixture_path"])
    resolved_pilot_source["extraction_response_file"] = str(paths["extraction_fixture_path"])
    resolved_pilot_source["helper_metadata_file"] = str(paths["helper_metadata_path"])
    case = ExactBookListCase(
        SOURCE_NAME,
        resolved_pilot_source,
        json.loads(paths["page_fixture_path"].read_text(encoding="utf-8")),
        json.loads(paths["extraction_fixture_path"].read_text(encoding="utf-8")),
    )
    assert_exact_book_list_contract(case, tmp_path / "contract-battery", monkeypatch)


def test_standard_temporary_directory_alias_is_accepted() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        if not temporary_path.is_relative_to(Path(tempfile.gettempdir())):
            pytest.skip("temporary directory is not below the standard temporary root")
        paths = _ready_case(temporary_path)
        report, code = _call(paths)
        assert code == 0
        assert report["status"] == "created"


def test_expected_sort_label_is_optional_and_not_synthesized(tmp_path: Path) -> None:
    paths = _ready_case(tmp_path, fixture_sort_label="")
    metadata = dict(METADATA)
    metadata.pop("expected_sort_label")

    report, code = _call(paths, metadata)

    assert code == 0
    assert report["status"] == "created"
    portfolio = json.loads(paths["portfolio_config"].read_text(encoding="utf-8"))
    source = portfolio["sources"][-1]
    assert "expected_sort_label" not in source
    page = json.loads(paths["page_fixture_path"].read_text(encoding="utf-8"))
    assert SORT_LABEL not in page["html"]


def test_supplied_sort_label_missing_from_evidence_is_rejected_without_writes(tmp_path: Path) -> None:
    paths = _ready_case(tmp_path, fixture_sort_label="")
    before = {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")}

    with pytest.raises(ScaffoldError, match="expected sort label"):
        _call(paths)

    assert {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")} == before


@pytest.mark.parametrize(
    "lifecycle_note", ["A routine source note.", "Issue 338 is reversible.", "Issue #338 is pending metrics."]
)
def test_invalid_lifecycle_note_is_rejected_before_any_config_write(tmp_path: Path, lifecycle_note: str) -> None:
    paths = _ready_case(tmp_path)
    before = {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")}

    metadata = dict(METADATA)
    metadata["lifecycle_note"] = lifecycle_note
    with pytest.raises(ScaffoldError, match="lifecycle_note"):
        _call(paths, metadata)

    assert {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")} == before


def test_lifecycle_note_is_case_insensitive_and_preserved_exactly(tmp_path: Path) -> None:
    paths = _ready_case(tmp_path)
    metadata = dict(METADATA)
    metadata["lifecycle_note"] = "  ISSUE #338: Evaluation is REVERSIBLE pending metrics.  "

    report, code = _call(paths, metadata)

    assert code == 0
    assert report["status"] == "created"
    source = json.loads(paths["portfolio_config"].read_text(encoding="utf-8"))["sources"][-1]
    assert source["lifecycle_note"] == metadata["lifecycle_note"]


def test_portfolio_name_overlay_can_override_resolved_source_name(tmp_path: Path) -> None:
    paths = _ready_case(tmp_path)
    for key in ("pilot_config", "pilot_live_config", "live_config"):
        config = json.loads(paths[key].read_text(encoding="utf-8"))
        config["sources"][0] = {
            "portfolio_name": "Existing Source",
            "name": "Existing Overlay Name",
        }
        _write_json(paths[key], config)

    report, code = _call(paths)

    assert code == 0
    assert report["status"] == "created"
    for key in ("pilot_config", "pilot_live_config", "live_config"):
        config = json.loads(paths[key].read_text(encoding="utf-8"))
        assert config["sources"][0] == {
            "portfolio_name": "Existing Source",
            "name": "Existing Overlay Name",
        }
        assert config["sources"][1]["name"] == SOURCE_NAME
        resolved = resolve_weekly_portfolio(config, paths[key])
        assert [item["name"] for item in resolved["sources"]] == ["Existing Overlay Name", SOURCE_NAME]


def test_partial_overlay_bundle_is_rejected_without_config_mutation(tmp_path: Path) -> None:
    paths = _ready_case(tmp_path)
    pilot = json.loads(paths["pilot_config"].read_text(encoding="utf-8"))
    pilot["sources"].pop()
    _write_json(paths["pilot_config"], pilot)
    before = {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")}

    with pytest.raises(ScaffoldError, match="source-name sequence"):
        _call(paths)

    assert {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")} == before


@pytest.mark.parametrize(
    "proposal_update, message",
    [
        ({"status": "proposed"}, "accepted"),
        ({"source_name": "Other Source"}, "name alignment"),
        ({"source_url": "https://other.test/list"}, "identity"),
    ],
)
def test_proposal_gate_rejects_without_mutating_proposal_or_configs(
    tmp_path: Path, proposal_update: dict[str, str], message: str
) -> None:
    paths = _ready_case(tmp_path)
    proposal = json.loads(paths["proposal_file"].read_text(encoding="utf-8"))
    proposal[0].update(proposal_update)
    _write_json(paths["proposal_file"], proposal)
    before = {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")}
    proposal_before = paths["proposal_file"].read_bytes()

    with pytest.raises(ScaffoldError, match=message):
        _call(paths)

    assert {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")} == before
    assert paths["proposal_file"].read_bytes() == proposal_before


def test_accepted_proposal_without_source_name_is_allowed(tmp_path: Path) -> None:
    paths = _ready_case(tmp_path)
    proposal = json.loads(paths["proposal_file"].read_text(encoding="utf-8"))
    proposal[0].pop("source_name")
    _write_json(paths["proposal_file"], proposal)
    proposal_before = paths["proposal_file"].read_bytes()

    report, code = _call(paths)

    assert code == 0
    assert report["status"] == "created"
    assert paths["proposal_file"].read_bytes() == proposal_before


@pytest.mark.parametrize("declared_outcome", ["reject", "defer", "stale"])
def test_explicit_nonaccepted_evidence_outcome_is_rejected(tmp_path: Path, declared_outcome: str) -> None:
    paths = _ready_case(tmp_path)
    _write_json(paths["evidence_path"], _evidence(outcome=declared_outcome))
    before = {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")}

    with pytest.raises(ScaffoldError, match="non-accepted outcome"):
        _call(paths)

    assert {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")} == before


def test_duplicate_source_name_is_refused(tmp_path: Path) -> None:
    paths = _ready_case(tmp_path)
    portfolio = json.loads(paths["portfolio_config"].read_text(encoding="utf-8"))
    portfolio["sources"].append({"name": SOURCE_NAME, "url": "https://other.test", "language": "en"})
    _write_json(paths["portfolio_config"], portfolio)
    before = {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")}

    with pytest.raises(ScaffoldError, match="source-name sequence|duplicate source name"):
        _call(paths)

    assert {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")} == before


def test_configuration_destination_cannot_alias_an_input_file(tmp_path: Path) -> None:
    paths = _ready_case(tmp_path)
    before = {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")}

    with pytest.raises(ScaffoldError, match="alias an input artifact"):
        _call(paths, portfolio_config=paths["evidence_path"])

    assert {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")} == before


def test_arbitrary_symlink_ancestor_is_rejected(tmp_path: Path) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    linked_directory = tmp_path / "arbitrary-link"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    paths = _ready_case(linked_directory)

    with pytest.raises(ScaffoldError, match="symlink ancestor"):
        _call(paths)


def test_hard_linked_configuration_destination_is_rejected(tmp_path: Path) -> None:
    paths = _ready_case(tmp_path)
    hard_link = tmp_path / "portfolio-hard-link.json"
    os.link(paths["portfolio_config"], hard_link)

    with pytest.raises(ScaffoldError, match="hard link"):
        _call(paths, portfolio_config=hard_link)


def test_duplicate_overlay_identity_is_refused(tmp_path: Path) -> None:
    paths = _ready_case(tmp_path)
    pilot = json.loads(paths["pilot_config"].read_text(encoding="utf-8"))
    pilot["sources"].append({"name": "Existing Source"})
    _write_json(paths["pilot_config"], pilot)
    before = {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")}

    with pytest.raises(ScaffoldError, match="duplicate overlay identity"):
        _call(paths)

    assert {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")} == before


def test_stale_or_mismatched_evidence_and_pair_are_refused(tmp_path: Path) -> None:
    paths = _ready_case(tmp_path)
    stale = _evidence(html=_html().replace("The Quiet Archive", "Changed Book"))
    _write_json(paths["evidence_path"], stale)
    with pytest.raises(ScaffoldError, match="fixture pair|helper metadata"):
        _call(paths)
    assert (
        not json.loads(paths["portfolio_config"].read_text(encoding="utf-8"))["sources"][-1].get("name") == SOURCE_NAME
    )

    paths = _ready_case(tmp_path / "url-mismatch")
    _write_json(paths["evidence_path"], _evidence(source_url="https://other.test/list"))
    with pytest.raises(ScaffoldError, match="different URL"):
        _call(paths)


def test_atomic_bundle_rolls_back_all_four_files_and_staging(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths = _ready_case(tmp_path)
    before = {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")}
    import autoscout.source_scaffold as scaffold

    calls = 0

    def fail_on_second_replace(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected replacement failure")
        os.replace(source, destination)

    monkeypatch.setattr(scaffold, "_staged_replace", fail_on_second_replace)
    with pytest.raises(ScaffoldError, match="replacement failure"):
        _call(paths)

    assert {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")} == before
    for config_path in paths.values():
        if config_path.suffix == ".json" and config_path.parent.exists():
            assert not list(config_path.parent.glob("*.stage"))
            assert not list(config_path.parent.glob("*.backup"))


def test_exact_retry_is_idempotent(tmp_path: Path) -> None:
    paths = _ready_case(tmp_path)
    first, first_code = _call(paths)
    before = {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")}

    second, second_code = _call(paths)

    assert first_code == second_code == 0
    assert first["status"] == "created"
    assert second["status"] == "unchanged"
    assert {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")} == before


@pytest.mark.parametrize("command", ["scaffold-probation-source", "scaffold-source-onboarding"])
def test_unrequested_scaffold_command_aliases_are_not_registered(command: str, capsys: Any) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main([command])
    assert exit_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_cli_allows_omitted_expected_sort_label(tmp_path: Path, capsys: Any) -> None:
    paths = _ready_case(tmp_path, fixture_sort_label="")
    argv = ["scaffold-source"]
    for option, key in (
        ("--probe-evidence", "evidence_path"),
        ("--helper-metadata", "helper_metadata_path"),
        ("--page-fixture", "page_fixture_path"),
        ("--extraction-fixture", "extraction_fixture_path"),
        ("--proposals-file", "proposal_file"),
        ("--portfolio-config", "portfolio_config"),
        ("--pilot-config", "pilot_config"),
        ("--pilot-live-config", "pilot_live_config"),
        ("--live-config", "live_config"),
    ):
        argv.extend([option, str(paths[key])])
    argv.extend(["--proposal-id", "proposal-338-example", "--slug", SLUG])
    for key, value in METADATA.items():
        if key == "expected_sort_label":
            continue
        argv.extend(["--" + key.replace("_", "-"), value])

    assert main(argv) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "created"
    source = json.loads(paths["portfolio_config"].read_text(encoding="utf-8"))["sources"][-1]
    assert "expected_sort_label" not in source


def test_cli_success_is_structured_json(tmp_path: Path, capsys: Any) -> None:
    paths = _ready_case(tmp_path)
    argv = ["scaffold-source"]
    for option, key in (
        ("--probe-evidence", "evidence_path"),
        ("--helper-metadata", "helper_metadata_path"),
        ("--page-fixture", "page_fixture_path"),
        ("--extraction-fixture", "extraction_fixture_path"),
        ("--proposals-file", "proposal_file"),
        ("--portfolio-config", "portfolio_config"),
        ("--pilot-config", "pilot_config"),
        ("--pilot-live-config", "pilot_live_config"),
        ("--live-config", "live_config"),
    ):
        argv.extend([option, str(paths[key])])
    argv.extend(["--proposal-id", "proposal-338-example", "--slug", SLUG])
    for key, value in METADATA.items():
        option = "--" + key.replace("_", "-")
        argv.extend([option, value])

    assert main(argv) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "created"
    assert report["exit_code"] == 0
