"""Issue #559: confirmed identity merges resolve conflicting generated descriptions.

The concrete shape is the PEOPLE duplicate the editor asked AutoScout to
handle: ``The Ultimate Renovation Planner`` (reviewed, rejected) and
``The Ultimate Renovation Planner and Organizer`` (unreviewed).  Both records
carry an independently generated Russian description and its own
``description_provenance``, so the guarded merge refused to produce a plan.

These tests pin the explicit-resolution contract: the correction artifact
names the winning description, the dry run reports the whole plan without
writing, invalid or stale resolution input fails before any state file is
touched, and the confirmed apply keeps the editor decision verbatim.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from state_store_support import before_database_commit
from state_store_support import fail_database_commits
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_databases

from autoscout import identity_corrections
from autoscout import safe_artifacts
from autoscout.__main__ import main
from autoscout.candidate_identity import effective_candidate_id
from autoscout.candidate_identity import has_conflicting_aliases
from autoscout.candidate_identity import resolve_candidate_id
from autoscout.identity_corrections import apply_identity_correction
from autoscout.identity_corrections import plan_identity_correction
from autoscout.review_rendering import assemble_cumulative_review
from autoscout.state_store import json_backend

REVIEWED_ID = "candidate-b1c5716f51505d2c51dc4523"
REPEAT_ID = "candidate-2c393d97235271b629928472"
REVIEWED_KEY = "the ultimate renovation planner|jonathan scott"
REPEAT_KEY = "the ultimate renovation planner and organizer|jonathan scott"
CANONICAL_KEY = REPEAT_KEY
CANONICAL_TITLE = "The Ultimate Renovation Planner and Organizer"
ARTICLE_URL = "https://people.com/jonathan-scott-the-ultimate-renovation-planner-cover-reveal-exclusive-12059205"
LANDING_URL = "https://people.com/books/"

REVIEWED_DESCRIPTION = (
    "«The Ultimate Renovation Planner» — книга Джонатана Скотта о DIY-проектах "
    "и планировании ремонта. Её редакционный хук — упростить организацию домашних преобразований."
)
REPEAT_DESCRIPTION = (
    "Джонатан Скотт предлагает планировщик и органайзер для более удобного ведения "
    "ремонтных DIY-проектов. Редакционный повод — эксклюзивная презентация обложки практического издания."
)
EDITOR_NOTE = (
    "Планирование ремонта. Книга завязана на зарубежные реалии и статистику планирования "
    "пространства, не применимые в РФ (такое должны писать отечественные авторы). Сейчас редакции "
    "скорее интересны практические темы про приусадебное хозяйство и садоводство."
)


def _write(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _evidence(note: str, source_url: str) -> dict:
    return {
        "evidence_note": note,
        "evidence_url": ARTICLE_URL,
        "signal_strength": "minor mention",
        "signal_type": "media/curated list",
        "source_check_date": "2026-08-26",
        "source_family": "media/curated list",
        "source_name": "PEOPLE Books",
        "source_url": source_url,
    }


def _reviewed_record() -> dict:
    return {
        "candidate_id": REVIEWED_ID,
        "content_description": REVIEWED_DESCRIPTION,
        "description_provenance": {
            "applied_date": "2026-08-17",
            "content_description": REVIEWED_DESCRIPTION,
            "evidence_basis": [ARTICLE_URL],
            "identity_key": REVIEWED_KEY,
            "provenance": "codex-generated",
        },
        "editor_note": EDITOR_NOTE,
        "editor_status": "reject",
        "first_seen_date": "2026-08-17",
        "identity_key": REVIEWED_KEY,
        "independent_signal_count": 1,
        "independent_signal_families": ["media/curated list"],
        "last_seen_date": "2026-08-26",
        "primary_authors": ["Jonathan Scott"],
        "source_evidence": [_evidence("Карточка PEOPLE называет книгу Джонатана Скотта.", ARTICLE_URL)],
        "title": "The Ultimate Renovation Planner",
    }


def _repeat_record() -> dict:
    return {
        "candidate_id": REPEAT_ID,
        "content_description": REPEAT_DESCRIPTION,
        "description_provenance": {
            "applied_date": "2026-08-21",
            "content_description": REPEAT_DESCRIPTION,
            "evidence_basis": [ARTICLE_URL],
            "identity_key": REPEAT_KEY,
            "provenance": "codex-generated",
        },
        "first_seen_date": "2026-08-21",
        "identity_key": REPEAT_KEY,
        "independent_signal_count": 1,
        "independent_signal_families": ["media/curated list"],
        "last_seen_date": "2026-08-26",
        "primary_authors": ["Jonathan Scott"],
        "source_evidence": [_evidence("Карточка PEOPLE Books помечена как Non-Fiction Books.", LANDING_URL)],
        "title": CANONICAL_TITLE,
    }


def _state(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    seed_state(state_dir, candidate_history=[_reviewed_record(), _repeat_record()])
    seed_state(
        state_dir,
        archived_editor_rows=[{"identity_key": REVIEWED_KEY, "Candidate ID": REVIEWED_KEY, "Editor Note": EDITOR_NOTE}],
    )
    seed_state(
        state_dir,
        candidate_visibility=[
            {"identity_key": REVIEWED_KEY, "visibility": "hidden", "audit_log": [{"date": "2026-08-18"}]}
        ],
    )
    return state_dir


def _resolution(chosen: str = REPEAT_KEY) -> dict:
    return {
        "canonical_source_identity_key": chosen,
        "observed_descriptions": {
            REVIEWED_KEY: REVIEWED_DESCRIPTION,
            REPEAT_KEY: REPEAT_DESCRIPTION,
        },
        "observed_description_provenance": {
            REVIEWED_KEY: _reviewed_record()["description_provenance"],
            REPEAT_KEY: _repeat_record()["description_provenance"],
        },
    }


def _correction(resolution: dict | None = None) -> dict:
    correction = {
        "source_identity_keys": [REVIEWED_KEY, REPEAT_KEY],
        "canonical": {
            "identity_key": CANONICAL_KEY,
            "title": CANONICAL_TITLE,
            "primary_authors": ["Jonathan Scott"],
        },
    }
    if resolution is not None:
        correction["description_resolution"] = resolution
    return correction


def _snapshot(state_dir: Path) -> dict[str, bytes]:
    return {**{path.name: path.read_bytes() for path in sorted(state_dir.glob("*.json"))}, **state_databases()}


def test_merge_without_resolution_still_refuses_conflicting_descriptions(tmp_path):
    """The #262 refusal stays in place when the artifact resolves nothing."""
    state_dir = _state(tmp_path)

    with pytest.raises(ValueError, match="conflicting content_description"):
        plan_identity_correction(state_dir, _correction())


