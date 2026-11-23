"""Input and policy validation for offline source onboarding.

This module owns destination policy, metadata checks, issue identity checks,
and confirmation payload identity.
It does not perform Git, network, model, or canonical-state writes.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from autoscout.json_io import read_json_or_default
from autoscout.ranking_detail_route import ranking_detail_route_declared
from autoscout.source_feasibility import FeasibilityVerdict
from autoscout.source_feasibility import retained_summary_samples
from autoscout.source_metadata import RANKING_TABLE_FIELDS
from autoscout.source_metadata import SOURCE_LAYOUT_FIELDS
from autoscout.source_metadata import SOURCE_SCAFFOLD_OPTIONAL_METADATA_FIELDS
from autoscout.source_metadata import SOURCE_SCAFFOLD_REQUIRED_METADATA_FIELDS
from autoscout.source_onboarding_models import ConfigPaths
from autoscout.source_onboarding_models import OnboardingIssue
from autoscout.source_proposals import normalize_source_identity

DEFAULT_STATE_DIR = Path("runs/current-weekly-pilot-russian")
# The file the JSON State Store keeps a state directory's Source Proposals in.
# The add-source flow names it for two file-level jobs only: its documented
# ``--proposals-file`` flag takes that file, and its onboarding commit
# fingerprints and commits the file's bytes the way the git envelope commits
# the export (#1073 teaches both the committed layout). The proposals
# themselves are read and written through the State Store of the file's
# directory, the accepted ones included (#1093), so the file is the JSON
# backend's or the SQLite backend's export of them.
PROPOSAL_FILE_NAME = "source-proposals.json"
DEFAULT_FIXTURE_DIR = Path("data/current-weekly-pilot")
_HELPER_SUFFIX = "-helper-metadata.json"
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SOURCE_TEST_RE = re.compile(r"^test_source_[a-z0-9][a-z0-9_-]*\.py$")
_FEASIBILITY_DOC_RE = re.compile(r"^issue-(?P<issue>[0-9]+)-[a-z0-9]+(?:-[a-z0-9]+)*-source-feasibility\.md$")
CANONICAL_CONFIG_NAMES = (
    "current-weekly-portfolio.json",
    "current-weekly-pilot.json",
    "current-weekly-pilot-live.json",
    "current-weekly-live.json",
)
_PLACEHOLDER_FEASIBILITY_MARKERS = (
    "draft status: draft",
    "intentionally incomplete",
    "requires editor or agent judgment",
    "no editorial rationale is invented",
    "todo",
    "tbd",
)


class SourceOnboardingError(ValueError):
    """A guarded onboarding stage failed.

    *details* carries the bounded, already-computed evidence an operator needs
    to correct this exact input, for example the deterministic representative
    entries a helper sidecar must mirror (Issue #409). The failure report
    copies it verbatim beside the reason code and the next action."""

    def __init__(self, stage: str, reason_code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.reason_code = reason_code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


@dataclass(frozen=True)
class CanonicalPaths:
    """The only repository destinations that onboarding may publish."""

    repo_root: Path
    proposal_file: Path
    config_paths: ConfigPaths
    fixture_dir: Path
    page_fixture: Path
    extraction_fixture: Path
    helper_metadata: Path
    feasibility_doc: Path
    source_specific_test: Path | None
    onboarding_artifacts: tuple[Path, ...] = ()

    @property
    def configs(self) -> tuple[Path, ...]:
        return self.config_paths.all


@dataclass(frozen=True)
class OnboardingContext:
    """Immutable operator payload used for preview and confirmation matching."""

    url: str
    proposal_id: str
    slug: str
    metadata: dict[str, str]
    reason: str
    proposed_by: str
    access_reason: str
    run_date: str
    onboarding_issue: OnboardingIssue
    artifact_identity: dict[str, Any]
    operation_id: str = ""


def error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__


def is_safe_regular_file(path: Path, *, allow_missing: bool = False) -> bool:
    """Return whether *path* is a single-link regular file.

    ``allow_missing`` is explicit for destinations that may be created later.
    ``lstat`` rejects symlinks without following them, and every inspection
    error fails closed.
    """
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return allow_missing
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1


def load_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        value = read_json_or_default(path, None)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise SourceOnboardingError(
            "probe", "malformed_evidence", f"{label} cannot be read: {error_text(exc)}"
        ) from exc
    if not isinstance(value, Mapping):
        raise SourceOnboardingError("probe", "malformed_evidence", f"{label} must be a JSON object")
    return dict(value)


def valid_url(value: str) -> bool:
    if not value or any(character.isspace() for character in value):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.netloc and parsed.hostname)


def repo_root_from_paths(repo_root: Path | None, candidates: Iterable[Path | None]) -> Path:
    if repo_root is not None:
        return repo_root.resolve()
    for candidate in candidates:
        if candidate is None:
            continue
        current = candidate.resolve() if candidate.exists() else candidate.absolute()
        for ancestor in (current, *current.parents):
            if (ancestor / ".git").exists():
                return ancestor
    return Path.cwd().resolve()


def relative_to_repo(path: Path, repo_root: Path) -> Path:
    try:
        return path.resolve().relative_to(repo_root.resolve())
    except ValueError as exc:
        raise SourceOnboardingError("inputs", "path_outside_repository", f"path is outside repository: {path}") from exc


def ensure_not_symlink(path: Path | None, label: str, *, stage: str = "inputs", reason_code: str | None = None) -> None:
    """Reject one symlink path before it can enter the onboarding flow."""
    if path is not None and path.is_symlink():
        raise SourceOnboardingError(stage, reason_code or f"{label}_invalid", f"{label} must not be a symlink")


def require_file(path: Path | None, label: str) -> Path:
    if path is None:
        raise SourceOnboardingError("inputs", f"{label}_required", f"{label} is required")
    ensure_not_symlink(path, label)
    resolved = path.resolve()
    if not is_safe_regular_file(resolved, allow_missing=True):
        raise SourceOnboardingError("inputs", f"{label}_invalid", f"{label} must be a safe regular file")
    if not resolved.exists():
        raise SourceOnboardingError("inputs", f"{label}_missing", f"{label} does not exist")
    return resolved


def source_slug(source_name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", source_name.casefold()).strip("-")
    return value or "source"


def validate_slug(slug: str) -> str:
    value = slug.strip()
    if not value or _SLUG_RE.fullmatch(value) is None:
        raise SourceOnboardingError(
            "proposal",
            "invalid_slug",
            "slug must contain only lowercase letters, digits, and single hyphens",
        )
    return value


def canonical_paths(
    *,
    repo_root: Path,
    proposal_file: Path | None,
    state_dir: Path | None,
    config_paths: ConfigPaths,
    feasibility_doc: Path,
    source_specific_test: Path | None,
    slug: str,
    onboarding_artifacts: tuple[Path, ...] = (),
) -> CanonicalPaths:
    root = repo_root.resolve()
    state = (state_dir or root / DEFAULT_STATE_DIR).resolve()
    proposal = (proposal_file or proposal_file_for(state)).resolve()
    fixture_root = (root / DEFAULT_FIXTURE_DIR).resolve()
    config_paths = ConfigPaths(*((path if path.is_absolute() else root / path).resolve() for path in config_paths.all))
    return CanonicalPaths(
        repo_root=root,
        proposal_file=proposal,
        config_paths=config_paths,
        fixture_dir=fixture_root,
        page_fixture=fixture_root / "placeholder-page.json",
        extraction_fixture=fixture_root / "placeholder-extraction.json",
        helper_metadata=fixture_root / f"{slug}{_HELPER_SUFFIX}",
        feasibility_doc=feasibility_doc.resolve(),
        source_specific_test=source_specific_test.resolve() if source_specific_test is not None else None,
        onboarding_artifacts=onboarding_artifacts,
    )


def with_fixture_paths(canonical: CanonicalPaths, url: str, slug: str) -> CanonicalPaths:
    from autoscout.replay_fixtures import _output_names

    page_name, extraction_name = _output_names(slug, url)
    return replace(
        canonical,
        page_fixture=canonical.fixture_dir / page_name,
        extraction_fixture=canonical.fixture_dir / extraction_name,
    )


def metadata_value(options: Mapping[str, Any], key: str) -> str:
    value = options.get(key)
    return value.strip() if isinstance(value, str) else ""


def build_metadata(
    options: Mapping[str, Any], evidence: Mapping[str, Any], verdict: FeasibilityVerdict
) -> dict[str, str]:
    """Accept only explicit operator metadata, with no judgment defaults."""
    missing = [field for field in SOURCE_SCAFFOLD_REQUIRED_METADATA_FIELDS if not metadata_value(options, field)]
    if missing:
        raise SourceOnboardingError(
            "proposal", "metadata_required", "missing onboarding metadata: " + ", ".join(missing)
        )
    values: dict[str, Any] = {
        field: metadata_value(options, field) for field in SOURCE_SCAFFOLD_REQUIRED_METADATA_FIELDS
    }
    values.update({field: metadata_value(options, field) for field in SOURCE_SCAFFOLD_OPTIONAL_METADATA_FIELDS})
    evidence_contract = str(evidence.get("contract_family", "")).strip().casefold()
    option_contract = metadata_value(options, "contract_family").casefold()
    if option_contract and evidence_contract and option_contract != evidence_contract:
        raise SourceOnboardingError("proposal", "metadata_mismatch", "contract_family does not match retained evidence")
    selected_contract = option_contract or evidence_contract
    if selected_contract:
        values["contract_family"] = selected_contract
    retained_contract_metadata = evidence.get("contract_metadata")
    retained_declarations: dict[str, Any] = (
        dict(retained_contract_metadata) if isinstance(retained_contract_metadata, Mapping) else {}
    )
    for field in (*SOURCE_LAYOUT_FIELDS, *RANKING_TABLE_FIELDS):
        if field in evidence:
            retained_declarations[field] = evidence[field]
    for field in ("visible_title_selector", "entry_scope_selector"):
        if evidence.get(field):
            retained_declarations[field] = evidence[field]
    for field, retained_value in retained_declarations.items():
        if field not in values and field not in options:
            values[field] = retained_value
        elif field in options and options[field] != retained_value:
            raise SourceOnboardingError(
                "proposal", "metadata_mismatch", f"{field} does not match retained probe evidence"
            )
    for field in (*SOURCE_LAYOUT_FIELDS, *RANKING_TABLE_FIELDS):
        if field in options:
            values[field] = options[field]
    evidence_name = str(evidence.get("source_name") or evidence.get("name") or "").strip()
    if evidence_name and evidence_name != values["source_name"]:
        raise SourceOnboardingError("proposal", "metadata_mismatch", "source_name does not match retained evidence")
    if values.get("contract_family", "").casefold() == "ranking-table":
        values.pop("expected_reporting_period", None)
        expected_evidence_url = "detail_page" if ranking_detail_route_declared(values) else "source_page"
        if values.get("ranking_evidence_url", "") != expected_evidence_url:
            raise SourceOnboardingError(
                "proposal", "metadata_mismatch", f"ranking_evidence_url must be exactly {expected_evidence_url}"
            )
    if values["expected_page_heading"] != verdict.detected_heading:
        raise SourceOnboardingError(
            "proposal", "metadata_mismatch", "expected_page_heading does not match retained evidence"
        )
    evidence_heading = str(evidence.get("detected_heading", "")).strip()
    if evidence_heading and evidence_heading != values["expected_page_heading"]:
        raise SourceOnboardingError("proposal", "metadata_mismatch", "retained heading is inconsistent with its HTML")
    if normalize_source_identity(values["url"]) != normalize_source_identity(str(evidence.get("source_url", ""))):
        raise SourceOnboardingError("proposal", "metadata_mismatch", "url does not match retained evidence")
    if values.get("contract_family", "").casefold() == "ranking-table":
        evidence_page = str(evidence.get("source_page", "")).strip()
        if normalize_source_identity(evidence_page) != normalize_source_identity(values["url"]):
            raise SourceOnboardingError("proposal", "metadata_mismatch", "source_page does not match the source URL")
    return values


def evidence_summary(evidence: Mapping[str, Any], verdict: FeasibilityVerdict) -> dict[str, Any]:
    return {
        "url": str(evidence.get("source_url", "")),
        "outcome": verdict.outcome,
        "reason_code": verdict.reason_code,
        "reason": verdict.reason,
        "head_status": evidence.get("head_status"),
        "get_status": evidence.get("get_status"),
        "resolved_url": str(evidence.get("resolved_url", "")),
        "detected_heading": verdict.detected_heading,
        "bounded_entry_count": verdict.bounded_entry_count,
        "contract_family": verdict.contract_family or str(evidence.get("contract_family", "")),
        "ranking_category": verdict.ranking_category,
        "reporting_period": verdict.reporting_period,
        "samples": retained_summary_samples(verdict),
    }


def parse_onboarding_issue(value: str) -> OnboardingIssue:
    try:
        return OnboardingIssue.parse(value)
    except (TypeError, ValueError) as exc:
        raise SourceOnboardingError(
            "proposal", "onboarding_issue_required", "onboarding issue must be a numeric issue identifier"
        ) from exc


def validate_run_date(value: str) -> str:
    candidate = value.strip()
    try:
        parsed = date.fromisoformat(candidate)
    except ValueError as exc:
        raise SourceOnboardingError(
            "proposal", "invalid_run_date", "run_date must be ISO formatted as YYYY-MM-DD"
        ) from exc
    if parsed.isoformat() != candidate:
        raise SourceOnboardingError("proposal", "invalid_run_date", "run_date must be ISO formatted as YYYY-MM-DD")
    return candidate


def validate_fixture_directory(path: Path, root: Path) -> Path:
    relative = relative_to_repo(path, root)
    expected = Path("data") / "current-weekly-pilot"
    if relative != expected:
        raise SourceOnboardingError(
            "inputs", "fixture_dir_invalid", "fixture directory must be exactly data/current-weekly-pilot"
        )
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise SourceOnboardingError("inputs", "fixture_dir_invalid", "fixture directory must be a directory")
    return path


def validate_feasibility_destination(path: Path, root: Path, issue: OnboardingIssue) -> Path:
    relative = relative_to_repo(path, root)
    if len(relative.parts) != 2 or relative.parts[0] != "docs":
        raise SourceOnboardingError(
            "feasibility",
            "feasibility_destination_invalid",
            "feasibility destination must be a Markdown file directly under docs/",
        )
    match = _FEASIBILITY_DOC_RE.fullmatch(relative.name)
    if match is None or match.group("issue") != issue.number:
        raise SourceOnboardingError(
            "feasibility",
            "feasibility_destination_invalid",
            "feasibility destination must match docs/issue-<onboarding-issue>-<market>-source-feasibility.md",
        )
    if not is_safe_regular_file(path, allow_missing=True):
        raise SourceOnboardingError(
            "feasibility", "feasibility_destination_invalid", "feasibility destination must be a safe regular file"
        )
    return path


def validate_feasibility_document(path: Path, context: OnboardingContext) -> bytes:
    try:
        content = path.read_bytes()
        text = content.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise SourceOnboardingError(
            "feasibility", "feasibility_document_invalid", "feasibility evidence must be UTF-8 text"
        ) from exc
    folded = text.casefold()
    if not text.strip() or len(text.strip()) < 80:
        raise SourceOnboardingError(
            "feasibility", "feasibility_document_incomplete", "completed feasibility evidence is required"
        )
    if any(marker in folded for marker in _PLACEHOLDER_FEASIBILITY_MARKERS):
        raise SourceOnboardingError(
            "feasibility", "feasibility_document_incomplete", "feasibility evidence is an incomplete draft"
        )
    if not context.onboarding_issue.matches(text):
        raise SourceOnboardingError(
            "feasibility", "feasibility_issue_missing", "feasibility evidence must name the onboarding issue as #<N>"
        )
    if context.url not in text:
        raise SourceOnboardingError(
            "feasibility", "feasibility_url_missing", "feasibility evidence must name the source URL"
        )
    if context.metadata["source_name"] not in text:
        raise SourceOnboardingError(
            "feasibility", "feasibility_source_missing", "feasibility evidence must name the source"
        )
    return content


def request_payload(context: OnboardingContext) -> dict[str, Any]:
    return {
        "url": context.url,
        "source_name": context.metadata["source_name"],
        "slug": context.slug,
        "metadata": dict(context.metadata),
        "reason": context.reason,
        "proposed_by": context.proposed_by,
        "access_reason": context.access_reason,
        "run_date": context.run_date,
        "onboarding_issue": context.onboarding_issue.serialize(),
        "artifact_identity": context.artifact_identity,
    }


def confirmation_token_for(payload: Mapping[str, Any]) -> str:
    """Return the deterministic token for one complete onboarding request."""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def same_request(proposal: Mapping[str, Any], context: OnboardingContext) -> bool:
    recorded = proposal.get("onboarding_request")
    return isinstance(recorded, Mapping) and dict(recorded) == request_payload(context)


def validate_source_specific_test(path: Path | None, repo_root: Path, onboarding_issue: OnboardingIssue) -> None:
    if path is None:
        return
    relative = relative_to_repo(path, repo_root)
    if len(relative.parts) != 2 or relative.parts[0] != "tests" or _SOURCE_TEST_RE.fullmatch(relative.name) is None:
        raise SourceOnboardingError(
            "inputs", "source_test_invalid", "source-specific test must be tests/test_source_<name>.py"
        )
    source_test = require_file(path, "source_test")
    try:
        module = ast.parse(source_test.read_text(encoding="utf-8"), filename=str(source_test))
    except (OSError, UnicodeError, SyntaxError, ValueError, TypeError) as exc:
        raise SourceOnboardingError(
            "inputs", "source_test_invalid", "source-specific test must be valid Python"
        ) from exc
    docstring = ast.get_docstring(module, clean=False)
    if not isinstance(docstring, str) or not docstring.strip():
        raise SourceOnboardingError(
            "inputs", "source_test_invalid", "source-specific test must have a module docstring"
        )
    if not onboarding_issue.matches(docstring):
        raise SourceOnboardingError(
            "inputs", "source_test_invalid", "source-specific test module docstring must name the onboarding issue"
        )


def validate_onboarding_artifacts(paths: Iterable[Path], repo_root: Path) -> tuple[Path, ...]:
    """Return the declared onboarding-owned files in a stable order.

    A new source can change a repository-wide pin, for example the pinned
    portfolio size or the configured-source verdict baseline. The operator
    declares each such file, and the driver validates and commits it with the
    bundle. Only existing files under ``tests/`` qualify, so a declaration can
    never carry production code, configuration, or state (Issue #664)."""
    root = repo_root.resolve()
    resolved: list[Path] = []
    for path in paths:
        ensure_not_symlink(path, "onboarding artifact", reason_code="onboarding_artifact_invalid")
        artifact = path.absolute().parent.resolve() / path.name
        relative = relative_to_repo(artifact, root)
        if len(relative.parts) < 2 or relative.parts[0] != "tests":
            raise SourceOnboardingError(
                "inputs", "onboarding_artifact_invalid", f"onboarding artifact must be a file under tests/: {relative}"
            )
        if not is_safe_regular_file(artifact):
            raise SourceOnboardingError(
                "inputs",
                "onboarding_artifact_invalid",
                f"onboarding artifact must be an existing regular file: {relative}",
            )
        if artifact in resolved:
            raise SourceOnboardingError(
                "inputs", "onboarding_artifact_invalid", f"onboarding artifact is declared more than once: {relative}"
            )
        resolved.append(artifact)
    return tuple(sorted(resolved))


def _standard_temp_alias_ancestor(ancestor: Path, requested: Path) -> bool:
    """Allow only the operating system alias chain for the temporary directory."""
    temp_root = Path(tempfile.gettempdir())
    try:
        requested.relative_to(temp_root)
    except ValueError:
        return False
    resolved_temp_root = temp_root.resolve(strict=False)
    lexical_chain = (temp_root, *temp_root.parents)
    resolved_chain = (resolved_temp_root, *resolved_temp_root.parents)
    if ancestor not in lexical_chain:
        return False
    try:
        return ancestor.resolve(strict=False) in resolved_chain
    except (OSError, RuntimeError):
        return False


def proposal_file_for(state_dir: Path) -> Path:
    """The Source Proposals file of *state_dir*, for the file-level jobs named at ``PROPOSAL_FILE_NAME``."""
    return state_dir / PROPOSAL_FILE_NAME


def proposal_state_dir(path: Path) -> Path:
    """The state directory whose Source Proposals *path* names; its content is read through that store."""
    if path.name != PROPOSAL_FILE_NAME:
        raise SourceOnboardingError(
            "inputs", "proposal_file_invalid", f"explicit proposal path must end with {PROPOSAL_FILE_NAME}"
        )
    return path.parent


def validate_proposal_file(path: Path | None) -> None:
    if path is None:
        return
    proposal_state_dir(path)

    requested = path if path.is_absolute() else Path.cwd() / path
    for ancestor in (requested, *requested.parents):
        if ancestor.is_symlink() and (ancestor == requested or not _standard_temp_alias_ancestor(ancestor, requested)):
            raise SourceOnboardingError(
                "inputs", "proposal_file_invalid", "proposal path must not use a symlink or symlink ancestor"
            )
        if ancestor != requested and ancestor.exists() and not ancestor.is_dir():
            raise SourceOnboardingError("inputs", "proposal_file_invalid", "proposal path parent must be a directory")

    if not is_safe_regular_file(requested, allow_missing=True):
        raise SourceOnboardingError("inputs", "proposal_file_invalid", "proposal path must be a safe regular file")


def validate_request_inputs(*, url: str | None, probe_evidence: Path | None) -> Path:
    if not url or not valid_url(url):
        raise SourceOnboardingError("probe", "url_required", "one valid HTTP(S) URL is required")
    if probe_evidence is None:
        raise SourceOnboardingError(
            "probe", "probe_evidence_missing", "accepted retained probe evidence must be provided"
        )
    try:
        return require_file(probe_evidence, "probe_evidence")
    except SourceOnboardingError as exc:
        if exc.reason_code == "probe_evidence_missing":
            raise SourceOnboardingError("probe", "probe_evidence_missing", exc.message) from exc
        raise


def file_identity(path: Path) -> dict[str, str]:
    try:
        content = path.read_bytes()
    except (OSError, UnicodeError) as exc:
        raise SourceOnboardingError("inputs", "artifact_unreadable", f"cannot read artifact: {path}") from exc
    return {"path": str(path), "sha256": hashlib.sha256(content).hexdigest()}


def proposal_state_identity(path: Path) -> dict[str, Any]:
    identity: dict[str, Any] = {"path": str(path), "present": path.exists(), "sha256": None}
    if identity["present"]:
        try:
            content = path.read_bytes()
        except (OSError, UnicodeError) as exc:
            raise SourceOnboardingError("inputs", "artifact_unreadable", f"cannot read artifact: {path}") from exc
        identity["sha256"] = hashlib.sha256(content).hexdigest()
    return identity


def artifact_identity(canonical: CanonicalPaths, inputs: Mapping[str, Path]) -> dict[str, Any]:
    helper = file_identity(inputs["helper_input"])
    helper["destination"] = str(canonical.helper_metadata)
    feasibility = file_identity(inputs["feasibility_input"])
    feasibility["destination"] = str(canonical.feasibility_doc)
    identity: dict[str, Any] = {
        "probe_evidence": file_identity(inputs["probe_evidence"]),
        "helper_metadata": helper,
        "feasibility_evidence": feasibility,
        "baseline_configs": [file_identity(path) for path in canonical.configs],
        "source_specific_test": None,
        "onboarding_artifacts": [file_identity(path) for path in canonical.onboarding_artifacts],
        "proposal_state": proposal_state_identity(canonical.proposal_file),
    }
    if canonical.source_specific_test is not None:
        identity["source_specific_test"] = file_identity(canonical.source_specific_test)
    return identity


def validate_existing_proposal(proposal: Mapping[str, Any], context: OnboardingContext) -> None:
    source_url = str(proposal.get("source_url", "")).strip()
    normalized = str(proposal.get("normalized_source", "")).strip()
    if not source_url or normalize_source_identity(source_url) != normalize_source_identity(context.url):
        raise SourceOnboardingError(
            "proposal", "proposal_identity_mismatch", "Source Proposal URL does not match the request"
        )
    if normalized and normalized != normalize_source_identity(source_url):
        raise SourceOnboardingError(
            "proposal", "proposal_identity_mismatch", "Source Proposal normalized URL is inconsistent"
        )
    status = str(proposal.get("status", "")).strip().casefold()
    if status == "accepted":
        raise SourceOnboardingError(
            "proposal", "proposal_already_accepted", "Source Proposal has already been accepted and onboarded"
        )
    if status in {"rejected", "infeasible"}:
        raise SourceOnboardingError("proposal", "proposal_not_acceptable", f"Source Proposal status is {status}")
    if status != "proposed":
        raise SourceOnboardingError("proposal", "proposal_not_acceptable", "Source Proposal status is not proposed")
    source_name = str(proposal.get("source_name", "")).strip()
    if source_name and source_name != context.metadata["source_name"]:
        raise SourceOnboardingError(
            "proposal", "proposal_metadata_mismatch", "Source Proposal source name does not match the request"
        )
    signal_family = str(proposal.get("suggested_signal_family", "")).strip()
    if signal_family and signal_family != context.metadata["signal_family"]:
        raise SourceOnboardingError(
            "proposal", "proposal_metadata_mismatch", "Source Proposal signal family does not match the request"
        )
