"""Deterministic, offline, read-only editorial audit for Issue #667.

The audit separates taste, availability, decision blockers, and product
defects from retained editorial evidence. It never collects sources,
researches externally, runs the weekly workflow, files issues, migrates
state, merges identities, or changes ranking, sources, or profile state.

Derived output lives outside canonical weekly/editorial state and is
reproducible from the identified evidence baseline (Candidate History,
Editorial Taste Profile, and optional Source Metrics / suggestions /
identity aliases). Qualitative interpretations are labeled as
human-review-required hypotheses; bare refusals stay unspecified.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.identity import normalize_author_display
from autoscout.identity import normalize_author_identity
from autoscout.identity import normalize_identity_text
from autoscout.identity import normalize_work_title
from autoscout.repository_root import repository_root
from autoscout.safe_artifacts import SafeArtifactError
from autoscout.safe_artifacts import pair_write_lock
from autoscout.safe_artifacts import publish_pair
from autoscout.safe_artifacts import resolve_storage_destination
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import open_state_store
from autoscout.state_store.layout import candidate_history_location
from autoscout.state_store.layout import stored_history_digest

EDITORIAL_AUDIT_ARTIFACT_TYPE = "editorial-audit"
EDITORIAL_AUDIT_SCHEMA_VERSION = 2
EDITORIAL_AUDIT_JSON_FILENAME = "editorial-audit.json"
EDITORIAL_AUDIT_MD_FILENAME = "editorial-audit.md"
DEFAULT_EDITORIAL_AUDIT_STATE_DIR = Path("runs/current-weekly-pilot-russian")
DEFAULT_EDITORIAL_AUDIT_OUTPUT_DIR = Path("data/editorial-audit")
BARE_REFUSAL = "Нет."
DECIDED_STATUSES = (
    "interested",
    "needs_deeper_analysis",
    "maybe_watch",
    "already_known",
    "reject",
)
EVIDENCE_THRESHOLD_INTERESTED = 5

# Curated real cases named in Issue #667. Each entry pins the reviewed
# verbatim note and status observed during review. The audit output always
# quotes the retained baseline verbatim; these pinned copies are used only
# to validate that the retained evidence still supports the curated label.
# Any change (bare refusal, edited note, or changed status) flags the case
# as requiring review instead of keeping its curated label.
CURATED_CASES: tuple[dict[str, Any], ...] = (
    {
        "identity_key": "dopamine nation|anna lembke",
        "expected_behavior": "must_not_learn_as_negative_taste: positive-but-unavailable availability note is not a taste rejection",
        "ambiguity": "false",
        "reviewed_status": "already_known",
        "reviewed_note": "«Дофаминовая нация» [editor note withheld].",
    },
    {
        "identity_key": "say nothing|patrick radden keefe",
        "expected_behavior": "must_not_learn_as_negative_taste: positive-but-unavailable availability note is not a taste rejection",
        "ambiguity": "false",
        "reviewed_status": "already_known",
        "reviewed_note": "Очень бы хотелось издать. Никто нам его не продаёт.",
    },
    {
        "identity_key": "kindred|octavia e butler",
        "expected_behavior": "must_not_learn_as_acquisition_lead: technical complaint (missing description, wrong category) is not a positive acquisition signal; route to product follow-up",
        "ambiguity": "false",
        "reviewed_status": "needs_deeper_analysis",
        "reviewed_note": "Скаут снова не дал описания; [editor note withheld].",
    },
    {
        "identity_key": "the 5 types of wealth|sahil bloom",
        "expected_behavior": "must_not_learn_as_acquisition_lead: purchase-status check is not a taste signal; await editor verification",
        "ambiguity": "true",
        "reviewed_status": "needs_deeper_analysis",
        "reviewed_note": "Мне кажется, мы её купили. Я проверю.",
    },
    {
        "identity_key": "50 capsulas de amor propio|sara espejo",
        "expected_behavior": "must_not_learn_as_topic_ban: presentation-specific rejection does not establish a general topic preference; counterexample visible",
        "ambiguity": "false",
        "reviewed_status": "reject",
        "reviewed_note": "[editor note withheld].",
    },
)

# Heuristic keyword signals (casefolded substrings). These are triage hints
# only; every interpretation is labeled as requiring human review.
_RIGHTS_KEYWORDS = (
    "прав",
    "купил",
    "куплен",
    "прода",
    "изда",
    "издат",
    "перекупи",
    "rights",
    "sold",
)
_PRODUCT_KEYWORDS = (
    "описани",
    "ссылк",
    "категор",
    "non-fiction",
    "nonfiction",
    "fiction",
    "science fiction",
    "сбой",
    "саммари",
    "summary",
    "аудиокниг",
    "дубликат",
    "дубл",
    "ошибк",
    "bad link",
    "missing",
    "incorrect",
    "обложк",
    "перевод",
    "translation",
)
_INFO_NEEDED_KEYWORDS = (
    "проверю",
    "проверим",
    "запрошу",
    "запросить",
    "посмотр",
    "не знаю",
    "вряд ли",
    "может",
    "check",
    "verify",
    "need to",
)
_APPEAL_KEYWORDS = (
    "нравится",
    "интерес",
    "люблю",
    "прекрасн",
    "отличн",
    "стоит запросить",
    "хочется",
    "хотелось",
    "love",
    "excellent",
    "interest",
)
_COMMERCIAL_KEYWORDS = (
    "продается",
    "продаётся",
    "рынок",
    "аудитор",
    "читател",
    "коммерч",
    "market",
    "audience",
    "bestseller",
    "бестселлер",
)


class EditorialAuditError(ValueError):
    """An editorial-audit input or publication problem."""


@dataclass(frozen=True)
class EditorialAuditRequest:
    """Explicit paths for one offline audit invocation."""

    state_dir: Path
    output_dir: Path
    run_date: str | None = None


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _repo_relative(path: Path) -> str:
    try:
        repo_root = repository_root()
        return str(path.resolve(strict=False).relative_to(repo_root))
    except ValueError:
        return str(path)


def _read_json_file(path: Path, label: str, *, required: bool) -> Any:
    if path.is_symlink():
        raise EditorialAuditError(f"{label} must not be a symlink: {path}")
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        if required:
            raise EditorialAuditError(f"{label} does not exist: {path}") from None
        return None
    except OSError as exc:
        raise EditorialAuditError(f"cannot read {label}: {exc}") from exc
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EditorialAuditError(f"{label} is not valid UTF-8 JSON: {exc}") from exc


def _read_collection(store: StateStore, collection: Collection, path: Path, label: str) -> Any:
    """Read one canonical input through the State Store.

    *path* is the committed file the evidence baseline names; a symlink there
    is refused, as for every other input.
    """
    if path.is_symlink():
        raise EditorialAuditError(f"{label} must not be a symlink: {path}")
    try:
        return store.read(collection)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EditorialAuditError(f"{label} is not valid UTF-8 JSON: {exc}") from exc


def _validate_iso_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise EditorialAuditError(f"Invalid ISO date '{value}'. Expected format: YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise EditorialAuditError(f"Invalid ISO date '{value}'. Expected format: YYYY-MM-DD")
    return value


def _history_identity(record: Mapping[str, Any]) -> str:
    return str(record.get("identity_key", ""))


def _is_bare_refusal(note: Any) -> bool:
    return isinstance(note, str) and note.strip() == BARE_REFUSAL


def _is_substantive_note(note: Any) -> bool:
    return isinstance(note, str) and bool(note.strip()) and not _is_bare_refusal(note)


def _curated_entry(identity: str) -> dict[str, Any] | None:
    for curated in CURATED_CASES:
        if str(curated.get("identity_key", "")) == identity:
            return dict(curated)
    return None


def _check_curated_evidence(identity: str, note: Any, status: Any) -> tuple[bool, str]:
    """Validate a curated case against its pinned reviewed evidence.

    The curated label applies only when the retained status and verbatim
    note still match what was reviewed. Any change — bare refusal, edited
    note (including contradictory rewrites that reuse reviewed keywords),
    missing note, or changed status — requires review instead of keeping
    the curated label. Keyword overlap alone never validates.
    """
    entry = _curated_entry(identity)
    if entry is None:
        return True, "not_a_curated_case"
    if _is_bare_refusal(note):
        return False, "bare_refusal_retained_as_unspecified_no_inference"
    reviewed_status = str(entry.get("reviewed_status", ""))
    if str(status or "").strip() != reviewed_status:
        return False, "status_changed_requires_review"
    if not isinstance(note, str) or not note.strip():
        return False, "no_substantive_note_requires_review"
    reviewed_note = str(entry.get("reviewed_note", ""))
    if note.strip() != reviewed_note.strip():
        return False, "note_changed_requires_review"
    return True, "reviewed_evidence_matches"


def _heuristic_dimensions(note: str) -> tuple[list[str], str]:
    """Return (heuristic dimension hits, basis) for one verbatim note."""
    folded = note.casefold()
    hits: list[str] = []
    if any(keyword in folded for keyword in _RIGHTS_KEYWORDS):
        hits.append("rights_availability")
    if any(keyword in folded for keyword in _PRODUCT_KEYWORDS):
        hits.append("product_data_quality_complaint")
    if any(keyword in folded for keyword in _INFO_NEEDED_KEYWORDS):
        hits.append("information_needed")
    if any(keyword in folded for keyword in _APPEAL_KEYWORDS):
        hits.append("editorial_appeal")
    if any(keyword in folded for keyword in _COMMERCIAL_KEYWORDS):
        hits.append("commercial_publisher_fit")
    if not hits:
        return ["unspecified_ambiguous"], "no_heuristic_signal_retained_as_unspecified"
    return sorted(set(hits)), "heuristic_keyword_triage_requires_human_review"


def _work_key(record: Mapping[str, Any]) -> str:
    title = normalize_work_title(str(record.get("title") or ""))
    authors = record.get("primary_authors") or []
    if not isinstance(authors, list):
        authors = []
    normalized_authors = sorted(
        {normalize_author_identity(normalize_author_display(str(author))) for author in authors if str(author).strip()}
    )
    return f"{title}|{' & '.join(normalized_authors)}"


def _parse_alias_map(aliases: Mapping[str, Any] | None) -> dict[str, str]:
    """Validate stored identity aliases and return identity_key -> Candidate ID.

    An alias is valid only when both sides are nonblank strings. When the
    aliases file carries a ``candidates`` index, the target Candidate ID must
    be present there; otherwise the alias is stale and ignored for grouping.
    """
    alias_map: dict[str, str] = {}
    if not isinstance(aliases, Mapping):
        return alias_map
    raw_aliases = aliases.get("aliases")
    if not isinstance(raw_aliases, Mapping):
        return alias_map
    candidates = aliases.get("candidates")
    candidate_ids: set[str] | None = None
    if isinstance(candidates, Mapping) and len(candidates) > 0:
        candidate_ids = {str(key) for key in candidates}
    for identity, candidate_id in raw_aliases.items():
        if not isinstance(identity, str) or not identity.strip():
            continue
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            continue
        target = candidate_id.strip()
        if candidate_ids is not None and target not in candidate_ids:
            continue
        alias_map[identity] = target
    return alias_map


def _alias_aware_work_key(record: Mapping[str, Any], alias_map: Mapping[str, str]) -> str:
    """Group works by validated durable Candidate ID where available.

    Two translated identities mapped to the same Candidate ID are one work,
    even when their normalized titles differ. Records without a valid alias
    fall back to the normalized work title plus authors.
    """
    identity = _history_identity(record)
    target = alias_map.get(identity, "")
    if isinstance(target, str) and target.strip():
        return f"candidate:{target.strip()}"
    return f"work:{_work_key(record)}"


def _author_display(record: Mapping[str, Any]) -> str:
    authors = record.get("primary_authors") or []
    if not isinstance(authors, list):
        return ""
    return ", ".join(str(author) for author in authors)


def _source_names(record: Mapping[str, Any]) -> list[str]:
    evidence = record.get("source_evidence")
    if not isinstance(evidence, list):
        return []
    names = sorted(
        {
            str(item.get("source_name", "")).strip()
            for item in evidence
            if isinstance(item, Mapping) and str(item.get("source_name", "")).strip()
        }
    )
    return names


def _validate_history(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise EditorialAuditError("Candidate History must be a JSON list")
    records: list[dict[str, Any]] = []
    for index, record in enumerate(value):
        if not isinstance(record, Mapping):
            raise EditorialAuditError(f"Candidate History entry {index} must be a JSON object")
        records.append(dict(record))
    return records


def _validate_profile(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EditorialAuditError("Editorial Taste Profile must be a JSON object")
    entries = value.get("entries")
    if not isinstance(entries, list):
        raise EditorialAuditError("Editorial Taste Profile entries must be a list")
    return {"entries": [dict(entry) for entry in entries if isinstance(entry, Mapping)]}


def _active_profile_entries(profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries = profile.get("entries")
    if not isinstance(entries, list):
        return []
    return [dict(entry) for entry in entries if isinstance(entry, Mapping) and entry.get("status") == "active"]


def _cohort_counts(
    history: list[dict[str, Any]],
    aliases: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    alias_map = _parse_alias_map(aliases)
    per_status: dict[str, dict[str, int]] = {}
    for status in (*DECIDED_STATUSES, "unreviewed"):
        per_status[status] = {
            "records": 0,
            "distinct_works": 0,
            "substantive_notes": 0,
            "bare_refusals": 0,
        }
    works_by_status: dict[str, set[str]] = {status: set() for status in per_status}
    bare_total = 0
    substantive_total = 0
    decided_total = 0
    for record in history:
        status = str(record.get("editor_status") or "").strip()
        bucket = status if status in DECIDED_STATUSES else "unreviewed"
        per_status[bucket]["records"] += 1
        works_by_status[bucket].add(_alias_aware_work_key(record, alias_map))
        note = record.get("editor_note")
        if _is_bare_refusal(note):
            per_status[bucket]["bare_refusals"] += 1
            bare_total += 1
        elif _is_substantive_note(note):
            per_status[bucket]["substantive_notes"] += 1
            substantive_total += 1
        if bucket != "unreviewed":
            decided_total += 1
    for status in per_status:
        per_status[status]["distinct_works"] = len(works_by_status[status])
    return {
        "records_total": len(history),
        "decided_total": decided_total,
        "unreviewed_total": per_status["unreviewed"]["records"],
        "bare_refusal_total": bare_total,
        "substantive_note_total": substantive_total,
        "per_status": per_status,
    }


def _work_identity_analysis(
    history: list[dict[str, Any]],
    aliases: Mapping[str, Any] | None,
) -> dict[str, Any]:
    decided = [record for record in history if str(record.get("editor_status") or "").strip() in DECIDED_STATUSES]
    alias_map = _parse_alias_map(aliases)
    work_to_identities: dict[str, set[str]] = {}
    for record in history:
        work_to_identities.setdefault(_alias_aware_work_key(record, alias_map), set()).add(_history_identity(record))
    decided_work_keys = {_alias_aware_work_key(record, alias_map) for record in decided}
    resolved_decided = sum(1 for record in decided if _history_identity(record) in alias_map)
    # Unresolved multilingual/work duplicates: same normalized title, different
    # author sets, or one candidate_id covering several identity keys.
    candidate_to_identities: dict[str, set[str]] = {}
    for identity, candidate_id in alias_map.items():
        candidate_to_identities.setdefault(candidate_id, set()).add(identity)
    merged_identities = sorted(
        {" | ".join(sorted(identities)) for identities in candidate_to_identities.values() if len(identities) > 1}
    )[:5]
    title_to_author_sets: dict[str, set[str]] = {}
    for record in decided:
        title = normalize_work_title(str(record.get("title") or ""))
        if not title:
            continue
        authors = record.get("primary_authors") or []
        author_key = " & ".join(
            sorted(
                {
                    normalize_author_identity(normalize_author_display(str(author)))
                    for author in authors
                    if isinstance(author, str) and author.strip()
                }
            )
        )
        title_to_author_sets.setdefault(title, set()).add(author_key)
    flagged = sorted(
        f"shared title {title!r} with {len(author_sets)} author sets; flagged for human review, never merged"
        for title, author_sets in title_to_author_sets.items()
        if len(author_sets) > 1
    )[:5]
    return {
        "record_count": len(history),
        "decided_record_count": len(decided),
        "distinct_work_count": len(work_to_identities),
        "distinct_decided_work_count": len(decided_work_keys),
        "alias_resolved_decided_count": resolved_decided,
        "method": (
            "Distinct works group records by validated durable Candidate ID where an alias exists, "
            "falling back to normalized work title plus normalized primary authors. Translated identities "
            "mapped to one Candidate ID are one work. Repeated runs of one "
            "identity are one editorial decision, never independent positive examples."
        ),
        "limitation": (
            "Current history carries current mutable metadata; decision-time evidence cannot be reconstructed "
            "from this file alone. Identity and counts use current metadata only for grouping, never as proof "
            "of what the editor saw when deciding."
        ),
        "merged_identity_examples": merged_identities,
        "unresolved_duplicate_flags": flagged,
        "never_merged": True,
    }


def _record_view(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "identity_key": _history_identity(record),
        "title": str(record.get("title") or ""),
        "author_display": _author_display(record),
        "editor_status": str(record.get("editor_status") or ""),
        "editor_note": record.get("editor_note"),
        "editor_decision_date": record.get("editor_decision_date"),
        "first_seen_date": record.get("first_seen_date"),
        "last_seen_date": record.get("last_seen_date"),
        "source_names": _source_names(record),
    }


def _heuristic_sample(
    history: list[dict[str, Any]],
    *,
    per_cohort: int = 3,
) -> list[dict[str, Any]]:
    by_status: dict[str, list[dict[str, Any]]] = {status: [] for status in DECIDED_STATUSES}
    for record in history:
        status = str(record.get("editor_status") or "")
        if status in by_status and _is_substantive_note(record.get("editor_note")):
            by_status[status].append(record)
    sample: list[dict[str, Any]] = []
    for status in DECIDED_STATUSES:
        ordered = sorted(by_status[status], key=lambda item: _history_identity(item))[:per_cohort]
        for record in ordered:
            note = str(record.get("editor_note") or "")
            dimensions, basis = _heuristic_dimensions(note)
            sample.append(
                {
                    **_record_view(record),
                    "heuristic_dimensions": dimensions,
                    "heuristic_basis": basis,
                    "interpretation_kind": "heuristic_triage_requires_human_review",
                    "verbatim_preserved": True,
                }
            )
    return sample


_COHORT_AUDIT_PRIORITY = {
    "interested": 0,
    "needs_deeper_analysis": 1,
    "maybe_watch": 2,
    "already_known": 3,
    "reject": 4,
}


def _full_interpretations(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Audit the full decided population before sampling presentation examples.

    Order follows Issue #667: all Interested, Needs Deeper Analysis, and
    Maybe / Watch records, then Already Known, then substantive Reject notes;
    bare refusals sort last within Reject with no inference.
    """
    decided = [record for record in history if str(record.get("editor_status") or "").strip() in DECIDED_STATUSES]

    def _sort_key(record: dict[str, Any]) -> tuple[int, int, str]:
        status = str(record.get("editor_status") or "").strip()
        priority = _COHORT_AUDIT_PRIORITY.get(status, 99)
        note = record.get("editor_note")
        substantive_first = 0 if _is_substantive_note(note) else 1
        return (priority, substantive_first, _history_identity(record))

    interpretations: list[dict[str, Any]] = []
    for record in sorted(decided, key=_sort_key):
        note = record.get("editor_note")
        if _is_bare_refusal(note):
            dimensions, basis = ["unspecified_ambiguous"], "bare_refusal_retained_as_unspecified"
        elif _is_substantive_note(note):
            dimensions, basis = _heuristic_dimensions(str(note))
        else:
            dimensions, basis = ["unspecified_ambiguous"], "no_note_retained_as_unspecified"
        interpretations.append(
            {
                **_record_view(record),
                "heuristic_dimensions": dimensions,
                "heuristic_basis": basis,
                "interpretation_kind": "heuristic_triage_requires_human_review",
                "verbatim_preserved": True,
            }
        )
    return interpretations


