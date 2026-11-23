"""Deep implementation of the offline, confirmation-gated source onboarding flow."""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Any
from typing import cast

from autoscout import replay_fixtures
from autoscout import safe_artifacts
from autoscout import source_feasibility
from autoscout import source_onboarding_policy as policy
from autoscout import source_onboarding_transaction as transaction
from autoscout import source_proposal_commands as proposal_commands
from autoscout import source_proposals
from autoscout import source_scaffold
from autoscout.source_exit_codes import EXIT_CONFIRMATION
from autoscout.source_exit_codes import EXIT_DEFER
from autoscout.source_exit_codes import EXIT_ERROR
from autoscout.source_exit_codes import EXIT_REJECT
from autoscout.source_exit_codes import EXIT_SUCCESS
from autoscout.source_onboarding_models import ConfigPaths
from autoscout.source_onboarding_models import OnboardingIssue
from autoscout.source_onboarding_models import OnboardingRequest
from autoscout.source_onboarding_transaction import OnboardingRunners
from autoscout.state_store import Collection
from autoscout.state_store import open_state_store

_FAILURE_ACTIONS: dict[tuple[str, str], tuple[str, str]] = {
    ("probe", "url_mismatch"): ("replace_probe_evidence", "Supply retained probe evidence for the requested URL."),
    ("probe", "collection_mode_invalid"): (
        "retain_probe_capture",
        "Retain a source-candidate probe capture and retry.",
    ),
    ("probe", "malformed_evidence"): ("repair_probe_evidence", "Provide a readable JSON probe evidence object."),
    ("probe", "probe_evidence_missing"): (
        "provide_probe_evidence",
        "Provide the accepted retained probe evidence file.",
    ),
    ("probe", source_feasibility.REASON_RETAINED_SUMMARY_INCONSISTENT): (
        "repair_probe_summary",
        "Correct the retained probe summary fields, then repeat the add-source preview.",
    ),
    ("inputs", replay_fixtures.REASON_HELPER_MISSING): (
        "provide_helper_metadata",
        "Provide the retained helper metadata sidecar.",
    ),
    ("inputs", replay_fixtures.REASON_HELPER_UNREADABLE): (
        "repair_helper_metadata_file",
        "Provide a readable version-1 helper metadata JSON object at the supplied path.",
    ),
    ("inputs", replay_fixtures.REASON_HELPER_SCHEMA_INVALID): (
        "repair_helper_metadata_header",
        "Correct the helper sidecar version, source_url, language, and entries fields.",
    ),
    ("inputs", replay_fixtures.REASON_HELPER_ENTRY_INVALID): (
        "repair_helper_metadata_entry",
        "Correct the named helper metadata entry field, then repeat the add-source preview.",
    ),
    ("inputs", replay_fixtures.REASON_HELPER_DUPLICATE_ENTRY): (
        "deduplicate_helper_metadata",
        "Keep exactly one helper entry per representative entry, then repeat the add-source preview.",
    ),
    ("inputs", replay_fixtures.REASON_HELPER_IDENTITY_MISMATCH): (
        "align_helper_metadata",
        (
            "Rewrite the helper sidecar as exactly the reported representative entries, "
            f"in the reported order and at most {replay_fixtures.MAX_REPLAY_ENTRIES} of them."
        ),
    ),
    ("inputs", replay_fixtures.REASON_REPRESENTATIVE_ENTRIES_MISSING): (
        "replace_probe_evidence",
        "Retain probe evidence that exposes at least one representative entry.",
    ),
    ("inputs", "probe_evidence_invalid"): (
        "move_probe_capture",
        "Move the probe capture into an allowed data capture directory.",
    ),
    ("proposal", "reason_required"): ("provide_reason", "Provide a nonblank onboarding reason."),
    ("proposal", "metadata_required"): ("provide_metadata", "Supply every required source metadata field."),
    ("proposal", "invalid_slug"): ("fix_slug", "Use a lowercase filesystem-safe source slug."),
    ("proposal", "confirmation_token_required"): (
        "provide_confirmation_token",
        "Pass the token returned by the matching preview.",
    ),
    ("proposal", "confirmation_payload_mismatch"): (
        "repreview_payload",
        "Re-run preview and confirm its unchanged payload.",
    ),
    ("proposal", "proposal_already_accepted"): (
        "stop_already_onboarded",
        "Use the existing accepted source instead of onboarding it again.",
    ),
    ("proposal", "proposal_not_acceptable"): (
        "prepare_proposed_proposal",
        "Use a proposed Source Proposal that is not rejected or infeasible.",
    ),
    ("proposal", "run_date_required"): ("provide_run_date", "Pass the same explicit ISO run date used for preview."),
    ("feasibility", "feasibility_document_incomplete"): (
        "complete_feasibility",
        "Provide completed feasibility evidence with the issue and source URL.",
    ),
    ("fixtures", "fixture_conflict"): (
        "resolve_fixture_conflict",
        "Remove or replace the conflicting replay fixture after review.",
    ),
    ("scaffold", "scaffold_failed"): (
        "repair_scaffold_inputs",
        "Correct the staged source metadata and replay inputs.",
    ),
    ("replay", "replay_validation_failed"): (
        "repair_replay_fixtures",
        "Correct the replay fixtures before retrying.",
    ),
    ("contracts", "contract_validation_failed"): (
        "repair_source_contracts",
        "Fix the source contract test failure before retrying.",
    ),
    ("validation", "full_validation_failed"): (
        "repair_full_validation",
        "Fix the full validation failure before retrying.",
    ),
    ("persistence", "git_commit_failed"): (
        "repair_git_commit",
        "Restore Git commit access and retry the confirmed payload.",
    ),
    ("persistence", "state_store_refused"): (
        "repair_state_store",
        (
            "Correct the State Store problem the error names, for example export the state database and commit "
            "the export, then repeat the unchanged confirmed operation."
        ),
    ),
    ("persistence", "state_database_commit_failed"): (
        "rebuild_state_database",
        (
            "The onboarding commit is made. Rebuild the state database from the committed JSON with "
            "./scripts/state-database rebuild --overwrite; do not repeat the onboarding."
        ),
    ),
    ("persistence", "unrelated_dirty_files"): (
        "clear_unrelated_changes",
        (
            "Clear unrelated working-tree changes before confirmation. "
            "Declare a pin under tests/ that this onboarding owns with --onboarding-artifact, then repeat the preview."
        ),
    ),
    ("persistence", "onboarding_artifact_not_dirty"): (
        "drop_unchanged_onboarding_artifact",
        "Remove the unchanged --onboarding-artifact or make its change, then repeat the preview.",
    ),
    ("inputs", "onboarding_artifact_invalid"): (
        "fix_onboarding_artifact",
        "Declare each onboarding artifact once as an existing regular file under tests/ that is no other onboarding input.",
    ),
    ("persistence", "canonical_destination_dirty"): (
        "clear_canonical_changes",
        "Commit or clear changes to canonical onboarding destinations.",
    ),
}
_STAGE_FAILURE_ACTIONS: dict[str, tuple[str, str]] = {
    "probe": ("repair_probe_failure", "Correct retained probe evidence, then repeat the add-source preview."),
    "inputs": ("repair_input_failure", "Correct the named onboarding input, then repeat the add-source preview."),
    "proposal": (
        "repair_proposal_failure",
        "Correct the proposed request, then repeat preview and confirm the unchanged payload.",
    ),
    "feasibility": (
        "repair_feasibility_failure",
        "Complete the feasibility evidence, then repeat the add-source preview.",
    ),
    "fixtures": (
        "repair_fixture_failure",
        "Correct the replay fixture inputs, then repeat the unchanged confirmed operation.",
    ),
    "scaffold": (
        "repair_scaffold_failure",
        "Correct the staged source configuration inputs, then repeat the unchanged confirmed operation.",
    ),
    "replay": (
        "repair_replay_failure",
        "Correct the replay validation inputs, then repeat the unchanged confirmed operation.",
    ),
    "contracts": (
        "repair_contract_failure",
        "Fix the source contract tests, then repeat the unchanged confirmed operation.",
    ),
    "validation": (
        "repair_validation_failure",
        "Correct the full validation failure, then repeat the unchanged confirmed operation.",
    ),
    "persistence": (
        "repair_persistence_failure",
        "Correct the Git persistence problem, then repeat the unchanged confirmed operation.",
    ),
    "onboarding": (
        "inspect_unexpected_onboarding_failure",
        "Correct the unexpected onboarding failure, then repeat the same unchanged command.",
    ),
}
# Report fields that name the failure itself. Correction details may add to a
# failure report but must never replace these.
_RESERVED_REPORT_KEYS = frozenset(
    {
        "version",
        "workflow",
        "stage",
        "outcome",
        "exit_code",
        "next_action",
        "next_action_code",
        "reason",
        "reason_code",
        "error",
        "stages",
    }
)
_DEFAULT_FAILURE_ACTION = (
    "inspect_unexpected_failure",
    "Inspect the bounded unexpected failure, then repeat the same unchanged command.",
)


