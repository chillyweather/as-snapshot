"""Qualify a Codex model for extraction without a weekly run (Issue #734).

Extraction runs on a pinned model (Issue #731), so moving to another model is
a deliberate step, and one the pinned model's retirement will eventually
force.  A weekly run is the wrong tool for that step: it takes two hours,
touches the network and weekly state, and one run per source cannot tell a
reliable model from a lucky one.  On 2026-09-26 identical captures returned
41 entries on one run and none on the next.

Qualification replays a frozen benchmark of retained captures through the
production extraction agent and the production validators, several times per
source, and compares the candidate model with the pinned model's stored
results.  It has no network access apart from the Codex model calls
themselves, writes nothing outside its report directory, never changes the
pin, and never calls Git.

The benchmark is refreshed only by an explicit step that reads committed
weekly runs from Git history: for each active source, the newest commit whose
extraction artifact was produced from the capture committed beside it and
still validates.  Captures are stored compressed; each one's digest is checked
on every load.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import subprocess
import tempfile
import time
import unicodedata
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from autoscout.bookmarks_contract import is_bookmarks_source_identity
from autoscout.bounded_workers import DaemonThreadPool
from autoscout.codex_model_pin import DEFAULT_BACKEND
from autoscout.codex_model_pin import DEFAULT_PIN_PATH
from autoscout.codex_model_pin import CodexModelPin
from autoscout.codex_model_pin import ModelPinError
from autoscout.codex_model_pin import load_extraction_model_pin
from autoscout.codex_model_pin import parse_model_pin
from autoscout.config import resolve_weekly_portfolio
from autoscout.helper_metadata import HELPER_METADATA_FIELDS
from autoscout.helper_metadata import load_helper_metadata
from autoscout.identity import normalize_identity_text
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.model_client import ModelFailure
from autoscout.model_client import recording_model_usage
from autoscout.partial_list import collected_list_refused
from autoscout.partial_list import gate_dropped_entries
from autoscout.partial_list import gate_dropped_ranks
from autoscout.ranking_author_policy import mention_author_exempt
from autoscout.repository_root import repository_root
from autoscout.runtime_budget import CAPTURE_RUNTIME_BUDGET_SECONDS
from autoscout.runtime_budget import FINALIZATION_RESERVE_SECONDS
from autoscout.runtime_budget import WEEKLY_OPERATION_CEILING_SECONDS
from autoscout.source_metadata import portable_source_metadata
from autoscout.source_rules import is_active_source
from autoscout.source_rules import is_skipped_source

REPOSITORY_ROOT = repository_root()
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "configs" / "current-weekly-pilot-live.json"
DEFAULT_BENCHMARK_DIR = REPOSITORY_ROOT / "data" / "model-qualification" / "benchmark"
DEFAULT_REPORTS_DIR = REPOSITORY_ROOT / "data" / "model-qualification" / "reports"
WEEKLY_RUN_DIR = "runs/current-weekly-pilot-russian"

MANIFEST_NAME = "manifest.json"
REPORT_JSON = "model-qualification.json"
REPORT_MARKDOWN = "model-qualification.md"
MANIFEST_VERSION = 1
REPORT_VERSION = 1
# The rule recall is scored by. A stored baseline scored under another rule is
# not comparable with a candidate scored under this one, so it is rerun.
# 2 (Issue #751): authors compared in any name order; a reference row without
# an author found by its title.
RECALL_SCORING_VERSION = 2

DEFAULT_REPETITIONS = 3
DEFAULT_WORKERS = 3
DEFAULT_PER_ATTEMPT_TIMEOUT_SECONDS = 600.0
# The extraction share of one weekly operation.  A candidate must leave room
# for a full retry round, so its first round may use at most half of it.
DEFAULT_EXTRACTION_BUDGET_SECONDS = float(
    WEEKLY_OPERATION_CEILING_SECONDS - FINALIZATION_RESERVE_SECONDS - CAPTURE_RUNTIME_BUDGET_SECONDS
)
MIN_RECALL = 0.95
# A source may also pass by staying within this distance of the pinned
# model's own recall on it. On 2026-09-26 the pinned model itself recalled 36
# of 41 PW Best Books 2025 references in a live smoke run, so a flat 95% bar
# alone would fail a candidate that is exactly as good as the pin.
RECALL_TOLERANCE = 0.05
HISTORY_DEPTH = 40

# Sources whose healthy captures came back empty or rejected on 2026-09-26.
# They are marked in the benchmark so a report shows them first.
HARD_CASE_SOURCES = frozenset(
    {
        "Bokhandlerforeningen Boklista - Generell litteratur voksne",
        "Audible Bestselling Science & Engineering Audiobooks",
        "PW Best Books 2025",
        "Andrew Carnegie Medals for Excellence - Nonfiction 2026",
        "Cúspide 100 más vendidos",
        "Women's Prize for Non-Fiction 2026 Shortlist",
    }
)

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INPUT_ERROR = 2

# A source whose capture mentions publishersweekly.com is refused by the
# Claude API backend before anything is sent (Issue #1149).  Its runs are
# excluded from scoring, and the report names it.
EXCLUDED_DIAGNOSTICS = frozenset({ModelFailure.PW_TEXT_NOT_PERMITTED.value})


class QualificationInputError(ValueError):
    """The benchmark, configuration, or request cannot be used."""


# -- Configuration -----------------------------------------------------------


def configured_sources(
    config_path: Path = DEFAULT_CONFIG_PATH, *, include_inactive: bool = False
) -> dict[str, dict[str, Any]]:
    """Return the active, non-skipped configured sources by name.

    With *include_inactive* a retired source is returned too.  A frozen
    benchmark case stays a valid measurement of a model after its source is
    retired, and its contract is still the one in the configuration.
    """
    raw = read_json(config_path, default={})
    if not isinstance(raw, dict):
        raise QualificationInputError(f"configuration is not an object: {config_path.name}")
    config = resolve_weekly_portfolio(raw, config_path)
    sources: dict[str, dict[str, Any]] = {}
    for source in config.get("sources", []):
        if not isinstance(source, dict) or is_skipped_source(source):
            continue
        if not include_inactive and not is_active_source(source):
            continue
        name = str(source.get("name", "")).strip()
        if name:
            sources[name] = dict(source)
    return sources


def _slug(name: str) -> str:
    # ASCII only, so a benchmark directory name is identical on every
    # filesystem regardless of its Unicode normalisation.
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    kept = "".join(char if char.isalnum() else "-" for char in ascii_name.casefold())
    return "-".join(part for part in kept.split("-") if part)[:80] or "source"


def qualification_work_item(source: Mapping[str, Any], capture_file: str, capture_sha256: str) -> dict[str, Any]:
    """Return the work item the weekly run would build for this capture.

    The contract is built from the current source configuration, so the
    candidate is judged on the prompt it would actually receive.
    """
    from autoscout.source_collectors.contract import extraction_contract

    name = str(source.get("name", ""))
    return {
        "source_name": name,
        "source_family": source.get("family", ""),
        "source_url": source.get("url", ""),
        "capture_file": capture_file,
        "expected_extraction_file": "extraction.json",
        "extraction_method": "codex",
        "extraction_contract": extraction_contract(dict(source), capture_file=capture_file),
        "source_metadata": {
            "access_status": source.get("access_status", ""),
            "notes": source.get("notes", ""),
            **portable_source_metadata(dict(source)),
        },
        "operation_id": f"model-qualification:{_slug(name)}",
        "capture_sha256": capture_sha256,
    }


def _helper_index(config_path: Path, source: Mapping[str, Any]) -> Any:
    try:
        return load_helper_metadata(config_path, source)
    except Exception:
        return None


def _mention_key(title: Any, authors: Any) -> str:
    first = ""
    if isinstance(authors, list) and authors:
        first = str(authors[0])
    elif isinstance(authors, str):
        first = authors
    return f"{normalize_identity_text(str(title or ''))}|{normalize_identity_text(first)}"


def _order_free_key(key: str) -> str:
    title, _, author = key.partition("|")
    return f"{title}|{' '.join(sorted(author.split()))}"


def score_against_reference(keys: set[str], reference: set[str]) -> tuple[int, int]:
    """Return how many reference rows *keys* found, and how many keys are extra.

    A reference is a committed extraction of the same capture, and two
    renderings of one row are the same book (Issue #751):

    - A page that prints "Bildner, Jim" is extracted as written in one run
      and as "Jim Bildner" in the next. Scoring that as a miss plus an extra
      gave USA TODAY 0.00 recall on an answer holding its whole list, so an
      author is compared as its name tokens in any order.
    - A row whose page showed no author was recorded with a blank one. It
      names its book by title alone: an answer that also supplies the author
      found it, which is what put BookPage at 0.61 recall. A reference row
      that names an author still needs that author.
    """
    wanted = {_order_free_key(key) for key in reference}
    unmatched = {_order_free_key(key) for key in keys}
    found = len(unmatched & wanted)
    missing = wanted - unmatched
    unmatched -= wanted
    for key in sorted(missing):
        title, _, author = key.partition("|")
        if author:
            continue
        same_title = sorted(candidate for candidate in unmatched if candidate.partition("|")[0] == title)
        if same_title:
            unmatched.discard(same_title[0])
            found += 1
    return found, len(unmatched)


def collected_mentions(
    capture: Mapping[str, Any],
    artifact: Any,
    source: Mapping[str, Any],
    *,
    config_path: Path,
) -> tuple[list[dict[str, Any]], int]:
    """Return the mentions production collection would accept, and the drops."""
    from autoscout.source_collectors import _validate_codex_raw_required_fields
    from autoscout.source_collectors.agent_mentions import agent_extraction_mentions_from_response
    from autoscout.source_collectors.base import extraction_entries

    run_date = str(capture.get("reference_date") or datetime.now(UTC).date().isoformat())
    gate_dropped = gate_dropped_entries(artifact)
    try:
        mentions, dropped = agent_extraction_mentions_from_response(
            dict(capture),
            artifact,
            dict(source),
            run_date,
            collector_kind="model-qualification",
            helper_index=_helper_index(config_path, source),
            upstream_dropped_ranks=gate_dropped_ranks(artifact),
        )
    except Exception:
        return [], 0
    # The same 80% rule production applies after every drop (Issue #839).
    entries = extraction_entries(artifact)
    raw_entries = entries if isinstance(entries, list) else []
    _field_valid, field_dropped = _validate_codex_raw_required_fields(
        raw_entries, author_optional=mention_author_exempt(source)
    )
    if collected_list_refused(
        len(mentions),
        len(raw_entries) + len(gate_dropped),
        gate_dropped=len(gate_dropped),
        bookmarks_field_dropped=is_bookmarks_source_identity(source) and bool(field_dropped),
    ):
        return [], len(raw_entries) + len(gate_dropped)
    return list(mentions), len(dropped) + len(gate_dropped)


# -- Benchmark ---------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkCase:
    source_name: str
    slug: str
    commit: str
    capture_file: str
    capture_sha256: str
    reference_count: int
    hard_case: bool
    directory: Path

    def capture_bytes(self) -> bytes:
        data = gzip.decompress((self.directory / "capture.json.gz").read_bytes())
        if hashlib.sha256(data).hexdigest() != self.capture_sha256:
            raise QualificationInputError(f"benchmark capture digest mismatch: {self.source_name}")
        return data

    def reference(self) -> dict[str, Any]:
        document = read_json(self.directory / "reference.json", default={})
        if (
            not isinstance(document, dict)
            or not isinstance(document.get("reference_keys"), list)
            or not isinstance(document.get("entries"), list)
        ):
            raise QualificationInputError(f"benchmark reference is unreadable: {self.source_name}")
        return document

    def reference_keys(self) -> list[str]:
        return [str(key) for key in self.reference()["reference_keys"]]


def manifest_digest(benchmark_dir: Path) -> str:
    return hashlib.sha256((benchmark_dir / MANIFEST_NAME).read_bytes()).hexdigest()


def load_benchmark(benchmark_dir: Path = DEFAULT_BENCHMARK_DIR) -> list[BenchmarkCase]:
    """Read the frozen benchmark manifest and return its cases."""
    manifest = read_json(benchmark_dir / MANIFEST_NAME, default=None)
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        raise QualificationInputError("model qualification benchmark is missing; refresh it with --refresh-benchmark")
    cases: list[BenchmarkCase] = []
    for entry in manifest.get("cases", []):
        if not isinstance(entry, dict):
            raise QualificationInputError("benchmark manifest holds a non-object case")
        slug = str(entry.get("slug", ""))
        if not slug or "/" in slug or slug.startswith("."):
            raise QualificationInputError(f"benchmark case has an unsafe slug: {slug!r}")
        cases.append(
            BenchmarkCase(
                source_name=str(entry["source_name"]),
                slug=slug,
                commit=str(entry.get("commit", "")),
                capture_file=str(entry["capture_file"]),
                capture_sha256=str(entry["capture_sha256"]),
                reference_count=int(entry["reference_count"]),
                hard_case=bool(entry.get("hard_case", False)),
                directory=benchmark_dir / "cases" / slug,
            )
        )
    return cases


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


def refresh_benchmark(
    *,
    repo_root: Path = REPOSITORY_ROOT,
    config_path: Path = DEFAULT_CONFIG_PATH,
    benchmark_dir: Path = DEFAULT_BENCHMARK_DIR,
    source_names: Iterable[str] | None = None,
    run_dir: str = WEEKLY_RUN_DIR,
    git_show: Callable[[Path, str, str], bytes | None] = _git_show,
    git_commits: Callable[[Path, str, int], list[str]] = _git_commits,
) -> dict[str, Any]:
    """Rebuild the benchmark from committed weekly runs.

    This is the only step that reads Git.  For each source it takes the newest
    commit whose extraction artifact names the digest of the capture committed
    beside it and still yields accepted mentions under the current contract.
    """
    from autoscout.source_collection import codex_capture_filename
    from autoscout.source_collection import codex_extraction_slug

    sources = configured_sources(config_path)
    wanted = [str(name) for name in source_names] if source_names else sorted(sources)
    unknown = [name for name in wanted if name not in sources]
    if unknown:
        raise QualificationInputError(f"unknown or inactive source: {', '.join(unknown)}")

    cases: list[dict[str, Any]] = []
    uncovered: list[dict[str, str]] = []
    staged: dict[str, tuple[bytes, dict[str, Any]]] = {}
    for name in wanted:
        source = sources[name]
        capture_file = codex_capture_filename(source)
        artifact_file = codex_extraction_slug(name, str(source.get("url", "")))
        chosen = None
        for commit in git_commits(repo_root, f"{run_dir}/{artifact_file}", HISTORY_DEPTH):
            artifact_bytes = git_show(repo_root, commit, f"{run_dir}/{artifact_file}")
            capture_bytes = git_show(repo_root, commit, f"{run_dir}/{capture_file}")
            if artifact_bytes is None or capture_bytes is None:
                continue
            try:
                artifact = json.loads(artifact_bytes.decode("utf-8"))
                capture = json.loads(capture_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            digest = hashlib.sha256(capture_bytes).hexdigest()
            if (
                not isinstance(artifact, dict)
                or not artifact.get("entries")
                or artifact.get("capture_sha256") != digest
            ):
                continue
            mentions, _dropped = collected_mentions(capture, artifact, source, config_path=config_path)
            if not mentions:
                continue
            keys = sorted(
                {_mention_key(m.get("title"), m.get("primary_authors") or m.get("authors")) for m in mentions}
            )
            chosen = (commit, capture_bytes, digest, keys, artifact["entries"])
            break
        if chosen is None:
            uncovered.append(
                {"source_name": name, "reason": "no committed capture with a matching, still-valid extraction"}
            )
            continue
        commit, capture_bytes, digest, keys, entries = chosen
        slug = _slug(name)
        if slug in staged:
            raise QualificationInputError(f"two sources share the benchmark slug {slug!r}")
        staged[slug] = (
            capture_bytes,
            {
                "source_name": name,
                "commit": commit,
                "capture_sha256": digest,
                "reference_keys": keys,
                # The accepted extraction itself, so the benchmark can prove it
                # still validates under the current contract.
                "entries": entries,
            },
        )
        cases.append(
            {
                "source_name": name,
                "slug": slug,
                "commit": commit,
                "capture_file": capture_file,
                "capture_sha256": digest,
                "reference_count": len(keys),
                "hard_case": name in HARD_CASE_SOURCES,
            }
        )

    cases_dir = benchmark_dir / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)
    for slug, (capture_bytes, reference) in staged.items():
        case_dir = cases_dir / slug
        case_dir.mkdir(parents=True, exist_ok=True)
        # mtime=0 keeps the compressed bytes identical for identical captures.
        (case_dir / "capture.json.gz").write_bytes(gzip.compress(capture_bytes, compresslevel=9, mtime=0))
        write_json(case_dir / "reference.json", reference)
    manifest = {
        "version": MANIFEST_VERSION,
        "run_dir": run_dir,
        "cases": cases,
        "uncovered": uncovered,
    }
    write_json(benchmark_dir / MANIFEST_NAME, manifest)
    return manifest


# -- Execution ---------------------------------------------------------------


AgentFactory = Callable[[CodexModelPin, float], Any]


def _default_agent(pin: CodexModelPin, timeout_seconds: float) -> Any:
    from autoscout.codex_recovery import CodexCliExtractionAgent

    return CodexCliExtractionAgent(timeout_seconds=timeout_seconds, model_pin=pin)


def _run_one(
    agent: Any,
    case: BenchmarkCase,
    source: Mapping[str, Any],
    repetition: int,
    *,
    per_attempt_timeout: float,
    config_path: Path,
    clock: Callable[[], float],
) -> dict[str, Any]:
    from autoscout.observed_extraction import partial_artifact_payload
    from autoscout.observed_extraction import validate_extraction_artifact

    capture_bytes = case.capture_bytes()
    capture = json.loads(capture_bytes.decode("utf-8"))
    item = qualification_work_item(source, case.capture_file, case.capture_sha256)
    reference = set(case.reference_keys())
    helper_fields = [
        field for field in item["extraction_contract"].get("required_fields", []) if field in HELPER_METADATA_FIELDS
    ]
    record: dict[str, Any] = {
        "source_name": case.source_name,
        "repetition": repetition,
        "status": "not_started",
        "artifact_valid": False,
        "artifact_diagnostic": None,
        "raw_count": 0,
        "valid_mentions": 0,
        "dropped_mentions": 0,
        "reference_count": len(reference),
        "matched": 0,
        "recall": 0.0,
        "extras": 0,
        "helper_compliant": None,
        "duration_seconds": 0.0,
    }
    with tempfile.TemporaryDirectory(prefix="autoscout-model-qualification-") as temporary:
        root = Path(temporary)
        capture_path = root / case.capture_file
        capture_path.write_bytes(capture_bytes)
        artifact_path = root / "extraction.json"
        started = clock()
        try:
            attempt = agent.extract_with_timeout(item, capture_path, artifact_path, timeout_seconds=per_attempt_timeout)
            record["status"] = str(getattr(getattr(attempt, "status", None), "value", "unknown"))
            followup = str(getattr(getattr(attempt, "diagnosis", None), "empty_answer_followup", "") or "")
            if followup:
                record["empty_answer_followup"] = followup
            if str(getattr(attempt, "diagnostic", "")) in EXCLUDED_DIAGNOSTICS:
                # The backend refused to send this source (Issue #1149): not a model failure.
                record["excluded"] = str(attempt.diagnostic)
        except Exception as exc:
            record["status"] = f"exception_{type(exc).__name__}"
        record["duration_seconds"] = round(max(0.0, clock() - started), 3)
        if record.get("excluded"):
            return record
        if not artifact_path.exists():
            record["artifact_diagnostic"] = "missing_output"
            return record
        validation = validate_extraction_artifact(artifact_path, item, capture_path)
        record["artifact_valid"] = bool(validation.valid)
        record["artifact_diagnostic"] = validation.diagnostic_class
        try:
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return record
    entries = artifact.get("entries") if isinstance(artifact, dict) else None
    entries = entries if isinstance(entries, list) else []
    record["raw_count"] = len(entries)
    if helper_fields and entries:
        record["helper_compliant"] = all(
            isinstance(entry, dict) and all(str(entry.get(field, "")).strip() for field in helper_fields)
            for entry in entries
        )
    if not record["artifact_valid"]:
        return record
    # Production publishes a partial artifact without the rows the gate
    # refused, and the collector reads only those (Issue #839).
    if isinstance(artifact, dict):
        artifact = partial_artifact_payload(artifact, validation)
    mentions, dropped = collected_mentions(capture, artifact, source, config_path=config_path)
    keys = {_mention_key(m.get("title"), m.get("primary_authors") or m.get("authors")) for m in mentions}
    record["valid_mentions"] = len(mentions)
    record["dropped_mentions"] = dropped
    record["matched"], record["extras"] = score_against_reference(keys, reference)
    record["recall"] = round(record["matched"] / len(reference), 4) if reference else 1.0
    return record


def run_model(
    pin: CodexModelPin,
    cases: list[BenchmarkCase],
    sources: Mapping[str, Mapping[str, Any]],
    *,
    repetitions: int,
    workers: int,
    per_attempt_timeout: float,
    config_path: Path,
    agent_factory: AgentFactory = _default_agent,
    clock: Callable[[], float] = time.monotonic,
) -> list[dict[str, Any]]:
    """Run every benchmark case *repetitions* times on one model."""
    agent = agent_factory(pin, per_attempt_timeout)
    jobs = [(case, repetition) for repetition in range(1, repetitions + 1) for case in cases]
    pool = DaemonThreadPool(max(1, workers), thread_name_prefix="autoscout-model-qualification-")
    try:
        futures = [
            pool.submit(
                _run_one,
                agent,
                case,
                sources[case.source_name],
                repetition,
                per_attempt_timeout=per_attempt_timeout,
                config_path=config_path,
                clock=clock,
            )
            for case, repetition in jobs
        ]
        return [future.result() for future in futures]
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# -- Analysis ----------------------------------------------------------------


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return round(ordered[index], 3)


def summarize(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return per-source metrics across repetitions."""
    by_source: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_source.setdefault(str(record["source_name"]), []).append(record)
    summary: dict[str, dict[str, Any]] = {}
    for name, runs in sorted(by_source.items()):
        count = len(runs)
        valid = sum(1 for run in runs if run["artifact_valid"] and run["valid_mentions"] > 0)
        empty = sum(1 for run in runs if run["raw_count"] == 0)
        helper_runs = [run["helper_compliant"] for run in runs if run["helper_compliant"] is not None]
        durations = [float(run["duration_seconds"]) for run in runs]
        recalls = [float(run["recall"]) for run in runs]
        summary[name] = {
            "runs": count,
            "valid_runs": valid,
            "empty_runs": empty,
            "validation_pass_rate": round(valid / count, 4) if count else 0.0,
            "empty_rate": round(empty / count, 4) if count else 0.0,
            "reliable": count > 0 and valid == count,
            "reference_count": runs[0]["reference_count"],
            "mean_recall": round(sum(recalls) / count, 4) if count else 0.0,
            "min_recall": round(min(recalls), 4) if recalls else 0.0,
            "extras": sum(int(run["extras"]) for run in runs),
            "helper_compliance_rate": (
                round(sum(1 for value in helper_runs if value) / len(helper_runs), 4) if helper_runs else None
            ),
            "duration_p50_seconds": _percentile(durations, 0.5),
            "duration_p95_seconds": _percentile(durations, 0.95),
        }
    return summary


def _empty_rate(summary: Mapping[str, Mapping[str, Any]]) -> float:
    runs = sum(int(item["runs"]) for item in summary.values())
    empty = sum(int(item["empty_runs"]) for item in summary.values())
    return round(empty / runs, 4) if runs else 0.0


def evaluate(
    candidate: Mapping[str, Mapping[str, Any]],
    baseline: Mapping[str, Mapping[str, Any]] | None,
    *,
    workers: int,
    extraction_budget_seconds: float,
) -> dict[str, Any]:
    """Apply the pass criteria and return the verdict with each criterion."""
    criteria: list[dict[str, Any]] = []
    if baseline is not None:
        regressions = sorted(
            name
            for name, item in candidate.items()
            if baseline.get(name, {}).get("reliable") is True and not item["reliable"]
        )
        criteria.append(
            {
                "name": "no_reliability_regression",
                "passed": not regressions,
                "detail": {"regressed_sources": regressions},
            }
        )
        # Both rates over the same sources: a source the baseline left unscored
        # (#1149) is judged by the recall check alone.
        shared = [name for name in candidate if name in baseline]
        candidate_rate = _empty_rate({name: candidate[name] for name in shared})
        baseline_rate = _empty_rate({name: baseline[name] for name in shared})
        criteria.append(
            {
                "name": "empty_rate_not_higher",
                "passed": candidate_rate <= baseline_rate + 1e-9,
                "detail": {"candidate_empty_rate": candidate_rate, "baseline_empty_rate": baseline_rate},
            }
        )

    def _recall_floor(name: str) -> float:
        if baseline is None or name not in baseline:
            return MIN_RECALL
        return min(MIN_RECALL, float(baseline[name].get("mean_recall", 0.0)) - RECALL_TOLERANCE)

    low_recall = sorted(
        name
        for name, item in candidate.items()
        if item["reference_count"] and item["mean_recall"] < _recall_floor(name) - 1e-9
    )
    criteria.append(
        {
            "name": "recall_at_least_95_percent_or_baseline",
            "passed": not low_recall,
            "detail": {
                "threshold": MIN_RECALL,
                "baseline_tolerance": RECALL_TOLERANCE if baseline is not None else None,
                "below_threshold": low_recall,
            },
        }
    )
    p95_total = sum(float(item["duration_p95_seconds"] or 0.0) for item in candidate.values())
    projected_round = round(p95_total / max(1, workers), 3)
    allowance = round(extraction_budget_seconds / 2, 3)
    criteria.append(
        {
            "name": "durations_fit_weekly_budget",
            "passed": projected_round <= allowance,
            "detail": {
                "projected_first_round_seconds": projected_round,
                "allowed_first_round_seconds": allowance,
                "workers": workers,
            },
        }
    )
    return {
        "verdict": "PASS" if all(item["passed"] for item in criteria) else "FAIL",
        "baseline_compared": baseline is not None,
        "criteria": criteria,
    }


# -- Qualification -----------------------------------------------------------


def _stored_baseline(
    pin: CodexModelPin,
    pin_document: Mapping[str, Any],
    pin_path: Path,
    digest: str,
    names: set[str],
    repetitions: int,
) -> tuple[dict[str, dict[str, Any]] | None, str]:
    raw_path = str(pin_document.get("qualification_report") or "").strip()
    if not raw_path:
        return None, "the pin names no qualification report"
    # A relative report path is relative to the repository that holds the
    # pin, which lives in its configs/ directory.
    path = Path(raw_path) if Path(raw_path).is_absolute() else pin_path.resolve().parents[1] / raw_path
    report = read_json(path, default=None)
    if not isinstance(report, dict):
        return None, "the pinned qualification report is unreadable"
    candidate = report.get("candidate", {})
    if candidate.get("model") != pin.model or candidate.get("reasoning_effort") != pin.reasoning_effort:
        return None, "the pinned qualification report is for another model"
    # A report without a backend predates #1149 and ran on the Codex CLI.
    if str(candidate.get("backend") or DEFAULT_BACKEND) != pin.backend:
        return None, "the pinned qualification report is for another backend"
    if report.get("benchmark_digest") != digest:
        return None, "the pinned qualification report used another benchmark"
    if report.get("recall_scoring_version") != RECALL_SCORING_VERSION:
        return None, "the pinned qualification report scored recall under another rule"
    summary = candidate.get("sources", {})
    # A source that report excluded (its backend refused to send it, #1149)
    # is unscored there, not missing: it stays unscored in the comparison.
    excluded = {str(entry.get("source_name", "")) for entry in candidate.get("excluded", []) if isinstance(entry, dict)}
    scored = names - excluded
    if not isinstance(summary, dict) or not scored <= set(summary):
        return None, "the pinned qualification report does not cover every selected source"
    if any(int(summary[name].get("runs", 0)) < repetitions for name in scored):
        return None, "the pinned qualification report ran fewer repetitions"
    return {name: summary[name] for name in scored}, raw_path


def excluded_sources(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return each source the backend refused to send, once, with the reason."""
    seen: dict[str, str] = {}
    for record in records:
        if record.get("excluded"):
            seen.setdefault(str(record["source_name"]), str(record["excluded"]))
    return [{"source_name": name, "reason": reason} for name, reason in sorted(seen.items())]


def qualify_model(
    model: str,
    reasoning_effort: str,
    *,
    backend: str = DEFAULT_BACKEND,
    repetitions: int = DEFAULT_REPETITIONS,
    workers: int = DEFAULT_WORKERS,
    source_names: Iterable[str] | None = None,
    rerun_baseline: bool = False,
    per_attempt_timeout: float = DEFAULT_PER_ATTEMPT_TIMEOUT_SECONDS,
    extraction_budget_seconds: float = DEFAULT_EXTRACTION_BUDGET_SECONDS,
    config_path: Path = DEFAULT_CONFIG_PATH,
    benchmark_dir: Path = DEFAULT_BENCHMARK_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    pin_path: Path = DEFAULT_PIN_PATH,
    agent_factory: AgentFactory = _default_agent,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Qualify *model* against the pinned model and write the report."""
    if repetitions < 1 or workers < 1 or per_attempt_timeout <= 0:
        raise QualificationInputError("repetitions, workers, and the per-attempt timeout must be positive")
    try:
        candidate_pin = parse_model_pin(
            {"extraction": {"model": model, "reasoning_effort": reasoning_effort, "backend": backend}}
        )
        pinned = load_extraction_model_pin(pin_path)
    except ModelPinError as exc:
        raise QualificationInputError(str(exc)) from exc
    pin_raw = read_json(pin_path, default={})
    pin_document = pin_raw.get("extraction", {}) if isinstance(pin_raw, dict) else {}

    cases = load_benchmark(benchmark_dir)
    if source_names:
        wanted = {str(name) for name in source_names}
        missing = sorted(wanted - {case.source_name for case in cases})
        if missing:
            raise QualificationInputError(f"source not in the benchmark: {', '.join(missing)}")
        cases = [case for case in cases if case.source_name in wanted]
    if not cases:
        raise QualificationInputError("the benchmark has no cases to run")
    sources = configured_sources(config_path, include_inactive=True)
    unconfigured = sorted(case.source_name for case in cases if case.source_name not in sources)
    if unconfigured:
        raise QualificationInputError(f"benchmark source is no longer configured: {', '.join(unconfigured)}")
    digest = manifest_digest(benchmark_dir)
    names = {case.source_name for case in cases}

    def _run(pin: CodexModelPin) -> list[dict[str, Any]]:
        return run_model(
            pin,
            cases,
            sources,
            repetitions=repetitions,
            workers=workers,
            per_attempt_timeout=per_attempt_timeout,
            config_path=config_path,
            agent_factory=agent_factory,
            clock=clock,
        )

    with recording_model_usage() as usage:
        candidate_records = _run(candidate_pin)
    excluded = excluded_sources(candidate_records)
    candidate_summary = summarize([record for record in candidate_records if not record.get("excluded")])

    is_pinned = candidate_pin.as_profile() == pinned.as_profile() and candidate_pin.backend == pinned.backend
    baseline_summary: dict[str, dict[str, Any]] | None = None
    baseline_records: list[dict[str, Any]] = []
    baseline_excluded: list[dict[str, str]] = []
    if is_pinned:
        baseline_source = "not compared: the candidate is the pinned model"
    else:
        stored, reason = (None, "a fresh baseline was requested")
        if not rerun_baseline:
            stored, reason = _stored_baseline(pinned, pin_document, pin_path, digest, names, repetitions)
        if stored is not None:
            baseline_summary = stored
            baseline_source = f"stored report {reason}"
            baseline_excluded = [
                {"source_name": name, "reason": "excluded in the stored report"} for name in sorted(names - set(stored))
            ]
        else:
            baseline_records = _run(pinned)
            # The pinned model's own refused sources are unscored too, never empty zero-recall runs.
            baseline_excluded = excluded_sources(baseline_records)
            baseline_summary = summarize([record for record in baseline_records if not record.get("excluded")])
            baseline_source = f"fresh run ({reason})"

    evaluation = evaluate(
        candidate_summary, baseline_summary, workers=workers, extraction_budget_seconds=extraction_budget_seconds
    )
    if excluded:
        # Exclusions do not fail a candidate, but a run that scored nothing is no evidence.
        evaluation["criteria"].append(
            {
                "name": "some_source_scored",
                "passed": bool(candidate_summary),
                "detail": {"excluded_sources": [entry["source_name"] for entry in excluded]},
            }
        )
        evaluation["verdict"] = "PASS" if all(item["passed"] for item in evaluation["criteria"]) else "FAIL"
    started_at = now()
    report = {
        "version": REPORT_VERSION,
        "recall_scoring_version": RECALL_SCORING_VERSION,
        "generated_at": started_at.isoformat(timespec="seconds"),
        "benchmark_digest": digest,
        "repetitions": repetitions,
        "workers": workers,
        "per_attempt_timeout_seconds": per_attempt_timeout,
        "extraction_budget_seconds": extraction_budget_seconds,
        "hard_cases": sorted(case.source_name for case in cases if case.hard_case),
        "candidate": {
            **candidate_pin.as_profile(),
            **({"backend": candidate_pin.backend} if candidate_pin.backend != DEFAULT_BACKEND else {}),
            "sources": candidate_summary,
            "runs": candidate_records,
            **({"excluded": excluded} if excluded else {}),
            **({"model_usage": usage.totals()} if usage.totals()["calls"] else {}),
        },
        "baseline": {
            **pinned.as_profile(),
            **({"backend": pinned.backend} if pinned.backend != DEFAULT_BACKEND else {}),
            "source": baseline_source,
            "sources": baseline_summary,
            "runs": baseline_records,
            **({"excluded": baseline_excluded} if baseline_excluded else {}),
        },
        **evaluation,
    }
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    prefix = f"{candidate_pin.backend}--" if candidate_pin.backend != DEFAULT_BACKEND else ""
    output_dir = reports_dir / f"{prefix}{candidate_pin.model}--{candidate_pin.reasoning_effort}--{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / REPORT_JSON, report)
    (output_dir / REPORT_MARKDOWN).write_text(render_markdown(report), encoding="utf-8")
    report["report_dir"] = str(output_dir)
    return report


def _cell(value: Any) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def render_markdown(report: Mapping[str, Any]) -> str:
    candidate = report["candidate"]
    baseline = report["baseline"]
    lines = [
        f"# Model qualification: {candidate['model']} ({candidate['reasoning_effort']})"
        + (f" on {candidate['backend']}" if candidate.get("backend") else ""),
        "",
        f"**Verdict: {report['verdict']}**",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Pinned baseline: {baseline['model']} ({baseline['reasoning_effort']}), {baseline['source']}",
        f"- Repetitions per source: {report['repetitions']}; workers: {report['workers']}",
        f"- Benchmark digest: `{report['benchmark_digest']}`",
        "",
        "## Criteria",
        "",
    ]
    for criterion in report["criteria"]:
        mark = "pass" if criterion["passed"] else "FAIL"
        lines.append(f"- {criterion['name']}: {mark} — `{json.dumps(criterion['detail'], ensure_ascii=False)}`")
    lines += [
        "",
        "## Sources",
        "",
        "| Source | Valid | Empty | Recall (mean/min) | Extras | Helper | p50 / p95 s | Baseline valid / empty / recall |",
        "|---|---|---|---|---|---|---|---|",
    ]
    hard = set(report.get("hard_cases", []))
    baseline_sources = baseline.get("sources") or {}
    ordered = sorted(candidate["sources"].items(), key=lambda pair: (pair[0] not in hard, pair[0]))
    for name, item in ordered:
        base = baseline_sources.get(name)
        base_cell = (
            f"{base['valid_runs']}/{base['runs']} · {base['empty_runs']} · {_cell(base['mean_recall'])}"
            if base
            else "–"
        )
        label = f"{name} (hard case)" if name in hard else name
        lines.append(
            f"| {label} | {item['valid_runs']}/{item['runs']} | {item['empty_runs']} | "
            f"{_cell(item['mean_recall'])} / {_cell(item['min_recall'])} | {item['extras']} | "
            f"{_cell(item['helper_compliance_rate'])} | "
            f"{_cell(item['duration_p50_seconds'])} / {_cell(item['duration_p95_seconds'])} | {base_cell} |"
        )
    excluded = candidate.get("excluded") or []
    if excluded:
        lines += [
            "",
            "## Excluded sources",
            "",
            "Sources the candidate's backend refused to send. They are not scored; judge the verdict with them in mind.",
            "",
        ]
        lines += [f"- {entry['source_name']} ({entry['reason']})" for entry in excluded]
    baseline_excluded = baseline.get("excluded") or []
    if baseline_excluded:
        lines += ["", "## Sources excluded from the baseline", ""]
        lines += [f"- {entry['source_name']} ({entry['reason']})" for entry in baseline_excluded]
    lines += [
        "",
        "A PASS report is the evidence a pin change cites. This report does not change the pin.",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "BenchmarkCase",
    "QualificationInputError",
    "configured_sources",
    "evaluate",
    "load_benchmark",
    "qualification_work_item",
    "qualify_model",
    "refresh_benchmark",
    "render_markdown",
    "run_model",
    "summarize",
]
