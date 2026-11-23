"""Offline coverage for issue #637: apply accepted descriptions through the gate.

Every accepted description is written with the existing description apply
gate and ``agent-adjudicated`` provenance. Two surviving owners apply a
two-source description, one surviving owner applies a single-source
description marked with the existing status text, and every other case
leaves the record at fallback with a bounded reason. The command writes a
complete preview before changing anything: without the explicit apply
option no state changes. Every test is offline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from adjudication_test_support import AUTHOR
from adjudication_test_support import OPERATION_DATE
from adjudication_test_support import TITLE
from adjudication_test_support import VALID_TEXT
from adjudication_test_support import citation as _citation
from adjudication_test_support import fetcher as _fetcher_for_pages
from adjudication_test_support import page as _page
from adjudication_test_support import record as _record
from adjudication_test_support import response as _response
from adjudication_test_support import workspace as _workspace
from state_store_support import read_state
from state_store_support import state_snapshot

from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.content_description import DESCRIPTION_GROUNDED_STATUS
from autoscout.content_description import DESCRIPTION_PROVENANCE_VALUES
from autoscout.content_description import DESCRIPTION_SINGLE_SOURCE_STATUS
from autoscout.content_description import DESCRIPTION_STAGE_OUTCOME_VALUES
from autoscout.content_description import validate_content_description
from autoscout.description_adjudication import ADJUDICATION_PROVENANCE
from autoscout.description_adjudication import ReplayDescriptionAdjudicationAdapter
from autoscout.description_adjudication import adjudicate_descriptions
from autoscout.grounded_description import OUTCOME_GROUNDED
from autoscout.grounded_description import OUTCOME_SINGLE_SOURCE
from autoscout.grounded_description import REASON_AMBIGUITY
from autoscout.grounded_description import REASON_FEWER_THAN_TWO
from autoscout.grounded_description import REASON_INVALID_DESCRIPTION
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import canonicalize_url
from autoscout.grounded_description import reason_label
from autoscout.json_io import read_json
from autoscout.review_rendering import _render_neutral_row

COPIED_TEXT = (
    "Книга о памяти и обществе. Обзор Publishers Weekly отметил глубину. "
    "Рецензия Booklist подчеркнула стиль. Отзыв Library Journal выделил героев. "
    "Мнение Editorial Review завершило картину."
)

PUBLISHER_URL = "https://publisher-example.test/work-one"
CATALOGUE_URL = "https://catalog-example.test/work-one"
STOREFRONT_URL = "https://www.amazon.com/work-one/dp/0000000000"
SECOND_STOREFRONT_URL = "https://www.barnesandnoble.com/w/work-one/1"


def _fetcher(*urls: str) -> BoundedHTTPFetcher:
    """Serve one page per URL, which is how this suite names its pages."""
    return _fetcher_for_pages({canonicalize_url(url): _page() for url in urls})


def test_two_sources_apply_with_provenance_and_record_fields(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path, [_record("candidate-001", "work one|alex author")])
    adapter = ReplayDescriptionAdjudicationAdapter(
        {
            "work one|alex author": _response(
                "work one|alex author",
                [
                    _citation(PUBLISHER_URL, "publisher-example.test", "publisher"),
                    _citation(CATALOGUE_URL, "catalog-example.test", "library_catalogue"),
                ],
            )
        }
    )

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=adapter,
        fetcher=_fetcher(PUBLISHER_URL, CATALOGUE_URL),
        apply=True,
    )

    assert result["applied"] == 1
    (record,) = read_state(state_dir, "candidate_history")
    assert record["content_description"] == VALID_TEXT
    # The applied record itself carries the whole set the issue names, so a
    # reader of Candidate History alone can see what the description rests
    # on. The preview repeats it, but state does not depend on the preview.
    provenance = record["description_provenance"]
    assert provenance["provenance"] == "agent-adjudicated"
    assert provenance["applied_date"] == OPERATION_DATE
    assert provenance["evidence_basis"] == [canonicalize_url(PUBLISHER_URL), canonicalize_url(CATALOGUE_URL)]
    assert provenance["candidate_id"] == "candidate-001"
    assert provenance["identity_key"] == "work one|alex author"
    assert provenance["owner_count"] == 2
    assert provenance["confidence"] == "high"
    assert record["grounded_outcome"] == OUTCOME_GROUNDED
    preview = read_json(output_dir / "description-adjudication-preview.json")
    (row,) = preview["preview"]
    assert row["decision"] == "apply_two_source"
    detail = row["applied_detail"]
    assert detail["candidate_id"] == "candidate-001"
    assert detail["identity_key"] == "work one|alex author"
    assert detail["provenance"] == "agent-adjudicated"
    assert detail["applied_date"] == OPERATION_DATE
    assert detail["evidence_urls"] == [canonicalize_url(PUBLISHER_URL), canonicalize_url(CATALOGUE_URL)]
    assert detail["owner_count"] == 2
    assert detail["confidence"] == "high"


def test_single_owner_applies_with_existing_status_text(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path, [_record("candidate-001", "work one|alex author")])
    adapter = ReplayDescriptionAdjudicationAdapter(
        {
            "work one|alex author": _response(
                "work one|alex author",
                # Two storefronts repeat publisher-supplied copy: the owner
                # policy gives them one shared owner, so they count once.
                [
                    _citation(STOREFRONT_URL, "amazon.com", "retail_listing"),
                    _citation(SECOND_STOREFRONT_URL, "barnesandnoble.com", "retail_listing"),
                ],
            )
        }
    )

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=adapter,
        fetcher=_fetcher(STOREFRONT_URL, SECOND_STOREFRONT_URL),
        apply=True,
    )

    assert result["applied"] == 1
    (record,) = read_state(state_dir, "candidate_history")
    assert record["content_description"] == VALID_TEXT
    assert record["description_provenance"]["provenance"] == "agent-adjudicated"
    assert record["description_provenance"]["owner_count"] == 1
    preview = read_json(output_dir / "description-adjudication-preview.json")
    (row,) = preview["preview"]
    assert row["decision"] == "apply_single_source"
    # The single-source mark reuses the exact deterministic-stage string.
    assert row["applied_detail"]["status_text"] == DESCRIPTION_SINGLE_SOURCE_STATUS
    assert row["applied_detail"]["owner_count"] == 1
    # The mark must reach the editor, not only the preview file. The review
    # derives it from the persisted stage outcome, so the applied record
    # carries that outcome and the rendered row shows the warning.
    assert record["grounded_outcome"] == OUTCOME_SINGLE_SOURCE
    rendered = _render_neutral_row(
        {"identity_key": "work one|alex author", "title": TITLE, "primary_authors": [AUTHOR]},
        {},
        record,
    )
    assert rendered["description_status"] == DESCRIPTION_SINGLE_SOURCE_STATUS


def test_two_source_applied_record_renders_the_grounded_status(tmp_path: Path) -> None:
    """A two-owner description must not borrow the one-owner warning."""
    state_dir, output_dir = _workspace(tmp_path, [_record("candidate-001", "work one|alex author")])
    adapter = ReplayDescriptionAdjudicationAdapter(
        {
            "work one|alex author": _response(
                "work one|alex author",
                [
                    _citation(PUBLISHER_URL, "publisher-example.test", "publisher"),
                    _citation(CATALOGUE_URL, "catalog-example.test", "library_catalogue"),
                ],
            )
        }
    )

    adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=adapter,
        fetcher=_fetcher(PUBLISHER_URL, CATALOGUE_URL),
        apply=True,
    )

    (record,) = read_state(state_dir, "candidate_history")
    rendered = _render_neutral_row(
        {"identity_key": "work one|alex author", "title": TITLE, "primary_authors": [AUTHOR]},
        {},
        record,
    )
    assert rendered["description_status"] == DESCRIPTION_GROUNDED_STATUS
    assert rendered["description_stage_outcome"] == OUTCOME_GROUNDED


def test_stage_outcome_values_match_the_grounded_stage_vocabulary() -> None:
    """The repeated outcome values must not drift from their owner.

    ``content_description`` cannot import ``grounded_description``, because
    the import runs the other way. The values are therefore repeated, and
    this pins them equal.
    """
    assert DESCRIPTION_STAGE_OUTCOME_VALUES == frozenset({OUTCOME_GROUNDED, OUTCOME_SINGLE_SOURCE})


def test_record_without_a_candidate_id_is_never_applied(tmp_path: Path) -> None:
    """Both halves of the identity must hold before anything is written.

    The acceptance bar names the exact Candidate ID as well as the exact
    canonical identity key. A record that carries no Candidate ID cannot be
    identified exactly, so it falls to fallback rather than being applied.
    """
    record = _record("", "work one|alex author")
    state_dir, output_dir = _workspace(tmp_path, [record])
    adapter = ReplayDescriptionAdjudicationAdapter(
        {
            "work one|alex author": _response(
                "work one|alex author",
                [
                    _citation(PUBLISHER_URL, "publisher-example.test", "publisher"),
                    _citation(CATALOGUE_URL, "catalog-example.test", "library_catalogue"),
                ],
            )
        }
    )

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=adapter,
        fetcher=_fetcher(PUBLISHER_URL, CATALOGUE_URL),
        apply=True,
    )

    assert result["applied"] == 0
    assert result["fallback"] == 1
    (stored,) = read_state(state_dir, "candidate_history")
    assert stored["content_description"] == DESCRIPTION_FALLBACK
    assert stored["content_description_rejection_reason"] == REASON_AMBIGUITY


def test_editor_corrected_record_is_preserved_byte_for_byte(tmp_path: Path) -> None:
    """Naming only a protected record must leave the file untouched."""
    protected = _record("candidate-001", "work one|alex author", VALID_TEXT)
    protected["description_provenance"] = {
        "identity_key": "work one|alex author",
        "content_description": VALID_TEXT,
        "provenance": "editor-corrected",
        "applied_date": "2026-09-01",
        "evidence_basis": [],
    }
    state_dir, output_dir = _workspace(tmp_path, [protected])
    before = state_snapshot(state_dir)

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        only=["work one|alex author"],
        effective_date=OPERATION_DATE,
        adapter=ReplayDescriptionAdjudicationAdapter({}),
        fetcher=_fetcher(),
        apply=True,
    )

    assert state_snapshot(state_dir) == before
    assert result["skipped"] == 1
    (row,) = read_json(output_dir / "description-adjudication-preview.json")["preview"]
    assert row["decision"] == "skipped"
    assert row["decision_reason"] == "editor-corrected record preserved"


def test_unresolved_response_leaves_fallback_with_bounded_reason(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path, [_record("candidate-001", "work one|alex author")])
    response = _response(
        "work one|alex author",
        [
            _citation(PUBLISHER_URL, "publisher-example.test", "publisher"),
            _citation(CATALOGUE_URL, "catalog-example.test", "library_catalogue"),
        ],
    )
    response["resolved"] = False
    response["unresolved_reason"] = "pages disagree on the work identity"
    adapter = ReplayDescriptionAdjudicationAdapter({"work one|alex author": response})

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=adapter,
        fetcher=_fetcher(PUBLISHER_URL, CATALOGUE_URL),
        apply=True,
    )

    assert result["applied"] == 0
    assert result["fallback"] == 1
    (record,) = read_state(state_dir, "candidate_history")
    assert record["content_description"] == DESCRIPTION_FALLBACK
    # The editor reads a closed reason code that already has a Russian
    # label, never the adapter's free text. The adapter's own words stay in
    # the preview, where they are diagnostics rather than editor copy.
    assert record["content_description_rejection_reason"] == REASON_AMBIGUITY
    assert reason_label(record["content_description_rejection_reason"]) == "Не удалось однозначно определить книгу"
    preview = read_json(output_dir / "description-adjudication-preview.json")
    assert preview["preview"][0]["decision"] == "fallback"
    assert preview["preview"][0]["response"]["unresolved_reason"] == "pages disagree on the work identity"


def test_zero_surviving_citations_leaves_fallback(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path, [_record("candidate-001", "work one|alex author")])
    adapter = ReplayDescriptionAdjudicationAdapter(
        {
            "work one|alex author": _response(
                "work one|alex author",
                [_citation(PUBLISHER_URL, "publisher-example.test", "publisher", title="Missing Title")],
                # Rule change (issue #791): a high-confidence description
                # with no surviving citation is now kept as unverified text,
                # which test_issue_791 covers. Below high confidence the
                # record still stays at fallback.
                confidence="medium",
            )
        }
    )

    adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=adapter,
        fetcher=_fetcher(PUBLISHER_URL),
        apply=True,
    )

    (record,) = read_state(state_dir, "candidate_history")
    assert record["content_description"] == DESCRIPTION_FALLBACK
    assert record["content_description_rejection_reason"] == REASON_FEWER_THAN_TWO
    preview = read_json(output_dir / "description-adjudication-preview.json")
    assert preview["preview"][0]["decision_reason"] == REASON_FEWER_THAN_TWO


def test_rejected_text_leaves_fallback(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path, [_record("candidate-001", "work one|alex author")])
    adapter = ReplayDescriptionAdjudicationAdapter(
        {
            "work one|alex author": _response(
                "work one|alex author",
                [
                    _citation(PUBLISHER_URL, "publisher-example.test", "publisher"),
                    _citation(CATALOGUE_URL, "catalog-example.test", "library_catalogue"),
                ],
                text="",
            )
        }
    )

    adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=adapter,
        fetcher=_fetcher(PUBLISHER_URL, CATALOGUE_URL),
        apply=True,
    )

    (record,) = read_state(state_dir, "candidate_history")
    assert record["content_description"] == DESCRIPTION_FALLBACK
    assert record["content_description_rejection_reason"] == REASON_INVALID_DESCRIPTION


def test_copied_prose_is_rejected_by_existing_validator(tmp_path: Path) -> None:
    validated, rejection = validate_content_description(COPIED_TEXT)

    assert validated is None
    assert rejection == "rejected as copied publisher/review prose"

    state_dir, output_dir = _workspace(tmp_path, [_record("candidate-001", "work one|alex author")])
    adapter = ReplayDescriptionAdjudicationAdapter(
        {
            "work one|alex author": _response(
                "work one|alex author",
                [
                    _citation(PUBLISHER_URL, "publisher-example.test", "publisher"),
                    _citation(CATALOGUE_URL, "catalog-example.test", "library_catalogue"),
                ],
                text=COPIED_TEXT,
            )
        }
    )

    adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=adapter,
        fetcher=_fetcher(PUBLISHER_URL, CATALOGUE_URL),
        apply=True,
    )

    (record,) = read_state(state_dir, "candidate_history")
    assert record["content_description"] == DESCRIPTION_FALLBACK
    assert record["content_description_rejection_reason"] == REASON_INVALID_DESCRIPTION


def test_provenance_value_is_separate_from_grounded_and_backfilled() -> None:
    assert ADJUDICATION_PROVENANCE == "agent-adjudicated"
    assert ADJUDICATION_PROVENANCE in DESCRIPTION_PROVENANCE_VALUES
    assert "grounded" in DESCRIPTION_PROVENANCE_VALUES
    assert "backfilled" in DESCRIPTION_PROVENANCE_VALUES
    assert ADJUDICATION_PROVENANCE not in ("grounded", "backfilled")


def test_editor_corrected_record_is_preserved_and_reported_skipped(tmp_path: Path) -> None:
    protected = _record("candidate-001", "work one|alex author", VALID_TEXT)
    protected["description_provenance"] = {
        "identity_key": "work one|alex author",
        "content_description": VALID_TEXT,
        "provenance": "editor-corrected",
        "applied_date": "2026-09-01",
        "evidence_basis": [],
    }
    state_dir, output_dir = _workspace(tmp_path, [protected, _record("candidate-002", "work two|alex author")])
    adapter = ReplayDescriptionAdjudicationAdapter(
        {
            "work two|alex author": {
                "identity_key": "work two|alex author",
                "resolved": True,
                "unresolved_reason": "",
                "confidence": "medium",
                "content_description": (
                    "Книга о дороге, доме и возвращении через ландшафты памяти и семейных историй. "
                    "Автор соединяет путевые заметки с размышлениями о принадлежности и утрате. "
                    "Редакционный крючок — медленный взгляд на знакомые места как на чужие."
                ),
                "citations": [
                    _citation(PUBLISHER_URL, "publisher-example.test", "publisher"),
                    _citation(CATALOGUE_URL, "catalog-example.test", "library_catalogue"),
                ],
            }
        }
    )
    before = read_state(state_dir, "candidate_history")[0]

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        only=["work one|alex author", "work two|alex author"],
        effective_date=OPERATION_DATE,
        adapter=adapter,
        fetcher=_fetcher(PUBLISHER_URL, CATALOGUE_URL),
        apply=True,
    )

    history = read_state(state_dir, "candidate_history")
    assert history[0] == before
    assert result["skipped"] == 1
    preview = read_json(output_dir / "description-adjudication-preview.json")
    rows = {row["identity_key"]: row for row in preview["preview"]}
    assert rows["work one|alex author"]["decision"] == "skipped"
    assert rows["work one|alex author"]["application"]["skipped"] == 1
    assert rows["work two|alex author"]["application"]["applied"] == 1


def test_valid_existing_description_is_preserved_and_reported_skipped(tmp_path: Path) -> None:
    kept = _record("candidate-001", "work one|alex author", VALID_TEXT)
    kept["description_provenance"] = {
        "identity_key": "work one|alex author",
        "content_description": VALID_TEXT,
        "provenance": "grounded",
        "applied_date": "2026-09-01",
        "evidence_basis": ["https://example.test/evidence"],
    }
    state_dir, output_dir = _workspace(tmp_path, [kept])
    before = read_state(state_dir, "candidate_history")[0]

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        only=["work one|alex author"],
        effective_date=OPERATION_DATE,
        apply=True,
    )

    assert read_state(state_dir, "candidate_history")[0] == before
    assert result["applied"] == 0
    assert result["skipped"] == 1
    preview = read_json(output_dir / "description-adjudication-preview.json")
    (row,) = preview["preview"]
    assert row["decision"] == "skipped"
    assert row["decision_reason"] == "valid description preserved"


def test_preview_without_apply_changes_no_state(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path, [_record("candidate-001", "work one|alex author")])
    adapter = ReplayDescriptionAdjudicationAdapter(
        {
            "work one|alex author": _response(
                "work one|alex author",
                [
                    _citation(PUBLISHER_URL, "publisher-example.test", "publisher"),
                    _citation(CATALOGUE_URL, "catalog-example.test", "library_catalogue"),
                ],
            )
        }
    )
    before_history = state_snapshot(state_dir)
    before_visibility = state_snapshot(state_dir)

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=adapter,
        fetcher=_fetcher(PUBLISHER_URL, CATALOGUE_URL),
    )

    assert state_snapshot(state_dir) == before_history
    assert state_snapshot(state_dir) == before_visibility
    # A dry run reports no applied work, because it applied none. What an
    # apply run would do is reported under a name that says so.
    assert result["applied"] == 0
    assert result["would_apply"] == 1
    # The preview is still complete: verification, decision, artifact, and
    # the gate verdict the apply run would produce.
    preview = read_json(output_dir / "description-adjudication-preview.json")
    assert preview["applied"] is False
    assert preview["counts"]["applied"] == 0
    assert preview["counts"]["would_apply"] == 1
    (row,) = preview["preview"]
    assert row["decision"] == "apply_two_source"
    assert len(row["verification"]["surviving"]) == 2
    assert row["artifact"]["content_description"] == VALID_TEXT
    assert row["application"]["applied"] == 1


def test_identity_mismatch_leaves_fallback(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path, [_record("candidate-001", "work one|alex author")])

    class _MismatchedAdapter:
        def adjudicate(self, work_item: dict[str, Any]) -> dict[str, Any]:
            response = _response(
                "another|identity",
                [_citation(PUBLISHER_URL, "publisher-example.test", "publisher")],
            )
            response["identity_key"] = "another|identity"
            return response

    adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=_MismatchedAdapter(),  # type: ignore[arg-type]
        fetcher=_fetcher(PUBLISHER_URL),
        apply=True,
    )

    (record,) = read_state(state_dir, "candidate_history")
    assert record["content_description"] == DESCRIPTION_FALLBACK
    assert record["content_description_rejection_reason"] == REASON_AMBIGUITY


def test_apply_changes_only_description_fields(tmp_path: Path) -> None:
    protected_fields = {
        "candidate_id": "candidate-001",
        "identity_key": "work one|alex author",
        "title": TITLE,
        "primary_authors": [AUTHOR],
        "publisher": "Example Press",
        "publication_year": "2025",
        "editor_status": "interested",
        "editor_note": "editor note stays",
    }
    record = dict(protected_fields)
    record["content_description"] = ""
    state_dir, output_dir = _workspace(tmp_path, [record])
    adapter = ReplayDescriptionAdjudicationAdapter(
        {
            "work one|alex author": _response(
                "work one|alex author",
                [
                    _citation(PUBLISHER_URL, "publisher-example.test", "publisher"),
                    _citation(CATALOGUE_URL, "catalog-example.test", "library_catalogue"),
                ],
            )
        }
    )

    adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=adapter,
        fetcher=_fetcher(PUBLISHER_URL, CATALOGUE_URL),
        apply=True,
    )

    (updated,) = read_state(state_dir, "candidate_history")
    for field, value in protected_fields.items():
        assert updated[field] == value
    assert updated["content_description"] == VALID_TEXT
