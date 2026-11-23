from __future__ import annotations

import html
import http.cookiejar
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.parse import urljoin
from urllib.request import HTTPCookieProcessor
from urllib.request import Request
from urllib.request import build_opener
from urllib.request import urlopen

from autoscout.identity import normalize_identity_text
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.source_rules import is_skipped_source
from autoscout.source_rules import skipped_reason

# ── Constants ──────────────────────────────────────────────────────────────

RUSSIAN_LIBRARY_CATALOG_FAMILIES = {
    "russian-library-catalog",
}

RSL_NAMES = {
    "rsl",
    "russian state library",
    "russian state library catalog",
}

RSL_FORBIDDEN_RECORDED_FIELDS = {
    "article_body",
    "page_body",
    "full_body",
    "full_description",
    "description",
    "full_review",
    "review",
    "copied_excerpt",
    "book_sample",
    "sample_text",
}

LITRES_NAMES = {
    "litres",
    "litres.ru",
    "litres catalog",
}

BOOK24_NAMES = {
    "book24",
    "book24.ru",
    "book24 catalog",
}

OZON_NAMES = {
    "ozon",
    "ozon.ru",
    "ozon catalog",
}

LABIRINT_NAMES = {
    "labirint",
    "labirint.ru",
    "labirint catalog",
}

CHITAI_GOROD_NAMES = {
    "chitai-gorod",
    "chitai gorod",
    "chitai-gorod.ru",
    "chitai-gorod catalog",
    "читай-город",
}

# Single source of truth for retailer checks considered configured by the
# pipeline. russian_check_adapter has a branch for every alias in this set.
RUSSIAN_EDITION_RETAILERS = LITRES_NAMES | BOOK24_NAMES | OZON_NAMES | LABIRINT_NAMES | CHITAI_GOROD_NAMES

RUSSIAN_VERIFICATION_COVERAGES = frozenset({"complete", "partial", "not_run", "failed"})
RUSSIAN_PROVIDER_OUTCOMES = frozenset({"matched", "no_match", "ambiguous", "blocked", "unimplemented", "failed"})
RUSSIAN_PROVIDER_EXECUTION_STATUSES = frozenset({"checked", "not_run", "skipped", "blocked", "failed", "unimplemented"})


class VerificationConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


RUSSIAN_VERIFICATION_CONFIDENCES = frozenset(item.value for item in VerificationConfidence)


