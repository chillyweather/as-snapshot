"""Qualify a Codex model for grounded description synthesis (Issue #843).

Description synthesis is the Codex call that writes the Russian Content
Description and its English Description, and it runs on no pinned model: it
inherits the operator's global Codex default.  On 2026-10-15 that default was
``gpt-6-luna`` while extraction ran on the pinned ``gpt-5.6-luna``, so the two
stages were written by different models and nothing recorded which model wrote
the descriptions.  Pinning needs a value, and choosing one needs evidence a
weekly run cannot give: it mixes discovery, fetching, source policy and the
model, runs each candidate once, and writes state.

Qualification replays a frozen benchmark of synthesis work items through the
production synthesis prompt and the production description gate, several
times per candidate model and effort, and writes one comparison report with a
blind side-by-side sheet for human rating.  It has no network access apart
from the Codex model calls themselves, writes nothing outside its report
directory, never changes a pin, and never calls Git.

The benchmark is rebuilt only by an explicit step that reads committed
``grounded-description-stage.json`` artifacts from Git history.  Each outcome
there keeps the candidate identity and its bounded research source records,
which is exactly what the synthesizer reads, so no page is fetched again.
Every load rebuilds the work item from that evidence and refuses an item that
no longer matches the one production would send.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Protocol

from autoscout.bounded_workers import DaemonThreadPool
from autoscout.codex_features import EXTRACTION_DISABLED_FEATURES
from autoscout.codex_model_pin import DEFAULT_BACKEND
from autoscout.codex_model_pin import CodexModelPin
from autoscout.codex_model_pin import ModelPinError
from autoscout.codex_model_pin import header_profile
from autoscout.codex_model_pin import model_unavailable_in
from autoscout.codex_model_pin import parse_model_pin
from autoscout.description_depth import DEPTH_MIN_CHARS
from autoscout.description_depth import assess_description_depth
from autoscout.grounded_description import CANDIDATE_ENVELOPE_SECONDS
from autoscout.grounded_description import GROUNDED_SYNTHESIS_PROMPT
from autoscout.grounded_description import MAX_WORKERS
from autoscout.grounded_description import OUTCOME_GROUNDED
from autoscout.grounded_description import OUTCOME_SINGLE_SOURCE
from autoscout.grounded_description import REASON_INSUFFICIENT_DEPTH
from autoscout.grounded_description import REASON_INSUFFICIENT_SUBJECT
from autoscout.grounded_description import REASON_INVALID_DESCRIPTION
from autoscout.grounded_description import AcceptedSource
from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import StageArtifact
from autoscout.grounded_description import SynthesisResult
from autoscout.grounded_description import _discovery_ordering_key
from autoscout.grounded_description import _source_sort_key
from autoscout.grounded_description import _validated_english
from autoscout.grounded_description import grounded_synthesis_work_item
from autoscout.grounded_description import screen_synthesized_description
from autoscout.grounded_description import synthesis_result_from_response
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.model_client import ClaudeApiClient
from autoscout.model_client import CodexCliClient
from autoscout.model_client import ModelCallStatus
from autoscout.model_client import ModelFailure
from autoscout.model_client import ModelRequest
from autoscout.model_qualification import _percentile
from autoscout.model_qualification import _slug
from autoscout.repository_root import repository_root
from autoscout.state_store import StateStoreError
from autoscout.state_store.layout import committed_candidate_history

REPOSITORY_ROOT = repository_root()
DEFAULT_BENCHMARK_DIR = REPOSITORY_ROOT / "data" / "description-qualification" / "benchmark"
DEFAULT_REPORTS_DIR = REPOSITORY_ROOT / "data" / "description-qualification" / "reports"
WEEKLY_RUN_DIR = "runs/current-weekly-pilot-russian"
STAGE_ARTIFACT = "grounded-description-stage.json"

MANIFEST_NAME = "manifest.json"
REPORT_JSON = "description-qualification.json"
REPORT_MARKDOWN = "description-qualification.md"
BLIND_SHEET = "blind-review.md"
BLIND_KEY = "blind-review-key.json"
BLIND_RATINGS = "blind-review-ratings.json"
MANIFEST_VERSION = 1
REPORT_VERSION = 1

DEFAULT_REPETITIONS = 3
DEFAULT_WORKERS = MAX_WORKERS
# The synthesis agent's own ceiling. A call is measured to completion, not
# cut at the envelope, so a slow candidate shows its real p95.
DEFAULT_PER_CALL_TIMEOUT_SECONDS = 180.0
DURATION_ENVELOPE_SECONDS = CANDIDATE_ENVELOPE_SECONDS
VALIDITY_MARGIN = 0.05
MIN_HONEST_DECLINE_RATE = 0.90
HISTORY_DEPTH = 60

# Benchmark composition. The quotas are minimums filled in this order; the
# rest of the set is filled with substantive items, newest first.
BENCHMARK_TARGET_SIZE = 36
THIN_QUOTA = 8
PROMINENCE_QUOTA = 6
LANGUAGE_QUOTA = 2
SOURCE_COUNT_QUOTA = 6
LANGUAGE_GROUPS: tuple[tuple[str, frozenset[str]], ...] = (
    ("de", frozenset({"de"})),
    ("es", frozenset({"es"})),
    ("fr", frozenset({"fr"})),
    ("nordic", frozenset({"no", "nb", "nn", "sv", "da", "fi", "is"})),
    ("cs_or_pl", frozenset({"cs", "pl"})),
    ("nl", frozenset({"nl"})),
)

CLASS_SUBSTANTIVE = "substantive"
CLASS_THIN = "thin_evidence"
SUBSTANTIVE_OUTCOMES = frozenset({OUTCOME_GROUNDED, OUTCOME_SINGLE_SOURCE})
# Refusals that judged the description, so their evidence passed the gate
# and was kept. They join the pool beside the accepted outcomes.
THIN_REASONS = frozenset({REASON_INSUFFICIENT_DEPTH, REASON_INSUFFICIENT_SUBJECT, REASON_INVALID_DESCRIPTION})

HISTORY_COLUMN = "candidate_history"
FAITHFULNESS_VALUES = ("faithful", "minor_issue", "unfaithful")
RATING_RANGE = range(1, 6)

VERDICT_ELIGIBLE = "eligible"
VERDICT_PENDING = "pending_blind_review"
VERDICT_INELIGIBLE = "ineligible"

EXIT_OK = 0
EXIT_NONE_ELIGIBLE = 1
EXIT_INPUT_ERROR = 2


class QualificationInputError(ValueError):
    """The benchmark, report, ratings, or request cannot be used."""


def parse_candidate(value: str) -> CodexModelPin:
    """Return the backend, model and effort named by ``[backend/]model:effort``.

    Without a backend the candidate runs on the Codex CLI; ``claude-api/``
    runs it on the Claude API (Issue #1149).
    """
    backend, slash, rest = str(value).strip().partition("/")
    if not slash:
        backend, rest = DEFAULT_BACKEND, backend
    model, separator, effort = rest.rpartition(":")
    if not separator or not model or not effort:
        raise QualificationInputError(f"candidate must be [backend/]model:effort, got {value!r}")
    entry = {"model": model, "reasoning_effort": effort, "backend": backend}
    try:
        return parse_model_pin({"synthesis": entry}, key="synthesis")
    except ModelPinError as exc:
        raise QualificationInputError(str(exc)) from exc


def candidate_label(pin: CodexModelPin) -> str:
    label = f"{pin.model}:{pin.reasoning_effort}"
    return label if pin.backend == DEFAULT_BACKEND else f"{pin.backend}/{label}"


# -- Benchmark ---------------------------------------------------------------


def _item_slug(identity_key: str) -> str:
    # Titles repeat across editions, so the slug carries a digest of the key.
    digest = hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:8]
    return f"{_slug(identity_key.partition('|')[0])[:60]}-{digest}"


def _work_item_digest(work_item: Mapping[str, Any]) -> str:
    encoded = json.dumps(work_item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def sources_from_records(records: Sequence[Mapping[str, Any]]) -> tuple[AcceptedSource, ...]:
    """Parse stored research source records exactly as the stage artifact does."""
    artifact = StageArtifact.from_mapping({"outcomes": [{"research_source_records": list(records)}]})
    return artifact.outcomes[0].research_sources if artifact.outcomes else ()


def synthesis_sources_from_records(records: Sequence[Mapping[str, Any]]) -> tuple[AcceptedSource, ...]:
    """Restore the gate's synthesis order, before artifact discovery ordering.

    The production fetcher derives source_preference from source_kind, which
    the gate's shared sort key also does when no explicit preference is set.
    All multi-source gate paths use this order; singletons are unchanged.
    """
    return tuple(
        sorted(
            sources_from_records(records),
            key=lambda source: _source_sort_key(FetchedSource.from_mapping(source.to_dict()), source.canonical_url),
        )
    )


def _ordered(sources: Sequence[AcceptedSource]) -> tuple[AcceptedSource, ...]:
    return tuple(
        sorted(
            sources,
            key=lambda source: _discovery_ordering_key(
                source.relevance_tier, source.source_kind, source.discovery_ordinal, source.canonical_url
            ),
        )
    )


def evidence_chars(sources: Sequence[AcceptedSource]) -> int:
    """Return the normalized length of the distinct evidence text."""
    texts = dict.fromkeys(source.display_text.strip() for source in sources if source.display_text.strip())
    return int(assess_description_depth(" ".join(texts))["char_count"])


def evidence_class(sources: Sequence[AcceptedSource]) -> str:
    """Flag short evidence for human review without judging its descriptions."""
    return CLASS_THIN if evidence_chars(sources) < DEPTH_MIN_CHARS else CLASS_SUBSTANTIVE


@dataclass(frozen=True)
class BenchmarkItem:
    slug: str
    identity_key: str
    commit: str
    item_class: str
    source_count: int
    languages: tuple[str, ...]
    has_prominence_claim: bool
    work_item_sha256: str
    path: Path

    def document(self) -> dict[str, Any]:
        document = read_json(self.path, default=None)
        if not isinstance(document, dict) or not isinstance(document.get("research_source_records"), list):
            raise QualificationInputError(f"benchmark item is unreadable: {self.slug}")
        return document


@dataclass(frozen=True)
class LoadedItem:
    """One benchmark item with its evidence parsed and its work item proven."""

    item: BenchmarkItem
    candidate: dict[str, Any]
    sources: tuple[AcceptedSource, ...]
    work_item: dict[str, Any]
    history_description: str

    @property
    def slug(self) -> str:
        return self.item.slug

    @property
    def thin(self) -> bool:
        return self.item.item_class == CLASS_THIN

    def target(self) -> DurableTarget:
        authors = self.candidate.get("primary_authors") or []
        return DurableTarget(
            candidate_id=str(self.candidate.get("candidate_id", "")),
            title=str(self.candidate.get("title", "")),
            primary_authors=tuple(str(author) for author in authors),
            record=self.candidate,
        )


def load_item(item: BenchmarkItem) -> LoadedItem:
    """Rebuild the production work item from the frozen evidence and check it.

    A mismatch means production now builds another work item from the same
    evidence, so a replay would measure a prompt nobody sends.
    """
    document = item.document()
    candidate = document.get("candidate")
    if not isinstance(candidate, dict) or candidate.get("identity_key") != item.identity_key:
        raise QualificationInputError(f"benchmark item names another candidate: {item.slug}")
    sources = synthesis_sources_from_records(document["research_source_records"])
    if not sources:
        raise QualificationInputError(f"benchmark item has no evidence: {item.slug}")
    if evidence_class(sources) != item.item_class:
        raise QualificationInputError(f"benchmark item class no longer matches its evidence: {item.slug}")
    work_item = grounded_synthesis_work_item(candidate, sources)
    if work_item != document.get("work_item") or _work_item_digest(work_item) != item.work_item_sha256:
        raise QualificationInputError(
            f"benchmark item no longer matches the production work item: {item.slug}; "
            "refresh it with --refresh-benchmark"
        )
    return LoadedItem(
        item=item,
        candidate=dict(candidate),
        sources=sources,
        work_item=work_item,
        history_description=str(document.get("history_description", "") or ""),
    )


def manifest_digest(benchmark_dir: Path) -> str:
    return hashlib.sha256((benchmark_dir / MANIFEST_NAME).read_bytes()).hexdigest()


def load_benchmark(benchmark_dir: Path = DEFAULT_BENCHMARK_DIR) -> list[BenchmarkItem]:
    """Read the frozen benchmark manifest and return its items."""
    manifest = read_json(benchmark_dir / MANIFEST_NAME, default=None)
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        raise QualificationInputError(
            "description qualification benchmark is missing; refresh it with --refresh-benchmark"
        )
    items: list[BenchmarkItem] = []
    for entry in manifest.get("items", []):
        if not isinstance(entry, dict):
            raise QualificationInputError("benchmark manifest holds a non-object item")
        slug = str(entry.get("slug", ""))
        if not slug or "/" in slug or slug.startswith("."):
            raise QualificationInputError(f"benchmark item has an unsafe slug: {slug!r}")
        items.append(
            BenchmarkItem(
                slug=slug,
                identity_key=str(entry["identity_key"]),
                commit=str(entry.get("commit", "")),
                item_class=str(entry["item_class"]),
                source_count=int(entry["source_count"]),
                languages=tuple(str(language) for language in entry.get("languages", [])),
                has_prominence_claim=bool(entry.get("has_prominence_claim", False)),
                work_item_sha256=str(entry["work_item_sha256"]),
                path=benchmark_dir / "items" / f"{slug}.json",
            )
        )
    return items


def _git_show(repo_root: Path, commit: str, path: str) -> bytes | None:
    completed = subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def _git_commits(repo_root: Path, path: str, depth: int) -> list[str]:
    completed = subprocess.run(
        ["git", "log", f"-{depth}", "--format=%H", "--", path],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _json_at(git_show: Callable[[Path, str, str], bytes | None], repo_root: Path, commit: str, path: str) -> Any:
    raw = git_show(repo_root, commit, path)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _history_at(git_show: Callable[[Path, str, str], bytes | None], repo_root: Path, commit: str, run_dir: str) -> Any:
    """Candidate History of the commit, one file or one file per book; ``None`` when unreadable."""
    try:
        return committed_candidate_history(repo_root, commit, run_dir, show=git_show)
    except StateStoreError:
        return None


def _history_index(document: Any) -> dict[str, Mapping[str, Any]]:
    records = document.get("candidates", []) if isinstance(document, dict) else document
    if not isinstance(records, list):
        return {}
    return {
        str(record.get("identity_key", "")): record
        for record in records
        if isinstance(record, Mapping) and str(record.get("identity_key", "")).strip()
    }


@dataclass
class _PoolEntry:
    order: int
    identity_key: str
    commit: str
    operation_date: str
    item_class: str
    outcome: Mapping[str, Any]
    candidate: dict[str, Any]
    records: list[dict[str, Any]]
    evidence_chars: int
    languages: tuple[str, ...]
    has_prominence_claim: bool
    history_description: str = ""
    tags: set[str] = field(default_factory=set)


def _pool_entry(
    order: int,
    commit: str,
    operation_date: str,
    outcome: Mapping[str, Any],
    history: Mapping[str, Mapping[str, Any]],
) -> _PoolEntry | None:
    if outcome.get("outcome") not in SUBSTANTIVE_OUTCOMES and outcome.get("reason_code") not in THIN_REASONS:
        return None
    raw_records = outcome.get("research_source_records")
    if not isinstance(raw_records, list):
        return None
    records = [dict(record) for record in raw_records if isinstance(record, Mapping)]
    if not records or not any(str(record.get("display_text", "")).strip() for record in records):
        return None
    identity_key = str(outcome.get("identity_key", "")).strip()
    record = history.get(identity_key)
    if not identity_key or record is None or not str(record.get("title", "")).strip():
        return None
    authors = record.get("primary_authors") or []
    if isinstance(authors, str):
        authors = [authors]
    candidate = {
        "candidate_id": str(record.get("candidate_id", "") or outcome.get("candidate_id", "")),
        "identity_key": identity_key,
        "title": str(record.get("title", "")),
        "primary_authors": [str(author) for author in authors if str(author).strip()],
    }
    languages = tuple(sorted({str(item.get("source_language", "")).strip().casefold() for item in records} - {""}))
    sources = sources_from_records(records)
    return _PoolEntry(
        order=order,
        identity_key=identity_key,
        commit=commit,
        operation_date=operation_date,
        item_class=evidence_class(sources),
        outcome=outcome,
        candidate=candidate,
        records=records,
        evidence_chars=evidence_chars(sources),
        languages=languages,
        has_prominence_claim=bool(outcome.get("prominence_claims")),
    )


def select_benchmark(pool: Sequence[_PoolEntry], target_size: int = BENCHMARK_TARGET_SIZE) -> list[_PoolEntry]:
    """Choose a deliberately mixed benchmark from the pool, deterministically.

    Thin-evidence items come first, the thinnest evidence of each historic
    outcome in turn, then items carrying a prominence claim, then each
    evidence language, then single- and multi-source evidence; the newest
    substantive items fill the rest.
    """
    selected: list[_PoolEntry] = []
    chosen: set[str] = set()

    def take(candidates: Iterable[_PoolEntry], quota: int, tag: str) -> None:
        candidates = list(candidates)
        have = sum(1 for entry in selected if entry in candidates)
        for entry in candidates:
            if have >= quota or len(selected) >= target_size:
                break
            if entry.identity_key in chosen:
                continue
            selected.append(entry)
            chosen.add(entry.identity_key)
            entry.tags.add(tag)
            have += 1

    newest = sorted(pool, key=lambda entry: entry.order)
    substantive = [entry for entry in newest if entry.item_class == CLASS_SUBSTANTIVE]
    # The historic outcomes take turns, each thinnest first, so thin evidence
    # an earlier model declined, padded below the floor, or described is all
    # in the set.
    by_reason: dict[str, list[_PoolEntry]] = {}
    for entry in sorted(newest, key=lambda entry: (entry.evidence_chars, entry.order)):
        if entry.item_class == CLASS_THIN:
            by_reason.setdefault(str(entry.outcome.get("reason_code", "")), []).append(entry)
    thin = sorted(
        (entry for group in by_reason.values() for entry in group),
        key=lambda entry: (by_reason[str(entry.outcome.get("reason_code", ""))].index(entry), entry.order),
    )
    take(thin, THIN_QUOTA, "thin_evidence")
    take((entry for entry in substantive if entry.has_prominence_claim), PROMINENCE_QUOTA, "prominence_claim")
    for name, languages in LANGUAGE_GROUPS:
        take((entry for entry in substantive if languages & set(entry.languages)), LANGUAGE_QUOTA, f"language_{name}")
    take((entry for entry in substantive if len(entry.records) == 1), SOURCE_COUNT_QUOTA, "single_source")
    take((entry for entry in substantive if len(entry.records) >= 2), SOURCE_COUNT_QUOTA, "multi_source")
    take(substantive, target_size, "fill")
    return selected


def _coverage(selected: Sequence[_PoolEntry]) -> tuple[dict[str, Any], list[str]]:
    languages: dict[str, int] = {}
    for entry in selected:
        for language in entry.languages or ("unknown",):
            languages[language] = languages.get(language, 0) + 1
    substantive = [entry for entry in selected if entry.item_class == CLASS_SUBSTANTIVE]
    coverage = {
        "items": len(selected),
        "thin_evidence": sum(1 for entry in selected if entry.item_class == CLASS_THIN),
        "prominence_claim": sum(1 for entry in substantive if entry.has_prominence_claim),
        "single_source": sum(1 for entry in substantive if len(entry.records) == 1),
        "multi_source": sum(1 for entry in substantive if len(entry.records) >= 2),
        "languages": dict(sorted(languages.items())),
    }
    uncovered = [
        name
        for name, group in (("en", frozenset({"en"})), *LANGUAGE_GROUPS)
        if not any(group & set(entry.languages) for entry in selected)
    ]
    if not coverage["thin_evidence"]:
        uncovered.append("thin_evidence")
    if not coverage["prominence_claim"]:
        uncovered.append("prominence_claim")
    return coverage, uncovered


def refresh_benchmark(
    *,
    repo_root: Path = REPOSITORY_ROOT,
    benchmark_dir: Path = DEFAULT_BENCHMARK_DIR,
    run_dir: str = WEEKLY_RUN_DIR,
    target_size: int = BENCHMARK_TARGET_SIZE,
    git_show: Callable[[Path, str, str], bytes | None] = _git_show,
    git_commits: Callable[[Path, str, int], list[str]] = _git_commits,
) -> dict[str, Any]:
    """Rebuild the benchmark from committed grounded-description stage artifacts.

    This is the only step that reads Git.  Each identity is taken from the
    newest commit whose stage artifact kept research source records for it.
    The candidate's title and authors come from the Candidate History of the
    same commit, and the description it holds today joins the blind review.
    """
    if target_size < 1:
        raise QualificationInputError("the benchmark target size must be positive")
    current_history = _history_index(_history_at(git_show, repo_root, "HEAD", run_dir))
    pool: list[_PoolEntry] = []
    seen: set[str] = set()
    order = 0
    for commit in git_commits(repo_root, f"{run_dir}/{STAGE_ARTIFACT}", HISTORY_DEPTH):
        artifact = _json_at(git_show, repo_root, commit, f"{run_dir}/{STAGE_ARTIFACT}")
        if not isinstance(artifact, dict) or not isinstance(artifact.get("outcomes"), list):
            continue
        history = _history_index(_history_at(git_show, repo_root, commit, run_dir))
        for outcome in artifact["outcomes"]:
            if not isinstance(outcome, Mapping):
                continue
            entry = _pool_entry(order, commit, str(artifact.get("operation_date", "")), outcome, history)
            order += 1
            if entry is None or entry.identity_key in seen:
                continue
            seen.add(entry.identity_key)
            current = current_history.get(entry.identity_key, {})
            entry.history_description = str(current.get("content_description", "") or "").strip()
            pool.append(entry)
    if not pool:
        raise QualificationInputError("no committed stage artifact holds usable synthesis evidence")

    selected = select_benchmark(pool, target_size)
    items_dir = benchmark_dir / "items"
    items_dir.mkdir(parents=True, exist_ok=True)
    manifest_items: list[dict[str, Any]] = []
    slugs: set[str] = set()
    for entry in selected:
        slug = _item_slug(entry.identity_key)
        if slug in slugs:
            raise QualificationInputError(f"two items share the benchmark slug {slug!r}")
        slugs.add(slug)
        work_item = grounded_synthesis_work_item(entry.candidate, synthesis_sources_from_records(entry.records))
        document = {
            "identity_key": entry.identity_key,
            "commit": entry.commit,
            "operation_date": entry.operation_date,
            "item_class": entry.item_class,
            "selection_tags": sorted(entry.tags),
            "candidate": entry.candidate,
            "historic_outcome": {
                "outcome": str(entry.outcome.get("outcome", "")),
                "reason_code": str(entry.outcome.get("reason_code", "")),
                "reason_detail": str(entry.outcome.get("reason_detail", "")),
                "content_description": str(entry.outcome.get("content_description", "")),
                "prominence_claims": list(entry.outcome.get("prominence_claims") or []),
            },
            "history_description": entry.history_description,
            "research_source_records": entry.records,
            "work_item": work_item,
        }
        write_json(items_dir / f"{slug}.json", document)
        manifest_items.append(
            {
                "slug": slug,
                "identity_key": entry.identity_key,
                "commit": entry.commit,
                "item_class": entry.item_class,
                "source_count": len(entry.records),
                "languages": list(entry.languages),
                "has_prominence_claim": entry.has_prominence_claim,
                "work_item_sha256": _work_item_digest(work_item),
            }
        )
    # A refresh replaces the set, so an item it no longer selects is removed.
    for stale in items_dir.glob("*.json"):
        if stale.stem not in slugs:
            stale.unlink()
    coverage, uncovered = _coverage(selected)
    manifest = {
        "version": MANIFEST_VERSION,
        "run_dir": run_dir,
        "target_size": target_size,
        "coverage": coverage,
        "uncovered": uncovered,
        "items": manifest_items,
    }
    write_json(benchmark_dir / MANIFEST_NAME, manifest)
    return manifest


# -- Runner ------------------------------------------------------------------


STATUS_ANSWERED = "answered"
STATUS_TIMEOUT = "timeout"
STATUS_EXIT_NONZERO = "exit_nonzero"
STATUS_UNREADABLE = "unreadable_answer"
STATUS_MODEL_UNAVAILABLE = "model_unavailable"
STATUS_UNAVAILABLE = "cli_unavailable"
STATUS_EXCEPTION = "runner_exception"
STATUS_CANCELLED = "cancelled"
# The Claude API backend refused an item whose evidence mentions
# publishersweekly.com (Issue #1149).  Such a call is excluded from scoring,
# not counted as a model failure, and the report names it.
STATUS_PW_TEXT_NOT_PERMITTED = ModelFailure.PW_TEXT_NOT_PERMITTED.value
EXCLUDED_STATUSES = frozenset({STATUS_PW_TEXT_NOT_PERMITTED})


@dataclass(frozen=True)
class SynthesisCall:
    """What one synthesis call returned: the raw JSON answer, unfiltered."""

    status: str
    response: Mapping[str, Any] | None
    duration_seconds: float
    effective_profile: Mapping[str, str] = field(default_factory=dict)
    tokens_used: int | None = None


class SynthesisRunner(Protocol):
    """Run one synthesis call; a runner may also offer ``terminate_active()``."""

    def run(
        self, pin: CodexModelPin, work_item: Mapping[str, Any], *, prompt: str, timeout_seconds: float
    ) -> SynthesisCall: ...

    def cli_version(self) -> str: ...


_CLI_VERSION = re.compile(r"codex-cli\s+\S+")
_TOKENS_USED = re.compile(r"tokens used\s*\n\s*([0-9][0-9,]*)", re.IGNORECASE)


def codex_synthesis_command(pin: CodexModelPin, prompt: str, response_path: Path, workdir: Path) -> list[str]:
    """Return the isolated ``codex exec`` command for one synthesis call.

    The model and effort are explicit and the operator's config is not
    loaded, so the call measures the model, not the desktop settings.
    """
    return CodexCliClient().command(
        ModelRequest(
            prompt=prompt,
            response_path=response_path,
            model=pin.model,
            reasoning_effort=pin.reasoning_effort,
            working_dir=workdir,
            isolated=True,
            disabled_features=EXTRACTION_DISABLED_FEATURES,
        )
    )


class CodexSynthesisRunner:
    """Run one synthesis work item through an isolated ``codex exec``, or on the Claude API.

    A candidate on the Claude API backend (Issue #1149) runs through
    :class:`ClaudeApiClient` in the calling thread; ``terminate_active``
    closes its running requests as it kills running Codex processes.
    """

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, *, api_client: ClaudeApiClient | None = None
    ) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._active: set[subprocess.Popen[bytes]] = set()
        self._closed = False
        self._api_client = api_client if api_client is not None else ClaudeApiClient()

    def terminate_active(self) -> None:
        """Kill every running call and refuse new ones.

        Each call runs in its own session, so an aborted comparison would
        otherwise leave billed model calls running past their timeouts.
        """
        with self._lock:
            self._closed = True
            active = list(self._active)
        self._api_client.abort()
        from autoscout.codex_recovery import _terminate_process_tree

        for process in active:
            _terminate_process_tree(process)

    def _run_api(
        self, pin: CodexModelPin, work_item: Mapping[str, Any], *, prompt: str, timeout_seconds: float
    ) -> SynthesisCall:
        from autoscout.description_enrichment import _encode_work_items

        with self._lock:
            if self._closed:
                return SynthesisCall(STATUS_CANCELLED, None, 0.0)
        with tempfile.TemporaryDirectory(prefix="autoscout-description-qualification-") as temporary:
            workdir = Path(temporary)
            (workdir / "work_items.json").write_text(
                json.dumps(_encode_work_items([dict(work_item)]), ensure_ascii=False), encoding="utf-8"
            )
            request = ModelRequest(
                prompt=prompt,
                response_path=workdir / "response.json",
                model=pin.model,
                reasoning_effort=pin.reasoning_effort,
                working_dir=workdir,
                isolated=True,
                input_files=("work_items.json",),
                purpose="description_qualification",
            )
            started = self._clock()
            result = self._api_client.run(request, timeout=timeout_seconds)
            duration = round(max(0.0, self._clock() - started), 3)
        with self._lock:
            if self._closed:
                return SynthesisCall(STATUS_CANCELLED, None, duration)
        # The API names the model that answered; the effort is the one sent.
        profile = (
            {"model": result.served_model, "reasoning_effort": pin.reasoning_effort} if result.served_model else {}
        )
        tokens_used = result.usage.input + result.usage.output if result.usage is not None else None
        status = {
            ModelCallStatus.TIMEOUT: STATUS_TIMEOUT,
            ModelCallStatus.NOT_STARTED: STATUS_UNAVAILABLE,
            ModelCallStatus.REFUSED: STATUS_UNAVAILABLE,
            ModelCallStatus.UNREADABLE_RESPONSE: STATUS_UNREADABLE,
            ModelCallStatus.EXIT_NONZERO: STATUS_EXIT_NONZERO,
        }.get(result.status, STATUS_ANSWERED)
        if result.failure is ModelFailure.PW_TEXT_NOT_PERMITTED:
            status = STATUS_PW_TEXT_NOT_PERMITTED
        elif result.failure is ModelFailure.MODEL_UNAVAILABLE:
            status = STATUS_MODEL_UNAVAILABLE
        if status != STATUS_ANSWERED or not isinstance(result.payload, dict):
            return SynthesisCall(
                status if status != STATUS_ANSWERED else STATUS_UNREADABLE, None, duration, profile, tokens_used
            )
        return SynthesisCall(STATUS_ANSWERED, result.payload, duration, profile, tokens_used)

    def cli_version(self) -> str:
        try:
            completed = subprocess.run(["codex", "--version"], capture_output=True, text=True, check=False, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return ""
        if completed.returncode != 0:
            return ""
        # A version-manager shim may print its own line first.
        found = _CLI_VERSION.search(completed.stdout)
        return found.group(0)[:100] if found else completed.stdout.strip()[:100]

    def run(
        self, pin: CodexModelPin, work_item: Mapping[str, Any], *, prompt: str, timeout_seconds: float
    ) -> SynthesisCall:
        if pin.backend != DEFAULT_BACKEND:
            return self._run_api(pin, work_item, prompt=prompt, timeout_seconds=timeout_seconds)
        from autoscout.codex_recovery import _terminate_process_tree
        from autoscout.description_enrichment import _encode_work_items

        with tempfile.TemporaryDirectory(prefix="autoscout-description-qualification-") as temporary:
            workdir = Path(temporary)
            response_path = workdir / "response.json"
            stderr_path = workdir / "stderr.txt"
            (workdir / "work_items.json").write_text(
                json.dumps(_encode_work_items([dict(work_item)]), ensure_ascii=False), encoding="utf-8"
            )
            command = codex_synthesis_command(pin, prompt, response_path, workdir)
            started = self._clock()
            status = STATUS_ANSWERED
            with stderr_path.open("wb") as stderr_file:
                with self._lock:
                    if self._closed:
                        return SynthesisCall(STATUS_CANCELLED, None, 0.0)
                    try:
                        process = subprocess.Popen(
                            command,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=stderr_file,
                            cwd=str(workdir),
                            start_new_session=True,
                        )
                    except OSError:
                        return SynthesisCall(STATUS_UNAVAILABLE, None, 0.0)
                    self._active.add(process)
                try:
                    returncode = process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    _terminate_process_tree(process)
                    returncode = None
                    status = STATUS_TIMEOUT
                finally:
                    with self._lock:
                        self._active.discard(process)
            duration = round(max(0.0, self._clock() - started), 3)
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
            tokens = _TOKENS_USED.search(stderr)
            profile = header_profile(stderr)
            tokens_used = int(tokens.group(1).replace(",", "")) if tokens else None
            if status == STATUS_TIMEOUT:
                return SynthesisCall(status, None, duration, profile, tokens_used)
            if returncode != 0:
                status = STATUS_MODEL_UNAVAILABLE if model_unavailable_in(stderr) else STATUS_EXIT_NONZERO
                return SynthesisCall(status, None, duration, profile, tokens_used)
            try:
                payload = json.loads(response_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return SynthesisCall(STATUS_UNREADABLE, None, duration, profile, tokens_used)
        if not isinstance(payload, dict):
            return SynthesisCall(STATUS_UNREADABLE, None, duration, profile, tokens_used)
        # The raw answer is kept: production reads only the item's own key,
        # and the assessment needs the rest to tell a decline from a
        # malformed answer.
        return SynthesisCall(STATUS_ANSWERED, payload, duration, profile, tokens_used)


# -- Assessment --------------------------------------------------------------


_NUMBER = re.compile(r"\d+(?:[.,  ]\d{3})*(?:[.,]\d+)?")
_LATIN_CAPITALISED = re.compile(r"(?<![\w'’])([A-Z][A-Za-zÀ-ɏ'’-]*[A-Za-zÀ-ɏ])")
_SENTENCE_START = re.compile(r"(?:^|[.!?…:]\s+|[«\"“(\[]\s*)$")
MAX_FLAGGED_TOKENS = 20


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _evidence_text(item: LoadedItem) -> str:
    parts: list[str] = [str(item.candidate.get("title", ""))]
    parts += [str(author) for author in item.candidate.get("primary_authors", [])]
    for source in item.sources:
        parts += [source.display_text, source.raw_title, source.discovery_title, *source.raw_authors]
    return "\n".join(parts)


def unsupported_tokens(text: str, evidence: str) -> list[str]:
    """Return numbers and capitalised Latin names in *text* absent from *evidence*.

    A heuristic flag for review, not a verdict: a Russian description names
    people in Cyrillic, which this cannot check, and a capitalised word may be
    ordinary English.
    """
    evidence_numbers = {_digits(match) for match in _NUMBER.findall(evidence)}
    evidence_words = {word.casefold() for word in re.findall(r"\w+", evidence)}
    flagged: list[str] = []
    for match in _NUMBER.finditer(text):
        digits = _digits(match.group(0))
        if digits and digits not in evidence_numbers and match.group(0) not in flagged:
            flagged.append(match.group(0))
    for match in _LATIN_CAPITALISED.finditer(text):
        if _SENTENCE_START.search(text[: match.start()]):
            continue
        word = match.group(1)
        stem = re.sub(r"['’]s$", "", word)
        pieces = [piece for piece in re.findall(r"\w+", stem.casefold()) if piece]
        if pieces and not all(piece in evidence_words for piece in pieces) and word not in flagged:
            flagged.append(word)
    return flagged[:MAX_FLAGGED_TOKENS]


def _well_formed(response: Mapping[str, Any] | None, identity_key: str) -> bool:
    if not isinstance(response, Mapping):
        return False
    for key, value in response.items():
        if str(key) != identity_key or not isinstance(value, str | Mapping):
            return False
        if isinstance(value, Mapping) and not isinstance(value.get("content_description", ""), str):
            return False
    return True


def assess_answer(item: LoadedItem, call: SynthesisCall) -> dict[str, Any]:
    """Judge one answer by the gate the stage applies before accepting it."""
    synthesis = (
        synthesis_result_from_response(call.response, item.work_item["identity_key"])
        if call.response is not None
        else SynthesisResult("")
    )
    description = synthesis.content_description.strip()
    # Only a well-formed answer earns decline credit: prose filed under
    # another key, or a value of the wrong shape, is a format failure that
    # production would silently drop, not an abstention.
    well_formed = call.status == STATUS_ANSWERED and _well_formed(call.response, item.work_item["identity_key"])
    if call.status != STATUS_ANSWERED:
        russian_status = "no_answer"
    else:
        russian_status = "declined" if well_formed else "malformed_response"
    record: dict[str, Any] = {
        "described": False,
        "declined": well_formed and not description,
        "russian_status": russian_status,
        "russian_detail": "",
        "english_status": "not_checked",
        "claims_returned": len(synthesis.prominence_claims),
        "claims_accepted": 0,
        "claims_refusal_detail": "",
        "valid": False,
        "content_description": description,
        "english_description": synthesis.english_description.strip(),
        "unsupported_tokens": [],
    }
    if not well_formed or not description:
        return record
    validated, reason, detail = screen_synthesized_description(description, item.sources)
    if reason:
        record["russian_status"] = reason
        record["russian_detail"] = detail
        return record
    record["described"] = True
    record["russian_status"] = "accepted"
    ordered = _ordered(item.sources)
    if not synthesis.english_description.strip():
        record["english_status"] = "missing"
    elif _validated_english(synthesis.english_description, ordered):
        record["english_status"] = "valid"
    else:
        record["english_status"] = "invalid"
    if synthesis.prominence_claims:
        claims, refusal = GroundedDescriptionStage._validated_prominence_claims(synthesis, item.target(), ordered)
        record["claims_accepted"] = len(claims)
        record["claims_refusal_detail"] = refusal
    evidence = _evidence_text(item)
    record["unsupported_tokens"] = unsupported_tokens(f"{validated}\n{record['english_description']}", evidence)
    record["valid"] = record["english_status"] == "valid" and not record["claims_refusal_detail"]
    return record


# -- Execution ---------------------------------------------------------------


def _run_one(
    runner: SynthesisRunner,
    pin: CodexModelPin,
    item: LoadedItem,
    repetition: int,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    try:
        call = runner.run(pin, item.work_item, prompt=GROUNDED_SYNTHESIS_PROMPT, timeout_seconds=timeout_seconds)
    except Exception as exc:
        call = SynthesisCall(f"{STATUS_EXCEPTION}_{type(exc).__name__}", None, 0.0)
    excluded = {"excluded": call.status} if call.status in EXCLUDED_STATUSES else {}
    return {
        **excluded,
        "candidate": candidate_label(pin),
        "item": item.slug,
        "item_class": item.item.item_class,
        "repetition": repetition,
        "status": call.status,
        "duration_seconds": round(float(call.duration_seconds), 3),
        "effective_profile": dict(call.effective_profile),
        "tokens_used": call.tokens_used,
        **assess_answer(item, call),
    }


def run_candidates(
    pins: Sequence[CodexModelPin],
    items: Sequence[LoadedItem],
    *,
    repetitions: int,
    workers: int,
    timeout_seconds: float,
    runner: SynthesisRunner,
    progress: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    """Run every item *repetitions* times on every candidate."""
    jobs = [(pin, item, repetition) for pin in pins for repetition in range(1, repetitions + 1) for item in items]
    pool = DaemonThreadPool(max(1, workers), thread_name_prefix="autoscout-description-qualification-")
    try:
        futures = [
            pool.submit(_run_one, runner, pin, item, repetition, timeout_seconds=timeout_seconds)
            for pin, item, repetition in jobs
        ]
        records = []
        for done, future in enumerate(futures, start=1):
            records.append(future.result())
            if progress is not None:
                progress(done, len(futures))
        return records
    except BaseException:
        # An interrupted comparison must not leave model calls running.
        terminate = getattr(runner, "terminate_active", None)
        if callable(terminate):
            terminate()
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# -- Analysis ----------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def excluded_items(records: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Return each item the backend refused to send, once, with the reason."""
    seen: dict[str, str] = {}
    for record in records:
        if record.get("excluded"):
            seen.setdefault(str(record["item"]), str(record["excluded"]))
    return [{"item": item, "reason": reason} for item, reason in sorted(seen.items())]


def summarize(records: Sequence[Mapping[str, Any]], pin: CodexModelPin) -> dict[str, Any]:
    """Return one candidate's deterministic metrics across every repetition.

    A call the backend refused to send is excluded, not scored (Issue #1149).
    """
    records = [record for record in records if not record.get("excluded")]
    substantive = [record for record in records if record["item_class"] == CLASS_SUBSTANTIVE]
    thin = [record for record in records if record["item_class"] == CLASS_THIN]
    described = [record for record in records if record["described"]]
    by_item: dict[str, set[bool]] = {}
    for record in records:
        by_item.setdefault(str(record["item"]), set()).add(bool(record["described"]))
    durations = [float(record["duration_seconds"]) for record in records if record["status"] != STATUS_UNAVAILABLE]
    tokens = [int(record["tokens_used"]) for record in records if record["tokens_used"] is not None]
    profiles: dict[str, int] = {}
    mismatched = 0
    unreported = 0
    for record in records:
        profile = record.get("effective_profile") or {}
        if not profile.get("model") or not profile.get("reasoning_effort"):
            # A call whose model the CLI did not state is not evidence of it.
            unreported += 1
            continue
        name = f"{profile['model']}:{profile['reasoning_effort']}"
        profiles[name] = profiles.get(name, 0) + 1
        if profile["model"] != pin.model or profile["reasoning_effort"] != pin.reasoning_effort:
            mismatched += 1
    statuses: dict[str, int] = {}
    for record in records:
        statuses[str(record["status"])] = statuses.get(str(record["status"]), 0) + 1
    return {
        "calls": len(records),
        "statuses": dict(sorted(statuses.items())),
        "substantive_calls": len(substantive),
        "valid_calls": sum(1 for record in substantive if record["valid"]),
        "validity_rate": _rate(sum(1 for record in substantive if record["valid"]), len(substantive)),
        "russian_accepted_rate": _rate(sum(1 for record in substantive if record["described"]), len(substantive)),
        "english_valid_rate": _rate(
            sum(1 for record in described if record["english_status"] == "valid"), len(described)
        ),
        "claims_returned": sum(int(record["claims_returned"]) for record in records),
        "claims_accepted": sum(int(record["claims_accepted"]) for record in records),
        "claim_refusals": sum(1 for record in records if record["claims_refusal_detail"]),
        "thin_calls": len(thin),
        "honest_declines": sum(1 for record in thin if record["declined"]),
        "honest_decline_rate": None
        if any(record["described"] for record in thin)
        else _rate(sum(1 for record in thin if record["declined"]), len(thin)),
        # Short evidence is a review flag, not proof that a description is padded.
        "padded_thin_calls": 0,
        "faithful_thin_calls": 0,
        "unreviewed_thin_calls": sum(1 for record in thin if record["described"]),
        "grounding_flagged_calls": sum(1 for record in described if record["unsupported_tokens"]),
        "grounding_flagged_tokens": sum(len(record["unsupported_tokens"]) for record in described),
        "items": len(by_item),
        "stable_items": sum(1 for outcomes in by_item.values() if len(outcomes) == 1),
        "stability_rate": _rate(sum(1 for outcomes in by_item.values() if len(outcomes) == 1), len(by_item)),
        "duration_p50_seconds": _percentile(durations, 0.5),
        "duration_p95_seconds": _percentile(durations, 0.95),
        "tokens_reported_calls": len(tokens),
        "tokens_total": sum(tokens) if tokens else None,
        "tokens_per_call_mean": round(sum(tokens) / len(tokens), 1) if tokens else None,
        "effective_profiles": dict(sorted(profiles.items())),
        "profile_mismatched_calls": mismatched,
        "profile_unreported_calls": unreported,
    }


def _criterion(name: str, passed: bool | None, **detail: Any) -> dict[str, Any]:
    return {"name": name, "passed": passed, "detail": detail}


def _compared_validity_rate(item: Mapping[str, Any], excluded_anywhere: set[str]) -> float | None:
    """Return a candidate's validity rate over the substantive items no compared candidate excluded."""
    runs = [
        run
        for run in item["runs"]
        if run["item_class"] == CLASS_SUBSTANTIVE and str(run["item"]) not in excluded_anywhere
    ]
    return _rate(sum(1 for run in runs if run["valid"]), len(runs))


def evaluate(candidates: list[dict[str, Any]], blind: Mapping[str, Any] | None, scope: Mapping[str, int]) -> None:
    """Apply the pass criteria to every candidate and set its verdict.

    A criterion that could not be measured fails, and a run over part of the
    benchmark or with fewer repetitions fails outright: a partial run is not
    the evidence a pin cites.  *scope* names the items run, the benchmark
    size, and the repetitions.  The blind-review criterion stays open until the
    ratings are folded in with the score step.
    """
    # The relative gate compares candidates on the items every one of them
    # could be scored on: an item one backend refused to send (#1149) is left
    # out for all, so no candidate is measured on a set the others skipped.
    excluded_anywhere = {str(run["item"]) for item in candidates for run in item["runs"] if run.get("excluded")}
    compared_rates = {
        item["label"]: _compared_validity_rate(item, excluded_anywhere)
        if excluded_anywhere
        else item["summary"]["validity_rate"]
        for item in candidates
    }
    rates = [float(rate) for rate in compared_rates.values() if rate is not None]
    best = max(rates) if rates else None
    blind_columns = (blind or {}).get("columns") or {}
    for item in candidates:
        summary = item["summary"]
        rate = compared_rates[item["label"]]
        if excluded_anywhere:
            item["coverage"] = {
                "items_scored": len({str(run["item"]) for run in item["runs"] if not run.get("excluded")}),
                "items_excluded": len({str(run["item"]) for run in item["runs"] if run.get("excluded")}),
                "items_compared": len({str(run["item"]) for run in item["runs"]} - excluded_anywhere),
                "own_validity_rate": summary["validity_rate"],
            }
        column = blind_columns.get(item["label"]) if isinstance(blind_columns, Mapping) else None
        reviewed = {
            (rating["item"], rating["text"]): rating["faithfulness"] for rating in (column or {}).get("ratings", [])
        }
        thin_descriptions = [run for run in item["runs"] if run["item_class"] == CLASS_THIN and run["described"]]
        judgments = [reviewed.get((run["item"], run["content_description"])) for run in thin_descriptions]
        summary["faithful_thin_calls"] = judgments.count("faithful")
        summary["padded_thin_calls"] = sum(judgment in ("minor_issue", "unfaithful") for judgment in judgments)
        summary["unreviewed_thin_calls"] = judgments.count(None)
        summary["honest_decline_rate"] = (
            None
            if summary["unreviewed_thin_calls"]
            else _rate(summary["honest_declines"] + summary["faithful_thin_calls"], summary["thin_calls"])
        )
        # The rate if every unrated thin description were rated faithful. A
        # candidate that misses the bar even then has already failed, so it
        # is not left waiting on ratings that cannot change the result.
        best_possible = _rate(
            summary["honest_declines"] + summary["faithful_thin_calls"] + summary["unreviewed_thin_calls"],
            summary["thin_calls"],
        )
        if best_possible is None or best_possible < MIN_HONEST_DECLINE_RATE - 1e-9:
            decline_passed: bool | None = False
        elif summary["unreviewed_thin_calls"]:
            decline_passed = None
        else:
            decline_passed = summary["honest_decline_rate"] >= MIN_HONEST_DECLINE_RATE - 1e-9
        criteria = [
            _criterion(
                "validity_within_5_points_of_best",
                # A candidate with no valid answer is never close to the best,
                # even when every candidate failed alike.
                rate is not None and rate > 0 and best is not None and rate >= best - VALIDITY_MARGIN - 1e-9,
                validity_rate=rate,
                best_validity_rate=best,
                margin=VALIDITY_MARGIN,
                **({"items_left_out_for_every_candidate": sorted(excluded_anywhere)} if excluded_anywhere else {}),
            ),
            _criterion(
                "honest_decline_at_least_90_percent",
                decline_passed,
                honest_decline_rate=summary["honest_decline_rate"],
                best_possible_rate=best_possible,
                thin_calls=summary["thin_calls"],
                faithful_thin_calls=summary["faithful_thin_calls"],
                unreviewed_thin_calls=summary["unreviewed_thin_calls"],
                threshold=MIN_HONEST_DECLINE_RATE,
            ),
            _criterion(
                "p95_within_candidate_envelope",
                summary["duration_p95_seconds"] is not None
                and summary["duration_p95_seconds"] <= DURATION_ENVELOPE_SECONDS,
                duration_p95_seconds=summary["duration_p95_seconds"],
                envelope_seconds=DURATION_ENVELOPE_SECONDS,
            ),
            _criterion(
                "ran_on_requested_model",
                summary["calls"] > 0
                and summary["profile_mismatched_calls"] == 0
                and summary["profile_unreported_calls"] == 0,
                effective_profiles=summary["effective_profiles"],
                mismatched_calls=summary["profile_mismatched_calls"],
                unreported_calls=summary["profile_unreported_calls"],
            ),
            _criterion(
                "full_benchmark_run",
                scope["items_run"] == scope["benchmark_items"] and scope["repetitions"] >= DEFAULT_REPETITIONS,
                **scope,
                required_repetitions=DEFAULT_REPETITIONS,
            ),
        ]
        if not column or not column.get("complete"):
            criteria.append(
                _criterion(
                    "no_unfaithful_blind_rating",
                    None,
                    status="pending" if not column else "incomplete",
                    unfaithful=(column or {}).get("unfaithful"),
                )
            )
        else:
            criteria.append(
                _criterion(
                    "no_unfaithful_blind_rating",
                    int(column["unfaithful"]) == 0,
                    unfaithful=column["unfaithful"],
                    rated=column["rated"],
                )
            )
        if any(criterion["passed"] is False for criterion in criteria):
            verdict = VERDICT_INELIGIBLE
        elif any(criterion["passed"] is None for criterion in criteria):
            verdict = VERDICT_PENDING
        else:
            verdict = VERDICT_ELIGIBLE
        item["criteria"] = criteria
        item["verdict"] = verdict
        item["blind_score"] = column.get("score") if column else None


def ranking(candidates: Sequence[Mapping[str, Any]]) -> list[str]:
    """Rank eligible candidates by blind-review score, then by cost."""
    eligible = [item for item in candidates if item["verdict"] == VERDICT_ELIGIBLE]

    def key(item: Mapping[str, Any]) -> tuple[float, float, str]:
        score = item.get("blind_score")
        tokens = item["summary"]["tokens_per_call_mean"]
        return (
            -float(score) if score is not None else 0.0,
            float(tokens) if tokens is not None else float("inf"),
            str(item["label"]),
        )

    return [str(item["label"]) for item in sorted(eligible, key=key)]


# -- Blind review ------------------------------------------------------------


def _column_text(records: Sequence[Mapping[str, Any]]) -> str:
    """Return the description a candidate's first accepted repetition wrote."""
    for record in sorted(records, key=lambda record: int(record["repetition"])):
        if record["described"]:
            return str(record["content_description"])
    return ""


def _labels(count: int) -> list[str]:
    labels = []
    for index in range(1, count + 1):
        label = ""
        while index:
            index, digit = divmod(index - 1, 26)
            label = chr(ord("A") + digit) + label
        labels.append(label)
    return labels


def build_blind_review(
    items: Sequence[LoadedItem],
    labels: Sequence[str],
    records: Sequence[Mapping[str, Any]],
    seed: str,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Return the blind sheet, its key, and an empty ratings template.

    Each item's columns are shuffled from the seed and the item slug, so the
    same seed always yields the same sheet, and the sheet names no model.
    """
    by_cell: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for record in records:
        by_cell.setdefault((str(record["candidate"]), str(record["item"])), []).append(record)
    key: dict[str, Any] = {"seed": seed, "items": {}, "texts": {}}
    template: dict[str, Any] = {
        "scale": {
            "faithfulness": list(FAITHFULNESS_VALUES),
            "usefulness": "1-5, editorial usefulness",
            "russian_quality": "1-5, quality of the Russian",
        },
        "items": {},
    }
    lines = [
        "# Слепое сравнение описаний",
        "",
        "Для каждой книги ниже даны источники и несколько русских описаний под буквами.",
        "Оцените каждое описание в файле оценок: верность источникам",
        "(faithful, minor_issue или unfaithful, если описание сообщает то, чего в источниках нет),",
        "редакторскую пользу (1–5) и качество русского языка (1–5).",
        "",
    ]
    for item in items:
        columns: list[tuple[str, str]] = []
        for label in labels:
            cell = by_cell.get((label, item.slug), [])
            if item.item.item_class == CLASS_THIN:
                texts = list(
                    dict.fromkeys(
                        str(record["content_description"])
                        for record in sorted(cell, key=lambda record: int(record["repetition"]))
                        if record["described"]
                    )
                )
                columns.extend((label, text) for text in (texts or [""]))
            else:
                columns.append((label, _column_text(cell)))
        if item.history_description:
            columns.append((HISTORY_COLUMN, item.history_description))
        random.Random(f"{seed}:{item.slug}").shuffle(columns)
        letters = _labels(len(columns))
        key["items"][item.slug] = {letter: name for letter, (name, _text) in zip(letters, columns, strict=True)}
        key["texts"][item.slug] = {letter: text for letter, (_name, text) in zip(letters, columns, strict=True)}
        template["items"][item.slug] = {
            letter: {"faithfulness": None, "usefulness": None, "russian_quality": None}
            for letter, (_name, text) in zip(letters, columns, strict=True)
            if text
        }
        authors = ", ".join(str(author) for author in item.candidate.get("primary_authors", []))
        lines += [f"## {item.slug}", "", f"**{item.candidate.get('title', '')}** — {authors}", "", "### Источники", ""]
        for number, source in enumerate(item.sources, start=1):
            evidence = " ".join(source.display_text.split()) or "(нет текста)"
            lines += [f"{number}. {source.canonical_url}", "", f"   > {evidence}", ""]
        lines += ["### Описания", ""]
        for letter, (_name, text) in zip(letters, columns, strict=True):
            lines += [f"**{letter}.** {text or '— описания нет —'}", ""]
    return "\n".join(lines), key, template


def _check_rating(slug: str, letter: str, rating: Any) -> dict[str, Any] | None:
    if not isinstance(rating, Mapping):
        raise QualificationInputError(f"rating for {slug} {letter} is not an object")
    values = {name: rating.get(name) for name in ("faithfulness", "usefulness", "russian_quality")}
    if all(value is None for value in values.values()):
        return None
    if values["faithfulness"] not in FAITHFULNESS_VALUES:
        raise QualificationInputError(f"rating for {slug} {letter} has an unknown faithfulness")
    for name in ("usefulness", "russian_quality"):
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, int) or value not in RATING_RANGE:
            raise QualificationInputError(f"rating for {slug} {letter} needs {name} from 1 to 5")
    return values


def fold_ratings(key: Mapping[str, Any], template: Mapping[str, Any], ratings: Mapping[str, Any]) -> dict[str, Any]:
    """Return per-column blind-review statistics from the editor's ratings."""
    expected = template.get("items", {})
    rated_items = ratings.get("items") if isinstance(ratings, Mapping) else None
    if not isinstance(rated_items, Mapping):
        raise QualificationInputError("ratings file has no items object")
    unknown = sorted(set(rated_items) - set(expected))
    if unknown:
        raise QualificationInputError(f"ratings name items outside the sheet: {', '.join(unknown)}")
    columns: dict[str, dict[str, Any]] = {}
    for slug, letters in expected.items():
        given = rated_items.get(slug, {})
        if not isinstance(given, Mapping):
            raise QualificationInputError(f"ratings for {slug} are not an object")
        extra = sorted(set(given) - set(letters))
        if extra:
            raise QualificationInputError(f"ratings for {slug} name columns without a description: {', '.join(extra)}")
        for letter in letters:
            name = str(key["items"][slug][letter])
            column = columns.setdefault(
                name,
                {
                    "cells": 0,
                    "rated": 0,
                    "unfaithful": 0,
                    "minor_issue": 0,
                    "usefulness": [],
                    "russian_quality": [],
                    "ratings": [],
                },
            )
            column["cells"] += 1
            rating = _check_rating(slug, letter, given.get(letter, {}))
            if rating is None:
                continue
            column["ratings"].append(
                {
                    "item": slug,
                    "text": key.get("texts", {}).get(slug, {}).get(letter, ""),
                    "faithfulness": rating["faithfulness"],
                }
            )
            column["rated"] += 1
            if rating["faithfulness"] == "unfaithful":
                column["unfaithful"] += 1
            elif rating["faithfulness"] == "minor_issue":
                column["minor_issue"] += 1
            column["usefulness"].append(rating["usefulness"])
            column["russian_quality"].append(rating["russian_quality"])
    result: dict[str, Any] = {}
    for name, column in sorted(columns.items()):
        usefulness = column.pop("usefulness")
        quality = column.pop("russian_quality")
        scores = [(useful + russian) / 2 for useful, russian in zip(usefulness, quality, strict=True)]
        result[name] = {
            **column,
            "complete": column["rated"] == column["cells"],
            "mean_usefulness": round(sum(usefulness) / len(usefulness), 3) if usefulness else None,
            "mean_russian_quality": round(sum(quality) / len(quality), 3) if quality else None,
            "score": round(sum(scores) / len(scores), 3) if scores else None,
        }
    return result


# -- Qualification -----------------------------------------------------------


def _select_items(benchmark: Sequence[BenchmarkItem], wanted: Iterable[str] | None) -> list[BenchmarkItem]:
    if not wanted:
        return list(benchmark)
    names = [str(name) for name in wanted]
    known = {item.slug: item for item in benchmark} | {item.identity_key: item for item in benchmark}
    missing = [name for name in names if name not in known]
    if missing:
        raise QualificationInputError(f"item not in the benchmark: {', '.join(missing)}")
    chosen = {known[name].slug for name in names}
    return [item for item in benchmark if item.slug in chosen]


def qualify_description_models(
    candidates: Sequence[str],
    *,
    repetitions: int = DEFAULT_REPETITIONS,
    workers: int = DEFAULT_WORKERS,
    items: Iterable[str] | None = None,
    per_call_timeout: float = DEFAULT_PER_CALL_TIMEOUT_SECONDS,
    seed: str | None = None,
    benchmark_dir: Path = DEFAULT_BENCHMARK_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    runner: SynthesisRunner | None = None,
    progress: Callable[[int, int], None] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Replay the benchmark on every candidate and write the comparison report."""
    if repetitions < 1 or workers < 1 or per_call_timeout <= 0:
        raise QualificationInputError("repetitions, workers, and the per-call timeout must be positive")
    pins = [parse_candidate(value) for value in candidates]
    if not pins:
        raise QualificationInputError("name at least one --candidate model:effort")
    labels = [candidate_label(pin) for pin in pins]
    if len(set(labels)) != len(labels):
        raise QualificationInputError("a candidate is named twice")
    if HISTORY_COLUMN in labels:
        raise QualificationInputError(f"{HISTORY_COLUMN!r} is reserved for the Candidate History column")
    benchmark = load_benchmark(benchmark_dir)
    loaded = [load_item(item) for item in _select_items(benchmark, items)]
    if not loaded:
        raise QualificationInputError("the benchmark has no items to run")
    digest = manifest_digest(benchmark_dir)
    runner = runner or CodexSynthesisRunner()

    started_at = now()
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    output_dir = reports_dir / stamp
    if output_dir.exists():
        raise QualificationInputError(f"report directory already exists: {output_dir.name}")
    records = run_candidates(
        pins,
        loaded,
        repetitions=repetitions,
        workers=workers,
        timeout_seconds=per_call_timeout,
        runner=runner,
        progress=progress,
    )
    results = []
    for pin, label in zip(pins, labels, strict=True):
        own = [record for record in records if record["candidate"] == label]
        backend = {"backend": pin.backend} if pin.backend != DEFAULT_BACKEND else {}
        excluded = {"excluded": excluded_items(own)} if excluded_items(own) else {}
        results.append(
            {"label": label, **pin.as_profile(), **backend, "summary": summarize(own, pin), "runs": own, **excluded}
        )
    scope = {"items_run": len(loaded), "benchmark_items": len(benchmark), "repetitions": repetitions}
    evaluate(results, None, scope)
    sheet, key, template = build_blind_review(loaded, labels, records, seed or stamp)
    report = {
        "version": REPORT_VERSION,
        "generated_at": started_at.isoformat(timespec="seconds"),
        "codex_cli_version": runner.cli_version(),
        "benchmark_digest": digest,
        "prompt_sha256": hashlib.sha256(GROUNDED_SYNTHESIS_PROMPT.encode("utf-8")).hexdigest(),
        "repetitions": repetitions,
        "scope": scope,
        "workers": workers,
        "per_call_timeout_seconds": per_call_timeout,
        "duration_envelope_seconds": DURATION_ENVELOPE_SECONDS,
        "items": [{"slug": item.slug, "item_class": item.item.item_class} for item in loaded],
        "candidates": results,
        "blind_review": {"status": "pending", "columns": None},
        "ranking": ranking(results),
    }
    output_dir.mkdir(parents=True)
    write_json(output_dir / REPORT_JSON, report)
    (output_dir / REPORT_MARKDOWN).write_text(render_markdown(report), encoding="utf-8")
    (output_dir / BLIND_SHEET).write_text(sheet, encoding="utf-8")
    write_json(output_dir / BLIND_KEY, key)
    write_json(output_dir / BLIND_RATINGS, template)
    report["report_dir"] = str(output_dir)
    return report


def score_blind_review(report_dir: Path, ratings_path: Path | None = None) -> dict[str, Any]:
    """Fold the blind-review ratings into the report and settle the verdicts."""
    report = read_json(report_dir / REPORT_JSON, default=None)
    key = read_json(report_dir / BLIND_KEY, default=None)
    ratings = read_json(ratings_path or report_dir / BLIND_RATINGS, default=None)
    if not isinstance(report, dict) or report.get("version") != REPORT_VERSION:
        raise QualificationInputError(f"not a description qualification report: {report_dir.name}")
    if not isinstance(key, dict) or not isinstance(key.get("items"), dict):
        raise QualificationInputError("the blind-review key is missing or unreadable")
    if not isinstance(ratings, dict):
        raise QualificationInputError("the ratings file is missing or unreadable")
    # The cells to rate are the ones the sheet showed with a description.
    # The key retains the exact text for each anonymous column, because the
    # editor may fill in the ratings template in place. Older keys can only
    # recover whether the first accepted description was present.
    expected = {
        "items": {
            slug: {
                letter: None
                for letter, name in columns.items()
                if (bool(key["texts"][slug][letter]) if "texts" in key else _has_text(report, slug, str(name)))
            }
            for slug, columns in key["items"].items()
        }
    }
    columns = fold_ratings(key, expected, ratings)
    report["blind_review"] = {
        "status": "complete" if all(column["complete"] for column in columns.values()) else "incomplete",
        "columns": columns,
    }
    evaluate(report["candidates"], report["blind_review"], report["scope"])
    report["ranking"] = ranking(report["candidates"])
    write_json(report_dir / REPORT_JSON, report)
    (report_dir / REPORT_MARKDOWN).write_text(render_markdown(report), encoding="utf-8")
    report["report_dir"] = str(report_dir)
    return report


def _has_text(report: Mapping[str, Any], slug: str, name: str) -> bool:
    if name == HISTORY_COLUMN:
        return True
    for candidate in report.get("candidates", []):
        if candidate["label"] == name:
            return bool(_column_text([run for run in candidate["runs"] if run["item"] == slug]))
    return False


def exit_code(report: Mapping[str, Any]) -> int:
    verdicts = {item["verdict"] for item in report["candidates"]}
    return EXIT_OK if verdicts & {VERDICT_ELIGIBLE, VERDICT_PENDING} else EXIT_NONE_ELIGIBLE


# -- Markdown ----------------------------------------------------------------


def _cell(value: Any) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Description synthesis model qualification",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Codex CLI: {report.get('codex_cli_version') or 'unknown'}",
        f"- Items: {len(report['items'])} of {report['scope']['benchmark_items']}; "
        f"repetitions: {report['repetitions']}; workers: {report['workers']}",
        f"- Benchmark digest: `{report['benchmark_digest']}`",
        f"- Blind review: {report['blind_review']['status']}",
        f"- Ranking of eligible candidates: {', '.join(report['ranking']) or 'none'}",
        "",
        "## Candidates",
        "",
        "| Candidate | Verdict | Validity | Honest decline | Padded thin | Grounding flags | Stable | "
        "p50 / p95 s | Tokens/call | Ran on | Blind score |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in report["candidates"]:
        summary = item["summary"]
        ran_on = ", ".join(f"{name} ×{count}" for name, count in summary["effective_profiles"].items()) or "–"
        lines.append(
            f"| {item['label']} | {item['verdict']} | {_cell(summary['validity_rate'])} | "
            f"{_cell(summary['honest_decline_rate'])} | {summary['padded_thin_calls']} | "
            f"{summary['grounding_flagged_calls']} | {_cell(summary['stability_rate'])} | "
            f"{_cell(summary['duration_p50_seconds'])} / {_cell(summary['duration_p95_seconds'])} | "
            f"{_cell(summary['tokens_per_call_mean'])} | {ran_on} | {_cell(item.get('blind_score'))} |"
        )
    excluded = [(item["label"], entry) for item in report["candidates"] for entry in item.get("excluded", [])]
    if excluded:
        lines += [
            "",
            "## Excluded items",
            "",
            "Items the candidate's backend refused to send. They are not scored; judge the verdict with them in mind.",
            "",
        ]
        lines += [f"- {label}: {entry['item']} ({entry['reason']})" for label, entry in excluded]
    lines += ["", "## Criteria", ""]
    for item in report["candidates"]:
        lines.append(f"### {item['label']}")
        lines.append("")
        for criterion in item["criteria"]:
            mark = {True: "pass", False: "FAIL", None: "pending"}[criterion["passed"]]
            lines.append(f"- {criterion['name']}: {mark} — `{json.dumps(criterion['detail'], ensure_ascii=False)}`")
        lines.append("")
    lines += [
        "## Items",
        "",
        "| Item | Class | " + " | ".join(item["label"] for item in report["candidates"]) + " |",
        "|---|---|" + "---|" * len(report["candidates"]),
    ]
    for entry in report["items"]:
        cells = []
        for item in report["candidates"]:
            runs = [run for run in item["runs"] if run["item"] == entry["slug"]]
            valid = sum(1 for run in runs if run["valid"])
            declined = sum(1 for run in runs if run["declined"])
            cells.append(f"{valid} valid · {declined} declined / {len(runs)}")
        lines.append(f"| {entry['slug']} | {entry['item_class']} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "Grounding flags count answers naming a number or a Latin-script name that appears in no evidence text.",
        "They are a heuristic for review, not a verdict.",
        "This report does not change any pin; a pin change is a separate reviewed commit that cites it.",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "BenchmarkItem",
    "CodexSynthesisRunner",
    "LoadedItem",
    "QualificationInputError",
    "SynthesisCall",
    "SynthesisRunner",
    "assess_answer",
    "build_blind_review",
    "codex_synthesis_command",
    "evaluate",
    "fold_ratings",
    "load_benchmark",
    "load_item",
    "parse_candidate",
    "qualify_description_models",
    "refresh_benchmark",
    "render_markdown",
    "run_candidates",
    "score_blind_review",
    "select_benchmark",
    "summarize",
    "unsupported_tokens",
]
