"""Tests for Editorial Taste Profile domain and CLI layer.

Covers:
- Filename (editorial-profile.json, legacy fallback)
- Malformed JSON → clear TasteProfileError
- Hard-rule scopes: author, topic, publisher
- Soft-preference persistence: direction, description, example_books
- hide_candidates_by_rules with all scopes
- apply_soft_preferences nudges and hard-floor enforcement
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from state_store_support import seed_state

from autoscout.editorial_taste_profile import TasteProfileError
from autoscout.editorial_taste_profile import active_hard_rules
from autoscout.editorial_taste_profile import active_soft_preferences
from autoscout.editorial_taste_profile import add_profile_entry
from autoscout.editorial_taste_profile import apply_soft_preferences
from autoscout.editorial_taste_profile import empty_profile
from autoscout.editorial_taste_profile import hide_candidates_by_rules
from autoscout.editorial_taste_profile import list_profile_entries
from autoscout.editorial_taste_profile import load_taste_profile
from autoscout.editorial_taste_profile import revoke_profile_entry
from autoscout.editorial_taste_profile import save_taste_profile
from autoscout.editorial_taste_profile import validate_taste_profile
from autoscout.scouting_candidates import ScoutingCandidate
from autoscout.state_store import Collection
from autoscout.state_store.json_backend import FILE_NAMES
from autoscout.state_store.json_backend import LEGACY_PROFILE_FILE_NAME as STATE_EDITORIAL_TASTE_PROFILE_LEGACY

# The profile's files, for the tests of how the JSON backend reads them.
STATE_EDITORIAL_PROFILE = FILE_NAMES[Collection.EDITORIAL_PROFILE]

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_candidate(
    title: str = "Test Book",
    authors: list[str] | None = None,
    category: str = "",
    publisher: str = "",
    priority_label: str = "C",
    visible_risk_status: str = "",
    risk_signal_scope: str = "",
) -> ScoutingCandidate:
    return ScoutingCandidate(
        {
            "title": title,
            "primary_authors": authors or ["Test Author"],
            "candidate_category": category,
            "publisher": publisher,
            "priority_label": priority_label,
            "visible_risk_status": visible_risk_status,
            "risk_signal_scope": risk_signal_scope,
        }
    )


def _profile_with(*entries: dict) -> dict:
    return {"entries": list(entries)}


# ---------------------------------------------------------------------------
# 1. Filename
# ---------------------------------------------------------------------------


@pytest.mark.json_backend_only("the JSON backend's profile file name")
def test_write_uses_spec_filename(tmp_path: Path) -> None:
    """save_taste_profile writes to editorial-profile.json."""
    state_dir = tmp_path / "state"
    profile = empty_profile()
    profile["entries"].append(
        {
            "id": "taste-0001",
            "kind": "hard_rule",
            "scope": "author",
            "subject": "Test",
            "verbatim_quote": "q",
            "provenance": "p",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        }
    )
    save_taste_profile(state_dir, profile)
    assert (state_dir / STATE_EDITORIAL_PROFILE).exists()
    assert not (state_dir / STATE_EDITORIAL_TASTE_PROFILE_LEGACY).exists()


@pytest.mark.json_backend_only("the JSON backend's legacy profile file name")
def test_load_falls_back_to_legacy_filename(tmp_path: Path) -> None:
    """When only the legacy file exists, load_taste_profile reads it."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    legacy_data = {
        "entries": [
            {
                "id": "taste-0001",
                "kind": "hard_rule",
                "scope": "author",
                "subject": "Legacy",
                "verbatim_quote": "q",
                "provenance": "p",
                "created_at": "2026-01-01",
                "created_by": "agent",
                "status": "active",
            }
        ]
    }
    (state_dir / STATE_EDITORIAL_TASTE_PROFILE_LEGACY).write_text(json.dumps(legacy_data), encoding="utf-8")
    profile = load_taste_profile(state_dir)
    assert profile["entries"][0]["subject"] == "Legacy"