@dataclass(frozen=True)
class LiveCandidateEvidence:
    """Execution result for one candidate from one live provider check."""

    status: str
    results: tuple[dict[str, Any], ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class LiveCheckEvidence:
    """Ordered live evidence for one configured provider check."""

    check_index: int
    provider_name: str
    candidates: tuple[tuple[str, LiveCandidateEvidence], ...]

    def for_candidate(self, identity_key: str) -> LiveCandidateEvidence:
        for key, evidence in self.candidates:
            if key == identity_key:
                return evidence
        return LiveCandidateEvidence(status="not_run", reason="Provider check was not executed for this candidate")


def validate_russian_edition_verification(value: Any) -> None:
    """Validate verification coverage, execution status, and provider outcome."""
    if not isinstance(value, dict):
        raise ValueError("Russian-edition verification must be an object")
    for field, allowed in (
        ("check_coverage", RUSSIAN_VERIFICATION_COVERAGES),
        ("confidence", RUSSIAN_VERIFICATION_CONFIDENCES),
    ):
        raw_value = value.get(field)
        if not isinstance(raw_value, str) or raw_value not in allowed:
            raise ValueError(f"Russian-edition verification {field} has an unsupported value")
    aggregate_outcome = value.get("provider_outcome")
    if aggregate_outcome is not None and (
        not isinstance(aggregate_outcome, str) or aggregate_outcome not in RUSSIAN_PROVIDER_OUTCOMES
    ):
        raise ValueError("Russian-edition verification provider_outcome has an unsupported value")
    providers = value.get("providers")
    if not isinstance(providers, list):
        raise ValueError("Russian-edition verification providers must be a list")
    provider_outcomes: list[str] = []
    checked_count = 0
    for provider in providers:
        if not isinstance(provider, dict):
            raise ValueError("Russian-edition verification provider must be an object")
        status = provider.get("status")
        if not isinstance(status, str) or status not in RUSSIAN_PROVIDER_EXECUTION_STATUSES:
            raise ValueError("Russian-edition provider status has an unsupported value")
        outcome = provider.get("outcome")
        if outcome is not None and (not isinstance(outcome, str) or outcome not in RUSSIAN_PROVIDER_OUTCOMES):
            raise ValueError("Russian-edition provider outcome has an unsupported value")
        if status == "checked":
            if outcome not in {"matched", "no_match", "ambiguous"}:
                raise ValueError("A checked Russian-edition provider must have a result outcome")
            checked_count += 1
        elif status == "blocked" and outcome != "blocked":
            raise ValueError("A blocked Russian-edition provider must have a blocked outcome")
        elif status == "failed" and outcome != "failed":
            raise ValueError("A failed Russian-edition provider must have a failed outcome")
        elif status == "unimplemented" and outcome != "unimplemented":
            raise ValueError("An unimplemented Russian-edition provider must have an unimplemented outcome")
        elif status in {"not_run", "skipped"} and outcome is not None:
            raise ValueError("A provider that did not run cannot have a provider outcome")
        if outcome is not None:
            provider_outcomes.append(outcome)

    coverage = value["check_coverage"]
    if coverage == "complete" and (not providers or checked_count != len(providers) or aggregate_outcome is None):
        raise ValueError("Complete Russian-edition coverage requires a result from every provider")
    if coverage == "partial" and not (0 < checked_count < len(providers)):
        raise ValueError("Partial Russian-edition coverage requires checked and unchecked providers")
    if coverage == "not_run" and checked_count:
        raise ValueError("Not-run Russian-edition coverage cannot contain a checked provider")
    if coverage == "failed" and checked_count:
        raise ValueError("Failed Russian-edition coverage cannot contain a checked provider")
    if aggregate_outcome == "no_match" and coverage != "complete":
        raise ValueError("Aggregate no_match requires complete Russian-edition coverage")
    if aggregate_outcome is not None and aggregate_outcome not in provider_outcomes:
        raise ValueError("Aggregate provider outcome must be present in provider outcomes")


def is_valid_russian_edition_verification(value: Any) -> bool:
    try:
        validate_russian_edition_verification(value)
    except ValueError:
        return False
    return True


LITRES_FORBIDDEN_RECORDED_FIELDS = RSL_FORBIDDEN_RECORDED_FIELDS
BOOK24_FORBIDDEN_RECORDED_FIELDS = RSL_FORBIDDEN_RECORDED_FIELDS

LITRES_LIVE_FAILURE_PATTERNS = {
    "captcha": "LitRes live check blocked by CAPTCHA-like response",
    "robot": "LitRes live check blocked by bot-protection response",
    "access denied": "LitRes live check blocked by access-denied response",
    "доступ запрещ": "LitRes live check blocked by access-denied response",
    "слишком много запросов": "LitRes live check blocked by rate-limit response",
}

BOOK24_LIVE_FAILURE_PATTERNS = {
    "captcha": "Book24 live check blocked by CAPTCHA-like response",
    "robot": "Book24 live check blocked by bot-protection response",
    "access denied": "Book24 live check blocked by access-denied response",
    "доступ запрещ": "Book24 live check blocked by access-denied response",
    "слишком много запросов": "Book24 live check blocked by rate-limit response",
}

RSL_LIVE_FAILURE_PATTERNS = {
    "captcha": "RSL live check blocked by CAPTCHA-like response",
    "robot": "RSL live check blocked by bot-protection response",
    "access denied": "RSL live check blocked by access-denied response",
    "доступ запрещ": "RSL live check blocked by access-denied response",
}


# ── Query / match helpers ─────────────────────────────────────────────────


def _query_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def russian_query_attempts(mention: dict[str, Any]) -> list[dict[str, Any]]:
    title = _query_text(mention.get("title", ""))
    authors = [_query_text(author) for author in mention.get("primary_authors", []) if _query_text(author)]
    author = authors[0] if authors else ""
    attempts: list[dict[str, Any]] = []
    append_query_attempt(attempts, "title", title, False)
    append_query_attempt(attempts, "author", author, False)
    append_query_attempt(attempts, "title_plus_author", " ".join(part for part in (title, author) if part), False)
    append_query_attempt(attempts, "isbn", _query_text(mention.get("isbn", "")), False)
    append_query_attempt(attempts, "search_translation", _query_text(mention.get("search_translation", "")), True)
    append_query_attempt(
        attempts,
        "author_transliteration_note",
        _query_text(mention.get("author_transliteration_note", "")),
        True,
    )
    return attempts


def append_query_attempt(attempts: list[dict[str, Any]], kind: str, query: str, search_aid: bool) -> None:
    if not query:
        return
    record = {"kind": kind, "query": query, "search_aid": search_aid}
    if record not in attempts:
        attempts.append(record)


def result_matches_candidate(result: dict[str, Any], candidate: dict[str, Any]) -> bool:
    result_title = normalize_identity_text(str(result.get("candidate_title", "")))
    candidate_title = normalize_identity_text(str(candidate.get("title", "")))
    result_authors = [normalize_identity_text(str(author)) for author in result.get("primary_authors", [])]
    candidate_authors = [normalize_identity_text(str(author)) for author in candidate.get("primary_authors", [])]
    return result_title == candidate_title and result_authors == candidate_authors


def russian_check_name(check: dict[str, Any]) -> str:
    return str(check.get("retailer") or check.get("name") or "Russian provider").strip()


def russian_result_matches_candidate(result: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """Match a result without allowing an unscoped result to fan out.

    Positive fixture records retain the existing exact title and author
    matcher.  Explicit ``no_match`` records may use the durable identity key,
    or a title and optional author scope.  An unscoped no-match is never
    evidence for a candidate.
    """
    if str(result.get("status", "")).strip().lower() != "no_match":
        return result_matches_candidate(result, candidate)
    result_identity = str(result.get("identity_key") or result.get("candidate_id") or "").strip()
    candidate_identity = str(candidate.get("identity_key") or candidate.get("candidate_id") or "").strip()
    if result_identity:
        return result_identity == candidate_identity
    title = str(result.get("candidate_title") or result.get("source_candidate_title") or "").strip()
    authors = result.get("primary_authors")
    if title:
        if normalize_identity_text(title) != normalize_identity_text(str(candidate.get("title", ""))):
            return False
        if authors is None or authors == []:
            return True
        result_authors = [normalize_identity_text(str(author)) for author in authors]
        candidate_authors = [normalize_identity_text(str(author)) for author in candidate.get("primary_authors", [])]
        return result_authors == candidate_authors
    return False


def is_same_work_russian_edition(result: dict[str, Any]) -> bool:
    match_type = str(result.get("match_type", "")).strip().lower()
    language = str(result.get("language", "")).strip().lower()
    return (
        match_type == "same-work russian-language edition"
        and language == "russian"
        and bool(str(result.get("edition_url", "")).strip())
    )


def is_imported_foreign_language_listing(result: dict[str, Any]) -> bool:
    return str(result.get("match_type", "")).strip().lower() == "imported foreign-language listing"


def is_ambiguous_russian_match(result: dict[str, Any]) -> bool:
    return str(result.get("match_type", "")).strip().lower() == "ambiguous author/title match"


def is_forthcoming(candidate: dict[str, Any]) -> bool:
    return str(candidate.get("publication_status", "")).strip().lower() == "forthcoming"


def russian_edition_evidence(result: dict[str, Any], run_date: str) -> dict[str, str]:
    evidence = {
        "retailer": str(result.get("retailer", "")),
        "source_type": str(result.get("source_type", "russian-retailer")),
        "source_name": str(result.get("source_name") or result.get("retailer", "")),
        "match_type": str(result.get("match_type", "")),
        "language": str(result.get("language", "")),
        "url": str(result.get("edition_url", "")),
        "evidence_note": str(result.get("evidence_note", "")),
        "check_date": run_date,
    }
    for field in (
        "record_url",
        "query",
        "result_title",
        "result_author",
        "language_clue",
        "publication_data",
        "isbn",
        "safe_debug_status",
    ):
        if result.get(field):
            evidence[field] = str(result.get(field, ""))
    return evidence


# ── Source spec ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _SourceSpec:
    """Source-specific configuration plugged into the shared adapter workflow."""

    name: str
    source_type: str
    source_name_default: str
    live_failure_patterns: dict[str, str]
    is_live_blocked: bool
    # Functions plugged in by each source
    search_response: Callable[[dict[str, Any], str], tuple[str, str]]
    parse_html_response: Callable[[str, str, dict[str, Any], dict[str, Any], str], tuple[list[dict[str, Any]], str]]
    parse_json_response: Callable[[str, str, dict[str, Any], dict[str, Any], str], tuple[list[dict[str, Any]], str]]
    validate_recorded_result: Callable[[dict[str, Any]], None]
    normalize_recorded_result: Callable[[dict[str, Any], dict[str, Any], str], dict[str, Any]]
    # Whether fixture_record_results should fall back to candidate title for the query
    fixture_query_fallback_to_title: bool = True
    # Retailer-specific fields (set only for LitRes / Book24; RSL keeps defaults)
    _retailer_default_url: str = ""
    _retailer_search_path: str = "/search/"
    _retailer_query_param: str = "q"
    _retailer_html_card_pattern: str = ""
    _retailer_html_link_pattern: str = ""
    _retailer_html_markers: tuple[str, ...] = ()
    _retailer_json_results_keys: tuple[str, str, str] = ("results", "items", "books")
    _retailer_json_url_keys: tuple[str, str, str] = ("result_url", "url", "book_url")
    _retailer_json_title_keys: tuple[str, str, str] = ("result_title", "title", "name")
    _retailer_json_author_keys: tuple[str, str, str] = ("result_author", "author", "authors")


# ── RussianCheckAdapter ────────────────────────────────────────────────────


@dataclass(frozen=True)
class RussianCheckAdapter:
    name: str
    source_type: str
    recorded_records: Callable[[Path, dict[str, Any]], list[dict[str, Any]]]
    validate_recorded_result: Callable[[dict[str, Any]], None]
    normalize_recorded_result: Callable[[dict[str, Any], dict[str, Any], str], dict[str, Any]]
    live_results_for_candidate: Callable[[Path, dict[str, Any], dict[str, Any], str], tuple[list[dict[str, Any]], str]]
    blocked_error: Callable[[str], bool]

    def recorded_results(self, config_path: Path, check: dict[str, Any], run_date: str) -> list[dict[str, Any]]:
        results = []
        for record in self.recorded_records(config_path, check):
            self.validate_recorded_result(record)
            results.append(self.normalize_recorded_result(record, check, run_date))
        return results

    def live_results(
        self,
        config_path: Path,
        check: dict[str, Any],
        candidate: dict[str, Any],
        run_date: str,
    ) -> tuple[list[dict[str, Any]], str]:
        return self.live_results_for_candidate(config_path, check, candidate, run_date)


# ── Shared adapter workflow functions ──────────────────────────────────────


def _recorded_records(spec: _SourceSpec, config_path: Path, check: dict[str, Any]) -> list[dict[str, Any]]:
    records = list(check.get("recorded_results", []))
    for raw_path in recorded_result_paths(check):
        path = resolve_config_path(config_path, raw_path)
        if not path.exists():
            raise FileNotFoundError(path)
        data = read_json(path)
        loaded = data.get("results", data) if isinstance(data, dict) else data
        if not isinstance(loaded, list):
            raise ValueError(f"{path}: recorded {spec.name} results must be a list")
        records.extend(loaded)
    return records


def _blocked_response_reason(spec: _SourceSpec, text: str) -> str:
    lowered = text.lower()
    for pattern, reason in spec.live_failure_patterns.items():
        if pattern in lowered:
            return reason
    return ""


def _live_status_error(spec: _SourceSpec, status: str) -> str:
    if status == "captcha":
        return f"{spec.name} live check blocked by CAPTCHA-like response"
    if status == "blocked":
        return f"{spec.name} live check blocked by access-denied response"
    if status == "timeout":
        return f"{spec.name} live check failed: request timed out"
    if status in {"changed-markup", "malformed"}:
        return f"{spec.name} live check failed: response markup could not be parsed safely"
    return f"{spec.name} live check failed"


def _blocked_error(spec: _SourceSpec, error: str) -> bool:
    if not spec.is_live_blocked:
        return False
    lowered = error.lower()
    return "blocked" in lowered or "captcha" in lowered or "http 403" in lowered or "http 429" in lowered


def _infer_russian_match_type(record: dict[str, Any], candidate: dict[str, Any]) -> str:
    language = str(record.get("language") or record.get("language_clue", "")).strip().lower()
    title = normalize_identity_text(str(record.get("title") or record.get("result_title", "")))
    candidate_title = normalize_identity_text(str(candidate.get("title", "")))
    author = normalize_identity_text(str(record.get("author") or record.get("result_author", "")))
    candidate_authors = [
        normalize_identity_text(str(candidate_author)) for candidate_author in candidate.get("primary_authors", [])
    ]
    if language and not is_russian_language_clue(language):
        return "imported foreign-language listing"
    same_title = title == candidate_title or bool(
        title and candidate_title and (title.startswith(candidate_title) or candidate_title in title)
    )
    same_author = (
        not author
        or author in candidate_authors
        or any(author_part in author for author_part in candidate_authors)
        or any(same_name_tokens(author, author_part) for author_part in candidate_authors)
    )
    if same_title and same_author:
        return "same-work Russian-language edition"
    return "ambiguous author/title match"


def _load_live_fixture(spec: _SourceSpec, config_path: Path, raw_path: str) -> tuple[list[Any], str]:
    path = resolve_config_path(config_path, raw_path)
    data = read_json(path, default={})
    loaded = data.get("results", data.get("responses", data)) if isinstance(data, dict) else data
    if not isinstance(loaded, list):
        return [], f"{path}: {spec.name} live response fixture must be a list"
    return loaded, ""


def _fixture_record_results(
    spec: _SourceSpec,
    record: Any,
    check: dict[str, Any],
    candidate: dict[str, Any],
    run_date: str,
) -> tuple[list[dict[str, Any]], str]:
    if not isinstance(record, dict):
        return [], f"{spec.name} live response fixture entries must be objects"
    status = str(record.get("status", "")).strip().lower()
    if status in {"blocked", "captcha", "timeout", "failed", "changed-markup", "malformed"}:
        return [], _live_status_error(spec, status)
    if status == "no_match":
        return [dict(record)], ""
    if "body" in record:
        return _parse_live_response(
            spec,
            str(record.get("body", "")),
            str(record.get("query") or (candidate.get("title", "") if spec.fixture_query_fallback_to_title else "")),
            candidate,
            check,
            run_date,
        )
    spec.validate_recorded_result(record)
    result = spec.normalize_recorded_result(record, check, run_date)
    if result_matches_candidate(result, candidate):
        return [result], ""
    return [], ""


def _fixture_live_results(
    spec: _SourceSpec,
    config_path: Path,
    check: dict[str, Any],
    candidate: dict[str, Any],
    run_date: str,
) -> tuple[list[dict[str, Any]], str]:
    raw_records = list(check.get("fixture_live_results", []))
    for raw_path in check.get("live_response_files", []):
        loaded, error = _load_live_fixture(spec, config_path, str(raw_path))
        if error:
            return [], error
        raw_records.extend(loaded)
    if check.get("live_response_file"):
        loaded, error = _load_live_fixture(spec, config_path, str(check["live_response_file"]))
        if error:
            return [], error
        raw_records.extend(loaded)
    results = []
    for record in raw_records:
        parsed_records, error = _fixture_record_results(spec, record, check, candidate, run_date)
        if error:
            return [], error
        results.extend(parsed_records)
    return results, ""


def _live_results_for_candidate(
    spec: _SourceSpec,
    config_path: Path,
    check: dict[str, Any],
    candidate: dict[str, Any],
    run_date: str,
) -> tuple[list[dict[str, Any]], str]:
    if "fixture_live_results" in check or check.get("live_response_file") or check.get("live_response_files"):
        return _fixture_live_results(spec, config_path, check, candidate, run_date)

    all_results = []
    for attempt in russian_query_attempts(candidate):
        response_text, error = spec.search_response(check, attempt["query"])
        if error:
            return [], error
        results, parse_error = _parse_live_response(
            spec,
            response_text,
            str(attempt["query"]),
            candidate,
            check,
            run_date,
        )
        if parse_error:
            return [], parse_error
        all_results.extend(results)
    return all_results, ""


def _parse_live_response(
    spec: _SourceSpec,
    response_text: str,
    query: str,
    candidate: dict[str, Any],
    check: dict[str, Any],
    run_date: str,
) -> tuple[list[dict[str, Any]], str]:
    if not response_text.strip():
        return [], ""
    blocked_reason = _blocked_response_reason(spec, response_text)
    if blocked_reason:
        return [], blocked_reason
    stripped = response_text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return spec.parse_json_response(response_text, query, candidate, check, run_date)
    return spec.parse_html_response(response_text, query, candidate, check, run_date)


# ── Adapter construction ───────────────────────────────────────────────────


def russian_check_adapter(check: dict[str, Any]) -> RussianCheckAdapter | None:
    if is_litres_check(check):
        spec = _LITRES_SPEC
    elif is_book24_check(check):
        spec = _BOOK24_SPEC
    elif is_rsl_check(check):
        spec = _RSL_SPEC
    elif is_generic_russian_retailer_check(check):
        return RussianCheckAdapter(
            name=str(check.get("retailer") or check.get("name") or "Russian retailer"),
            source_type="russian-retailer",
            recorded_records=generic_retailer_recorded_records,
            validate_recorded_result=validate_generic_retailer_recorded_result,
            normalize_recorded_result=normalize_generic_retailer_result,
            live_results_for_candidate=generic_retailer_live_results,
            blocked_error=generic_retailer_blocked_error,
        )
    else:
        return None
    return RussianCheckAdapter(
        name=spec.name,
        source_type=spec.source_type,
        recorded_records=partial(_recorded_records, spec),
        validate_recorded_result=spec.validate_recorded_result,
        normalize_recorded_result=spec.normalize_recorded_result,
        live_results_for_candidate=partial(_live_results_for_candidate, spec),
        blocked_error=partial(_blocked_error, spec),
    )


# ── Top-level pipeline functions ───────────────────────────────────────────


def prepare_russian_edition_checks(
    config_path: Path,
    output_dir: Path,
    checks: list[dict[str, Any]],
    run_date: str,
) -> list[dict[str, Any]]:
    prepared = []
    for check in checks:
        prepared_check = dict(check)
        prepared_check["fixture_results_present"] = "fixture_results" in check
        prepared_check["fixture_live_results_present"] = bool(
            "fixture_live_results" in check or check.get("live_response_file") or check.get("live_response_files")
        )
        try:
            prepared_check["fixture_results"] = normalized_check_results(config_path, prepared_check, run_date)
        except (OSError, ValueError):
            # A bad replay is provider evidence, not a reason to discard the
            # sibling providers or abort the weekly pipeline.
            prepared_check["fixture_results"] = []
            prepared_check["fixture_error"] = "Russian provider replay could not be loaded or validated"
        prepared.append(prepared_check)
    return prepared


def normalized_check_results(config_path: Path, check: dict[str, Any], run_date: str) -> list[dict[str, Any]]:
    results = list(check.get("fixture_results", []))
    adapter = russian_check_adapter(check)
    if adapter is not None:
        results.extend(adapter.recorded_results(config_path, check, run_date))
    return results


def _candidate_key(candidate: dict[str, Any]) -> str:
    return str(candidate.get("identity_key") or candidate.get("candidate_id") or "").strip()


def _safe_live_error(error: str, *, blocked: bool = False) -> str:
    lowered = error.lower()
    if "captcha" in lowered:
        if blocked:
            return "Provider access was blocked by CAPTCHA-like response"
        return "Provider check failed due to CAPTCHA-like response"
    if blocked:
        return "Provider access was blocked"
    if "timed out" in lowered or "timeout" in lowered:
        return "Provider request timed out"
    if "unimplemented" in lowered or "no implemented adapter" in lowered:
        return "Provider adapter is not implemented"
    if "markup" in lowered or "json" in lowered or "parse" in lowered or "malformed" in lowered:
        return "Provider response could not be parsed safely"
    return "Provider check failed"


def apply_live_rsl_checks(
    candidates: list[dict[str, Any]],
    checks: list[dict[str, Any]],
    run_date: str,
    config_path: Path | None,
    output_dir: Path | None,
) -> tuple[LiveCheckEvidence, ...]:
    """Run opted-in checks and return explicit ordered candidate evidence."""
    config_path = config_path or Path(".")
    candidate_keys = [_candidate_key(candidate) for candidate in candidates]
    all_live_evidence: list[LiveCheckEvidence] = []
    for check_index, check in enumerate(checks):
        provider_name = russian_check_name(check)
        candidate_evidence: list[tuple[str, LiveCandidateEvidence]] = [
            (key, LiveCandidateEvidence(status="not_run")) for key in candidate_keys
        ]
        if check.get("enable_live") and not is_skipped_source(check) and not check.get("fixture_error"):
            adapter = russian_check_adapter(check)
            if adapter is not None:
                captured_results = []
                for candidate_index, (candidate, key) in enumerate(zip(candidates, candidate_keys, strict=False)):
                    try:
                        results, error = adapter.live_results(config_path, check, candidate, run_date)
                    except Exception as exc:
                        results, error = [], str(exc)
                    if error:
                        is_blocked = adapter.blocked_error(error)
                        safe_error = _safe_live_error(error, blocked=is_blocked)
                        status = (
                            "blocked"
                            if is_blocked
                            else "unimplemented"
                            if "not implemented" in safe_error.lower() or "unimplemented" in safe_error.lower()
                            else "failed"
                        )
                        candidate_evidence[candidate_index] = (
                            key,
                            LiveCandidateEvidence(status=status, reason=safe_error),
                        )
                        continue
                    # A live response that parsed safely is execution evidence.
                    # An explicitly empty live fixture is not an executed check.
                    has_fixture = bool(check.get("fixture_live_results_present"))
                    executed = not has_fixture or any(
                        russian_result_matches_candidate(result, candidate) for result in results
                    )
                    status = "checked" if executed else "not_run"
                    candidate_evidence[candidate_index] = (
                        key,
                        LiveCandidateEvidence(status=status, results=tuple(results)),
                    )
                    if executed:
                        check.setdefault("fixture_results", []).extend(results)
                    captured_results.extend(results)
                if captured_results and output_dir is not None and is_rsl_check(check):
                    capture_rsl_recorded_results(output_dir, check, captured_results)
        statuses = [evidence.status for _key, evidence in candidate_evidence]
        if "checked" in statuses:
            check["live_run_status"] = "checked"
        elif "blocked" in statuses:
            check["live_run_status"] = "blocked"
        elif "failed" in statuses:
            check["live_run_status"] = "failed"
        elif "unimplemented" in statuses or (russian_check_adapter(check) is None and check.get("enable_live")):
            check["live_run_status"] = "unimplemented"
        else:
            check["live_run_status"] = "not_run"
        check["live_run_reason"] = next(
            (evidence.reason for evidence in (item[1] for item in candidate_evidence) if evidence.reason),
            "",
        )
        all_live_evidence.append(
            LiveCheckEvidence(
                check_index=check_index,
                provider_name=provider_name,
                candidates=tuple(candidate_evidence),
            )
        )
    return tuple(all_live_evidence)


# ── Path / IO helpers ──────────────────────────────────────────────────────


def recorded_result_paths(check: dict[str, Any]) -> list[str]:
    paths = []
    if check.get("recorded_results_file"):
        paths.append(str(check["recorded_results_file"]))
    paths.extend(str(path) for path in check.get("recorded_results_files", []))
    return paths


def recorded_rsl_result_paths(check: dict[str, Any]) -> list[str]:
    return recorded_result_paths(check)


def resolve_config_path(config_path: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return config_path.parent / path


def generic_retailer_recorded_records(config_path: Path, check: dict[str, Any]) -> list[dict[str, Any]]:
    records = list(check.get("recorded_results", []))
    for raw_path in recorded_result_paths(check):
        path = resolve_config_path(config_path, raw_path)
        if not path.exists():
            raise FileNotFoundError(path)
        data = read_json(path)
        loaded = data.get("results", data) if isinstance(data, dict) else data
        if not isinstance(loaded, list):
            retailer = str(check.get("retailer") or check.get("name") or "Russian retailer")
            raise ValueError(f"{path}: recorded {retailer} results must be a list")
        records.extend(loaded)
    return records


def generic_retailer_live_results(
    config_path: Path,
    check: dict[str, Any],
    candidate: dict[str, Any],
    run_date: str,
) -> tuple[list[dict[str, Any]], str]:
    retailer = str(check.get("retailer") or check.get("name") or "Russian retailer")
    return [], f"{retailer} live check has no implemented adapter"


def generic_retailer_blocked_error(error: str) -> bool:
    return False


# ── Source-specific: normalize ─────────────────────────────────────────────


def normalize_rsl_result(record: dict[str, Any], check: dict[str, Any], run_date: str) -> dict[str, Any]:
    record_url = str(record.get("record_url", "")).strip()
    language = str(record.get("language") or record.get("language_clue", "")).strip()
    return {
        "candidate_title": str(
            record.get("candidate_title") or record.get("source_candidate_title") or record.get("title", "")
        ).strip(),
        "primary_authors": list(record.get("primary_authors", [])),
        "match_type": str(record.get("match_type", "")).strip(),
        "language": language,
        "edition_url": record_url,
        "record_url": record_url,
        "evidence_note": safe_rsl_metadata(str(record.get("evidence_note") or rsl_evidence_note(record)).strip()),
        "source_type": "russian-library-catalog",
        "source_name": str(record.get("source_name") or check.get("name") or "RSL").strip(),
        "query": str(record.get("query", "")).strip(),
        "result_title": str(record.get("result_title", "")).strip(),
        "result_author": str(record.get("result_author", "")).strip(),
        "language_clue": str(record.get("language_clue", "")).strip(),
        "publication_data": safe_rsl_metadata(
            str(record.get("publication_data", "")).strip(), _rsl_publication_limit(check)
        ),
        "isbn": str(record.get("isbn", "")).strip(),
        "check_date": str(record.get("check_date") or run_date),
        "safe_debug_status": str(record.get("safe_debug_status", "")).strip(),
    }


def normalize_litres_result(record: dict[str, Any], check: dict[str, Any], run_date: str) -> dict[str, Any]:
    result_url = str(record.get("result_url") or record.get("edition_url") or record.get("url", "")).strip()
    language = str(record.get("language") or record.get("language_clue", "")).strip()
    return {
        "candidate_title": str(record.get("candidate_title") or record.get("source_candidate_title") or "").strip(),
        "primary_authors": list(record.get("primary_authors", [])),
        "match_type": str(record.get("match_type", "")).strip(),
        "language": language,
        "edition_url": result_url,
        "record_url": result_url,
        "evidence_note": safe_retailer_metadata(
            str(record.get("evidence_note") or litres_evidence_note(record)).strip()
        ),
        "source_type": "russian-retailer",
        "source_name": str(record.get("source_name") or check.get("name") or check.get("retailer") or "LitRes").strip(),
        "query": str(record.get("query", "")).strip(),
        "result_title": str(record.get("result_title", "")).strip(),
        "result_author": str(record.get("result_author", "")).strip(),
        "language_clue": str(record.get("language_clue", "")).strip(),
        "format_clue": safe_retailer_metadata(str(record.get("format_clue", "")).strip()),
        "isbn": str(record.get("isbn", "")).strip(),
        "check_date": str(record.get("check_date") or run_date),
        "safe_debug_status": str(record.get("safe_debug_status", "")).strip(),
    }


def normalize_book24_result(record: dict[str, Any], check: dict[str, Any], run_date: str) -> dict[str, Any]:
    result_url = str(record.get("result_url") or record.get("edition_url") or record.get("url", "")).strip()
    language = str(record.get("language") or record.get("language_clue", "")).strip()
    return {
        "candidate_title": str(record.get("candidate_title") or record.get("source_candidate_title") or "").strip(),
        "primary_authors": list(record.get("primary_authors", [])),
        "match_type": str(record.get("match_type", "")).strip(),
        "language": language,
        "edition_url": result_url,
        "record_url": result_url,
        "evidence_note": safe_retailer_metadata(
            str(record.get("evidence_note") or book24_evidence_note(record)).strip()
        ),
        "source_type": "russian-retailer",
        "source_name": str(record.get("source_name") or check.get("name") or check.get("retailer") or "Book24").strip(),
        "query": str(record.get("query", "")).strip(),
        "result_title": str(record.get("result_title", "")).strip(),
        "result_author": str(record.get("result_author", "")).strip(),
        "language_clue": str(record.get("language_clue", "")).strip(),
        "format_clue": safe_retailer_metadata(str(record.get("format_clue", "")).strip()),
        "isbn": str(record.get("isbn", "")).strip(),
        "check_date": str(record.get("check_date") or run_date),
        "safe_debug_status": str(record.get("safe_debug_status", "")).strip(),
    }


def normalize_generic_retailer_result(record: dict[str, Any], check: dict[str, Any], run_date: str) -> dict[str, Any]:
    result_url = str(record.get("result_url") or record.get("edition_url") or record.get("url", "")).strip()
    language = str(record.get("language") or record.get("language_clue", "")).strip()
    retailer = str(check.get("retailer") or check.get("name") or "Russian retailer").strip()
    return {
        "candidate_title": str(record.get("candidate_title") or record.get("source_candidate_title") or "").strip(),
        "primary_authors": list(record.get("primary_authors", [])),
        "match_type": str(record.get("match_type", "")).strip(),
        "language": language,
        "edition_url": result_url,
        "record_url": result_url,
        "evidence_note": safe_retailer_metadata(str(record.get("evidence_note") or "").strip()),
        "source_type": "russian-retailer",
        "source_name": str(record.get("source_name") or check.get("name") or retailer).strip(),
        "query": str(record.get("query", "")).strip(),
        "result_title": str(record.get("result_title", "")).strip(),
        "result_author": str(record.get("result_author", "")).strip(),
        "language_clue": str(record.get("language_clue", "")).strip(),
        "format_clue": safe_retailer_metadata(str(record.get("format_clue", "")).strip()),
        "isbn": str(record.get("isbn", "")).strip(),
        "check_date": str(record.get("check_date") or run_date),
        "safe_debug_status": str(record.get("safe_debug_status", "")).strip(),
    }


# ── Source-specific: evidence notes ────────────────────────────────────────


def litres_evidence_note(record: dict[str, Any]) -> str:
    parts = [
        str(record.get(field, "")).strip()
        for field in ("result_title", "result_author", "language_clue", "format_clue")
        if str(record.get(field, "")).strip()
    ]
    return "LitRes metadata: " + "; ".join(parts)


def book24_evidence_note(record: dict[str, Any]) -> str:
    parts = [
        str(record.get(field, "")).strip()
        for field in ("result_title", "result_author", "language_clue", "format_clue")
        if str(record.get(field, "")).strip()
    ]
    return "Book24 metadata: " + "; ".join(parts)


def rsl_evidence_note(record: dict[str, Any]) -> str:
    parts = [
        str(record.get(field, "")).strip()
        for field in ("result_title", "result_author", "publication_data")
        if str(record.get(field, "")).strip()
    ]
    return "RSL catalog metadata: " + "; ".join(parts)


def safe_retailer_metadata(value: str, limit: int = 240) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def safe_rsl_metadata(value: str, limit: int = 240) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


# ── Source-specific: validation ────────────────────────────────────────────


def _rsl_publication_limit(check: dict[str, Any]) -> int:
    """How long the bibliographic line is kept: 240 characters unless the caller asks for more (#1181)."""
    limit = check.get("publication_data_limit")
    return limit if isinstance(limit, int) and not isinstance(limit, bool) and limit > 240 else 240


def validate_rsl_recorded_result(record: dict[str, Any], *, publication_limit: int = 240) -> None:
    forbidden = RSL_FORBIDDEN_RECORDED_FIELDS & set(record)
    if forbidden:
        raise ValueError(f"recorded RSL result stores forbidden copied text fields: {sorted(forbidden)}")
    required = {"query", "record_url", "result_title", "result_author", "source_name", "check_date"}
    missing = [field for field in sorted(required) if not str(record.get(field, "")).strip()]
    if missing:
        raise ValueError(f"recorded RSL result missing required metadata fields: {', '.join(missing)}")
    for field in ("evidence_note", "publication_data", "safe_debug_status"):
        limit = publication_limit if field == "publication_data" else 240
        if len(str(record.get(field, ""))) > limit:
            raise ValueError(f"recorded RSL result field {field} must be metadata-only and <= {limit} characters")


def validate_litres_recorded_result(record: dict[str, Any]) -> None:
    forbidden = LITRES_FORBIDDEN_RECORDED_FIELDS & set(record)
    if forbidden:
        raise ValueError(f"recorded LitRes result stores forbidden copied text fields: {sorted(forbidden)}")
    required = {"query", "result_url", "result_title", "candidate_title", "primary_authors", "match_type"}
    missing = [field for field in sorted(required) if not recorded_litres_field_present(record, field)]
    if missing:
        raise ValueError(f"recorded LitRes result missing required metadata fields: {', '.join(missing)}")
    for field in ("evidence_note", "format_clue", "safe_debug_status"):
        if len(str(record.get(field, ""))) > 240:
            raise ValueError(f"recorded LitRes result field {field} must be metadata-only and <= 240 characters")


def validate_book24_recorded_result(record: dict[str, Any]) -> None:
    forbidden = BOOK24_FORBIDDEN_RECORDED_FIELDS & set(record)
    if forbidden:
        raise ValueError(f"recorded Book24 result stores forbidden copied text fields: {sorted(forbidden)}")
    required = {"query", "result_url", "result_title", "candidate_title", "primary_authors", "match_type"}
    missing = [field for field in sorted(required) if not recorded_litres_field_present(record, field)]
    if missing:
        raise ValueError(f"recorded Book24 result missing required metadata fields: {', '.join(missing)}")
    for field in ("evidence_note", "format_clue", "safe_debug_status"):
        if len(str(record.get(field, ""))) > 240:
            raise ValueError(f"recorded Book24 result field {field} must be metadata-only and <= 240 characters")


def validate_generic_retailer_recorded_result(record: dict[str, Any]) -> None:
    forbidden = RSL_FORBIDDEN_RECORDED_FIELDS & set(record)
    if forbidden:
        raise ValueError(f"recorded Russian retailer result stores forbidden copied text fields: {sorted(forbidden)}")
    required = {"query", "result_title", "candidate_title", "primary_authors", "match_type"}
    missing = [field for field in sorted(required) if not recorded_litres_field_present(record, field)]
    if not any(str(record.get(field, "")).strip() for field in ("result_url", "edition_url", "url")):
        missing.append("result_url")
    if missing:
        raise ValueError(f"recorded Russian retailer result missing required metadata fields: {', '.join(missing)}")
    for field in ("evidence_note", "format_clue", "safe_debug_status"):
        if len(str(record.get(field, ""))) > 240:
            raise ValueError(
                f"recorded Russian retailer result field {field} must be metadata-only and <= 240 characters"
            )


def recorded_litres_field_present(record: dict[str, Any], field: str) -> bool:
    if field == "primary_authors":
        return bool(record.get(field))
    return bool(str(record.get(field, "")).strip())


# ── Source-specific: RSL live search (unique AJAX+CSRF mechanism) ──────────


def rsl_live_search_response(check: dict[str, Any], query: str) -> tuple[str, str]:
    base_url = str(check.get("url") or "https://search.rsl.ru/").strip()
    timeout = float(check.get("timeout_seconds", 10))
    # The Russian-edition completion pass (#1181) names itself; every other caller keeps this name.
    user_agent = str(check.get("user_agent") or "AutoScout RSL metadata check")
    root_url = base_url.rstrip("/")
    if root_url.endswith("/ru"):
        root_url = root_url[:-3]
    cookie_jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    try:
        bootstrap = opener.open(
            Request(
                f"{root_url}/ru/search",
                headers={"User-Agent": user_agent},
            ),
            timeout=timeout,
        )
        bootstrap_text = bootstrap.read(300_000).decode(
            bootstrap.headers.get_content_charset() or "utf-8",
            errors="replace",
        )
        csrf = csrf_token(bootstrap_text)
        data = urlencode({"SearchFilterForm[search]": query}).encode()
        request = Request(
            f"{root_url}/site/ajax-search?language=ru",
            data=data,
            headers={
                "User-Agent": user_agent,
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
            },
        )
        with opener.open(request, timeout=timeout) as response:
            body = response.read(500_000)
            text = body.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    except TimeoutError:
        return "", "RSL live check failed: request timed out"
    except HTTPError as exc:
        if exc.code in {401, 403, 429}:
            return "", f"RSL live check blocked by HTTP {exc.code}"
        return "", f"RSL live check failed: HTTP {exc.code}"
    except URLError as exc:
        return "", f"RSL live check failed: {exc.reason}"
    return text, _blocked_response_reason(_RSL_SPEC, text)


# ── Source-specific: retailer live search (shared LitRes + Book24 pattern) ─


def _retailer_live_search_response(spec: _SourceSpec, check: dict[str, Any], query: str) -> tuple[str, str]:
    base_url = str(check.get("url") or spec._retailer_default_url).strip().rstrip("/")
    timeout = float(check.get("timeout_seconds", 10))
    search_path = str(check.get("search_path") or spec._retailer_search_path)
    if not search_path.startswith("/"):
        search_path = "/" + search_path
    query_param = str(check.get("query_param") or spec._retailer_query_param)
    url = f"{base_url}{search_path}?{urlencode({query_param: query})}"
    try:
        request = Request(
            url,
            headers={
                "User-Agent": f"AutoScout {spec.name} metadata check",
                "Accept": "text/html,application/json",
            },
        )
        with urlopen(request, timeout=timeout) as response:
            body = response.read(500_000)
            text = body.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    except TimeoutError:
        return "", f"{spec.name} live check failed: request timed out"
    except HTTPError as exc:
        if exc.code in {401, 403, 429}:
            return "", f"{spec.name} live check blocked by HTTP {exc.code}"
        return "", f"{spec.name} live check failed: HTTP {exc.code}"
    except URLError as exc:
        return "", f"{spec.name} live check failed: {exc.reason}"
    return text, _blocked_response_reason(spec, text)


# ── Source-specific: RSL HTML / JSON parsing ───────────────────────────────


def parse_rsl_live_response(
    response_text: str,
    query: str,
    candidate: dict[str, Any],
    check: dict[str, Any],
    run_date: str,
) -> tuple[list[dict[str, Any]], str]:
    return _parse_live_response(_RSL_SPEC, response_text, query, candidate, check, run_date)


def parse_rsl_json_response(
    response_text: str,
    query: str,
    candidate: dict[str, Any],
    check: dict[str, Any],
    run_date: str,
) -> tuple[list[dict[str, Any]], str]:
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError:
        return [], "RSL live check failed: response JSON could not be parsed safely"
    if isinstance(data, dict) and isinstance(data.get("content"), str):
        return parse_rsl_html_response(data["content"], query, candidate, check, run_date)
    records = data.get("results", data.get("records", data)) if isinstance(data, dict) else data
    if not isinstance(records, list):
        return [], "RSL live check failed: response JSON could not be parsed safely"
    results = []
    for record in records:
        if not isinstance(record, dict):
            continue
        normalized_record = {
            "query": str(record.get("query") or query),
            "record_url": str(record.get("record_url") or record.get("url") or ""),
            "result_title": str(record.get("result_title") or record.get("title") or ""),
            "result_author": str(record.get("result_author") or record.get("author") or ""),
            "language_clue": str(record.get("language_clue") or record.get("language") or ""),
            "publication_data": safe_rsl_metadata(
                str(record.get("publication_data") or record.get("publication") or ""), _rsl_publication_limit(check)
            ),
            "isbn": str(record.get("isbn") or ""),
            "source_name": str(record.get("source_name") or check.get("name") or "RSL"),
            "check_date": str(record.get("check_date") or run_date),
            "safe_debug_status": str(record.get("safe_debug_status") or "live metadata parse"),
            "candidate_title": str(record.get("candidate_title") or candidate.get("title", "")),
            "primary_authors": list(record.get("primary_authors") or candidate.get("primary_authors", [])),
            "match_type": str(record.get("match_type") or _infer_russian_match_type(record, candidate)),
        }
        try:
            validate_rsl_recorded_result(normalized_record, publication_limit=_rsl_publication_limit(check))
        except ValueError:
            return [], "RSL live check failed: response metadata could not be parsed safely"
        results.append(normalize_rsl_result(normalized_record, check, run_date))
    return results, ""


def parse_rsl_html_response(
    response_text: str,
    query: str,
    candidate: dict[str, Any],
    check: dict[str, Any],
    run_date: str,
) -> tuple[list[dict[str, Any]], str]:
    cards = re.findall(
        r'<div\b[^>]*class="[^"]*search-container[^"]*"[^>]*>(.*?)(?=<div\b[^>]*class="[^"]*search-container|\Z)',
        response_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if cards:
        return parse_rsl_search_container_cards(cards, query, candidate, check, run_date), ""

    cards = re.findall(r"<article\b[^>]*>(.*?)</article>", response_text, flags=re.IGNORECASE | re.DOTALL)
    if not cards:
        cards = re.findall(
            r'<div\b[^>]*class="[^"]*(?:record|result|document)[^"]*"[^>]*>(.*?)</div>',
            response_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
    if not cards:
        if any(marker in response_text.lower() for marker in ("rsl", "search", "result", "record", "найден")):
            return [], ""
        return [], "RSL live check failed: response markup could not be parsed safely"
    results = []
    for card in cards:
        record_url = html_attr(card, "href")
        if record_url:
            record_url = urljoin(str(check.get("url") or "https://search.rsl.ru/"), record_url)
        title = html_text(first_tag(card, ("h1", "h2", "h3", "a")))
        text = html_text(card)
        author = labeled_text(text, ("Author", "Автор"))
        language_clue = labeled_text(text, ("Language", "Язык"))
        publication_data = labeled_text(text, ("Publication", "Выходные данные", "Published"))
        isbn = labeled_text(text, ("ISBN",))
        record = {
            "query": query,
            "record_url": record_url,
            "result_title": title,
            "result_author": author,
            "language_clue": language_clue,
            "publication_data": publication_data,
            "isbn": isbn,
            "source_name": str(check.get("name") or "RSL"),
            "check_date": run_date,
            "safe_debug_status": "live HTML metadata parse",
            "candidate_title": str(candidate.get("title", "")),
            "primary_authors": list(candidate.get("primary_authors", [])),
            "match_type": _infer_russian_match_type(record_from_html(title, author, language_clue), candidate),
        }
        try:
            validate_rsl_recorded_result(record, publication_limit=_rsl_publication_limit(check))
        except ValueError:
            continue
        results.append(normalize_rsl_result(record, check, run_date))
    return results, ""


_RSL_AUTHOR_OPEN = re.compile(r'<b\b[^>]*class="[^"]*js-item-authorinfo[^"]*"[^>]*>', re.IGNORECASE)
_B_TAG = re.compile(r"<(/?)b\b[^>]*>", re.IGNORECASE)


def _rsl_complete_author(card: str) -> str:
    """The whole author of a State Library card, nested name parts included, or empty."""
    opening = _RSL_AUTHOR_OPEN.search(card)
    if opening is None:
        return ""
    depth = 1
    for tag in _B_TAG.finditer(card, opening.end()):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return re.sub(r"\s+([,.;])", r"\1", html_text(card[opening.end() : tag.start()])).rstrip(".")
    return ""


def parse_rsl_search_container_cards(
    cards: list[str],
    query: str,
    candidate: dict[str, Any],
    check: dict[str, Any],
    run_date: str,
) -> list[dict[str, Any]]:
    results = []
    for card in cards:
        description_link = re.search(
            r'<a\b[^>]*href=["\']([^"\']+/record/[^"\']+)["\'][^>]*>\s*Описание\s*</a>',
            card,
            flags=re.IGNORECASE | re.DOTALL,
        )
        record_url = html.unescape(description_link.group(1).strip()) if description_link else html_attr(card, "href")
        if record_url:
            record_url = urljoin(str(check.get("url") or "https://search.rsl.ru/"), record_url)
        author_match = re.search(
            r'<b\b[^>]*class="[^"]*js-item-authorinfo[^"]*"[^>]*>(.*?)</b>',
            card,
            flags=re.IGNORECASE | re.DOTALL,
        )
        main_info_match = re.search(
            r'<span\b[^>]*class="[^"]*js-item-maininfo[^"]*"[^>]*>(.*?)</span>',
            card,
            flags=re.IGNORECASE | re.DOTALL,
        )
        cover_descr_match = re.search(
            r'<div\b[^>]*class="[^"]*rsl-item-nocover-descr[^"]*"[^>]*>(.*?)</div>',
            card,
            flags=re.IGNORECASE | re.DOTALL,
        )
        main_info = html_text(main_info_match.group(1)) if main_info_match else ""
        cover_descr = html_text(cover_descr_match.group(1)) if cover_descr_match else ""
        author = html_text(author_match.group(1)).rstrip(".") if author_match else ""
        if check.get("complete_author"):
            # The author tag nests a <b> per name part, so the match above
            # stops after the surname; the Russian-edition pass (#1181) asks
            # for the whole name («Уолкер, Мэттью»). Other callers keep theirs.
            author = _rsl_complete_author(card) or author
        title = (main_info or cover_descr).split("//", 1)[0].strip()
        record = {
            "query": query,
            "record_url": record_url,
            "result_title": title,
            "result_author": author or labeled_text(main_info, ("Author", "Автор")),
            "language_clue": labeled_text(main_info, ("Language", "Язык")) or "Russian",
            "publication_data": safe_rsl_metadata(main_info, _rsl_publication_limit(check)),
            "isbn": labeled_text(main_info, ("ISBN",)),
            "source_name": str(check.get("name") or "RSL"),
            "check_date": run_date,
            "safe_debug_status": "live RSL search-container metadata parse",
            "candidate_title": str(candidate.get("title", "")),
            "primary_authors": list(candidate.get("primary_authors", [])),
            "match_type": _infer_russian_match_type(record_from_html(title, author, "Russian"), candidate),
        }
        try:
            validate_rsl_recorded_result(record, publication_limit=_rsl_publication_limit(check))
        except ValueError:
            continue
        results.append(normalize_rsl_result(record, check, run_date))
    return results


# ── Source-specific: retailer HTML / JSON parsing (shared LitRes + Book24) ─


def parse_litres_live_response(
    response_text: str,
    query: str,
    candidate: dict[str, Any],
    check: dict[str, Any],
    run_date: str,
) -> tuple[list[dict[str, Any]], str]:
    return _parse_live_response(_LITRES_SPEC, response_text, query, candidate, check, run_date)


def parse_book24_live_response(
    response_text: str,
    query: str,
    candidate: dict[str, Any],
    check: dict[str, Any],
    run_date: str,
) -> tuple[list[dict[str, Any]], str]:
    return _parse_live_response(_BOOK24_SPEC, response_text, query, candidate, check, run_date)


def _retailer_parse_html_response(
    spec: _SourceSpec,
    response_text: str,
    query: str,
    candidate: dict[str, Any],
    check: dict[str, Any],
    run_date: str,
) -> tuple[list[dict[str, Any]], str]:
    cards = re.findall(
        spec._retailer_html_card_pattern,
        response_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not cards:
        links = re.findall(
            spec._retailer_html_link_pattern,
            response_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        cards = [f'<a href="{href}">{label}</a>' for href, label in links[:10]]
    if not cards:
        if any(marker in response_text.lower() for marker in spec._retailer_html_markers):
            return [], ""
        return [], f"{spec.name} live check failed: response markup could not be parsed safely"
    results = []
    for card in cards[:10]:
        record_url = html_attr(card, "href")
        if record_url:
            record_url = urljoin(str(check.get("url") or spec._retailer_default_url), record_url)
        title = html_text(first_tag(card, ("h1", "h2", "h3", "a")))
        text = html_text(card)
        author = (
            meta_attr(card, "author")
            or clean_retailer_labeled_value(labeled_text(text, ("Author", "Автор", "Авторы")))
            or author_from_retailer_text(text, title)
        )
        language_clue = clean_retailer_labeled_value(labeled_text(text, ("Language", "Язык"))) or "Russian"
        format_clue = clean_retailer_labeled_value(labeled_text(text, ("Format", "Формат")))
        isbn = labeled_text(text, ("ISBN",))
        record = {
            "query": query,
            "result_url": record_url,
            "result_title": title,
            "result_author": author,
            "language_clue": language_clue,
            "format_clue": safe_retailer_metadata(format_clue),
            "isbn": isbn,
            "source_name": str(check.get("name") or check.get("retailer") or spec.source_name_default),
            "check_date": run_date,
            "safe_debug_status": f"live {spec.name} HTML metadata parse",
            "candidate_title": str(candidate.get("title", "")),
            "primary_authors": list(candidate.get("primary_authors", [])),
            "match_type": _infer_russian_match_type(record_from_html(title, author, language_clue), candidate),
        }
        try:
            spec.validate_recorded_result(record)
        except ValueError:
            continue
        results.append(spec.normalize_recorded_result(record, check, run_date))
    return results, ""


def parse_litres_html_response(
    response_text: str,
    query: str,
    candidate: dict[str, Any],
    check: dict[str, Any],
    run_date: str,
) -> tuple[list[dict[str, Any]], str]:
    return _retailer_parse_html_response(_LITRES_SPEC, response_text, query, candidate, check, run_date)


def parse_book24_html_response(
    response_text: str,
    query: str,
    candidate: dict[str, Any],
    check: dict[str, Any],
    run_date: str,
) -> tuple[list[dict[str, Any]], str]:
    return _retailer_parse_html_response(_BOOK24_SPEC, response_text, query, candidate, check, run_date)


def _retailer_parse_json_response(
    spec: _SourceSpec,
    response_text: str,
    query: str,
    candidate: dict[str, Any],
    check: dict[str, Any],
    run_date: str,
) -> tuple[list[dict[str, Any]], str]:
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError:
        return [], f"{spec.name} live check failed: response JSON could not be parsed safely"
    if isinstance(data, dict) and isinstance(data.get("content"), str):
        return _retailer_parse_html_response(spec, data["content"], query, candidate, check, run_date)
    records = (
        data.get(
            spec._retailer_json_results_keys[0],
            data.get(spec._retailer_json_results_keys[1], data.get(spec._retailer_json_results_keys[2], data)),
        )
        if isinstance(data, dict)
        else data
    )
    if not isinstance(records, list):
        return [], f"{spec.name} live check failed: response JSON could not be parsed safely"
    results = []
    for record in records:
        if not isinstance(record, dict):
            continue
        url = str(
            record.get(spec._retailer_json_url_keys[0])
            or record.get(spec._retailer_json_url_keys[1])
            or record.get(spec._retailer_json_url_keys[2])
            or ""
        )
        if url:
            url = urljoin(str(check.get("url") or spec._retailer_default_url), url)
        normalized_record = {
            "query": str(record.get("query") or query),
            "result_url": url,
            "result_title": str(
                record.get(spec._retailer_json_title_keys[0])
                or record.get(spec._retailer_json_title_keys[1])
                or record.get(spec._retailer_json_title_keys[2])
                or ""
            ),
            "result_author": str(
                record.get(spec._retailer_json_author_keys[0])
                or record.get(spec._retailer_json_author_keys[1])
                or record.get(spec._retailer_json_author_keys[2])
                or ""
            ),
            "language_clue": str(record.get("language_clue") or record.get("language") or "Russian"),
            "format_clue": safe_retailer_metadata(str(record.get("format_clue") or record.get("format") or "")),
            "isbn": str(record.get("isbn") or ""),
            "source_name": str(
                record.get("source_name") or check.get("name") or check.get("retailer") or spec.source_name_default
            ),
            "check_date": str(record.get("check_date") or run_date),
            "safe_debug_status": str(record.get("safe_debug_status") or f"live {spec.name} JSON metadata parse"),
            "candidate_title": str(record.get("candidate_title") or candidate.get("title", "")),
            "primary_authors": list(record.get("primary_authors") or candidate.get("primary_authors", [])),
            "match_type": str(record.get("match_type") or _infer_russian_match_type(record, candidate)),
        }
        try:
            spec.validate_recorded_result(normalized_record)
        except ValueError:
            return [], f"{spec.name} live check failed: response metadata could not be parsed safely"
        results.append(spec.normalize_recorded_result(normalized_record, check, run_date))
    return results, ""


def parse_litres_json_response(
    response_text: str,
    query: str,
    candidate: dict[str, Any],
    check: dict[str, Any],
    run_date: str,
) -> tuple[list[dict[str, Any]], str]:
    return _retailer_parse_json_response(_LITRES_SPEC, response_text, query, candidate, check, run_date)


def parse_book24_json_response(
    response_text: str,
    query: str,
    candidate: dict[str, Any],
    check: dict[str, Any],
    run_date: str,
) -> tuple[list[dict[str, Any]], str]:
    return _retailer_parse_json_response(_BOOK24_SPEC, response_text, query, candidate, check, run_date)


# ── Legacy live-search wrappers (kept for backward compatibility) ──────────


def litres_live_search_response(check: dict[str, Any], query: str) -> tuple[str, str]:
    return _retailer_live_search_response(_LITRES_SPEC, check, query)


def book24_live_search_response(check: dict[str, Any], query: str) -> tuple[str, str]:
    return _retailer_live_search_response(_BOOK24_SPEC, check, query)


# ── Legacy recorded-records wrappers (kept for backward compatibility) ─────


def rsl_recorded_records(config_path: Path, check: dict[str, Any]) -> list[dict[str, Any]]:
    return _recorded_records(_RSL_SPEC, config_path, check)


def litres_recorded_records(config_path: Path, check: dict[str, Any]) -> list[dict[str, Any]]:
    return _recorded_records(_LITRES_SPEC, config_path, check)


def book24_recorded_records(config_path: Path, check: dict[str, Any]) -> list[dict[str, Any]]:
    return _recorded_records(_BOOK24_SPEC, config_path, check)


# ── Legacy live-results wrappers ───────────────────────────────────────────


def rsl_live_results_for_candidate(
    config_path: Path,
    check: dict[str, Any],
    candidate: dict[str, Any],
    run_date: str,
) -> tuple[list[dict[str, Any]], str]:
    return _live_results_for_candidate(_RSL_SPEC, config_path, check, candidate, run_date)


def litres_live_results_for_candidate(
    config_path: Path,
    check: dict[str, Any],
    candidate: dict[str, Any],
    run_date: str,
) -> tuple[list[dict[str, Any]], str]:
    return _live_results_for_candidate(_LITRES_SPEC, config_path, check, candidate, run_date)


def book24_live_results_for_candidate(
    config_path: Path,
    check: dict[str, Any],
    candidate: dict[str, Any],
    run_date: str,
) -> tuple[list[dict[str, Any]], str]:
    return _live_results_for_candidate(_BOOK24_SPEC, config_path, check, candidate, run_date)


# ── Legacy match-type wrappers ─────────────────────────────────────────────


def infer_rsl_match_type(record: dict[str, Any], candidate: dict[str, Any]) -> str:
    return _infer_russian_match_type(record, candidate)


def infer_litres_match_type(record: dict[str, Any], candidate: dict[str, Any]) -> str:
    return _infer_russian_match_type(record, candidate)


def infer_book24_match_type(record: dict[str, Any], candidate: dict[str, Any]) -> str:
    return _infer_russian_match_type(record, candidate)


# ── Legacy blocked/error wrappers ──────────────────────────────────────────


def rsl_blocked_response_reason(text: str) -> str:
    return _blocked_response_reason(_RSL_SPEC, text)


def litres_blocked_response_reason(text: str) -> str:
    return _blocked_response_reason(_LITRES_SPEC, text)


def book24_blocked_response_reason(text: str) -> str:
    return _blocked_response_reason(_BOOK24_SPEC, text)


def is_litres_live_blocked_error(error: str) -> bool:
    return _blocked_error(_LITRES_SPEC, error)


def is_book24_live_blocked_error(error: str) -> bool:
    return _blocked_error(_BOOK24_SPEC, error)


def is_rsl_live_blocked_error(error: str) -> bool:
    return _blocked_error(_RSL_SPEC, error)


def rsl_live_status_error(status: str) -> str:
    return _live_status_error(_RSL_SPEC, status)


def litres_live_status_error(status: str) -> str:
    return _live_status_error(_LITRES_SPEC, status)


def book24_live_status_error(status: str) -> str:
    return _live_status_error(_BOOK24_SPEC, status)


# ── HTML helpers ───────────────────────────────────────────────────────────


def record_from_html(title: str, author: str, language_clue: str) -> dict[str, str]:
    return {"title": title, "author": author, "language": language_clue}


def record_from_litres_html(title: str, author: str, language_clue: str) -> dict[str, str]:
    return {"title": title, "author": author, "language": language_clue}


def same_name_tokens(left: str, right: str) -> bool:
    left_tokens = {token for token in left.split() if token}
    right_tokens = {token for token in right.split() if token}
    return bool(left_tokens) and left_tokens == right_tokens


def is_russian_language_clue(language: str) -> bool:
    normalized = language.strip().lower()
    return (
        normalized in {"russian", "русский", "ru"}
        or normalized.startswith("russian ")
        or normalized.startswith("русский ")
    )


def html_attr(markup: str, attr: str) -> str:
    match = re.search(rf'\b{re.escape(attr)}=["\']([^"\']+)["\']', markup, flags=re.IGNORECASE)
    return html.unescape(match.group(1).strip()) if match else ""


def meta_attr(markup: str, name: str) -> str:
    match = re.search(
        rf'<meta\b[^>]*(?:name|property)=["\'][^"\']*{re.escape(name)}[^"\']*["\'][^>]*content=["\']([^"\']+)["\']',
        markup,
        flags=re.IGNORECASE,
    )
    return html.unescape(match.group(1).strip()) if match else ""


def author_from_retailer_text(text: str, title: str) -> str:
    if title and text.startswith(title):
        text = text[len(title) :].strip(" \n\r\t-:,.")
    return text.splitlines()[0].strip()[:120] if text else ""


# Aliases (kept for backward compatibility)
author_from_litres_text = author_from_retailer_text


def clean_retailer_labeled_value(value: str) -> str:
    return re.split(r"\s+(?:Author|Автор|Авторы|Language|Язык|Format|Формат|ISBN)\s*[:：]", value, maxsplit=1)[
        0
    ].strip()


# Aliases (kept for backward compatibility)
clean_litres_labeled_value = clean_retailer_labeled_value


def csrf_token(markup: str) -> str:
    match = re.search(
        r'<meta\b[^>]*name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)["\']',
        markup,
        flags=re.IGNORECASE,
    )
    return html.unescape(match.group(1).strip()) if match else ""


def first_tag(markup: str, tags: tuple[str, ...]) -> str:
    for tag in tags:
        match = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", markup, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1)
    return ""


def html_text(markup: str) -> str:
    text = re.sub(r"<[^>]+>", " ", markup)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def labeled_text(text: str, labels: tuple[str, ...]) -> str:
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*[:：]\s*([^|;\n]+)", text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


# ── RSL capture ────────────────────────────────────────────────────────────


def capture_rsl_recorded_results(output_dir: Path, check: dict[str, Any], results: list[dict[str, Any]]) -> None:
    raw_path = check.get("capture_recorded_results_file")
    if not raw_path:
        return
    path = Path(str(raw_path))
    if not path.is_absolute():
        path = output_dir / path
    write_json(path, {"results": [rsl_capture_record(result) for result in results]})


def rsl_capture_record(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "query": str(result.get("query", "")),
        "record_url": str(result.get("record_url", "")),
        "result_title": str(result.get("result_title", "")),
        "result_author": str(result.get("result_author", "")),
        "language_clue": str(result.get("language_clue", "")),
        "publication_data": str(result.get("publication_data", "")),
        "isbn": str(result.get("isbn", "")),
        "source_name": str(result.get("source_name", "")),
        "check_date": str(result.get("check_date", "")),
        "safe_debug_status": str(result.get("safe_debug_status", "")),
        "candidate_title": str(result.get("candidate_title", "")),
        "primary_authors": list(result.get("primary_authors", [])),
        "match_type": str(result.get("match_type", "")),
    }


# ── Source classification helpers ──────────────────────────────────────────


def is_configured_russian_retailer(check: dict[str, Any]) -> bool:
    retailer = str(check.get("retailer") or check.get("name", "")).strip().lower()
    return retailer in RUSSIAN_EDITION_RETAILERS


def is_rsl_check(check: dict[str, Any]) -> bool:
    family = str(check.get("family") or check.get("source_family", "")).strip().lower()
    name = str(check.get("name") or check.get("retailer", "")).strip().lower()
    return family in RUSSIAN_LIBRARY_CATALOG_FAMILIES or name in RSL_NAMES


def is_litres_check(check: dict[str, Any]) -> bool:
    retailer = str(check.get("retailer") or check.get("name", "")).strip().lower()
    return retailer in LITRES_NAMES


def is_book24_check(check: dict[str, Any]) -> bool:
    retailer = str(check.get("retailer") or check.get("name", "")).strip().lower()
    return retailer in BOOK24_NAMES


def is_generic_russian_retailer_check(check: dict[str, Any]) -> bool:
    retailer = str(check.get("retailer") or check.get("name", "")).strip().lower()
    return retailer in (OZON_NAMES | LABIRINT_NAMES | CHITAI_GOROD_NAMES)


def is_deferred_russian_publisher_check(check: dict[str, Any]) -> bool:
    """Keep publisher-site checks outside the operational retailer set."""
    family = str(check.get("family") or check.get("source_family", "")).strip().lower()
    name = str(check.get("retailer") or check.get("name", "")).strip().lower()
    return "publisher-site" in family or "publisher site" in family or "publisher site" in name


def is_configured_russian_check(check: dict[str, Any]) -> bool:
    """Return whether a declared provider belongs to required coverage.

    Recognized retailer and RSL checks are required.
    Unknown providers are also required when their retailer or name field is
    a nonblank string, so they are reported as unimplemented.
    Empty or malformed undeclared checks are ignored.
    Russian publisher-site checks remain deferred.
    """
    if not isinstance(check, dict) or is_deferred_russian_publisher_check(check):
        return False
    if is_rsl_check(check) or is_configured_russian_retailer(check):
        return True
    return any(isinstance(check.get(field), str) and check[field].strip() for field in ("retailer", "name"))


def russian_check_source_type(check: dict[str, Any]) -> str:
    if is_rsl_check(check):
        return "russian-library-catalog"
    if is_configured_russian_check(check):
        return "russian-retailer"
    return "deferred"


# ── Run status helpers ─────────────────────────────────────────────────────


def russian_edition_check_runs(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    runs = []
    for check in checks:
        is_configured = is_configured_russian_check(check)
        status = russian_check_run_status(check, is_configured)
        run = {
            "retailer": str(check.get("retailer") or check.get("name", "")),
            "url": str(check.get("url", "")),
            "status": status,
            "reason": russian_check_run_reason(check, is_configured, status),
            "fixture_result_count": len(check.get("fixture_results", [])),
        }
        if is_rsl_check(check):
            run["source_type"] = "russian-library-catalog"
        runs.append(run)
    return runs


def russian_check_run_status(check: dict[str, Any], is_configured: bool) -> str:
    if not is_configured:
        return "deferred"
    if str(check.get("access_status", "")).strip().lower() == "blocked" or check.get("live_blocked_reason"):
        return "blocked"
    if check.get("active", True) is False:
        return "skipped"
    if check.get("live_unimplemented_reason") or russian_check_adapter(check) is None:
        return "unimplemented"
    if is_skipped_source(check):
        return "skipped"
    if check.get("fixture_error"):
        return "failed"
    if check.get("enable_live"):
        return str(check.get("live_run_status", "not_run"))
    if not check.get("fixture_results"):
        return "not_run"
    return "checked"


def russian_check_run_reason(check: dict[str, Any], is_configured: bool, status: str) -> str:
    if not is_configured:
        return "Russian-edition MVP checks are scoped to Ozon, Labirint, LitRes, Chitai-gorod, and Book24"
    if status == "blocked":
        return str(check.get("live_run_reason") or check.get("live_blocked_reason", "")) or "Provider access is blocked"
    if status == "unimplemented":
        return (
            str(check.get("live_run_reason") or check.get("live_unimplemented_reason", ""))
            or "Provider adapter is not implemented"
        )
    if status == "skipped":
        return str(check.get("live_blocked_reason", "")) or skipped_reason(check)
    if status == "failed":
        return str(check.get("fixture_error") or check.get("live_run_reason", ""))
    if status == "not_run":
        if check.get("fixture_results_present") or check.get("fixture_live_results_present"):
            return "Empty fixture is not proof of absence"
        return "Provider check was not executed"
    return ""


def russian_check_skipped_count(checks: list[dict[str, Any]]) -> int:
    return sum(1 for run in russian_edition_check_runs(checks) if run["status"] == "skipped")


def russian_check_failed_count(checks: list[dict[str, Any]]) -> int:
    return sum(1 for run in russian_edition_check_runs(checks) if run["status"] == "failed")


def russian_check_blocked_count(checks: list[dict[str, Any]]) -> int:
    return sum(
        1
        for check in checks
        if is_configured_russian_check(check)
        and (
            str(check.get("access_status", "")).strip().lower() == "blocked"
            or bool(check.get("live_blocked_reason"))
            or russian_check_run_status(check, True) == "blocked"
        )
    )


def russian_check_unimplemented_count(checks: list[dict[str, Any]]) -> int:
    return sum(1 for run in russian_edition_check_runs(checks) if run["status"] == "unimplemented")


def configured_russian_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return every configured provider, including providers that did not run.

    Status decisions must use this complete required set, otherwise a skipped
    provider silently becomes a negative result.
    """
    return [check for check in checks if is_configured_russian_check(check)]


def _provider_result_classification(
    results: list[dict[str, Any]], run_date: str
) -> tuple[str, str, list[dict[str, Any]]]:
    """Classify one provider's candidate-scoped results conservatively.

    Ambiguity wins over a positive result.  A positive and an explicit
    no-match in one provider are also conflicting evidence.  This precedence
    is deterministic and keeps identity uncertainty from becoming exclusion.
    """
    evidence: list[dict[str, Any]] = [russian_edition_evidence(result, run_date) for result in results]
    outcomes: list[str] = []
    reasons: list[str] = []
    for result in results:
        status = str(result.get("status", "")).strip().lower()
        if status == "no_match" or is_imported_foreign_language_listing(result):
            outcomes.append("no_match")
            if is_imported_foreign_language_listing(result):
                reasons.append("imported foreign-language listing")
        elif is_ambiguous_russian_match(result):
            outcomes.append("ambiguous")
            reasons.append("ambiguous title or author match")
        elif is_same_work_russian_edition(result):
            outcomes.append("matched")
        else:
            outcomes.append("ambiguous")
            reasons.append("identity or language evidence was not verified")
    if "ambiguous" in outcomes:
        return "ambiguous", "; ".join(dict.fromkeys(reasons)) or "ambiguous provider evidence", evidence
    if "matched" in outcomes and "no_match" in outcomes:
        return "ambiguous", "conflicting provider evidence", evidence
    if "matched" in outcomes:
        return "matched", "", evidence
    if "no_match" in outcomes:
        return "no_match", "; ".join(dict.fromkeys(reasons)), evidence
    return "ambiguous", "provider returned no recognized result", evidence


def russian_provider_assessment(
    check: dict[str, Any],
    candidate: dict[str, Any],
    run_date: str,
    live_evidence: LiveCheckEvidence | None = None,
) -> dict[str, Any]:
    """Return auditable candidate-level execution truth for one provider."""
    name = russian_check_name(check)
    source_type = russian_check_source_type(check)
    base: dict[str, Any] = {
        "name": name,
        "source_type": source_type,
        "status": "not_run",
        "outcome": None,
        "reason": "",
        "checked_at": run_date,
        "evidence_urls": [],
        "evidence": [],
        "result_count": 0,
    }
    access_status = str(check.get("access_status", "")).strip().lower()
    if access_status == "blocked":
        base.update(status="blocked", outcome="blocked", reason="Provider access is blocked")
        return base
    if check.get("active", True) is False:
        base.update(status="skipped", reason="Provider is disabled or skipped by configuration")
        return base
    if russian_check_adapter(check) is None:
        base.update(status="unimplemented", outcome="unimplemented", reason="Provider adapter is not implemented")
        return base
    if is_skipped_source(check):
        base.update(status="skipped", reason="Provider is disabled or skipped by configuration")
        return base

    key = _candidate_key(candidate)
    results: list[dict[str, Any]] = []
    if check.get("enable_live"):
        if check.get("fixture_error"):
            base.update(status="failed", outcome="failed", reason="Provider fixture failed")
            return base
        live = live_evidence.for_candidate(key) if live_evidence is not None else LiveCandidateEvidence("not_run")
        if live.status != "checked":
            if live.status == "blocked":
                base.update(status="blocked", outcome="blocked", reason=live.reason)
            elif live.status == "failed":
                base.update(status="failed", outcome="failed", reason=live.reason)
            elif live.status == "unimplemented":
                base.update(status="unimplemented", outcome="unimplemented", reason=live.reason)
            else:
                base["reason"] = live.reason or "Live check was not executed for this candidate"
            return base
        results = list(live.results)
    else:
        if check.get("fixture_error"):
            base.update(status="failed", outcome="failed", reason="Provider fixture failed")
            return base
        raw_results = list(check.get("fixture_results", []))
        if not raw_results:
            base["reason"] = "Empty fixture is not proof of absence"
            return base
        results = [
            result
            for result in raw_results
            if isinstance(result, dict) and russian_result_matches_candidate(result, candidate)
        ]
        if not results:
            base["reason"] = "Recorded provider evidence did not run this candidate"
            return base

    # Live fixture results have already been candidate-bound by the adapter,
    # but retain the exact matcher here as defense in depth.
    results = [result for result in results if russian_result_matches_candidate(result, candidate)]
    results = [
        result | {"retailer": name, "source_type": source_type} for result in results if isinstance(result, dict)
    ]
    evidence: list[dict[str, Any]]
    if not results:
        outcome, reason, evidence = "no_match", "Live response parsed safely with no matching result", []
    else:
        outcome, reason, evidence = _provider_result_classification(results, run_date)
    urls = []
    for item in evidence:
        url = str(item.get("url", "")).strip()
        if url and url not in urls:
            urls.append(url)
    base.update(
        status="checked",
        outcome=outcome,
        reason=reason,
        evidence_urls=urls,
        evidence=evidence,
        result_count=len(results),
    )
    return base


def russian_edition_verification(
    checks: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    run_date: str,
    live_evidence: tuple[LiveCheckEvidence, ...] = (),
) -> dict[str, dict[str, Any]]:
    """Build candidate/provider verification state for all required checks."""
    verification_by_key: dict[str, dict[str, Any]] = {}
    required_checks = configured_russian_checks(checks)
    live_by_index = {item.check_index: item for item in live_evidence}
    for candidate in candidates:
        providers = []
        for check_index, check in enumerate(required_checks):
            providers.append(
                russian_provider_assessment(
                    check,
                    candidate,
                    run_date,
                    live_by_index.get(check_index),
                )
            )
        statuses = [str(provider.get("status", "not_run")) for provider in providers]
        checked_count = sum(status == "checked" for status in statuses)
        if providers and checked_count == len(providers):
            coverage = "complete"
        elif checked_count:
            coverage = "partial"
        elif any(status == "failed" for status in statuses):
            coverage = "failed"
        else:
            coverage = "not_run"

        outcomes = [
            str(provider["outcome"]) for provider in providers if provider.get("outcome") in RUSSIAN_PROVIDER_OUTCOMES
        ]
        if "ambiguous" in outcomes:
            aggregate: str | None = "ambiguous"
        elif "matched" in outcomes:
            aggregate = "matched"
        elif "blocked" in outcomes:
            aggregate = "blocked"
        elif "failed" in outcomes:
            aggregate = "failed"
        elif "unimplemented" in outcomes:
            aggregate = "unimplemented"
        elif outcomes and len(outcomes) == len(providers) and all(outcome == "no_match" for outcome in outcomes):
            aggregate = "no_match"
        else:
            aggregate = None
        checked_sources = [str(provider["name"]) for provider in providers if provider.get("status") == "checked"]
        evidence_urls = []
        for provider in providers:
            for url in provider.get("evidence_urls", []):
                if url not in evidence_urls:
                    evidence_urls.append(url)
        if aggregate in {"matched", "no_match"} and coverage == "complete":
            confidence = VerificationConfidence.HIGH.value
        elif aggregate == "matched":
            confidence = VerificationConfidence.MEDIUM.value
        else:
            confidence = VerificationConfidence.LOW.value
        reasons = [f"{provider['name']}: {provider['reason']}" for provider in providers if provider.get("reason")]
        if not providers:
            reasons.append("No recognized Russian-edition providers were configured")
        verification = {
            "check_coverage": coverage,
            "provider_outcome": aggregate,
            "confidence": confidence,
            "checked_sources": checked_sources,
            "checked_at": run_date,
            "query_timestamp": run_date,
            "evidence_urls": evidence_urls,
            "providers": providers,
            "reason": "; ".join(reasons),
        }
        validate_russian_edition_verification(verification)
        verification_by_key[_candidate_key(candidate)] = verification
    return verification_by_key


# ── Source specs (must come after all function definitions) ────────────────

_RSL_SPEC = _SourceSpec(
    name="RSL",
    source_type="russian-library-catalog",
    source_name_default="RSL",
    live_failure_patterns=RSL_LIVE_FAILURE_PATTERNS,
    is_live_blocked=True,
    search_response=rsl_live_search_response,
    parse_html_response=parse_rsl_html_response,
    parse_json_response=parse_rsl_json_response,
    validate_recorded_result=validate_rsl_recorded_result,
    normalize_recorded_result=normalize_rsl_result,
    fixture_query_fallback_to_title=False,
)

_LITRES_SPEC = _SourceSpec(
    name="LitRes",
    source_type="russian-retailer",
    source_name_default="LitRes",
    live_failure_patterns=LITRES_LIVE_FAILURE_PATTERNS,
    is_live_blocked=True,
    search_response=litres_live_search_response,
    parse_html_response=parse_litres_html_response,
    parse_json_response=parse_litres_json_response,
    validate_recorded_result=validate_litres_recorded_result,
    normalize_recorded_result=normalize_litres_result,
    fixture_query_fallback_to_title=True,
    _retailer_default_url="https://www.litres.ru",
    _retailer_html_card_pattern=(
        r'<(?:article|div)\b[^>]*class=["\'][^"\']*(?:art-item|book|card|result|product)[^"\']*["\'][^>]*>'
        r'(.*?)(?=<(?:article|div)\b[^>]*class=["\'][^"\']*(?:art-item|book|card|result|product)|\Z)'
    ),
    _retailer_html_link_pattern=r'<a\b[^>]*href=["\']([^"\']*/book/[^"\']+)["\'][^>]*>(.*?)</a>',
    _retailer_html_markers=("litres", "литрес", "search", "result", "найден"),
    _retailer_json_results_keys=("results", "items", "books"),
    _retailer_json_url_keys=("result_url", "url", "book_url"),
)

_BOOK24_SPEC = _SourceSpec(
    name="Book24",
    source_type="russian-retailer",
    source_name_default="Book24",
    live_failure_patterns=BOOK24_LIVE_FAILURE_PATTERNS,
    is_live_blocked=True,
    search_response=book24_live_search_response,
    parse_html_response=parse_book24_html_response,
    parse_json_response=parse_book24_json_response,
    validate_recorded_result=validate_book24_recorded_result,
    normalize_recorded_result=normalize_book24_result,
    fixture_query_fallback_to_title=True,
    _retailer_default_url="https://book24.ru",
    _retailer_html_card_pattern=(
        r'<(?:article|div)\b[^>]*class=["\'][^"\']*(?:product|book|card|result|catalog)[^"\']*["\'][^>]*>'
        r'(.*?)(?=<(?:article|div)\b[^>]*class=["\'][^"\']*(?:product|book|card|result|catalog)|\Z)'
    ),
    _retailer_html_link_pattern=r'<a\b[^>]*href=["\']([^"\']*/product/[^"\']+)["\'][^>]*>(.*?)</a>',
    _retailer_html_markers=("book24", "бук24", "search", "result", "найден"),
    _retailer_json_results_keys=("results", "items", "products"),
    _retailer_json_url_keys=("result_url", "url", "product_url"),
)
