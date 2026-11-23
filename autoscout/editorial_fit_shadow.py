"""Editorial-fit shadow scoring with the ``fit-v2`` Jev question set (issue #725).

The weekly ordering carries almost no editorial signal, and the September
backtest showed that the ``fit-v1`` rubric rewarded serious books for educated
readers while the editor favours popular, broadly appealing nonfiction.
``fit-v2`` asks the question the editor's recorded words actually pose.

Everything here is advisory. A score is recorded beside each candidate before
the editor reviews the week, in its own directory, and is compared with the
editor's decisions only afterwards. Nothing reads these artifacts back into
the pipeline: ``priority_label``, ``publisher_fit``, ordering, and every state
file stay as they were. Activation is a separate explicit editor decision.

Editor decisions, notes, priority, visibility, and the Editorial Taste Profile
never enter the model input. The questions are AutoScout's own English wording
and quote no editor text.
"""

from __future__ import annotations

import math
import random
import time
from collections import Counter
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from autoscout.content_description import current_english_description
from autoscout.fit_scores import BACKLOG_ARTIFACT_TYPE
from autoscout.fit_scores import QUESTION_SET_VERSION
from autoscout.fit_scores import SCORES_ARTIFACT_TYPE
from autoscout.fit_scores import SHADOW_DIRECTORY as SHADOW_DIRECTORY
from autoscout.fit_scores import FitScoreArtifactError
from autoscout.fit_scores import book_keys as _book_keys
from autoscout.fit_scores import durable_id as _durable_id
from autoscout.fit_scores import load_score_artifacts
from autoscout.fit_scores import recorded_fit_scores
from autoscout.fit_scores import shadow_dir
from autoscout.jev_adapter import JEV_PINNED_MODEL
from autoscout.jev_adapter import JEV_PRICE_USD_PER_MILLION_INPUT_TOKENS
from autoscout.jev_adapter import REASON_BUDGET_EXHAUSTED
from autoscout.jev_adapter import REASON_CREDENTIAL_MISSING
from autoscout.jev_adapter import TERMINAL_REASONS
from autoscout.jev_adapter import JevAdapter
from autoscout.jev_adapter import JevAssessment
from autoscout.jev_adapter import JevQuestion
from autoscout.jev_adapter import request_digest
from autoscout.jev_adapter import request_payload
from autoscout.jev_adapter import unavailable
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.review_rendering import current_review_order
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import open_state_store

REPORT_ARTIFACT_TYPE = "editorial-fit-shadow-report"
SCHEMA_VERSION = 1
MINIMUM_SHADOW_WEEKS = 3

DEFAULT_WORKERS = 6
DEFAULT_STAGE_BUDGET_SECONDS = 180.0


POSITIVE_STATUSES = frozenset({"interested", "maybe_watch", "needs_deeper_analysis"})
NEGATIVE_STATUSES = frozenset({"reject"})

_DESCRIPTION_RU_CHARS = 1200
_DESCRIPTION_EN_CHARS = 1200
_ANNOTATION_CHARS = 800
_SIGNAL_NOTE_CHARS = 200
_MAX_SIGNALS = 5
_SHORT_TEXT_CHARS = 300

FIT_INSTRUCTIONS = (
    "The state describes a foreign nonfiction book. Judge how well it suits a Russian trade publisher "
    "whose list is popular, broadly appealing nonfiction for a wide adult audience. The best candidates "
    "can be read with no background, speak to things many people care about in their own lives "
    "(psychology, relationships, self-development, emotional wellbeing, health and the body, how the "
    "mind works) or tell a vivid, widely interesting story, and travel well outside the country where "
    "they were written. Being serious, scholarly, prize-winning, or well reviewed does not by itself "
    "make a book a better fit. Weaker candidates are academic or specialist, depend on local context, "
    "are personal stories of people readers have not heard of, repeat what many existing books already "
    "say, or are reference books, field guides, travel guides, or cookbooks. Judge only from the state."
)
FIT_LEVELS = (
    (
        "0",
        "Clearly not a fit: an academic or specialist study, a narrow local subject, a personal story "
        "by a private person, a reference or hobby guide, or a cookbook",
    ),
    (
        "1",
        "Weak fit: readable, but the subject is narrow, tied to one country, already covered by many "
        "similar books, or demanding for a casual reader",
    ),
    ("2", "Possible fit: solid general nonfiction with no clear popular hook"),
    (
        "3",
        "Good fit: an accessible book on a subject many ordinary readers care about, with a clear "
        "hook or a useful takeaway",
    ),
    (
        "4",
        "Strong fit: a popular book with obvious wide appeal, a fresh angle on something that touches "
        "everyday life, or an author or premise that sells itself",
    ),
)
# The audience options keep the fit-v1 labels so the audience breakdown stays
# comparable with the September backtest.
AUDIENCE_OPTIONS = (
    (
        "mass_market",
        "Broad popular nonfiction for a wide audience (popular psychology, big-idea books, celebrity "
        "or widely known authors).",
    ),
    (
        "educated_general",
        "Serious but accessible nonfiction for curious educated general readers (narrative history, "
        "popular science, essays, reportage).",
    ),
    ("niche_specialist", "Written for a specialist, academic, professional, or narrow hobby audience."),
    (
        "practical_manual",
        "A practical how-to, workbook, guide, or reference book whose main value is instructions.",
    ),
    ("unclear", "The state does not give enough information to tell."),
)
# Jev cannot look anything up, so author reach is judged from the state alone
# and has an explicit option for a state that does not say.
AUTHOR_REACH_OPTIONS = (
    (
        "widely_known",
        "The state itself says or shows that the author is widely known to general readers: a "
        "celebrity, a bestselling author, or a public figure named as such.",
    ),
    (
        "established_in_field",
        "The state presents the author as a recognised expert, scholar, journalist, or practitioner, "
        "without evidence of fame among general readers.",
    ),
    (
        "little_known",
        "The state presents the author as a private person or a first-time author with no stated public standing.",
    ),
    ("unclear", "The state does not say enough about the author to tell."),
)

