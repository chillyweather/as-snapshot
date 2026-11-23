"""Acceptance tests for issue #328: candidate-quality diagnostics in Search Optimization Review.

Uses a deterministic pipeline fixture exercising all five initial
diagnostic classes while preserving canonical artifact output.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

from state_store_support import seed_state
from state_store_support import sync_state

from autoscout.pipeline import run_pipeline

RUN_DATE = "2026-09-01"

DIAGNOSTIC_CLASS_IDS = (
    "already_known_in_normal_review",
    "reject_resurfaced_without_material_change",
    "probable_duplicate_identity_fragmentation",
    "scouting_candidate_definition_conflict",
    "missing_trial_structured_data",
)

DIAGNOSTIC_LABELS = {
    "already_known_in_normal_review": "Already Known candidate in normal review",
    "reject_resurfaced_without_material_change": "Reject candidate resurfaced without Material Change",
    "probable_duplicate_identity_fragmentation": "Probable duplicate identity fragmentation",
    "scouting_candidate_definition_conflict": "Scouting Candidate definition conflict",
    "missing_trial_structured_data": "Missing structured data required to evaluate the trial",
}


def _candidate(title: str, author: str, signal_strength: str, category: str = "history") -> dict[str, object]:
    return {
        "title": title,
        "primary_authors": [author],
        "candidate_category": category,
        "signal_strength": signal_strength,
        "evidence_note": f"{title} appears in the configured fixture.",
        "publisher_fit": "clear fit",
        "russian_edition_status": "not found in Russian",
        "visible_risk_status": "no visible risk",
    }


def _source(
    name: str,
    family: str,
    candidates: list[dict[str, object]],
    *,
    collector_mentions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "name": name,
        "family": family,
        "url": f"https://example.test/{name.lower().replace(' ', '-')}",
        "access_status": "open",
        "extraction_method": "fixture",
        "check_frequency": "weekly",
        "fixture_candidates": candidates,
    }
    if collector_mentions:
        result["collector_mentions"] = collector_mentions
    return result


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def _build_diagnostics_fixture(
    root: Path,
    *,
    trial_enabled: bool = True,
) -> tuple[Path, Path]:
    """Build a fixture exercising all five diagnostic classes.

    Classes exercised:
    1. already_known_in_normal_review -- "Already Known Work" has history status
       already_known, still appears in normal review (Material Change).
    2. reject_resurfaced_without_material_change -- "Rejected No Change" has
       history status reject, no Material Change -> suppressed.
    3. probable_duplicate_identity_fragmentation -- "Fragmented Work" has
       two mentions with a shared author and therefore must be grouped as one
       candidate under the shared-author identity rule.
    4. scouting_candidate_definition_conflict -- a collector_mentions entry
       with underlying_work_type="podcast" (non-book/work) plus a candidate
       with an ineligible category excluded via internal_exclusions.
    5. missing_trial_structured_data -- a candidate missing candidate_category
       (no category in the source mention).
    """
    root.mkdir(parents=True, exist_ok=True)

    # ── Candidates ──────────────────────────────────────────────────
    already_known_candidate = _candidate("Already Known Work", "Known Author", "winner")

    rejected_candidate = _candidate("Rejected No Change", "Reject Author", "minor mention")

    # Shared-author pair: the pipeline must group these mentions as one work.
    duplicate_a = _candidate("Fragmented Work", "Author Alpha", "shortlist")
    duplicate_b = _candidate("Fragmented Work: The Sequel", "Author Beta", "shortlist")
    # The subtitle is ignored for work-title matching, and both mentions
    # share Author Alpha despite their different secondary contributors.
    duplicate_a["primary_authors"] = ["Author Alpha", "Author Beta"]
    duplicate_b["primary_authors"] = ["Author Alpha", "Author Gamma"]

    ineligible_candidate = _candidate("Fiction Excluded", "Fiction Author", "minor mention", category="fiction")

    missing_category_candidate = _candidate("No Category Work", "Missing Author", "winner")
    missing_category_candidate["candidate_category"] = ""  # Missing category.

    normal_candidate = _candidate("Normal Reference", "Normal Author", "major media list")

    all_fixture_candidates = [
        already_known_candidate,
        rejected_candidate,
        duplicate_a,
        duplicate_b,
        ineligible_candidate,
        missing_category_candidate,
        normal_candidate,
    ]

    # ── Collector mention for work-type conflict ───────────────────
    # A collector mention with underlying_work_type="podcast" (non-book/work).
    # This survives into source_mentions through the schema and reaches
    # normal review because the award source family does not gate on
    # underlying_work_type. The diagnostic reports it as a definition conflict
    # with canonical_gate="normal-review".
    podcast_mention: dict[str, object] = {
        "title": "Podcast-Only Work",
        "primary_authors": ["Podcast Author"],
        "candidate_category": "history",
        "signal_strength": "minor mention",
        "signal_type": "award",
        "evidence_note": "Podcast mention from a generic discovery source.",
        "source_url": "https://example.test/fixture-awards",
        "underlying_work_type": "podcast",
        "publication_status": "published",
    }

    # ── Sources ────────────────────────────────────────────────────
    config_path = root / "config.json"
    _write_json(
        config_path,
        {
            "run_date": RUN_DATE,
            "search_optimization_trial": trial_enabled,
            "sources": [
                _source(
                    "Fixture Awards",
                    "award",
                    all_fixture_candidates,
                    collector_mentions=[podcast_mention],
                ),
            ],
        },
    )

    # ── State: Candidate History ───────────────────────────────────
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    history = []

    # already_known status -- normal review candidate (Material Change brings it in).
    history.append(
        {
            "identity_key": "already known work|known author",
            "title": "Already Known Work",
            "primary_authors": ["Known Author"],
            "editor_status": "already_known",
            "editor_note": "Already evaluated and acquired.",
            "last_seen_date": "2026-08-25",
            "source_evidence": [
                {"source_name": "Fixture Awards", "source_family": "award", "signal_strength": "minor mention"},
            ],
            "independent_signal_families": ["award"],
            "primary_signal_strength": "minor mention",
            "priority_label": "C",
            "russian_edition_status": "not found in Russian",
            "visible_risk_status": "no visible risk",
        }
    )

    # reject status, with weak history evidence (no Material Change -> suppressed).
    history.append(
        {
            "identity_key": "rejected no change|reject author",
            "title": "Rejected No Change",
            "primary_authors": ["Reject Author"],
            "editor_status": "reject",
            "editor_note": "Not the right fit.",
            "last_seen_date": "2026-08-25",
            "source_evidence": [
                {"source_name": "Fixture Awards", "source_family": "award", "signal_strength": "minor mention"},
            ],
            "independent_signal_families": ["award"],
            "primary_signal_strength": "minor mention",
            "priority_label": "C",
            "russian_edition_status": "not found in Russian",
            "visible_risk_status": "no visible risk",
        }
    )

    seed_state(state_dir, candidate_history=history)
    seed_state(state_dir, editorial_profile={"entries": []})
    seed_state(state_dir, candidate_visibility=[])

    output_dir = root / "run"
    run_pipeline(config_path, output_dir, state_dir)
    return output_dir, state_dir


class TestIssue328CandidateQualityDiagnosticsTests:
    """Acceptance tests for the #328 candidate-quality diagnostics contract."""

    # ── Fixture expectations ────────────────────────────────────────

    def _load_review(self, output_dir: Path) -> Any:
        with (output_dir / "search-optimization-review.json").open(encoding="utf-8") as f:
            return json.load(f)

    def _load_markdown(self, output_dir: Path) -> str:
        return (output_dir / "search-optimization-review.md").read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Schema and structure
    # ------------------------------------------------------------------

    def test_schema_version_is_4(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_diagnostics_fixture(tmp_path)
        review = self._load_review(output_dir)
        assert review["schema_version"] == 4

    def test_top_level_field_present(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_diagnostics_fixture(tmp_path)
        review = self._load_review(output_dir)
        assert "candidate_quality_diagnostics" in review
        cq = review["candidate_quality_diagnostics"]
        assert "section_non_effects" in cq
        assert "classes" in cq
        assert len(cq["section_non_effects"]) >= 1

    def test_all_five_classes_present(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_diagnostics_fixture(tmp_path)
        review = self._load_review(output_dir)
        classes = review["candidate_quality_diagnostics"]["classes"]
        for class_id in DIAGNOSTIC_CLASS_IDS:
            assert class_id in classes, f"Missing diagnostic class: {class_id}"

    def test_each_class_has_label_count_rank_effect_findings(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_diagnostics_fixture(tmp_path)
        review = self._load_review(output_dir)
        for class_id in DIAGNOSTIC_CLASS_IDS:
            class_data = review["candidate_quality_diagnostics"]["classes"][class_id]
            assert "label" in class_data, f"Missing label in {class_id}"
            assert "count" in class_data, f"Missing count in {class_id}"
            assert isinstance(class_data["count"], int), f"Count not int in {class_id}"
            assert "rank_effect" in class_data, f"Missing rank_effect in {class_id}"
            assert "findings" in class_data, f"Missing findings in {class_id}"
            assert isinstance(class_data["findings"], list), f"Findings not list in {class_id}"

    # ------------------------------------------------------------------
    # Class 1: already_known_in_normal_review
    # ------------------------------------------------------------------

    def test_already_known_detected(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_diagnostics_fixture(tmp_path)
        review = self._load_review(output_dir)
        ak_class = review["candidate_quality_diagnostics"]["classes"]["already_known_in_normal_review"]
        assert ak_class["count"] == 1
        assert ak_class["findings"][0]["identity_key"] == "already known work|known author"
        assert ak_class["findings"][0]["title"] == "Already Known Work"
        assert ak_class["findings"][0]["editor_status"] == "already_known"
        # Already Known candidate must still be in the normal review (eligible set).
        candidate_ids = {c["identity_key"] for c in review["candidates"]}
        assert "already known work|known author" in candidate_ids

    # ------------------------------------------------------------------
    # Class 2: reject_resurfaced_without_material_change
    # ------------------------------------------------------------------

    def test_reject_resurface_detected(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_diagnostics_fixture(tmp_path)
        review = self._load_review(output_dir)
        rej_class = review["candidate_quality_diagnostics"]["classes"]["reject_resurfaced_without_material_change"]
        assert rej_class["count"] == 1
        assert rej_class["findings"][0]["identity_key"] == "rejected no change|reject author"
        assert rej_class["findings"][0]["title"] == "Rejected No Change"
        assert rej_class["findings"][0]["editor_status"] == "reject"
        # Reject candidate must NOT be in the eligible set.
        candidate_ids = {c["identity_key"] for c in review["candidates"]}
        assert "rejected no change|reject author" not in candidate_ids

    # ------------------------------------------------------------------
    # Class 3: probable_duplicate_identity_fragmentation
    # ------------------------------------------------------------------

    def test_shared_author_mentions_are_not_reported_as_fragmentation(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_diagnostics_fixture(tmp_path)
        review = self._load_review(output_dir)
        dup_class = review["candidate_quality_diagnostics"]["classes"]["probable_duplicate_identity_fragmentation"]
        assert dup_class["count"] == 0
        assert dup_class["findings"] == []
        candidate_ids = {c["identity_key"] for c in review["candidates"] if "fragmented work" in c["identity_key"]}
        assert candidate_ids == {"fragmented work|author alpha & author beta"}

    # ------------------------------------------------------------------
    # Class 4: scouting_candidate_definition_conflict
    # ------------------------------------------------------------------

    def test_definition_conflict_detected(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_diagnostics_fixture(tmp_path)
        review = self._load_review(output_dir)
        conflict_class = review["candidate_quality_diagnostics"]["classes"]["scouting_candidate_definition_conflict"]
        # Expect exactly 2 findings:
        # 1. Fiction category exclusion (INELIGIBLE_CANDIDATE_CATEGORIES).
        # 2. Podcast work_type reaching normal review.
        assert conflict_class["count"] == 2

        findings = conflict_class["findings"]

        # Fiction Excluded candidate was removed because category "fiction" is ineligible.
        fiction_finding = next(f for f in findings if "fiction" in f.get("identity_key", "").lower())
        assert fiction_finding["field"] == "candidate_category"
        assert fiction_finding["field_value"] == "fiction"
        assert fiction_finding["canonical_gate"] == "category-exclusion"

        # Fiction candidate must NOT be in the eligible set.
        candidate_ids = {c["identity_key"] for c in review["candidates"]}
        assert "fiction excluded|fiction author" not in candidate_ids

        # Podcast work_type finding -- survives into source_mentions and passes
        # can_create_source_mention because award family does not gate on it.
        podcast_finding = next(f for f in findings if f["field"] == "underlying_work_type")
        assert podcast_finding["field_value"] == "podcast"
        assert podcast_finding["canonical_gate"] == "normal-review"
        # Normal-review means it is in the eligible set.
        assert podcast_finding["identity_key"] in candidate_ids
        # Verify source identity present.
        assert len(podcast_finding["source_identities"]) >= 1
        assert podcast_finding["source_identities"][0]["source_name"] == "Fixture Awards"
        # Work-type finding must have a diagnostic flag on its comparison record.
        pod_candidate = next(c for c in review["candidates"] if c["identity_key"] == podcast_finding["identity_key"])
        assert "scouting_candidate_definition_conflict" in pod_candidate.get("diagnostic_flags", [])

    # ------------------------------------------------------------------
    # Class 5: missing_trial_structured_data
    # ------------------------------------------------------------------

    def test_missing_data_detected(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_diagnostics_fixture(tmp_path)
        review = self._load_review(output_dir)
        missing_class = review["candidate_quality_diagnostics"]["classes"]["missing_trial_structured_data"]
        # "No Category Work" has empty candidate_category.
        assert missing_class["count"] == 1
        findings = missing_class["findings"]
        no_cat = findings[0]
        assert "no category work" in no_cat["identity_key"].lower()
        assert "candidate_category" in no_cat["missing_fields"]
        assert no_cat["canonical_gate"] == "normal-review"
        # Candidate must still be in the eligible set (missing data doesn't gate).
        candidate_ids = {c["identity_key"] for c in review["candidates"]}
        assert "no category work|missing author" in candidate_ids
        # Source identities must be present.
        assert len(no_cat["source_identities"]) >= 1

    # ------------------------------------------------------------------
    # Non-effect: diagnostics don't alter canonical artifacts
    # ------------------------------------------------------------------

    def test_canonical_artifacts_unchanged_by_diagnostics(self, tmp_path: Path) -> None:
        """Canonical editor-facing rows, identities, priority labels, and
        suppression behavior are unchanged by candidate-quality diagnostics."""
        enabled_output, enabled_state = _build_diagnostics_fixture(tmp_path / "enabled", trial_enabled=True)
        disabled_output, disabled_state = _build_diagnostics_fixture(tmp_path / "disabled", trial_enabled=False)

        # When trial is disabled, no optimization artifacts are produced.
        assert not (disabled_output / "search-optimization-review.json").exists()
        assert not (disabled_output / "search-optimization-review.md").exists()

        # All canonical (non-optimization) artifacts must be identical.
        enabled_files = {
            p.name: p.read_bytes()
            for p in enabled_output.iterdir()
            if p.is_file() and not p.name.startswith("search-optimization-review.")
        }
        disabled_files = {p.name: p.read_bytes() for p in disabled_output.iterdir() if p.is_file()}
        assert enabled_files == disabled_files, (
            f"Canonical artifact divergence: "
            f"enabled-only: {set(enabled_files) - set(disabled_files)}, "
            f"disabled-only: {set(disabled_files) - set(enabled_files)}"
        )

        # State files must match.
        enabled_state_files = {p.name: p.read_bytes() for p in enabled_state.iterdir() if p.is_file()}
        disabled_state_files = {p.name: p.read_bytes() for p in disabled_state.iterdir() if p.is_file()}
        assert enabled_state_files == disabled_state_files

    # ------------------------------------------------------------------
    # Byte determinism: same fixture twice produces identical output
    # ------------------------------------------------------------------

    def test_byte_determinism(self, tmp_path: Path) -> None:
        first_output, _first_state = _build_diagnostics_fixture(tmp_path / "first")
        second_output, _second_state = _build_diagnostics_fixture(tmp_path / "second")

        first_json = (first_output / "search-optimization-review.json").read_bytes()
        second_json = (second_output / "search-optimization-review.json").read_bytes()
        assert first_json == second_json

        first_md = (first_output / "search-optimization-review.md").read_bytes()
        second_md = (second_output / "search-optimization-review.md").read_bytes()
        assert first_md == second_md

    # ------------------------------------------------------------------
    # JSON / Markdown agreement
    # ------------------------------------------------------------------

    def test_json_markdown_agreement(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_diagnostics_fixture(tmp_path)
        review = self._load_review(output_dir)
        md = self._load_markdown(output_dir)

        # Diagnostics section must be present in Markdown.
        assert "## Candidate-Quality Diagnostics" in md

        def _assert_nonblank_sid_in_md(sid: dict[str, str]) -> None:
            for key in ("source_name", "source_family", "source_url", "evidence_url"):
                value = str(sid.get(key, "")).strip()
                if value:
                    escaped = value.replace("|", "&#124;")
                    assert escaped in md, f"Missing source identity field {key}={value} in Markdown"

        cq = review["candidate_quality_diagnostics"]
        for class_id in DIAGNOSTIC_CLASS_IDS:
            class_data = cq["classes"][class_id]
            label = class_data["label"]
            # The label must appear in Markdown as a subsection header.
            assert f"### {label}" in md, f"Missing {label} heading in Markdown"
            # Count must appear.
            assert f"Count: {class_data['count']}" in md, f"Missing count for {class_id} in Markdown"
            # Rank effect statement must appear (first 80 chars).
            rank_effect = class_data.get("rank_effect", "")
            if rank_effect:
                snippet = rank_effect[:80]
                assert snippet in md, f"Missing rank effect snippet '{snippet}' for {class_id} in Markdown"

            # For classes with findings, verify identity keys, gates, source identities, and evidence.
            for finding in class_data["findings"]:
                # Nonblank canonical_gate must appear.
                gate = str(finding.get("canonical_gate", ""))
                if gate:
                    assert gate in md, f"Missing canonical_gate '{gate}' in Markdown"

                if "identity_key" in finding:
                    escaped = str(finding["identity_key"]).replace("|", "&#124;")
                    assert escaped in md, f"Missing escaped identity key {escaped} in Markdown"
                    # Every nonblank source identity field must appear.
                    for sid in finding.get("source_identities", []):
                        _assert_nonblank_sid_in_md(sid)
                if "identity_group" in finding:
                    for identity in finding["identity_group"]:
                        escaped = str(identity["identity_key"]).replace("|", "&#124;")
                        assert escaped in md, f"Missing escaped group identity {escaped} in Markdown"
                        for sid in identity.get("source_identities", []):
                            _assert_nonblank_sid_in_md(sid)
                if "evidence" in finding:
                    evidence = finding["evidence"]
                    reason = str(evidence.get("reason", "")).strip()
                    if reason:
                        assert reason[:60] in md, f"Missing evidence reason '{reason[:60]}' in Markdown"
                    shared_isbn = str(evidence.get("shared_isbn", "")).strip()
                    if shared_isbn:
                        assert shared_isbn in md, f"Missing shared ISBN '{shared_isbn}' in Markdown"

        # Section-wide non-effects must appear.
        for non_effect in cq["section_non_effects"]:
            assert non_effect[:60] in md, f"Missing non-effect: {non_effect[:60]}..."

    # ------------------------------------------------------------------
    # No shadow-rank effect from diagnostics
    # ------------------------------------------------------------------

    def test_no_shadow_rank_effect_from_diagnostics(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_diagnostics_fixture(tmp_path)
        review = self._load_review(output_dir)

        # Verify rank_effect statements are consistent with reality.
        cq = review["candidate_quality_diagnostics"]
        candidate_by_id = {c["identity_key"]: c for c in review["candidates"]}

        # already_known candidate is still in normal review (Material Change).
        ak_class = cq["classes"]["already_known_in_normal_review"]
        for finding in ak_class["findings"]:
            assert finding["identity_key"] in candidate_by_id, (
                f"Already Known candidate {finding['identity_key']} should be in normal review"
            )
            assert "no shadow-rank effect" in ak_class["rank_effect"].lower()

        # reject suppressed candidate not in eligible set.
        rej_class = cq["classes"]["reject_resurfaced_without_material_change"]
        for finding in rej_class["findings"]:
            assert finding["identity_key"] not in candidate_by_id, (
                f"Reject candidate {finding['identity_key']} should NOT be in normal review"
            )

    # ------------------------------------------------------------------
    # Diagnostic flags on every candidate comparison record
    # ------------------------------------------------------------------

    def test_every_candidate_has_diagnostic_flags(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_diagnostics_fixture(tmp_path)
        review = self._load_review(output_dir)
        for candidate in review["candidates"]:
            assert "diagnostic_flags" in candidate, f"Missing diagnostic_flags on {candidate['identity_key']}"
            assert isinstance(candidate["diagnostic_flags"], list)

    def test_diagnostic_flags_attached_to_comparisons(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_diagnostics_fixture(tmp_path)
        review = self._load_review(output_dir)

        ak_candidate = next(c for c in review["candidates"] if c["identity_key"] == "already known work|known author")
        assert "already_known_in_normal_review" in ak_candidate["diagnostic_flags"]

        # Shared-author mentions are grouped and are not flagged as fragmented.
        dup_candidates = [c for c in review["candidates"] if "fragmented work" in c["identity_key"]]
        assert len(dup_candidates) == 1
        assert "probable_duplicate_identity_fragmentation" not in dup_candidates[0]["diagnostic_flags"]

        # Missing category candidate must be flagged.
        no_cat = next(c for c in review["candidates"] if "no category work" in c["identity_key"])
        assert "missing_trial_structured_data" in no_cat["diagnostic_flags"]

        # Podcast work-type conflict (normal-review) must be flagged.
        pod_candidate = next(c for c in review["candidates"] if "podcast" in c["identity_key"])
        assert "scouting_candidate_definition_conflict" in pod_candidate["diagnostic_flags"]

        # Normal candidate must have empty flags.
        normal = next(c for c in review["candidates"] if c["identity_key"] == "normal reference|normal author")
        assert normal["diagnostic_flags"] == []

    # ------------------------------------------------------------------
    # Preserves #326/#327 contract
    # ------------------------------------------------------------------

    def test_preserves_326_contract(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_diagnostics_fixture(tmp_path)
        review = self._load_review(output_dir)

        assert review["ordering_method"]["kind"] == "deterministic lexicographic"
        assert review["ordering_method"]["opaque_score"] is False
        assert "canonical_constraints_preserved" in review["ordering_method"]
        assert "non_effects" in review
        assert review["candidate_count"] == len(review["candidates"])
        assert "candidates" in review
        assert "metrics" in review
        assert "examples" in review
        assert "false_negative_audit" in review

        for candidate in review["candidates"]:
            for field in (
                "identity_key",
                "title",
                "author_display",
                "canonical_rank",
                "shadow_rank",
                "rank_delta",
                "canonical_update_delta",
                "canonical_priority_label",
                "editor_status",
                "outcome_cohort",
                "independent_signal_families",
                "source_evidence_summary",
            ):
                assert field in candidate, f"Missing field '{field}'"

    # ------------------------------------------------------------------
    # A4: Trial-failure isolation
    # ------------------------------------------------------------------

    def test_trial_failure_isolation_no_raise(self, tmp_path: Path) -> None:
        """When build_search_optimization_review raises, run_pipeline does not propagate.

        Canonical artifacts and state are byte-identical to a trial-disabled run.
        Optimization review files are absent.
        """
        # Build shared seed state, then fork into disabled and enabled runs.
        seed_root = tmp_path / "seed"
        seed_root.mkdir(parents=True, exist_ok=True)
        seed_state_dir = seed_root / "state"
        seed_state_dir.mkdir(parents=True, exist_ok=True)
        seed_state(
            seed_state_dir,
            candidate_history=[
                {
                    "identity_key": "already known work|known author",
                    "title": "Already Known Work",
                    "primary_authors": ["Known Author"],
                    "editor_status": "already_known",
                    "editor_note": "Already evaluated and acquired.",
                    "last_seen_date": "2026-08-25",
                    "source_evidence": [
                        {"source_name": "Fixture Awards", "source_family": "award", "signal_strength": "minor mention"},
                    ],
                    "independent_signal_families": ["award"],
                    "primary_signal_strength": "minor mention",
                    "priority_label": "C",
                    "russian_edition_status": "not found in Russian",
                    "visible_risk_status": "no visible risk",
                },
                {
                    "identity_key": "rejected no change|reject author",
                    "title": "Rejected No Change",
                    "primary_authors": ["Reject Author"],
                    "editor_status": "reject",
                    "editor_note": "Not the right fit.",
                    "last_seen_date": "2026-08-25",
                    "source_evidence": [
                        {"source_name": "Fixture Awards", "source_family": "award", "signal_strength": "minor mention"},
                    ],
                    "independent_signal_families": ["award"],
                    "primary_signal_strength": "minor mention",
                    "priority_label": "C",
                    "russian_edition_status": "not found in Russian",
                    "visible_risk_status": "no visible risk",
                },
            ],
        )
        seed_state(seed_state_dir, editorial_profile={"entries": []})
        seed_state(seed_state_dir, candidate_visibility=[])

        def _build_config(trial_enabled: bool) -> dict[str, Any]:
            return {
                "run_date": RUN_DATE,
                "search_optimization_trial": trial_enabled,
                "sources": [
                    _source(
                        "Fixture Awards",
                        "award",
                        [
                            _candidate("Already Known Work", "Known Author", "winner"),
                            _candidate("Rejected No Change", "Reject Author", "minor mention"),
                            _candidate("Fragmented Work", "Author Alpha", "shortlist"),
                            _candidate("Fragmented Work", "Author Beta", "shortlist"),
                            _candidate("Fiction Excluded", "Fiction Author", "minor mention", category="fiction"),
                            _candidate("No Category Work", "Missing Author", "winner"),
                            _candidate("Normal Reference", "Normal Author", "major media list"),
                        ],
                        collector_mentions=[
                            {
                                "title": "Podcast-Only Work",
                                "primary_authors": ["Podcast Author"],
                                "candidate_category": "history",
                                "signal_strength": "minor mention",
                                "signal_type": "award",
                                "evidence_note": "Podcast mention.",
                                "source_url": "https://example.test/fixture-awards",
                                "underlying_work_type": "podcast",
                                "publication_status": "published",
                            }
                        ],
                    ),
                ],
            }

        # Disabled run.
        disabled_root = tmp_path / "disabled"
        disabled_root.mkdir(parents=True, exist_ok=True)
        _write_json(disabled_root / "config.json", _build_config(trial_enabled=False))
        disabled_state = disabled_root / "state"
        disabled_state.mkdir(parents=True, exist_ok=True)
        for fname in ("candidate-history.json", "editorial-profile.json", "candidate-visibility.json"):
            (disabled_state / fname).write_bytes((seed_state_dir / fname).read_bytes())
        sync_state(disabled_state)
        disabled_output = disabled_root / "run"
        run_pipeline(disabled_root / "config.json", disabled_output, disabled_state)

        # Enabled run with injected failure.
        enabled_root = tmp_path / "enabled"
        enabled_root.mkdir(parents=True, exist_ok=True)
        _write_json(enabled_root / "config.json", _build_config(trial_enabled=True))
        enabled_state = enabled_root / "state"
        enabled_state.mkdir(parents=True, exist_ok=True)
        for fname in ("candidate-history.json", "editorial-profile.json", "candidate-visibility.json"):
            (enabled_state / fname).write_bytes((seed_state_dir / fname).read_bytes())
        sync_state(enabled_state)
        enabled_output = enabled_root / "run"

        with mock.patch(
            "autoscout.pipeline.SearchOptimizationReview.construct",
            side_effect=RuntimeError("simulated trial failure"),
        ):
            result = run_pipeline(enabled_root / "config.json", enabled_output, enabled_state)

        # Returns diagnostic mapping, not None.
        assert isinstance(result, dict)
        assert result["search_optimization_trial"]["status"] == "failed"
        assert result["search_optimization_trial"]["exception_type"] == "RuntimeError"
        # Exception message must not appear.
        assert "simulated" not in str(result)

        # Optimization review files must not exist.
        assert not (enabled_output / "search-optimization-review.json").exists()
        assert not (enabled_output / "search-optimization-review.md").exists()

        # Canonical (non-optimization) artifacts must be byte-identical.
        enabled_files = {
            p.name: p.read_bytes()
            for p in enabled_output.iterdir()
            if p.is_file() and not p.name.startswith("search-optimization-review.")
        }
        disabled_files = {p.name: p.read_bytes() for p in disabled_output.iterdir() if p.is_file()}
        assert enabled_files == disabled_files, (
            f"Canonical artifact divergence: "
            f"enabled-only: {set(enabled_files) - set(disabled_files)}, "
            f"disabled-only: {set(disabled_files) - set(enabled_files)}"
        )

        # State files must match.
        enabled_state_files = {p.name: p.read_bytes() for p in enabled_state.iterdir() if p.is_file()}
        disabled_state_files = {p.name: p.read_bytes() for p in disabled_state.iterdir() if p.is_file()}
        assert enabled_state_files == disabled_state_files


# ------------------------------------------------------------------
# Post-enrichment refresh: pre-suppression snapshot evidence (#391)
# ------------------------------------------------------------------


def test_refreshed_review_keeps_reject_author_and_source_identity(tmp_path: Path) -> None:
    """The post-enrichment refresh rebuilds the trial from the persisted
    pre-suppression candidate snapshot, so the refreshed Reject resurface
    finding keeps the exact author display and the configured source
    identity instead of losing them."""
    from autoscout.pipeline import refresh_post_enrichment_artifacts

    output_dir, state_dir = _build_diagnostics_fixture(tmp_path, trial_enabled=True)
    config_path = tmp_path / "config.json"

    result = refresh_post_enrichment_artifacts(config_path, output_dir, state_dir)
    assert result["status"] == "ok"

    review = json.loads((output_dir / "search-optimization-review.json").read_text(encoding="utf-8"))
    rej_class = review["candidate_quality_diagnostics"]["classes"]["reject_resurfaced_without_material_change"]
    assert rej_class["count"] == 1
    finding = rej_class["findings"][0]
    assert finding["identity_key"] == "rejected no change|reject author"
    assert finding["title"] == "Rejected No Change"
    assert finding["author_display"] == "Reject Author"
    assert finding["primary_authors"] == ["Reject Author"]
    assert len(finding["source_identities"]) == 1
    source_identity = finding["source_identities"][0]
    assert source_identity["source_name"] == "Fixture Awards"
    assert source_identity["source_family"] == "award"
    # The Reject candidate stays out of the refreshed eligible set.
    candidate_ids = {c["identity_key"] for c in review["candidates"]}
    assert "rejected no change|reject author" not in candidate_ids

    # The Markdown diagnostic table carries the author and source identity.
    md = (output_dir / "search-optimization-review.md").read_text(encoding="utf-8")
    assert "Reject Author" in md
    assert "Fixture Awards" in md


def test_refresh_rejects_non_mapping_candidates_before_editor_row_conversion(tmp_path: Path) -> None:
    from autoscout.pipeline import refresh_post_enrichment_artifacts

    output_dir, state_dir = _build_diagnostics_fixture(tmp_path, trial_enabled=True)
    config_path = tmp_path / "config.json"
    (output_dir / "scouting-candidates.json").write_text(
        json.dumps(["tck rejects"]),
        encoding="utf-8",
    )

    result = refresh_post_enrichment_artifacts(config_path, output_dir, state_dir)

    assert result == {"status": "skipped", "reason": "invalid scouting-candidates.json"}
    assert "tck rejects" not in json.dumps(result)
    assert str(output_dir) not in json.dumps(result)


def test_pre_suppression_snapshot_is_bounded_and_keeps_suppressed_evidence(tmp_path: Path) -> None:
    """The internal pre-suppression snapshot keeps the suppressed Reject
    candidate with its full author and source evidence, and never carries
    raw source prose fields."""
    from autoscout.source_collectors import FORBIDDEN_COLLECTOR_RESPONSE_FIELDS

    output_dir, _state_dir = _build_diagnostics_fixture(tmp_path, trial_enabled=True)
    snapshot = json.loads((output_dir / "internal-pre-suppression-candidates.json").read_text(encoding="utf-8"))
    assert isinstance(snapshot, list)
    by_id = {str(c["identity_key"]): c for c in snapshot}
    rejected = by_id["rejected no change|reject author"]
    assert rejected["primary_authors"] == ["Reject Author"]
    assert rejected["source_evidence"][0]["source_name"] == "Fixture Awards"
    assert rejected["source_evidence"][0]["source_family"] == "award"
    for candidate in snapshot:
        for evidence in candidate.get("source_evidence", []):
            assert not (set(evidence) & FORBIDDEN_COLLECTOR_RESPONSE_FIELDS)


def test_refresh_all_current_candidates_drops_enrichment_exclusions() -> None:
    """The refresh candidate filter drops only candidates excluded by
    post-enrichment authoritative eligibility; editor-status-suppressed
    candidates stay, and a missing snapshot falls back to final candidates."""
    from autoscout.pipeline import _refresh_all_current_candidates

    snapshot = [
        {"identity_key": "kept|a", "primary_authors": ["A"], "source_evidence": [{"source_name": "S"}]},
        {"identity_key": "excluded|b", "primary_authors": ["B"], "source_evidence": [{"source_name": "S"}]},
    ]
    exclusions = [
        {
            "identity_key": "excluded|b",
            "exclusion_reason": "fiction",
            "authoritative_work_type": "fiction",
        }
    ]
    result = _refresh_all_current_candidates(snapshot, exclusions, [])
    assert [c["identity_key"] for c in result] == ["kept|a"]

    # Candidates suppressed for editor status are not in the exclusion
    # records and therefore stay with their full evidence.
    suppressed_only = _refresh_all_current_candidates(snapshot, [], [])
    assert [c["identity_key"] for c in suppressed_only] == ["kept|a", "excluded|b"]

    # No snapshot (legacy artifacts): the final candidates are the fallback.
    fallback = _refresh_all_current_candidates([], exclusions, [{"identity_key": "final|c"}])
    assert [c["identity_key"] for c in fallback] == ["final|c"]