@pytest.mark.json_backend_only("the JSON backend's legacy profile file name")
def test_load_prefers_new_over_legacy(tmp_path: Path) -> None:
    """When both files exist, load_taste_profile reads the new filename."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    new_data = {
        "entries": [
            {
                "id": "taste-0001",
                "kind": "hard_rule",
                "scope": "author",
                "subject": "New",
                "verbatim_quote": "q",
                "provenance": "p",
                "created_at": "2026-01-01",
                "created_by": "agent",
                "status": "active",
            }
        ]
    }
    legacy_data = {
        "entries": [
            {
                "id": "taste-0002",
                "kind": "hard_rule",
                "scope": "author",
                "subject": "Legacy",
                "verbatim_quote": "q",
                "provenance": "p",
                "created_at": "2026-01-01",
                "created_by": "agent",
                "status": "active",
            }
        ]
    }
    (state_dir / STATE_EDITORIAL_PROFILE).write_text(json.dumps(new_data), encoding="utf-8")
    (state_dir / STATE_EDITORIAL_TASTE_PROFILE_LEGACY).write_text(json.dumps(legacy_data), encoding="utf-8")
    profile = load_taste_profile(state_dir)
    assert len(profile["entries"]) == 1
    assert profile["entries"][0]["subject"] == "New"


# ---------------------------------------------------------------------------
# 2. Malformed JSON → clear TasteProfileError
# ---------------------------------------------------------------------------


@pytest.mark.json_backend_only("a malformed state file on disk")
def test_malformed_json_raises_taste_profile_error(tmp_path: Path) -> None:
    """Invalid JSON in the profile file raises TasteProfileError, not JSONDecodeError."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / STATE_EDITORIAL_PROFILE).write_text("not json {{{", encoding="utf-8")
    with pytest.raises(TasteProfileError, match="invalid JSON"):
        load_taste_profile(state_dir)


@pytest.mark.json_backend_only("a malformed state file on disk")
def test_malformed_structure_raises_taste_profile_error(tmp_path: Path) -> None:
    """Entries with missing fields raise TasteProfileError."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    # Missing required fields
    (state_dir / STATE_EDITORIAL_PROFILE).write_text(json.dumps({"entries": [{"id": "taste-0001"}]}), encoding="utf-8")
    with pytest.raises(TasteProfileError, match="must be a non-empty string"):
        load_taste_profile(state_dir)


@pytest.mark.json_backend_only("a malformed state file on disk")
def test_invalid_json_cli_returns_nonzero(tmp_path: Path) -> None:
    """CLI list command returns non-zero for invalid JSON, no traceback."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / STATE_EDITORIAL_PROFILE).write_text("garbage", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "autoscout", "editorial-profile", "list", "--state-dir", str(state_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "invalid JSON" in result.stdout or "invalid JSON" in result.stderr
    assert "Traceback" not in result.stderr


# ---------------------------------------------------------------------------
# 3. Hard-rule scopes: author, topic, publisher
# ---------------------------------------------------------------------------


def test_add_hard_rule_rejects_invalid_scope() -> None:
    """add_profile_entry returns code 2 for unknown scope."""
    result = add_profile_entry(
        Path("/nonexistent"),
        kind="hard_rule",
        scope="unknown_scope",
        subject="x",
        quote="q",
        provenance="p",
        created_by="agent",
    )
    assert result == 2


def test_add_hard_rule_accepts_topic_scope(tmp_path: Path) -> None:
    """add_profile_entry with scope=topic succeeds."""
    state_dir = tmp_path / "state"
    result = add_profile_entry(
        state_dir,
        kind="hard_rule",
        scope="topic",
        subject="fiction",
        quote="No fiction",
        provenance="editor chat",
        created_by="agent",
        matching_terms=["fiction"],
        confirmed_by_editor=True,
    )
    assert result == 0
    profile = load_taste_profile(state_dir)
    assert profile["entries"][0]["scope"] == "topic"


def test_add_hard_rule_accepts_publisher_scope(tmp_path: Path) -> None:
    """add_profile_entry with scope=publisher succeeds."""
    state_dir = tmp_path / "state"
    result = add_profile_entry(
        state_dir,
        kind="hard_rule",
        scope="publisher",
        subject="Bad Press",
        quote="No Bad Press books",
        provenance="editor chat",
        created_by="agent",
        confirmed_by_editor=True,
    )
    assert result == 0
    profile = load_taste_profile(state_dir)
    assert profile["entries"][0]["scope"] == "publisher"


def test_validate_taste_profile_rejects_unknown_scope() -> None:
    """validate_taste_profile raises on unknown scope."""
    with pytest.raises(TasteProfileError, match="must be one of"):
        validate_taste_profile(
            {
                "entries": [
                    {
                        "id": "taste-0001",
                        "kind": "hard_rule",
                        "scope": "bogus",
                        "subject": "x",
                        "verbatim_quote": "q",
                        "provenance": "p",
                        "created_at": "2026-01-01",
                        "created_by": "agent",
                        "status": "active",
                    }
                ]
            }
        )


# ---------------------------------------------------------------------------
# 4. Soft-preference persistence
# ---------------------------------------------------------------------------


def test_soft_preference_defaults_direction_to_positive(tmp_path: Path) -> None:
    """A soft preference added without --direction defaults to 'positive'."""
    state_dir = tmp_path / "state"
    result = add_profile_entry(
        state_dir,
        kind="soft_preference",
        scope="author",
        subject="Ada Example",
        quote="Ada writes well.",
        provenance="editor chat",
        created_by="agent",
        confirmed_by_editor=True,
    )
    assert result == 0
    profile = load_taste_profile(state_dir)
    entry = profile["entries"][0]
    assert entry["kind"] == "soft_preference"
    assert entry["direction"] == "positive"


def test_soft_preference_explicit_direction(tmp_path: Path) -> None:
    """A soft preference with explicit --direction negative is stored."""
    state_dir = tmp_path / "state"
    result = add_profile_entry(
        state_dir,
        kind="soft_preference",
        scope="author",
        subject="Bad Author",
        quote="Not a fan.",
        provenance="editor chat",
        created_by="agent",
        direction="negative",
        confirmed_by_editor=True,
    )
    assert result == 0
    profile = load_taste_profile(state_dir)
    assert profile["entries"][0]["direction"] == "negative"


def test_soft_preference_symbol_direction_is_normalized(tmp_path: Path) -> None:
    """A soft preference accepts +/- direction shorthand and stores canonical text."""
    state_dir = tmp_path / "state"
    positive = add_profile_entry(
        state_dir,
        kind="soft_preference",
        scope="author",
        subject="Good Author",
        quote="More of this.",
        provenance="editor chat",
        created_by="agent",
        direction="+",
        confirmed_by_editor=True,
    )
    negative = add_profile_entry(
        state_dir,
        kind="soft_preference",
        scope="author",
        subject="Bad Author",
        quote="Less of this.",
        provenance="editor chat",
        created_by="agent",
        direction="-",
        confirmed_by_editor=True,
    )
    assert positive == 0
    assert negative == 0
    profile = load_taste_profile(state_dir)
    assert [entry["direction"] for entry in profile["entries"]] == ["positive", "negative"]


def test_soft_preference_with_description(tmp_path: Path) -> None:
    """A soft preference stores an optional plain-language description."""
    state_dir = tmp_path / "state"
    result = add_profile_entry(
        state_dir,
        kind="soft_preference",
        scope="author",
        subject="Good Author",
        quote="Editor likes them.",
        provenance="editor chat",
        created_by="agent",
        description="Writes compelling non-fiction about technology.",
        confirmed_by_editor=True,
    )
    assert result == 0
    profile = load_taste_profile(state_dir)
    assert profile["entries"][0]["description"] == "Writes compelling non-fiction about technology."


def test_soft_preference_with_example_books(tmp_path: Path) -> None:
    """A soft preference stores optional example books as a list."""
    state_dir = tmp_path / "state"
    result = add_profile_entry(
        state_dir,
        kind="soft_preference",
        scope="author",
        subject="Author X",
        quote="Great writer.",
        provenance="editor chat",
        created_by="agent",
        example_books="Book One, Book Two, Book Three",
        confirmed_by_editor=True,
    )
    assert result == 0
    profile = load_taste_profile(state_dir)
    assert profile["entries"][0]["example_books"] == ["Book One", "Book Two", "Book Three"]


def test_soft_preference_has_stable_id(tmp_path: Path) -> None:
    """Entries get stable taste-NNNN IDs."""
    state_dir = tmp_path / "state"
    add_profile_entry(
        state_dir,
        kind="soft_preference",
        scope="author",
        subject="A",
        quote="q",
        provenance="p",
        created_by="agent",
        confirmed_by_editor=True,
    )
    add_profile_entry(
        state_dir,
        kind="soft_preference",
        scope="author",
        subject="B",
        quote="q",
        provenance="p",
        created_by="agent",
        confirmed_by_editor=True,
    )
    profile = load_taste_profile(state_dir)
    ids = [e["id"] for e in profile["entries"]]
    assert ids == ["taste-0001", "taste-0002"]


def test_soft_preference_revocation_preserves_data(tmp_path: Path) -> None:
    """Revoked soft preferences keep direction/description/example_books."""
    state_dir = tmp_path / "state"
    add_profile_entry(
        state_dir,
        kind="soft_preference",
        scope="author",
        subject="X",
        quote="q",
        provenance="p",
        created_by="agent",
        direction="negative",
        description="Desc",
        example_books="Book A",
        confirmed_by_editor=True,
    )
    result = revoke_profile_entry(state_dir, "taste-0001", "Changed mind", "editor", confirmed_by_editor=True)
    assert result == 0
    profile = load_taste_profile(state_dir)
    entry = profile["entries"][0]
    assert entry["status"] == "revoked"
    assert entry["direction"] == "negative"
    assert entry["description"] == "Desc"
    assert entry["example_books"] == ["Book A"]


def test_soft_preference_list_shows_direction(tmp_path: Path) -> None:
    """List output includes direction for soft preferences."""
    state_dir = tmp_path / "state"
    add_profile_entry(
        state_dir,
        kind="soft_preference",
        scope="author",
        subject="Ada",
        quote="Good writer.",
        provenance="chat",
        created_by="agent",
        direction="positive",
        confirmed_by_editor=True,
    )
    import io
    import sys as _sys

    old_stdout = _sys.stdout
    _sys.stdout = buf = io.StringIO()
    try:
        list_profile_entries(state_dir)
    finally:
        _sys.stdout = old_stdout
    output = buf.getvalue()
    assert "(positive)" in output


# ---------------------------------------------------------------------------
# 5. hide_candidates_by_rules — all scopes
# ---------------------------------------------------------------------------


def test_hide_candidates_author_rule() -> None:
    """Candidate with matching author is hidden by author hard rule."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "hard_rule",
            "scope": "author",
            "subject": "Hidden Author",
            "verbatim_quote": "No more.",
            "provenance": "chat",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        }
    )
    candidates = [
        _make_candidate("Visible Book", authors=["Safe Author"]),
        _make_candidate("Hidden Book", authors=["Hidden Author"]),
    ]
    visible, hidden = hide_candidates_by_rules(candidates, profile)
    assert len(visible) == 1
    assert visible[0].data["title"] == "Visible Book"
    assert len(hidden) == 1
    assert hidden[0]["title"] == "Hidden Book"
    assert "author Hard Rule" in hidden[0]["reason"]


def test_hide_candidates_topic_rule() -> None:
    """Candidate with matching topic is hidden by topic hard rule."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "hard_rule",
            "scope": "topic",
            "subject": "fiction",
            "matching_terms": ["fiction"],
            "verbatim_quote": "Skip fiction.",
            "provenance": "chat",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        }
    )
    candidates = [
        _make_candidate("Fiction Book", category="fiction"),
        _make_candidate("Memoir Book", category="memoir"),
    ]
    visible, hidden = hide_candidates_by_rules(candidates, profile)
    assert len(visible) == 1
    assert visible[0].data["title"] == "Memoir Book"
    assert len(hidden) == 1
    assert hidden[0]["title"] == "Fiction Book"
    assert "topic Hard Rule" in hidden[0]["reason"]


def test_hide_candidates_publisher_rule() -> None:
    """Candidate with matching publisher is hidden by publisher hard rule."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "hard_rule",
            "scope": "publisher",
            "subject": "Bad Press",
            "verbatim_quote": "Block Bad Press.",
            "provenance": "chat",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        }
    )
    candidates = [
        _make_candidate("Blocked Book", publisher="Bad Press Inc."),
        _make_candidate("OK Book", publisher="Good Press"),
    ]
    visible, hidden = hide_candidates_by_rules(candidates, profile)
    assert len(visible) == 1
    assert visible[0].data["title"] == "OK Book"
    assert len(hidden) == 1
    assert hidden[0]["title"] == "Blocked Book"
    assert "publisher Hard Rule" in hidden[0]["reason"]


def test_hide_candidates_revoked_rule_does_not_hide() -> None:
    """A revoked hard rule does not hide candidates."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "hard_rule",
            "scope": "author",
            "subject": "Revoked Author",
            "verbatim_quote": "q",
            "provenance": "p",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "revoked",
            "revoked_at": "2026-02-01",
            "revoked_by": "editor",
            "revocation_reason": "Changed mind.",
        }
    )
    candidates = [_make_candidate("Visible", authors=["Revoked Author"])]
    visible, hidden = hide_candidates_by_rules(candidates, profile)
    assert len(visible) == 1
    assert len(hidden) == 0


