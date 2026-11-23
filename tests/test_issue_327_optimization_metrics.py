"""Acceptance tests for issue #327: optimization review metrics and examples.

Uses a deterministic 55-candidate fixture with static worked-literal
expectations derived from the exact pipeline output.
The fixture exercises:
- Reject / Already Known density via Material Change
- At least one valuable candidate demoted for a non-empty false-negative audit
- Multiple source families, signal strengths, and priority labels
- 2 Interested (below threshold 5, .5 median), 4 Actionable (.5 shadow median)
- Positive estimated rows avoided (30) while retaining all Interested
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from state_store_support import seed_state

from autoscout.pipeline import run_pipeline

RUN_DATE = "2026-08-15"


def _candidate(title: str, author: str, signal_strength: str) -> dict[str, object]:
    return {
        "title": title,
        "primary_authors": [author],
        "candidate_category": "history",
        "signal_strength": signal_strength,
        "evidence_note": f"{title} appears in the configured fixture.",
        "publisher_fit": "clear fit",
        "russian_edition_status": "not found in Russian",
        "visible_risk_status": "no visible risk",
    }


def _source(name: str, family: str, candidates: list[dict[str, object]]) -> dict[str, object]:
    return {
        "name": name,
        "family": family,
        "url": f"https://example.test/{name.lower().replace(' ', '-')}",
        "access_status": "open",
        "extraction_method": "fixture",
        "check_frequency": "weekly",
        "fixture_candidates": candidates,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Deterministic fixture: 55 candidates, 5 source families.
#
# Status assignments via Candidate History with WEAK evidence.
# reject/already_known rows get STRONGER / CROSS-FAMILY current evidence
# to trigger Material Change and appear in the eligible set.
#
# Demotion: Book 006 (Actionable, weak upgraded) is demoted by 1 when
# Book 020 (Actionable, unchanged winner) leapfrogs from canonical-last
# to shadow rank ~10, pushing all upgraded candidates down.
#
#   idx=0  -> strong_interest  (winner, award, upgraded)
#   idx=3  -> strong_interest  (winner, reader-interest, upgraded)
#   idx=6  -> actionable_follow_up (minor mention, media, upgraded, DEMOTED)
#   idx=10 -> actionable_follow_up (major media list, media, upgraded)
#   idx=15 -> actionable_follow_up (top ranking, reader-interest, upgraded)
#   idx=20 -> actionable_follow_up (winner, award, UNCHANGED - leapfrogger)
#   idx=25 -> reject (shortlist, reader-interest, upgraded)
#   idx=30 -> already_known (winner, bestseller, upgraded)
#   idx=35 -> reject (reader favorite, reader-interest, upgraded)
#   idx=40 -> already_known (major media list, media, upgraded)
# ---------------------------------------------------------------------------


def _build_fixture(
    root: Path,
    *,
    candidate_count: int = 55,
) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)

    families = [
        ("Fixture Awards A", "award"),
        ("Fixture Awards B", "award"),
        ("Fixture Media Alpha", "media/curated list"),
        ("Fixture Reader", "reader-interest"),
        ("Fixture Bestseller", "bestseller/ranking"),
    ]

    strengths = [
        "winner",
        "shortlist",
        "minor mention",
        "top ranking",
        "major media list",
        "reader favorite",
    ]

    all_candidates: list[dict[str, object]] = []
    for i in range(candidate_count):
        if i == 6:
            strength = "minor mention"  # Weak upgraded actionable -> demoted.
        elif i == 20:
            strength = "winner"  # Unchanged actionable -> leapfrogger.
        else:
            strength = strengths[i % len(strengths)]
        title = f"Book {i:03d}"
        author = f"Author {(i % 13) + 1}"
        all_candidates.append(_candidate(title, author, strength))

    # Source routing: some candidates move to different families than their
    # natural mod-5 mapping to create cross-family Material Change or to
    # match history evidence.
    sources = []
    for fam_idx, (source_name, family) in enumerate(families):
        source_candidates = []
        for i in range(candidate_count):
            if i == 6:
                if fam_idx == 2:
                    source_candidates.append(all_candidates[i])
            elif i == 20:
                if fam_idx == 0:
                    source_candidates.append(all_candidates[i])
            elif i == 30:
                if fam_idx == 4:
                    source_candidates.append(all_candidates[i])
            elif i % len(families) == fam_idx:
                source_candidates.append(all_candidates[i])
        sources.append(_source(source_name, family, source_candidates))

    config_path = root / "config.json"
    _write_json(
        config_path,
        {
            "run_date": RUN_DATE,
            "search_optimization_trial": True,
            "sources": sources,
        },
    )

    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    status_assignment: dict[int, tuple[str, str]] = {
        0: ("interested", "Strong signal from awards and media."),
        3: ("interested", "Reader-interest winner worth tracking."),
        6: ("needs_deeper_analysis", "Weak media signal - demoted by unchanged winner."),
        10: ("needs_deeper_analysis", "Needs Russian market context check."),
        15: ("needs_deeper_analysis", "Complex rights situation."),
        20: ("needs_deeper_analysis", "Unchanged winner - leapfrogs from end."),
        25: ("reject", "Not the right fit for our list."),
        30: ("already_known", "Acquired last season."),
        35: ("reject", "Topic out of scope."),
        40: ("already_known", "Already evaluated and passed."),
    }

    history: list[dict[str, object]] = []
    for i, (status, note) in status_assignment.items():
        if i >= candidate_count:
            continue
        c = all_candidates[i]
        title = str(c["title"])
        authors = c["primary_authors"]
        author = str(authors[0]) if isinstance(authors, list) and authors else "Unknown"
        # Book 020 history matches current evidence (winner, award) -> unchanged.
        if i == 20:
            hist_strength = "winner"
            hist_family = "award"
            hist_source = "Fixture Awards A"
        else:
            hist_strength = "minor mention"
            hist_family = "award"
            hist_source = "Fixture Awards A"
        history.append(
            {
                "identity_key": f"{title.lower()}|{author.lower()}",
                "title": title,
                "primary_authors": [author],
                "editor_status": status,
                "editor_note": note,
                "last_seen_date": "2026-08-08",
                "source_evidence": [
                    {
                        "source_name": hist_source,
                        "source_family": hist_family,
                        "signal_strength": hist_strength,
                    }
                ],
                "independent_signal_families": [hist_family],
                "primary_signal_strength": hist_strength,
                "priority_label": "C",
                "russian_edition_status": "not found in Russian",
                "visible_risk_status": "no visible risk",
            }
        )
    seed_state(state_dir, candidate_history=history)

    output_dir = root / "run"
    run_pipeline(config_path, output_dir, state_dir)
    return output_dir, state_dir


# ---------------------------------------------------------------------------
# Static literal expectations from the deterministic fixture.
# These are NOT derived by filtering the artifact; they are pre-computed
# from the exact known pipeline output.
# ---------------------------------------------------------------------------

EXPECTED_COHORT_COUNTS = {
    "strong_interest": 2,
    "actionable_follow_up": 4,
    "watch": 0,
    "known_workflow_item": 2,
    "negative": 2,
    "unreviewed": 45,
}

EXPECTED_ELIGIBLE_RECALL = {
    "canonical": {"recalled_count": 55, "eligible_count": 55, "recall_rate": 1.0},
    "shadow": {"recalled_count": 55, "eligible_count": 55, "recall_rate": 1.0},
    "identity_sets_identical": True,
}

EXPECTED_COVERAGE_RANK_25 = {
    "canonical": {
        "strong_interest": {
            "count_in_top_cutoff": 0,
            "total_eligible_in_cohort": 2,
            "rate": 0.0,
            "evidence_label": "exploratory",
        },
        "actionable_follow_up": {
            "count_in_top_cutoff": 0,
            "total_eligible_in_cohort": 4,
            "rate": 0.0,
            "evidence_label": "observational",
        },
    },
    "shadow": {
        "strong_interest": {
            "count_in_top_cutoff": 2,
            "total_eligible_in_cohort": 2,
            "rate": 1.0,
            "evidence_label": "exploratory",
        },
        "actionable_follow_up": {
            "count_in_top_cutoff": 2,
            "total_eligible_in_cohort": 4,
            "rate": 0.5,
            "evidence_label": "observational",
        },
    },
}

EXPECTED_COVERAGE_RANK_50 = {
    "canonical": {
        "strong_interest": {
            "count_in_top_cutoff": 2,
            "total_eligible_in_cohort": 2,
            "rate": 1.0,
            "evidence_label": "exploratory",
        },
        "actionable_follow_up": {
            "count_in_top_cutoff": 2,
            "total_eligible_in_cohort": 4,
            "rate": 0.5,
            "evidence_label": "observational",
        },
    },
    "shadow": {
        "strong_interest": {
            "count_in_top_cutoff": 2,
            "total_eligible_in_cohort": 2,
            "rate": 1.0,
            "evidence_label": "exploratory",
        },
        "actionable_follow_up": {
            "count_in_top_cutoff": 3,
            "total_eligible_in_cohort": 4,
            "rate": 0.75,
            "evidence_label": "observational",
        },
    },
}

EXPECTED_DENSITY_RANK_25 = {
    "canonical": {
        "negative": {"count_in_top_cutoff": 0, "rows_inspected": 25, "rate": 0.0},
        "known_workflow_item": {"count_in_top_cutoff": 0, "rows_inspected": 25, "rate": 0.0},
    },
    "shadow": {
        "negative": {"count_in_top_cutoff": 0, "rows_inspected": 25, "rate": 0.0},
        "known_workflow_item": {"count_in_top_cutoff": 1, "rows_inspected": 25, "rate": 0.04},
    },
}

EXPECTED_DENSITY_RANK_50 = {
    "canonical": {
        "negative": {"count_in_top_cutoff": 0, "rows_inspected": 50, "rate": 0.0},
        "known_workflow_item": {"count_in_top_cutoff": 1, "rows_inspected": 50, "rate": 0.02},
    },
    "shadow": {
        "negative": {"count_in_top_cutoff": 2, "rows_inspected": 50, "rate": 0.04},
        "known_workflow_item": {"count_in_top_cutoff": 2, "rows_inspected": 50, "rate": 0.04},
    },
}

EXPECTED_DIVERSITY_RANK_25_CANONICAL: dict[str, Any] = {
    "family_names": ["award", "bestseller/ranking", "media/curated list", "reader-interest"],
    "family_count": 4,
}
EXPECTED_DIVERSITY_RANK_25_SHADOW: dict[str, Any] = {
    "family_names": ["award", "bestseller/ranking", "media/curated list", "reader-interest"],
    "family_count": 4,
}

EXPECTED_DIVERSITY_RANK_50_CANONICAL: dict[str, Any] = {
    "family_names": ["award", "bestseller/ranking", "media/curated list", "reader-interest"],
    "family_count": 4,
}
EXPECTED_DIVERSITY_RANK_50_SHADOW: dict[str, Any] = {
    "family_names": ["award", "bestseller/ranking", "media/curated list", "reader-interest"],
    "family_count": 4,
}

EXPECTED_MEDIAN_RANKS = {
    "strong_interest": {
        "eligible_count": 2,
        "canonical": 47.5,
        "shadow": 13.5,
        "evidence_label": "exploratory",
    },
    "actionable_follow_up": {
        "eligible_count": 4,
        "canonical": 52.0,
        "shadow": 26.5,
        "evidence_label": "observational",
    },
}

EXPECTED_WORKLOAD = {
    "canonical_rows_to_retain_all_interested": 49,
    "shadow_rows_to_retain_all_interested": 19,
    "estimated_rows_avoided": 30,
    "evidence_label": "exploratory",
}

EXPECTED_AK_EXPOSURE = {
    "canonical_ak_in_interested_window": 1,
    "shadow_ak_in_interested_window": 1,
    "ak_rows_difference": 0,
}

EXPECTED_DISCOVERY_QUALITY = {
    "strong_interest_coverage_delta": 2,
    "strong_interest_coverage_delta_evidence_label": "exploratory",
    "actionable_coverage_delta": 2,
    "strong_interest_median_rank_delta": -34.0,
    "strong_interest_median_rank_delta_evidence_label": "exploratory",
    "actionable_median_rank_delta": -25.5,
}

EXPECTED_WARNINGS = [
    "Fewer than 5 observed Interested candidates (2); strong-interest-dependent"
    " metrics and the overall workload conclusion are marked exploratory.",
]

EXPECTED_SELECTION_NOTE = (
    "Up to 6 representative examples per direction, selected by absolute rank delta"
    " descending, then canonical rank ascending, then identity key lexicographic."
    " Total promoted: 9, total demoted: 39."
)

EXPECTED_PROMOTED_IDS = [
    "book 020|author 8",
    "book 000|author 1",
    "book 030|author 5",
    "book 015|author 3",
    "book 003|author 4",
    "book 010|author 11",
]

EXPECTED_DEMOTED_IDS = [
    "book 050|author 12",
    "book 026|author 1",
    "book 002|author 3",
    "book 032|author 7",
    "book 008|author 9",
    "book 038|author 13",
]

EXPECTED_FN_AUDIT = [
    {
        "identity_key": "book 006|author 7",
        "title": "Book 006",
        "author_display": "Author 7",
        "canonical_rank": 54,
        "shadow_rank": 55,
        "rank_delta": -1,
        "outcome_cohort": "actionable_follow_up",
    },
]

EXPECTED_SI_EXAMPLE_FIELDS = {
    "identity_key": "book 000|author 1",
    "title": "Book 000",
    "author_display": "Author 1",
    "canonical_rank": 46,
    "shadow_rank": 8,
    "rank_delta": 38,
    "outcome_cohort": "strong_interest",
}

EXPECTED_OBSERVED_INTERESTED = 2
EXPECTED_EVIDENCE_THRESHOLD = 5

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_review(output_dir: Path) -> Any:
    with (output_dir / "search-optimization-review.json").open(encoding="utf-8") as f:
        return json.load(f)


def _load_markdown(output_dir: Path) -> str:
    return (output_dir / "search-optimization-review.md").read_text(encoding="utf-8")


def _extract_md_section(md: str, section_header: str) -> list[str]:
    """Return all lines from *section_header* until the next `## ` heading."""
    lines = md.split("\n")
    in_section = False
    result: list[str] = []
    for line in lines:
        if line.strip() == section_header:
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                break
            result.append(line)
    return result


class TestIssue327OptimizationMetricsTests:
    """Static-literal tests for the #327 optimization review contract."""

    # ------------------------------------------------------------------
    # Schema and identity
    # ------------------------------------------------------------------

    def test_schema_and_identity(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_fixture(tmp_path)
        review = _load_review(output_dir)
        assert review["schema_version"] == 4
        assert review["artifact_type"] == "search-optimization-review"
        assert review["run_identity"] == RUN_DATE
        assert review["canonical_effect"] == "none"
        assert review["eligible_count"] == 55
        assert review["candidate_count"] == 55
        assert review["outcome_cohort_counts"] == EXPECTED_COHORT_COUNTS

    # ------------------------------------------------------------------
    # Eligible recall - per ordering
    # ------------------------------------------------------------------

    def test_eligible_recall(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_fixture(tmp_path)
        review = _load_review(output_dir)
        assert review["metrics"]["eligible_recall"] == EXPECTED_ELIGIBLE_RECALL

    # ------------------------------------------------------------------
    # Cohort coverage
    # ------------------------------------------------------------------

    def test_cohort_coverage(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_fixture(tmp_path)
        review = _load_review(output_dir)
        cov = review["metrics"]["cohort_coverage"]
        assert cov["rank_25"] == EXPECTED_COVERAGE_RANK_25
        assert cov["rank_50"] == EXPECTED_COVERAGE_RANK_50

    # ------------------------------------------------------------------
    # Density
    # ------------------------------------------------------------------

    def test_density(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_fixture(tmp_path)
        review = _load_review(output_dir)
        den = review["metrics"]["density"]
        assert den["rank_25"] == EXPECTED_DENSITY_RANK_25
        assert den["rank_50"] == EXPECTED_DENSITY_RANK_50

    # ------------------------------------------------------------------
    # Source-family diversity
    # ------------------------------------------------------------------

    def test_diversity(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_fixture(tmp_path)
        review = _load_review(output_dir)
        div = review["metrics"]["diversity"]
        assert div["rank_25"]["canonical"] == EXPECTED_DIVERSITY_RANK_25_CANONICAL
        assert div["rank_25"]["shadow"] == EXPECTED_DIVERSITY_RANK_25_SHADOW
        assert div["rank_50"]["canonical"] == EXPECTED_DIVERSITY_RANK_50_CANONICAL
        assert div["rank_50"]["shadow"] == EXPECTED_DIVERSITY_RANK_50_SHADOW

    # ------------------------------------------------------------------
    # Median ranks - including .5 half-ranks
    # ------------------------------------------------------------------

    def test_median_ranks(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_fixture(tmp_path)
        review = _load_review(output_dir)
        assert review["metrics"]["median_ranks"] == EXPECTED_MEDIAN_RANKS

    # ------------------------------------------------------------------
    # Workload, Already Known exposure, discovery quality
    # ------------------------------------------------------------------

    def test_workload(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_fixture(tmp_path)
        review = _load_review(output_dir)
        wl = review["metrics"]["workload"]

        assert (
            wl["canonical_rows_to_retain_all_interested"]
            == EXPECTED_WORKLOAD["canonical_rows_to_retain_all_interested"]
        )
        assert wl["shadow_rows_to_retain_all_interested"] == EXPECTED_WORKLOAD["shadow_rows_to_retain_all_interested"]
        assert wl["estimated_rows_avoided"] == EXPECTED_WORKLOAD["estimated_rows_avoided"]
        assert wl["evidence_label"] == EXPECTED_WORKLOAD["evidence_label"]

        # Already Known exposure.
        ak = wl["already_known_exposure"]
        assert "No additional suppression occurs" in ak["note"]
        assert "the shadow review does not alter" in ak["note"]
        assert ak["canonical_ak_in_interested_window"] == EXPECTED_AK_EXPOSURE["canonical_ak_in_interested_window"]
        assert ak["shadow_ak_in_interested_window"] == EXPECTED_AK_EXPOSURE["shadow_ak_in_interested_window"]
        assert ak["ak_rows_difference"] == EXPECTED_AK_EXPOSURE["ak_rows_difference"]

        # Discovery quality.
        dq = wl["discovery_quality"]
        assert "negative value means earlier access" in dq["note"]
        assert "positive value means later access" in dq["note"]
        assert dq["strong_interest_coverage_delta"] == EXPECTED_DISCOVERY_QUALITY["strong_interest_coverage_delta"]
        assert dq["actionable_coverage_delta"] == EXPECTED_DISCOVERY_QUALITY["actionable_coverage_delta"]
        assert (
            dq["strong_interest_median_rank_delta"] == EXPECTED_DISCOVERY_QUALITY["strong_interest_median_rank_delta"]
        )
        assert dq["actionable_median_rank_delta"] == EXPECTED_DISCOVERY_QUALITY["actionable_median_rank_delta"]
        assert dq["strong_interest_coverage_delta_evidence_label"] == "exploratory"
        assert dq["strong_interest_median_rank_delta_evidence_label"] == "exploratory"

    # ------------------------------------------------------------------
    # Warnings
    # ------------------------------------------------------------------

    def test_warnings(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_fixture(tmp_path)
        review = _load_review(output_dir)
        assert review["metrics"]["warnings"] == EXPECTED_WARNINGS

    # ------------------------------------------------------------------
    # Evidence labels on individual metrics
    # ------------------------------------------------------------------

    def test_evidence_labels(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_fixture(tmp_path)
        review = _load_review(output_dir)
        metrics = review["metrics"]
        ec = metrics["evidence_classification"]
        assert ec["observed_interested_count"] == EXPECTED_OBSERVED_INTERESTED
        assert ec["threshold"] == EXPECTED_EVIDENCE_THRESHOLD
        assert ec["is_exploratory"] is True

        # Strong-interest coverage is exploratory in all cutoff/order combos.
        cov = metrics["cohort_coverage"]
        for ck in ("rank_25", "rank_50"):
            for order in ("canonical", "shadow"):
                assert cov[ck][order]["strong_interest"]["evidence_label"] == "exploratory"
                assert cov[ck][order]["actionable_follow_up"]["evidence_label"] == "observational"

        assert metrics["median_ranks"]["strong_interest"]["evidence_label"] == "exploratory"
        assert metrics["median_ranks"]["actionable_follow_up"]["evidence_label"] == "observational"
        assert metrics["workload"]["evidence_label"] == "exploratory"

    # ------------------------------------------------------------------
    # Examples - deterministic selection, escaping, exact fields
    # ------------------------------------------------------------------

    def test_examples(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_fixture(tmp_path)
        review = _load_review(output_dir)
        examples = review["examples"]

        assert examples["selection_note"] == EXPECTED_SELECTION_NOTE
        assert examples["promoted_count"] == 9
        assert examples["demoted_count"] == 39

        # Exact identity lists in deterministic order.
        promoted_ids = [ex["identity_key"] for ex in examples["promoted"]]
        assert promoted_ids == EXPECTED_PROMOTED_IDS
        demoted_ids = [ex["identity_key"] for ex in examples["demoted"]]
        assert demoted_ids == EXPECTED_DEMOTED_IDS

        # Verify first promoted example has exact expected fields.
        si_ex = examples["promoted"][1]  # Book 000 at index 1 in promoted list
        for key, expected in EXPECTED_SI_EXAMPLE_FIELDS.items():
            assert si_ex[key] == expected, f"Mismatch on {key}: {si_ex[key]} != {expected}"
        assert len(si_ex["explanations"]) > 0

        # Every example has explanations with identity keys escaped in MD.
        md = _load_markdown(output_dir)
        for ex in examples["promoted"] + examples["demoted"]:
            escaped_key = ex["identity_key"].replace("|", "&#124;")
            assert escaped_key in md, f"Missing escaped key {escaped_key} in Markdown"
            # Verify explanations column contains escaped content.
            assert ex["explanations"]
            first_expl = ex["explanations"][0]
            escaped_expl_snippet = first_expl[:40].replace("|", "&#124;")
            assert escaped_expl_snippet in md, "Missing escaped explanation snippet in Markdown"

    # ------------------------------------------------------------------
    # False-negative audit - substantive, exact fields
    # ------------------------------------------------------------------

    def test_false_negative_audit(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_fixture(tmp_path)
        review = _load_review(output_dir)
        fn_audit = review["false_negative_audit"]

        assert len(fn_audit) == 1
        item = fn_audit[0]
        for key, expected in EXPECTED_FN_AUDIT[0].items():
            assert item[key] == expected, f"Mismatch on {key}: {item[key]} != {expected}"
        assert len(item["explanations"]) > 0

    # ------------------------------------------------------------------
    # Markdown completeness
    # ------------------------------------------------------------------

    def test_markdown_completeness(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_fixture(tmp_path)
        review = _load_review(output_dir)
        md = _load_markdown(output_dir)

        required_sections = [
            "## Warnings",
            "## Evidence Classification",
            "## Eligible Recall",
            "## Cohort Coverage",
            "### At rank cutoff: 25",
            "### At rank cutoff: 50",
            "## Density",
            "## Source-Family Diversity",
            "## Median Ranks",
            "## Workload Estimate",
            "### Already Known Exposure",
            "### Discovery Quality",
            "## Representative Examples",
            "## False-Negative Audit",
            "## What this review does not change",
        ]
        for section in required_sections:
            assert section in md, f"Missing section: {section}"

        assert review["run_identity"] in md
        assert f"Schema version: {review['schema_version']}" in md
        assert "read-only" in md.lower()
        assert "exploratory" in md.lower()
        assert "[exploratory]" in md
        assert "[observational]" in md

    # ------------------------------------------------------------------
    # JSON / Markdown agreement - exact metric representations
    # ------------------------------------------------------------------

    def test_artifact_agreement(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_fixture(tmp_path)
        review = _load_review(output_dir)
        md = _load_markdown(output_dir)

        # Warnings verbatim.
        for w in review["metrics"]["warnings"]:
            assert w in md

        # Evidence classification.
        ec = review["metrics"]["evidence_classification"]
        assert str(ec["observed_interested_count"]) in md
        assert str(ec["threshold"]) in md

        # Eligible recall.
        recall = review["metrics"]["eligible_recall"]
        assert str(recall["canonical"]["recalled_count"]) in md
        assert str(recall["canonical"]["eligible_count"]) in md
        assert "1.000" in md  # recall rate formatted to three decimals
        assert str(recall["shadow"]["recalled_count"]) in md
        assert str(recall["identity_sets_identical"]) in md

        # Cohort coverage rates formatted to three decimals.
        assert "0.000" in md
        assert "0.500" in md
        assert "0.750" in md
        assert "1.000" in md

        # Density -- exact per-cutoff per-order table rows.
        density_md = _extract_md_section(md, "## Density")
        density_text = "\n".join(density_md)
        assert "| Reject - negative | 0 / 25 | 0.000 | 0 / 25 | 0.000 |" in density_text
        assert "| Already Known - known workflow item | 0 / 25 | 0.000 | 1 / 25 | 0.040 |" in density_text
        assert "| Reject - negative | 0 / 50 | 0.000 | 2 / 50 | 0.040 |" in density_text
        assert "| Already Known - known workflow item | 1 / 50 | 0.020 | 2 / 50 | 0.040 |" in density_text

        # Diversity -- exact per-cutoff lines; both cutoffs produce identical text
        # so we assert each line occurs twice to prove both were rendered.
        diversity_md = _extract_md_section(md, "## Source-Family Diversity")
        diversity_text = "\n".join(diversity_md)
        canonical_diversity_line = (
            "Canonical: 4 families - award, bestseller/ranking, media/curated list, reader-interest"
        )
        shadow_diversity_line = "Shadow: 4 families - award, bestseller/ranking, media/curated list, reader-interest"
        assert diversity_text.count(canonical_diversity_line) == 2, (
            f"Expected canonical diversity line twice, got {diversity_text.count(canonical_diversity_line)}"
        )
        assert diversity_text.count(shadow_diversity_line) == 2, (
            f"Expected shadow diversity line twice, got {diversity_text.count(shadow_diversity_line)}"
        )

        # Median ranks -- exact table rows.
        median_md = _extract_md_section(md, "## Median Ranks")
        median_text = "\n".join(median_md)
        assert "| Interested - strong interest [exploratory] | 2 | 47.5 | 13.5 |" in median_text
        assert "| Needs Deeper Analysis - actionable follow-up [observational] | 4 | 52 | 26.5 |" in median_text

        # Workload values.
        assert str(EXPECTED_WORKLOAD["canonical_rows_to_retain_all_interested"]) in md
        assert str(EXPECTED_WORKLOAD["shadow_rows_to_retain_all_interested"]) in md
        assert str(EXPECTED_WORKLOAD["estimated_rows_avoided"]) in md

        # Already Known exposure note - no additional suppression.
        ak_note = review["metrics"]["workload"]["already_known_exposure"]["note"]
        assert "No additional suppression occurs" in ak_note
        assert "No additional suppression occurs" in md
        assert "the shadow review does not alter canonical suppression behavior" in md

        # Discovery quality note.
        dq_note = review["metrics"]["workload"]["discovery_quality"]["note"]
        assert "negative value means earlier access" in dq_note
        assert "negative value means earlier access" in md
        assert "positive value means later access" in md

        # Discovery quality deltas rendered.
        assert "-34" in md  # strong-interest median-rank delta
        assert "-25.5" in md  # actionable median-rank delta

        # Every example identity escaped.
        promoted_ex: list[dict[str, Any]] = review["examples"]["promoted"]
        demoted_ex: list[dict[str, Any]] = review["examples"]["demoted"]
        for ex in promoted_ex + demoted_ex:
            escaped = ex["identity_key"].replace("|", "&#124;")
            assert escaped in md, f"Missing escaped example identity {escaped}"

        # Selection note verbatim.
        assert EXPECTED_SELECTION_NOTE[:60] in md

        # False-negative audit.
        for item in review["false_negative_audit"]:
            escaped = item["identity_key"].replace("|", "&#124;")
            assert escaped in md, f"Missing escaped FN identity {escaped}"
            assert str(item["canonical_rank"]) in md
            assert str(item["shadow_rank"]) in md
            assert str(item["rank_delta"]) in md

    # ------------------------------------------------------------------
    # Canonical artifacts unchanged
    # ------------------------------------------------------------------

    def test_canonical_unchanged(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_fixture(tmp_path)
        with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 55
        review = _load_review(output_dir)
        review_by_id = {c["identity_key"]: c for c in review["candidates"]}
        for idx, row in enumerate(rows):
            cid = row["Candidate ID"]
            assert cid in review_by_id
            assert review_by_id[cid]["canonical_rank"] == idx + 1

    # ------------------------------------------------------------------
    # #326 contract preservation
    # ------------------------------------------------------------------

    def test_preserves_326_contract(self, tmp_path: Path) -> None:
        output_dir, _state_dir = _build_fixture(tmp_path)
        review = _load_review(output_dir)

        assert review["ordering_method"]["kind"] == "deterministic lexicographic"
        assert review["ordering_method"]["opaque_score"] is False
        assert "canonical_constraints_preserved" in review["ordering_method"]
        assert "non_effects" in review

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
            if candidate["rank_delta"] != 0:
                assert candidate["explanations"]

    # ------------------------------------------------------------------
    # A4: Explanation collapse threshold
    # ------------------------------------------------------------------

    def test_explanation_collapse_above_threshold(self, tmp_path: Path) -> None:
        """A candidate with >=7 crossed peers produces exactly 2 explanations.

        The first is the decisive peer explanation and the second is a
        summary naming the decisive factor.
        """
        output_dir, _state_dir = _build_fixture(tmp_path)
        review = _load_review(output_dir)

        # Find candidates that cross >=7 peers.
        candidates_with_many_crossed = [c for c in review["candidates"] if abs(int(c["rank_delta"])) >= 7]
        # At least one candidate should have large enough rank delta.
        assert len(candidates_with_many_crossed) >= 1, "Expected at least one candidate with |rank_delta| >= 7"

        for candidate in candidates_with_many_crossed:
            explanations = candidate["explanations"]
            assert len(explanations) == 2, (
                f"Expected exactly 2 explanations for {candidate['identity_key']} "
                f"(delta {candidate['rank_delta']}), got {len(explanations)}"
            )
            # Second explanation must name the decisive factor.
            assert "decisive factor" in explanations[1].lower(), (
                f"Second explanation must mention decisive factor: {explanations[1]}"
            )