def _profile_relationship(keywords: list[str], active_entries: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_keywords = {normalize_identity_text(keyword) for keyword in keywords if keyword.strip()}
    related: list[dict[str, str]] = []
    for entry in active_entries:
        subject = str(entry.get("subject", ""))
        terms = entry.get("matching_terms")
        haystack = normalize_identity_text(subject)
        if isinstance(terms, list):
            haystack += " " + " ".join(normalize_identity_text(str(term)) for term in terms if isinstance(term, str))
        tokens = set(haystack.split())
        if normalized_keywords & tokens or any(
            keyword and keyword in normalize_identity_text(subject) for keyword in normalized_keywords
        ):
            related.append(
                {
                    "id": str(entry.get("id", "")),
                    "kind": str(entry.get("kind", "")),
                    "scope": str(entry.get("scope", "")),
                    "subject": subject,
                }
            )
    return {
        "related_active_entries": sorted(related, key=lambda item: item["id"]),
        "new_rule_proposed": False,
    }


def _decision_guide(
    history_by_identity: dict[str, dict[str, Any]],
    active_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    def view(identity: str) -> dict[str, Any] | None:
        record = history_by_identity.get(identity)
        if record is None:
            return None
        note = record.get("editor_note")
        dimensions, basis = (
            (["unspecified_ambiguous"], "bare_refusal_retained_as_unspecified")
            if _is_bare_refusal(note)
            else _heuristic_dimensions(str(note or ""))
            if _is_substantive_note(note)
            else (["unspecified_ambiguous"], "no_note_retained_as_unspecified")
        )
        evidence_ok, evidence_check = _check_curated_evidence(identity, note, record.get("editor_status"))
        return {
            **_record_view(record),
            "heuristic_dimensions": dimensions,
            "heuristic_basis": basis,
            "verbatim_preserved": True,
            "curated_evidence_ok": evidence_ok,
            "curated_evidence_check": evidence_check,
        }

    distinctions: list[dict[str, Any]] = [
        {
            "id": "positive_but_unavailable_is_not_taste_rejection",
            "claim": "Already Known with an availability note is not negative taste and must not train rejection.",
            "supporting_identities": ["dopamine nation|anna lembke", "say nothing|patrick radden keefe"],
            "opposing_or_qualifying_identities": ["50 capsulas de amor propio|sara espejo"],
            "uncertainty": "Editor-reported availability is not independently verified publication or rights fact.",
            "keywords": ["права", "прода", "издать", "already known"],
        },
        {
            "id": "technical_complaint_is_not_acquisition_lead",
            "claim": "Needs Deeper Analysis with a product complaint is a defect report, not an attractive lead.",
            "supporting_identities": ["kindred|octavia e butler"],
            "opposing_or_qualifying_identities": ["the 5 types of wealth|sahil bloom"],
            "uncertainty": "Decision-time description/category evidence cannot be reconstructed; current metadata may differ.",
            "keywords": ["описани", "категор", "fiction", "сбой"],
        },
        {
            "id": "purchase_status_check_is_not_taste_signal",
            "claim": "A purchase-status note is an information need, not a positive or negative taste verdict.",
            "supporting_identities": ["the 5 types of wealth|sahil bloom"],
            "opposing_or_qualifying_identities": ["kindred|octavia e butler"],
            "uncertainty": "Ambiguous until the editor verifies the purchase; retain as ambiguous, do not invent a label.",
            "keywords": ["купили", "проверю"],
        },
        {
            "id": "presentation_rejection_is_not_topic_ban",
            "claim": "A presentation-specific Reject does not establish a general topic preference.",
            "supporting_identities": ["50 capsulas de amor propio|sara espejo"],
            "opposing_or_qualifying_identities": ["dopamine nation|anna lembke"],
            "uncertainty": "What makes one treatment acceptable when another is not needs human judgment with counterexamples.",
            "keywords": ["приторн", "нравится", "reject"],
        },
        {
            "id": "bare_refusal_stays_unspecified",
            "claim": "Bare refusals carry no reason; no automatic inference is made from them.",
            "supporting_identities": [],
            "opposing_or_qualifying_identities": [],
            "uncertainty": "None on method: every exact «Нет.» stays unspecified_ambiguous by construction.",
            "keywords": [],
        },
    ]
    guide: list[dict[str, Any]] = []
    for distinction in distinctions:
        raw_supporting = [view(identity) for identity in distinction["supporting_identities"]]
        supporting: list[dict[str, Any]] = [item for item in raw_supporting if item is not None]
        raw_opposing = [view(identity) for identity in distinction["opposing_or_qualifying_identities"]]
        opposing: list[dict[str, Any]] = [item for item in raw_opposing if item is not None]
        relationship = _profile_relationship(distinction["keywords"], active_entries)
        evidence_changed = any(item.get("curated_evidence_ok") is False for item in (*supporting, *opposing))
        guide.append(
            {
                "id": distinction["id"],
                "claim": distinction["claim"],
                "interpretation_kind": "human_review_required_hypothesis",
                "supporting_cases": supporting,
                "opposing_or_qualifying_cases": opposing,
                "uncertainty": distinction["uncertainty"],
                "existing_profile_relationship": relationship,
                "evidence_changed_requires_review": evidence_changed,
                "established_vs_hypothesis": (
                    "established_recorded_preference_only_when_an_active_profile_entry_is_listed; "
                    "otherwise a new hypothesis requiring explicit preview-and-confirm"
                ),
            }
        )
    return guide


def _repo_root() -> Path:
    return repository_root()


def _implementation_evidence(
    code_refs: list[str],
    code_symbols: Mapping[str, list[str]],
    run_date: str,
) -> tuple[list[dict[str, Any]], str]:
    """Retain dated implementation evidence for one defect class.

    Each referenced path contributes existence, kind, file digest, and the
    expected domain symbols found in its current content. Symbol presence
    shows the relevant machinery exists; it never proves the historical
    complaint is repaired or still reproducible, so callers keep currency
    unverified pending focused reproduction.
    """
    root = _repo_root()
    evidence: list[dict[str, Any]] = []
    for ref in code_refs:
        candidate = root / ref
        try:
            exists = candidate.exists()
        except OSError:
            exists = False
        if exists:
            try:
                if candidate.is_dir():
                    kind = "directory"
                elif candidate.is_file():
                    kind = "file"
                else:
                    kind = "other"
            except OSError:
                kind = "unknown"
        else:
            kind = "missing"
        digest: str | None = None
        symbols_found: list[str] = []
        if kind == "file":
            try:
                digest = _sha256_bytes(candidate.read_bytes())
            except OSError:
                digest = None
            expected = code_symbols.get(ref, [])
            if expected:
                try:
                    content = candidate.read_text(encoding="utf-8", errors="replace").casefold()
                except OSError:
                    content = ""
                symbols_found = sorted({symbol for symbol in expected if symbol.casefold() in content})
        evidence.append(
            {
                "ref": ref,
                "exists": exists,
                "kind": kind,
                "sha256": digest,
                "expected_symbols": sorted(code_symbols.get(ref, [])),
                "symbols_found": symbols_found,
            }
        )
    return evidence, run_date


def _repair_path_verified(implementation_refs: list[dict[str, Any]]) -> bool:
    """Confirm the identity/merge repair path from successful checks only.

    Every referenced path must exist, and every expected symbol must be
    found in its current content. Refs without expected symbols (such as
    directories) pass on existence alone. Anything less leaves the repair
    claim unverified.
    """
    if not implementation_refs:
        return False
    for ref in implementation_refs:
        if ref.get("exists") is not True:
            return False
        expected = ref.get("expected_symbols", [])
        found = ref.get("symbols_found", [])
        if isinstance(expected, list) and expected and sorted(found) != sorted(expected):
            return False
    return True


def _issue_evidence(issue_refs: list[str], run_date: str) -> dict[str, Any]:
    """Retain dated issue-status evidence (offline: explicitly unverified)."""
    return {
        "checked_on": run_date,
        "refs": list(issue_refs),
        "status": "unverified_offline_requires_separate_check",
        "basis": (
            "The audit makes no network calls, so GitHub issue state was not checked; "
            "declared references are pointers for a separate follow-up, not checked results."
        ),
    }


def _backlog(history: list[dict[str, Any]], run_date: str) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    examples: dict[str, dict[str, Any]] = {}
    for record in history:
        note = str(record.get("editor_note") or "")
        if not _is_substantive_note(record.get("editor_note")):
            continue
        folded = note.casefold()
        classes: list[str] = []
        if any(keyword in folded for keyword in ("описани", "missing", "description")):
            classes.append("missing_description")
        if "fiction" in folded or "категор" in folded:
            classes.append("fiction_leakage_or_category_error")
        if "ссылк" in folded or "bad link" in folded:
            classes.append("bad_links")
        if "перевод" in folded or "translation" in folded or "english" in folded:
            classes.append("untranslated_or_unusable_title")
        if "дубликат" in folded or "дубл" in folded or "duplicate" in folded:
            classes.append("duplicate_identity")
        if "russian" in folded or "русск" in folded or "издан" in folded:
            classes.append("russian_edition_uncertainty")
        dimensions, _basis = _heuristic_dimensions(note)
        if "information_needed" in dimensions:
            classes.append("recurring_information_need")
        for defect in classes:
            counts[defect] = counts.get(defect, 0) + 1
            if defect not in examples:
                examples[defect] = _record_view(record)
    registry = [
        {
            "id": "missing_description",
            "label": "Missing or inadequate description",
            "currency": "unknown_requires_focused_followup",
            "code_refs": [
                "autoscout/content_description.py",
                "autoscout/grounded_description.py",
                "autoscout/description_adjudication.py",
                "autoscout/description_depth.py",
            ],
            "code_symbols": {
                "autoscout/content_description.py": ["content_description"],
                "autoscout/grounded_description.py": ["grounded"],
                "autoscout/description_adjudication.py": ["adjudicat"],
                "autoscout/description_depth.py": ["depth"],
            },
            "issue_refs": ["#671", "#672", "#673", "#674", "#675"],
            "followup": "Reproduce the Kindred-class complaint on a current run before calling it a current defect.",
        },
        {
            "id": "fiction_leakage_or_category_error",
            "label": "Fiction leakage or wrong nonfiction classification",
            "currency": "unknown_requires_focused_followup",
            "code_refs": ["autoscout/scouting_candidates.py", "autoscout/candidate_schema.py"],
            "code_symbols": {
                "autoscout/scouting_candidates.py": ["INELIGIBLE", "fiction"],
                "autoscout/candidate_schema.py": ["category"],
            },
            "issue_refs": [],
            "followup": "Check the current category gate against the Kindred record; file a focused technical issue only after reproduction.",
        },
        {
            "id": "bad_links",
            "label": "Bad or unusable source links",
            "currency": "unknown_requires_focused_followup",
            "code_refs": ["autoscout/http_evidence.py", "autoscout/source_collectors/"],
            "code_symbols": {"autoscout/http_evidence.py": ["http", "evidence"]},
            "issue_refs": [],
            "followup": "Re-check the cited links live in a separate step; the audit makes no network calls.",
        },
        {
            "id": "untranslated_or_unusable_title",
            "label": "Untranslated or unusable title",
            "currency": "unknown_requires_focused_followup",
            "code_refs": ["autoscout/english_title_backfill.py"],
            "code_symbols": {"autoscout/english_title_backfill.py": ["english_title", "backfill"]},
            "issue_refs": [],
            "followup": "Verify helper-metadata provenance for the cited record before proposing a title fix.",
        },
        {
            "id": "duplicate_identity",
            "label": "Duplicate or fragmented work identity",
            "currency": "repair_path_exists_but_unverified_for_these_records",
            "code_refs": [
                "autoscout/identity.py",
                "autoscout/identity_corrections.py",
                "autoscout/candidate_identity.py",
            ],
            "code_symbols": {
                "autoscout/identity.py": ["mentions_are_identity_match", "normalized_identity_key"],
                "autoscout/identity_corrections.py": ["merge", "correction"],
                "autoscout/candidate_identity.py": ["identity"],
            },
            "issue_refs": ["#456", "#559"],
            "followup": "Use the guarded merge-candidate-identity dry-run path only after explicit confirmation; the audit never merges.",
        },
        {
            "id": "russian_edition_uncertainty",
            "label": "Russian-edition or rights-availability question",
            "currency": "unverified_requires_focused_followup",
            "code_refs": ["autoscout/russian_editions.py"],
            "code_symbols": {"autoscout/russian_editions.py": ["russian", "edition"]},
            "issue_refs": ["#616"],
            "followup": "Check #616 for current Russian-edition verification coverage, then reproduce the cited record before calling it a current defect.",
        },
        {
            "id": "recurring_information_need",
            "label": "Recurring decision blocker (method, scope, genre/treatment, author relevance, usable translation)",
            "currency": "unknown_requires_focused_followup",
            "code_refs": [],
            "code_symbols": {},
            "issue_refs": [],
            "followup": "Collect the blocker question verbatim in future reviews instead of inferring it.",
        },
    ]
    items: list[dict[str, Any]] = []
    for entry in registry:
        defect_id = str(entry["id"])
        evidence_count = counts.get(defect_id, 0)
        example = examples.get(defect_id)
        code_refs = [str(ref) for ref in entry.get("code_refs", []) if str(ref).strip()]
        raw_symbols = entry.get("code_symbols", {})
        code_symbols: dict[str, list[str]] = {}
        if isinstance(raw_symbols, Mapping):
            for ref, symbols in raw_symbols.items():
                if isinstance(symbols, list):
                    code_symbols[str(ref)] = [str(symbol) for symbol in symbols if str(symbol).strip()]
        implementation_refs, _checked_on = _implementation_evidence(code_refs, code_symbols, run_date)
        issue_refs = [str(ref) for ref in entry.get("issue_refs", []) if str(ref).strip()]
        issues = _issue_evidence(issue_refs, run_date)
        checked = bool(code_refs)
        symbols_hit = [ref for ref in implementation_refs if ref.get("symbols_found")]
        if defect_id == "duplicate_identity":
            if _repair_path_verified(implementation_refs):
                effective_currency = "repair_path_exists_but_unverified_for_these_records"
                currency_basis = (
                    f"Implementation evidence on {run_date} shows identity/merge machinery present "
                    f"({', '.join(sorted({symbol for ref in symbols_hit for symbol in ref['symbols_found']}))}); "
                    "whether these records are repaired is unverified without reproduction."
                )
            else:
                effective_currency = "unverified_requires_focused_followup"
                currency_basis = (
                    f"Implementation checks on {run_date} did not confirm the identity/merge repair path "
                    "(missing files or expected symbols); status unverified and repair availability not asserted."
                )
        elif checked:
            effective_currency = str(entry.get("currency", ""))
            currency_basis = (
                f"Implementation paths checked on {run_date}; symbol presence shows relevant machinery exists "
                "but defect reproduction was not performed, so currency remains "
                f"{effective_currency}."
            )
        else:
            effective_currency = str(entry.get("currency", ""))
            currency_basis = (
                f"No code path to check on {run_date}; unverified report, currency remains {effective_currency}."
            )
        if checked:
            verification_basis = (
                f"Code-path existence and symbols checked on {run_date}; "
                f"defect reproduction not performed, so currency remains {effective_currency}."
            )
        else:
            verification_basis = (
                f"No code path to check on {run_date}; unverified report, currency remains {effective_currency}."
            )
        items.append(
            {
                **entry,
                "currency": effective_currency,
                "evidence_count_in_substantive_notes": evidence_count,
                "example_case": example,
                "heuristic_basis": "heuristic_keyword_triage_requires_human_review",
                "verification_date": run_date,
                "code_refs_checked": checked,
                "code_refs_evidence": implementation_refs,
                "implementation_evidence": {
                    "checked_on": run_date,
                    "refs": implementation_refs,
                },
                "issue_evidence": issues,
                "currency_basis": currency_basis,
                "verification_basis": verification_basis,
            }
        )
    return items


def _evaluation_set(history_by_identity: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for curated in CURATED_CASES:
        identity = str(curated.get("identity_key", ""))
        record = history_by_identity.get(identity)
        if record is None:
            cases.append(
                {
                    "identity_key": identity,
                    "status": "baseline_absent",
                    "expected_behavior": str(curated.get("expected_behavior", "")),
                    "ambiguous": str(curated.get("ambiguity", "")) == "true",
                }
            )
            continue
        evidence_ok, evidence_check = _check_curated_evidence(
            identity, record.get("editor_note"), record.get("editor_status")
        )
        if evidence_ok:
            cases.append(
                {
                    **_record_view(record),
                    "expected_behavior": str(curated.get("expected_behavior", "")),
                    "ambiguous": str(curated.get("ambiguity", "")) == "true",
                    "verbatim_preserved": True,
                    "evidence_changed_requires_review": False,
                    "curated_evidence_check": evidence_check,
                    "gold_label": None,
                    "gold_label_note": (
                        "Ambiguous cases stay ambiguous; an anecdote becomes a product-rule test "
                        "only after the intended behavior is established."
                    )
                    if str(curated.get("ambiguity", "")) == "true"
                    else "Reviewed expected handling for future changes.",
                }
            )
            continue
        cases.append(
            {
                **_record_view(record),
                "expected_behavior": "requires_review_curated_evidence_changed_no_inference",
                "curated_basis": str(curated.get("expected_behavior", "")),
                "curated_basis_stale": True,
                "ambiguous": True,
                "verbatim_preserved": True,
                "evidence_changed_requires_review": True,
                "curated_evidence_check": evidence_check,
                "gold_label": None,
                "gold_label_note": (
                    "Retained evidence no longer supports the curated label; "
                    "bare refusals stay unspecified and mismatches stay ambiguous."
                ),
            }
        )
    cases.append(
        {
            "identity_key": "__bare_refusal_class__",
            "title": "",
            "author_display": "",
            "editor_status": "reject",
            "editor_note": BARE_REFUSAL,
            "expected_behavior": "must_remain_unspecified_no_inference_from_bare_refusal",
            "ambiguous": True,
            "verbatim_preserved": True,
            "gold_label": None,
            "gold_label_note": "Bare refusals never become invented gold labels.",
        }
    )
    return sorted(cases, key=lambda item: str(item.get("identity_key", "")))


def _source_analysis(
    source_metrics: Mapping[str, Any] | None,
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    history_names: dict[str, int] = {}
    for record in history:
        for name in _source_names(record):
            history_names[name] = history_names.get(name, 0) + 1
    if source_metrics is None:
        return {
            "status": "insufficient_metadata",
            "per_source": [],
            "guidance": (
                "Source Metrics were absent, so no source-worth conclusion is drawn. No zero allocation is proposed."
            ),
            "history_source_name_count": len(history_names),
        }
    metrics = source_metrics.get("metrics")
    if not isinstance(metrics, list):
        raise EditorialAuditError("Source Metrics metrics must be a list")
    per_source: list[dict[str, Any]] = []
    for entry in metrics:
        if not isinstance(entry, Mapping):
            continue
        reviewed = entry.get("reviewed_candidate_count", 0)
        reviewed_count = reviewed if isinstance(reviewed, int) and not isinstance(reviewed, bool) else 0
        per_source.append(
            {
                "source_name": str(entry.get("source_name", "")),
                "source_url": str(entry.get("source_url", "")),
                "family": str(entry.get("family", entry.get("signal_family", ""))),
                "active": bool(entry.get("active", True)),
                "lifecycle_status": str(entry.get("lifecycle_status", "")),
                "candidate_count": entry.get("candidate_count", 0),
                "reviewed_candidate_count": reviewed_count,
                "interested_count": entry.get("interested_count", 0),
                "needs_deeper_analysis_count": entry.get("needs_deeper_analysis_count", 0),
                "maybe_watch_count": entry.get("maybe_watch_count", 0),
                "already_known_count": entry.get("already_known_count", 0),
                "reject_count": entry.get("reject_count", 0),
                "unique_contribution_rate": entry.get("unique_contribution_rate"),
                "corroboration_rate": entry.get("corroboration_rate"),
                "cross_family_corroboration_rate": entry.get("cross_family_corroboration_rate"),
                "confidence": ("insufficient_data_small_sample" if reviewed_count < 10 else "observational"),
            }
        )
    per_source.sort(key=lambda item: (str(item["source_name"]).casefold(), str(item["source_url"])))
    return {
        "status": "observational",
        "per_source": per_source,
        "per_source_count": len(per_source),
        "history_source_name_count": len(history_names),
        "guidance": (
            "Watch, follow-up, and interest are never collapsed into acquisition success. "
            "Source worth is never inferred from rejection rate alone. No zero allocation is proposed "
            "without considering corroboration, exploration, probation, and small samples."
        ),
        "limits": (
            "Denominators come from retained Source Metrics; overlap and reviewed/unreviewed populations "
            "are as stored. Small samples and probation sources stay insufficient_data."
        ),
    }


def _recommendation(
    cohort_counts: Mapping[str, Any],
    work_identity: Mapping[str, Any],
    source_analysis: Mapping[str, Any],
) -> dict[str, Any]:
    per_status = cohort_counts.get("per_status", {})
    interested = per_status.get("interested", {}).get("records", 0) if isinstance(per_status, Mapping) else 0
    insufficient_interested = not isinstance(interested, int) or interested < 30
    return {
        "priority_next_steps": [
            "Keep positive-but-unavailable, technical-complaint, and genuine-rejection evidence on separate learning tracks.",
            "Resolve the missing-description / category-complaint class with a focused reproduction before any ranking change.",
            "Collect decision blockers verbatim in future reviews instead of inferring reasons from bare refusals.",
        ],
        "what_can_be_learned": (
            "Conditional distinctions with supporting and opposing verbatim cases, and recurring information "
            "needs that future reviews should answer directly."
        ),
        "what_cannot_be_learned": (
            "Sales or acquisition prediction from a small Interested cohort or from repeated runs of the same works; "
            "general topic bans from rejection percentages; verified rights facts from editor-reported availability."
        ),
        "insufficient_evidence_findings": {
            "interested_records": interested,
            "insufficient_for_acquisition_prediction": True if insufficient_interested else False,
            "note": (
                "Conclusions do not claim sales/acquisition prediction from the Interested cohort "
                "or from the number of repeated runs."
            ),
        },
        "future_ranking_evaluation_proposal": (
            "Evaluate future ranking changes on subsequent new books with ranking frozen before feedback, "
            "work identities kept together across splits, and, where possible, measured actual review time "
            "and later workflow outcomes rather than only rank estimates."
        ),
        "taste_learning_protocol": "Any subsequent taste learning uses the existing preview-and-confirm protocol.",
        "distinct_decided_works": work_identity.get("distinct_decided_work_count"),
        "source_table_size": source_analysis.get("per_source_count", 0),
    }


def build_editorial_audit(
    history: list[dict[str, Any]],
    profile: dict[str, Any],
    source_metrics: Mapping[str, Any] | None = None,
    suggestions: Mapping[str, Any] | None = None,
    aliases: Mapping[str, Any] | None = None,
    *,
    run_date: str,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the deterministic audit report from in-memory inputs."""
    _validate_iso_date(run_date)
    validated_history = _validate_history(history)
    validated_profile = _validate_profile(profile)
    active_entries = _active_profile_entries(validated_profile)
    history_by_identity = {
        _history_identity(record): record for record in validated_history if _history_identity(record)
    }
    cohort_counts = _cohort_counts(validated_history, aliases if isinstance(aliases, Mapping) else None)
    work_identity = _work_identity_analysis(validated_history, aliases if isinstance(aliases, Mapping) else None)
    source_analysis = _source_analysis(
        source_metrics if source_metrics is None or isinstance(source_metrics, Mapping) else None,
        validated_history,
    )
    decision_guide = _decision_guide(history_by_identity, active_entries)
    backlog = _backlog(validated_history, run_date)
    evaluation_set = _evaluation_set(history_by_identity)
    interpretations = _full_interpretations(validated_history)
    heuristic_sample = _heuristic_sample(validated_history)
    baseline_section = dict(baseline or {})
    baseline_section.setdefault("run_date", run_date)
    baseline_section.setdefault("history_records", len(validated_history))
    baseline_section.setdefault("history_decided_records", cohort_counts["decided_total"])
    baseline_section.setdefault("profile_entries", len(validated_profile.get("entries", [])))
    baseline_section.setdefault("profile_active_entries", len(active_entries))
    baseline_section.setdefault(
        "inputs_resolved_from",
        "Candidate History and retained run evidence, not remembered titles",
    )
    baseline_section.setdefault(
        "decision_time_limitation",
        "Decision-time evidence cannot be reconstructed from current history alone; current metadata is used only for identity grouping.",
    )
    baseline_section.setdefault(
        "reproducibility",
        "Derived output is reproducible from the identified baseline digests; reruns with the same inputs and run date are byte-identical apart from key-sorted JSON formatting.",
    )
    suggestion_count = 0
    if isinstance(suggestions, Mapping) and isinstance(suggestions.get("suggestions"), list):
        suggestion_count = len(suggestions["suggestions"])
    return {
        "artifact_type": EDITORIAL_AUDIT_ARTIFACT_TYPE,
        "schema_version": EDITORIAL_AUDIT_SCHEMA_VERSION,
        "run_date": run_date,
        "canonical_effect": "none",
        "evidence_baseline": baseline_section,
        "cohort_counts": cohort_counts,
        "work_identity": work_identity,
        "interpretations": interpretations,
        "interpretations_note": (
            "Full decided population audited in cohort priority order before sampling; "
            f"{len(interpretations)} decided records carry heuristic annotations requiring human review. "
            "Bare refusals (exact «Нет.») are always unspecified_ambiguous with no inference."
        ),
        "heuristic_triage_sample": heuristic_sample,
        "heuristic_triage_note": (
            "Heuristic dimensions are audit annotations only, requiring human review. "
            "Bare refusals (exact «Нет.») are always unspecified_ambiguous with no inference."
        ),
        "decision_guide": decision_guide,
        "quality_and_information_gap_backlog": backlog,
        "evaluation_set": evaluation_set,
        "source_analysis": source_analysis,
        "profile_check": {
            "active_entry_count": len(active_entries),
            "active_entries": [
                {
                    "id": str(entry.get("id", "")),
                    "kind": str(entry.get("kind", "")),
                    "scope": str(entry.get("scope", "")),
                    "subject": str(entry.get("subject", "")),
                    "verbatim_quote": str(entry.get("verbatim_quote", "")),
                }
                for entry in sorted(active_entries, key=lambda item: str(item.get("id", "")))
            ],
            "derived_suggestion_count": suggestion_count,
            "new_rules_proposed": False,
            "note": (
                "Active profile rules were checked before proposing additions; counterexamples and "
                "possible overlaps are visible in the decision guide. No profile mutation occurs here."
            ),
        },
        "recommendation": _recommendation(cohort_counts, work_identity, source_analysis),
        "non_effects": [
            "No source collection, external research, or weekly workflow ran as part of the audit.",
            "No automatic issue creation, canonical state migration, identity merge, ranking change, source change, or profile mutation occurred.",
            "The shadow-ranking experiment is not reopened or replaced; no opaque taste model is trained.",
            "Editor-reported availability is not treated as independently verified publication or rights fact.",
        ],
        "limits": [
            "Counts are refreshed from the chosen baseline; observed counts in the issue are dated observations.",
            "Repeated runs of one work are one decision, never independent positive examples.",
            "Historical product complaints are unknown until reproduced; only focused follow-ups are proposed.",
            "Insufficient evidence is an accepted finding.",
        ],
        "next_action": (
            "Review the decision guide and backlog with the editor; confirm any taste learning through "
            "preview-and-confirm and any product or configuration change as a separate authorized step."
        ),
    }


def validate_editorial_audit(report: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the canonical audit shape."""
    if not isinstance(report, Mapping):
        raise EditorialAuditError("Editorial audit must be a JSON object")
    if report.get("artifact_type") != EDITORIAL_AUDIT_ARTIFACT_TYPE:
        raise EditorialAuditError("Editorial audit has an unsupported artifact type")
    if report.get("schema_version") != EDITORIAL_AUDIT_SCHEMA_VERSION:
        raise EditorialAuditError("Editorial audit has an unsupported schema version")
    if report.get("canonical_effect") != "none":
        raise EditorialAuditError("Editorial audit must declare canonical_effect none")
    for section in (
        "evidence_baseline",
        "cohort_counts",
        "work_identity",
        "interpretations",
        "decision_guide",
        "quality_and_information_gap_backlog",
        "evaluation_set",
        "source_analysis",
        "profile_check",
        "recommendation",
    ):
        if section not in report:
            raise EditorialAuditError(f"Editorial audit is missing required section '{section}'")
    run_date = str(report.get("run_date", ""))
    _validate_iso_date(run_date)
    return dict(report)


def render_editorial_audit_markdown(report: Mapping[str, Any]) -> str:
    """Render the editor-readable audit from the canonical JSON model."""
    lines: list[str] = [
        f"# Editorial audit ({report.get('run_date', '')})",
        "",
        "Read-only, evidence-backed audit. No ranking model, no automatic classification in production, no new taste rules.",
        "",
        f"- Artifact: `{EDITORIAL_AUDIT_ARTIFACT_TYPE}` v{EDITORIAL_AUDIT_SCHEMA_VERSION}",
        f"- Canonical effect: `{report.get('canonical_effect', 'none')}`",
        "",
    ]
    baseline = report.get("evidence_baseline", {})
    if isinstance(baseline, Mapping):
        lines.extend(["## Evidence baseline", ""])
        for key in sorted(baseline):
            if key in {"reproducibility", "decision_time_limitation"}:
                continue
            lines.append(f"- {key}: `{baseline[key]}`")
        if baseline.get("decision_time_limitation"):
            lines.append(f"- Decision-time limitation: {baseline['decision_time_limitation']}")
        lines.append("")
    counts = report.get("cohort_counts", {})
    if isinstance(counts, Mapping):
        lines.extend(
            [
                "## Cohort counts (refreshed from baseline)",
                "",
                f"- Records: {counts.get('records_total', 0)}; decided: {counts.get('decided_total', 0)}; "
                f"unreviewed: {counts.get('unreviewed_total', 0)}",
                f"- Bare refusals (exact «Нет.»): {counts.get('bare_refusal_total', 0)} — retained as unspecified, no inference",
                f"- Substantive notes: {counts.get('substantive_note_total', 0)}",
                "",
            ]
        )
        interpretations = report.get("interpretations", [])
        if isinstance(interpretations, list):
            lines.extend(
                [
                    f"- Full interpretations audited: {len(interpretations)} decided records in cohort priority order; "
                    "presentation samples below are drawn from that audited population.",
                    "",
                ]
            )
        per_status = counts.get("per_status", {})
        if isinstance(per_status, Mapping):
            lines.extend(["| Status | Records | Distinct works | Substantive | Bare |", "|---|---|---|---|---|"])
            for status in (*DECIDED_STATUSES, "unreviewed"):
                entry = per_status.get(status, {})
                if isinstance(entry, Mapping):
                    lines.append(
                        f"| {status} | {entry.get('records', 0)} | {entry.get('distinct_works', 0)} "
                        f"| {entry.get('substantive_notes', 0)} | {entry.get('bare_refusals', 0)} |"
                    )
            lines.append("")
    work = report.get("work_identity", {})
    if isinstance(work, Mapping):
        lines.extend(
            [
                "## Work identity",
                "",
                f"- Decided records: {work.get('decided_record_count', 0)}; "
                f"distinct decided works: {work.get('distinct_decided_work_count', 0)}",
                f"- Method: {work.get('method', '')}",
                f"- Limitation: {work.get('limitation', '')}",
                "- Identities are never merged here; flags need human review.",
                "",
            ]
        )
    guide = report.get("decision_guide", [])
    if isinstance(guide, list):
        lines.extend(["## Editorial decision guide (hypotheses, human review required)", ""])
        for distinction in guide:
            if not isinstance(distinction, Mapping):
                continue
            lines.append(f"### {distinction.get('id', '')}")
            lines.append("")
            lines.append(str(distinction.get("claim", "")))
            lines.append("")
            if distinction.get("evidence_changed_requires_review") is True:
                lines.append(
                    "- Warning: retained evidence changed since review; "
                    "affected cases below are excluded from supporting evidence pending re-review."
                )
            for case in distinction.get("supporting_cases", []) or []:
                if not isinstance(case, Mapping):
                    continue
                if case.get("curated_evidence_ok") is False:
                    lines.append(
                        f"- Evidence changed, requires review (excluded from supporting evidence): "
                        f"{case.get('title', '')} ({case.get('identity_key', '')}) "
                        f"[{case.get('editor_status', '')}]: «{case.get('editor_note', '')}» "
                        f"({case.get('curated_evidence_check', '')})"
                    )
                    continue
                lines.append(
                    f"- Supporting: {case.get('title', '')} ({case.get('identity_key', '')}) "
                    f"[{case.get('editor_status', '')}]: «{case.get('editor_note', '')}»"
                )
            for case in distinction.get("opposing_or_qualifying_cases", []) or []:
                if not isinstance(case, Mapping):
                    continue
                if case.get("curated_evidence_ok") is False:
                    lines.append(
                        f"- Evidence changed, requires review (excluded from qualifying evidence): "
                        f"{case.get('title', '')} ({case.get('identity_key', '')}) "
                        f"[{case.get('editor_status', '')}]: «{case.get('editor_note', '')}» "
                        f"({case.get('curated_evidence_check', '')})"
                    )
                    continue
                lines.append(
                    f"- Qualifying: {case.get('title', '')} ({case.get('identity_key', '')}) "
                    f"[{case.get('editor_status', '')}]: «{case.get('editor_note', '')}»"
                )
            lines.append(f"- Uncertainty: {distinction.get('uncertainty', '')}")
            relationship = distinction.get("existing_profile_relationship", {})
            if isinstance(relationship, Mapping):
                related = relationship.get("related_active_entries", [])
                if related:
                    lines.append(
                        f"- Related active profile entries: {', '.join(item.get('id', '') for item in related)}"
                    )
                else:
                    lines.append("- Related active profile entries: none; a new rule would need explicit confirmation")
            lines.append("")
    backlog = report.get("quality_and_information_gap_backlog", [])
    if isinstance(backlog, list):
        lines.extend(["## Quality and information-gap backlog (focused follow-ups, not auto-filed)", ""])
        for item in backlog:
            if not isinstance(item, Mapping):
                continue
            lines.append(f"### {item.get('id', '')}: {item.get('label', '')}")
            lines.append("")
            lines.append(f"- Currency: `{item.get('currency', '')}`")
            if item.get("currency_basis"):
                lines.append(f"- Currency basis: {item.get('currency_basis', '')}")
            lines.append(f"- Evidence in substantive notes: {item.get('evidence_count_in_substantive_notes', 0)}")
            if item.get("code_refs"):
                lines.append(f"- Code refs: {', '.join(str(ref) for ref in item['code_refs'])}")
            if item.get("issue_refs"):
                lines.append(
                    f"- Issue refs: {', '.join(str(ref) for ref in item['issue_refs'])} "
                    "(status unverified offline; requires a separate check)"
                )
            if item.get("verification_date"):
                lines.append(f"- Verified on: {item.get('verification_date', '')}")
            lines.append(f"- Follow-up: {item.get('followup', '')}")
            lines.append("")
    evaluation = report.get("evaluation_set", [])
    if isinstance(evaluation, list):
        lines.extend(["## Curated evaluation-set proposal", ""])
        for case in evaluation:
            if not isinstance(case, Mapping):
                continue
            identity = case.get("identity_key", "")
            if identity == "__bare_refusal_class__":
                lines.append(f"- Bare-refusal class: exact «Нет.» stays unspecified; ambiguous={case.get('ambiguous')}")
                continue
            changed_suffix = (
                " [retained evidence changed, requires review]"
                if case.get("evidence_changed_requires_review") is True
                else ""
            )
            lines.append(
                f"- {identity} [{case.get('editor_status', case.get('status', ''))}]: "
                f"{case.get('expected_behavior', '')} (ambiguous={case.get('ambiguous')}){changed_suffix}"
            )
        lines.append("")
        lines.append(
            "Ambiguous cases stay ambiguous; anecdotes become product-rule tests only after intended behavior is established."
        )
        lines.append("")
    recommendation = report.get("recommendation", {})
    if isinstance(recommendation, Mapping):
        lines.extend(["## Concise recommendation", ""])
        for step in recommendation.get("priority_next_steps", []) or []:
            lines.append(f"- {step}")
        lines.append("")
        lines.append(f"- Can be learned: {recommendation.get('what_can_be_learned', '')}")
        lines.append(f"- Cannot be learned: {recommendation.get('what_cannot_be_learned', '')}")
        lines.append(f"- Future evaluation: {recommendation.get('future_ranking_evaluation_proposal', '')}")
        lines.append(f"- Taste learning: {recommendation.get('taste_learning_protocol', '')}")
        lines.append("")
    lines.extend(
        [
            "## Non-effects and limits",
            "",
            "No collection, research, workflow, issue creation, migration, merge, ranking, source, or profile change occurred.",
            "Insufficient evidence is an accepted finding.",
            "",
        ]
    )
    return "\n".join(lines)


def _json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _paths_overlap(first: Path, second: Path) -> bool:
    first_resolved = first.resolve(strict=False)
    second_resolved = second.resolve(strict=False)
    return (
        first_resolved == second_resolved
        or first_resolved in second_resolved.parents
        or second_resolved in first_resolved.parents
    )


def run_editorial_audit(request: EditorialAuditRequest) -> tuple[dict[str, Any], int]:
    """Run one offline audit and publish its JSON/Markdown pair.

    Candidate History, the Editorial Taste Profile and the alias table are
    read through the State Store of the state directory; the evidence
    baseline names the committed files and their SHA-256 as Git holds them.
    """
    run_date = request.run_date or date.today().isoformat()
    try:
        _validate_iso_date(run_date)
    except EditorialAuditError as exc:
        return _error_report(str(exc)), 2
    try:
        state_dir = request.state_dir
        output_dir = request.output_dir
        # The single file, or the directory of one file per book (#1073).
        history_path = candidate_history_location(state_dir)
        profile_path = state_dir / "editorial-profile.json"
        source_metrics_path = state_dir / "source-metrics.json"
        suggestions_path = state_dir / "derived-rule-suggestions.json"
        aliases_path = state_dir / "candidate-identity-aliases.json"
        store = open_state_store(state_dir)
        history_raw = _read_collection(store, Collection.CANDIDATE_HISTORY, history_path, "Candidate History")
        if history_raw == []:
            # The store reads a missing history as an empty one; both are refused.
            raise EditorialAuditError(f"Candidate History does not exist or is empty: {history_path}")
        profile_raw = _read_collection(store, Collection.EDITORIAL_PROFILE, profile_path, "Editorial Taste Profile")
        source_metrics_raw = _read_json_file(source_metrics_path, "Source Metrics", required=False)
        suggestions_raw = _read_json_file(suggestions_path, "Derived Rule Suggestions", required=False)
        aliases_raw = _read_collection(
            store, Collection.CANDIDATE_IDENTITY_ALIASES, aliases_path, "Candidate Identity Aliases"
        )
        history = _validate_history(history_raw)
        profile = _validate_profile(profile_raw)
        if source_metrics_raw is not None and not isinstance(source_metrics_raw, Mapping):
            raise EditorialAuditError("Source Metrics must be a JSON object")
        if suggestions_raw is not None and not isinstance(suggestions_raw, Mapping):
            raise EditorialAuditError("Derived Rule Suggestions must be a JSON object")
        if aliases_raw is not None and not isinstance(aliases_raw, Mapping):
            raise EditorialAuditError("Candidate Identity Aliases must be a JSON object")

        json_destination = resolve_storage_destination(
            output_dir / EDITORIAL_AUDIT_JSON_FILENAME, "file", "editorial audit JSON report"
        )
        md_destination = resolve_storage_destination(
            output_dir / EDITORIAL_AUDIT_MD_FILENAME, "file", "editorial audit Markdown report"
        )
        state_resolved = state_dir.resolve(strict=False)
        output_resolved = output_dir.resolve(strict=False)
        if _paths_overlap(output_resolved, state_resolved):
            raise EditorialAuditError("editorial audit output must remain outside canonical weekly/editorial state")
        for input_path in (history_path, profile_path, source_metrics_path, suggestions_path, aliases_path):
            for destination in (json_destination.path, md_destination.path):
                if _paths_overlap(destination, input_path.resolve(strict=False)):
                    raise EditorialAuditError("editorial audit destination overlaps an input artifact")

        baseline = _baseline_section(
            history_path, profile_path, source_metrics_path, suggestions_path, aliases_path, history, profile
        )
        report = build_editorial_audit(
            history,
            profile,
            source_metrics_raw if isinstance(source_metrics_raw, Mapping) else None,
            suggestions_raw if isinstance(suggestions_raw, Mapping) else None,
            aliases_raw if isinstance(aliases_raw, Mapping) else None,
            run_date=run_date,
            baseline=baseline,
        )
        validated = validate_editorial_audit(report)
        json_text = _json_text(validated)
        markdown_text = render_editorial_audit_markdown(validated)
        json_destination.path.parent.mkdir(parents=True, exist_ok=True)
        with pair_write_lock(json_destination.path, md_destination.path):
            publish_pair(json_destination.path, md_destination.path, json_text, markdown_text)
        return validated, 0
    except (EditorialAuditError, SafeArtifactError, OSError, TypeError, ValueError) as exc:
        return _error_report(str(exc)), 2


def _baseline_section(
    history_path: Path,
    profile_path: Path,
    source_metrics_path: Path,
    suggestions_path: Path,
    aliases_path: Path,
    history: list[dict[str, Any]],
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    def digest(path: Path) -> str | None:
        try:
            if not path.exists() or path.is_symlink():
                return None
            return _sha256_bytes(path.read_bytes())
        except OSError:
            return None

    decided = [record for record in history if str(record.get("editor_status") or "").strip() in DECIDED_STATUSES]
    first_seen = sorted(
        str(record.get("first_seen_date", "")) for record in history if str(record.get("first_seen_date", "")).strip()
    )
    last_seen = sorted(
        str(record.get("last_seen_date", "")) for record in history if str(record.get("last_seen_date", "")).strip()
    )
    decisions = sorted(
        str(record.get("editor_decision_date", ""))
        for record in decided
        if str(record.get("editor_decision_date", "")).strip()
    )
    active_count = len(_active_profile_entries(profile))
    entries = profile.get("entries")
    entry_count = len(entries) if isinstance(entries, list) else 0
    return {
        "history_path": _repo_relative(history_path),
        "profile_path": _repo_relative(profile_path),
        "source_metrics_path": _repo_relative(source_metrics_path),
        "suggestions_path": _repo_relative(suggestions_path),
        "aliases_path": _repo_relative(aliases_path),
        # The single file's digest, or one over every book's file (#1073).
        "history_sha256": stored_history_digest(history_path.parent),
        "profile_sha256": digest(profile_path),
        "source_metrics_sha256": digest(source_metrics_path),
        "suggestions_sha256": digest(suggestions_path),
        "aliases_sha256": digest(aliases_path),
        "history_records": len(history),
        "history_decided_records": len(decided),
        "profile_entries": entry_count,
        "profile_active_entries": active_count,
        "first_seen_range": [first_seen[0], first_seen[-1]] if first_seen else [],
        "last_seen_range": [last_seen[0], last_seen[-1]] if last_seen else [],
        "decision_date_range": [decisions[0], decisions[-1]] if decisions else [],
        "inputs_resolved_from": "Candidate History and retained run evidence, not remembered titles",
    }


def _error_report(error: str) -> dict[str, Any]:
    return {
        "artifact_type": EDITORIAL_AUDIT_ARTIFACT_TYPE,
        "version": EDITORIAL_AUDIT_SCHEMA_VERSION,
        "status": "input_error",
        "error": error.strip() or "input_error",
        "next_action": "Correct the audit inputs, then run the offline editorial audit again.",
    }


__all__ = [
    "BARE_REFUSAL",
    "CURATED_CASES",
    "DECIDED_STATUSES",
    "DEFAULT_EDITORIAL_AUDIT_OUTPUT_DIR",
    "DEFAULT_EDITORIAL_AUDIT_STATE_DIR",
    "EDITORIAL_AUDIT_ARTIFACT_TYPE",
    "EDITORIAL_AUDIT_JSON_FILENAME",
    "EDITORIAL_AUDIT_MD_FILENAME",
    "EDITORIAL_AUDIT_SCHEMA_VERSION",
    "EditorialAuditError",
    "EditorialAuditRequest",
    "build_editorial_audit",
    "render_editorial_audit_markdown",
    "run_editorial_audit",
    "validate_editorial_audit",
]