def test_active_hard_rules_excludes_revoked() -> None:
    """active_hard_rules returns only active rules."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "hard_rule",
            "scope": "author",
            "subject": "A",
            "verbatim_quote": "q",
            "provenance": "p",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        },
        {
            "id": "taste-0002",
            "kind": "hard_rule",
            "scope": "author",
            "subject": "B",
            "verbatim_quote": "q",
            "provenance": "p",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "revoked",
            "revoked_at": "2026-02-01",
            "revoked_by": "editor",
            "revocation_reason": "reason",
        },
    )
    rules = active_hard_rules(profile)
    assert len(rules) == 1
    assert rules[0]["subject"] == "A"


# ---------------------------------------------------------------------------
# 6. apply_soft_preferences
# ---------------------------------------------------------------------------


def test_apply_soft_preferences_positive_nudge_up() -> None:
    """Positive soft preference nudges priority up one step (C→B)."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "soft_preference",
            "scope": "author",
            "subject": "Good Author",
            "direction": "positive",
            "verbatim_quote": "Great writer.",
            "provenance": "chat",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        }
    )
    candidate = _make_candidate("Book", authors=["Good Author"], priority_label="C")
    apply_soft_preferences([candidate], profile)
    assert candidate.data["priority_label"] == "B"
    assert "C to B" in candidate.data["editorial_profile_notes"]


def test_apply_soft_preferences_negative_nudge_down() -> None:
    """Negative soft preference nudges priority down one step (B→C)."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "soft_preference",
            "scope": "author",
            "subject": "Bad Author",
            "direction": "negative",
            "verbatim_quote": "Not a fan.",
            "provenance": "chat",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        }
    )
    candidate = _make_candidate("Book", authors=["Bad Author"], priority_label="B")
    apply_soft_preferences([candidate], profile)
    assert candidate.data["priority_label"] == "C"
    assert "B to C" in candidate.data["editorial_profile_notes"]


def test_apply_soft_preferences_positive_at_a_stays() -> None:
    """A already at A stays at A with positive nudge."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "soft_preference",
            "scope": "author",
            "subject": "Good Author",
            "direction": "positive",
            "verbatim_quote": "q",
            "provenance": "p",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        }
    )
    candidate = _make_candidate("Book", authors=["Good Author"], priority_label="A")
    apply_soft_preferences([candidate], profile)
    assert candidate.data["priority_label"] == "A"
    assert "did not change" in candidate.data["editorial_profile_notes"]