@dataclass(frozen=True)
class ProbeControl:
    exit_code: int
    next_action: str
    next_action_code: str


PROBE_CONTROLS: dict[str, ProbeControl] = {
    "accept": ProbeControl(
        EXIT_SUCCESS,
        "Continue with the accepted Source Proposal preview.",
        "continue_preview",
    ),
    "defer": ProbeControl(EXIT_DEFER, "Record the deferred source and stop onboarding.", "record_defer"),
    "reject": ProbeControl(EXIT_REJECT, "Record the rejected source and stop onboarding.", "record_reject"),
}
_PROBE_ERROR_CONTROL = ProbeControl(EXIT_ERROR, "Repair retained probe evidence and retry.", "repair_probe_failure")


@dataclass(frozen=True)
class RetainedProbe:
    evidence_path: Path
    evidence: Mapping[str, Any]
    verdict: source_feasibility.FeasibilityVerdict
    control: ProbeControl


@dataclass(frozen=True)
class PreparedRequest:
    root: Path
    source_url: str
    evidence_path: Path
    helper_input: Path
    feasibility_input: Path
    feasibility_bytes: bytes
    input_paths: Mapping[str, Path]
    canonical: policy.CanonicalPaths
    context: policy.OnboardingContext
    request: Mapping[str, Any]
    proposal: Mapping[str, Any]
    prepared_proposals: tuple[dict[str, Any], ...]
    representative_entries: tuple[source_feasibility.EntrySample, ...]


def _stage_result(
    name: str,
    outcome: str,
    exit_code: int,
    next_action: str,
    *,
    next_action_code: str = "",
    **details: Any,
) -> dict[str, Any]:
    if not next_action.strip():
        raise ValueError("stage next_action must be nonblank")
    result: dict[str, Any] = {
        "stage": name,
        "outcome": outcome,
        "exit_code": exit_code,
        "next_action": next_action,
    }
    if next_action_code:
        result["next_action_code"] = next_action_code
    result.update(details)
    return result