def test_dry_run_reports_the_resolution_and_writes_nothing(tmp_path):
    state_dir = _state(tmp_path)
    before = _snapshot(state_dir)

    report = apply_identity_correction(state_dir, _correction(_resolution()), dry_run=True)

    assert _snapshot(state_dir) == before
    assert report["canonical_identity_key"] == CANONICAL_KEY
    assert report["canonical_title"] == CANONICAL_TITLE
    assert report["canonical_authors"] == ["Jonathan Scott"]
    assert report["source_identity_keys"] == [REPEAT_KEY, REVIEWED_KEY]
    assert report["merged_history_records"] == 2
    assert report["rewritten_references"] == 3
    assert report["preserved_editor_status"] == "reject"
    assert report["preserved_editor_note"] == EDITOR_NOTE
    assert report["content_description"] == REPEAT_DESCRIPTION
    assert report["description_resolution"] == {
        "chosen_source_identity_key": REPEAT_KEY,
        "provenance": "codex-generated",
        "evidence_basis": [ARTICLE_URL],
        "discarded_source_identity_keys": [REVIEWED_KEY],
    }


def test_confirmed_apply_merges_the_two_identities(tmp_path):
    state_dir = _state(tmp_path)

    apply_identity_correction(state_dir, _correction(_resolution()))

    history = read_state(state_dir, "candidate_history")
    assert [record["identity_key"] for record in history] == [CANONICAL_KEY]
    record = history[0]
    assert record["title"] == CANONICAL_TITLE
    assert record["primary_authors"] == ["Jonathan Scott"]
    assert record["editor_status"] == "reject"
    assert record["editor_note"] == EDITOR_NOTE
    assert record["first_seen_date"] == "2026-08-17"
    assert record["last_seen_date"] == "2026-08-26"
    assert record["content_description"] == REPEAT_DESCRIPTION
    assert record["description_provenance"] == {
        "applied_date": "2026-08-21",
        "content_description": REPEAT_DESCRIPTION,
        "evidence_basis": [ARTICLE_URL],
        "identity_key": CANONICAL_KEY,
        "provenance": "codex-generated",
    }
    assert record["description_merge_resolution"] == {
        "canonical_identity_key": CANONICAL_KEY,
        "chosen_source_identity_key": REPEAT_KEY,
        "discarded_source_identity_keys": [REVIEWED_KEY],
    }
    assert [item["source_url"] for item in record["source_evidence"]] == [LANDING_URL, ARTICLE_URL]
    assert record["candidate_id"] == REPEAT_ID


def test_confirmed_apply_rewrites_supported_references(tmp_path):
    state_dir = _state(tmp_path)

    apply_identity_correction(state_dir, _correction(_resolution()))

    archived = read_state(state_dir, "archived_editor_rows")
    assert archived == [{"identity_key": CANONICAL_KEY, "Candidate ID": CANONICAL_KEY, "Editor Note": EDITOR_NOTE}]
    visibility = read_state(state_dir, "candidate_visibility")
    assert visibility == [
        {"identity_key": CANONICAL_KEY, "visibility": "hidden", "audit_log": [{"date": "2026-08-18"}]}
    ]


@pytest.mark.parametrize(
    ("resolution", "message"),
    [
        pytest.param({"observed_descriptions": {}}, "canonical_source_identity_key", id="missing-chosen-key"),
        pytest.param(
            {"canonical_source_identity_key": REPEAT_KEY},
            "observed_descriptions",
            id="missing-observed-descriptions",
        ),
        pytest.param(
            {
                "canonical_source_identity_key": "some other book|nobody",
                "observed_descriptions": {REVIEWED_KEY: REVIEWED_DESCRIPTION, REPEAT_KEY: REPEAT_DESCRIPTION},
            },
            "unknown source identity key",
            id="chosen-key-not-merged",
        ),
        pytest.param(
            {
                "canonical_source_identity_key": REPEAT_KEY,
                "observed_descriptions": {REPEAT_KEY: REPEAT_DESCRIPTION},
            },
            "stale",
            id="missing-observed-record",
        ),
        pytest.param(
            {
                "canonical_source_identity_key": REPEAT_KEY,
                "observed_descriptions": {
                    REVIEWED_KEY: "Устаревший текст.",
                    REPEAT_KEY: REPEAT_DESCRIPTION,
                },
            },
            "stale",
            id="stale-observed-text",
        ),
        pytest.param(
            {
                "canonical_source_identity_key": REPEAT_KEY,
                "observed_descriptions": {
                    REVIEWED_KEY: REVIEWED_DESCRIPTION,
                    REPEAT_KEY: REPEAT_DESCRIPTION,
                    "some other book|nobody": "Лишняя запись.",
                },
            },
            "unknown source identity key",
            id="extra-observed-record",
        ),
        pytest.param("not-an-object", "must be an object", id="wrong-type"),
    ],
)
def test_incompatible_resolution_fails_before_any_state_file_is_written(tmp_path, resolution, message):
    state_dir = _state(tmp_path)
    before = _snapshot(state_dir)

    with pytest.raises(ValueError, match=message):
        apply_identity_correction(state_dir, _correction(resolution))

    assert _snapshot(state_dir) == before


