from autoscout.pipeline import update_candidate_history_snapshots

# ---------------------------------------------------------------------------
# first_seen_date stability
# ---------------------------------------------------------------------------


def test_first_seen_date_is_stable_across_sightings():
    """first_seen_date must never move forward on a later appearance."""
    identity_key = "stable work|stable author"
    history = [
        {
            "identity_key": identity_key,
            "title": "Stable Work",
            "primary_authors": ["Stable Author"],
            "first_seen_date": "2026-06-01",
            "last_seen_date": "2026-06-01",
        }
    ]
    current_candidate = {
        "identity_key": identity_key,
        "title": "Stable Work",
        "primary_authors": ["Stable Author"],
        "update_delta": "unchanged",
    }

    updated = update_candidate_history_snapshots(history, [current_candidate], "2026-07-13")

    assert updated[0]["first_seen_date"] == "2026-06-01"
    assert updated[0]["last_seen_date"] == "2026-07-13"


def test_first_seen_date_initialised_on_first_sighting():
    """A brand-new candidate gets first_seen_date set to the run date."""
    identity_key = "new work|new author"
    current_candidate = {
        "identity_key": identity_key,
        "title": "New Work",
        "primary_authors": ["New Author"],
    }

    updated = update_candidate_history_snapshots([], [current_candidate], "2026-07-13")

    assert updated[0]["first_seen_date"] == "2026-07-13"
    assert updated[0]["last_seen_date"] == "2026-07-13"


# ---------------------------------------------------------------------------
# last_material_change_date
# ---------------------------------------------------------------------------


def test_material_change_sets_last_material_change_date():
    """An existing upgraded/changed candidate sets last_material_change_date."""
    identity_key = "improved work|improved author"
    history = [
        {
            "identity_key": identity_key,
            "title": "Improved Work",
            "primary_authors": ["Improved Author"],
            "first_seen_date": "2026-06-01",
            "last_seen_date": "2026-06-01",
            "primary_signal_strength": "reader favorite",
        }
    ]
    current_candidate = {
        "identity_key": identity_key,
        "title": "Improved Work",
        "primary_authors": ["Improved Author"],
        "update_delta": "upgraded",
        "primary_signal": {"signal_strength": "winner"},
    }

    updated = update_candidate_history_snapshots(history, [current_candidate], "2026-07-13")

    assert updated[0]["last_material_change_date"] == "2026-07-13"


def test_new_addition_does_not_get_fake_updated_date():
    """New additions must not pretend to have an Updated date."""
    identity_key = "new arrival|new author"
    current_candidate = {
        "identity_key": identity_key,
        "title": "New Arrival",
        "primary_authors": ["New Author"],
        "update_delta": "new",
    }

    updated = update_candidate_history_snapshots([], [current_candidate], "2026-07-13")

    assert "last_material_change_date" not in updated[0]


def test_unchanged_sighting_preserves_prior_material_change_date():
    """An unchanged sighting does not erase a previously set Updated date."""
    identity_key = "prior change|prior author"
    history = [
        {
            "identity_key": identity_key,
            "title": "Prior Change",
            "primary_authors": ["Prior Author"],
            "first_seen_date": "2026-06-01",
            "last_seen_date": "2026-06-08",
            "last_material_change_date": "2026-06-08",
        }
    ]
    current_candidate = {
        "identity_key": identity_key,
        "title": "Prior Change",
        "primary_authors": ["Prior Author"],
        "update_delta": "unchanged",
    }

    updated = update_candidate_history_snapshots(history, [current_candidate], "2026-07-13")

    assert updated[0]["last_material_change_date"] == "2026-06-08"


def test_material_change_advances_last_material_change_date():
    """A second material change advances the date."""
    identity_key = "changed twice|changed author"
    history = [
        {
            "identity_key": identity_key,
            "title": "Changed Twice",
            "primary_authors": ["Changed Author"],
            "first_seen_date": "2026-06-01",
            "last_seen_date": "2026-06-08",
            "last_material_change_date": "2026-06-08",
            "russian_edition_status": "not found in Russian",
        }
    ]
    current_candidate = {
        "identity_key": identity_key,
        "title": "Changed Twice",
        "primary_authors": ["Changed Author"],
        "update_delta": "changed",
        "russian_edition_status": "found in Russian",
    }

    updated = update_candidate_history_snapshots(history, [current_candidate], "2026-07-13")

    assert updated[0]["last_material_change_date"] == "2026-07-13"


def test_mislabeled_new_does_not_erase_existing_updated_date():
    """An existing record labeled 'new' must not erase a prior Updated date."""
    identity_key = "existing mislabeled|existing author"
    history = [
        {
            "identity_key": identity_key,
            "title": "Existing Mislabeled",
            "primary_authors": ["Existing Author"],
            "first_seen_date": "2026-06-01",
            "last_seen_date": "2026-06-08",
            "last_material_change_date": "2026-06-08",
        }
    ]
    current_candidate = {
        "identity_key": identity_key,
        "title": "Existing Mislabeled",
        "primary_authors": ["Existing Author"],
        "update_delta": "new",
    }

    updated = update_candidate_history_snapshots(history, [current_candidate], "2026-07-13")

    assert updated[0]["last_material_change_date"] == "2026-06-08"


def test_brand_new_mislabeled_as_changed_gets_no_updated_date():
    """A brand-new record labeled 'changed' must not get a fake Updated date."""
    identity_key = "new mislabeled|new author"
    current_candidate = {
        "identity_key": identity_key,
        "title": "New Mislabeled",
        "primary_authors": ["New Author"],
        "update_delta": "changed",
    }

    updated = update_candidate_history_snapshots([], [current_candidate], "2026-07-13")

    assert "last_material_change_date" not in updated[0]


def test_blank_weekly_bibliographic_metadata_does_not_erase_history():
    identity_key = "known book|known author"
    history = [
        {
            "identity_key": identity_key,
            "publisher": "Known Press",
            "publication_year": "2025",
            "last_seen_date": "2026-07-06",
        }
    ]
    current_candidate = {
        "identity_key": identity_key,
        "title": "Known Book",
        "primary_authors": ["Known Author"],
        "publisher": "",
        "publication_year": "",
    }

    updated = update_candidate_history_snapshots(history, [current_candidate], "2026-07-13")

    assert updated[0]["publisher"] == "Known Press"
    assert updated[0]["publication_year"] == "2025"


def test_new_weekly_bibliographic_metadata_enriches_history():
    identity_key = "known book|known author"
    history = [{"identity_key": identity_key, "last_seen_date": "2026-07-06"}]
    current_candidate = {
        "identity_key": identity_key,
        "title": "Known Book",
        "primary_authors": ["Known Author"],
        "publisher": "Known Press",
        "publication_year": "2025",
    }

    updated = update_candidate_history_snapshots(history, [current_candidate], "2026-07-13")

    assert updated[0]["publisher"] == "Known Press"
    assert updated[0]["publication_year"] == "2025"