FIT_QUESTIONS: tuple[JevQuestion, ...] = (
    JevQuestion("fit", "score", FIT_INSTRUCTIONS, FIT_LEVELS),
    JevQuestion("audience", "choice", "Which audience is this book primarily written for?", AUDIENCE_OPTIONS),
    JevQuestion(
        "author_reach",
        "choice",
        "How well known is the author of this book to general readers, judging only from the state?",
        AUTHOR_REACH_OPTIONS,
    ),
)

# Fixed before any fit-v2 result exists. Code combines the atomic answers.
MASS_MARKET_WEIGHT = 0.15
AUTHOR_REACH_WEIGHT = 0.10
COMPOSITE_FORMULA = (
    f"fit.expected + {MASS_MARKET_WEIGHT} * P(audience=mass_market) "
    f"+ {AUTHOR_REACH_WEIGHT} * P(author_reach=widely_known)"
)

_PRIORITY_SCORE = {"A": 3.0, "B": 2.0, "C": 1.0, "D": 0.0}

SCORER_PRIORITY = "current_priority_label"
SCORER_ORDER = "current_review_order"
SCORER_FIT = "fit_v2_fit_only"
SCORER_COMPOSITE = "fit_v2_composite"
SCORERS = (SCORER_PRIORITY, SCORER_ORDER, SCORER_FIT, SCORER_COMPOSITE)


class EditorialFitShadowError(ValueError):
    """The shadow inputs cannot be read as the expected artifacts."""


# ── Model input ───────────────────────────────────────────────────────────


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value).split())[:limit] if isinstance(value, str) else ""