def test_resolution_cannot_choose_a_record_without_a_description(tmp_path):
    state_dir = _state(tmp_path)
    repeat = _repeat_record()
    repeat["content_description"] = ""
    repeat.pop("description_provenance")
    seed_state(state_dir, candidate_history=[_reviewed_record(), repeat])
    before = _snapshot(state_dir)
    resolution = {
        "canonical_source_identity_key": REPEAT_KEY,
        "observed_descriptions": {REVIEWED_KEY: REVIEWED_DESCRIPTION},
    }

    with pytest.raises(ValueError, match="content description"):
        apply_identity_correction(state_dir, _correction(resolution))

    assert _snapshot(state_dir) == before


def test_cli_dry_run_then_confirmed_apply(tmp_path, capsys):
    state_dir = _state(tmp_path)
    correction_path = tmp_path / "correction.json"
    _write(correction_path, _correction(_resolution()))
    argv = [
        "merge-candidate-identity",
        "--state-dir",
        str(state_dir),
        "--correction-file",
        str(correction_path),
    ]

    assert main([*argv, "--dry-run"]) == 0
    preview = capsys.readouterr().out
    assert "identity_correction=dry-run" in preview
    assert f"canonical_identity_key={CANONICAL_KEY}" in preview
    assert f"canonical_title={CANONICAL_TITLE}" in preview
    assert "canonical_authors=Jonathan Scott" in preview
    assert f"source_identity_key={REVIEWED_KEY}" in preview
    assert f"source_identity_key={REPEAT_KEY}" in preview
    assert "rewritten_references=3" in preview
    assert "preserved_editor_status=reject" in preview
    assert json.dumps(EDITOR_NOTE, ensure_ascii=False) in preview
    assert "description_resolution=explicit" in preview
    assert f"description_source_identity_key={REPEAT_KEY}" in preview
    assert "description_provenance=codex-generated" in preview
    assert f"description_evidence_basis={ARTICLE_URL}" in preview
    assert f"description_discarded_identity_key={REVIEWED_KEY}" in preview
    assert json.dumps(REPEAT_DESCRIPTION, ensure_ascii=False) in preview
    assert read_state(state_dir, "candidate_history") == [
        _repeat_record(),
        _reviewed_record(),
    ]

    assert main(argv) == 0
    applied = capsys.readouterr().out
    assert "identity_correction=applied" in applied
    history = read_state(state_dir, "candidate_history")
    assert [record["identity_key"] for record in history] == [CANONICAL_KEY]


def test_cli_reports_absent_resolution_for_a_conflict_free_merge(tmp_path, capsys):
    state_dir = _state(tmp_path)
    repeat = _repeat_record()
    repeat["content_description"] = REVIEWED_DESCRIPTION
    repeat["description_provenance"] = dict(_reviewed_record()["description_provenance"])
    seed_state(state_dir, candidate_history=[_reviewed_record(), repeat])
    correction_path = tmp_path / "correction.json"
    _write(correction_path, _correction())

    assert (
        main(
            [
                "merge-candidate-identity",
                "--state-dir",
                str(state_dir),
                "--correction-file",
                str(correction_path),
                "--dry-run",
            ]
        )
        == 0
    )

    assert "description_resolution=none" in capsys.readouterr().out


def test_weekly_review_renders_one_canonical_book_after_the_merge(tmp_path):
    state_dir = _state(tmp_path)
    history_before = read_state(state_dir, "candidate_history")
    rows_before, _ = assemble_cumulative_review([], history_before, [])
    assert len([row for row in rows_before if "renovation planner" in row["identity_key"]]) == 2

    apply_identity_correction(state_dir, _correction(_resolution()))

    history_after = read_state(state_dir, "candidate_history")
    rows_after, _ = assemble_cumulative_review([], history_after, [])
    merged = [row for row in rows_after if "renovation planner" in row["identity_key"]]
    assert len(merged) == 1
    assert merged[0]["identity_key"] == CANONICAL_KEY
    assert merged[0]["title"] == CANONICAL_TITLE


def test_resolution_reports_a_record_discarded_only_over_its_provenance(tmp_path):
    """Same text under a different provenance is still a discarded alternative."""
    state_dir = _state(tmp_path)
    repeat = _repeat_record()
    repeat["content_description"] = REVIEWED_DESCRIPTION
    repeat["description_provenance"] = {
        "applied_date": "2026-08-21",
        "content_description": REVIEWED_DESCRIPTION,
        "evidence_basis": [LANDING_URL],
        "identity_key": REPEAT_KEY,
        "provenance": "backfilled",
    }
    seed_state(state_dir, candidate_history=[_reviewed_record(), repeat])
    resolution = {
        "canonical_source_identity_key": REVIEWED_KEY,
        "observed_descriptions": {REVIEWED_KEY: REVIEWED_DESCRIPTION, REPEAT_KEY: REVIEWED_DESCRIPTION},
        "observed_description_provenance": {
            REVIEWED_KEY: _reviewed_record()["description_provenance"],
            REPEAT_KEY: repeat["description_provenance"],
        },
    }

    report = apply_identity_correction(state_dir, _correction(resolution))

    assert report["description_resolution"]["discarded_source_identity_keys"] == [REPEAT_KEY]
    record = read_state(state_dir, "candidate_history")[0]
    assert record["description_provenance"]["provenance"] == "codex-generated"
    assert record["description_provenance"]["evidence_basis"] == [ARTICLE_URL]
    assert record["description_merge_resolution"]["discarded_source_identity_keys"] == [REPEAT_KEY]


def test_resolution_discards_nothing_when_the_other_record_offers_nothing(tmp_path):
    """A record with no description of its own is not a discarded alternative."""
    state_dir = _state(tmp_path)
    repeat = _repeat_record()
    repeat["content_description"] = ""
    repeat.pop("description_provenance")
    seed_state(state_dir, candidate_history=[_reviewed_record(), repeat])
    resolution = {
        "canonical_source_identity_key": REVIEWED_KEY,
        "observed_descriptions": {REVIEWED_KEY: REVIEWED_DESCRIPTION},
        "observed_description_provenance": {REVIEWED_KEY: _reviewed_record()["description_provenance"]},
    }

    report = apply_identity_correction(state_dir, _correction(resolution))

    assert report["description_resolution"]["discarded_source_identity_keys"] == []