def test_apply_soft_preferences_negative_at_d_stays() -> None:
    """D stays D with negative nudge."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "soft_preference",
            "scope": "author",
            "subject": "Bad Author",
            "direction": "negative",
            "verbatim_quote": "q",
            "provenance": "p",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        }
    )
    candidate = _make_candidate("Book", authors=["Bad Author"], priority_label="D")
    apply_soft_preferences([candidate], profile)
    assert candidate.data["priority_label"] == "D"


def test_apply_soft_preferences_hard_floor_visible_risk() -> None:
    """Clear book-content visible risk stays at D even with positive preference."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "soft_preference",
            "scope": "author",
            "subject": "Good Author",
            "direction": "positive",
            "verbatim_quote": "q",
            "provenance": "p",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        }
    )
    candidate = _make_candidate(
        "Book",
        authors=["Good Author"],
        priority_label="D",
        visible_risk_status="clear book-content visible risk",
    )
    apply_soft_preferences([candidate], profile)
    assert candidate.data["priority_label"] == "D"
    assert "did not change" in candidate.data.get("editorial_profile_notes", "")


def test_apply_soft_preferences_topic_scope() -> None:
    """Soft preference with topic scope matches an explicit term."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "soft_preference",
            "scope": "topic",
            "subject": "science",
            "matching_terms": ["science"],
            "direction": "positive",
            "verbatim_quote": "Science books are priorities.",
            "provenance": "chat",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        }
    )
    candidate = _make_candidate("Science Book", category="popular science", priority_label="C")
    apply_soft_preferences([candidate], profile)
    assert candidate.data["priority_label"] == "B"


def test_apply_soft_preferences_publisher_scope() -> None:
    """Soft preference with publisher scope matches when publisher contains subject."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "soft_preference",
            "scope": "publisher",
            "subject": "MIT Press",
            "direction": "positive",
            "verbatim_quote": "MIT Press is high quality.",
            "provenance": "chat",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        }
    )
    candidate = _make_candidate("Book", publisher="MIT Press", priority_label="D")
    apply_soft_preferences([candidate], profile)
    assert candidate.data["priority_label"] == "C"


def test_apply_soft_preferences_multiple_preferences() -> None:
    """Mixed matching preferences all contribute to notes but do not stack or conflict-nudge."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "soft_preference",
            "scope": "author",
            "subject": "Multi Author",
            "direction": "positive",
            "verbatim_quote": "Good.",
            "provenance": "chat",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        },
        {
            "id": "taste-0002",
            "kind": "soft_preference",
            "scope": "topic",
            "subject": "history",
            "matching_terms": ["history"],
            "direction": "negative",
            "verbatim_quote": "History is low pri.",
            "provenance": "chat",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        },
    )
    candidate = _make_candidate("History Book", authors=["Multi Author"], category="history", priority_label="B")
    apply_soft_preferences([candidate], profile)
    # Conflicting positive/negative matches are reported but leave priority unchanged.
    assert candidate.data["priority_label"] == "B"
    notes = candidate.data["editorial_profile_notes"]
    assert "taste-0001" in notes
    assert "taste-0002" in notes


def test_apply_soft_preferences_same_direction_preferences_nudge_only_one_step() -> None:
    """Multiple same-direction matches still nudge only one priority step total."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "soft_preference",
            "scope": "author",
            "subject": "Multi Author",
            "direction": "positive",
            "verbatim_quote": "Good author.",
            "provenance": "chat",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        },
        {
            "id": "taste-0002",
            "kind": "soft_preference",
            "scope": "topic",
            "subject": "science",
            "matching_terms": ["science"],
            "direction": "positive",
            "verbatim_quote": "Good topic.",
            "provenance": "chat",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        },
    )
    candidate = _make_candidate("Science Book", authors=["Multi Author"], category="science", priority_label="C")
    apply_soft_preferences([candidate], profile)
    assert candidate.data["priority_label"] == "B"
    notes = candidate.data["editorial_profile_notes"]
    assert "taste-0001" in notes
    assert "taste-0002" in notes


def test_apply_soft_preferences_no_matching_preference() -> None:
    """Candidate with no matching preferences is unchanged."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "soft_preference",
            "scope": "author",
            "subject": "Other Author",
            "direction": "positive",
            "verbatim_quote": "q",
            "provenance": "p",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        }
    )
    candidate = _make_candidate("Book", authors=["Unrelated Author"], priority_label="C")
    apply_soft_preferences([candidate], profile)
    assert candidate.data["priority_label"] == "C"
    assert "editorial_profile_notes" not in candidate.data


def test_apply_soft_preferences_revoked_preferences_ignored() -> None:
    """Revoked soft preferences do not affect candidates."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "soft_preference",
            "scope": "author",
            "subject": "Good Author",
            "direction": "positive",
            "verbatim_quote": "q",
            "provenance": "p",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "revoked",
            "revoked_at": "2026-02-01",
            "revoked_by": "editor",
            "revocation_reason": "Changed mind.",
        }
    )
    candidate = _make_candidate("Book", authors=["Good Author"], priority_label="C")
    apply_soft_preferences([candidate], profile)
    assert candidate.data["priority_label"] == "C"


