"""Review rendering operator command: render-review."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.archive_backfill import archive_backfill
from autoscout.archive_backfill import archive_batch_label
from autoscout.autobiographical_context import autobiographical_subject_context_warning
from autoscout.candidate_identity import candidate_id_for
from autoscout.candidate_identity import effective_candidate_id
from autoscout.candidate_identity import empty_alias_table
from autoscout.candidate_identity import short_candidate_ids
from autoscout.candidate_schema import CandidateIsbn
from autoscout.candidate_verification import is_unrun_provider_verification
from autoscout.candidate_verification import restore_russian_edition_from_exclusion
from autoscout.content_description import DESCRIPTION_ATTEMPTED_STATUS
from autoscout.content_description import DESCRIPTION_BACKFILLED_STATUS
from autoscout.content_description import DESCRIPTION_BELOW_DEPTH_STATUS
from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.content_description import DESCRIPTION_GROUNDED_STATUS
from autoscout.content_description import DESCRIPTION_NOT_ATTEMPTED_STATUS
from autoscout.content_description import DESCRIPTION_NOT_YET_PUBLISHED_DATE_FIELD
from autoscout.content_description import DESCRIPTION_NOT_YET_PUBLISHED_STATUS
from autoscout.content_description import DESCRIPTION_PM_DEAL_PENDING_TEXT
from autoscout.content_description import DESCRIPTION_PM_LOGLINE_PROVENANCE
from autoscout.content_description import DESCRIPTION_PM_LOGLINE_STATUS
from autoscout.content_description import DESCRIPTION_SINGLE_SOURCE_STATUS
from autoscout.content_description import DESCRIPTION_UNCONFIRMED_STATUS
from autoscout.content_description import awaits_publication
from autoscout.content_description import candidate_source_names
from autoscout.content_description import coverage_denominator
from autoscout.content_description import description_coverage
from autoscout.content_description import has_nonfallback_description
from autoscout.content_description import is_unconfirmed_description
from autoscout.content_description import pm_logline_count
from autoscout.content_description import record_has_usable_description
from autoscout.content_description import validate_record_description
from autoscout.content_description import verified_description_count
from autoscout.description_adjudication import ADJUDICATION_PROVENANCE
from autoscout.description_adjudication import ADJUDICATION_UNVERIFIED_PROVENANCE
from autoscout.description_depth import meets_description_depth
from autoscout.editorial_taste_profile import TasteProfileError
from autoscout.editorial_taste_profile import active_hard_rules
from autoscout.editorial_taste_profile import prepare_rules
from autoscout.exclusion_history import merge_exclusion_records
from autoscout.fit_scores import FIT_ORDER_NOTE
from autoscout.fit_scores import FIT_SCORE_COLUMN
from autoscout.fit_scores import FitScores
from autoscout.fit_scores import fit_ordering_enabled
from autoscout.fit_scores import fit_score_text
from autoscout.fit_scores import read_fit_scores
from autoscout.grounded_description import OUTCOME_ATTEMPTED_NO_DESCRIPTION
from autoscout.grounded_description import OUTCOME_GROUNDED
from autoscout.grounded_description import OUTCOME_NOT_ATTEMPTED
from autoscout.grounded_description import OUTCOME_SINGLE_SOURCE
from autoscout.grounded_description import prominence_structured_fields
from autoscout.grounded_description import prominence_summary
from autoscout.grounded_description import reason_label
from autoscout.grounded_description import stage_outcome_map
from autoscout.html_review import build_html_review
from autoscout.original_edition_presentation import format_original_edition
from autoscout.pm_publication import is_pm_only
from autoscout.pm_publication import is_rights_lead_only
from autoscout.probable_duplicates import load_dismissals
from autoscout.probable_duplicates import probable_duplicate_pairs
from autoscout.reporting import capture_ledger_from_workflow_log
from autoscout.review_dates import ARCHIVE_DECIDED_SENTENCE
from autoscout.review_dates import added_date
from autoscout.review_dates import added_on_run_date_count
from autoscout.review_dates import marked_updated_count
from autoscout.review_dates import reading_order
from autoscout.review_dates import updated_cell_text
from autoscout.review_hiding import DECIDED_EDITION_HIDDEN_LABEL
from autoscout.review_hiding import EARLIER_SOURCE_NAMES_FIELD
from autoscout.review_hiding import HIDDEN_BY_DECIDED_EDITION
from autoscout.review_hiding import HIDDEN_BY_HARD_RULE
from autoscout.review_hiding import HIDDEN_BY_RETIRED_SOURCE
from autoscout.review_hiding import HIDDEN_FROM_REVIEW_ARTIFACT
from autoscout.review_hiding import decided_edition_hidden_record
from autoscout.review_hiding import evidence_source_names
from autoscout.review_hiding import known_titles
from autoscout.review_hiding import review_hidden_record
from autoscout.review_hiding import withdrawn_source_names_from_config
from autoscout.review_presentation import DESCRIPTION_IDENTITY_NOTE_TEXT
from autoscout.review_presentation import description_provenance_label
from autoscout.review_presentation import editor_status_label
from autoscout.review_presentation import review_context as presentation_review_context
from autoscout.review_presentation import rights_deal_line
from autoscout.review_presentation import risk_status_label
from autoscout.review_presentation import russian_status_label
from autoscout.review_presentation import russian_verification_review_text
from autoscout.review_presentation import russian_verification_summary_text
from autoscout.review_presentation import visible_risk_coverage_breakdown
from autoscout.review_presentation import visible_risk_coverage_review_text
from autoscout.review_presentation import visible_risk_coverage_summary_text
from autoscout.russian_artifacts import russian_verification_counts
from autoscout.russian_edition import validate_russian_edition
from autoscout.russian_edition_presentation import format_russian_edition
from autoscout.russian_editions import is_valid_russian_edition_verification
from autoscout.russian_status import is_found_in_russian
from autoscout.scouting_candidates import ScoutingCandidate
from autoscout.shop_links import bookfinder_link
from autoscout.shop_links import iberlibro_link
from autoscout.source_eligibility import historical_record_source_eligibility_exclusion_reason
from autoscout.source_page_identity import incomplete_coverage_from_run
from autoscout.source_page_identity import left_out_titles_text
from autoscout.source_problem_notes import complete_source_runs
from autoscout.source_problem_notes import source_problem_lines
from autoscout.source_problem_notes import source_problem_notes
from autoscout.source_row_history import departure_review_lines
from autoscout.source_run_status import summarize_source_runs
from autoscout.source_trial import TrialSource
from autoscout.source_trial import row_trial_fields
from autoscout.source_trial import trial_sources_from_config
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import open_run_output_store
from autoscout.state_store import open_state_store
from autoscout.state_store import select_state_backend_from_file
from autoscout.title_evidence import clean_title
from autoscout.title_evidence import format_editor_english_author_display
from autoscout.title_evidence import format_editor_english_title
from autoscout.title_evidence import merge_title_metadata
from autoscout.title_evidence import title_evidence_source_urls
from autoscout.visible_risk_coverage import VISIBLE_RISK_SCREEN_COVERAGE_FIELD
from autoscout.weekly_report import WeeklyReportStore
from autoscout.weekly_report import read_run_history
from autoscout.weekly_report_row import editor_lifecycle_label
from autoscout.weekly_report_row import editor_row_from_candidate


def markdown_table_cell(value: Any) -> str:
    return str(value).replace("\n", " ").replace("|", "&#124;")


def _merge_evidence_records(*groups: Any) -> list[dict[str, Any]]:
    """Merge evidence groups while preserving first-seen order and uniqueness."""
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        records = [group] if isinstance(group, dict) else group
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            key = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
            merged.setdefault(key, deepcopy(record))
    return list(merged.values())


def _safe_history_candidate(record: dict[str, Any]) -> dict[str, Any]:
    """Turn a possibly old history record into a renderable candidate shape."""
    candidate = deepcopy(record)
    identity_key = str(candidate.get("identity_key", "")).strip()
    candidate["identity_key"] = identity_key
    authors = candidate.get("primary_authors", [])
    if isinstance(authors, str):
        candidate["primary_authors"] = [authors] if authors else []
    elif isinstance(authors, list):
        candidate["primary_authors"] = [str(author) for author in authors if str(author).strip()]
    else:
        candidate["primary_authors"] = []
    if not candidate.get("title") and "|" in identity_key:
        candidate["title"] = identity_key.split("|", 1)[0].title()
    evidence = candidate.get("source_evidence", [])
    if isinstance(evidence, dict):
        candidate["source_evidence"] = [evidence]
    elif isinstance(evidence, list):
        candidate["source_evidence"] = [item for item in evidence if isinstance(item, dict)]
    else:
        candidate["source_evidence"] = []
    entity = ScoutingCandidate.from_mapping(candidate)
    if not candidate.get("primary_signal") and candidate["source_evidence"]:
        candidate["primary_signal"] = entity.primary_signal()
    if not candidate.get("independent_signal_count") and candidate["source_evidence"]:
        candidate["independent_signal_count"] = len(entity.independent_signal_families())
    if not candidate.get("independent_signal_families") and candidate["source_evidence"]:
        candidate["independent_signal_families"] = entity.independent_signal_families()
    if not candidate.get("priority_label"):
        candidate["priority_label"] = entity.priority_label()
    if not candidate.get("signal_summary") and candidate["source_evidence"]:
        entity.apply_signal_aggregation()
        candidate["signal_summary"] = entity.get("signal_summary", "")
    return candidate


LIFECYCLE_ORDER = {"new": 0, "upgraded": 1, "changed": 1, "unchanged": 2, "previously_seen": 3}
RAW_UPDATE_DELTAS = {"new", "upgraded", "changed", "unchanged"}
PRIORITY_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3}


def _is_backfilled(provenance: Any) -> bool:
    """True when a description provenance names an operator backfill."""
    return isinstance(provenance, dict) and str(provenance.get("provenance", "")).strip() == "backfilled"


def _shop_link_fields(isbn: str, title: str, author: str) -> dict[str, str]:
    """The review row's shop links: BookFinder's URL, IberLibro's URL and, for an exact edition, the ISBN.

    Both links come from the same ISBN, title and author, so they are always
    the same kind: two exact-edition links or two searches.
    """
    link = bookfinder_link(isbn, title, author)
    if link is None:
        return {"shop_url": "", "shop_isbn": "", "iberlibro_url": ""}
    second = iberlibro_link(isbn, title, author)
    return {"shop_url": link.url, "shop_isbn": link.isbn, "iberlibro_url": second.url if second else ""}


def _render_neutral_row(
    candidate: dict[str, Any],
    row: dict[str, str],
    history_record: dict[str, Any] | None = None,
    *,
    update_delta: str | None = None,
    review_context: str = "",
    grounded_stage_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    history_record = history_record or {}
    identity_key = str(candidate.get("identity_key", ""))
    # Candidate History owns the durable ID; a current snapshot only fills in
    # when no history record exists yet.
    candidate_id = str(history_record.get("candidate_id", "") or candidate.get("candidate_id", "")).strip()
    editor_status = str(
        history_record.get("editor_status", "") or candidate.get("editor_status", "") or row.get("Editor status", "")
    )
    editor_note = str(
        history_record.get("editor_note", "") or candidate.get("editor_note", "") or row.get("Editor note", "")
    )
    # The current snapshot wins: the post-enrichment refresh labels a late
    # Visible Risk change there, after the pipeline wrote the CSV row.
    raw_delta = str(candidate.get("update_delta") or row.get("Update delta") or "unchanged").strip()
    if raw_delta not in RAW_UPDATE_DELTAS:
        raw_delta = "unchanged"
    lifecycle_delta = str(update_delta if update_delta is not None else raw_delta).strip()
    lifecycle_label = (
        "Previously seen" if lifecycle_delta == "previously_seen" else editor_lifecycle_label(lifecycle_delta)
    )
    candidate_authors = candidate.get("primary_authors", [])
    authors = ", ".join(candidate_authors) if isinstance(candidate_authors, list) else str(candidate_authors)
    candidate_record = {**history_record, **candidate}
    # Candidate snapshots can be older than a standalone description
    # backfill.  Description state is canonical in Candidate History, so do
    # not let a stale current snapshot mask a newer persisted description.
    if str(history_record.get("content_description", "")).strip():
        candidate_record["content_description"] = history_record["content_description"]
        if "description_provenance" in history_record:
            candidate_record["description_provenance"] = deepcopy(history_record["description_provenance"])
        if "content_description_rejection_reason" in history_record:
            candidate_record["content_description_rejection_reason"] = history_record[
                "content_description_rejection_reason"
            ]
    # The same holds for the Russian-edition finding (#1181): a snapshot written
    # before a later completion pass must not hide the finding history holds.
    if validate_russian_edition(history_record.get("russian_edition")):
        candidate_record["russian_edition"] = deepcopy(history_record["russian_edition"])
    stage_record = grounded_stage_record or {}
    if stage_record and str(stage_record.get("outcome", "")).strip() not in {
        OUTCOME_GROUNDED,
        OUTCOME_SINGLE_SOURCE,
    }:
        # The adjudication stage runs second, over what the grounded stage
        # left at fallback, so a durable adjudicated description is the
        # newer fact: it wins over a failing grounded record from the same
        # run instead of showing a failure beside a real description (issue
        # #644). Every other precedence is unchanged: a succeeded grounded
        # record still wins, and only the adjudication provenances override.
        # An agent-unverified description is the same newer fact with its
        # own warning status (issue #791).
        history_provenance = history_record.get("description_provenance")
        if (
            isinstance(history_provenance, dict)
            and str(history_provenance.get("provenance", "")).strip()
            in (ADJUDICATION_PROVENANCE, ADJUDICATION_UNVERIFIED_PROVENANCE)
            and record_has_usable_description(history_record)
        ):
            stage_record = {}
    # A standalone editor-approved backfill can land after the weekly
    # grounded-description stage has already recorded a not-attempted result.
    # The canonical backfill provenance is newer than that stale stage result;
    # project it into the same status vocabulary so the review does not show
    # a fresh description beside "not attempted" metadata.
    history_provenance = history_record.get("description_provenance")
    if (
        stage_record
        and str(stage_record.get("outcome", "")).strip() in {OUTCOME_NOT_ATTEMPTED, OUTCOME_ATTEMPTED_NO_DESCRIPTION}
        and isinstance(history_provenance, dict)
        and str(history_provenance.get("provenance", "")).strip() == "backfilled"
        and record_has_usable_description(history_record)
    ):
        evidence_basis = history_provenance.get("evidence_basis", [])
        evidence_count = len(evidence_basis) if isinstance(evidence_basis, list) else 0
        backfill_outcome = (
            OUTCOME_SINGLE_SOURCE if evidence_count == 1 else OUTCOME_GROUNDED if evidence_count >= 2 else ""
        )
        if backfill_outcome:
            stage_record = {
                "candidate_id": str(history_record.get("candidate_id", "")),
                "identity_key": identity_key,
                "outcome": backfill_outcome,
                "content_description": str(history_record.get("content_description", "")),
                "description_provenance": deepcopy(history_provenance),
            }
    if not stage_record:
        persisted_outcome = str(history_record.get("grounded_outcome", "")).strip()
        if persisted_outcome in {OUTCOME_GROUNDED, OUTCOME_SINGLE_SOURCE}:
            stage_record = {
                "candidate_id": str(history_record.get("candidate_id", "")),
                "identity_key": identity_key,
                "outcome": persisted_outcome,
                "content_description": str(history_record.get("content_description", "")),
                "description_provenance": deepcopy(history_record.get("description_provenance", {})),
            }
    stage_outcome = str(stage_record.get("outcome", "")).strip()
    stage_reason = str(stage_record.get("reason_code", "")).strip()
    if (
        stage_outcome in {OUTCOME_GROUNDED, OUTCOME_SINGLE_SOURCE}
        and str(stage_record.get("content_description", "")).strip()
    ):
        candidate_record["content_description"] = str(stage_record["content_description"])
        stage_provenance = stage_record.get("description_provenance")
        if isinstance(stage_provenance, dict):
            candidate_record["description_provenance"] = deepcopy(stage_provenance)
    description_record = candidate_record if candidate_record.get("content_description", "") else history_record
    raw_content_description = str(description_record.get("content_description", "")).strip()
    rejection_reason = str(
        candidate.get("content_description_rejection_reason", "")
        or history_record.get("content_description_rejection_reason", "")
    ).strip()
    if not rejection_reason:
        rejection_reason = next(
            (
                str(evidence.get("content_description_rejection_reason", "")).strip()
                for evidence in candidate.get("source_evidence", [])
                if isinstance(evidence, dict) and evidence.get("content_description_rejection_reason")
            ),
            "",
        )
    validated_description, rendering_reason = validate_record_description(description_record)
    raw_has_description = has_nonfallback_description(raw_content_description)
    if not raw_has_description or validated_description is None:
        rendered_description = DESCRIPTION_FALLBACK
        if raw_has_description and not rejection_reason:
            rejection_reason = rendering_reason or "unknown"
    else:
        raw_content_description = validated_description
        rendered_description = validated_description
    # Mechanical depth is assessed on the rendered text, separately from
    # safety and grounding: a fallback row carries no depth marker, and a
    # below-floor description keeps its safety/grounding status with the
    # depth marker beside it (issue #672).
    description_meets_depth = rendered_description == DESCRIPTION_FALLBACK or meets_description_depth(
        rendered_description
    )
    description_depth_status = "" if description_meets_depth else DESCRIPTION_BELOW_DEPTH_STATUS
    legacy_unconfirmed = is_unconfirmed_description(
        rendered_description,
        description_record.get("description_provenance"),
    )
    logline_provenance = description_record.get("description_provenance")
    if (
        rendered_description != DESCRIPTION_FALLBACK
        and isinstance(logline_provenance, dict)
        and logline_provenance.get("provenance") == DESCRIPTION_PM_LOGLINE_PROVENANCE
    ):
        # Written from one sentence of the deal announcement: the label says
        # so, whatever stage result sits beside the row (issue #950). It
        # restates one sentence, so it carries no depth marker either.
        description_status = DESCRIPTION_PM_LOGLINE_STATUS
        description_depth_status = ""
    elif legacy_unconfirmed:
        # Part 1 remains visible for historical Codex rows even when a later
        # grounded-stage attempt is recorded beside the row.
        description_status = DESCRIPTION_UNCONFIRMED_STATUS
    elif rendered_description == DESCRIPTION_FALLBACK and awaits_publication(history_record):
        # Nothing failed for this row: the book is not out yet, and that is
        # truer than whichever stage result sits beside it (issue #791).
        description_status = DESCRIPTION_NOT_YET_PUBLISHED_STATUS
    elif stage_outcome in {OUTCOME_GROUNDED, OUTCOME_SINGLE_SOURCE} and _is_backfilled(
        description_record.get("description_provenance")
    ):
        # Research sources listed by an operator are not checked pages, so a
        # backfill never shows the grounded statuses (issue #912).
        description_status = DESCRIPTION_BACKFILLED_STATUS
    elif stage_outcome == OUTCOME_GROUNDED:
        description_status = DESCRIPTION_GROUNDED_STATUS
    elif stage_outcome == OUTCOME_SINGLE_SOURCE:
        description_status = DESCRIPTION_SINGLE_SOURCE_STATUS
    elif stage_outcome == OUTCOME_ATTEMPTED_NO_DESCRIPTION:
        description_status = DESCRIPTION_ATTEMPTED_STATUS
    elif stage_outcome == OUTCOME_NOT_ATTEMPTED:
        description_status = DESCRIPTION_NOT_ATTEMPTED_STATUS
    else:
        description_status = ""
    rights_deal = rights_deal_line(candidate_record.get("source_evidence"))
    # The text the editor reads. A deal card with no description says the book
    # is not out yet and points at its deal facts, in place of the generic
    # placeholder; everything below still reasons about ``rendered_description``.
    displayed_description = rendered_description
    if description_status == DESCRIPTION_NOT_YET_PUBLISHED_STATUS and rights_deal and is_pm_only(history_record):
        displayed_description = DESCRIPTION_PM_DEAL_PENDING_TEXT
    # An archive backfill's books wait for a decision in their own block; a
    # decided one is a card like any other, and keeps the marker in history.
    archive_marker = archive_backfill(history_record)
    archive_pending = archive_marker is not None and not editor_status.strip()
    archive_label = archive_batch_label(archive_marker["batch"]) if archive_marker else ""
    # One provenance line beside the one status: which sources the
    # description rests on, in the editor's words. It follows the same
    # record that supplied the rendered description, and it is shown only
    # beside a real description, so a fallback row never gains a line and
    # runs without verified descriptions render exactly as before (issue
    # #644).
    provenance_label = ""
    identity_note_text = ""
    if rendered_description != DESCRIPTION_FALLBACK:
        provenance_record = description_record.get("description_provenance")
        provenance_label = description_provenance_label(provenance_record)
        if provenance_label and isinstance(provenance_record, dict) and provenance_record.get("identity_note_present"):
            identity_note_text = DESCRIPTION_IDENTITY_NOTE_TEXT
    provenance_display = provenance_label
    if provenance_label and identity_note_text:
        provenance_display = f"{provenance_label}. {identity_note_text}"
    title_metadata = merge_title_metadata(history_record, candidate)
    prominence_claims = stage_record.get("prominence_claims", [])
    if not isinstance(prominence_claims, list):
        prominence_claims = []
    prominence_line = prominence_summary(prominence_claims)
    prominence_claim_type, prominence_evidence_url = prominence_structured_fields(prominence_claims)
    if title_metadata:
        candidate_record.update(title_metadata)
    subject_context_warning = autobiographical_subject_context_warning(
        candidate_record,
        content_description=rendered_description,
    )
    publisher = str(candidate.get("publisher", "") or history_record.get("publisher", "")).strip()
    publication_year = str(candidate.get("publication_year", "") or history_record.get("publication_year", "")).strip()
    primary_source = candidate_record.get("primary_signal")
    if not isinstance(primary_source, dict):
        evidence_records = candidate_record.get("source_evidence", [])
        primary_source = evidence_records[0] if isinstance(evidence_records, list) and evidence_records else {}
    if not isinstance(primary_source, dict):
        primary_source = {}
    rendered_title = clean_title(candidate.get("title", row.get("Title", "")))
    risk_status = str(candidate.get("visible_risk_status", "") or row.get("Visible-risk status", ""))
    risk_comment = str(candidate.get("visible_risk_comment", "") or row.get("Risk comment", ""))
    priority = str(candidate.get("priority_label", "") or row.get("Priority label", ""))
    # The post-enrichment refresh rewrites a candidate's profile notes, and
    # can clear them, after the CSV row was written (issue #747).
    taste_notes = str(
        candidate["editorial_profile_notes"]
        if "editorial_profile_notes" in candidate
        else row.get("Taste profile notes", "")
    )
    evidence_languages = candidate_record.get("source_evidence")
    source_evidence_languages = [
        {"language": evidence.get("language", "")}
        for evidence in (evidence_languages if isinstance(evidence_languages, list) else [])
        if isinstance(evidence, dict)
    ]
    editor_row = dict(row)
    editor_row.update(
        {
            "Title": rendered_title,
            "Author": authors or row.get("Author", ""),
            "Editor status": editor_status,
            "Editor note": editor_note,
            "Candidate ID": identity_key,
            "Durable Candidate ID": candidate_id,
            "Update delta": raw_delta,
            "Lifecycle": lifecycle_label,
            "Review context": review_context,
            "Description": displayed_description,
            "Description status": description_status,
            "Publisher / year": " / ".join(value for value in (publisher, publication_year) if value),
            "English title": format_editor_english_title(candidate_record),
            "English author display": format_editor_english_author_display(candidate_record),
            # The post-enrichment rescreen can settle these after the
            # pipeline wrote its CSV, so the CSV states what the review
            # states (issue #714).
            "Visible-risk status": risk_status,
            "Risk comment": risk_comment,
            "Priority label": priority,
            "Taste profile notes": taste_notes,
        }
    )
    if stage_record:
        # The column always states the reason of the attempt that produced the
        # row. Writing it only when a reason exists left a repaired row showing
        # the reason of the attempt that had failed before it, next to a
        # confirmed description (issue #563).
        editor_row["Description reason"] = reason_label(stage_reason) if stage_reason else ""
    elif stage_reason:
        editor_row["Description reason"] = reason_label(stage_reason)
    if prominence_line:
        editor_row["Prominence"] = prominence_line
    if prominence_claim_type:
        editor_row["Prominence claim type"] = prominence_claim_type
    if prominence_evidence_url:
        editor_row["Prominence evidence URL"] = prominence_evidence_url
    if provenance_display:
        # Optional CSV field: the header stays stable and only gains this
        # column when a row actually carries a provenance line (issue #644).
        editor_row["Description provenance"] = provenance_display
    if description_depth_status:
        # Same conditional-column contract for the mechanical depth marker:
        # runs with no below-floor description keep their exact CSV shape.
        editor_row["Description depth"] = description_depth_status
    if rights_deal:
        # Same conditional-column contract: a run with no rights deal keeps its CSV shape (#945).
        editor_row["Rights deal"] = rights_deal
    editor_row.pop("Archive batch", None)
    if archive_pending:
        # Same conditional-column contract: the column names the block a row
        # waits in, so the editor can filter the CSV to the batch.
        editor_row["Archive batch"] = archive_label
    editor_row.pop("Original edition", None)
    original_text, original_url, _ = format_original_edition(candidate_record)
    if original_text:
        # CSV includes the supporting URL in parentheses when available.
        editor_row["Original edition"] = original_text + (f" ({original_url})" if original_url else "")
    editor_row.pop("Russian-edition finding", None)
    russian_text, russian_url, _ = format_russian_edition(candidate_record)
    if russian_text:
        # Same contract as the original edition: the mark with its evidence link (#1181).
        editor_row["Russian-edition finding"] = f"{russian_text} ({russian_url})"
    return {
        "idx": 0,
        "identity_key": identity_key,
        "candidate_id": candidate_id,
        "short_candidate_id": "",
        "title": rendered_title,
        "primary_authors": deepcopy(candidate_record.get("primary_authors", [])),
        "authors": authors or row.get("Author", ""),
        "update_delta": raw_delta,
        "lifecycle_label": lifecycle_label,
        "priority": priority,
        "signal_count": str(candidate.get("independent_signal_count", "") or row.get("Signal count", "")),
        "russian": str(candidate.get("russian_edition_status", "") or row.get("Russian-edition status", "")),
        "russian_url": str(candidate.get("russian_edition_url", "") or row.get("Russian-edition URL", "")),
        "russian_verification": deepcopy(candidate_record.get("russian_edition_verification", {})),
        "risk": risk_status,
        "risk_category": str(candidate.get("visible_risk_category", "")),
        "risk_comment": risk_comment,
        "risk_scope": str(candidate.get("risk_signal_scope", "")),
        "risk_evidence_url": str(candidate.get("risk_evidence_url", "")),
        VISIBLE_RISK_SCREEN_COVERAGE_FIELD: deepcopy(candidate_record.get(VISIBLE_RISK_SCREEN_COVERAGE_FIELD)),
        "editor_status": editor_status,
        "editor_note": editor_note,
        "description": displayed_description,
        "description_status": description_status,
        "description_meets_depth": description_meets_depth,
        "description_depth_status": description_depth_status,
        "description_provenance_label": provenance_label,
        "description_identity_note": identity_note_text,
        "description_provenance_display": provenance_display,
        "description_stage_outcome": stage_outcome,
        "description_stage_reason": reason_label(stage_reason) if stage_reason else "",
        "description_stage_reason_code": stage_reason,
        "prominence": prominence_line,
        "prominence_claim_type": prominence_claim_type,
        "prominence_evidence_url": prominence_evidence_url,
        "prominence_claims": deepcopy(prominence_claims),
        "autobiographical_subject_context_warning": subject_context_warning,
        "publisher": publisher,
        "publication_year": publication_year,
        "rights_deal": rights_deal,
        # The review page keeps rights leads on their own tab.
        "rights_lead_only": is_rights_lead_only(
            history_record if history_record.get("source_evidence") else candidate_record
        ),
        "original_edition": deepcopy(candidate_record.get("original_edition")),
        "russian_edition": deepcopy(candidate_record.get("russian_edition")),
        "work_age": deepcopy(candidate_record.get("work_age")),
        "content_description": raw_content_description,
        "content_description_rejection_reason": rejection_reason,
        "english_title": str(candidate_record.get("english_title", "")),
        "english_author_display": str(candidate_record.get("english_author_display", "")),
        "english_title_type": str(candidate_record.get("english_title_type", "")),
        "english_title_provenance": deepcopy(candidate_record.get("english_title_provenance")),
        "english_title_sources": title_evidence_source_urls(candidate_record),
        # The formatted helper title reads the evidence languages, so a
        # non-English-only book does not read as an English original (#969).
        "source_evidence": source_evidence_languages,
        "why_entered": str(row.get("Why it entered the list", "")),
        "signal_summary": str(candidate.get("signal_summary", "") or row.get("Signal summary", "")),
        "source": str(row.get("Source", "")),
        # Every source citing the candidate, not only the primary one shown in
        # ``source``: description coverage names fallbacks by source (#759).
        "source_names": candidate_source_names(candidate_record),
        "source_url": str(row.get("Source URL", "")),
        "source_country": str(primary_source.get("country", "") or primary_source.get("market", "")),
        "taste_notes": taste_notes,
        "links": str(row.get("Links", "")),
        **_shop_link_fields(
            # A blank snapshot ISBN must not hide one Candidate History holds
            # (issue #978): the history ISBN is the durable, fill-only value.
            CandidateIsbn.from_record(candidate).value or CandidateIsbn.from_record(history_record).value,
            rendered_title,
            next(iter(candidate_authors), "") if isinstance(candidate_authors, list) else str(candidate_authors or ""),
        ),
        # Issue #678 decision-support inputs: the blocker fields the review
        # card surfaces when evidence exists. narrative_classification lives
        # on per-mention source evidence; feedback_dimensions flow from
        # confirmed editor feedback via Candidate History.
        "narrative_classification": str(
            candidate_record.get("narrative_classification", "")
            or (primary_source.get("narrative_classification", "") if isinstance(primary_source, dict) else "")
        ),
        "candidate_category": str(candidate_record.get("candidate_category", "")),
        "feedback_dimensions": deepcopy(candidate_record.get("feedback_dimensions", {})),
        "review_context": review_context,
        "archive_batch": archive_marker["batch"] if archive_marker else "",
        "archive_batch_label": archive_label,
        "archive_applied_date": archive_marker["applied_date"] if archive_marker else "",
        "archive_pending": archive_pending,
        "first_seen_date": str(history_record.get("first_seen_date") or candidate.get("first_seen_date", "")),
        "last_material_change_date": str(
            history_record.get("last_material_change_date") or candidate.get("last_material_change_date", "")
        ),
        "editor_row": editor_row,
    }


def _build_review_rows(
    candidates: list[dict[str, Any]],
    id_to_row: dict[str, dict[str, str]],
    history: dict[str, dict[str, Any]],
    grounded_stage: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Assemble the render-neutral row model for the current run."""
    grounded_stage = grounded_stage or {}
    return [
        _render_neutral_row(
            candidate,
            id_to_row.get(str(candidate.get("identity_key", "")), editor_row_from_candidate(candidate)),
            history.get(str(candidate.get("identity_key", ""))),
            grounded_stage_record=grounded_stage.get(str(candidate.get("candidate_id", ""))),
        )
        for candidate in candidates
    ]