def test_observed_description_ignores_only_surrounding_whitespace(tmp_path):
    """Padding is not a change; anything else in the text is."""
    state_dir = _state(tmp_path)
    padded = dict(_resolution())
    padded["observed_descriptions"] = {
        REVIEWED_KEY: f"  {REVIEWED_DESCRIPTION}\n",
        REPEAT_KEY: REPEAT_DESCRIPTION,
    }

    report = apply_identity_correction(state_dir, _correction(padded), dry_run=True)
    assert report["content_description"] == REPEAT_DESCRIPTION

    inner = dict(_resolution())
    inner["observed_descriptions"] = {
        REVIEWED_KEY: REVIEWED_DESCRIPTION.replace("книга", "книгa"),
        REPEAT_KEY: REPEAT_DESCRIPTION,
    }
    with pytest.raises(ValueError, match="stale"):
        apply_identity_correction(state_dir, _correction(inner), dry_run=True)


# --- Finding 1: the retired durable identity keeps the reject decision --------


def _alias_table() -> dict:
    return {
        "version": 2,
        "aliases": {REVIEWED_KEY: REVIEWED_ID, REPEAT_KEY: REPEAT_ID},
        "candidates": {
            REVIEWED_ID: {
                "candidate_id": REVIEWED_ID,
                "canonical_identity_key": REVIEWED_KEY,
                "title": "The Ultimate Renovation Planner",
                "primary_authors": ["Jonathan Scott"],
            },
            REPEAT_ID: {
                "candidate_id": REPEAT_ID,
                "canonical_identity_key": REPEAT_KEY,
                "title": CANONICAL_TITLE,
                "primary_authors": ["Jonathan Scott"],
            },
        },
    }


def _state_with_aliases(tmp_path: Path) -> Path:
    state_dir = _state(tmp_path)
    seed_state(state_dir, candidate_identity_aliases=_alias_table())
    return state_dir


def test_apply_redirects_the_retired_durable_id_to_the_survivor(tmp_path):
    """The old identity must still reach the reject decision after the merge."""
    state_dir = _state_with_aliases(tmp_path)

    report = apply_identity_correction(state_dir, _correction(_resolution()))

    assert report["retired_candidate_ids"] == [REVIEWED_ID]
    assert report["survivor_candidate_id"] == REPEAT_ID
    table = read_state(state_dir, "candidate_identity_aliases")
    assert effective_candidate_id(REVIEWED_ID, table) == REPEAT_ID
    assert table["candidates"][REVIEWED_ID]["redirect_to"] == REPEAT_ID
    # A recapture under the old title resolves to the surviving record, which
    # is the one holding the editor's reject.
    recaptured = {"identity_key": REVIEWED_KEY, "title": "The Ultimate Renovation Planner"}
    assert resolve_candidate_id(recaptured, table) == REPEAT_ID
    history = read_state(state_dir, "candidate_history")
    survivor = [record for record in history if record["candidate_id"] == REPEAT_ID]
    assert len(survivor) == 1
    assert survivor[0]["editor_status"] == "reject"


def test_dry_run_reports_the_durable_redirect_without_writing_it(tmp_path):
    state_dir = _state_with_aliases(tmp_path)
    before = _snapshot(state_dir)

    report = apply_identity_correction(state_dir, _correction(_resolution()), dry_run=True)

    assert _snapshot(state_dir) == before
    assert report["retired_candidate_ids"] == [REVIEWED_ID]
    assert report["survivor_candidate_id"] == REPEAT_ID


def test_merge_without_an_alias_table_records_no_redirect(tmp_path):
    state_dir = _state(tmp_path)

    report = apply_identity_correction(state_dir, _correction(_resolution()))

    assert report["retired_candidate_ids"] == []
    assert not (state_dir / "candidate-identity-aliases.json").exists()


# --- Finding 2: every retained reference follows the canonical identity -------


def _latest_row() -> dict:
    return {
        "identity_key": REVIEWED_KEY,
        "candidate_id": REVIEWED_ID,
        "title": "The Ultimate Renovation Planner",
        "primary_authors": ["Jonathan Scott"],
        "content_description": REVIEWED_DESCRIPTION,
        "description_provenance": _reviewed_record()["description_provenance"],
        "source_evidence": [_evidence("Карточка PEOPLE.", ARTICLE_URL)],
    }


def test_single_retained_reference_follows_the_canonical_identity(tmp_path):
    state_dir = _state_with_aliases(tmp_path)
    seed_state(state_dir, latest_scouting_candidates=[_latest_row()])

    apply_identity_correction(state_dir, _correction(_resolution()))

    row = read_state(state_dir, "latest_scouting_candidates")[0]
    assert row["identity_key"] == CANONICAL_KEY
    assert row["candidate_id"] == REPEAT_ID
    assert row["title"] == CANONICAL_TITLE
    assert row["primary_authors"] == ["Jonathan Scott"]
    assert row["content_description"] == REPEAT_DESCRIPTION
    assert row["description_provenance"]["identity_key"] == CANONICAL_KEY
    assert row["description_provenance"]["content_description"] == REPEAT_DESCRIPTION
    table = read_state(state_dir, "candidate_identity_aliases")
    assert not has_conflicting_aliases(row, table)


def test_retained_reference_keeps_its_own_shape(tmp_path):
    """A row that never carried a title does not gain one."""
    state_dir = _state_with_aliases(tmp_path)

    apply_identity_correction(state_dir, _correction(_resolution()))

    archived = read_state(state_dir, "archived_editor_rows")[0]
    assert archived["identity_key"] == CANONICAL_KEY
    assert set(archived) == {"identity_key", "Candidate ID", "Editor Note"}
    visibility = read_state(state_dir, "candidate_visibility")[0]
    assert set(visibility) == {"identity_key", "visibility", "audit_log"}


