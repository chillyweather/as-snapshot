"""Zero-mutation rollover proposals for annual sources (Issue #680).

When the evidence audit finds a newer edition or a newer award stage, the
operator needs to know exactly what a source change would be and what it
would take, before deciding anything. This module turns one verdict into one
bounded proposal: the old and proposed identity, URL, edition year and stage;
how the record would be handled; the official evidence; and the validation
work this repository's real wiring requires for that source.

A proposal is printed and never applied. It writes nothing, and its last two
fields say so (``mutation=none``, ``confirmation_required=true``). Applying
one stays an explicit, confirmed, one-source-at-a-time change made through
``docs/source-onboarding-runbook.md``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from autoscout.annual_edition import AnnualEdition
from autoscout.annual_edition import parse_annual_edition
from autoscout.annual_source_audit import ROLLOVER_AVAILABLE
from autoscout.annual_source_audit import STABLE_URL_NEW_EDITION
from autoscout.annual_source_audit import STAGE_REVIEW_REQUIRED
from autoscout.annual_source_audit import AnnualSourceVerdict

KIND_EDITION_ROLLOVER = "edition-rollover"
KIND_STABLE_URL_REFRESH = "stable-url-edition-refresh"
KIND_STAGE_CHANGE = "stage-change"
_KINDS = {
    ROLLOVER_AVAILABLE: KIND_EDITION_ROLLOVER,
    STABLE_URL_NEW_EDITION: KIND_STABLE_URL_REFRESH,
    STAGE_REVIEW_REQUIRED: KIND_STAGE_CHANGE,
}

RETIRE_OLD_AND_ADD_NEW = "retire-old-and-add-new"
RENAME_IN_PLACE = "rename-in-place"
UPDATE_IN_PLACE = "update-in-place"

# The word a source name uses for each stage. ``list`` is absent on purpose:
# it is too common a word to be read as a stage in a name.
STAGE_NAME_WORDS: dict[str, str] = {
    "longlist": "Longlist",
    "semifinalists": "Semifinalists",
    "shortlist": "Shortlist",
    "finalists": "Finalists",
    "nominees": "Nominees",
    "winner": "Winner",
}

BENCHMARK_MANIFEST = "data/model-qualification/benchmark/manifest.json"
OVERLAY_CONFIGS = (
    "configs/current-weekly-pilot.json",
    "configs/current-weekly-pilot-live.json",
    "configs/current-weekly-live.json",
)
DISPLAY_LABELS = "autoscout/html_review.py"

# Source fields that are identity, bookkeeping or wiring rather than page
# shape; each is either reported elsewhere in the proposal or not a contract.
_NOT_PAGE_SHAPE = frozenset(
    {
        "name",
        "portfolio_name",
        "url",
        "annual_edition",
        "notes",
        "lifecycle_status",
        "lifecycle_note",
        "retired_at",
        "retired_by",
        "retirement_reason",
    }
)


@dataclass(frozen=True)
class RepositoryWiring:
    """What the repository wires to source names, read once by the command."""

    benchmark_cases: frozenset[str] = frozenset()
    benchmark_uncovered: frozenset[str] = frozenset()
    overlays: tuple[tuple[str, Mapping[str, Mapping[str, Any]]], ...] = ()
    display_names: frozenset[str] = frozenset()
    test_references: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceState:
    name: str
    url: str
    edition_year: int
    stage: str


@dataclass(frozen=True)
class RolloverProposal:
    kind: str
    old: SourceState
    new: SourceState
    stage_basis: str
    record_handling: str
    record_handling_reason: str
    record_handling_alternative: str
    evidence: tuple[str, ...]
    validation: tuple[str, ...]
    alternatives: tuple[str, ...]


# ── Pure parts ──────────────────────────────────────────────────────────────


def proposed_name(name: str, old: SourceState, new_year: int, new_stage: str) -> str:
    """Replace the year where the name carries it and the stage word where it carries that."""
    result = name
    if new_year != old.edition_year:
        result = re.sub(rf"(?<!\d){old.edition_year}(?!\d)", str(new_year), result)
    old_word, new_word = STAGE_NAME_WORDS.get(old.stage), STAGE_NAME_WORDS.get(new_stage)
    if new_stage != old.stage and old_word and new_word:

        def keep_case(match: re.Match[str]) -> str:
            return new_word.lower() if match.group(0).islower() else new_word

        result = re.sub(rf"\b{old_word}\b", keep_case, result, flags=re.IGNORECASE)
    return result


def describe_evidence(verdict: AnnualSourceVerdict) -> list[str]:
    """One line per observed page: what was requested, what answered, what it showed."""
    if verdict.evidence is None:
        return []
    lines = []
    for observation in verdict.evidence.observations:
        checks = [check for check in verdict.marker_checks if check.role == observation.role]
        marker_text = (
            "; ".join(f"{check.kind}_marker={check.marker!r} found={str(check.found).lower()}" for check in checks)
            or "markers_checked=none"
        )
        lines.append(
            f"role={observation.role}; requested_url={observation.requested_url}; "
            f"status={observation.status if observation.status is not None else 'none'}; "
            f"resolved_url={observation.resolved_url or 'none'}; "
            f"stayed_on_requested_url={str(observation.stayed_on_requested_url).lower()}; {marker_text}; "
            f"body_sha256={observation.body_sha256 or 'none'}; fetched_at={observation.fetched_at or 'none'}"
            + (f"; error={observation.error}" if observation.error else "")
        )
    return lines


def _record_handling(old: SourceState, new: SourceState, wiring: RepositoryWiring) -> tuple[str, str, str]:
    if new.name == old.name:
        return (
            UPDATE_IN_PLACE,
            "the source name does not change, so every reference to it, including any benchmark case, stays valid",
            "",
        )
    if old.name in wiring.benchmark_cases:
        return (
            RETIRE_OLD_AND_ADD_NEW,
            f"a frozen model-qualification benchmark case names {old.name!r} ({BENCHMARK_MANIFEST}); "
            "a frozen case stays valid only while its source is configured, so the old record is retired, "
            "not renamed, and a new record is added",
            f"{RENAME_IN_PLACE} and remove the benchmark case, lowering the benchmark floor (operator decision, "
            "as in #762 and #826)",
        )
    return (
        RENAME_IN_PLACE,
        f"no frozen model-qualification benchmark case names {old.name!r}",
        "",
    )


def _carries(value: Any, year: int | None, stage: str) -> bool:
    if isinstance(value, list):
        return any(_carries(item, year, stage) for item in value)
    if year is not None and isinstance(value, int) and not isinstance(value, bool):
        return value == year
    if not isinstance(value, str):
        return False
    if year is not None and re.search(rf"(?<!\d){year}(?!\d)", value):
        return True
    if stage:
        stem = stage.removesuffix("s")
        return re.search(rf"\b{re.escape(stem)}s?\b", value, flags=re.IGNORECASE) is not None
    return False


def _page_shape_fields(source: Mapping[str, Any], old: SourceState, new: SourceState) -> list[str]:
    year = old.edition_year if new.edition_year != old.edition_year else None
    stage = old.stage if new.stage != old.stage and old.stage != "list" else ""
    return [
        f"page shape: update {key}={value!r}"
        for key, value in source.items()
        if key not in _NOT_PAGE_SHAPE and not key.endswith(("_file", "_sha256")) and _carries(value, year, stage)
    ]


def _validation_work(
    source: Mapping[str, Any],
    declared: AnnualEdition,
    old: SourceState,
    new: SourceState,
    record_handling: str,
    wiring: RepositoryWiring,
) -> tuple[str, ...]:
    work = []
    if new.url == old.url:
        work.append(f"feasibility: same URL; re-probe it as it now reads: ./scripts/probe-source --url {old.url}")
    elif new.url.startswith("https://"):
        work.append(f"feasibility: probe the new URL before any change: ./scripts/probe-source --url {new.url}")
    else:
        work.append(
            f"feasibility: locate the edition {new.edition_year} page from {declared.landing_url}, "
            "then probe it with ./scripts/probe-source --url <page>"
        )
    for label, entries in wiring.overlays:
        entry = entries.get(old.name)
        if entry is None:
            continue
        files = [f"{key}={value}" for key, value in entry.items() if key.endswith("_file")]
        line = f"overlay: {label} entry {old.name!r}"
        line += f" -> {new.name!r}" if new.name != old.name else " (name unchanged)"
        if files:
            line += "; refresh before live collection: " + ", ".join(files)
        work.append(line)
    if new.name != old.name and old.name in wiring.display_names:
        work.append(f"display label: {DISPLAY_LABELS} maps {old.name!r}; add {new.name!r}")
    if source.get("helper_metadata_file"):
        work.append(
            f"helper metadata: refresh {source['helper_metadata_file']} and its helper_metadata_sha256 "
            f"{source.get('helper_metadata_sha256', 'none')} in the portfolio and every overlay"
        )
    work.extend(_page_shape_fields(source, old, new))
    work.append(
        f"annual_edition: edition_year {old.edition_year} -> {new.edition_year}, "
        f"tracked_stage {old.stage} -> {new.stage}; check edition_marker, next_edition_url and stage_markers"
    )
    work.append(f"benchmark: {record_handling}; see record_handling_reason")
    if new.name != old.name and old.name in wiring.benchmark_uncovered:
        work.append(f"benchmark: {BENCHMARK_MANIFEST} lists {old.name!r} as uncovered; list {new.name!r} instead")
    references = wiring.test_references.get(old.name, ())
    if references:
        work.append(f"tests naming {old.name!r}: " + ", ".join(references))
    work.append("suite: ./scripts/check")
    work.append("persistence: commit and push the confirmed source change (canonical persistence)")
    work.append(
        "Candidate History: books already collected keep their identities; a Candidate identity is the book's "
        "title and authors, not the source name"
    )
    return tuple(work)


def build_proposal(
    source: Mapping[str, Any], verdict: AnnualSourceVerdict, wiring: RepositoryWiring
) -> RolloverProposal | None:
    """Return the proposal a verdict calls for, or ``None`` when it calls for none.

    An edition proposal for a source with no ``stage_markers`` (one URL carries the whole cycle) keeps the
    tracked stage; with ``stage_markers`` it proposes the first stage unless the page shows a later one.
    """
    kind = _KINDS.get(verdict.verdict)
    declared = parse_annual_edition(source)
    if kind is None or declared is None:
        return None
    old = SourceState(verdict.source_name, verdict.url, declared.edition_year, declared.tracked_stage)
    if kind == KIND_STAGE_CHANGE:
        new_year, new_stage, basis = old.edition_year, verdict.observed_stage, "the newest stage shown on the page"
        new_url = old.url
    else:
        new_year = verdict.observed_edition_year or old.edition_year + 1
        if verdict.observed_stage:
            new_stage, basis = verdict.observed_stage, "the newest stage shown on the new edition's page"
        elif declared.stage_markers:
            new_stage, basis = declared.stages[0], "the first declared stage; the page shows no later stage marker"
        else:
            new_stage = old.stage
            basis = "the tracked stage, kept: the source declares no stage_markers, so one URL carries the whole cycle"
        new_url = verdict.proposed_url or f"to be located from the landing page {declared.landing_url}"
    new = SourceState(proposed_name(old.name, old, new_year, new_stage), new_url, new_year, new_stage)
    handling, reason, alternative = _record_handling(old, new, wiring)
    alternatives: tuple[str, ...] = ()
    if kind == KIND_STAGE_CHANGE:
        alternatives = (
            f"move the source to the {new_stage} stage (this proposal)",
            f"keep tracking the {old.stage} stage",
            "retire the source until the next edition",
        )
    return RolloverProposal(
        kind=kind,
        old=old,
        new=new,
        stage_basis=basis,
        record_handling=handling,
        record_handling_reason=reason,
        record_handling_alternative=alternative,
        evidence=tuple(describe_evidence(verdict)),
        validation=_validation_work(source, declared, old, new, handling, wiring),
        alternatives=alternatives,
    )


def render_proposal(proposal: RolloverProposal) -> list[str]:
    """The proposal as bounded ``key=value`` lines."""
    lines = [f"proposal_begin source={proposal.old.name}", f"proposal.kind={proposal.kind}"]
    for label, state in (("old", proposal.old), ("new", proposal.new)):
        lines += [
            f"proposal.{label}.name={state.name}",
            f"proposal.{label}.url={state.url}",
            f"proposal.{label}.edition_year={state.edition_year}",
            f"proposal.{label}.stage={state.stage}",
        ]
    lines.append(f"proposal.new.stage_basis={proposal.stage_basis}")
    lines.append(f"proposal.record_handling={proposal.record_handling}")
    lines.append(f"proposal.record_handling_reason={proposal.record_handling_reason}")
    if proposal.record_handling_alternative:
        lines.append(f"proposal.record_handling_alternative={proposal.record_handling_alternative}")
    lines += [f"proposal.evidence={line}" for line in proposal.evidence]
    lines += [f"proposal.validation={line}" for line in proposal.validation]
    lines += [f"proposal.alternative={line}" for line in proposal.alternatives]
    lines += [
        "proposal.mutation=none",
        "proposal.confirmation_required=true",
        f"proposal_end source={proposal.old.name}",
    ]
    return lines


# ── Reading the repository's wiring ─────────────────────────────────────────


def _overlay_entries(path: Path) -> Mapping[str, Mapping[str, Any]]:
    from autoscout.json_io import read_json

    document = read_json(path, default=None)
    sources = document.get("sources") if isinstance(document, dict) else None
    entries: dict[str, Mapping[str, Any]] = {}
    for entry in sources if isinstance(sources, list) else []:
        if isinstance(entry, dict):
            name = str(entry.get("portfolio_name") or entry.get("name", "")).strip()
            if name:
                entries[name] = entry
    return entries


def _test_references(root: Path, names: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """Test modules and top-level test fixtures that spell a source name out."""
    found: dict[str, list[str]] = {name: [] for name in names}
    candidates = sorted((root / "tests").glob("*.py")) + sorted((root / "tests" / "fixtures").glob("*.json"))
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for name in names:
            if name in text:
                found[name].append(path.relative_to(root).as_posix())
    return {name: tuple(paths) for name, paths in found.items()}


def load_repository_wiring(root: Path, names: Sequence[str]) -> RepositoryWiring:
    """Read the benchmark manifest, the overlays, the display labels and the tests. Read-only."""
    from autoscout.html_review import SOURCE_SHORT_NAMES
    from autoscout.json_io import read_json

    manifest = read_json(root / BENCHMARK_MANIFEST, default=None)
    manifest = manifest if isinstance(manifest, dict) else {}

    def manifest_names(key: str) -> frozenset[str]:
        items = manifest.get(key)
        return (
            frozenset(str(item.get("source_name", "")) for item in items if isinstance(item, dict))
            if isinstance(items, list)
            else frozenset()
        )

    return RepositoryWiring(
        benchmark_cases=manifest_names("cases"),
        benchmark_uncovered=manifest_names("uncovered"),
        overlays=tuple((label, _overlay_entries(root / label)) for label in OVERLAY_CONFIGS),
        display_names=frozenset(SOURCE_SHORT_NAMES),
        test_references=_test_references(root, names),
    )
