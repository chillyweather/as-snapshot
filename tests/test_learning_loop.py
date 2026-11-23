import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot


def run_autoscout(*args):
    return subprocess.run([sys.executable, "-m", "autoscout", *args], check=False, capture_output=True, text=True)


def write_config(path: Path, candidates, run_date="2026-05-21"):
    path.write_text(
        json.dumps(
            {
                "run_date": run_date,
                "sources": [
                    {
                        "name": "Fixture Awards",
                        "family": "award",
                        "url": "https://example.test/awards",
                        "access_status": "open",
                        "extraction_method": "fixture",
                        "check_frequency": "weekly",
                        "language": "en",
                        "fixture_candidates": candidates,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_editorial_profile_cli_add_list_and_revoke(tmp_path):
    state_dir = tmp_path / "state"

    add = run_autoscout(
        "editorial-profile",
        "add",
        "--kind",
        "hard-rule",
        "--scope",
        "author",
        "--subject",
        "Alex Example",
        "--quote",
        "No more Alex Example books.",
        "--provenance",
        "editor chat",
        "--state-dir",
        str(state_dir),
        "--confirmed-by-editor",
    )

    assert add.returncode == 0, add.stdout + add.stderr
    assert "id=taste-0001" in add.stdout
    listed = run_autoscout("editorial-profile", "list", "--state-dir", str(state_dir))
    assert "No more Alex Example books." in listed.stdout
    assert "[active]" in listed.stdout

    revoked = run_autoscout(
        "editorial-profile",
        "revoke",
        "--id",
        "taste-0001",
        "--reason",
        "Editor changed their mind.",
        "--state-dir",
        str(state_dir),
        "--confirmed-by-editor",
    )
    assert revoked.returncode == 0, revoked.stdout + revoked.stderr
    listed = run_autoscout("editorial-profile", "list", "--state-dir", str(state_dir))
    assert "[revoked]" in listed.stdout
    assert "Editor changed their mind." in listed.stdout


@pytest.mark.json_backend_only("a malformed state file on disk")
def test_editorial_profile_cli_list_reports_a_malformed_profile(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    # A malformed profile on disk: the State Store refuses to write it, so the file is written directly.
    (state_dir / "editorial-profile.json").write_text(json.dumps({"entries": [{"id": "bad"}]}), encoding="utf-8")
    malformed = run_autoscout("editorial-profile", "list", "--state-dir", str(state_dir))
    assert malformed.returncode != 0
    assert "malformed editorial taste profile" in malformed.stdout


def test_author_hard_rule_hides_candidate_but_preserves_history_and_revocation_resurfaces(tmp_path):
    state_dir = tmp_path / "state"
    config = tmp_path / "sources.json"
    write_config(
        config,
        [
            {
                "title": "Hidden Book",
                "primary_authors": ["Example, Alex"],
                "source_url": "https://example.test/hidden",
                "signal_strength": "winner",
                "evidence_note": "Won a fixture award.",
                "russian_edition_status": "not found in Russian",
            },
            {
                "title": "Visible Book",
                "primary_authors": ["Bea Example"],
                "source_url": "https://example.test/visible",
                "signal_strength": "shortlist",
                "evidence_note": "Shortlisted for a fixture award.",
                "russian_edition_status": "not found in Russian",
            },
        ],
    )
    add = run_autoscout(
        "editorial-profile",
        "add",
        "--kind",
        "hard-rule",
        "--scope",
        "author",
        "--subject",
        "Alex Example",
        "--quote",
        "Hide Alex Example.",
        "--provenance",
        "editor chat",
        "--state-dir",
        str(state_dir),
        "--confirmed-by-editor",
    )
    assert add.returncode == 0, add.stdout + add.stderr

    output_dir = tmp_path / "run-one"
    first = run_autoscout(
        "run-mvp", "--config", str(config), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
    )
    assert first.returncode == 0, first.stderr
    rows = list(csv.DictReader((output_dir / "editor-facing.csv").open(encoding="utf-8")))
    assert [row["Title"] for row in rows] == ["Visible Book"]
    hidden = json.loads((output_dir / "hidden-by-rule.json").read_text(encoding="utf-8"))
    assert hidden[0]["hidden_by_rule_id"] == "taste-0001"
    summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
    assert summary["hidden_by_rule_count"] == 1
    rendered = run_autoscout("render-review", "--output-dir", str(output_dir), "--state-dir", str(state_dir))
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    review = (output_dir / "weekly-review.md").read_text(encoding="utf-8")
    html_review = (output_dir / "weekly-review.html").read_text(encoding="utf-8")
    assert "скрыто правилами — 1" in review
    assert "скрытых редактором — 0" in review
    assert "скрыто правилами — 1" in html_review
    assert "скрытых редактором — 0" in html_review
    history = read_state(state_dir, "candidate_history")
    assert {record["identity_key"] for record in history} >= {"hidden book|alex example", "visible book|bea example"}
    # After author-display normalization (issue #275), "Example, Alex"
    # normalizes to "Alex Example" → "alex example" in the identity key.

    revoke = run_autoscout(
        "editorial-profile",
        "revoke",
        "--id",
        "taste-0001",
        "--reason",
        "Allow again.",
        "--state-dir",
        str(state_dir),
        "--confirmed-by-editor",
    )
    assert revoke.returncode == 0, revoke.stdout + revoke.stderr
    output_dir_two = tmp_path / "run-two"
    second = run_autoscout(
        "run-mvp", "--config", str(config), "--output-dir", str(output_dir_two), "--state-dir", str(state_dir)
    )
    assert second.returncode == 0, second.stderr
    rows = list(csv.DictReader((output_dir_two / "editor-facing.csv").open(encoding="utf-8")))
    assert "Hidden Book" in [row["Title"] for row in rows]
    candidates = json.loads((output_dir_two / "scouting-candidates.json").read_text(encoding="utf-8"))
    restored = next(candidate for candidate in candidates if candidate["title"] == "Hidden Book")
    assert restored["update_delta"] == "unchanged"
    # Revoked rules let candidates reappear naturally without special delta machinery.
    assert "material_change_reasons" not in restored or restored.get("material_change_reasons") == []


def test_run_mvp_does_not_overwrite_profile_state(tmp_path):
    state_dir = tmp_path / "state"
    add = run_autoscout(
        "editorial-profile",
        "add",
        "--kind",
        "soft-preference",
        "--scope",
        "author",
        "--subject",
        "Alex Example",
        "--quote",
        "Alex is promising.",
        "--provenance",
        "editor chat",
        "--state-dir",
        str(state_dir),
        "--confirmed-by-editor",
    )
    assert add.returncode == 0, add.stdout + add.stderr
    before = state_snapshot(state_dir)["editorial_profile"]
    config = tmp_path / "sources.json"
    write_config(config, [])

    result = run_autoscout(
        "run-mvp", "--config", str(config), "--output-dir", str(tmp_path / "run"), "--state-dir", str(state_dir)
    )

    assert result.returncode == 0, result.stderr
    assert state_snapshot(state_dir)["editorial_profile"] == before


@pytest.mark.json_backend_only("a malformed state file on disk")
def test_run_mvp_reports_malformed_profile_without_traceback(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    # A malformed profile on disk: the State Store refuses to write it, so the file is written directly.
    (state_dir / "editorial-profile.json").write_text(json.dumps({"entries": [{"id": "bad"}]}), encoding="utf-8")
    config = tmp_path / "sources.json"
    write_config(config, [])

    result = run_autoscout(
        "run-mvp", "--config", str(config), "--output-dir", str(tmp_path / "run"), "--state-dir", str(state_dir)
    )

    assert result.returncode != 0
    assert "malformed editorial taste profile" in result.stdout
    assert "Traceback" not in result.stderr


def test_update_digest_labels_order_and_review_summary(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    seed_state(
        state_dir,
        candidate_history=[
            {
                "identity_key": "upgraded book|ada example",
                "independent_signal_families": ["award"],
                "primary_signal_strength": "minor mention",
                "russian_edition_status": "not found in Russian",
                "visible_risk_status": "",
            },
            {
                "identity_key": "unchanged book|cy example",
                "independent_signal_families": ["award"],
                "primary_signal_strength": "shortlist",
                "russian_edition_status": "not found in Russian",
                "visible_risk_status": "",
            },
            {
                "identity_key": "changed book|bea example",
                "independent_signal_families": ["media/curated list"],
                "primary_signal_strength": "shortlist",
                "russian_edition_status": "not found in Russian",
                "visible_risk_status": "",
            },
            {
                "identity_key": "priority book|priority example",
                "independent_signal_families": ["award"],
                "primary_signal_strength": "shortlist",
                "priority_label": "C",
                "russian_edition_status": "not found in Russian",
                "visible_risk_status": "",
            },
        ],
    )
    seed_state(
        state_dir,
        editorial_profile={
            "entries": [
                {
                    "id": "taste-0001",
                    "kind": "soft_preference",
                    "scope": "author",
                    "subject": "Priority Example",
                    "direction": "positive",
                    "verbatim_quote": "More like Priority Example.",
                    "provenance": "editor chat",
                    "created_at": "2026-05-01",
                    "created_by": "agent",
                    "status": "active",
                }
            ]
        },
    )
    config = tmp_path / "sources.json"
    write_config(
        config,
        [
            {
                "title": "Unchanged Book",
                "primary_authors": ["Cy Example"],
                "source_url": "https://e.test/u",
                "signal_strength": "shortlist",
                "evidence_note": "Same.",
                "russian_edition_status": "not found in Russian",
            },
            {
                "title": "Changed Book",
                "primary_authors": ["Bea Example"],
                "source_url": "https://e.test/c",
                "signal_strength": "shortlist",
                "evidence_note": "New family.",
                "russian_edition_status": "not found in Russian",
            },
            {
                "title": "New Book",
                "primary_authors": ["New Example"],
                "source_url": "https://e.test/n",
                "signal_strength": "shortlist",
                "evidence_note": "New.",
                "russian_edition_status": "not found in Russian",
            },
            {
                "title": "Priority Book",
                "primary_authors": ["Priority Example"],
                "source_url": "https://e.test/p",
                "signal_strength": "shortlist",
                "evidence_note": "Preferred author.",
                "russian_edition_status": "not found in Russian",
            },
            {
                "title": "Upgraded Book",
                "primary_authors": ["Ada Example"],
                "source_url": "https://e.test/up",
                "signal_strength": "winner",
                "evidence_note": "Won.",
                "russian_edition_status": "not found in Russian",
            },
        ],
    )
    output_dir = tmp_path / "run"
    result = run_autoscout(
        "run-mvp", "--config", str(config), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
    )
    assert result.returncode == 0, result.stderr
    candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
    # Changed Book gets a new independent signal family (award vs media/curated list),
    # which classifies as "upgraded" per the updated spec.
    assert [(c["title"], c["update_delta"]) for c in candidates] == [
        ("New Book", "new"),
        ("Priority Book", "upgraded"),
        ("Upgraded Book", "upgraded"),
        ("Changed Book", "upgraded"),
        ("Unchanged Book", "unchanged"),
    ]
    priority_book = next(candidate for candidate in candidates if candidate["title"] == "Priority Book")
    assert priority_book["priority_label"] == "B"
    assert "More like Priority Example." in priority_book["editorial_profile_notes"]
    render = run_autoscout("render-review", "--output-dir", str(output_dir), "--state-dir", str(state_dir))
    assert render.returncode == 0, render.stdout + render.stderr
    review = (output_dir / "weekly-review.md").read_text(encoding="utf-8")
    assert (
        "**За эту неделю**: новых — 1, улучшенных — 3, изменённых — 0, без изменений — 1, скрыто правилами — 0."
        in review
    )