def test_active_soft_preferences_excludes_revoked() -> None:
    """active_soft_preferences returns only active soft preferences."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "soft_preference",
            "scope": "author",
            "subject": "A",
            "direction": "positive",
            "verbatim_quote": "q",
            "provenance": "p",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        },
        {
            "id": "taste-0002",
            "kind": "soft_preference",
            "scope": "author",
            "subject": "B",
            "direction": "positive",
            "verbatim_quote": "q",
            "provenance": "p",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "revoked",
            "revoked_at": "2026-02-01",
            "revoked_by": "editor",
            "revocation_reason": "reason",
        },
    )
    prefs = active_soft_preferences(profile)
    assert len(prefs) == 1
    assert prefs[0]["subject"] == "A"


# ---------------------------------------------------------------------------
# 7. CLI integration tests
# ---------------------------------------------------------------------------


def test_cli_add_soft_preference_with_new_flags(tmp_path: Path) -> None:
    """CLI add with --direction, --description, --example-books for soft preference."""
    state_dir = tmp_path / "state"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "autoscout",
            "editorial-profile",
            "add",
            "--kind",
            "soft-preference",
            "--scope",
            "author",
            "--subject",
            "Ada Lovelace",
            "--quote",
            "One of our best authors.",
            "--provenance",
            "editorial review 2026-Q3",
            "--created-by",
            "editor",
            "--direction",
            "positive",
            "--description",
            "Writes accessible technology history.",
            "--example-books",
            "Book Alpha, Book Beta",
            "--state-dir",
            str(state_dir),
            "--confirmed-by-editor",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "profile_entry_created=true" in result.stdout

    profile = load_taste_profile(state_dir)
    entry = profile["entries"][0]
    assert entry["kind"] == "soft_preference"
    assert entry["direction"] == "positive"
    assert entry["description"] == "Writes accessible technology history."
    assert entry["example_books"] == ["Book Alpha", "Book Beta"]
    assert entry["status"] == "active"


def test_cli_add_hard_rule_with_topic_scope(tmp_path: Path) -> None:
    """CLI add with --scope topic for hard rules."""
    state_dir = tmp_path / "state"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "autoscout",
            "editorial-profile",
            "add",
            "--kind",
            "hard-rule",
            "--scope",
            "topic",
            "--subject",
            "fiction",
            "--match-term",
            "fiction",
            "--quote",
            "Discontinue all fiction coverage.",
            "--provenance",
            "strategic decision Q2",
            "--state-dir",
            str(state_dir),
            "--confirmed-by-editor",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    profile = load_taste_profile(state_dir)
    assert profile["entries"][0]["scope"] == "topic"


def test_cli_add_hard_rule_with_publisher_scope(tmp_path: Path) -> None:
    """CLI add with --scope publisher for hard rules."""
    state_dir = tmp_path / "state"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "autoscout",
            "editorial-profile",
            "add",
            "--kind",
            "hard-rule",
            "--scope",
            "publisher",
            "--subject",
            "Controversial Press",
            "--quote",
            "Block all Controversial Press output.",
            "--provenance",
            "legal review",
            "--state-dir",
            str(state_dir),
            "--confirmed-by-editor",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    profile = load_taste_profile(state_dir)
    assert profile["entries"][0]["scope"] == "publisher"


def test_cli_rejects_invalid_scope(tmp_path: Path) -> None:
    """CLI returns non-zero for invalid scope."""
    state_dir = tmp_path / "state"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "autoscout",
            "editorial-profile",
            "add",
            "--kind",
            "hard-rule",
            "--scope",
            "nonexistent",
            "--subject",
            "x",
            "--quote",
            "q",
            "--provenance",
            "p",
            "--state-dir",
            str(state_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "invalid_scope" in result.stdout


def test_cli_rejects_invalid_direction(tmp_path: Path) -> None:
    """CLI returns non-zero for invalid direction on soft preference."""
    state_dir = tmp_path / "state"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "autoscout",
            "editorial-profile",
            "add",
            "--kind",
            "soft-preference",
            "--scope",
            "author",
            "--subject",
            "x",
            "--quote",
            "q",
            "--provenance",
            "p",
            "--direction",
            "sideways",
            "--state-dir",
            str(state_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "invalid_direction" in result.stdout


# ---------------------------------------------------------------------------
# 8. Topic Hard Rule matching-terms (issue #281)
# ---------------------------------------------------------------------------


def _make_topic_candidate(
    title: str = "",
    authors: list[str] | None = None,
    category: str = "",
    description: str = "",
    english_title: str = "",
    english_annotation: str = "",
    short_description: str = "",
    evidence_note: str = "",
) -> ScoutingCandidate:
    data: dict[str, Any] = {
        "title": title,
        "primary_authors": authors or ["Test Author"],
        "candidate_category": category,
        "content_description": description,
        "english_title": english_title,
        "english_annotation": english_annotation,
        "short_description": short_description,
    }
    if evidence_note:
        data["source_evidence"] = [{"evidence_note": evidence_note}]
    return ScoutingCandidate(data)


# — preview quote preservation + criteria + matched candidates + zero mutation —


def test_topic_hard_rule_preview_preserves_quote(tmp_path: Path) -> None:
    """Preview preserves verbatim_quote byte-for-byte and prints matching terms as JSON."""
    import io
    import sys as _sys

    state_dir = tmp_path / "state"
    quote = "Категорически не хотим «queer studies» — скрываем."

    old_stdout = _sys.stdout
    _sys.stdout = buf = io.StringIO()
    try:
        result = add_profile_entry(
            state_dir,
            kind="hard_rule",
            scope="topic",
            subject="queer studies",
            quote=quote,
            provenance="editor chat",
            created_by="agent",
            matching_terms=["queerness", "queer", "квир"],
        )
    finally:
        _sys.stdout = old_stdout
    output = buf.getvalue()
    assert result == 3
    assert "confirmation_required=true" in output
    assert f"verbatim_quote={quote}" in output
    # JSON-encoded matching_terms preserving first-supplied order
    assert 'matching_terms=["queerness","queer","квир"]' in output
    # Zero mutation — no profile file created
    assert not (state_dir / STATE_EDITORIAL_PROFILE).exists()


def test_topic_hard_rule_preview_with_candidates(tmp_path: Path) -> None:
    """Preview evaluates matching_terms against candidates and prints JSON matches."""
    import io
    import sys as _sys

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)

    # Write candidate state for preview — one with explicit identity_key,
    # one without (fallback derivation).
    candidates = [
        {
            "title": "Queer Theory Today",
            "primary_authors": ["Scholar One"],
            "english_title": "Queer Theory Today",
            "content_description": "A comprehensive overview.",
            "identity_key": "queer-theory-today|scholar one",
        },
        {
            "title": "Квир-оптика в современной культуре",
            "primary_authors": ["Scholar Two"],
            "content_description": "Исследование квир-оптики и культурных нарративов.",
            "identity_key": "kvir-optika|scholar two",
        },
        {
            "title": "Normal History Book",
            "primary_authors": ["Historian"],
            "content_description": "Standard historical analysis.",
            "identity_key": "normal-history-book|historian",
        },
    ]
    seed_state(state_dir, latest_scouting_candidates=candidates)

    old_stdout = _sys.stdout
    _sys.stdout = buf = io.StringIO()
    try:
        add_profile_entry(
            state_dir,
            kind="hard_rule",
            scope="topic",
            subject="queer studies",
            quote="No queer books.",
            provenance="editor chat",
            created_by="agent",
            matching_terms=["queerness", "queer", "квир"],
        )
    finally:
        _sys.stdout = old_stdout
    output = buf.getvalue()

    assert "current_matches=2" in output
    assert "Queer Theory Today" in output
    assert "queer-theory-today|scholar one" in output
    assert "Квир-оптика" in output
    assert "kvir-optika|scholar two" in output
    # Term "queer" / "квир" matched
    assert "queer" in output
    assert "квир" in output
    # Normal book absent
    assert "Normal History Book" not in output
    # Zero mutation
    assert not (state_dir / STATE_EDITORIAL_PROFILE).exists()


def test_topic_hard_rule_preview_no_candidates_clean(tmp_path: Path) -> None:
    """When candidate state is absent, the preview still prints an unambiguous
    current_matches=0 count with no match rows and asks for confirmation
    cleanly (#556)."""
    import io
    import sys as _sys

    state_dir = tmp_path / "state"

    old_stdout = _sys.stdout
    _sys.stdout = buf = io.StringIO()
    try:
        add_profile_entry(
            state_dir,
            kind="hard_rule",
            scope="topic",
            subject="queer studies",
            quote="No queer books.",
            provenance="editor chat",
            created_by="agent",
            matching_terms=["queer", "квир"],
        )
    finally:
        _sys.stdout = old_stdout
    output = buf.getvalue()
    assert "current_matches=0" in output
    assert "current_match=" not in output
    assert "confirmation_required=true" in output


# — confirmed persistence —


def test_topic_hard_rule_confirmed_persists_matching_terms(tmp_path: Path) -> None:
    """Confirmed add persists matching_terms on the profile entry."""
    state_dir = tmp_path / "state"
    result = add_profile_entry(
        state_dir,
        kind="hard_rule",
        scope="topic",
        subject="queer studies",
        quote="No queer books.",
        provenance="editor chat",
        created_by="agent",
        matching_terms=["queer", "квир"],
        confirmed_by_editor=True,
    )
    assert result == 0
    profile = load_taste_profile(state_dir)
    entry = profile["entries"][0]
    assert entry["kind"] == "hard_rule"
    assert entry["scope"] == "topic"
    assert entry["subject"] == "queer studies"
    assert entry["matching_terms"] == ["queer", "квир"]
    assert entry["verbatim_quote"] == "No queer books."


# — OR matching: English inflection variant + Russian description variant —


def test_topic_hard_rule_or_matching_english_inflection(tmp_path: Path) -> None:
    """Topic hard rule with matching_terms uses OR across them; English title matches 'queerness'."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "hard_rule",
            "scope": "topic",
            "subject": "queer studies",
            "matching_terms": ["queerness", "queer", "квир"],
            "verbatim_quote": "No queer topics.",
            "provenance": "chat",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        }
    )
    candidates = [
        _make_topic_candidate("The History of Queerness", english_title="The History of Queerness"),
        _make_topic_candidate("Normal Book", english_title="Normal Book"),
    ]
    visible, hidden = hide_candidates_by_rules(candidates, profile)
    assert len(visible) == 1
    assert visible[0].data["title"] == "Normal Book"
    assert len(hidden) == 1
    assert hidden[0]["title"] == "The History of Queerness"
    assert "queerness" in hidden[0]["match_explanation"]


def test_topic_hard_rule_or_matching_russian_description(tmp_path: Path) -> None:
    """Topic hard rule matches 'квир' in Russian content_description including 'квир-оптику'."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "hard_rule",
            "scope": "topic",
            "subject": "queer studies",
            "matching_terms": ["queerness", "queer", "квир"],
            "verbatim_quote": "No queer topics.",
            "provenance": "chat",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        }
    )
    # Use a title without 'квир' so that the match is forced to the content_description
    candidates = [
        _make_topic_candidate("Современная оптика", description="Исследование квир-оптики и культурных нарративов."),
        _make_topic_candidate("Normal Russian Book", description="Обычное исследование."),
    ]
    visible, hidden = hide_candidates_by_rules(candidates, profile)
    assert len(visible) == 1
    assert visible[0].data["title"] == "Normal Russian Book"
    assert len(hidden) == 1
    assert hidden[0]["title"] == "Современная оптика"
    assert "квир" in hidden[0]["match_explanation"]
    # Match explanation identifies the description field
    assert "description" in hidden[0]["match_explanation"] or "content_description" in hidden[0]["match_explanation"]