# --- Finding 3: the provenance selection is pinned and checked ----------------


def test_provenance_changed_after_preview_fails_before_any_write(tmp_path):
    state_dir = _state_with_aliases(tmp_path)
    moved = _repeat_record()
    moved["description_provenance"] = dict(moved["description_provenance"])
    moved["description_provenance"]["evidence_basis"] = ["https://people.com/some-other-article-99999999"]
    seed_state(state_dir, candidate_history=[_reviewed_record(), moved])
    before = _snapshot(state_dir)

    with pytest.raises(ValueError, match="stale"):
        apply_identity_correction(state_dir, _correction(_resolution()))

    assert _snapshot(state_dir) == before


def test_missing_observed_provenance_fails_before_any_write(tmp_path):
    state_dir = _state_with_aliases(tmp_path)
    without = dict(_resolution())
    without.pop("observed_description_provenance")
    before = _snapshot(state_dir)

    with pytest.raises(ValueError, match="observed_description_provenance"):
        apply_identity_correction(state_dir, _correction(without))

    assert _snapshot(state_dir) == before


def test_provenance_describing_other_text_is_refused(tmp_path):
    state_dir = _state_with_aliases(tmp_path)
    mismatched = _repeat_record()
    mismatched["description_provenance"] = dict(mismatched["description_provenance"])
    mismatched["description_provenance"]["content_description"] = REVIEWED_DESCRIPTION
    seed_state(state_dir, candidate_history=[_reviewed_record(), mismatched])
    resolution = dict(_resolution())
    resolution["observed_description_provenance"] = {
        REVIEWED_KEY: _reviewed_record()["description_provenance"],
        REPEAT_KEY: mismatched["description_provenance"],
    }
    before = _snapshot(state_dir)

    with pytest.raises(ValueError, match="describes a different content_description"):
        apply_identity_correction(state_dir, _correction(resolution))

    assert _snapshot(state_dir) == before


# --- Finding 4: a failed write leaves nothing half-merged --------------------


def _fail_state_write(monkeypatch, on_call: int) -> None:
    """Fail the *on_call*-th file replacement when the State Store publishes a transaction."""
    real_publish_bundle = json_backend.publish_bundle
    calls = {"count": 0}

    def failing_replace(source: Path, destination: Path) -> None:
        calls["count"] += 1
        if calls["count"] == on_call:
            raise OSError("no space left on device")
        safe_artifacts.staged_replace(source, destination)

    def publish_bundle(paths, contents, **_kwargs):
        return real_publish_bundle(paths, contents, replace=failing_replace)

    monkeypatch.setattr(json_backend, "publish_bundle", publish_bundle)
    fail_database_commits(monkeypatch)


def test_failed_write_restores_every_state_file_and_the_retry_succeeds(tmp_path, monkeypatch):
    state_dir = _state_with_aliases(tmp_path)
    seed_state(state_dir, latest_scouting_candidates=[_latest_row()])
    before = _snapshot(state_dir)

    # History, aliases and the latest candidates are one transaction; the second file fails.
    _fail_state_write(monkeypatch, on_call=2)
    with pytest.raises(OSError):
        apply_identity_correction(state_dir, _correction(_resolution()))
    assert _snapshot(state_dir) == before
    assert not list(state_dir.glob(".*.tmp")) and not list(state_dir.glob(".*.stage"))

    monkeypatch.undo()
    report = apply_identity_correction(state_dir, _correction(_resolution()))

    assert report["canonical_identity_key"] == CANONICAL_KEY
    history = read_state(state_dir, "candidate_history")
    assert [record["identity_key"] for record in history] == [CANONICAL_KEY]


def test_cli_reports_a_write_failure_instead_of_raising(tmp_path, capsys, monkeypatch):
    state_dir = _state_with_aliases(tmp_path)
    correction_path = tmp_path / "correction.json"
    _write(correction_path, _correction(_resolution()))

    _fail_state_write(monkeypatch, on_call=1)
    result = main(
        [
            "merge-candidate-identity",
            "--state-dir",
            str(state_dir),
            "--correction-file",
            str(correction_path),
        ]
    )

    assert result == 1
    assert "error=" in capsys.readouterr().out


# --- Finding 5: description-attempt history survives the merge ---------------


def _attempt(date: str, reason: str, operation_id: str = "") -> dict:
    attempt = {"date": date, "outcome": "attempted_no_description", "reason_code": reason}
    if operation_id:
        attempt["operation_id"] = operation_id
    return attempt


def test_merge_keeps_the_description_attempts_of_both_records(tmp_path):
    state_dir = _state(tmp_path)
    reviewed = _reviewed_record()
    reviewed["grounded_description_attempts"] = [
        _attempt("2026-08-17", "source_identity_disagreement"),
        _attempt("2026-08-20", "title_mismatch"),
        _attempt("2026-09-03", "not_attempted_stage_budget", "shared-operation"),
    ]
    repeat = _repeat_record()
    repeat["grounded_description_attempts"] = [
        _attempt("2026-08-21", "title_mismatch"),
        _attempt("2026-09-03", "not_attempted_stage_budget", "shared-operation"),
    ]
    seed_state(state_dir, candidate_history=[reviewed, repeat])

    apply_identity_correction(state_dir, _correction(_resolution()))

    record = read_state(state_dir, "candidate_history")[0]
    assert [item["date"] for item in record["grounded_description_attempts"]] == [
        "2026-08-17",
        "2026-08-20",
        "2026-08-21",
        "2026-09-03",
    ]


