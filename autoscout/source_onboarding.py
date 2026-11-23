"""Public façade for offline, confirmation-gated source onboarding."""

from __future__ import annotations

import json
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from autoscout.source_onboarding_flow import failure_report
from autoscout.source_onboarding_flow import run_onboarding_flow
from autoscout.source_onboarding_models import ConfigPaths
from autoscout.source_onboarding_models import OnboardingIssue
from autoscout.source_onboarding_models import OnboardingRequest
from autoscout.source_onboarding_transaction import OnboardingRunners


def run_add_source(
    *,
    url: str | None,
    probe_evidence: Path | None,
    helper_metadata_file: Path | None = None,
    helper_metadata_destination: Path | None = None,
    feasibility_evidence: Path | None = None,
    config_paths: ConfigPaths | None = None,
    proposal_file: Path | None = None,
    state_dir: Path | None = None,
    feasibility_doc: Path | None = None,
    source_specific_test: Path | None = None,
    onboarding_artifacts: Sequence[Path] = (),
    slug: str | None = None,
    repo_root: Path | None = None,
    metadata: Mapping[str, Any] | None = None,
    reason: str = "",
    proposed_by: str = "agent",
    access_reason: str = "",
    onboarding_issue: str = "",
    run_date: str | None = None,
    confirmation_token: str | None = None,
    confirmed_by_editor: bool = False,
    runners: OnboardingRunners | None = None,
    git_runner: Callable[..., Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """Run the onboarding flow with explicit, typed dependency seams."""
    request = OnboardingRequest(
        url=url,
        probe_evidence=probe_evidence,
        helper_metadata_file=helper_metadata_file,
        helper_metadata_destination=helper_metadata_destination,
        feasibility_evidence=feasibility_evidence,
        config_paths=config_paths,
        proposal_file=proposal_file,
        state_dir=state_dir,
        feasibility_doc=feasibility_doc,
        source_specific_test=source_specific_test,
        onboarding_artifacts=tuple(onboarding_artifacts),
        slug=slug,
        repo_root=repo_root,
        metadata=metadata or {},
        reason=reason,
        proposed_by=proposed_by,
        access_reason=access_reason,
        onboarding_issue=onboarding_issue,
        run_date=run_date,
        confirmation_token=confirmation_token,
        confirmed_by_editor=confirmed_by_editor,
    )
    return run_onboarding_flow(request, runners=runners, git_runner=git_runner)


def add_source_command(
    *,
    url: str | None,
    probe_evidence: Path | None,
    helper_metadata_file: Path | None = None,
    helper_metadata_destination: Path | None = None,
    feasibility_evidence: Path | None = None,
    config_paths: ConfigPaths | None = None,
    proposal_file: Path | None = None,
    state_dir: Path | None = None,
    feasibility_doc: Path | None = None,
    source_specific_test: Path | None = None,
    onboarding_artifacts: Sequence[Path] = (),
    slug: str | None = None,
    repo_root: Path | None = None,
    metadata: Mapping[str, Any] | None = None,
    reason: str = "",
    proposed_by: str = "agent",
    access_reason: str = "",
    onboarding_issue: str = "",
    run_date: str | None = None,
    confirmation_token: str | None = None,
    confirmed_by_editor: bool = False,
    runners: OnboardingRunners | None = None,
    git_runner: Callable[..., Any] | None = None,
) -> int:
    """Print one structured JSON report for the public ``add-source`` CLI."""
    try:
        report, code = run_add_source(
            url=url,
            probe_evidence=probe_evidence,
            helper_metadata_file=helper_metadata_file,
            helper_metadata_destination=helper_metadata_destination,
            feasibility_evidence=feasibility_evidence,
            config_paths=config_paths,
            proposal_file=proposal_file,
            state_dir=state_dir,
            feasibility_doc=feasibility_doc,
            source_specific_test=source_specific_test,
            onboarding_artifacts=onboarding_artifacts,
            slug=slug,
            repo_root=repo_root,
            metadata=metadata,
            reason=reason,
            proposed_by=proposed_by,
            access_reason=access_reason,
            onboarding_issue=onboarding_issue,
            run_date=run_date,
            confirmation_token=confirmation_token,
            confirmed_by_editor=confirmed_by_editor,
            runners=runners,
            git_runner=git_runner,
        )
    except Exception as exc:
        report, code = failure_report([], "onboarding", "unexpected_failure", str(exc).strip() or type(exc).__name__)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return code


__all__ = [
    "ConfigPaths",
    "OnboardingIssue",
    "OnboardingRequest",
    "OnboardingRunners",
    "add_source_command",
    "run_add_source",
]
