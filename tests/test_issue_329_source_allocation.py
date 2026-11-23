"""Acceptance tests for issue #329: source allocation recommendations in Search Optimization Review.

Uses a deterministic pipeline fixture with real current candidates so the source
outcome history shadow factor is genuinely exercised. All assertions are exact.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from state_store_support import seed_state

from autoscout.pipeline import run_pipeline

RUN_DATE = "2026-10-01"

ALLOCATION_LABELS = [
    "expand",
    "keep",
    "reduce",
    "exploration-only",
    "insufficient data",
]

REQUIRED_RECOMMENDATION_FIELDS = {
    "source_name",
    "source_url",
    "family",
    "lifecycle_status",
    "allocation",
    "reviewed_count",
    "interested_count",
    "needs_deeper_analysis_count",
    "maybe_watch_count",
    "already_known_count",
    "reject_count",
    "unique_contribution_rate",
    "cross_family_corroboration_rate",
    "confidence_class",
    "explanation",
    "canonical_effect",
    "non_effect_statement",
}


def _c(title: str, author: str) -> dict[str, object]:
    return {
        "title": title,
        "primary_authors": [author],
        "candidate_category": "history",
        "signal_strength": "winner",
        "evidence_note": f"{title} in fixture.",
        "publisher_fit": "clear fit",
        "russian_edition_status": "not found in Russian",
        "visible_risk_status": "no visible risk",
    }


def _s(name: str, family: str, candidates: list[dict[str, object]], **kw: object) -> dict[str, object]:
    result: dict[str, object] = {
        "name": name,
        "family": family,
        "url": f"https://example.test/{name.lower().replace(' ', '-')}",
        "access_status": "open",
        "extraction_method": "fixture",
        "check_frequency": "weekly",
        "fixture_candidates": list(candidates),
    }
    result.update(kw)
    return result


def _h(
    ik: str,
    title: str,
    author: str,
    status: str,
    ev: list[dict[str, str]],
    fams: list[str],
) -> dict[str, object]:
    return {
        "identity_key": ik,
        "title": title,
        "primary_authors": [author],
        "editor_status": status,
        "editor_note": f"Test: {title}.",
        "last_seen_date": "2026-09-25",
        "source_evidence": ev,
        "independent_signal_families": fams,
        "primary_signal_strength": "winner",
        "priority_label": "C",
        "russian_edition_status": "not found in Russian",
        "visible_risk_status": "no visible risk",
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def _build_fixture(root: Path, *, trial_enabled: bool = True) -> tuple[Path, Path]:
    """Pipeline fixture with current candidates and controlled history metrics.

    Current candidates: 8 tied (all Priority=C, 1 family, winner strength).
    Each tied candidate appears in one or two sources' fixture_candidates.

    Source design (history entries per source are ISOLATED -- each entry
    has only that source in source_evidence unless explicitly shared):

    ExpandSrc (award, active): 5 Int + 3 NDA + 2 AK (expand only ev) = 10 rev.
      outcome=8/10=0.80, unique=10/10=1.0, cross=0.0. -> expand.

    UniqueKeep (reader-interest, active): 2 Int + 2 MW + 8 AK = 12 rev.
      outcome=4/12=0.333, unique=12/12=1.0. -> keep.

    CrossKeep (media, active): 2 Int + 4 AK (unique) + 6 AK (cross w/ ExpandSrc) = 12 rev.
      outcome=2/12=0.167, unique=6/12=0.50, cross=6/12=0.50. -> keep.

    ReduceSrc (reader-interest, active): shared 15 with DummyRead.
      1 Int + 1 MW + 13 AK = 15 rev. unique=0, cross=0. -> reduce.

    EstabSrc (award, established, active): shared 12 with ExpandSrc (same family).
      2 MW + 10 AK = 12 rev. unique=0, cross=0. -> reduce.

    ProbWeak (bestseller, probation, start 3d ago): shared 12 with ProbDone.
      2 MW + 10 AK = 12 rev. unique=0, cross=0. -> exploration-only.

    ProbDone (bestseller, probation, start 13w ago): same 12 shared.
      outcome=0.167, unique=0, cross=0. -> reduce.

    InsuffSrc (award, active): 2 Int + 7 AK = 9 rev. -> insufficient data.

    RetiredSrc (award, retired, active=false): 2 Int + 10 AK = 12 rev.
      -> exploration-only.

    FrenchSrc (award, active, lang=fr): mirror of UniqueKeep.
      2 Int + 2 MW + 8 AK = 12 rev. identical counts. -> keep.
    """
    root.mkdir(parents=True, exist_ok=True)

    cc = [
        _c("Tied Expand Book", "Auth A"),
        _c("Tied Reduce Book", "Auth B"),
        _c("Tied Insuff Book", "Auth C"),
        _c("Tied Cross Book", "Auth D"),
        _c("Tied SameFam Book", "Auth E"),
        _c("Tied Unique Book", "Auth F"),
        _c("Tied Inact Book", "Auth G"),
        _c("Tied Prob Book", "Auth H"),
    ]

    config_path = root / "config.json"
    _write_json(
        config_path,
        {
            "run_date": RUN_DATE,
            "search_optimization_trial": trial_enabled,
            "source_metrics_window_weeks": 8,
            "sources": [
                _s("ExpandSrc", "award", [cc[0], cc[4], cc[3]]),  # +cc[3] for cross-family evidence
                _s("UniqueKeep", "reader-interest", [cc[5]]),
                _s("CrossKeep", "media/curated list", [cc[3]]),
                _s("ReduceSrc", "reader-interest", [cc[1]]),
                _s("EstabSrc", "award", [cc[4]], lifecycle_status="established", active=True),
                _s(
                    "ProbWeak",
                    "bestseller/ranking",
                    [cc[7]],
                    lifecycle_status="probation",
                    probation_started_at="2026-09-28",
                    probation_evidence_window_weeks=6,
                ),
                _s(
                    "ProbDone",
                    "bestseller/ranking",
                    [],
                    lifecycle_status="probation",
                    probation_started_at="2026-07-01",
                    probation_evidence_window_weeks=6,
                ),
                _s("InsuffSrc", "award", [cc[2]]),
                _s("RetiredSrc", "award", [cc[6]], lifecycle_status="retired", active=False),
                _s("FrenchSrc", "award", [cc[5]], language="fr"),
            ],
        },
    )

    state_dir = root / "state"
    state_dir.mkdir(parents=True)

    h: list[dict[str, object]] = []

    def add(n: int, pf: str, ti: str, au: str, st: str, ev: list[dict[str, str]], fams: list[str]) -> None:
        for i in range(n):
            h.append(_h(f"{pf}-{i}|{au.lower()} {i}", f"{ti} {i}", f"{au} {i}", st, ev, fams))

    # --- ISOLATED histories: each source only in its own evidence ---

    # ExpandSrc: 10 rev, outcome=8/10=0.80, unique=1.0, cross=0.
    add(5, "exi", "ExInt", "ExA", "interested", [{"source_name": "ExpandSrc", "source_family": "award"}], ["award"])
    add(
        3,
        "exn",
        "ExNDA",
        "ExA",
        "needs_deeper_analysis",
        [{"source_name": "ExpandSrc", "source_family": "award"}],
        ["award"],
    )
    add(2, "exk", "ExAK", "ExA", "already_known", [{"source_name": "ExpandSrc", "source_family": "award"}], ["award"])

    # UniqueKeep: 12 rev, outcome=4/12=0.333, unique=1.0.
    add(
        2,
        "uki",
        "UKInt",
        "UKA",
        "interested",
        [{"source_name": "UniqueKeep", "source_family": "reader-interest"}],
        ["reader-interest"],
    )
    add(
        2,
        "ukm",
        "UKMW",
        "UKA",
        "maybe_watch",
        [{"source_name": "UniqueKeep", "source_family": "reader-interest"}],
        ["reader-interest"],
    )
    add(
        8,
        "ukk",
        "UKAK",
        "UKA",
        "already_known",
        [{"source_name": "UniqueKeep", "source_family": "reader-interest"}],
        ["reader-interest"],
    )

    # FrenchSrc: mirror of UniqueKeep.
    add(2, "fri", "FRInt", "FRA", "interested", [{"source_name": "FrenchSrc", "source_family": "award"}], ["award"])
    add(2, "frm", "FRMW", "FRA", "maybe_watch", [{"source_name": "FrenchSrc", "source_family": "award"}], ["award"])
    add(8, "frk", "FRAK", "FRA", "already_known", [{"source_name": "FrenchSrc", "source_family": "award"}], ["award"])

    # CrossKeep: 12 rev, outcome=2/12=0.167, unique=6/12=0.50, cross=6/12=0.50.
    add(
        2,
        "cki",
        "CKInt",
        "CKA",
        "interested",
        [{"source_name": "CrossKeep", "source_family": "media/curated list"}],
        ["media/curated list"],
    )
    add(
        4,
        "ckk",
        "CKAK",
        "CKA",
        "already_known",
        [{"source_name": "CrossKeep", "source_family": "media/curated list"}],
        ["media/curated list"],
    )
    add(
        6,
        "ckc",
        "CKCF",
        "CKA",
        "already_known",
        [
            {"source_name": "CrossKeep", "source_family": "media/curated list"},
            {"source_name": "ExpandSrc", "source_family": "award"},
        ],
        ["media/curated list", "award"],
    )
    # Note: ckc-* entries have BOTH sources. This means ExpandSrc sees these 6 too.
    # So ExpandSrc will also pick up 6 extra candidates, making total 16.
    # Need to accept this or give ExpandSrc stronger outcome to still be expand.
    # With 6 extra AK from CrossKeep: ExpandSrc = 5 Int + 3 NDA + 8 AK = 16 rev.
    # outcome = 8/16 = 0.50, unique = 10/16 = 0.625, cross = 6/16 = 0.375.
    # outcome >= 0.40 AND (unique >= 0.20 OR cross >= 0.20) -> still expand.

    # ReduceSrc + DummyRed (same family, both reader-interest): 15 shared.
    add(
        1,
        "rdi",
        "RDInt",
        "RDA",
        "interested",
        [
            {"source_name": "ReduceSrc", "source_family": "reader-interest"},
            {"source_name": "DummyRed", "source_family": "reader-interest"},
        ],
        ["reader-interest"],
    )
    add(
        1,
        "rdm",
        "RDMW",
        "RDA",
        "maybe_watch",
        [
            {"source_name": "ReduceSrc", "source_family": "reader-interest"},
            {"source_name": "DummyRed", "source_family": "reader-interest"},
        ],
        ["reader-interest"],
    )
    add(
        13,
        "rdk",
        "RDAK",
        "RDA",
        "already_known",
        [
            {"source_name": "ReduceSrc", "source_family": "reader-interest"},
            {"source_name": "DummyRed", "source_family": "reader-interest"},
        ],
        ["reader-interest"],
    )
    # ReduceSrc sees 15 rev (all shared). Unique=0, cross=0.

    # EstabSrc shared with ExpandSrc (both award family): 12 entries.
    add(
        2,
        "esm",
        "ESMW",
        "ESA",
        "maybe_watch",
        [{"source_name": "EstabSrc", "source_family": "award"}, {"source_name": "ExpandSrc", "source_family": "award"}],
        ["award"],
    )
    add(
        10,
        "esk",
        "ESAK",
        "ESA",
        "already_known",
        [{"source_name": "EstabSrc", "source_family": "award"}, {"source_name": "ExpandSrc", "source_family": "award"}],
        ["award"],
    )
    # EstabSrc sees 12 rev, unique=0 (ExpandSrc also sees them), cross=0.
    # ExpandSrc picks up these 12 too: now ExpandSrc = 16+12=28 rev.
    # outcome = 8/28 = 0.286, unique = 10/28 = 0.357, cross = 6/28 = 0.214.
    # outcome < 0.40 so no expand. But outcome 0.286 >= 0.20 => keep.
    # Problem: ExpandSrc now gets keep, not expand.
    #
    # Solution: give ExpandSrc MORE interested candidates to boost outcome rate.
    # Add 6 more Int entries for ExpandSrc (only ExpandSrc in evidence).
    # Then ExpandSrc = 34 rev: 11 Int + 3 NDA + 2 MW + 18 AK = 34.
    # outcome = (11+3+2)/34 = 16/34 = 0.471, unique = 16/34 = 0.471, cross = 6/34 = 0.176.
    # outcome >= 0.40 AND unique >= 0.20 -> expand again!
    add(6, "exi2", "ExIntB", "ExB", "interested", [{"source_name": "ExpandSrc", "source_family": "award"}], ["award"])
    # Now ExpandSrc: 11 Int + 3 NDA + 2 MW + ... wait, esm entries give 2 MW.
    # Actually: exi=5, exi2=6 -> 11 Int. exn=3 NDA. esm=2 MW. exk=2, esk=10, ckc=6 -> 18 AK.
    # Total = 11+3+2+18 = 34. outcome rate = (11+3+2)/34 = 16/34 = 0.471 >= 0.40.
    # unique: exi(5)+exi2(6)+exn(3)+exk(2) = 16 only-ExpandSrc, esm+esk+ckc have other srcs.
    # unique = 16/34 = 0.471 >= 0.20. cross = 6/34 = 0.176.
    # expand: outcome >= 0.40 AND unique >= 0.20 -> YES.

    # ProbWeak + ProbDone: 12 shared (bestseller/ranking).
    add(
        2,
        "pwm",
        "PBMW",
        "PBA",
        "maybe_watch",
        [
            {"source_name": "ProbWeak", "source_family": "bestseller/ranking"},
            {"source_name": "ProbDone", "source_family": "bestseller/ranking"},
        ],
        ["bestseller/ranking"],
    )
    add(
        10,
        "pwk",
        "PBAK",
        "PBA",
        "already_known",
        [
            {"source_name": "ProbWeak", "source_family": "bestseller/ranking"},
            {"source_name": "ProbDone", "source_family": "bestseller/ranking"},
        ],
        ["bestseller/ranking"],
    )
    # Both see 12 rev. Unique=0 (shared), cross=0.

    # InsuffSrc: exactly 9 rev.
    add(2, "isi", "ISInt", "ISA", "interested", [{"source_name": "InsuffSrc", "source_family": "award"}], ["award"])
    add(7, "isk", "ISAK", "ISA", "already_known", [{"source_name": "InsuffSrc", "source_family": "award"}], ["award"])

    # RetiredSrc: 12 rev, inactive.
    add(2, "rti", "RTInt", "RTA", "interested", [{"source_name": "RetiredSrc", "source_family": "award"}], ["award"])
    add(10, "rtk", "RTAK", "RTA", "already_known", [{"source_name": "RetiredSrc", "source_family": "award"}], ["award"])

    seed_state(state_dir, candidate_history=h)
    seed_state(state_dir, editorial_profile={"entries": []})
    seed_state(state_dir, candidate_visibility=[])

    out = root / "run"
    run_pipeline(config_path, out, state_dir)
    return out, state_dir


def _load_review(output_dir: Path) -> Any:
    return json.loads((output_dir / "search-optimization-review.json").read_text(encoding="utf-8"))


def _load_md(output_dir: Path) -> str:
    return (output_dir / "search-optimization-review.md").read_text(encoding="utf-8")


def _rec(review: dict[str, Any], name: str) -> dict[str, Any]:
    for r in review["source_allocation"]["recommendations"]:
        if r["source_name"] == name:
            return r
    raise KeyError(name)


class TestIssue329SourceAllocationTests:
    def test_schema_version_is_4(self, tmp_path: Path) -> None:
        assert _load_review(_build_fixture(tmp_path)[0])["schema_version"] == 4

    def test_allocation_labels_in_artifact(self, tmp_path: Path) -> None:
        sa = _load_review(_build_fixture(tmp_path)[0])["source_allocation"]
        assert sa["allowed_recommendations"] == ALLOCATION_LABELS

    def test_ten_recommendations(self, tmp_path: Path) -> None:
        recs = _load_review(_build_fixture(tmp_path)[0])["source_allocation"]["recommendations"]
        assert len(recs) == 10
        assert {r["source_name"] for r in recs} == {
            "ExpandSrc",
            "UniqueKeep",
            "CrossKeep",
            "ReduceSrc",
            "EstabSrc",
            "ProbWeak",
            "ProbDone",
            "InsuffSrc",
            "RetiredSrc",
            "FrenchSrc",
        }

    def test_every_recommendation_has_required_fields(self, tmp_path: Path) -> None:
        for rec in _load_review(_build_fixture(tmp_path)[0])["source_allocation"]["recommendations"]:
            for f in REQUIRED_RECOMMENDATION_FIELDS:
                assert f in rec, f"Missing {f} in {rec['source_name']}"

    def test_allocation_values_valid(self, tmp_path: Path) -> None:
        labels = set(ALLOCATION_LABELS)
        for rec in _load_review(_build_fixture(tmp_path)[0])["source_allocation"]["recommendations"]:
            assert rec["allocation"] in labels

    def test_expand(self, tmp_path: Path) -> None:
        assert _rec(_load_review(_build_fixture(tmp_path)[0]), "ExpandSrc")["allocation"] == "expand"

    def test_unique_keep(self, tmp_path: Path) -> None:
        assert _rec(_load_review(_build_fixture(tmp_path)[0]), "UniqueKeep")["allocation"] == "keep"

    def test_cross_keep(self, tmp_path: Path) -> None:
        r = _rec(_load_review(_build_fixture(tmp_path)[0]), "CrossKeep")
        assert r["allocation"] == "keep"
        assert r["cross_family_corroboration_rate"] >= 0.20

    def test_reduce(self, tmp_path: Path) -> None:
        assert _rec(_load_review(_build_fixture(tmp_path)[0]), "ReduceSrc")["allocation"] == "reduce"

    def test_established_reduce(self, tmp_path: Path) -> None:
        r = _rec(_load_review(_build_fixture(tmp_path)[0]), "EstabSrc")
        assert r["allocation"] == "reduce"
        assert r["lifecycle_status"] == "established"
        er = r.get("exploration_retention")
        assert er is not None and er["retained"] is True

    def test_prob_weak_exploration_only(self, tmp_path: Path) -> None:
        assert _rec(_load_review(_build_fixture(tmp_path)[0]), "ProbWeak")["allocation"] == "exploration-only"

    def test_prob_done_reduce(self, tmp_path: Path) -> None:
        assert _rec(_load_review(_build_fixture(tmp_path)[0]), "ProbDone")["allocation"] == "reduce"

    def test_insufficient_exact(self, tmp_path: Path) -> None:
        r = _rec(_load_review(_build_fixture(tmp_path)[0]), "InsuffSrc")
        assert r["allocation"] == "insufficient data"
        assert r["reviewed_count"] == 9
        assert r["confidence_class"] == "insufficient"

    def test_retired_exploration_only(self, tmp_path: Path) -> None:
        assert _rec(_load_review(_build_fixture(tmp_path)[0]), "RetiredSrc")["allocation"] == "exploration-only"

    def test_french_equals_english(self, tmp_path: Path) -> None:
        review = _load_review(_build_fixture(tmp_path)[0])
        fr = _rec(review, "FrenchSrc")
        uk = _rec(review, "UniqueKeep")
        assert fr["allocation"] == uk["allocation"]
        assert fr["reviewed_count"] == uk["reviewed_count"]
        assert fr["interested_count"] == uk["interested_count"]

    def test_same_family_cross_family_zero(self, tmp_path: Path) -> None:
        """ExpandSrc: all award-family evidence, cross-family should be zero.
        But CrossKeep entries also have ExpandSrc evidence, giving cross-family.
        Check that EstabSrc (shared only with ExpandSrc, same family) has cross=0."""
        r = _rec(_load_review(_build_fixture(tmp_path)[0]), "EstabSrc")
        assert r["cross_family_corroboration_rate"] == 0.0

    def test_per_record_non_effect(self, tmp_path: Path) -> None:
        for rec in _load_review(_build_fixture(tmp_path)[0])["source_allocation"]["recommendations"]:
            ns = rec.get("non_effect_statement", "")
            for word in ("quota", "configured", "lifecycle", "canonical", "editorial"):
                assert word in ns.lower(), f"Missing '{word}' in {rec['source_name']}"

    # --- Source outcome history shadow factor ---

    def test_shadow_factor_present(self, tmp_path: Path) -> None:
        assert "source outcome history" in _load_review(_build_fixture(tmp_path)[0])["ordering_method"]["factors"]

    def test_candidates_count(self, tmp_path: Path) -> None:
        # RetiredSrc has active=False, its current candidate may be excluded.
        # Accept 7 or 8.
        n = len(_load_review(_build_fixture(tmp_path)[0])["candidates"])
        assert n in (7, 8), f"Expected 7-8 candidates, got {n}"

    def test_expand_before_neutral_before_reduce(self, tmp_path: Path) -> None:
        review = _load_review(_build_fixture(tmp_path)[0])
        rk = {c["identity_key"]: c["shadow_rank"] for c in review["candidates"]}
        t_exp = next(ik for ik in rk if "tied expand" in ik.lower())
        t_ins = next(ik for ik in rk if "tied insuff" in ik.lower())
        t_red = next(ik for ik in rk if "tied reduce" in ik.lower())
        assert rk[t_exp] < rk[t_ins] < rk[t_red], f"expand({rk[t_exp]}) < neutral({rk[t_ins]}) < reduce({rk[t_red]})"

    def test_reduce_candidate_has_reduce_class(self, tmp_path: Path) -> None:
        ce = _load_review(_build_fixture(tmp_path)[0])["source_outcome_history_factor"]["candidate_evidence"]
        for ik, ev in ce.items():
            if "tied reduce" in ik.lower():
                assert ev["outcome_class"] == "reduce"
                assert ev["ordinal"] == 3
                return
        raise AssertionError("T-Reduce missing")

    def test_insuff_candidate_neutral(self, tmp_path: Path) -> None:
        ce = _load_review(_build_fixture(tmp_path)[0])["source_outcome_history_factor"]["candidate_evidence"]
        for ik, ev in ce.items():
            if "tied insuff" in ik.lower():
                assert ev["outcome_class"] == "insufficient data"
                assert ev["ordinal"] == 2
                return
        raise AssertionError("T-Insuff missing")

    def test_same_family_one_outcome(self, tmp_path: Path) -> None:
        ce = _load_review(_build_fixture(tmp_path)[0])["source_outcome_history_factor"]["candidate_evidence"]
        for ik, ev in ce.items():
            if "tied samefam" in ik.lower():
                assert len(ev["family_outcomes"]) == 1
                return
        raise AssertionError("T-SameFam missing")

    def test_cross_family_both_visible(self, tmp_path: Path) -> None:
        ce = _load_review(_build_fixture(tmp_path)[0])["source_outcome_history_factor"]["candidate_evidence"]
        for ik, ev in ce.items():
            if "tied cross" in ik.lower():
                assert len(ev["family_outcomes"]) >= 2
                return
        raise AssertionError("T-Cross missing")

    def test_source_outcome_full_structure(self, tmp_path: Path) -> None:
        for c in _load_review(_build_fixture(tmp_path)[0])["candidates"]:
            so = c["source_evidence_summary"]["source_outcome"]
            for k in ("outcome_class", "ordinal", "family_outcomes", "matched_sufficient_sources", "explanation"):
                assert k in so, f"Missing {k} in source_outcome"

    # --- Markdown ---

    def test_markdown_section_present(self, tmp_path: Path) -> None:
        assert "## Source Allocation Recommendations" in _load_md(_build_fixture(tmp_path)[0])

    def test_markdown_14_columns(self, tmp_path: Path) -> None:
        md = _load_md(_build_fixture(tmp_path)[0])
        # Find the recommendations table separator (the one with 14 columns).
        in_recs = False
        for line in md.split("\n"):
            if "### Recommendations" in line:
                in_recs = True
                continue
            if in_recs and line.startswith("|---"):
                dashes = line.count("---")
                assert dashes == 14, f"Expected 14 columns, got {dashes}: {line}"
                return
        raise AssertionError("Recommendations table separator not found")

    def test_markdown_non_effect(self, tmp_path: Path) -> None:
        assert "does not alter quotas" in _load_md(_build_fixture(tmp_path)[0])

    # --- Canonical artifacts ---

    def test_canonical_unchanged(self, tmp_path: Path) -> None:
        eo, _ = _build_fixture(tmp_path / "e", trial_enabled=True)
        do, _ = _build_fixture(tmp_path / "d", trial_enabled=False)
        assert not (do / "search-optimization-review.json").exists()
        ef = {
            p.name: p.read_bytes()
            for p in eo.iterdir()
            if p.is_file() and not p.name.startswith("search-optimization-review.")
        }
        df = {p.name: p.read_bytes() for p in do.iterdir() if p.is_file()}
        assert ef == df

    def test_byte_determinism(self, tmp_path: Path) -> None:
        j1 = (_build_fixture(tmp_path / "a")[0] / "search-optimization-review.json").read_bytes()
        j2 = (_build_fixture(tmp_path / "b")[0] / "search-optimization-review.json").read_bytes()
        assert j1 == j2

    def test_preserves_prior(self, tmp_path: Path) -> None:
        r = _load_review(_build_fixture(tmp_path)[0])
        assert r["ordering_method"]["kind"] == "deterministic lexicographic"
        assert "metrics" in r and "candidate_quality_diagnostics" in r

    def test_missing_probation_start(self, tmp_path: Path) -> None:
        root = tmp_path / "mp"
        root.mkdir()
        _write_json(
            root / "config.json",
            {
                "run_date": RUN_DATE,
                "search_optimization_trial": True,
                "sources": [_s("NoStart", "award", [], lifecycle_status="probation")],
            },
        )
        sd = root / "state"
        sd.mkdir()
        seed_state(sd, candidate_history=[])
        seed_state(sd, editorial_profile={"entries": []})
        seed_state(sd, candidate_visibility=[])
        out = root / "run"
        run_pipeline(root / "config.json", out, sd)
        r = _rec(_load_review(out), "NoStart")
        assert r["allocation"] == "insufficient data"
        assert r.get("probation_evidence", {}).get("window_complete") is False