# — future candidates (verified via hide_candidates_by_rules) —


def test_topic_hard_rule_matches_future_candidates(tmp_path: Path) -> None:
    """A confirmed topic hard rule applies to future candidates — the rule is stored correctly."""
    state_dir = tmp_path / "state"
    add_profile_entry(
        state_dir,
        kind="hard_rule",
        scope="topic",
        subject="queer studies",
        quote="No queer books.",
        provenance="editor chat",
        created_by="agent",
        matching_terms=["queer", "квир"],
        confirmed_by_editor=True,
    )
    profile = load_taste_profile(state_dir)
    # Future candidate with 'queer' in English title
    future = _make_topic_candidate("Queer Futures", english_title="Queer Futures")
    visible, hidden = hide_candidates_by_rules([future], profile)
    assert len(hidden) == 1
    assert hidden[0]["title"] == "Queer Futures"


# — no unrelated broadening —


def test_topic_hard_rule_no_unrelated_broadening(tmp_path: Path) -> None:
    """A topic hard rule does not broaden; unrelated tokens are not matched."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "hard_rule",
            "scope": "topic",
            "subject": "queer studies",
            "matching_terms": ["queerness", "queer", "квир"],
            "verbatim_quote": "No queer topics.",
            "provenance": "chat",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        }
    )
    # 'que' is not 'queer'; 'квитанция' contains 'квит' but not 'квир'
    candidates = [
        _make_topic_candidate("The Queue Problem", english_title="The Queue Problem"),
        _make_topic_candidate("Квитанция", description="Квитанция об оплате."),
    ]
    visible, hidden = hide_candidates_by_rules(candidates, profile)
    assert len(visible) == 2
    assert len(hidden) == 0


# — legacy entry compatibility (no matching_terms field) —


def test_legacy_topic_entry_loads_without_matching_terms() -> None:
    """A stored profile entry without matching_terms loads but cannot match."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "hard_rule",
            "scope": "topic",
            "subject": "fiction",
            "verbatim_quote": "Skip fiction.",
            "provenance": "chat",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        }
    )
    validate_taste_profile(profile)
    candidates = [
        _make_topic_candidate("Fiction Book", category="fiction"),
        _make_topic_candidate("Nonfiction Book", category="nonfiction"),
    ]
    visible, hidden = hide_candidates_by_rules(candidates, profile)
    assert len(visible) == 2
    assert len(hidden) == 0


def test_legacy_topic_entry_does_not_mutate_on_load(tmp_path: Path) -> None:
    """Loading a legacy topic entry does not add matching_terms to on-disk state."""
    state_dir = tmp_path / "state"
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "hard_rule",
            "scope": "topic",
            "subject": "fiction",
            "verbatim_quote": "Skip fiction.",
            "provenance": "chat",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        }
    )
    save_taste_profile(state_dir, profile)

    # Load and verify no matching_terms added
    loaded = load_taste_profile(state_dir)
    assert "matching_terms" not in loaded["entries"][0]

    # Re-save and re-load — still no matching_terms
    save_taste_profile(state_dir, loaded)
    reloaded = load_taste_profile(state_dir)
    assert "matching_terms" not in reloaded["entries"][0]