# Product rule changed in #1023, authorized by the operator (Dima) on
# 2026-11-12: two different attempts under one operation_id no longer refuse
# a merge that carries a description_resolution. The attempt of the record
# the resolution chose is kept and the others move to the audit record. The
# refusal stays when the correction resolves nothing, or when the chosen
# record has no attempt of its own for the conflicting operation.
def test_conflicting_attempts_under_one_operation_id_follow_the_description_resolution(tmp_path):
    state_dir = _state(tmp_path)
    reviewed = _reviewed_record()
    reviewed["grounded_description_attempts"] = [_attempt("2026-09-03", "title_mismatch", "shared-operation")]
    repeat = _repeat_record()
    repeat["grounded_description_attempts"] = [_attempt("2026-09-03", "author_mismatch", "shared-operation")]
    seed_state(state_dir, candidate_history=[reviewed, repeat])
    before = _snapshot(state_dir)

    report = apply_identity_correction(state_dir, _correction(_resolution()), dry_run=True)

    assert _snapshot(state_dir) == before
    discarded = {
        "source_identity_key": REVIEWED_KEY,
        **_attempt("2026-09-03", "title_mismatch", "shared-operation"),
    }
    assert report["description_resolution"]["discarded_description_attempts"] == [discarded]

    apply_identity_correction(state_dir, _correction(_resolution()))

    record = read_state(state_dir, "candidate_history")[0]
    assert record["grounded_description_attempts"] == [_attempt("2026-09-03", "author_mismatch", "shared-operation")]
    assert record["description_merge_resolution"]["discarded_description_attempts"] == [discarded]
    assert record["description_merge_resolution"]["chosen_source_identity_key"] == REPEAT_KEY


def test_conflicting_attempts_without_a_description_resolution_are_refused(tmp_path):
    state_dir = _state(tmp_path)
    reviewed = _reviewed_record()
    repeat = _repeat_record()
    # Same description on both, so only the attempts conflict.
    del reviewed["description_provenance"], repeat["description_provenance"]
    repeat["content_description"] = REVIEWED_DESCRIPTION
    reviewed["grounded_description_attempts"] = [_attempt("2026-09-03", "title_mismatch", "shared-operation")]
    repeat["grounded_description_attempts"] = [_attempt("2026-09-03", "author_mismatch", "shared-operation")]
    seed_state(state_dir, candidate_history=[reviewed, repeat])
    before = _snapshot(state_dir)

    with pytest.raises(
        ValueError,
        match="conflicting grounded_description_attempts for operation shared-operation require explicit resolution",
    ):
        apply_identity_correction(state_dir, _correction())

    assert _snapshot(state_dir) == before


def test_conflicting_attempts_are_refused_when_the_chosen_record_has_no_attempt(tmp_path):
    third_key = "ultimate renovation planner|jonathan scott"
    state_dir = _state(tmp_path)
    reviewed = _reviewed_record()
    reviewed["grounded_description_attempts"] = [_attempt("2026-09-03", "title_mismatch", "shared-operation")]
    repeat = _repeat_record()
    third = {
        "identity_key": third_key,
        "title": "Ultimate Renovation Planner",
        "primary_authors": ["Jonathan Scott"],
        "grounded_description_attempts": [_attempt("2026-09-03", "author_mismatch", "shared-operation")],
    }
    seed_state(state_dir, candidate_history=[reviewed, repeat, third])
    correction = _correction(_resolution())
    correction["source_identity_keys"].append(third_key)
    before = _snapshot(state_dir)

    with pytest.raises(ValueError, match="grounded_description_attempts for operation shared-operation"):
        apply_identity_correction(state_dir, correction)

    assert _snapshot(state_dir) == before


def test_three_record_merge_keeps_the_chosen_attempt_and_audits_the_others(tmp_path):
    third_key = "ultimate renovation planner|jonathan scott"
    state_dir = _state(tmp_path)
    reviewed = _reviewed_record()
    reviewed["grounded_description_attempts"] = [_attempt("2026-09-03", "title_mismatch", "shared-operation")]
    repeat = _repeat_record()
    repeat["grounded_description_attempts"] = [
        _attempt("2026-08-21", "title_mismatch"),
        _attempt("2026-09-03", "author_mismatch", "shared-operation"),
    ]
    third = {
        "identity_key": third_key,
        "title": "Ultimate Renovation Planner",
        "primary_authors": ["Jonathan Scott"],
        "grounded_description_attempts": [
            _attempt("2026-09-03", "source_identity_disagreement", "shared-operation"),
            # Identical to the chosen attempt, so it is the same event, not a discarded one.
            _attempt("2026-09-03", "author_mismatch", "shared-operation"),
        ],
    }
    seed_state(state_dir, candidate_history=[reviewed, repeat, third])
    correction = _correction(_resolution())
    correction["source_identity_keys"].append(third_key)

    apply_identity_correction(state_dir, correction)

    history = read_state(state_dir, "candidate_history")
    assert len(history) == 1
    record = history[0]
    assert record["grounded_description_attempts"] == [
        _attempt("2026-08-21", "title_mismatch"),
        _attempt("2026-09-03", "author_mismatch", "shared-operation"),
    ]
    assert sorted(
        (item["source_identity_key"], item["reason_code"])
        for item in record["description_merge_resolution"]["discarded_description_attempts"]
    ) == [(REVIEWED_KEY, "title_mismatch"), (third_key, "source_identity_disagreement")]


def test_a_later_merge_keeps_attempts_an_earlier_merge_discarded(tmp_path):
    later_key = "ultimate renovation planner and organiser|jonathan scott"
    state_dir = _state(tmp_path)
    reviewed = _reviewed_record()
    reviewed["grounded_description_attempts"] = [_attempt("2026-09-03", "title_mismatch", "shared-operation")]
    repeat = _repeat_record()
    repeat["grounded_description_attempts"] = [_attempt("2026-09-03", "author_mismatch", "shared-operation")]
    seed_state(state_dir, candidate_history=[reviewed, repeat])
    apply_identity_correction(state_dir, _correction(_resolution()))
    first = read_state(state_dir, "candidate_history")[0]
    later = {
        "identity_key": later_key,
        "title": "Ultimate Renovation Planner and Organiser",
        "primary_authors": ["Jonathan Scott"],
        "grounded_description_attempts": [_attempt("2026-10-01", "title_mismatch", "later-operation")],
    }
    seed_state(state_dir, candidate_history=[first, later])

    # No description conflict this time, so the later merge carries no resolution.
    apply_identity_correction(
        state_dir,
        {
            "source_identity_keys": [later_key],
            "canonical": {
                "identity_key": CANONICAL_KEY,
                "title": CANONICAL_TITLE,
                "primary_authors": ["Jonathan Scott"],
            },
        },
    )

    record = read_state(state_dir, "candidate_history")[0]
    assert record["description_merge_resolution"]["discarded_description_attempts"] == [
        {"source_identity_key": REVIEWED_KEY, **_attempt("2026-09-03", "title_mismatch", "shared-operation")}
    ]
    assert [item["operation_id"] for item in record["grounded_description_attempts"]] == [
        "shared-operation",
        "later-operation",
    ]


