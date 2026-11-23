"""Contract tests for the State Store (#1068, #1073, #1011).

Every test that takes ``open_store`` runs against each backend in
``STATE_BACKENDS`` and each layout Candidate History can have in a state
directory (one file, or one file per book), and observes state only through
the State Store, so the SQLite backend (#1074) joins the suite by being added
to that tuple. Tests named for the JSON backend check what only that backend
promises; the sharded layout's own promises are in
``test_state_store_sharded_layout.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from state_store_support import LAYOUTS
from state_store_support import SINGLE_FILE
from state_store_support import history_shards
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot
from state_store_support import store_selection
from state_store_support import use_layout

from autoscout.config import validate_config
from autoscout.editorial_taste_profile import TasteProfileError
from autoscout.state_store import SQLITE_BACKEND
from autoscout.state_store import STATE_BACKEND_ENV
from autoscout.state_store import STATE_BACKENDS
from autoscout.state_store import BackendSelection
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import StateStoreError
from autoscout.state_store import open_state_store
from autoscout.state_store import rebuild_from_json
from autoscout.state_store import select_state_backend

OpenStore = Callable[[], StateStore]

ALPHA = {"candidate_id": "candidate-aaaaaaaaaaaaaaaaaaaaaaaa", "identity_key": "alpha|one", "title": "Alpha"}
BETA = {"candidate_id": "candidate-bbbbbbbbbbbbbbbbbbbbbbbb", "identity_key": "beta|two", "title": "Бета"}
GAMMA = {"candidate_id": "candidate-cccccccccccccccccccccccc", "identity_key": "gamma|three", "title": "Gamma"}

PROFILE_ENTRY = {
    "id": "taste-0001",
    "kind": "hard_rule",
    "scope": "author",
    "subject": "Some Author",
    "verbatim_quote": "Этого автора не показывай.",
    "provenance": "conversation",
    "created_at": "2026-11-14",
    "created_by": "editor",
    "status": "active",
}

ALIAS_TABLE = {
    "version": 1,
    "aliases": {"alpha|one": "candidate-aaaaaaaaaaaaaaaaaaaaaaaa"},
    "candidates": {
        "candidate-aaaaaaaaaaaaaaaaaaaaaaaa": {
            "candidate_id": "candidate-aaaaaaaaaaaaaaaaaaaaaaaa",
            "identity_key": "alpha|one",
        }
    },
}

# One stored value per collection, already in the collection's canonical order.
STORED_VALUES: dict[Collection, Any] = {
    Collection.CANDIDATE_HISTORY: [ALPHA, BETA],
    Collection.EXCLUDED_CANDIDATE_HISTORY: [{"identity_key": "excluded|one", "reason": "fiction"}],
    Collection.CANDIDATE_VISIBILITY: [{"identity_key": "alpha|one", "visibility": "hidden"}],
    Collection.PENDING_EDITOR_ROWS: [{"identity_key": "zeta|z", "Editor Note": "Позже"}, {"identity_key": "alpha|one"}],
    Collection.ARCHIVED_EDITOR_ROWS: [{"identity_key": "alpha|one", "Editor Status": "reject"}],
    Collection.EDITORIAL_PROFILE: {"entries": [PROFILE_ENTRY]},
    Collection.CANDIDATE_IDENTITY_ALIASES: ALIAS_TABLE,
    Collection.SOURCE_PROPOSALS: [{"id": "proposal-1", "source_url": "https://example.test/"}],
    Collection.RANKING_FRESHNESS: [{"source": "b"}, {"source": "a"}],
    Collection.LATEST_SCOUTING_CANDIDATES: [ALPHA, GAMMA],
    Collection.SOURCE_FAILURE_STREAKS: [
        {"source_name": "Amazon Charts Most Read Nonfiction", "consecutive_failures": 3},
        {"source_name": "Spiegel Bestseller Sachbuch", "consecutive_failures": 0},
    ],
}

EMPTY_VALUES: dict[Collection, Any] = {
    collection: []
    for collection in Collection
    if collection not in {Collection.EDITORIAL_PROFILE, Collection.CANDIDATE_IDENTITY_ALIASES}
}
EMPTY_VALUES[Collection.EDITORIAL_PROFILE] = {"entries": []}
EMPTY_VALUES[Collection.CANDIDATE_IDENTITY_ALIASES] = {"version": 1, "aliases": {}, "candidates": {}}


@pytest.fixture(
    params=[(backend, layout) for backend in STATE_BACKENDS for layout in LAYOUTS],
    ids=lambda param: "-".join(param),
)
def open_store(request: pytest.FixtureRequest, tmp_path: Path) -> OpenStore:
    backend, layout = request.param
    selection = store_selection(backend, tmp_path)
    state_dir = tmp_path / "state"
    use_layout(state_dir, layout)
    if backend == SQLITE_BACKEND:
        # The layout's JSON is where the SQLite backend's export goes; the database starts from it.
        rebuild_from_json(state_dir, selection=selection)
    return lambda: open_state_store(state_dir, selection)


def _state_dir_bytes(state_dir: Path) -> dict[str, bytes]:
    """Every file under *state_dir*, by its relative path; directories are listed with empty bytes."""
    if not state_dir.exists():
        return {}
    return {
        path.relative_to(state_dir).as_posix(): b"" if path.is_dir() else path.read_bytes()
        for path in sorted(state_dir.rglob("*"))
    }


# ── Whole-collection read and replace ───────────────────────────────────────


def test_every_collection_has_a_stored_value_in_this_suite():
    assert set(STORED_VALUES) == set(Collection)
    assert set(EMPTY_VALUES) == set(Collection)


@pytest.mark.parametrize("collection", list(Collection), ids=lambda collection: collection.value)
def test_a_collection_never_written_reads_as_its_empty_value(open_store, collection):
    assert open_store().read(collection) == EMPTY_VALUES[collection]


@pytest.mark.parametrize("collection", list(Collection), ids=lambda collection: collection.value)
def test_a_replaced_collection_reads_back_equal_from_a_new_store(open_store, collection):
    open_store().replace(collection, STORED_VALUES[collection])

    assert open_store().read(collection) == STORED_VALUES[collection]


def test_candidate_records_are_stored_in_identity_key_order(open_store):
    open_store().replace(Collection.CANDIDATE_HISTORY, [GAMMA, ALPHA, BETA])

    assert open_store().read(Collection.CANDIDATE_HISTORY) == [ALPHA, BETA, GAMMA]


def test_pending_editor_rows_keep_the_order_they_were_staged_in(open_store):
    rows = [{"identity_key": "zeta|z"}, {"identity_key": "alpha|one"}]
    open_store().replace(Collection.PENDING_EDITOR_ROWS, rows)

    assert open_store().read(Collection.PENDING_EDITOR_ROWS) == rows


def test_a_record_list_collection_refuses_a_value_that_is_not_a_list(open_store):
    with pytest.raises(StateStoreError):
        open_store().replace(Collection.CANDIDATE_VISIBILITY, {"identity_key": "alpha|one"})


def test_an_invalid_editorial_profile_is_refused_and_nothing_is_written(open_store):
    store = open_store()
    store.replace(Collection.EDITORIAL_PROFILE, {"entries": [PROFILE_ENTRY]})

    with pytest.raises(TasteProfileError):
        store.replace(Collection.EDITORIAL_PROFILE, {"entries": [{"id": "taste-0002"}]})

    assert open_store().read(Collection.EDITORIAL_PROFILE) == {"entries": [PROFILE_ENTRY]}


def test_an_invalid_alias_table_is_refused(open_store):
    with pytest.raises(ValueError):
        open_store().replace(Collection.CANDIDATE_IDENTITY_ALIASES, {"aliases": []})


# ── Candidate History by Candidate ID ───────────────────────────────────────


def test_get_candidate_returns_the_record_with_that_candidate_id(open_store):
    open_store().replace(Collection.CANDIDATE_HISTORY, [ALPHA, BETA])

    assert open_store().get_candidate(BETA["candidate_id"]) == BETA
    assert open_store().get_candidate(GAMMA["candidate_id"]) is None


def test_upsert_candidate_inserts_a_new_record_and_replaces_an_existing_one(open_store):
    open_store().replace(Collection.CANDIDATE_HISTORY, [ALPHA, BETA])
    changed_beta = {**BETA, "editor_status": "interested"}

    open_store().upsert_candidate(GAMMA)
    open_store().upsert_candidate(changed_beta)

    assert open_store().read(Collection.CANDIDATE_HISTORY) == [ALPHA, changed_beta, GAMMA]


def test_delete_candidate_removes_only_that_record_and_reports_whether_it_existed(open_store):
    open_store().replace(Collection.CANDIDATE_HISTORY, [ALPHA, BETA])

    assert open_store().delete_candidate(ALPHA["candidate_id"]) is True
    assert open_store().delete_candidate(ALPHA["candidate_id"]) is False
    assert open_store().read(Collection.CANDIDATE_HISTORY) == [BETA]


def test_delete_candidate_inside_a_transaction_is_seen_by_its_reads_and_written_at_its_end(open_store):
    open_store().replace(Collection.CANDIDATE_HISTORY, [ALPHA, BETA, GAMMA])
    store = open_store()
    with store.transaction():
        assert store.delete_candidate(BETA["candidate_id"]) is True
        assert store.get_candidate(BETA["candidate_id"]) is None
        assert store.delete_candidate(BETA["candidate_id"]) is False
        assert store.read(Collection.CANDIDATE_HISTORY) == [ALPHA, GAMMA]
        assert open_store().read(Collection.CANDIDATE_HISTORY) == [ALPHA, BETA, GAMMA]

    assert open_store().read(Collection.CANDIDATE_HISTORY) == [ALPHA, GAMMA]


def test_a_record_deleted_and_upserted_again_in_one_transaction_is_kept(open_store):
    open_store().replace(Collection.CANDIDATE_HISTORY, [ALPHA, BETA])
    changed_alpha = {**ALPHA, "editor_status": "reject"}
    store = open_store()
    with store.transaction():
        store.delete_candidate(ALPHA["candidate_id"])
        store.upsert_candidate(changed_alpha)

    assert open_store().read(Collection.CANDIDATE_HISTORY) == [changed_alpha, BETA]


def test_replacing_the_history_inside_a_transaction_drops_records_staged_before(open_store):
    open_store().replace(Collection.CANDIDATE_HISTORY, [ALPHA])
    store = open_store()
    with store.transaction():
        store.upsert_candidate(BETA)
        store.replace(Collection.CANDIDATE_HISTORY, [GAMMA])
        assert store.get_candidate(BETA["candidate_id"]) is None
        assert store.get_candidate(ALPHA["candidate_id"]) is None

    assert open_store().read(Collection.CANDIDATE_HISTORY) == [GAMMA]


@pytest.mark.parametrize("record", [{"identity_key": "no|id"}, {"candidate_id": "  ", "identity_key": "blank|id"}])
def test_upsert_candidate_refuses_a_record_without_a_candidate_id(open_store, record):
    with pytest.raises(StateStoreError):
        open_store().upsert_candidate(record)

    assert open_store().read(Collection.CANDIDATE_HISTORY) == []


# ── Transactions ────────────────────────────────────────────────────────────


def test_a_transaction_writes_every_collection_when_the_block_completes(open_store):
    store = open_store()
    with store.transaction():
        store.replace(Collection.CANDIDATE_HISTORY, [ALPHA])
        store.replace(Collection.CANDIDATE_VISIBILITY, [{"identity_key": "alpha|one", "visibility": "hidden"}])
        store.upsert_candidate(BETA)

    reopened = open_store()
    assert reopened.read(Collection.CANDIDATE_HISTORY) == [ALPHA, BETA]
    assert reopened.read(Collection.CANDIDATE_VISIBILITY) == [{"identity_key": "alpha|one", "visibility": "hidden"}]


def test_reads_inside_a_transaction_see_its_staged_writes_and_other_stores_do_not(open_store):
    store = open_store()
    store.replace(Collection.CANDIDATE_HISTORY, [ALPHA])
    with store.transaction():
        store.upsert_candidate(BETA)
        assert store.read(Collection.CANDIDATE_HISTORY) == [ALPHA, BETA]
        assert store.get_candidate(BETA["candidate_id"]) == BETA
        assert open_store().read(Collection.CANDIDATE_HISTORY) == [ALPHA]


def test_an_exception_inside_a_transaction_writes_nothing(open_store):
    store = open_store()
    store.replace(Collection.CANDIDATE_HISTORY, [ALPHA])

    with pytest.raises(RuntimeError, match="interrupted"):
        with store.transaction():
            store.replace(Collection.CANDIDATE_HISTORY, [BETA])
            store.delete_candidate(BETA["candidate_id"])
            store.replace(Collection.PENDING_EDITOR_ROWS, [{"identity_key": "beta|two"}])
            store.replace(Collection.EDITORIAL_PROFILE, {"entries": [PROFILE_ENTRY]})
            raise RuntimeError("interrupted")

    reopened = open_store()
    assert reopened.read(Collection.CANDIDATE_HISTORY) == [ALPHA]
    assert reopened.read(Collection.PENDING_EDITOR_ROWS) == []
    assert reopened.read(Collection.EDITORIAL_PROFILE) == {"entries": []}


def test_a_store_can_start_a_new_transaction_after_one_failed(open_store):
    store = open_store()
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.replace(Collection.CANDIDATE_HISTORY, [BETA])
            raise RuntimeError("interrupted")

    with store.transaction():
        store.replace(Collection.CANDIDATE_HISTORY, [ALPHA])

    assert open_store().read(Collection.CANDIDATE_HISTORY) == [ALPHA]


def test_transactions_do_not_nest(open_store):
    store = open_store()
    with pytest.raises(StateStoreError), store.transaction():
        with store.transaction():
            pass


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_json_backend_leaves_the_state_directory_byte_identical_after_a_failed_transaction(tmp_path, layout):
    state_dir = tmp_path / "state"
    use_layout(state_dir, layout)
    seed_state(state_dir, candidate_history=[ALPHA, GAMMA], candidate_visibility=[])
    before = _state_dir_bytes(state_dir)
    store = open_state_store(state_dir)

    with pytest.raises(TasteProfileError):
        with store.transaction():
            store.replace(Collection.CANDIDATE_HISTORY, [BETA, GAMMA])
            store.delete_candidate(GAMMA["candidate_id"])
            store.replace(Collection.LATEST_SCOUTING_CANDIDATES, [BETA])
            store.replace(Collection.EDITORIAL_PROFILE, {"entries": [{"id": "broken"}]})

    assert _state_dir_bytes(state_dir) == before


def test_the_json_backend_writes_the_single_file_as_before_in_that_layout(tmp_path):
    state_dir = tmp_path / "state"
    use_layout(state_dir, SINGLE_FILE)
    seed_state(state_dir, candidate_history=[BETA, ALPHA])

    files = _state_dir_bytes(state_dir)
    assert list(files) == ["candidate-history.json"]
    expected = json.dumps([ALPHA, BETA], indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    assert files["candidate-history.json"] == expected.encode("utf-8")


# ── The seeding helper ──────────────────────────────────────────────────────


def test_the_seeding_helper_writes_and_reads_through_the_state_store(tmp_path):
    state_dir = tmp_path / "state"
    seed_state(state_dir, candidate_history=[BETA, ALPHA], editorial_profile={"entries": [PROFILE_ENTRY]})

    assert read_state(state_dir, "candidate_history") == [ALPHA, BETA]
    assert read_state(state_dir, Collection.EDITORIAL_PROFILE) == {"entries": [PROFILE_ENTRY]}
    with pytest.raises(ValueError):
        seed_state(state_dir, no_such_collection=[])


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_state_snapshot_covers_every_collection_and_sees_a_change(tmp_path, layout, state_backend):
    state_dir = tmp_path / "state"
    use_layout(state_dir, layout)
    seed_state(state_dir, candidate_history=[ALPHA])
    before = state_snapshot(state_dir)
    history_key = "candidate_history" if layout == SINGLE_FILE else "candidate_history_shards"

    assert {collection.value for collection in Collection} <= set(before)
    assert before["candidate_visibility"] is None and before[history_key] is not None
    assert (before["state_database"] is not None) == (state_backend == SQLITE_BACKEND)
    assert state_snapshot(state_dir) == before

    seed_state(state_dir, candidate_history=[ALPHA, BETA])
    assert state_snapshot(state_dir) != before
    seed_state(state_dir, candidate_history=[ALPHA])
    restored = state_snapshot(state_dir)
    database = restored.pop("state_database")
    assert restored == {name: value for name, value in before.items() if name != "state_database"}
    # The database keeps the change marker of every write, so a write that was undone still shows there.
    assert database is None if before["state_database"] is None else database != before["state_database"]

    seed_state(state_dir, candidate_visibility=[])
    assert state_snapshot(state_dir) != before
    if layout == SINGLE_FILE:
        (state_dir / "candidate-history.json").write_text("[ ]", encoding="utf-8")
        assert state_snapshot(state_dir)["candidate_history"] == b"[ ]"
    else:
        [shard] = history_shards(state_dir)
        (state_dir / shard).write_text("{ }", encoding="utf-8")
        assert state_snapshot(state_dir)["candidate_history_shards"].endswith(b"\0{ }\0")


# ── Backend selection ───────────────────────────────────────────────────────


def test_json_is_the_default_backend():
    selection = select_state_backend({}, environ={})

    assert selection == BackendSelection("json", "default")
    assert selection.describe() == {"backend": "json", "selected_by": "default"}


def test_the_weekly_config_chooses_the_backend_and_the_environment_overrides_it():
    assert select_state_backend({"state_backend": "json"}, environ={}).selected_by == "config"
    assert select_state_backend({"state_backend": "json"}, environ={STATE_BACKEND_ENV: "json"}).selected_by == (
        "environment"
    )


@pytest.mark.parametrize(
    ("config", "environ"),
    [({"state_backend": "postgres"}, {}), ({}, {STATE_BACKEND_ENV: "postgres"})],
)
def test_an_unknown_backend_is_refused_rather_than_replaced_by_the_default(config, environ):
    with pytest.raises(StateStoreError):
        select_state_backend(config, environ=environ)


def test_an_opened_store_describes_its_backend_and_how_it_was_chosen(tmp_path, monkeypatch):
    monkeypatch.setenv(STATE_BACKEND_ENV, "json")

    assert open_state_store(tmp_path).describe() == {"backend": "json", "selected_by": "environment"}


def test_config_validation_refuses_an_unknown_state_backend():
    assert validate_config({"sources": [], "state_backend": "json"}) == []
    assert validate_config({"sources": [], "state_backend": "sqlite", "state_database": "data/x.sqlite3"}) == []
    errors = validate_config({"sources": [], "state_backend": "postgres"})
    assert len(errors) == 1
    assert "state_backend" in errors[0]


def test_the_weekly_run_records_its_state_backend_in_the_workflow_log(tmp_path, monkeypatch):
    from test_weekly_workflow import _write_capture_fixture
    from test_weekly_workflow import _write_codex_fixture_config
    from test_weekly_workflow import _write_extraction_fixture

    from autoscout.weekly_workflow import deterministic_adapters
    from autoscout.weekly_workflow import run_weekly_workflow

    config = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture_fixture(tmp_path, output_dir)
    adapters = deterministic_adapters()
    adapters.capture.prepare(config, output_dir, live=False)
    _write_extraction_fixture(output_dir)
    # The default selection, which a run under --state-backend sqlite replaces with the environment's.
    monkeypatch.delenv(STATE_BACKEND_ENV, raising=False)

    result = run_weekly_workflow(config, output_dir, adapters=adapters)

    assert result.outcome.value.startswith("completed/"), result.failed_stage
    persisted = json.loads((output_dir / "workflow-log.json").read_text(encoding="utf-8"))
    start = next(entry for entry in persisted if entry["stage"] == "operation-start")
    assert start["detail"]["state_backend"] == {"backend": "json", "selected_by": "default"}


def test_a_weekly_run_with_an_unknown_state_backend_stops_before_it_collects(tmp_path, monkeypatch):
    from test_weekly_workflow import _write_codex_fixture_config

    from autoscout.weekly_workflow import WorkflowOutcome
    from autoscout.weekly_workflow import deterministic_adapters
    from autoscout.weekly_workflow import run_weekly_workflow

    monkeypatch.setenv(STATE_BACKEND_ENV, "postgres")
    config = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"

    result = run_weekly_workflow(config, output_dir, adapters=deterministic_adapters())

    assert result.outcome is WorkflowOutcome.INTERVENTION_REQUIRED
    assert result.failed_stage == "operation-start"
    assert not any(entry.get("stage") == "capture" for entry in result.log)


def test_a_weekly_run_hands_the_store_its_config_chose_to_every_stage_that_reads_state(tmp_path, monkeypatch):
    from test_weekly_workflow import _write_capture_fixture
    from test_weekly_workflow import _write_codex_fixture_config
    from test_weekly_workflow import _write_extraction_fixture

    from autoscout import weekly_workflow
    from autoscout.weekly_workflow import deterministic_adapters
    from autoscout.weekly_workflow import run_weekly_workflow

    monkeypatch.delenv(STATE_BACKEND_ENV, raising=False)
    config = _write_codex_fixture_config(tmp_path)
    config.write_text(
        json.dumps({**json.loads(config.read_text(encoding="utf-8")), "state_backend": "json"}), encoding="utf-8"
    )
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture_fixture(tmp_path, output_dir)
    adapters = deterministic_adapters()
    adapters.capture.prepare(config, output_dir, live=False)
    _write_extraction_fixture(output_dir)
    handed: dict[str, Any] = {}

    def recording(stage: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def stage_call(*args: Any, **kwargs: Any) -> Any:
            handed[stage] = kwargs.get("state_store")
            return real(*args, **kwargs)

        return stage_call

    monkeypatch.setattr(
        weekly_workflow, "_run_pipeline_stage", recording("pipeline", weekly_workflow._run_pipeline_stage)
    )
    monkeypatch.setattr(weekly_workflow, "_run_review_stage", recording("review", weekly_workflow._run_review_stage))

    result = run_weekly_workflow(config, output_dir, output_dir, adapters=adapters)

    assert result.outcome.value.startswith("completed/"), result.failed_stage
    assert set(handed) == {"pipeline", "review"}
    assert handed["pipeline"] is handed["review"]
    assert handed["pipeline"].describe() == {"backend": "json", "selected_by": "config"}
    assert read_state(output_dir, Collection.CANDIDATE_HISTORY)