# — topic hard rule without explicit matching_terms defaults to subject —


def test_topic_hard_rule_without_match_terms_is_rejected(tmp_path: Path) -> None:
    """A new topic hard rule requires explicit matching terms."""
    state_dir = tmp_path / "state"
    result = add_profile_entry(
        state_dir,
        kind="hard_rule",
        scope="topic",
        subject="queer studies",
        quote="No queer books.",
        provenance="editor chat",
        created_by="agent",
        confirmed_by_editor=True,
    )
    assert result == 2
    assert not (state_dir / STATE_EDITORIAL_PROFILE).exists()


# — preview for topic hard rule without explicit matching_terms displays subject as criterion —


def test_topic_hard_rule_without_match_terms_reports_required_error(tmp_path: Path) -> None:
    """A topic hard rule without --match-term is rejected before preview."""
    import io
    import sys as _sys

    state_dir = tmp_path / "state"

    old_stdout = _sys.stdout
    _sys.stdout = buf = io.StringIO()
    try:
        add_profile_entry(
            state_dir,
            kind="hard_rule",
            scope="topic",
            subject="queer studies",
            quote="No queer books.",
            provenance="editor chat",
            created_by="agent",
        )
    finally:
        _sys.stdout = old_stdout
    output = buf.getvalue()
    assert "error=matching_terms_required_for_topic" in output
    assert "confirmation_required=true" not in output


# — listing exposes stored matching terms —


def test_list_exposes_topic_matching_terms(tmp_path: Path) -> None:
    """List output includes JSON-encoded matching terms for topic hard rules."""
    import io
    import sys as _sys

    state_dir = tmp_path / "state"
    add_profile_entry(
        state_dir,
        kind="hard_rule",
        scope="topic",
        subject="queer studies",
        quote="No queer books.",
        provenance="editor chat",
        created_by="agent",
        matching_terms=["queer", "квир"],
        confirmed_by_editor=True,
    )

    old_stdout = _sys.stdout
    _sys.stdout = buf = io.StringIO()
    try:
        list_profile_entries(state_dir)
    finally:
        _sys.stdout = old_stdout
    output = buf.getvalue()
    assert 'Matching terms: ["queer","квир"]' in output


# — validation of matching_terms type —


def test_validate_rejects_non_list_matching_terms() -> None:
    """validate_taste_profile rejects matching_terms that is not a list."""
    with pytest.raises(TasteProfileError, match="must be a non-empty list"):
        validate_taste_profile(
            {
                "entries": [
                    {
                        "id": "taste-0001",
                        "kind": "hard_rule",
                        "scope": "topic",
                        "subject": "x",
                        "verbatim_quote": "q",
                        "provenance": "p",
                        "created_at": "2026-01-01",
                        "created_by": "agent",
                        "status": "active",
                        "matching_terms": "not-a-list",
                    }
                ]
            }
        )


def test_validate_rejects_non_string_matching_terms_items() -> None:
    """validate_taste_profile rejects matching_terms with non-string items."""
    with pytest.raises(TasteProfileError, match="must be a non-empty list"):
        validate_taste_profile(
            {
                "entries": [
                    {
                        "id": "taste-0001",
                        "kind": "hard_rule",
                        "scope": "topic",
                        "subject": "x",
                        "verbatim_quote": "q",
                        "provenance": "p",
                        "created_at": "2026-01-01",
                        "created_by": "agent",
                        "status": "active",
                        "matching_terms": [1, 2, 3],
                    }
                ]
            }
        )


def test_validate_rejects_empty_matching_terms() -> None:
    """validate_taste_profile rejects an empty matching_terms list."""
    with pytest.raises(TasteProfileError, match="must be a non-empty list"):
        validate_taste_profile(
            {
                "entries": [
                    {
                        "id": "taste-0001",
                        "kind": "hard_rule",
                        "scope": "topic",
                        "subject": "x",
                        "verbatim_quote": "q",
                        "provenance": "p",
                        "created_at": "2026-01-01",
                        "created_by": "agent",
                        "status": "active",
                        "matching_terms": [],
                    }
                ]
            }
        )


def test_validate_rejects_blank_matching_terms_items() -> None:
    """validate_taste_profile rejects matching_terms with blank strings."""
    with pytest.raises(TasteProfileError, match="must be a non-empty list"):
        validate_taste_profile(
            {
                "entries": [
                    {
                        "id": "taste-0001",
                        "kind": "hard_rule",
                        "scope": "topic",
                        "subject": "x",
                        "verbatim_quote": "q",
                        "provenance": "p",
                        "created_at": "2026-01-01",
                        "created_by": "agent",
                        "status": "active",
                        "matching_terms": ["   ", ""],
                    }
                ]
            }
        )


def test_validate_rejects_matching_terms_on_non_topic() -> None:
    """validate_taste_profile rejects matching_terms outside topic scope."""
    with pytest.raises(TasteProfileError, match="only allowed on topic entries"):
        validate_taste_profile(
            {
                "entries": [
                    {
                        "id": "taste-0001",
                        "kind": "hard_rule",
                        "scope": "author",
                        "subject": "x",
                        "verbatim_quote": "q",
                        "provenance": "p",
                        "created_at": "2026-01-01",
                        "created_by": "agent",
                        "status": "active",
                        "matching_terms": ["queer"],
                    }
                ]
            }
        )


def test_validate_accepts_matching_terms_on_topic_soft_preference() -> None:
    """validate_taste_profile accepts matching_terms on a topic soft preference."""
    validate_taste_profile(
        {
            "entries": [
                {
                    "id": "taste-0001",
                    "kind": "soft_preference",
                    "scope": "topic",
                    "subject": "x",
                    "direction": "positive",
                    "verbatim_quote": "q",
                    "provenance": "p",
                    "created_at": "2026-01-01",
                    "created_by": "agent",
                    "status": "active",
                    "matching_terms": ["queer"],
                }
            ]
        }
    )


# — CLI integration: --match-term repeatable —


def test_cli_topic_hard_rule_with_match_term(tmp_path: Path) -> None:
    """CLI add accepts --match-term for topic hard rules; preserves first-supplied order."""
    state_dir = tmp_path / "state"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "autoscout",
            "editorial-profile",
            "add",
            "--kind",
            "hard-rule",
            "--scope",
            "topic",
            "--subject",
            "queer studies",
            "--quote",
            "No queer books.",
            "--provenance",
            "editor chat",
            "--match-term",
            "queerness",
            "--match-term",
            "квир",
            "--match-term",
            "queerness",
            "--state-dir",
            str(state_dir),
            "--confirmed-by-editor",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "profile_entry_created=true" in result.stdout
    profile = load_taste_profile(state_dir)
    entry = profile["entries"][0]
    # First-supplied order, duplicates removed
    assert entry["matching_terms"] == ["queerness", "квир"]