def test_cli_preview_names_each_discarded_attempt(tmp_path, capsys):
    state_dir = _state(tmp_path)
    reviewed = _reviewed_record()
    reviewed["grounded_description_attempts"] = [_attempt("2026-09-03", "title_mismatch", "shared-operation")]
    repeat = _repeat_record()
    repeat["grounded_description_attempts"] = [_attempt("2026-09-03", "author_mismatch", "shared-operation")]
    seed_state(state_dir, candidate_history=[reviewed, repeat])
    correction_path = tmp_path / "correction.json"
    _write(correction_path, _correction(_resolution()))

    assert (
        main(
            [
                "merge-candidate-identity",
                "--state-dir",
                str(state_dir),
                "--correction-file",
                str(correction_path),
                "--dry-run",
            ]
        )
        == 0
    )

    preview = capsys.readouterr().out
    lines = [line for line in preview.splitlines() if line.startswith("description_discarded_attempt=")]
    assert len(lines) == 1
    assert json.loads(lines[0].split("=", 1)[1]) == {
        "source_identity_key": REVIEWED_KEY,
        **_attempt("2026-09-03", "title_mismatch", "shared-operation"),
    }


def test_snapshot_rows_follow_the_chosen_attempt(tmp_path):
    """Merged snapshot rows show the attempt log the history merge kept."""
    state_dir = _state(tmp_path)
    reviewed = _reviewed_record()
    reviewed["grounded_description_attempts"] = [_attempt("2026-09-03", "title_mismatch", "shared-operation")]
    repeat = _repeat_record()
    repeat["grounded_description_attempts"] = [_attempt("2026-09-03", "author_mismatch", "shared-operation")]
    seed_state(state_dir, candidate_history=[reviewed, repeat])
    seed_state(state_dir, latest_scouting_candidates=[reviewed, repeat])

    apply_identity_correction(state_dir, _correction(_resolution()))

    rows = read_state(state_dir, "latest_scouting_candidates")
    assert len(rows) == 1
    assert rows[0]["grounded_description_attempts"] == [_attempt("2026-09-03", "author_mismatch", "shared-operation")]


def test_cli_preview_names_the_retired_durable_id(tmp_path, capsys):
    state_dir = _state_with_aliases(tmp_path)
    correction_path = tmp_path / "correction.json"
    _write(correction_path, _correction(_resolution()))

    assert (
        main(
            [
                "merge-candidate-identity",
                "--state-dir",
                str(state_dir),
                "--correction-file",
                str(correction_path),
                "--dry-run",
            ]
        )
        == 0
    )

    preview = capsys.readouterr().out
    assert f"survivor_candidate_id={REPEAT_ID}" in preview
    assert f"retired_candidate_id={REVIEWED_ID}" in preview


def test_repeated_attempts_without_an_operation_id_are_all_kept(tmp_path):
    """Two identical attempts are two attempts, not one."""
    state_dir = _state(tmp_path)
    reviewed = _reviewed_record()
    reviewed["grounded_description_attempts"] = [
        _attempt("2026-08-20", "title_mismatch"),
        _attempt("2026-08-20", "title_mismatch"),
    ]
    repeat = _repeat_record()
    repeat["grounded_description_attempts"] = [_attempt("2026-08-20", "title_mismatch")]
    seed_state(state_dir, candidate_history=[reviewed, repeat])

    apply_identity_correction(state_dir, _correction(_resolution()))

    record = read_state(state_dir, "candidate_history")[0]
    assert len(record["grounded_description_attempts"]) == 3


# --- Review round 2, finding 1: one correction at a time per state ------------

OTHER_KEYS = ("some other book|first author", "some other book and more|first author")
OTHER_CANONICAL = OTHER_KEYS[1]


def _other_pair() -> list[dict]:
    return [
        {
            "identity_key": OTHER_KEYS[0],
            "candidate_id": "candidate-000000000000000000000001",
            "title": "Some Other Book",
            "primary_authors": ["First Author"],
            "first_seen_date": "2026-08-01",
            "last_seen_date": "2026-08-26",
            "editor_status": "interested",
            "source_evidence": [_evidence("Первый источник.", "https://example.test/one")],
        },
        {
            "identity_key": OTHER_KEYS[1],
            "candidate_id": "candidate-000000000000000000000002",
            "title": "Some Other Book and More",
            "primary_authors": ["First Author"],
            "first_seen_date": "2026-08-05",
            "last_seen_date": "2026-08-26",
            "source_evidence": [_evidence("Второй источник.", "https://example.test/two")],
        },
    ]


def _other_correction() -> dict:
    return {
        "source_identity_keys": list(OTHER_KEYS),
        "canonical": {
            "identity_key": OTHER_CANONICAL,
            "title": "Some Other Book and More",
            "primary_authors": ["First Author"],
        },
    }