def _review_tie_break_key(row: dict[str, Any]) -> tuple[int, int, str, str]:
    try:
        signal_count = int(row.get("signal_count", "") or 0)
    except (TypeError, ValueError):
        signal_count = 0
    return (
        PRIORITY_ORDER.get(str(row.get("priority", "")).strip().upper(), 9),
        -signal_count,
        str(row.get("title", "")).casefold(),
        str(row.get("identity_key", "")),
    )


def _current_sort_key(row: dict[str, Any]) -> tuple[int, int, int, str, str]:
    return (
        LIFECYCLE_ORDER.get(str(row.get("update_delta", "unchanged")), LIFECYCLE_ORDER["unchanged"]),
        *_review_tie_break_key(row),
    )


def _history_sort_key(row: dict[str, Any]) -> tuple[int, int, int, str, str]:
    raw_date = str(row.get("_last_seen_date", ""))
    try:
        date_value = date.fromisoformat(raw_date).toordinal()
    except ValueError:
        date_value = 0
    return (-date_value, *_review_tie_break_key(row))


def current_review_order(
    current_candidates: list[dict[str, Any]],
    candidate_history: list[dict[str, Any]],
) -> list[str]:
    """Identity keys of the current candidates in the order the review shows them.

    This is the same exclusion filter, row model, and sort key that
    ``assemble_cumulative_review`` applies to the current section, then the
    review's date order (#1057): newest added date first, that page order
    inside a date, an archive block kept together. It serves consumers that
    need the displayed order without rendering.
    """
    history_by_identity = {
        str(record.get("identity_key", "")).strip(): record
        for record in candidate_history
        if isinstance(record, dict) and str(record.get("identity_key", "")).strip()
    }
    shown = [
        candidate
        for candidate in current_candidates
        if not ScoutingCandidate.from_mapping(candidate).exclusion_reason()
    ]
    rows = _build_review_rows(shown, {}, history_by_identity)
    rows.sort(key=_current_sort_key)
    return [str(row.get("identity_key", "")) for row in reading_order(rows)]