def fit_model_state(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Build the model input from a strict allowlist of public book facts.

    Fields are named one by one; nothing else on the candidate can reach the
    vendor. Editor status, editor note, priority, visibility, profile notes,
    Visible Risk, and Russian-edition findings are deliberately absent.
    """
    authors = candidate.get("english_author_display") or candidate.get("primary_authors") or []
    if isinstance(authors, str):
        authors = [authors]
    families = candidate.get("independent_signal_families")
    signals: list[dict[str, str]] = []
    evidence = candidate.get("source_evidence")
    for entry in evidence if isinstance(evidence, list) else []:
        if not isinstance(entry, Mapping):
            continue
        signal = {
            "source": _text(entry.get("source_name"), _SHORT_TEXT_CHARS),
            "signal_type": _text(entry.get("signal_type"), _SHORT_TEXT_CHARS),
            "strength": _text(entry.get("signal_strength"), _SHORT_TEXT_CHARS),
            "note": _text(entry.get("evidence_note"), _SIGNAL_NOTE_CHARS),
        }
        signals.append({key: value for key, value in signal.items() if value})
        if len(signals) == _MAX_SIGNALS:
            break
    state: dict[str, Any] = {
        "title": _text(candidate.get("title"), _SHORT_TEXT_CHARS),
        "english_title": _text(candidate.get("english_title"), _SHORT_TEXT_CHARS),
        "authors": [_text(author, _SHORT_TEXT_CHARS) for author in authors if isinstance(author, str)][:10]
        if isinstance(authors, list)
        else [],
        "category": _text(candidate.get("candidate_category"), _SHORT_TEXT_CHARS),
        "publisher": _text(candidate.get("publisher"), _SHORT_TEXT_CHARS),
        "publication_year": _text(str(candidate.get("publication_year") or ""), 10),
        "english_annotation": _text(candidate.get("english_annotation"), _ANNOTATION_CHARS),
        "description_en": _text(current_english_description(candidate), _DESCRIPTION_EN_CHARS),
        "description_ru": _text(candidate.get("content_description"), _DESCRIPTION_RU_CHARS),
        "independent_signal_families": [
            _text(family, _SHORT_TEXT_CHARS) for family in families if isinstance(family, str)
        ][:10]
        if isinstance(families, list)
        else [],
        "signals": signals,
    }
    return {key: value for key, value in state.items() if value not in ("", [], None)}


def preview_request(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """The exact request one candidate would produce. Nothing is sent."""
    return request_payload(fit_model_state(candidate), FIT_QUESTIONS)


# ── Combining answers ─────────────────────────────────────────────────────


def _rounded(distribution: Mapping[str, float]) -> dict[str, float]:
    return {label: round(float(value), 6) for label, value in distribution.items()}


def _top(distribution: Mapping[str, float], order: Sequence[str]) -> str:
    return max(order, key=lambda label: (distribution.get(label, 0.0), -order.index(label)))


def combine_answers(assessment: JevAssessment) -> tuple[dict[str, Any], float]:
    """Turn the three atomic answers into recorded components and one composite."""
    fit = assessment.answers["fit"].probabilities
    audience = assessment.answers["audience"].probabilities
    author_reach = assessment.answers["author_reach"].probabilities
    top_level = len(FIT_LEVELS) - 1
    expected_level = sum(int(level) * probability for level, probability in fit.items())
    expected = expected_level / top_level
    composite = (
        expected
        + MASS_MARKET_WEIGHT * audience.get("mass_market", 0.0)
        + AUTHOR_REACH_WEIGHT * author_reach.get("widely_known", 0.0)
    )
    components = {
        "fit": {
            "probabilities": _rounded(fit),
            "expected_level": round(expected_level, 6),
            "expected": round(expected, 6),
            "confidence": assessment.answers["fit"].confidence,
        },
        "audience": {
            "probabilities": _rounded(audience),
            "top": _top(audience, [label for label, _ in AUDIENCE_OPTIONS]),
            "confidence": assessment.answers["audience"].confidence,
        },
        "author_reach": {
            "probabilities": _rounded(author_reach),
            "top": _top(author_reach, [label for label, _ in AUTHOR_REACH_OPTIONS]),
            "confidence": assessment.answers["author_reach"].confidence,
        },
    }
    return components, round(composite, 6)


# ── Scoring one weekly run ────────────────────────────────────────────────


def scores_path(output_dir: Path, run_date: str) -> Path:
    return shadow_dir(output_dir) / f"scores-{run_date}.json"


def _list_of_dicts(path: Path, *, required: bool) -> list[dict[str, Any]]:
    try:
        data = read_json(path, default=None)
    except (OSError, ValueError) as exc:
        raise EditorialFitShadowError(f"{path.name} is not readable JSON") from exc
    if data is None:
        if required:
            raise EditorialFitShadowError(f"{path.name} is missing")
        return []
    if not isinstance(data, list):
        raise EditorialFitShadowError(f"{path.name} is not a list")
    return [entry for entry in data if isinstance(entry, dict)]


def read_candidates(output_dir: Path) -> list[dict[str, Any]]:
    """The run's final candidates, in the order the review presents them."""
    return _list_of_dicts(output_dir / "scouting-candidates.json", required=True)


def _state_records(store: StateStore, collection: Collection) -> list[dict[str, Any]]:
    """The records of one canonical collection; a collection never written has none."""
    try:
        data = store.read(collection)
    except (OSError, ValueError) as exc:
        raise EditorialFitShadowError(f"{collection.value} is not readable JSON") from exc
    if not isinstance(data, list):
        raise EditorialFitShadowError(f"{collection.value} is not a list")
    return [entry for entry in data if isinstance(entry, dict)]


def _shadow_state(state_dir: Path, state_store: StateStore | None) -> StateStore:
    return state_store if state_store is not None else open_state_store(state_dir)


def _hidden_ids(store: StateStore) -> set[str]:
    return {
        str(record.get("identity_key", "")).strip()
        for record in _state_records(store, Collection.CANDIDATE_VISIBILITY)
        if str(record.get("visibility", "")).strip().lower() == "hidden"
    } - {""}


class _EditorDecisions:
    """Editor statuses from Candidate History, joined by identity key or durable ID."""

    def __init__(self, store: StateStore) -> None:
        self.aliases = store.read(Collection.CANDIDATE_IDENTITY_ALIASES)
        self._by_key: dict[str, str] = {}
        for entry in _state_records(store, Collection.CANDIDATE_HISTORY):
            status = str(entry.get("editor_status", "")).strip().lower()
            if status:
                for key in _book_keys(entry, self.aliases):
                    self._by_key[key] = status

    def book_keys(self, record: Mapping[str, Any]) -> set[str]:
        return _book_keys(record, self.aliases)

    def status_for(self, record: Mapping[str, Any]) -> str:
        identity = str(record.get("identity_key", "")).strip()
        durable = _durable_id(record, self.aliases)
        return self._by_key.get(f"identity:{identity}") or self._by_key.get(f"id:{durable}", "")


def _percentile(values: Sequence[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def _estimated_cost(input_tokens: int) -> float:
    return round(input_tokens / 1_000_000 * JEV_PRICE_USD_PER_MILLION_INPUT_TOKENS, 6)


class _BoundedCalls:
    """Calls to the adapter under one time budget; a terminal failure stops the rest."""

    def __init__(self, adapter: JevAdapter, budget_seconds: float, monotonic: Callable[[], float]) -> None:
        self._adapter = adapter
        self._monotonic = monotonic
        self._deadline = monotonic() + budget_seconds
        self._stop_reason = ""

    @property
    def stop_reason(self) -> str:
        """The terminal failure that stopped the calls, or "" when none did."""
        return self._stop_reason

    def attempt(self, state: Mapping[str, Any]) -> JevAssessment | None:
        """The assessment of *state*, or ``None`` when the call was not made."""
        if self._stop_reason or self._monotonic() >= self._deadline:
            return None
        assessment = self._adapter.assess(state, FIT_QUESTIONS)
        if not assessment.available and assessment.reason_code in TERMINAL_REASONS and not self._stop_reason:
            self._stop_reason = assessment.reason_code
        return assessment

    def not_made(self) -> JevAssessment:
        """The record of a call that was not made: the terminal failure, or the spent budget."""
        return unavailable(self._adapter.kind, self._stop_reason or REASON_BUDGET_EXHAUSTED)


def score_weekly_run(
    output_dir: Path,
    state_dir: Path,
    run_date: str,
    adapter: JevAdapter,
    *,
    workers: int = DEFAULT_WORKERS,
    budget_seconds: float = DEFAULT_STAGE_BUDGET_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    state_store: StateStore | None = None,
    review_ordered_by_fit: bool = False,
) -> dict[str, Any]:
    """Score the run's final editor-facing candidates and write the advisory artifact.

    *review_ordered_by_fit* says the review of this run lists books by the
    fit score (#1105). It is recorded on the artifact, because from then on
    the editor's decisions are no longer blind to the score.

    Canonical state is read through *state_store*, the run's store, or a
    store opened over *state_dir*.

    Reads the report candidates, the visibility state, and Candidate History;
    writes only inside ``editorial-fit-shadow``. A candidate already scored
    today on identical evidence is reused, so a rerun spends nothing and never
    replaces a score recorded before the editor's decision with a later one.
    A failed call is recorded as ``unavailable`` with its reason; it is never
    dropped.
    """
    candidates = read_candidates(output_dir)
    store = _shadow_state(state_dir, state_store)
    hidden = _hidden_ids(store)
    statuses = _EditorDecisions(store)
    by_identity = {str(candidate.get("identity_key", "")).strip(): candidate for candidate in candidates}
    # The baseline is the order the review shows, not the order of the file.
    visible = [
        by_identity[identity]
        for identity in current_review_order(candidates, _state_records(store, Collection.CANDIDATE_HISTORY))
        if identity and identity in by_identity and identity not in hidden
    ]

    target = scores_path(output_dir, run_date)
    previous: list[dict[str, Any]] = []
    existing = read_json(target, default=None) if target.exists() else None
    if isinstance(existing, dict) and existing.get("question_set_version") == QUESTION_SET_VERSION:
        for record in existing.get("records", []):
            if isinstance(record, dict) and str(record.get("identity_key", "")):
                # Older records carry their provenance on the artifact.
                record.setdefault("adapter", existing.get("adapter", ""))
                previous.append(record)

    # Today's earlier record of the same book. An identity correction can
    # rename the key between two runs, so the match goes through the durable
    # Candidate ID as well as the key.
    previous_by_key = {key: record for record in previous for key in statuses.book_keys(record)}
    prior = [
        next((previous_by_key[key] for key in sorted(statuses.book_keys(candidate)) if key in previous_by_key), None)
        for candidate in visible
    ]

    def kept(index: int, digest: str) -> bool:
        """Whether today's earlier record stands instead of a new call.

        An identical request to the same adapter is reused. A record made
        before the editor decided, scored or failed, is frozen once the editor
        has decided: a later call could no longer test the rubric, and
        replacing the record would drop an observation from the evaluation.
        """
        record = prior[index]
        if record is None:
            return False
        if not record.get("decided_before_scoring") and bool(statuses.status_for(visible[index])):
            # Failed or scored, the pre-decision observation stands.
            return True
        return (
            record.get("status") == "scored"
            and record.get("evidence_digest") == digest
            and record.get("adapter") == adapter.kind
        )

    calls = _BoundedCalls(adapter, budget_seconds, monotonic)

    def assess(state: Mapping[str, Any]) -> JevAssessment:
        return calls.attempt(state) or calls.not_made()

    states = [fit_model_state(candidate) for candidate in visible]
    digests = [request_digest(state, FIT_QUESTIONS) for state in states]
    reused_flags = [kept(index, digest) for index, digest in enumerate(digests)]
    to_call = [index for index, reused in enumerate(reused_flags) if not reused]
    assessments: dict[int, JevAssessment] = {}
    if to_call:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for index, assessment in zip(to_call, pool.map(lambda i: assess(states[i]), to_call), strict=True):
                assessments[index] = assessment

    records: list[dict[str, Any]] = []
    for index, candidate in enumerate(visible):
        identity = str(candidate["identity_key"]).strip()
        if reused_flags[index]:
            record = dict(prior[index] or {})
            record["reused"] = True
            records.append(record)
            continue
        assessment = assessments[index]
        record = {
            "identity_key": identity,
            "candidate_id": str(candidate.get("candidate_id", "")),
            "title": _text(candidate.get("english_title") or candidate.get("title"), _SHORT_TEXT_CHARS),
            "canonical_rank": index + 1,
            "priority_label": str(candidate.get("priority_label", "")),
            # Read from Candidate History only to mark the record; it is never
            # model input. A book the editor had already decided on cannot
            # test a score made afterwards, so the report leaves it out.
            "decided_before_scoring": bool(statuses.status_for(candidate)),
            "evidence_digest": digests[index],
            "adapter": assessment.adapter_kind,
            "status": "scored" if assessment.available else "unavailable",
            "reason_code": assessment.reason_code,
            "returned_model": assessment.returned_model,
            "input_tokens": assessment.input_tokens,
            "latency_ms": assessment.latency_ms,
            "reused": False,
        }
        if assessment.available:
            record["components"], record["composite"] = combine_answers(assessment)
        records.append(record)

    # A book scored earlier today that has since left the review (hidden,
    # excluded, or decided) stays on record. Later editorial actions must not
    # change which books the evaluation observed.
    matched = {id(record) for record in prior if record is not None}
    for record in previous:
        if id(record) not in matched:
            records.append({**record, "reused": True})

    scored = [record for record in records if record["status"] == "scored"]
    failed = [record for record in records if record["status"] != "scored"]
    input_tokens = sum(int(record.get("input_tokens", 0)) for record in records)
    latencies = [int(record["latency_ms"]) for record in records if int(record.get("latency_ms", 0)) > 0]
    counts = {
        "candidates": len(records),
        "scored": len(scored),
        "unavailable": len(failed),
        "reused": sum(1 for record in records if record["reused"]),
    }
    reason_counts = dict(sorted(Counter(str(record["reason_code"]) for record in failed).items()))
    artifact = {
        "artifact_type": SCORES_ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "advisory": True,
        "non_effect": (
            "Shadow scores only. priority_label, publisher_fit, review ordering, and all weekly and "
            "editorial state are unchanged."
        ),
        "run_date": run_date,
        "question_set_version": QUESTION_SET_VERSION,
        "requested_model": JEV_PINNED_MODEL,
        "adapter": adapter.kind,
        "record_adapters": sorted({str(record.get("adapter", "")) for record in records}),
        "composite_formula": COMPOSITE_FORMULA,
        "counts": counts,
        "unavailable_reason_counts": reason_counts,
        "input_tokens": input_tokens,
        "estimated_cost_usd": _estimated_cost(input_tokens),
        "latency_ms": {"p50": _percentile(latencies, 0.5), "p95": _percentile(latencies, 0.95)},
        "records": records,
    }
    if review_ordered_by_fit or (isinstance(existing, dict) and existing.get("review_ordered_by_fit") is True):
        # Present only on a run whose review used the score, so earlier artifacts keep their shape.
        # A rescore of the same run keeps the mark: the review of that day was already shown.
        artifact["review_ordered_by_fit"] = True
    target.parent.mkdir(parents=True, exist_ok=True)
    write_json(target, artifact)
    return {
        "status": "ok",
        "run_date": run_date,
        "adapter": adapter.kind,
        **counts,
        "unavailable_reason_counts": reason_counts,
        # The terminal failure that stopped this scoring, "" when none did.
        "stopped_by": calls.stop_reason,
    }


# ── Scoring the undecided backlog ─────────────────────────────────────────


def backlog_scores_path(output_dir: Path, run_date: str) -> Path:
    return shadow_dir(output_dir) / f"backlog-scores-{run_date}.json"


def score_undecided_backlog(
    output_dir: Path,
    state_dir: Path,
    run_date: str,
    adapter: JevAdapter,
    *,
    workers: int = DEFAULT_WORKERS,
    budget_seconds: float = DEFAULT_STAGE_BUDGET_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    state_store: StateStore | None = None,
) -> dict[str, Any]:
    """Score the undecided books of Candidate History that have no recorded score.

    A run scores only its own candidates, so a book of an earlier run that the
    editor has not decided on can have no score, and the review cannot order
    it. This scores those books, the newest first, and writes them to a
    backlog artifact of *run_date*. The review reads the artifact; the shadow
    report and the ranking benchmark do not, because these scores were not
    made beside a run and have no review position to compare with.

    Hidden and decided books are left alone. A call that was not made, because
    the budget ran out or the key was refused, leaves no record: the book
    stays in the backlog for the next run. A failed call is recorded as
    ``unavailable`` and the book is called again on the next run. Nothing is
    written when no call was made.
    """
    store = _shadow_state(state_dir, state_store)
    hidden = _hidden_ids(store)
    statuses = _EditorDecisions(store)
    try:
        recorded = recorded_fit_scores(output_dir, statuses.aliases)
    except FitScoreArtifactError as exc:
        raise EditorialFitShadowError(str(exc)) from exc
    pending = [
        record
        for record in _state_records(store, Collection.CANDIDATE_HISTORY)
        if str(record.get("identity_key", "")).strip()
        and str(record.get("identity_key", "")).strip() not in hidden
        and not statuses.status_for(record)
        and recorded.score_for(record) is None
    ]
    pending.sort(key=lambda record: str(record["identity_key"]))
    pending.sort(key=lambda record: str(record.get("first_seen_date", "")), reverse=True)

    calls = _BoundedCalls(adapter, budget_seconds, monotonic)
    states = [fit_model_state(record) for record in pending]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        assessments = list(pool.map(calls.attempt, states))

    records: list[dict[str, Any]] = []
    for book, state, assessment in zip(pending, states, assessments, strict=True):
        if assessment is None:
            continue
        record: dict[str, Any] = {
            "identity_key": str(book["identity_key"]).strip(),
            "candidate_id": str(book.get("candidate_id", "")),
            "title": _text(book.get("english_title") or book.get("title"), _SHORT_TEXT_CHARS),
            "evidence_digest": request_digest(state, FIT_QUESTIONS),
            "adapter": assessment.adapter_kind,
            "status": "scored" if assessment.available else "unavailable",
            "reason_code": assessment.reason_code,
            "returned_model": assessment.returned_model,
            "input_tokens": assessment.input_tokens,
            "latency_ms": assessment.latency_ms,
        }
        if assessment.available:
            record["components"], record["composite"] = combine_answers(assessment)
        records.append(record)

    result = {
        "status": "ok",
        "undecided_without_score": len(pending),
        "scored": sum(1 for record in records if record["status"] == "scored"),
        "unavailable": sum(1 for record in records if record["status"] != "scored"),
        "not_attempted": len(pending) - len(records),
    }
    if not records:
        return result
    target = backlog_scores_path(output_dir, run_date)
    existing = read_json(target, default=None) if target.exists() else None
    called = {key for record in records for key in statuses.book_keys(record)}
    earlier = [
        record
        for record in (existing.get("records", []) if isinstance(existing, dict) else [])
        # A failure of earlier today that was now called again is replaced.
        if isinstance(record, dict) and not statuses.book_keys(record) & called
    ]
    records = [*earlier, *records]
    input_tokens = sum(int(record.get("input_tokens", 0)) for record in records)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        target,
        {
            "artifact_type": BACKLOG_ARTIFACT_TYPE,
            "schema_version": SCHEMA_VERSION,
            "advisory": True,
            "run_date": run_date,
            "question_set_version": QUESTION_SET_VERSION,
            "requested_model": JEV_PINNED_MODEL,
            "input_tokens": input_tokens,
            "estimated_cost_usd": _estimated_cost(input_tokens),
            "records": records,
        },
    )
    return result


# ── Report arithmetic ─────────────────────────────────────────────────────


def _sort_key(score: float | None) -> float:
    """A failed call has no score. It ranks below every scored book, so a
    failure costs recall instead of quietly leaving the comparison."""
    return -math.inf if score is None else score


def auc(positives: Sequence[float | None], negatives: Sequence[float | None]) -> float | None:
    """Mann-Whitney AUC: the chance a random positive outranks a random
    negative. A tie counts half. Undefined when either class is empty."""
    if not positives or not negatives:
        return None
    wins = 0.0
    negative_keys = sorted(_sort_key(score) for score in negatives)
    for score in positives:
        key = _sort_key(score)
        below = _count_below(negative_keys, key)
        equal = _count_at_most(negative_keys, key) - below
        wins += below + equal / 2
    return wins / (len(positives) * len(negatives))


def _count_below(ordered: Sequence[float], key: float) -> int:
    low, high = 0, len(ordered)
    while low < high:
        middle = (low + high) // 2
        if ordered[middle] < key:
            low = middle + 1
        else:
            high = middle
    return low


def _count_at_most(ordered: Sequence[float], key: float) -> int:
    low, high = 0, len(ordered)
    while low < high:
        middle = (low + high) // 2
        if ordered[middle] <= key:
            low = middle + 1
        else:
            high = middle
    return low


def auc_interval(
    positives: Sequence[float | None],
    negatives: Sequence[float | None],
    *,
    samples: int = 1000,
    seed: int = 7,
) -> tuple[float, float] | None:
    """A seeded percentile bootstrap, so the same inputs give the same interval."""
    if not positives or not negatives:
        return None
    rng = random.Random(seed)
    values = sorted(
        auc([rng.choice(positives) for _ in positives], [rng.choice(negatives) for _ in negatives]) or 0.0
        for _ in range(samples)
    )
    return values[int(0.025 * samples)], values[min(samples - 1, int(0.975 * samples))]


def top_fraction_hits(scored: Sequence[tuple[float | None, bool]], fraction: float) -> tuple[float, int]:
    """Expected positives in the top ``fraction`` and the size of that top.

    Books with equal scores are interchangeable, so a tie group that straddles
    the cut contributes its positives in proportion to the places it fills.
    That is the expectation over every tie order, with no random draw.
    """
    count = len(scored)
    if count == 0:
        return 0.0, 0
    top = max(1, math.floor(count * fraction + 0.5))
    groups: dict[float, list[bool]] = {}
    for score, positive in scored:
        groups.setdefault(_sort_key(score), []).append(positive)
    hits = 0.0
    remaining = top
    for key in sorted(groups, reverse=True):
        if remaining <= 0:
            break
        group = groups[key]
        taken = min(remaining, len(group))
        hits += sum(group) * taken / len(group)
        remaining -= taken
    return hits, top


def ranking_metrics(scored: Sequence[tuple[float | None, bool]]) -> dict[str, Any]:
    positives = [score for score, positive in scored if positive]
    negatives = [score for score, positive in scored if not positive]
    metrics: dict[str, Any] = {"auc": None, "auc_ci95": None}
    area = auc(positives, negatives)
    if area is not None:
        interval = auc_interval(positives, negatives)
        metrics["auc"] = round(area, 4)
        metrics["auc_ci95"] = [round(interval[0], 4), round(interval[1], 4)] if interval else None
    for label, fraction in (("20", 0.2), ("40", 0.4)):
        hits, top = top_fraction_hits(scored, fraction)
        metrics[f"recall_at_{label}"] = round(hits / len(positives), 4) if positives else None
        metrics[f"precision_at_{label}"] = round(hits / top, 4) if top else None
    return metrics


def _positive_rates(rows: Sequence[dict[str, Any]], key: str, order: Sequence[str]) -> list[dict[str, Any]]:
    result = []
    for label in order:
        group = [row for row in rows if row.get(key) == label]
        if group:
            positive = sum(1 for row in group if row["positive"])
            result.append({"value": label, "books": len(group), "positive_rate": round(positive / len(group), 4)})
    return result


def _slice_report(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    positives = sum(1 for row in rows if row["positive"])
    scored = [row for row in rows if row["scored"]]
    return {
        "labeled_books": len(rows),
        "positives": positives,
        "rejects": len(rows) - positives,
        "base_rate": round(positives / len(rows), 4) if rows else None,
        "failed_calls_among_labeled": len(rows) - len(scored),
        "scorers": {
            scorer: ranking_metrics([(row["scores"][scorer], row["positive"]) for row in rows]) for scorer in SCORERS
        },
        "positive_rate_by_audience": _positive_rates(scored, "audience", [label for label, _ in AUDIENCE_OPTIONS]),
        "positive_rate_by_fit_level": _positive_rates(scored, "fit_level", [label for label, _ in FIT_LEVELS]),
    }


def _load_score_artifacts(output_dir: Path) -> list[dict[str, Any]]:
    try:
        return load_score_artifacts(output_dir)
    except FitScoreArtifactError as exc:
        raise EditorialFitShadowError(str(exc)) from exc


def build_shadow_report(output_dir: Path, state_dir: Path, *, state_store: StateStore | None = None) -> dict[str, Any]:
    """Compare recorded shadow scores with the editor's later decisions.

    Each book is judged on its first shadow record only, and only when that
    record was made before the editor had decided on the book. ``already_known``
    is about the editor's prior knowledge, not fit, and is left out.
    """
    return _report_and_observations(output_dir, state_dir, state_store)[0]


def labeled_observations(
    output_dir: Path, state_dir: Path, *, state_store: StateStore | None = None
) -> list[dict[str, Any]]:
    """The books the shadow report counts, one entry each, oldest run first.

    Each entry has the ``run_date`` of the book's first shadow record, whether
    the editor's decision is ``positive``, whether the review of that run was
    ordered by the fit score (``review_ordered_by_fit``), and its ``scores`` by
    scorer name (``None`` for a failed call). The ranking benchmark reads these.
    """
    return _report_and_observations(output_dir, state_dir, state_store)[1]


def _report_and_observations(
    output_dir: Path, state_dir: Path, state_store: StateStore | None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    artifacts = _load_score_artifacts(output_dir)
    statuses = _EditorDecisions(_shadow_state(state_dir, state_store))
    # One book is one observation even when an identity correction renamed
    # its key or redirected its Candidate ID between runs.
    seen: set[str] = set()
    weeks: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []
    input_tokens = 0
    latencies: list[int] = []
    calls = 0
    failed_calls = 0
    for artifact in artifacts:
        records = [record for record in artifact["records"] if isinstance(record, dict)]
        total = len(records)
        rows: list[dict[str, Any]] = []
        first_records = 0
        decided_before = 0
        undecided = 0
        already_known = 0
        for record in records:
            calls += 1
            if record.get("status") != "scored":
                failed_calls += 1
            if int(record.get("latency_ms", 0) or 0) > 0:
                latencies.append(int(record["latency_ms"]))
            keys = statuses.book_keys(record)
            if not keys or keys & seen:
                continue
            seen |= keys
            first_records += 1
            if record.get("decided_before_scoring"):
                decided_before += 1
                continue
            status = statuses.status_for(record)
            if status == "already_known":
                already_known += 1
                continue
            if status not in POSITIVE_STATUSES | NEGATIVE_STATUSES:
                undecided += 1
                continue
            is_scored = record.get("status") == "scored"
            components = record.get("components") if is_scored else None
            rank = int(record.get("canonical_rank", total) or total)
            rows.append(
                {
                    "run_date": artifact["run_date"],
                    "review_ordered_by_fit": artifact.get("review_ordered_by_fit") is True,
                    "positive": status in POSITIVE_STATUSES,
                    "scored": is_scored,
                    "audience": components["audience"]["top"] if components else None,
                    "fit_level": str(math.floor(components["fit"]["expected_level"] + 0.5)) if components else None,
                    "scores": {
                        SCORER_PRIORITY: _PRIORITY_SCORE.get(str(record.get("priority_label", "")), 0.0),
                        SCORER_ORDER: 1.0 - (rank - 1) / max(1, total - 1),
                        SCORER_FIT: components["fit"]["expected"] if components else None,
                        SCORER_COMPOSITE: record.get("composite") if is_scored else None,
                    },
                }
            )
        input_tokens += int(artifact.get("input_tokens", 0) or 0)
        all_rows.extend(rows)
        weeks.append(
            {
                "run_date": artifact["run_date"],
                "adapters": sorted({str(record.get("adapter") or artifact.get("adapter", "")) for record in records}),
                "candidates": total,
                "scored": sum(1 for record in records if record.get("status") == "scored"),
                "unavailable": sum(1 for record in records if record.get("status") != "scored"),
                "first_scored_this_week": first_records,
                "excluded_decided_before_scoring": decided_before,
                "excluded_already_known": already_known,
                "awaiting_editor_decision": undecided,
                "review_ordered_by_fit": artifact.get("review_ordered_by_fit") is True,
                **_slice_report(rows),
            }
        )
    # A week tests the rubric only when it has a decided book that was scored.
    weeks_with_decisions = sum(1 for week in weeks if week["labeled_books"] - week["failed_calls_among_labeled"] > 0)
    enough = weeks_with_decisions >= MINIMUM_SHADOW_WEEKS
    report = {
        "artifact_type": REPORT_ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "advisory": True,
        "question_set_version": QUESTION_SET_VERSION,
        "requested_model": JEV_PINNED_MODEL,
        "composite_formula": COMPOSITE_FORMULA,
        "positive_statuses": sorted(POSITIVE_STATUSES),
        "negative_statuses": sorted(NEGATIVE_STATUSES),
        "shadow_weeks_scored": len(weeks),
        "shadow_weeks_with_decisions": weeks_with_decisions,
        "minimum_shadow_weeks": MINIMUM_SHADOW_WEEKS,
        "activation": {
            "status": "not_activated",
            "evidence": "ready_for_editor_decision" if enough else "insufficient",
            "note": (
                "Shadow mode only. Populating publisher_fit or changing ordering needs a separate "
                "explicit editor decision backed by this report."
            ),
        },
        "operations": {
            "calls": calls,
            "failed_calls": failed_calls,
            "input_tokens": input_tokens,
            "estimated_cost_usd": _estimated_cost(input_tokens),
            "latency_ms": {"p50": _percentile(latencies, 0.5), "p95": _percentile(latencies, 0.95)},
        },
        "cumulative": _slice_report(all_rows),
        "weeks": weeks,
    }
    return report, all_rows


def _cell(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, list):
        return f"{value[0]:.2f}–{value[1]:.2f}"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


_SCORER_TITLES = {
    SCORER_PRIORITY: "Current priority label",
    SCORER_ORDER: "Current review order",
    SCORER_FIT: "fit-v2 fit only",
    SCORER_COMPOSITE: "fit-v2 composite",
}


def _slice_markdown(title: str, data: Mapping[str, Any]) -> list[str]:
    lines = [
        f"### {title}",
        "",
        f"{data['positives']} positive / {data['rejects']} reject, base rate {_cell(data['base_rate'])}. "
        f"Failed calls among labeled books: {data['failed_calls_among_labeled']} (ranked last, not dropped).",
        "",
        "| Scorer | AUC | 95% CI | Recall@20% | Precision@20% | Recall@40% |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for scorer in SCORERS:
        metrics = data["scorers"][scorer]
        lines.append(
            f"| {_SCORER_TITLES[scorer]} | {_cell(metrics['auc'])} | {_cell(metrics['auc_ci95'])} | "
            f"{_cell(metrics['recall_at_20'])} | {_cell(metrics['precision_at_20'])} | "
            f"{_cell(metrics['recall_at_40'])} |"
        )
    for heading, key in (("audience", "positive_rate_by_audience"), ("fit level", "positive_rate_by_fit_level")):
        if data[key]:
            parts = [f"{row['value']} {_cell(row['positive_rate'])} (n={row['books']})" for row in data[key]]
            lines.extend(["", f"Positive rate by {heading}: " + ", ".join(parts) + "."])
    lines.append("")
    return lines


def render_shadow_report(report: Mapping[str, Any]) -> str:
    operations = report["operations"]
    activation = report["activation"]
    lines = [
        "# Editorial-fit shadow report",
        "",
        f"Question set `{report['question_set_version']}`, model `{report['requested_model']}`. "
        "Advisory only: no priority label, ordering, or state was changed.",
        "",
        f"Shadow weeks scored: {report['shadow_weeks_scored']}; with editor decisions: "
        f"{report['shadow_weeks_with_decisions']} of the {report['minimum_shadow_weeks']} needed.",
        f"Activation: {activation['status']} (evidence: {activation['evidence']}). {activation['note']}",
        "",
        f"Composite, fixed before any result: `{report['composite_formula']}`.",
        "A book counts once, on its first shadow score, and only when that score was recorded before the "
        "editor decided on it. `already_known` is excluded.",
        "",
        "## Operations",
        "",
        f"Calls: {operations['calls']}, failed: {operations['failed_calls']}. "
        f"Input tokens: {operations['input_tokens']}, estimated cost ${operations['estimated_cost_usd']:.4f}. "
        f"Latency p50 / p95: {_cell(operations['latency_ms']['p50'])} / {_cell(operations['latency_ms']['p95'])} ms.",
        "",
        "## Results",
        "",
    ]
    lines.extend(_slice_markdown("Cumulative", report["cumulative"]))
    for week in report["weeks"]:
        lines.extend(
            _slice_markdown(
                f"Week {week['run_date']}",
                week,
            )
        )
        lines.extend(
            [
                f"Adapters: {', '.join(week['adapters']) or 'n/a'}. Candidates {week['candidates']}: scored {week['scored']}, unavailable {week['unavailable']}; "
                f"awaiting a decision {week['awaiting_editor_decision']}, decided before scoring "
                f"{week['excluded_decided_before_scoring']}, already known {week['excluded_already_known']}."
                + (
                    " After activation: the review of this run was ordered by the fit score."
                    if week["review_ordered_by_fit"]
                    else ""
                ),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def write_shadow_report(output_dir: Path, state_dir: Path, *, state_store: StateStore | None = None) -> dict[str, Any]:
    report = build_shadow_report(output_dir, state_dir, state_store=state_store)
    directory = shadow_dir(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "report.json", report)
    (directory / "report.md").write_text(render_shadow_report(report), encoding="utf-8")
    return report


class ProductionEditorialFitShadowStage:
    """The weekly stage: score the run with the live adapter, refresh the report, then score the backlog."""

    def score(
        self,
        output_dir: Path,
        state_dir: Path,
        run_date: str,
        *,
        state_store: StateStore | None = None,
        review_ordered_by_fit: bool = False,
    ) -> dict[str, Any]:
        from autoscout.jev_adapter import LiveJevAdapter
        from autoscout.jev_adapter import load_api_key

        api_key = load_api_key()
        if not api_key:
            # No key means no call was attempted, so there is nothing to record.
            return {"status": "skipped", "reason": REASON_CREDENTIAL_MISSING}
        adapter = LiveJevAdapter(api_key)
        started = time.monotonic()
        result = score_weekly_run(
            output_dir,
            state_dir,
            run_date,
            adapter,
            state_store=state_store,
            review_ordered_by_fit=review_ordered_by_fit,
        )
        write_shadow_report(output_dir, state_dir, state_store=state_store)
        # The backlog has what the run left of the stage budget, and its
        # failure cannot take the scores of the run with it.
        if result["stopped_by"]:
            # A terminal failure stopped the run just now; the backlog would only repeat it.
            result["backlog"] = {"status": "skipped", "reason": result["stopped_by"]}
            return result
        try:
            result["backlog"] = score_undecided_backlog(
                output_dir,
                state_dir,
                run_date,
                adapter,
                budget_seconds=DEFAULT_STAGE_BUDGET_SECONDS - (time.monotonic() - started),
                state_store=state_store,
            )
        except Exception as exc:
            result["backlog"] = {"status": "failed", "exception_type": type(exc).__name__}
        return result