def test_concurrent_disjoint_merges_both_survive(tmp_path, monkeypatch):
    """Planning must not read state another correction is about to replace."""
    state_dir = _state_with_aliases(tmp_path)
    seed_state(state_dir, candidate_history=[_reviewed_record(), _repeat_record(), *_other_pair()])

    publishing = threading.Event()
    release = threading.Event()
    original = json_backend.publish_bundle

    def hold_the_first_commit() -> None:
        if threading.current_thread().name == "merge-renovation" and not publishing.is_set():
            publishing.set()
            release.wait(timeout=10)

    def gated_publish(paths, contents, **kwargs):
        hold_the_first_commit()
        return original(paths, contents, **kwargs)

    # The same hold for the SQLite backend, whose commit is the database's.
    before_database_commit(monkeypatch, hold_the_first_commit)

    results: dict[str, object] = {}

    def run(name: str, correction: dict) -> None:
        try:
            results[name] = apply_identity_correction(state_dir, correction)
        except BaseException as exc:
            results[name] = exc

    first = threading.Thread(target=run, name="merge-renovation", args=("renovation", _correction(_resolution())))
    second = threading.Thread(target=run, name="merge-other", args=("other", _other_correction()))
    try:
        json_backend.publish_bundle = gated_publish
        first.start()
        assert publishing.wait(timeout=10)
        second.start()
        # Give the second correction time to reach the lock. It must block
        # there rather than plan against the half-published state.
        time.sleep(0.2)
        release.set()
        first.join(timeout=30)
        second.join(timeout=30)
    finally:
        json_backend.publish_bundle = original
        release.set()

    assert not first.is_alive() and not second.is_alive()
    for name in ("renovation", "other"):
        assert not isinstance(results[name], BaseException), results[name]
    history = read_state(state_dir, "candidate_history")
    assert sorted(record["identity_key"] for record in history) == sorted([CANONICAL_KEY, OTHER_CANONICAL])
    table = read_state(state_dir, "candidate_identity_aliases")
    assert effective_candidate_id(REVIEWED_ID, table) == REPEAT_ID
    assert effective_candidate_id("candidate-000000000000000000000001", table) == "candidate-000000000000000000000002"


def test_the_state_lock_does_not_depend_on_the_planned_file_set(tmp_path):
    """Two corrections planning different files still exclude each other."""
    state_dir = _state_with_aliases(tmp_path)
    seed_state(state_dir, candidate_history=[_reviewed_record(), _repeat_record(), *_other_pair()])
    held = threading.Event()
    blocked = threading.Event()

    def hold() -> None:
        with identity_corrections._state_write_lock(state_dir):
            held.set()
            blocked.wait(timeout=10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(timeout=10)
        done = threading.Event()

        def merge() -> None:
            apply_identity_correction(state_dir, _other_correction())
            done.set()

        worker = threading.Thread(target=merge)
        worker.start()
        assert not done.wait(timeout=0.5), "the correction planned while another held the state lock"
    finally:
        blocked.set()
        holder.join(timeout=10)
    worker.join(timeout=30)
    assert done.is_set()


# --- Review round 2, finding 2: obsolete reference provenance is removed ------


def test_reference_drops_provenance_when_the_chosen_description_has_none(tmp_path):
    state_dir = _state_with_aliases(tmp_path)
    repeat = _repeat_record()
    repeat.pop("description_provenance")
    seed_state(state_dir, candidate_history=[_reviewed_record(), repeat])
    seed_state(state_dir, latest_scouting_candidates=[_latest_row()])
    resolution = {
        "canonical_source_identity_key": REPEAT_KEY,
        "observed_descriptions": {REVIEWED_KEY: REVIEWED_DESCRIPTION, REPEAT_KEY: REPEAT_DESCRIPTION},
        "observed_description_provenance": {REVIEWED_KEY: _reviewed_record()["description_provenance"]},
    }

    apply_identity_correction(state_dir, _correction(resolution))

    record = read_state(state_dir, "candidate_history")[0]
    assert record["content_description"] == REPEAT_DESCRIPTION
    assert "description_provenance" not in record
    row = read_state(state_dir, "latest_scouting_candidates")[0]
    assert row["content_description"] == REPEAT_DESCRIPTION
    assert "description_provenance" not in row


def test_snapshot_rows_without_the_chosen_record_take_the_history_attempts(tmp_path):
    """A snapshot that lacks the chosen record's row does not refuse a merge history resolves (#1023 review)."""
    third_key = "ultimate renovation planner|jonathan scott"
    state_dir = _state(tmp_path)
    reviewed = _reviewed_record()
    reviewed["grounded_description_attempts"] = [_attempt("2026-09-03", "title_mismatch", "shared-operation")]
    repeat = _repeat_record()
    repeat["grounded_description_attempts"] = [_attempt("2026-09-03", "author_mismatch", "shared-operation")]
    third = {
        "identity_key": third_key,
        "title": "Ultimate Renovation Planner",
        "primary_authors": ["Jonathan Scott"],
        "grounded_description_attempts": [_attempt("2026-09-03", "no_results", "shared-operation")],
    }
    seed_state(state_dir, candidate_history=[reviewed, repeat, third])
    seed_state(state_dir, latest_scouting_candidates=[reviewed, third])
    correction = _correction(_resolution())
    correction["source_identity_keys"].append(third_key)

    apply_identity_correction(state_dir, correction)

    rows = read_state(state_dir, "latest_scouting_candidates")
    assert len(rows) == 1
    assert rows[0]["grounded_description_attempts"] == [_attempt("2026-09-03", "author_mismatch", "shared-operation")]


def test_single_snapshot_row_from_a_discarded_record_shows_the_history_attempts(tmp_path):
    state_dir = _state(tmp_path)
    reviewed = _reviewed_record()
    reviewed["grounded_description_attempts"] = [_attempt("2026-09-03", "title_mismatch", "shared-operation")]
    repeat = _repeat_record()
    repeat["grounded_description_attempts"] = [_attempt("2026-09-03", "author_mismatch", "shared-operation")]
    seed_state(state_dir, candidate_history=[reviewed, repeat])
    seed_state(state_dir, latest_scouting_candidates=[reviewed])

    apply_identity_correction(state_dir, _correction(_resolution()))

    rows = read_state(state_dir, "latest_scouting_candidates")
    assert rows[0]["identity_key"] == CANONICAL_KEY
    assert rows[0]["grounded_description_attempts"] == [_attempt("2026-09-03", "author_mismatch", "shared-operation")]