def assemble_cumulative_review(
    current_candidates: list[dict[str, Any]],
    candidate_history: list[dict[str, Any]],
    current_editor_rows: list[dict[str, str]],
    *,
    hidden_by_rule_count: int = 0,
    visibility_records: list[dict[str, Any]] | None = None,
    suppressed_candidates: list[dict[str, Any]] | None = None,
    current_exclusions: list[dict[str, Any]] | None = None,
    excluded_candidate_history: list[dict[str, Any]] | None = None,
    grounded_description_stage: Any | None = None,
    retained_candidate_ids: list[str] | None = None,
    hard_rules: list[dict[str, Any]] | None = None,
    withdrawn_sources: frozenset[str] | None = None,
    fit_scores: FitScores | None = None,
    order_by_fit: bool = False,
    trial_sources: Mapping[str, TrialSource] | None = None,
    decided_editions_of: Callable[[Mapping[str, Any]], Sequence[Mapping[str, Any]]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Project current evidence and history into one deterministic review.

    Current candidates win factual fields. Historical rows are reconstructed
    from their latest snapshot, so rendering never reads obsolete weekly files.
    The returned rows are render-neutral and include the stable CSV row under
    ``editor_row`` for all editor-facing representations.

    A row that an active Hard Rule in *hard_rules* matches, or whose every
    source is in *withdrawn_sources*, is left out of the rows and listed
    under ``review_hidden_cards`` in the summary (#1025).

    Each row carries the recorded fit score of its book under ``fit_score``,
    ``None`` when *fit_scores* has none for it. With *order_by_fit* the rows
    are listed by that score, highest first, and the unscored rows follow in
    the order they have without it; the renderers group the rows by date, so
    the books of one date are then in fit order (#1105).

    Each row names the Probation Sources in *trial_sources* that report its
    book under ``trial_sources``.

    *decided_editions_of* gives the linked records of a row's book that carry
    an editor decision. An undecided row whose linked decisions all settle the
    book is left out and listed under ``review_hidden_cards`` as well.
    """
    # Each rule is prepared once for the whole review, not once per card (#1165).
    prepared_rules = prepare_rules(hard_rules or [])
    withdrawn_sources = withdrawn_sources or frozenset()
    review_hidden: dict[str, dict[str, Any]] = {}
    id_to_row = {str(row.get("Candidate ID", "")): row for row in current_editor_rows}
    history_by_identity = {
        str(record.get("identity_key", "")).strip(): record
        for record in candidate_history
        if isinstance(record, dict) and str(record.get("identity_key", "")).strip()
    }
    # A current artifact can predate a newly added source-eligibility policy.
    # Apply the same canonical exclusion predicate at render time so the
    # editor does not keep seeing already-known non-book or fixture records
    # until the next full collection refresh.
    current_candidates = [
        candidate
        for candidate in current_candidates
        if not ScoutingCandidate.from_mapping(candidate).exclusion_reason()
    ]
    current_ids = {str(candidate.get("identity_key", "")) for candidate in current_candidates}
    grounded_stage = stage_outcome_map(grounded_description_stage)
    rows: list[dict[str, Any]] = _build_review_rows(current_candidates, id_to_row, history_by_identity, grounded_stage)
    rows.sort(key=_current_sort_key)
    if prepared_rules or withdrawn_sources:
        for current in current_candidates:
            current_key = str(current.get("identity_key", ""))
            stored_record = history_by_identity.get(current_key.strip(), {})
            stored_status = stored_record.get("editor_status", "")
            hidden_record = review_hidden_record(
                # The history record knows sources the candidate does not: those
                # of earlier runs, and those a merge or a backfill added to it.
                {
                    **current,
                    EARLIER_SOURCE_NAMES_FIELD: sorted(
                        {str(name) for name in stored_record.get(EARLIER_SOURCE_NAMES_FIELD) or []}
                        | evidence_source_names(stored_record.get("source_evidence"))
                    ),
                },
                prepared_rules,
                withdrawn_sources,
                editor_status=str(stored_status or current.get("editor_status", "")),
            )
            if hidden_record is not None:
                review_hidden[current_key] = {**hidden_record, "identity_key": current_key}

    suppressed_by_identity = {
        str(record.get("identity_key", "")).strip(): record
        for record in (suppressed_candidates or [])
        if isinstance(record, dict) and str(record.get("identity_key", "")).strip()
    }
    exclusion_records_by_identity: dict[str, list[dict[str, Any]]] = {}
    for records in (excluded_candidate_history or [], current_exclusions or []):
        for record in records:
            if isinstance(record, dict) and str(record.get("identity_key", "")).strip():
                identity_key = str(record["identity_key"]).strip()
                exclusion_records_by_identity.setdefault(identity_key, []).append(record)
    excluded_by_identity = {
        identity_key: merge_exclusion_records(records)
        for identity_key, records in exclusion_records_by_identity.items()
    }

    historical_rows: list[dict[str, Any]] = []
    historical_ids = list(history_by_identity)
    for identity_key in historical_ids:
        if identity_key in current_ids:
            continue
        record = history_by_identity.get(identity_key, {})
        if historical_record_source_eligibility_exclusion_reason(record):
            # Legacy invalid records remain in Candidate History.  This is a
            # read-only editor-review projection filter.
            continue
        exclusion = excluded_by_identity.get(identity_key)
        candidate_data = deepcopy(record)
        if exclusion:
            for field_name in ("title", "primary_authors", "exclusion_reason", "exclusion_date"):
                if exclusion.get(field_name):
                    candidate_data[field_name] = deepcopy(exclusion[field_name])
            if not is_found_in_russian(exclusion.get("exclusion_reason", "")) and exclusion.get("russian_edition_url"):
                candidate_data["russian_edition_url"] = deepcopy(exclusion["russian_edition_url"])
            if exclusion.get("evidence"):
                candidate_data["source_evidence"] = _merge_evidence_records(
                    candidate_data.get("source_evidence", []), exclusion["evidence"]
                )
            if is_found_in_russian(exclusion.get("exclusion_reason", "")):
                current_verification = exclusion.get("russian_edition_verification")
                restore_russian_edition_from_exclusion(candidate_data, exclusion)
                # Keep the latest incomplete operational result visible while
                # restoration uses the retained verified positive provenance.
                if (
                    isinstance(current_verification, dict)
                    and is_valid_russian_edition_verification(current_verification)
                    and is_found_in_russian(candidate_data.get("russian_edition_status", ""))
                ):
                    candidate_data["russian_edition_verification"] = deepcopy(current_verification)
            elif isinstance(exclusion.get("russian_edition_verification"), dict):
                candidate_data["russian_edition_verification"] = deepcopy(exclusion["russian_edition_verification"])
            if "russian_edition_note" in exclusion:
                candidate_data["russian_edition_note"] = deepcopy(exclusion["russian_edition_note"])
        candidate = _safe_history_candidate(candidate_data)
        review_contexts: list[str] = []
        rule_id = str(record.get("hidden_by_rule_id", "")).strip()
        rule_reason = str(record.get("hidden_by_rule_reason", "")).strip()
        if rule_id:
            rule_date = str(record.get("last_hidden_by_rule_date", "")).strip()
            applied = f"Hard Rule {rule_id} applied"
            if rule_date:
                applied += f" on {rule_date}"
            review_contexts.append(f"{applied}: {rule_reason}".strip())
        editor_status = str(record.get("editor_status", candidate.get("editor_status", ""))).strip()
        suppressed = suppressed_by_identity.get(identity_key)
        if suppressed:
            editor_status = str(suppressed.get("editor_status", editor_status)).strip()
            review_contexts.append(
                f"Suppressed from current recommendations: editor status '{editor_status}'"
                f" ({str(suppressed.get('suppression_reason', '')).strip()})."
            )
        elif editor_status in {"reject", "already_known"}:
            review_contexts.append(f"Previously suppressed by editor status '{editor_status}'.")
        review_context = "; ".join(review_contexts)
        if prepared_rules or withdrawn_sources:
            hidden_record = review_hidden_record(
                candidate, prepared_rules, withdrawn_sources, editor_status=editor_status
            )
            if hidden_record is not None:
                review_hidden[identity_key] = {**hidden_record, "identity_key": identity_key}
        if exclusion:
            exclusion_reason = str(exclusion.get("exclusion_reason", "")).strip()
            if exclusion_reason:
                exclusion_context = f"Excluded from acquisition: {exclusion_reason}"
                exclusion_date = str(exclusion.get("exclusion_date", "")).strip()
                if exclusion_date:
                    exclusion_context += f" on {exclusion_date}"
                if is_found_in_russian(exclusion_reason):
                    russian_url = str(exclusion.get("russian_edition_url", "")).strip()
                    if russian_url:
                        exclusion_context += f"; evidence: {russian_url}"
                exclusion_context += "."
                review_contexts.append(exclusion_context)
                review_context = "; ".join(review_contexts)
        editor_source_row = editor_row_from_candidate(candidate)
        historical_row = _render_neutral_row(
            candidate,
            editor_source_row,
            record,
            update_delta="previously_seen",
            review_context=review_context,
        )
        historical_row["_last_seen_date"] = str(
            (exclusion or {}).get("exclusion_date", "") or record.get("last_seen_date", "")
        )
        historical_rows.append(historical_row)
    historical_rows.sort(key=_history_sort_key)
    rows.extend(historical_rows)

    # Weekly activity describes all current-run evidence, including an
    # explicitly hidden current candidate. Visibility only changes the
    # cumulative editor-facing projection below.
    delta_counts = {"new": 0, "upgraded": 0, "changed": 0, "unchanged": 0}
    for row in rows:
        if row["identity_key"] not in current_ids:
            continue
        delta = str(row.get("update_delta", "unchanged"))
        if delta in delta_counts:
            delta_counts[delta] += 1

    hidden_ids = {
        str(record.get("identity_key", "")).strip()
        for record in (visibility_records or [])
        if isinstance(record, dict)
        and str(record.get("identity_key", "")).strip()
        and str(record.get("visibility", "")).strip().lower() == "hidden"
    }
    hidden_count = sum(1 for row in rows if row["identity_key"] in hidden_ids)
    rows = [row for row in rows if row["identity_key"] not in hidden_ids]
    if decided_editions_of is not None:
        for row in rows:
            if row["identity_key"] in review_hidden:
                continue
            decided_record = decided_edition_hidden_record(row, decided_editions_of(row))
            if decided_record is not None:
                review_hidden[row["identity_key"]] = decided_record
    # The editor's own hide comes first, so each withheld card is counted once.
    review_hidden_cards = [
        {
            **review_hidden[row["identity_key"]],
            "candidate_id": row["candidate_id"],
            "editor_status": row["editor_status"],
        }
        for row in rows
        if row["identity_key"] in review_hidden
    ]
    rows = [row for row in rows if row["identity_key"] not in review_hidden]
    # The archive block is contiguous in every rendering: its rows follow the
    # new and updated books of this run, one batch after another, and keep
    # their order inside a batch.
    archive_block = sorted((row for row in rows if row["archive_pending"]), key=lambda row: row["archive_batch"])
    if archive_block:
        others = [row for row in rows if not row["archive_pending"]]
        lead = sum(
            1
            for row in others
            if row["identity_key"] in current_ids and row.get("update_delta") in {"new", "upgraded", "changed"}
        )
        rows = [*others[:lead], *archive_block, *others[lead:]]
    # Short IDs are unique across all of Candidate History and the IDs a
    # merge retired, not only the rows shown, so a short ID copied from any
    # review names one identity (#851).
    short_ids = short_candidate_ids(
        [str(record.get("candidate_id", "")).strip() for record in candidate_history if isinstance(record, dict)]
        + [row["candidate_id"] for row in rows]
        + list(retained_candidate_ids or [])
    )
    for review_row in rows:
        review_row["fit_score"] = fit_scores.score_for(review_row) if fit_scores else None
    # Only a review with a recorded score is reordered, the one that says so in its header.
    if order_by_fit and any(row["fit_score"] is not None for row in rows):
        # A stable sort: books with equal scores, and the unscored ones, keep their order.
        # Priority D books follow every other book: the score knows nothing of their content risk.
        rows.sort(
            key=lambda row: (
                str(row.get("priority", "")).strip().upper() == "D",
                row["fit_score"] is None,
                -(row["fit_score"] or 0.0),
            )
        )
    for idx, review_row in enumerate(rows, start=1):
        review_row["idx"] = idx
        review_row["editor_row"]["Candidate ID"] = review_row["identity_key"]
        review_row["editor_row"]["Durable Candidate ID"] = review_row["candidate_id"]
        review_row["short_candidate_id"] = short_ids.get(review_row["candidate_id"], "")

    visible_delta_counts = {"new": 0, "upgraded": 0, "changed": 0, "unchanged": 0}
    # A row waiting in an archive block is counted there and in no lifecycle
    # group, so the groups and the blocks add up to the visible total.
    for row in rows:
        if row["identity_key"] not in current_ids or row["archive_pending"]:
            continue
        delta = str(row.get("update_delta", "unchanged"))
        if delta in visible_delta_counts:
            visible_delta_counts[delta] += 1
    visible_historical_count = sum(
        1 for row in rows if row["identity_key"] not in current_ids and not row["archive_pending"]
    )
    archive_batches = archive_batch_counts(rows)
    counts = {
        "new_count": visible_delta_counts["new"],
        "updated_count": visible_delta_counts["upgraded"] + visible_delta_counts["changed"],
        "current_unchanged_count": visible_delta_counts["unchanged"],
        "previously_seen_count": visible_historical_count,
        "visible_total_count": len(rows),
        "editor_hidden_count": hidden_count,
        "rule_hidden_count": sum(card["hidden_by"] == HIDDEN_BY_HARD_RULE for card in review_hidden_cards),
        "decided_edition_hidden_count": sum(
            card["hidden_by"] == HIDDEN_BY_DECIDED_EDITION for card in review_hidden_cards
        ),
        "retired_source_hidden_count": sum(
            card["hidden_by"] == HIDDEN_BY_RETIRED_SOURCE for card in review_hidden_cards
        ),
    }
    if archive_batches:
        # Present only when the review shows a book from an archive backfill,
        # so a review without one keeps its exact counts.
        counts["archive_pending_count"] = len(archive_block)
        counts["archive_batches"] = archive_batches
    description_stage_counts = {
        OUTCOME_GROUNDED: sum(row.get("description_stage_outcome") == OUTCOME_GROUNDED for row in rows),
        OUTCOME_SINGLE_SOURCE: sum(row.get("description_stage_outcome") == OUTCOME_SINGLE_SOURCE for row in rows),
        OUTCOME_ATTEMPTED_NO_DESCRIPTION: sum(
            row.get("description_stage_outcome") == OUTCOME_ATTEMPTED_NO_DESCRIPTION for row in rows
        ),
        OUTCOME_NOT_ATTEMPTED: sum(row.get("description_stage_outcome") == OUTCOME_NOT_ATTEMPTED for row in rows),
    }
    verification_rows = [
        {
            "russian_edition_verification": row.get("russian_verification", {}),
            "russian_edition_status": row.get("russian", ""),
        }
        for row in rows
        if isinstance(row.get("russian_verification"), dict) and row.get("russian_verification")
    ]
    russian_breakdown = russian_verification_counts(verification_rows) if verification_rows else {}
    summary = {
        **counts,
        "delta_counts": delta_counts,
        "hidden_by_rule_count": hidden_by_rule_count,
        "review_hidden_cards": review_hidden_cards,
        "current_run_count": len(current_candidates),
        "description_stage_counts": description_stage_counts,
        "description_coverage": description_coverage(
            [
                {
                    "identity_key": row["identity_key"],
                    "content_description": row["content_description"],
                    "content_description_rejection_reason": row["content_description_rejection_reason"],
                    "source_evidence": [{"source_name": name} for name in row["source_names"]],
                    # The row's own status is what the editor reads, so the
                    # unsourced count follows it exactly (issue #788).
                    "description_provenance": (
                        {"provenance": "codex-generated"}
                        if row["description_status"] == DESCRIPTION_UNCONFIRMED_STATUS
                        # A logline description has its own line (issue #950).
                        else {"provenance": DESCRIPTION_PM_LOGLINE_PROVENANCE}
                        if row["description_status"] == DESCRIPTION_PM_LOGLINE_STATUS
                        else {}
                    ),
                    # The same rule for a book that is not yet published
                    # (issue #791): the count follows the status the row shows.
                    DESCRIPTION_NOT_YET_PUBLISHED_DATE_FIELD: (
                        date.min.isoformat()
                        if row["description_status"] == DESCRIPTION_NOT_YET_PUBLISHED_STATUS
                        else ""
                    ),
                }
                for row in rows
            ]
        ),
    }
    if russian_breakdown:
        summary["russian_verification_breakdown"] = russian_breakdown
    if rows:
        summary["visible_risk_screen_coverage_breakdown"] = visible_risk_coverage_breakdown(rows)
    for row in rows:
        row.update(row_trial_fields(row.get("source_names") or [], trial_sources or {}))
    return rows, summary


def archive_batch_counts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per archive batch among *rows*: how many of its books wait for a decision and how many have one.

    Batches are listed oldest first by the day they were applied. A batch
    applied over several days is listed under its first day.
    """
    batches: dict[str, dict[str, Any]] = {}
    for row in rows:
        batch = str(row.get("archive_batch", "")).strip()
        if not batch:
            continue
        entry = batches.setdefault(
            batch,
            {
                "batch": batch,
                "label": str(row.get("archive_batch_label", "")),
                "applied_date": str(row.get("archive_applied_date", "")),
                "pending_count": 0,
                "decided_count": 0,
            },
        )
        entry["applied_date"] = min(entry["applied_date"], str(row.get("archive_applied_date", "")))
        entry["pending_count" if row.get("archive_pending") else "decided_count"] += 1
    return sorted(batches.values(), key=lambda entry: (entry["applied_date"], entry["batch"]))


def archive_block_text(entry: dict[str, Any]) -> str:
    """One Russian sentence saying how many books of an archive batch wait for a decision."""
    total = int(entry["pending_count"]) + int(entry["decided_count"])
    return (
        f"без решения — {entry['pending_count']} из {total} видимых книг архива, "
        f"добавлены {entry['applied_date']}. {ARCHIVE_DECIDED_SENTENCE}"
    )


def review_withheld_line(counts: dict[str, Any]) -> str:
    """Say how many cards Hard Rules, retired sources and decisions on other editions keep out of the review.

    Empty when nothing is withheld, so a review without such cards reads as before.
    """
    rule_hidden = int(counts.get("rule_hidden_count", 0) or 0)
    retired_hidden = int(counts.get("retired_source_hidden_count", 0) or 0)
    edition_hidden = int(counts.get("decided_edition_hidden_count", 0) or 0)
    if not rule_hidden and not retired_hidden and not edition_hidden:
        return ""
    line = f"правилами редактора — {rule_hidden}, из отключённых источников — {retired_hidden}"
    if edition_hidden:
        line += f", {DECIDED_EDITION_HIDDEN_LABEL} — {edition_hidden}"
    return line + "."


def _alias_table_or_empty(store: StateStore) -> dict[str, Any]:
    """The alias table only widens the short-ID namespace; a review still renders without it."""
    try:
        alias_table: dict[str, Any] = store.read(Collection.CANDIDATE_IDENTITY_ALIASES)
    except ValueError:
        return empty_alias_table()
    return alias_table


def _record_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def _is_inherited_unrun_verification(verification: dict[str, Any], review_date: str) -> bool:
    """True for a verification in which no provider ran, made before this run.

    A record with no readable date, or dated on or after the run date, is
    the run's own and keeps its provider diagnostics (#616, #1181).
    """
    if not is_unrun_provider_verification(verification):
        return False
    checked_on = _record_date(verification.get("checked_at") or verification.get("query_timestamp"))
    run_on = _record_date(review_date)
    return checked_on is not None and run_on is not None and checked_on < run_on


def _russian_edition_markdown_cell(row: dict[str, Any]) -> str:
    """The Russian-edition mark followed by a Markdown link to its evidence (#1181).

    The record's text is shown literally in a Markdown preview: ``&``, ``<``
    and ``>`` become HTML entities, and the characters that change meaning in
    a table cell line (backslash, backtick, ``*``, ``_`` and brackets) are
    backslash-escaped. The URL is percent-encoded where Markdown would end it
    early. The table cell escaping (``|``, newlines) is applied by the caller.
    """
    text, url, label = format_russian_edition(row)
    if not text:
        return ""
    entities = "".join(_MARKDOWN_TEXT_ENTITIES.get(char, char) for char in text)
    escaped_text = re.sub(r"([\\`*_\[\]])", r"\\\1", entities)
    safe_url = "".join(_MARKDOWN_URL_ESCAPES.get(char, char) for char in url)
    return f"{escaped_text} [{label}]({safe_url})"


_MARKDOWN_TEXT_ENTITIES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
_MARKDOWN_URL_ESCAPES = {" ": "%20", "(": "%28", ")": "%29", "<": "%3C", ">": "%3E", "|": "%7C", "\\": "%5C"}


def _markdown_review_table(rows: list[dict[str, Any]], review_date: str) -> list[str]:
    """The review's candidate table, its rows in reading order: by date added, newest first (#1057).

    The «Обновлено» column holds the change date of a book carrying the updated
    mark, or «обновлено» when it has none. An «Архив» column names the block of a book that waits in an archive
    backfill, only when the review shows one.
    """
    # One provenance column beside the one status column, only when a row
    # carries a provenance line. Runs without verified descriptions keep
    # their exact table shape (issue #644).
    #
    # The header and the delimiter row are built from one column list. They
    # were two hand-aligned string literals, and splitting them to insert a
    # column left the delimiter one cell wider than the header in both
    # states. GFM does not recognise a table whose delimiter row does not
    # match its header, so the whole candidate table rendered as raw pipe
    # text (issue #644 review). One list cannot fall out of step with
    # itself.
    show_provenance_column = any(str(row.get("description_provenance_label", "")).strip() for row in rows)
    # Same conditional-column contract for the mechanical depth marker:
    # runs with no below-floor description keep their exact table shape.
    show_depth_column = any(str(row.get("description_depth_status", "")).strip() for row in rows)
    # And for a rights deal (#945): runs without one keep their exact table shape.
    show_rights_deal_column = any(str(row.get("rights_deal", "")).strip() for row in rows)
    show_original_column = any(format_original_edition(row)[0] for row in rows)
    show_russian_finding_column = any(format_russian_edition(row)[0] for row in rows)
    show_earlier_column = any(row["earlier_edition_decisions"] for row in rows)
    show_archive_column = any(row["archive_pending"] for row in rows)
    # And for the fit score (#1105): a review with no recorded score keeps its exact table shape.
    show_fit_column = any(row["fit_score"] is not None for row in rows)
    review_columns = [
        "#",
        "ID",
        "Название",
        "Автор",
        "Добавлено",
        "Обновлено",
        *(["Архив"] if show_archive_column else []),
        "Контекст",
        *(["Оригинал"] if show_original_column else []),
        *(["Поиск русского издания"] if show_russian_finding_column else []),
        *(["Другое издание"] if show_earlier_column else []),
        *(["Сделка о правах"] if show_rights_deal_column else []),
        "Приоритет",
        *([FIT_SCORE_COLUMN] if show_fit_column else []),
        "Сигналы",
        "Русское издание",
        "Риск",
        "Проверка риска",
        "Комментарий о риске",
        "Статус описания",
        *(["Глубина описания"] if show_depth_column else []),
        *(["Источники описания"] if show_provenance_column else []),
        "Причина описания",
        "Решение редактора",
        "Источник",
        "Заметка редактора",
    ]
    lines = [
        "",
        "| " + " | ".join(review_columns) + " |",
        "|" + "|".join("-" * (len(name) + 2) for name in review_columns) + "|",
    ]
    for row in reading_order(rows):
        provenance_cell = (row["description_provenance_display"],) if show_provenance_column else ()
        depth_cell = (row["description_depth_status"],) if show_depth_column else ()
        rights_deal_cell = (row["rights_deal"],) if show_rights_deal_column else ()
        # A book waiting in an archive block is named by its block.
        archive_cell = (row["archive_batch_label"] if row["archive_pending"] else "",) if show_archive_column else ()
        lines.append(
            "| "
            + " | ".join(
                markdown_table_cell(value)
                for value in (
                    row["idx"],
                    row["short_candidate_id"],
                    str(row["title"])[:60],
                    str(row["authors"])[:40],
                    added_date(row),
                    updated_cell_text(row, review_date),
                    *archive_cell,
                    presentation_review_context(row["review_context"]),
                    *((format_original_edition(row)[0],) if show_original_column else ()),
                    *((_russian_edition_markdown_cell(row),) if show_russian_finding_column else ()),
                    *(("; ".join(row["earlier_edition_decisions"]),) if show_earlier_column else ()),
                    *rights_deal_cell,
                    row["priority"],
                    *((fit_score_text(row["fit_score"]),) if show_fit_column else ()),
                    row["signal_count"],
                    russian_status_label(row["russian"]),
                    risk_status_label(row["risk"]),
                    visible_risk_coverage_review_text(row[VISIBLE_RISK_SCREEN_COVERAGE_FIELD]),
                    row["risk_comment"],
                    row["description_status"],
                    *depth_cell,
                    *provenance_cell,
                    row["description_stage_reason"],
                    editor_status_label(row["editor_status"]),
                    f"{row['source']} ({row['source_url']})"
                    if row["source"] and row["source_url"]
                    else row["source"] or row["source_url"],
                    row["editor_note"],
                )
            )
            + " |"
        )
    return lines


def render_review(
    output_dir: Path,
    state_dir: Path | None = None,
    effective_date: str | None = None,
    config_path: Path | None = None,
    *,
    state_store: StateStore | None = None,
) -> int:
    """Render the cumulative review from the run artifacts and the state.

    *config_path* names the weekly configuration whose retired sources
    withdraw their cards (#1025). Without it no source is treated as
    withdrawn; the active Hard Rules apply either way.

    Canonical state is read through *state_store*, the run's store for
    *state_dir*, or a store opened here with the backend *config_path*
    chooses. Without a state directory the output directory stands in for
    it, as it always has: Candidate History is the run's copy, and the other
    collections are read through a store over the output directory.
    """
    report_store = WeeklyReportStore(output_dir)
    snapshot = report_store.snapshot("renderable", effective_date)
    if snapshot.transaction_in_progress or snapshot.read_inconsistent:
        print("error=weekly_report_transaction_in_progress")
        return 1
    if "scouting-candidates.json" in snapshot.missing:
        print("No scouting-candidates.json found in output directory")
        return 1
    if not snapshot.coherent:
        print("error=weekly_report_snapshot_invalid")
        return 1

    run_summary_value = snapshot.json("run-summary.json", {}) or {}
    run_summary = run_summary_value if isinstance(run_summary_value, dict) else {}
    review_date = snapshot.effective_date or effective_date or date.today().isoformat()
    candidates = snapshot.json("scouting-candidates.json", [])
    editor_rows_value = snapshot.csv("editor-facing.csv", [])
    editor_rows = editor_rows_value if isinstance(editor_rows_value, list) else []
    hidden_by_rule_count = int(run_summary.get("hidden_by_rule_count", 0))
    # The page is what the editor returns to all week, so it has to carry how
    # much of the report is this week's evidence. The spoken summary states it
    # once and is gone (Issue #384).
    source_runs_value = snapshot.json("source-runs.json", None)
    source_statuses: list[str] | None = None
    if isinstance(source_runs_value, list):
        source_run_summary = summarize_source_runs(
            run.get("status", "") for run in source_runs_value if isinstance(run, dict)
        )
        source_statuses = list(source_run_summary.statuses)
        reused_source_count = source_run_summary.stale_source_count
        collecting_source_count = source_run_summary.collecting_source_count
    else:
        # Reports from before source-runs.json was required retain their
        # persisted aggregate fields.
        reused_source_count = int(run_summary.get("reused_artifact_count", 0) or 0)
        collecting_source_count = reused_source_count + int(run_summary.get("freshly_collected_count", 0) or 0)
    suppressed_candidates = snapshot.json("suppressed-candidates.json", [])
    current_exclusions = snapshot.json("internal-exclusions.json", [])
    grounded_description_stage = snapshot.json("grounded-description-stage.json", None)

    if state_store is None:
        selection = select_state_backend_from_file(config_path)
        state_store = (
            open_state_store(state_dir, selection) if state_dir else open_run_output_store(output_dir, selection)
        )
    # Canonical state is authoritative, an empty collection included; only a
    # run without state reads the run's copy of Candidate History.
    history_records = state_store.read(Collection.CANDIDATE_HISTORY) if state_dir else read_run_history(output_dir)
    excluded_history_records = state_store.read(Collection.EXCLUDED_CANDIDATE_HISTORY)
    visibility_records = state_store.read(Collection.CANDIDATE_VISIBILITY)
    if not isinstance(history_records, list):
        history_records = []
    if not isinstance(excluded_history_records, list):
        excluded_history_records = []
    if not isinstance(suppressed_candidates, list):
        suppressed_candidates = []
    if not isinstance(current_exclusions, list):
        current_exclusions = []
    if not isinstance(visibility_records, list):
        visibility_records = []
    alias_table = _alias_table_or_empty(state_store)
    try:
        hard_rules = active_hard_rules(state_store.read(Collection.EDITORIAL_PROFILE))
    except TasteProfileError:
        print("error=editorial_profile_invalid")
        return 1
    withdrawn_sources: frozenset[str] = frozenset()
    trial_sources: dict[str, TrialSource] = {}
    if config_path is not None:
        try:
            withdrawn_sources = withdrawn_source_names_from_config(config_path)
            trial_sources = trial_sources_from_config(config_path)
        except (ValueError, json.JSONDecodeError):
            print("error=review_config_invalid")
            return 1
    order_by_fit = fit_ordering_enabled(config_path)
    # Records of one book under different identities: another edition, or a
    # duplicate. Each side learns the other's decision.
    pairs = probable_duplicate_pairs(history_records, alias_table, load_dismissals(state_dir or output_dir))["pairs"]
    titles_by_identity = {
        str(record.get("identity_key", "")): known_titles(record)
        for record in history_records
        if isinstance(record, dict)
    }
    linked: dict[str, list[dict[str, Any]]] = {}
    for pair in pairs:
        first, second = pair["records"]
        for source, other in ((first, second), (second, first)):
            if not other["editor_status"].strip():
                continue
            wording = "другое издание" if pair["kind"] == "possible_editions" else "эту книгу в другой записи"
            message = f"Уже рассматривали {wording}: «{other['title']}» — {editor_status_label(other['editor_status'])}"
            if other["editor_note"].strip():
                message += f". Заметка: {other['editor_note'].strip()}"
            linked.setdefault(effective_candidate_id(source["candidate_id"], alias_table), []).append(
                {
                    **other,
                    "message": message,
                    "signals": pair["signals"],
                    "linked_titles": (
                        titles_by_identity.get(source["identity_key"], []),
                        titles_by_identity.get(other["identity_key"], []),
                    ),
                }
            )

    def linked_decisions(row: Mapping[str, Any]) -> list[dict[str, Any]]:
        durable_id = effective_candidate_id(row["candidate_id"] or candidate_id_for(row["identity_key"]), alias_table)
        return sorted(linked.get(durable_id, []), key=lambda other: (other["title"], other["identity_key"]))

    rows, summary = assemble_cumulative_review(
        candidates,
        history_records,
        editor_rows,
        hidden_by_rule_count=hidden_by_rule_count,
        visibility_records=visibility_records,
        suppressed_candidates=suppressed_candidates,
        current_exclusions=current_exclusions,
        excluded_candidate_history=excluded_history_records,
        grounded_description_stage=grounded_description_stage,
        retained_candidate_ids=sorted({*alias_table.get("candidates", {}), *alias_table.get("redirects", {})}),
        hard_rules=hard_rules,
        withdrawn_sources=withdrawn_sources,
        fit_scores=read_fit_scores(output_dir, alias_table),
        order_by_fit=order_by_fit,
        trial_sources=trial_sources,
        decided_editions_of=linked_decisions,
    )
    review_hidden_cards = summary.pop("review_hidden_cards")

    # A decided card names the editions withheld because of its decision.
    withheld_editions: dict[str, list[str]] = {}
    for card in review_hidden_cards:
        if card["hidden_by"] != HIDDEN_BY_DECIDED_EDITION:
            continue
        for decided in card["decided_records"]:
            withheld_editions.setdefault(decided["identity_key"], []).append(f"«{card['title']}»")
    for row in rows:
        messages = [other["message"] for other in linked_decisions(row)]
        row["earlier_edition_decisions"] = messages
        row["withheld_editions"] = sorted(withheld_editions.get(row["identity_key"], []))
        row["editor_row"].pop("Earlier edition decision", None)
        if messages:
            row["editor_row"]["Earlier edition decision"] = "; ".join(messages)

    lines = [
        "# Еженедельный обзор AutoScout",
        "",
        f"**Дата обзора**: {review_date}",
        "",
        (
            f"**За эту неделю**: новых — {summary['delta_counts']['new']}, "
            f"улучшенных — {summary['delta_counts']['upgraded']}, "
            f"изменённых — {summary['delta_counts']['changed']}, "
            f"без изменений — {summary['delta_counts']['unchanged']}, "
            f"скрыто правилами — {hidden_by_rule_count}."
        ),
    ]
    has_stale_source = reused_source_count > 0
    for caveat in run_summary.get("description_caveats", []):
        lines.append(str(caveat))
    if has_stale_source:
        lines.append(
            f"**⚠ {reused_source_count} из {collecting_source_count} источников** — "
            "данные предыдущего запуска, а не свежий сбор. "
            "Числа выше относятся не только к этой неделе."
        )
    incomplete_coverages = (
        [
            coverage
            for run in source_runs_value
            if isinstance(run, dict) and (coverage := incomplete_coverage_from_run(run)) is not None
        ]
        if isinstance(source_runs_value, list)
        else []
    )
    for coverage in incomplete_coverages:
        left_out = left_out_titles_text(coverage)
        lines.append(
            f"**⚠ {coverage['source_name']}** — неполный охват рейтинга: "
            f"{coverage['observed_rows']} из {coverage['expected_rows']} строк; "
            + (f"{left_out}; " if left_out else "")
            + "в обзор вошли только доступные строки."
        )
    # The count alone does not say which books are stale or which sources
    # gave nothing this week; the names do (Issue #825).
    capture_ledger, _capture_ledger_status = capture_ledger_from_workflow_log(
        output_dir, str(run_summary.get("operation_id", "")).strip()
    )
    # A stale source is credited with the current-run rows the review shows,
    # after editor-hidden books are removed, so its count never names rows
    # that are not on the page.
    current_identity_keys = {
        str(candidate.get("identity_key", "")) for candidate in candidates if isinstance(candidate, dict)
    }
    visible_rows_by_source: Counter[str] = Counter(
        name
        for row in rows
        if row["identity_key"] in current_identity_keys
        for name in set(row.get("source_names") or [])
    )
    source_problems = source_problem_notes(
        complete_source_runs(capture_ledger, source_runs_value if isinstance(source_runs_value, list) else None),
        review_row_counts=visible_rows_by_source,
    )
    # Issue #765: a source whose row count left its usual range is named here too.
    source_lines = source_problem_lines(source_problems) + departure_review_lines(
        run_summary.get("row_count_departures")
    )
    lines.extend(f"**⚠ Источники**: {line}" for line in source_lines)
    lines.extend(
        [
            f"**Всего в обзоре**: добавлено в последний запуск — {added_on_run_date_count(rows, review_date)}, "
            f"с отметкой «обновлено» — {marked_updated_count(rows, review_date)}, скрытых редактором — {summary['editor_hidden_count']}.",
            *(
                f"**{entry['label']}**: {archive_block_text(entry)}"
                for entry in summary.get("archive_batches", [])
                if entry["pending_count"]
            ),
            "",
            f"**Видимых книг**: {summary['visible_total_count']}.",
        ]
    )
    withheld_line = review_withheld_line(summary)
    if withheld_line:
        lines.append(f"**Скрыто из всего обзора**: {withheld_line}")
    desc_cov = summary.get("description_coverage", {})
    if desc_cov:
        desc_fallback = desc_cov.get("fallback_count", 0)
        # Books awaiting publication are no gap in the N (issue #944).
        desc_total = coverage_denominator(desc_cov)
        # The clause says "of the verified ones", so it reads the
        # sourced-only depth count once coverage carries it (#788).
        desc_below_depth = desc_cov.get("sourced_below_depth_count", desc_cov.get("below_depth_count", 0))
        try:
            desc_below_depth = int(desc_below_depth or 0)
        except (TypeError, ValueError):
            desc_below_depth = 0
        depth_clause = (
            f" Из проверенных краткими остаются — {desc_below_depth} "
            "(порог глубины: от 200 знаков и не менее двух предложений)."
            if desc_below_depth > 0
            else ""
        )
        # Unsourced stubs are not counted as verified (issue #788); coverage
        # written before the split carries no key and reads as before.
        try:
            desc_unsourced = int(desc_cov.get("unsourced_count", 0) or 0)
        except (TypeError, ValueError):
            desc_unsourced = 0
        unsourced_clause = f"не подтверждены источниками — {desc_unsourced}, " if desc_unsourced > 0 else ""
        # Books that are not yet published need nothing from anyone, so
        # they are reported apart from the rows that do (issue #791).
        try:
            desc_unpublished = int(desc_cov.get("not_yet_published_count", 0) or 0)
        except (TypeError, ValueError):
            desc_unpublished = 0
        unpublished_clause = f", ещё не опубликованы — {desc_unpublished}" if desc_unpublished > 0 else ""
        # A logline description is no gap and no verified one (issue #950).
        desc_logline = pm_logline_count(desc_cov)
        logline_clause = f", по логлайну сделки PM — {desc_logline}" if desc_logline > 0 else ""
        lines.append(
            f"**Русские описания**: проверено — {verified_description_count(desc_cov)} из {desc_total}, "
            f"{unsourced_clause}требуют дополнения — {max(0, int(desc_fallback or 0) - desc_unpublished)}"
            f"{unpublished_clause}{logline_clause}.{depth_clause} "
            "Пробелы в описаниях — предупреждение о дополнении данных, а не сбой сбора."
        )
    stage_counts = summary.get("description_stage_counts", {})
    if any(stage_counts.values()):
        lines.append(
            f"**Проверка источников**: подтверждено несколькими источниками - {stage_counts.get(OUTCOME_GROUNDED, 0)}, "
            f"по одному источнику - {stage_counts.get(OUTCOME_SINGLE_SOURCE, 0)}, "
            f"без описания - {stage_counts.get(OUTCOME_ATTEMPTED_NO_DESCRIPTION, 0)}, "
            f"поиск не запускался - {stage_counts.get(OUTCOME_NOT_ATTEMPTED, 0)}."
        )
    russian_breakdown = summary.get("russian_verification_breakdown", {})
    russian_summary_text = russian_verification_summary_text(russian_breakdown)
    if russian_summary_text:
        lines.append(f"**{russian_summary_text}**")
    if russian_breakdown:
        summary["russian_verification_breakdown"] = russian_breakdown
        verification_lines = [
            "",
            "### Операционная проверка русского издания",
            "",
        ]
        for row in rows:
            verification = row.get("russian_verification")
            if isinstance(verification, dict) and _is_inherited_unrun_verification(verification, review_date):
                # A record from an earlier run in which no provider ran: its
                # old diagnostics (an empty fixture) say nothing about the
                # book; the line keeps only "not run", which is what the
                # breakdown counts (#1181). A check configured in this run
                # that did not run still names its provider (#616).
                verification = {**verification, "providers": []}
            verification_text = russian_verification_review_text(verification)
            if verification_text:
                verification_lines.append(
                    f"- {markdown_table_cell(row['title'])} ({markdown_table_cell(row['authors'])}): "
                    f"{markdown_table_cell(verification_text)}"
                )
        lines.extend(verification_lines)
    risk_coverage_text = visible_risk_coverage_summary_text(summary.get("visible_risk_screen_coverage_breakdown"))
    if risk_coverage_text:
        lines.append(f"**Проверка риска**: {risk_coverage_text}")
    if order_by_fit and any(row["fit_score"] is not None for row in rows):
        lines.append(f"**Порядок книг**: {FIT_ORDER_NOTE}")
    lines.extend(_markdown_review_table(rows, review_date))

    lines.extend(
        [
            "",
            "---",
            "",
            "## Что делать редактору",
            "",
            "Напишите решения по книгам в чате: что интересно, что стоит наблюдать, "
            "что не подходит, что уже известно или что требует углублённого анализа.",
        ]
    )

    review_path = report_store.path("weekly-review.md")
    html_summary = {
        "run_date": review_date,
        "delta_counts": summary["delta_counts"],
        "hidden_by_rule_count": hidden_by_rule_count,
        "editor_rows_count": summary["visible_total_count"],
        "total_count": summary["visible_total_count"],
        "cumulative_counts": summary,
        "reused_source_count": reused_source_count,
        "collecting_source_count": collecting_source_count,
        "source_statuses": source_statuses,
        "incomplete_source_coverages": [
            {
                "source_name": str(coverage["source_name"]),
                "observed_rows": int(coverage["observed_rows"]),
                "expected_rows": int(coverage["expected_rows"]),
                **({"left_out_titles": list(coverage["left_out_titles"])} if coverage.get("left_out_titles") else {}),
            }
            for coverage in incomplete_coverages
        ],
        "description_stage_counts": summary.get("description_stage_counts", {}),
        "ordered_by_fit": order_by_fit,
        "source_problem_lines": source_lines,
        "description_caveats": run_summary.get("description_caveats", []),
    }
    if summary.get("russian_verification_breakdown"):
        html_summary["russian_verification_breakdown"] = summary["russian_verification_breakdown"]
    if summary.get("visible_risk_screen_coverage_breakdown"):
        html_summary["visible_risk_screen_coverage_breakdown"] = summary["visible_risk_screen_coverage_breakdown"]
    html_path = report_store.path("weekly-review.html")
    html_value = build_html_review(rows, html_summary)

    desc_cov = summary.get("description_coverage", {})
    with report_store.transaction("render-review"):
        report_store.write_text("weekly-review.md", "\n".join(lines) + "\n")
        report_store.write_editor_csv([row["editor_row"] for row in rows])
        report_store.write_text("weekly-review.html", html_value)
        if desc_cov:
            report_store.write_json("description-coverage.json", desc_cov)
        # Written only when a card is withheld, and rewritten to an empty list
        # once none is, so the file never names cards that are back in the review.
        if review_hidden_cards or report_store.path(HIDDEN_FROM_REVIEW_ARTIFACT).exists():
            report_store.write_json(HIDDEN_FROM_REVIEW_ARTIFACT, review_hidden_cards)
    if review_hidden_cards:
        print(f"review_rule_hidden_count={summary['rule_hidden_count']}")
        print(f"review_retired_source_hidden_count={summary['retired_source_hidden_count']}")
        print(f"review_decided_edition_hidden_count={summary['decided_edition_hidden_count']}")
    if desc_cov:
        print(f"description_coverage={desc_cov.get('coverage', 'unknown')}")
        print(f"description_validated_count={desc_cov.get('validated_count', 0)}")
        print(f"description_fallback_count={desc_cov.get('fallback_count', 0)}")
        for identity_key in desc_cov.get("fallback_ids", []):
            print(f"description_fallback_candidate={identity_key}")
        for source in desc_cov.get("fallback_by_source", []):
            if not isinstance(source, dict):
                continue
            print(
                f"description_fallback_source={source.get('source_name', '')}: "
                f"{source.get('fallback_count', 0)} of {source.get('candidate_count', 0)}"
            )

    print(f"Review rendered to {review_path}")
    print(f"HTML review rendered to {html_path}")
    print(f"Candidates reviewed: {summary['visible_total_count']}")
    return 0