def test_cli_topic_hard_rule_preview_with_match_term(tmp_path: Path) -> None:
    """CLI preview returns code 3 with JSON matching_terms and current matches."""

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)

    # Write candidate state — use "queer" as a standalone token in the title
    candidates = [
        {
            "title": "Queer Studies in Focus",
            "primary_authors": ["Scholar"],
            "english_title": "Queer Studies in Focus",
            "identity_key": "queer-studies-in-focus|scholar",
        }
    ]
    seed_state(state_dir, latest_scouting_candidates=candidates)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "autoscout",
            "editorial-profile",
            "add",
            "--kind",
            "hard-rule",
            "--scope",
            "topic",
            "--subject",
            "queer studies",
            "--quote",
            "No queer books.",
            "--provenance",
            "editor chat",
            "--match-term",
            "queer",
            "--match-term",
            "квир",
            "--state-dir",
            str(state_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert "confirmation_required=true" in result.stdout
    assert 'matching_terms=["queer","квир"]' in result.stdout
    assert "current_matches=1" in result.stdout
    assert "Queer Studies in Focus" in result.stdout


def test_cli_topic_hard_rule_rejects_empty_only_match_terms(tmp_path: Path) -> None:
    """CLI rejects --match-term that produce only empty/whitespace values."""
    state_dir = tmp_path / "state"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "autoscout",
            "editorial-profile",
            "add",
            "--kind",
            "hard-rule",
            "--scope",
            "topic",
            "--subject",
            "queer studies",
            "--quote",
            "No queer books.",
            "--provenance",
            "editor chat",
            "--match-term",
            "   ",
            "--match-term",
            "",
            "--state-dir",
            str(state_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "matching_terms_must_be_non_empty" in result.stdout


# — soft preferences use the shared topic matcher —


def test_soft_preference_topic_matching_uses_matching_terms(tmp_path: Path) -> None:
    """Soft preference topic matching uses explicit matching terms."""
    profile = _profile_with(
        {
            "id": "taste-0001",
            "kind": "soft_preference",
            "scope": "topic",
            "subject": "science",
            "matching_terms": ["science"],
            "direction": "positive",
            "verbatim_quote": "Science is good.",
            "provenance": "chat",
            "created_at": "2026-01-01",
            "created_by": "agent",
            "status": "active",
        }
    )
    candidate = _make_topic_candidate("Popular Science Book", category="popular science")
    candidate.data["priority_label"] = "C"
    apply_soft_preferences([candidate], profile)
    assert candidate.data["priority_label"] == "B"


def test_non_topic_hard_rule_rejects_matching_terms(tmp_path: Path) -> None:
    """--match-term is rejected for non-topic hard rules (finding 5)."""
    state_dir = tmp_path / "state"
    result = add_profile_entry(
        state_dir,
        kind="hard_rule",
        scope="author",
        subject="Hidden Author",
        quote="No more.",
        provenance="editor chat",
        created_by="agent",
        matching_terms=["queer", "квир"],
    )
    assert result == 2
    assert not (state_dir / STATE_EDITORIAL_PROFILE).exists()


def test_soft_preference_preview_accepts_matching_terms(tmp_path: Path) -> None:
    """--match-term is accepted for topic soft preferences."""
    state_dir = tmp_path / "state"
    result = add_profile_entry(
        state_dir,
        kind="soft_preference",
        scope="topic",
        subject="science",
        quote="Science is good.",
        provenance="editor chat",
        created_by="agent",
        matching_terms=["science"],
    )
    assert result == 3
    assert not (state_dir / STATE_EDITORIAL_PROFILE).exists()


# — stored identity_key preservation (finding 6) —


def test_preview_uses_stored_identity_key(tmp_path: Path) -> None:
    """Preview reports the exact stored identity_key, not the title/author-derived one."""
    import io
    import sys as _sys

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)

    # Stored identity_key intentionally differs from the title|author derived key
    candidates = [
        {
            "title": "Some Book Title",
            "primary_authors": ["Author Name"],
            "english_title": "Queer Book",
            "identity_key": "stored-custom-key|different",
        }
    ]
    seed_state(state_dir, latest_scouting_candidates=candidates)

    old_stdout = _sys.stdout
    _sys.stdout = buf = io.StringIO()
    try:
        add_profile_entry(
            state_dir,
            kind="hard_rule",
            scope="topic",
            subject="queer",
            quote="No queer books.",
            provenance="editor chat",
            created_by="agent",
            matching_terms=["queer"],
        )
    finally:
        _sys.stdout = old_stdout
    output = buf.getvalue()

    # Must show the stored identity key, not the derived one
    assert "stored-custom-key|different" in output
    # The title-derived key would be something like "some-book-title|author name"
    assert "some-book-title" not in output.lower()


def test_preview_falls_back_to_derived_identity_key(tmp_path: Path) -> None:
    """When stored identity_key is absent, preview falls back to derived key."""
    import io
    import sys as _sys

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)

    candidates = [
        {
            "title": "Queer Theory",
            "primary_authors": ["Scholar One"],
            "english_title": "Queer Theory",
            # No identity_key — must derive
        }
    ]
    seed_state(state_dir, latest_scouting_candidates=candidates)

    old_stdout = _sys.stdout
    _sys.stdout = buf = io.StringIO()
    try:
        add_profile_entry(
            state_dir,
            kind="hard_rule",
            scope="topic",
            subject="queer",
            quote="No queer.",
            provenance="editor chat",
            created_by="agent",
            matching_terms=["queer"],
        )
    finally:
        _sys.stdout = old_stdout
    output = buf.getvalue()

    # Fallback derived key should appear (normalized title uses spaces)
    assert "queer theory|scholar one" in output


# — deduplication preserves first-supplied order —


def test_matching_terms_deduplication_first_order(tmp_path: Path) -> None:
    """Duplicates are removed preserving first appearance; no sorting."""
    state_dir = tmp_path / "state"
    add_profile_entry(
        state_dir,
        kind="hard_rule",
        scope="topic",
        subject="queer studies",
        quote="No queer.",
        provenance="editor chat",
        created_by="agent",
        matching_terms=["квир", "queer", "квир", "queerness", "queer"],
        confirmed_by_editor=True,
    )
    profile = load_taste_profile(state_dir)
    assert profile["entries"][0]["matching_terms"] == ["квир", "queer", "queerness"]