def _report(
    stages: list[dict[str, Any]], current: Mapping[str, Any], *, confirmation_required: bool = False, **details: Any
) -> tuple[dict[str, Any], int]:
    report: dict[str, Any] = {
        "version": 1,
        "workflow": "add-source",
        "stage": current["stage"],
        "outcome": current["outcome"],
        "exit_code": current["exit_code"],
        "next_action": current["next_action"],
        "stages": stages,
    }
    if confirmation_required:
        report["confirmation_required"] = True
    report.update(details)
    return report, int(current["exit_code"])


def failure_report(
    stages: list[dict[str, Any]],
    stage: str,
    reason_code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    next_action_code, next_action = _FAILURE_ACTIONS.get(
        (stage, reason_code), _STAGE_FAILURE_ACTIONS.get(stage, _DEFAULT_FAILURE_ACTION)
    )
    # Correction evidence never overwrites the reported failure identity: a
    # reserved key would otherwise shadow the stage, reason, or next action a
    # small model reads to decide what to do next.
    failure_details = {name: value for name, value in dict(details or {}).items() if name not in _RESERVED_REPORT_KEYS}
    current = _stage_result(
        stage,
        "failed",
        EXIT_ERROR,
        next_action,
        next_action_code=next_action_code,
        reason_code=reason_code,
        reason=message,
        **failure_details,
    )
    stages.append(current)
    return _report(
        stages,
        current,
        error=message,
        reason_code=reason_code,
        reason=message,
        next_action_code=next_action_code,
        **failure_details,
    )


def _evaluate_retained_probe(url: str | None, evidence_path: Path | None) -> RetainedProbe:
    path = policy.validate_request_inputs(url=url, probe_evidence=evidence_path)
    assert url is not None
    evidence = policy.load_mapping(path, "probe evidence")
    evidence_url = str(evidence.get("source_url") or evidence.get("source_page") or "").strip()
    if source_proposals.normalize_source_identity(evidence_url) != source_proposals.normalize_source_identity(url):
        raise policy.SourceOnboardingError("probe", "url_mismatch", "retained probe evidence is for a different URL")
    if evidence.get("collection_mode") != replay_fixtures.PROBE_COLLECTION_MODE:
        raise policy.SourceOnboardingError(
            "probe",
            "collection_mode_invalid",
            f"retained probe evidence collection_mode must be {replay_fixtures.PROBE_COLLECTION_MODE}",
        )
    contract_metadata = evidence.get("contract_metadata")
    source_metadata = dict(contract_metadata) if isinstance(contract_metadata, Mapping) else {}
    source_metadata.update(
        {
            "url": url,
            "contract_family": str(evidence.get("contract_family", "")).strip(),
            "visible_title_selector": str(
                evidence.get("visible_title_selector") or source_metadata.get("visible_title_selector", "")
            ),
            "entry_scope_selector": str(
                evidence.get("entry_scope_selector") or source_metadata.get("entry_scope_selector", "")
            ),
        }
    )
    verdict = source_feasibility.evaluate_source_feasibility(source_metadata, evidence)
    control = PROBE_CONTROLS.get(str(verdict.outcome), _PROBE_ERROR_CONTROL)
    if verdict.outcome == source_feasibility.OUTCOME_ACCEPT:
        summary_error = source_feasibility.validate_retained_summary(evidence, verdict)
        if summary_error is not None:
            reason_code, reason = summary_error
            raise policy.SourceOnboardingError("probe", reason_code, reason)
    return RetainedProbe(path, evidence, verdict, control)


def _probe_stage(probe: RetainedProbe) -> dict[str, Any]:
    return _stage_result(
        "probe",
        str(probe.verdict.outcome),
        probe.control.exit_code,
        probe.control.next_action,
        next_action_code=probe.control.next_action_code,
        reason_code=probe.verdict.reason_code,
        reason=probe.verdict.reason,
        detected_heading=probe.verdict.detected_heading,
        bounded_entry_count=probe.verdict.bounded_entry_count,
        contract_family=probe.verdict.contract_family or str(probe.evidence.get("contract_family", "")),
        ranking_category=probe.verdict.ranking_category,
        reporting_period=probe.verdict.reporting_period,
    )


def _resolve_root(
    repo_root: Path | None,
    proposal_file: Path | None,
    state_dir: Path | None,
    config_paths: ConfigPaths | None,
    probe_evidence: Path | None,
    helper_metadata: Path | None,
    feasibility_evidence: Path | None,
    feasibility_doc: Path | None,
    source_specific_test: Path | None,
    onboarding_artifacts: tuple[Path, ...],
) -> Path:
    candidates = [
        proposal_file,
        state_dir,
        probe_evidence,
        helper_metadata,
        feasibility_evidence,
        feasibility_doc,
        source_specific_test,
        *onboarding_artifacts,
    ]
    if config_paths is not None:
        candidates.extend(config_paths.all)
    return policy.repo_root_from_paths(repo_root, candidates)


def _canonicalize_config_paths(root: Path, config_paths: ConfigPaths | None) -> ConfigPaths:
    return config_paths or ConfigPaths.from_root(root)


def _prepare_request(
    *,
    root: Path,
    source_url: str,
    probe: RetainedProbe,
    config_paths: ConfigPaths,
    helper_metadata_file: Path | None,
    helper_metadata_destination: Path | None,
    feasibility_evidence: Path | None,
    feasibility_doc: Path | None,
    proposal_file: Path | None,
    state_dir: Path | None,
    source_specific_test: Path | None,
    onboarding_artifacts: tuple[Path, ...],
    slug: str | None,
    metadata: Mapping[str, Any],
    reason: str,
    proposed_by: str,
    access_reason: str,
    onboarding_issue: str,
    run_day: str,
    confirmed_by_editor: bool,
    confirmation_token: str | None,
    git_runner: Callable[..., Any] | None,
) -> PreparedRequest:
    helper_input = policy.require_file(helper_metadata_file, "helper_metadata")
    feasibility_input = policy.require_file(feasibility_evidence or feasibility_doc, "feasibility_evidence")
    if feasibility_evidence is not None and feasibility_doc is None:
        raise policy.SourceOnboardingError(
            "feasibility", "feasibility_destination_required", "an explicit feasibility destination is required"
        )
    destination_doc = (feasibility_doc or feasibility_input).resolve()
    policy.ensure_not_symlink(feasibility_doc, "feasibility destination", reason_code="feasibility_destination_invalid")
    policy.ensure_not_symlink(source_specific_test, "source-specific test", reason_code="source_test_invalid")
    source_specific = source_specific_test.resolve() if source_specific_test is not None else None
    issue = policy.parse_onboarding_issue(onboarding_issue)
    selected_slug = slug.strip() if slug is not None else policy.source_slug(str(metadata.get("source_name", "")))
    selected_slug = policy.validate_slug(selected_slug)
    policy.validate_source_specific_test(source_specific, root, issue)
    artifacts = policy.validate_onboarding_artifacts(onboarding_artifacts, root)
    for config_path in config_paths.all:
        policy.ensure_not_symlink(
            config_path, "canonical configurations", reason_code="configuration_destination_invalid"
        )
    canonical = policy.canonical_paths(
        repo_root=root,
        proposal_file=proposal_file,
        state_dir=state_dir,
        config_paths=config_paths,
        feasibility_doc=destination_doc,
        source_specific_test=source_specific,
        slug=selected_slug,
        onboarding_artifacts=artifacts,
    )
    canonical = policy.with_fixture_paths(canonical, source_url, selected_slug)
    _validate_canonical_destinations(canonical, root, issue, helper_metadata_destination)
    for config_path in canonical.configs:
        if not policy.is_safe_regular_file(config_path):
            raise policy.SourceOnboardingError(
                "scaffold", "configuration_missing", "the four canonical configuration files are required"
            )
    request_reason = reason.strip()
    if not request_reason:
        raise policy.SourceOnboardingError("proposal", "reason_required", "an explicit onboarding reason is required")
    options = dict(metadata)
    options["url"] = source_url
    metadata_values = policy.build_metadata(options, probe.evidence, probe.verdict)
    selected_entries = validate_helper_metadata_agreement(
        helper_input,
        source_url=source_url,
        language=metadata_values["language"],
        verdict=probe.verdict,
        author_note_optional=(
            metadata_values.get("contract_family", "").casefold() == "ranking-table"
            and metadata_values.get("ranking_author_optional") is True
        ),
    )
    input_paths = {
        "probe_evidence": probe.evidence_path,
        "helper_input": helper_input,
        "feasibility_input": feasibility_input,
    }
    # A declared artifact is never another onboarding input or destination:
    # probe evidence in particular stays out of the onboarding commit (#664).
    claimed = {
        *input_paths.values(),
        canonical.proposal_file,
        *canonical.configs,
        canonical.page_fixture,
        canonical.extraction_fixture,
        canonical.helper_metadata,
        canonical.feasibility_doc,
        canonical.source_specific_test,
    }
    if claimed.intersection(canonical.onboarding_artifacts):
        raise policy.SourceOnboardingError(
            "inputs",
            "onboarding_artifact_invalid",
            "onboarding artifact must not repeat another onboarding input or destination",
        )
    context, proposals, existing, proposal_state_error = _prepare_proposal_state(
        canonical=canonical,
        source_url=source_url,
        metadata=metadata_values,
        reason=request_reason,
        proposed_by=proposed_by,
        access_reason=access_reason,
        run_day=run_day,
        onboarding_issue=issue,
        input_paths=input_paths,
        probe=probe,
        selected_slug=selected_slug,
        confirmed_by_editor=confirmed_by_editor,
    )
    request = policy.request_payload(context)
    expected_token = policy.confirmation_token_for(request)
    if confirmed_by_editor and not confirmation_token:
        raise policy.SourceOnboardingError(
            "proposal", "confirmation_token_required", "confirmation requires the exact preview token"
        )
    if confirmed_by_editor and confirmation_token != expected_token:
        raise policy.SourceOnboardingError(
            "proposal", "confirmation_payload_mismatch", "confirmation token does not match the request payload"
        )
    if confirmed_by_editor and proposal_state_error is not None:
        raise policy.SourceOnboardingError("proposal", "invalid_proposal_state", proposal_state_error)
    if existing is not None:
        policy.validate_existing_proposal(existing, context)
        recorded_request = existing.get("onboarding_request")
        if isinstance(recorded_request, Mapping) and not policy.same_request(existing, context):
            raise policy.SourceOnboardingError(
                "proposal", "confirmation_payload_mismatch", "Source Proposal payload differs from the prepared request"
            )
    prepared, proposal, _created = proposal_commands.prepare_source_proposal(
        list(proposals),
        source_url=context.url,
        proposed_by=context.proposed_by,
        reason=context.reason,
        proposal_date=context.run_date,
        supporting_evidence=[policy.evidence_summary(probe.evidence, probe.verdict)],
        suggested_signal_family=context.metadata["signal_family"],
        source_name=context.metadata["source_name"],
        access_status=context.metadata["access_status"],
        caveats=context.metadata["notes"],
    )
    policy.validate_existing_proposal(proposal, context)
    feasibility_bytes = policy.validate_feasibility_document(feasibility_input, context)
    allowed_inputs = set(input_paths.values())
    if canonical.source_specific_test is not None:
        allowed_inputs.add(canonical.source_specific_test)
    allowed_inputs.update(canonical.onboarding_artifacts)
    if confirmed_by_editor:
        dirty = transaction.git_preflight(canonical, allowed_inputs, git_runner=git_runner)
        if canonical.source_specific_test is not None and canonical.source_specific_test not in dirty:
            raise policy.SourceOnboardingError(
                "persistence", "source_test_not_dirty", "explicit source-specific test must be dirty before staging"
            )
        # The commit is selected by content, so a mode-only change counts as unchanged.
        clean_artifacts = [
            path
            for path in canonical.onboarding_artifacts
            if path not in dirty or transaction.head_bytes(root, path, transaction.git_bytes) == path.read_bytes()
        ]
        if clean_artifacts:
            names = ", ".join(str(path.relative_to(root)) for path in clean_artifacts)
            raise policy.SourceOnboardingError(
                "persistence",
                "onboarding_artifact_not_dirty",
                f"declared onboarding artifacts have no content change: {names}",
            )
    return PreparedRequest(
        root,
        source_url,
        probe.evidence_path,
        helper_input,
        feasibility_input,
        feasibility_bytes,
        input_paths,
        canonical,
        context,
        request,
        proposal,
        tuple(dict(item) for item in prepared),
        selected_entries,
    )


def validate_helper_metadata_agreement(
    helper_input: Path,
    *,
    source_url: str,
    language: str,
    verdict: source_feasibility.FeasibilityVerdict,
    author_note_optional: bool = False,
) -> tuple[source_feasibility.EntrySample, ...]:
    """Agree the retained helper sidecar with the selected representatives.

    The confirmed operation generates its replay fixture pair from the same
    deterministic representative selection and refuses a sidecar that does not
    match it exactly. Running that agreement check here means the offline
    preview refuses the same sidecar with a specific correctable reason code,
    so the editor is never asked to confirm helper metadata that the confirmed
    fixture-generation contract must reject (Issue #409).

    The check reads the retained sidecar bytes only; it makes no network call,
    no model call, and no filesystem mutation. An author-optional ranking may
    omit the transliteration note for its authorless representative.
    """
    try:
        samples = replay_fixtures.select_representative_entries(verdict)
    except replay_fixtures.ReplayFixtureError as exc:
        raise _helper_metadata_error(exc) from exc
    try:
        replay_fixtures.validate_helper_sidecar(
            helper_input,
            url=source_url,
            language=language,
            samples=samples,
            author_note_optional=author_note_optional,
        )
    except replay_fixtures.ReplayFixtureError as exc:
        raise _helper_metadata_error(exc, samples) from exc
    return samples


def _helper_metadata_error(
    exc: replay_fixtures.ReplayFixtureError, samples: tuple[source_feasibility.EntrySample, ...] = ()
) -> policy.SourceOnboardingError:
    """Turn one refused helper input into a specific correctable failure."""
    return policy.SourceOnboardingError(
        "inputs",
        exc.reason_code or "helper_metadata_invalid",
        policy.error_text(exc),
        details=({"representative_entries": serialize_representative_entries(samples)} if samples else None),
    )


def serialize_representative_entries(samples: tuple[source_feasibility.EntrySample, ...]) -> list[dict[str, str]]:
    """Report the selected representative identities the sidecar must mirror."""
    return [{"title": sample.title, "author": sample.author, "evidence_url": sample.url} for sample in samples]


def _validate_canonical_destinations(
    canonical: policy.CanonicalPaths,
    root: Path,
    issue: OnboardingIssue,
    helper_metadata_destination: Path | None,
) -> None:
    for path, expected_name in zip(canonical.configs, policy.CANONICAL_CONFIG_NAMES, strict=True):
        if policy.relative_to_repo(path, root).parts != ("configs", expected_name):
            raise policy.SourceOnboardingError(
                "inputs", "configuration_destination_invalid", "canonical configurations must remain under configs/"
            )
    policy.relative_to_repo(canonical.proposal_file, root)
    policy.validate_fixture_directory(canonical.fixture_dir, root)
    policy.relative_to_repo(canonical.helper_metadata, root)
    policy.validate_feasibility_destination(canonical.feasibility_doc, root, issue)
    policy.ensure_not_symlink(
        helper_metadata_destination,
        "helper metadata destination",
        reason_code="helper_metadata_destination_invalid",
    )
    if helper_metadata_destination is not None and helper_metadata_destination.absolute() != canonical.helper_metadata:
        raise policy.SourceOnboardingError(
            "inputs",
            "helper_metadata_destination_invalid",
            "helper metadata destination must be the canonical slug sidecar",
        )
    for path in (
        canonical.page_fixture,
        canonical.extraction_fixture,
        canonical.helper_metadata,
        canonical.feasibility_doc,
    ):
        if not policy.is_safe_regular_file(path, allow_missing=True):
            raise policy.SourceOnboardingError(
                "inputs", "destination_invalid", "onboarding destination must be a safe regular file"
            )


def _prepare_proposal_state(
    *,
    canonical: policy.CanonicalPaths,
    source_url: str,
    metadata: Mapping[str, str],
    reason: str,
    proposed_by: str,
    access_reason: str,
    run_day: str,
    onboarding_issue: OnboardingIssue,
    input_paths: Mapping[str, Path],
    probe: RetainedProbe,
    selected_slug: str,
    confirmed_by_editor: bool,
) -> tuple[policy.OnboardingContext, list[dict[str, Any]], Mapping[str, Any] | None, str | None]:
    proposal_id = source_proposals.proposal_id_for(source_url)
    proposals: list[dict[str, Any]] = []
    existing: Mapping[str, Any] | None = None
    proposal_state_error: str | None = None
    try:
        value = open_state_store(policy.proposal_state_dir(canonical.proposal_file)).read(Collection.SOURCE_PROPOSALS)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        if not confirmed_by_editor:
            raise
        value = []
        proposal_state_error = policy.error_text(exc)
    if not isinstance(value, list):
        if not confirmed_by_editor:
            raise policy.SourceOnboardingError(
                "proposal", "invalid_proposal_state", "Source Proposal state must be a list"
            )
        proposal_state_error = "Source Proposal state must be a list"
    else:
        proposals = cast(list[dict[str, Any]], value)
        try:
            existing = proposal_commands.find_source_proposal(proposals, proposal_id)
        except (AttributeError, TypeError) as exc:
            if not confirmed_by_editor:
                raise
            proposal_state_error = policy.error_text(exc)
        if existing is not None and str(existing.get("status", "")).casefold() == "accepted":
            raise policy.SourceOnboardingError(
                "proposal", "proposal_already_accepted", "Source Proposal has already been accepted and onboarded"
            )
    context = policy.OnboardingContext(
        url=source_url.strip(),
        proposal_id=proposal_id,
        slug=selected_slug,
        metadata=dict(metadata),
        reason=reason,
        proposed_by=proposed_by.strip() or "agent",
        access_reason=access_reason.strip(),
        run_date=run_day,
        onboarding_issue=onboarding_issue,
        artifact_identity=policy.artifact_identity(canonical, input_paths),
        operation_id=(
            "replay-fixture:"
            + selected_slug
            + ":"
            + hashlib.sha256(source_proposals.normalize_source_identity(source_url).encode("utf-8")).hexdigest()[:8]
        ),
    )
    for field in ("lifecycle_note", "notes"):
        if not onboarding_issue.matches(context.metadata[field]):
            raise policy.SourceOnboardingError(
                "proposal", "onboarding_issue_missing", f"{field} must name the onboarding issue"
            )
    return context, proposals, existing, proposal_state_error


def _preview_report(prepared: PreparedRequest, stages: list[dict[str, Any]]) -> tuple[dict[str, Any], int]:
    context = prepared.context
    stage = _stage_result(
        "proposal",
        "confirmation_required",
        EXIT_CONFIRMATION,
        "Confirm the proposed Source Proposal to continue offline onboarding.",
        next_action_code="confirmation_required",
        proposal_id=context.proposal_id,
        status=prepared.proposal.get("status", "proposed"),
        metadata=dict(context.metadata),
        slug=context.slug,
        onboarding_issue=context.onboarding_issue.serialize(),
        effective_run_date=context.run_date,
        artifact_identity=context.artifact_identity,
        representative_entries=serialize_representative_entries(prepared.representative_entries),
    )
    stages.append(stage)
    return _report(
        stages,
        stage,
        confirmation_required=True,
        proposal_id=context.proposal_id,
        effective_run_date=context.run_date,
        artifact_identity=context.artifact_identity,
        representative_entries=serialize_representative_entries(prepared.representative_entries),
        onboarding_request=dict(prepared.request),
        confirmation_token=policy.confirmation_token_for(prepared.request),
        next_action_code="confirmation_required",
    )


def _snapshot_runner_inputs(
    prepared: PreparedRequest,
    staged: transaction.StagedPaths,
    publication: Mapping[Path, Path],
) -> dict[Path, bytes | None]:
    paths = [
        *prepared.input_paths.values(),
        staged.probe_evidence,
        staged.helper_input,
        staged.feasibility_input,
        *publication.keys(),
        *publication.values(),
    ]
    return transaction.snapshot_paths(dict.fromkeys(paths))


def _run_validation_adapter(
    name: str,
    runner: Callable[..., Mapping[str, Any]],
    arguments: tuple[Any, ...],
    snapshot_paths: Callable[[], Mapping[Path, bytes | None]],
    next_action: str,
    reason_code: str,
) -> dict[str, Any]:
    before = snapshot_paths()
    result = runner(*arguments)
    transaction.assert_snapshot_unchanged(before, name)
    if not transaction.runner_passed(result):
        details: Mapping[str, Any] = {}
        if name == "replay" and isinstance(result.get("diagnostics"), Mapping):
            details = {"replay_diagnostics": dict(result["diagnostics"])}
        raise policy.SourceOnboardingError(
            name,
            reason_code,
            str(result.get("reason", f"{name} validation failed")),
            details=details,
        )
    return _stage_result(name, "passed", EXIT_SUCCESS, next_action, **dict(result))


def _run_guarded_validation(
    runners: OnboardingRunners,
    prepared: PreparedRequest,
    stage_root: Path,
    staged: transaction.StagedPaths,
    source: Mapping[str, Any],
    publication: Mapping[Path, Path],
    stages: list[dict[str, Any]],
) -> None:
    """Run every validation adapter while protecting canonical and staged inputs.

    Each stage result is appended to *stages* as soon as that adapter passes,
    so a later failure keeps the earlier evidence. The failure report then
    names the stage that failed and shows which validation stages already
    passed, instead of hiding them (#404).
    """

    def snapshot() -> Mapping[Path, bytes | None]:
        return _snapshot_runner_inputs(prepared, staged, publication)

    adapters: tuple[tuple[str, Callable[..., Mapping[str, Any]], tuple[Any, ...], str, str], ...] = (
        (
            "replay",
            runners.replay,
            (stage_root, staged.mapping, source),
            "Run the source contract suites.",
            "replay_validation_failed",
        ),
        (
            "contracts",
            runners.contracts,
            (stage_root, staged.mapping, source),
            "Run the full validation suite.",
            "contract_validation_failed",
        ),
        (
            "validation",
            runners.full_suite,
            (stage_root,),
            "Persist the validated onboarding bundle in one commit.",
            "full_validation_failed",
        ),
    )
    for name, runner, arguments, next_action, reason_code in adapters:
        stages.append(_run_validation_adapter(name, runner, arguments, snapshot, next_action, reason_code))


def _execute_confirmed(
    prepared: PreparedRequest,
    stages: list[dict[str, Any]],
    *,
    runners: OnboardingRunners | None,
    git_runner: Callable[..., Any] | None,
) -> tuple[dict[str, Any], int]:
    active_runners = runners or transaction.default_onboarding_runners()
    stage_root = transaction.stage_copy(prepared.root)
    try:
        staged_inputs = {
            name: transaction.stage_input(stage_root, path, name, prepared.root)
            for name, path in prepared.input_paths.items()
        }
        staged = transaction.stage_paths(prepared.canonical, stage_root, staged_inputs)
        transaction.copy_exact_to_stage(staged.helper_input, staged.helper_metadata, "helper metadata sidecar")
        transaction.copy_exact_to_stage(staged.feasibility_input, staged.feasibility_doc, "feasibility document")
        staged_request = dict(prepared.request)
        staged_proposal = dict(prepared.proposal)
        staged_proposal["onboarding_request"] = staged_request
        staged_proposals = [dict(item) for item in prepared.prepared_proposals]
        staged_match = proposal_commands.find_source_proposal(staged_proposals, prepared.context.proposal_id)
        if staged_match is None:
            raise policy.SourceOnboardingError(
                "proposal", "confirmation_payload_mismatch", "prepared Source Proposal is not available for staging"
            )
        staged_proposals[staged_proposals.index(staged_match)] = staged_proposal
        staged_state_dir = policy.proposal_state_dir(staged.proposal_file)
        staged_state_dir.mkdir(parents=True, exist_ok=True)
        proposal_commands.write_source_proposals_state(staged_state_dir, staged_proposals)
        proposal_commands.accept_source_proposal_state(
            staged_state_dir,
            prepared.context.proposal_id,
            store=proposal_commands.staged_proposals_store(staged_state_dir),
            source_name=prepared.context.metadata["source_name"],
            access_status=prepared.context.metadata["access_status"],
            access_reason=prepared.context.access_reason or prepared.context.metadata["lifecycle_note"],
        )
        normalized_metadata = source_scaffold.normalize_operator_metadata(prepared.context.metadata)
        replay_contract_metadata = source_scaffold.build_probation_replay_contract_metadata(normalized_metadata)
        pair = replay_fixtures.build_replay_fixture_pair(
            prepared.context.url,
            prepared.context.slug,
            policy.load_mapping(staged.probe_evidence, "probe evidence"),
            source_name=normalized_metadata["source_name"],
            signal_family=normalized_metadata["signal_family"],
            contract_family=str(normalized_metadata.get("contract_family", "")),
            contract_metadata=replay_contract_metadata,
            signal_strength=normalized_metadata["expected_signal_strength"],
            candidate_category=normalized_metadata["eligible_candidate_category"],
            language=normalized_metadata["language"],
            expected_sort_label=normalized_metadata["expected_sort_label"],
            helper_metadata_file=staged.helper_metadata,
        )
        _stage_fixtures(staged, pair, stages)
        context = replace(prepared.context, operation_id=pair.operation_id)
        scaffold_report, scaffold_code = source_scaffold.run_scaffold_probation_source(
            probe_evidence=staged.probe_evidence,
            page_fixture=staged.page_fixture,
            extraction_fixture=staged.extraction_fixture,
            helper_metadata_file=staged.helper_metadata,
            proposal_file=staged.proposal_file,
            proposal_id=context.proposal_id,
            slug=context.slug,
            **staged.config_paths.scaffold_kwargs(),
            metadata=normalized_metadata,
        )
        if scaffold_code != 0:
            raise policy.SourceOnboardingError("scaffold", "scaffold_failed", str(scaffold_report))
        scaffold_details = dict(scaffold_report)
        scaffold_details.pop("exit_code", None)
        stages.append(
            _stage_result("scaffold", "staged", EXIT_SUCCESS, "Run the replay validation.", **scaffold_details)
        )
        source = {"name": context.metadata["source_name"], "run_date": context.run_date}
        publication = transaction.publication_mapping(prepared.canonical, staged)
        _run_guarded_validation(active_runners, prepared, stage_root, staged, source, publication, stages)
        if staged.feasibility_doc.read_bytes() != prepared.feasibility_bytes:
            raise policy.SourceOnboardingError(
                "feasibility", "feasibility_content_mismatch", "staged feasibility evidence is not exact"
            )
        commit = transaction.publish_and_commit(
            prepared.canonical,
            staged,
            context,
            allowed_unpublished=prepared.input_paths.values(),
            git_runner=git_runner,
        )
        persistence = _stage_result(
            "persistence", "completed", EXIT_SUCCESS, "Review the completed onboarding operation.", commit=commit
        )
        stages.append(persistence)
        return _report(
            stages,
            persistence,
            commit=commit,
            source_name=context.metadata["source_name"],
            onboarding_issue=context.onboarding_issue.serialize(),
            effective_run_date=context.run_date,
            artifact_identity=context.artifact_identity,
            onboarding_request=policy.request_payload(context),
        )
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def _stage_fixtures(
    staged: transaction.StagedPaths, pair: replay_fixtures.ReplayFixturePair, stages: list[dict[str, Any]]
) -> None:
    if staged.page_fixture.exists() and staged.page_fixture.read_bytes() != pair.page_bytes:
        raise policy.SourceOnboardingError(
            "fixtures", "fixture_conflict", "existing page fixture differs from deterministic output"
        )
    if staged.extraction_fixture.exists() and staged.extraction_fixture.read_bytes() != pair.extraction_bytes:
        raise policy.SourceOnboardingError(
            "fixtures", "fixture_conflict", "existing extraction fixture differs from deterministic output"
        )
    staged.page_fixture.parent.mkdir(parents=True, exist_ok=True)
    safe_artifacts.publish_bundle(
        [staged.page_fixture, staged.extraction_fixture], [pair.page_bytes, pair.extraction_bytes]
    )
    stages.append(
        _stage_result(
            "fixtures",
            "staged",
            EXIT_SUCCESS,
            "Validate replay before publishing live configuration.",
            page_fixture=str(staged.page_fixture),
            extraction_fixture=str(staged.extraction_fixture),
            helper_metadata=str(staged.helper_metadata),
            operation_id=pair.operation_id,
            capture_sha256=pair.capture_sha256,
        )
    )


def run_onboarding_flow(
    request: OnboardingRequest,
    *,
    runners: OnboardingRunners | None = None,
    git_runner: Callable[..., Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """Coordinate retained-probe evaluation, preparation, preview, or execution."""
    stages: list[dict[str, Any]] = []
    try:
        root = _resolve_root(
            request.repo_root,
            request.proposal_file,
            request.state_dir,
            request.config_paths,
            request.probe_evidence,
            request.helper_metadata_file,
            request.feasibility_evidence,
            request.feasibility_doc,
            request.source_specific_test,
            request.onboarding_artifacts,
        )
        if request.proposal_file is not None:
            policy.validate_proposal_file(request.proposal_file)
        else:
            policy.validate_proposal_file(
                policy.proposal_file_for(request.state_dir or root / policy.DEFAULT_STATE_DIR)
            )
        probe = _evaluate_retained_probe(request.url, request.probe_evidence)
        probe_stage = _probe_stage(probe)
        stages.append(probe_stage)
        if str(probe.verdict.outcome) != source_feasibility.OUTCOME_ACCEPT:
            return _report(
                stages,
                probe_stage,
                reason_code=probe.verdict.reason_code,
                reason=probe.verdict.reason,
                next_action_code=probe.control.next_action_code,
            )
        if request.run_date is None:
            raise policy.SourceOnboardingError(
                "proposal", "run_date_required", "accepted onboarding requires an explicit ISO run date"
            )
        run_day = policy.validate_run_date(request.run_date)
        root_configs = _canonicalize_config_paths(root, request.config_paths)
        prepared = _prepare_request(
            root=root,
            source_url=request.url or "",
            probe=probe,
            config_paths=root_configs,
            helper_metadata_file=request.helper_metadata_file,
            helper_metadata_destination=request.helper_metadata_destination,
            feasibility_evidence=request.feasibility_evidence,
            feasibility_doc=request.feasibility_doc,
            proposal_file=request.proposal_file,
            state_dir=request.state_dir,
            source_specific_test=request.source_specific_test,
            onboarding_artifacts=request.onboarding_artifacts,
            slug=request.slug,
            metadata=request.metadata,
            reason=request.reason,
            proposed_by=request.proposed_by,
            access_reason=request.access_reason,
            onboarding_issue=request.onboarding_issue,
            run_day=run_day,
            confirmed_by_editor=request.confirmed_by_editor,
            confirmation_token=request.confirmation_token,
            git_runner=git_runner,
        )
        if not request.confirmed_by_editor:
            return _preview_report(prepared, stages)
        stages.append(
            _stage_result(
                "proposal",
                "accepted",
                EXIT_SUCCESS,
                "Generate the deterministic replay fixture pair.",
                proposal_id=prepared.context.proposal_id,
            )
        )
        return _execute_confirmed(
            prepared,
            stages,
            runners=runners,
            git_runner=git_runner,
        )
    except policy.SourceOnboardingError as exc:
        return failure_report(stages, exc.stage, exc.reason_code, exc.message, exc.details)
    except (
        OSError,
        replay_fixtures.ReplayFixtureError,
        safe_artifacts.SafeArtifactError,
        ValueError,
        TypeError,
    ) as exc:
        return failure_report(stages, "onboarding", "unexpected_failure", policy.error_text(exc))
