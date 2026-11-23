"""A short Russian description from the PM deal logline (Issue #950).

A Publishers Marketplace deal announces a book one to three years before it
is published, so no page describes it and a full description is not expected
(#947). The deal itself carries one logline, PM's sentence about the book.
With the weekly-config switch ``pm_logline_descriptions`` on, this stage asks
a model to restate that logline in Russian, adding nothing, and records the
text under its own provenance, ``pm-deal-logline``. The review labels it as
built from the deal logline, never as a book description, and the coverage
summary counts it on its own line, never as a gap.

The logline never leaves the Git-ignored PM pass result (#943): it is read
from that file here and sent to the model, and nothing but its SHA-256 digest
is kept beside the description. The description is cached by that digest in a
Git-ignored file next to the pass result, so the same logline is sent once,
whatever week it comes back in.

The book stays PM-only for #947: neither description stage searches it until
its publication season or another source cites it. When they do, a
description grounded on its own replaces this one, because both stages treat
a logline description like a missing one.

With the switch off (the default in code), this stage reads no logline and
calls no model.

A call that fails is not a logline the model declined. On 2026-11-10 every
call failed on a model the account could not use, each book was recorded as
declined, and the stage reported ``ok`` with nothing written. A failed call
is now counted under ``failed`` with its reason and never recorded as
declined, and any such book left without a description sets
``attention_required`` on the result.

With the second switch, ``pm_logline_deepseek_fallback``, a book whose Codex
call failed is sent once to the DeepSeek API instead. The operator allowed
the logline to go there on 2026-11-12; the permission is recorded in
``docs/agents/contracts/pm-precollection.md``. The key comes from
``DEEPSEEK_API_KEY`` or the operator's key file outside the repository.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import time
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from autoscout.content_description import DESCRIPTION_PM_LOGLINE_PROVENANCE
from autoscout.content_description import description_provenance_record
from autoscout.content_description import has_nonfallback_description
from autoscout.content_description import is_pm_logline_description
from autoscout.content_description import record_has_usable_description
from autoscout.content_description import validate_content_description
from autoscout.json_io import read_json_or_default
from autoscout.json_io import write_json
from autoscout.pm_publication import is_pm_deal_evidence
from autoscout.pm_publication import is_pm_only
from autoscout.source_eligibility import historical_record_source_eligibility_exclusion_reason
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import open_state_store

# The weekly-config switch. Off unless the config sets it to ``true``.
SWITCH = "pm_logline_descriptions"
WEEKLY_STAGE_NAME = "pm-logline-descriptions"

# A usable logline says something about the book in a sentence or more. PM
# also prints fragments: the second title of a two-book deal ("and THE
# SECOND TITLE") or a bare phrase. Measured on a week of real deals emails
# (2026-11-03, counts only), every Non-fiction logline had ten words or more
# and every fragment fewer than six, or began with a joining word. Eight words
# and 40 characters sit between the two; a leading joining word marks the
# rest of an earlier deal, whatever its length.
MIN_LOGLINE_WORDS = 8
MIN_LOGLINE_CHARS = 40
LEADING_FRAGMENT_WORDS = ("and", "plus", "or", "with", "also", "along", "as", "together")
# PM sometimes puts the foreword credit first: "with a foreword by A. Name,
# an introduction to ...". The credit is cut and the rest is judged on its own.
_LEADING_CREDIT = re.compile(
    r"^with (?:a|an|the) (?:foreword|preface|introduction|afterword) by [^,;]{2,80}[,;]\s+", re.IGNORECASE
)
# The longest logline sent: a longer one is cut at a word, never sent whole.
MAX_LOGLINE_CHARS = 1000
# The written description restates one sentence, so it is short. A longer
# answer has padded the logline with something it did not say.
MAX_DESCRIPTION_CHARS = 600

# One call per book, each bounded; the stage stops starting calls when its
# budget cannot fit one more. A book not reached waits for the next run.
CALL_TIMEOUT_SECONDS = 120.0
STAGE_BUDGET_SECONDS = 900.0

# The digest of the logline a description was written from, on its provenance.
LOGLINE_DIGEST_FIELD = "logline_digest"
# The description cache: digest -> Russian description. Git-ignored, beside the pass result.
CACHE_FILENAME = "pm-logline-descriptions.json"
CACHE_SCHEMA_VERSION = 1
# The second weekly-config switch: send a book whose Codex call failed to DeepSeek.
FALLBACK_SWITCH = "pm_logline_deepseek_fallback"
# Outside the repository, mode 0600. Read only when the environment has no key.
DEEPSEEK_KEY_FILE = Path.home() / ".config" / "autoscout" / "deepseek.env"
# A failure the stage itself names: the agent raised instead of answering.
CALL_FAILURE_EXCEPTION = "exception"

# Logline digests whose last call gave no usable answer, with that call's
# date. Books never tried go first and the longest-declined go next, so a
# prefix of failing calls can never keep later books from their turn.
DECLINED_FIELD = "declined"

PROMPT = (
    "Read work_items.json. Return only a JSON object keyed by the exact identity_key of each item. "
    "Each item carries deal_logline: the one-sentence summary of a forthcoming book from a "
    "publishing-rights deal announcement. Title and authors identify the book; they are not evidence. "
    'Return each value as {"content_description": "..."}: original Russian prose of one or two sentences '
    "that restates only what deal_logline says about the book's subject. "
    "Add no fact, name, number, date, place, claim or judgement that deal_logline does not state, "
    "and do not use outside knowledge, search or browse. "
    "Leave out every pitch comparison to other books, authors, films or shows, such as "
    '"pitched as X meets Y", "in the vein of X" or "for fans of X". '
    "Leave out the deal itself: its terms, agents, editors and publishers. "
    "Paraphrase; never copy the logline word for word, and do not translate or repeat the title. "
    "When deal_logline says too little about the book's subject for one full sentence, "
    "omit content_description for that item."
)

FALLBACK_PROMPT = (
    "You help an editor at a Russian publishing house who scouts foreign nonfiction for translation rights. "
    "The user message carries one forthcoming book: its title, its authors and deal_logline, the one-sentence "
    "summary of the book from a publishing-rights deal announcement. Title and authors identify the book; "
    "they are not evidence.\n\n"
    "Write original Russian prose of one or two sentences that restates only what deal_logline says about the "
    "book's subject. Add no fact, name, number, date, place, claim or judgement that deal_logline does not "
    "state, and use nothing you know about the book or author from elsewhere. Leave out every pitch comparison "
    "to other books, authors, films or shows, and leave out the deal itself: its terms, agents, editors and "
    "publishers. Paraphrase; never copy the logline word for word, and do not translate or repeat the title. "
    "Write natural editorial Russian and give personal and place names in their established Russian form.\n\n"
    "Return only JSON with a single string field named content_description. When deal_logline says too little "
    "about the book's subject for one full sentence, return an empty string in that field."
)

_WORD = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*", re.UNICODE)


def enabled(config: Any) -> bool:
    """True only when the weekly config sets the switch to ``true``."""
    return isinstance(config, Mapping) and config.get(SWITCH) is True


def config_enabled(config_path: Path) -> bool:
    """The switch, read from the weekly config file; an unreadable config is off."""
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return enabled(config)


def normalized_logline(value: Any) -> str:
    return " ".join(str(value or "").split()) if isinstance(value, str) else ""


def usable_logline(value: Any) -> str:
    """The logline when it is a sentence or more about the book, else ''."""
    text = _LEADING_CREDIT.sub("", normalized_logline(value).strip(" ,;:"), count=1)
    words = _WORD.findall(text)
    if len(words) < MIN_LOGLINE_WORDS or len(text) < MIN_LOGLINE_CHARS:
        return ""
    if words[0].casefold() in LEADING_FRAGMENT_WORDS:
        return ""
    if len(text) > MAX_LOGLINE_CHARS:
        text = text[:MAX_LOGLINE_CHARS].rsplit(" ", 1)[0]
    return text


def logline_digest(logline: str) -> str:
    return hashlib.sha256(normalized_logline(logline).encode("utf-8")).hexdigest()


def _title_key(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _deal_rows(collection: Any) -> list[tuple[dict[str, Any], str]]:
    """Each readable result row in the capture's own shape, with its raw logline."""
    from autoscout.pm_deal_capture import _checked_row

    rows = collection.get("rows") if isinstance(collection, Mapping) else None
    found: list[tuple[dict[str, Any], str]] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            checked = _checked_row(row)
        except (KeyError, TypeError, ValueError):
            continue
        found.append((checked, normalized_logline(row.get("logline"))))
    return found


def record_logline(record: Mapping[str, Any], rows: Sequence[tuple[dict[str, Any], str]]) -> str | None:
    """The logline of the one deal row this record's PM evidence came from.

    The evidence keeps no deal key, so a row matches when it gives the same
    rights lead and evidence link the capture wrote on the evidence, and the
    record carries its title and authors. Neither the rights lead nor the
    link names the author, so a different book with the same title, agent
    and terms is told apart by its authors. ``None`` when no row or more than
    one matches.
    """
    from autoscout.pm_deal_capture import evidence_url
    from autoscout.pm_deal_capture import rights_deal

    title = _title_key(record.get("title"))
    raw_authors = record.get("primary_authors")
    authors = [_title_key(author) for author in raw_authors] if isinstance(raw_authors, list) else []
    entries = [entry for entry in record.get("source_evidence", []) or [] if is_pm_deal_evidence(entry)]
    matches: set[str] = set()
    keys: set[str] = set()
    for row, logline in rows:
        if _title_key(row["title"]) != title or [_title_key(author) for author in row["authors"]] != authors:
            continue
        expected_deal, expected_url = rights_deal(row), evidence_url(row)
        if any(
            entry.get("rights_deal") == expected_deal and entry.get("evidence_url") == expected_url for entry in entries
        ):
            keys.add(row["deal_key"])
            matches.add(logline)
    if len(keys) != 1:
        return None
    return next(iter(matches))


def work_item(record: Mapping[str, Any], logline: str) -> dict[str, Any]:
    """The one work item sent for a book: who it is and its logline, nothing else."""
    authors = record.get("primary_authors", [])
    return {
        "identity_key": str(record.get("identity_key", "")).strip(),
        "title": str(record.get("title", "")),
        "metadata": {
            "title": str(record.get("title", "")),
            "primary_authors": [str(author) for author in authors if str(author).strip()]
            if isinstance(authors, list)
            else [],
        },
        "deal_logline": logline,
    }


def _validated(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("content_description", "")
    # The placeholder is valid Russian prose, but it is no description.
    if not has_nonfallback_description(value):
        return None
    validated, _reason = validate_content_description(value)
    if validated is None or len(validated) > MAX_DESCRIPTION_CHARS:
        return None
    return validated


def _call(agent: Any, item: dict[str, Any], timeout_seconds: float) -> tuple[Any, str]:
    """One agent call with this stage's prompt: the answer, and why the call failed or ``""``.

    An agent that takes ``_on_failure`` names its own failure; an older or
    test agent that does not is taken at its word, as before.
    """
    try:
        parameters = list(inspect.signature(agent.generate).parameters.values())
    except (TypeError, ValueError):
        parameters = []
    open_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters)
    names = {parameter.name for parameter in parameters}
    kwargs: dict[str, Any] = {}
    if open_kwargs or "_prompt_override" in names:
        kwargs["_prompt_override"] = PROMPT
    if open_kwargs or "_timeout_seconds" in names:
        kwargs["_timeout_seconds"] = timeout_seconds
    failures: list[str] = []
    # Only for an agent that names it: one taking ``**kwargs`` may pass them on to one that does not.
    if "_on_failure" in names:
        kwargs["_on_failure"] = failures.append
    response = agent.generate([item], **kwargs)
    answer = response.get(item["identity_key"]) if isinstance(response, Mapping) else None
    return answer, (failures[0] if failures else "")


def _polled_call(agent: Any, item: dict[str, Any], timeout_seconds: float, progress: Any | None) -> tuple[Any, str]:
    """One call on a daemon worker, so the heartbeat keeps polling while the model works."""
    if progress is None or not callable(getattr(progress, "collect", None)):
        return _call(agent, item, timeout_seconds)
    from autoscout.bounded_workers import DaemonThreadPool

    pool = DaemonThreadPool(1, thread_name_prefix="autoscout-pm-logline-")
    try:
        answer, failure = progress.collect([pool.submit(_call, agent, item, timeout_seconds)])[0]
        return answer, str(failure)
    finally:
        pool.shutdown(wait=False)


def _load_cache(path: Path) -> dict[str, str]:
    value = read_json_or_default(path, {})
    entries = value.get("descriptions") if isinstance(value, Mapping) else None
    if not isinstance(entries, Mapping):
        return {}
    return {str(key): str(text) for key, text in entries.items() if isinstance(text, str) and text.strip()}


def _load_declined(path: Path) -> dict[str, str]:
    """The date each logline digest last got no usable answer, from the cache file."""
    value = read_json_or_default(path, {})
    entries = value.get(DECLINED_FIELD) if isinstance(value, Mapping) else None
    if not isinstance(entries, Mapping):
        return {}
    return {str(key): str(day) for key, day in entries.items() if isinstance(day, str) and day.strip()}


def _write_cache(path: Path, cache: Mapping[str, str], declined: Mapping[str, str]) -> None:
    write_json(
        path,
        {
            "schema_version": CACHE_SCHEMA_VERSION,
            "descriptions": dict(sorted(cache.items())),
            DECLINED_FIELD: dict(sorted(declined.items())),
        },
    )


def _pm_source(config_path: Path) -> Mapping[str, Any] | None:
    """The PM deals source of the weekly config, with its portfolio overlay applied.

    The live configs name the PM source's contract family in the weekly
    portfolio, not in their own source entry, so the config is resolved the
    way the run resolves it before the source is picked.
    """
    from autoscout.config import resolve_weekly_portfolio_or_raise
    from autoscout.pm_deal_capture import FAMILY
    from autoscout.pm_deal_capture import METHODS

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        config = resolve_weekly_portfolio_or_raise(raw, config_path) if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return None
    sources = config.get("sources", []) if isinstance(config, Mapping) else []
    return next(
        (
            item
            for item in sources
            if isinstance(item, Mapping)
            and (item.get("contract_family") == FAMILY or item.get("extraction_method") in METHODS)
        ),
        None,
    )


def _hidden_identities(store: StateStore) -> set[str]:
    records = store.read(Collection.CANDIDATE_VISIBILITY)
    return {
        str(record.get("identity_key", "")).strip()
        for record in (records if isinstance(records, list) else [])
        if isinstance(record, Mapping) and str(record.get("visibility", "")).strip().casefold() == "hidden"
    }


def _wanted(record: Mapping[str, Any], hidden: set[str]) -> bool:
    """A visible PM-only book with no description of its own, or with a logline one."""
    if not is_pm_only(record) or str(record.get("identity_key", "")).strip() in hidden:
        return False
    if historical_record_source_eligibility_exclusion_reason(record):
        return False
    return is_pm_logline_description(record) or not record_has_usable_description(record)


def _write_logline_description(record: dict[str, Any], description: str, digest: str, effective_date: str) -> None:
    record["content_description"] = description
    record["description_provenance"] = {
        **description_provenance_record(
            str(record.get("identity_key", "")).strip(),
            description,
            DESCRIPTION_PM_LOGLINE_PROVENANCE,
            applied_date=effective_date,
        ),
        LOGLINE_DIGEST_FIELD: digest,
    }
    # Nothing was searched or confirmed: no stage outcome or rejection
    # reason of an earlier attempt stays beside this text.
    record.pop("grounded_outcome", None)
    record.pop("content_description_rejection_reason", None)


def describe_from_loglines(
    config_path: Path,
    output_dir: Path,
    state_dir: Path,
    *,
    effective_date: str,
    agent: Any,
    collection_file: Path | None = None,
    cache_file: Path | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    stage_budget_seconds: float = STAGE_BUDGET_SECONDS,
    progress: Any | None = None,
    fallback_agent: Any | None = None,
    state_store: StateStore | None = None,
) -> dict[str, Any]:
    """Write a logline description for each PM-only book that has a usable logline.

    Canonical state is read and written through *state_store*, the run's
    store, or a store opened over *state_dir*. The descriptions are written
    at the end in one store transaction that reads Candidate History again
    and writes only to records that still want a logline description, so a
    description another writer gave a book during the model calls is kept.

    Returns counts only: no title and no logline. With the switch off, it
    returns at once, before any file is read. ``progress`` is the shared
    per-candidate heartbeat (``CandidateProgressSnapshot``): one row per book
    that needs a model call.

    ``fallback_agent`` gets a book once when the call to ``agent`` failed,
    never when ``agent`` answered and declined. After a failure no other
    book would escape (no usable model, no quota), ``agent`` is not called
    again in this run. A book whose calls all failed is counted under
    ``failed`` and tried again next run, ahead of the declined ones.
    """
    if not config_enabled(config_path):
        return {"status": "skipped", "reason": "switch_off"}
    from autoscout.pm_deal_capture import CAPTURE_ERROR
    from autoscout.pm_deal_capture import collection_path
    from autoscout.pm_deal_capture import read_collection

    if collection_file is None:
        source = _pm_source(Path(config_path))
        if source is None:
            return {"status": "skipped", "reason": "no_pm_source"}
        try:
            collection_file = collection_path(Path(config_path), source)
        except CAPTURE_ERROR:
            return {"status": "skipped", "reason": "pm_declaration"}
    try:
        collection = read_collection(collection_file)
    except CAPTURE_ERROR:
        collection = None
    if collection is None:
        return {"status": "skipped", "reason": "pm_collection_missing"}
    rows = _deal_rows(collection)
    cache_path = cache_file if cache_file is not None else collection_file.parent / CACHE_FILENAME
    cache = _load_cache(cache_path)
    declined = _load_declined(cache_path)

    store = state_store if state_store is not None else open_state_store(state_dir)
    history = store.read(Collection.CANDIDATE_HISTORY)
    if not isinstance(history, list):
        return {"status": "failed", "reason": "invalid_candidate_history"}
    hidden = _hidden_identities(store)
    counts = {
        "pm_only": 0,
        "no_usable_logline": 0,
        "unchanged": 0,
        "from_cache": 0,
        "written": 0,
        "model_calls": 0,
        "declined": 0,
        "failed": 0,
        "fallback_calls": 0,
        "written_by_fallback": 0,
        "not_attempted_stage_budget": 0,
    }
    failure_reasons: dict[str, int] = {}
    deadline = monotonic() + stage_budget_seconds
    # The description each book gets, by identity key, applied to fresh
    # history at the end.
    written: dict[str, tuple[str, str]] = {}

    def write(record: dict[str, Any], description: str, digest: str) -> None:
        _write_logline_description(record, description, digest, effective_date)
        written[str(record.get("identity_key", "")).strip()] = (description, digest)
        counts["written"] += 1

    to_call: list[tuple[dict[str, Any], str, str]] = []
    for record in history:
        if not isinstance(record, dict) or not _wanted(record, hidden):
            continue
        counts["pm_only"] += 1
        logline = usable_logline(record_logline(record, rows))
        if not logline:
            counts["no_usable_logline"] += 1
            continue
        digest = logline_digest(logline)
        provenance = record.get("description_provenance")
        if (
            is_pm_logline_description(record)
            and isinstance(provenance, Mapping)
            and provenance.get(LOGLINE_DIGEST_FIELD) == digest
        ):
            counts["unchanged"] += 1
            continue
        cached = _validated(cache.get(digest))
        if cached is not None:
            counts["from_cache"] += 1
            write(record, cached, digest)
            continue
        to_call.append((record, logline, digest))

    def key(record: Mapping[str, Any]) -> str:
        return str(record.get("candidate_id", "") or record.get("identity_key", "")).strip()

    # Stable: unattempted books keep Candidate History order, ahead of declined ones.
    to_call.sort(key=lambda entry: (entry[2] in declined, declined.get(entry[2], "")))
    if progress is not None:
        progress.initial([(key(record), str(record.get("title", "") or "")) for record, _logline, _digest in to_call])
    from autoscout.codex_model_pin import CODEX_CLI_BACKEND
    from autoscout.description_enrichment import CALL_FAILURE_PM_TEXT_NOT_PERMITTED
    from autoscout.description_enrichment import CALL_FAILURE_PW_TEXT_NOT_PERMITTED
    from autoscout.description_enrichment import CALL_FAILURES_FOR_EVERY_BOOK

    # Only a Codex call that failed may hand the logline to the fallback. An
    # agent without a pin is a Codex stand-in, as before #1149.
    codex_runs_the_call = (
        str(getattr(getattr(agent, "model_pin", None), "backend", CODEX_CLI_BACKEND)) == CODEX_CLI_BACKEND
    )
    refused_before_sending = frozenset({CALL_FAILURE_PM_TEXT_NOT_PERMITTED, CALL_FAILURE_PW_TEXT_NOT_PERMITTED})

    def attempt(caller: Any, record: dict[str, Any], logline: str, timeout_seconds: float) -> tuple[str | None, str]:
        """One call: the validated description, and why the call failed or ``""``."""
        try:
            answer, failure = _polled_call(caller, work_item(record, logline), timeout_seconds, progress)
        except Exception:
            return None, CALL_FAILURE_EXCEPTION
        return (None, failure) if failure else (_validated(answer), "")

    # The reason the agent, or the fallback, cannot serve any book of this run.
    agent_down = ""
    fallback_down = ""
    try:
        for record, logline, digest in to_call:
            if deadline - monotonic() < CALL_TIMEOUT_SECONDS:
                counts["not_attempted_stage_budget"] += 1
                continue
            if progress is not None:
                progress.begin(key(record))
            description: str | None = None
            failure = agent_down
            try:
                if not agent_down:
                    counts["model_calls"] += 1
                    description, failure = attempt(
                        agent, record, logline, min(CALL_TIMEOUT_SECONDS, deadline - monotonic())
                    )
                    if failure in CALL_FAILURES_FOR_EVERY_BOOK:
                        agent_down = failure
                remaining = deadline - monotonic()
                # The fallback answers a failed Codex call. A call another
                # backend made, or refused before sending, is no such failure,
                # so the logline is never sent elsewhere for it (#1149).
                fallback_allowed = codex_runs_the_call and failure not in refused_before_sending
                if failure and fallback_allowed and fallback_agent is not None and not fallback_down and remaining > 0:
                    counts["fallback_calls"] += 1
                    description, fallback_failure = attempt(
                        fallback_agent, record, logline, min(CALL_TIMEOUT_SECONDS, remaining)
                    )
                    if fallback_failure in CALL_FAILURES_FOR_EVERY_BOOK:
                        fallback_down = fallback_failure
                    if description is not None:
                        counts["written_by_fallback"] += 1
                    # The fallback answered: an empty answer is its own decision, not a failure.
                    failure = f"fallback_{fallback_failure}" if fallback_failure else ""
            finally:
                if progress is not None:
                    progress.finish(key(record))
            if failure:
                # Never recorded as declined: the book keeps its place for the next run.
                counts["failed"] += 1
                failure_reasons[failure] = failure_reasons.get(failure, 0) + 1
                continue
            if description is None:
                counts["declined"] += 1
                declined[digest] = effective_date
                _write_cache(cache_path, cache, declined)
                continue
            cache[digest] = description
            declined.pop(digest, None)
            _write_cache(cache_path, cache, declined)
            write(record, description, digest)
    except BaseException:
        if progress is not None:
            progress.final(status="failed")
        raise
    if progress is not None:
        progress.final()
    from autoscout.description_projection import project_history_onto_snapshots

    if written:
        with store.transaction():
            history = store.read(Collection.CANDIDATE_HISTORY)
            if not isinstance(history, list):
                raise ValueError("Candidate History changed into an invalid shape during the PM logline pass")
            hidden = _hidden_identities(store)
            for record in history:
                if not isinstance(record, dict):
                    continue
                pending = written.get(str(record.get("identity_key", "")).strip())
                if pending is not None and _wanted(record, hidden):
                    _write_logline_description(record, *pending, effective_date)
            store.replace(Collection.CANDIDATE_HISTORY, history)
    if written or counts["unchanged"]:
        # Projection is idempotent over durable history, so a retry after an
        # interrupted projection repairs the snapshots without a model call.
        project_history_onto_snapshots(history, output_dir, store)
    result: dict[str, Any] = {"status": "ok", **counts}
    if counts["failed"]:
        # Books this run could have described and did not, for a reason that is not theirs.
        result["failure_reasons"] = dict(sorted(failure_reasons.items()))
        result["attention_required"] = True
    return result


class DeepSeekLoglineAgent:
    """The fallback: one DeepSeek chat call per book, behind the same ``generate`` seam.

    It sends the title, the authors and the logline off this machine, under
    the permission recorded in ``docs/agents/contracts/pm-precollection.md``.
    """

    def __init__(self, api_key: str, *, model: str | None = None, client_factory: Any | None = None) -> None:
        from autoscout.pw_local_descriptions import DEEPSEEK_MODEL
        from autoscout.pw_local_descriptions import DeepSeekModel

        self._api_key = api_key
        self._model = model or DEEPSEEK_MODEL
        self._client_factory = client_factory or DeepSeekModel

    def generate(
        self,
        work_items: list[dict[str, Any]],
        *,
        _prompt_override: str | None = None,
        _timeout_seconds: float | None = None,
        _on_failure: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        from autoscout.description_enrichment import CALL_FAILURE_INVALID_RESPONSE
        from autoscout.description_enrichment import CALL_FAILURE_MODEL_UNAVAILABLE
        from autoscout.description_enrichment import CALL_FAILURE_NOT_STARTED
        from autoscout.pw_local_descriptions import CloudModelRejectedError
        from autoscout.pw_local_descriptions import LocalModelError

        def failed(reason: str) -> dict[str, Any]:
            if _on_failure is not None:
                _on_failure(reason)
            return {}

        answers: dict[str, Any] = {}
        for item in work_items:
            raw_metadata = item.get("metadata")
            metadata: Mapping[str, Any] = raw_metadata if isinstance(raw_metadata, Mapping) else {}
            request = {
                "title": str(item.get("title", "")),
                "authors": list(metadata.get("primary_authors", [])),
                "deal_logline": str(item.get("deal_logline", "")),
            }
            try:
                client = self._client_factory(
                    self._model, self._api_key, timeout=float(_timeout_seconds or CALL_TIMEOUT_SECONDS)
                )
                answer = client._chat(FALLBACK_PROMPT, request)
            except CloudModelRejectedError:
                # A bad key, no balance or an unknown model: no other book would fare better.
                return failed(CALL_FAILURE_MODEL_UNAVAILABLE)
            except LocalModelError as exc:
                unreachable = str(exc) == "pw_local_model_unavailable"
                return failed(CALL_FAILURE_NOT_STARTED if unreachable else CALL_FAILURE_INVALID_RESPONSE)
            except ValueError:
                return failed(CALL_FAILURE_NOT_STARTED)
            value = answer.get("content_description")
            if isinstance(value, str) and value.strip():
                answers[str(item.get("identity_key", ""))] = {"content_description": value}
        return answers


def fallback_enabled(config_path: Path) -> bool:
    """The fallback switch, read from the weekly config file; an unreadable config is off."""
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(config, Mapping) and config.get(FALLBACK_SWITCH) is True


def deepseek_fallback(
    config_path: Path, *, environ: Mapping[str, str] | None = None, key_file: Path | None = None
) -> DeepSeekLoglineAgent | None:
    """The fallback agent when the weekly config asks for it and a key exists, else ``None``."""
    if not fallback_enabled(config_path):
        return None
    from autoscout.pm_mailbox import load_credential
    from autoscout.pw_local_descriptions import DEEPSEEK_KEY_ENV

    api_key = load_credential(DEEPSEEK_KEY_ENV, environ, DEEPSEEK_KEY_FILE if key_file is None else key_file)
    return DeepSeekLoglineAgent(api_key) if api_key else None


class CodexPmLoglineDescriber:
    """The weekly stage with the production Codex agent the description stages use."""

    def __init__(self, agent: Any | None = None, *, fallback_agent: Any | None = None) -> None:
        if agent is None:
            from autoscout.description_enrichment import CodexDescriptionEnrichmentAgent

            agent = CodexDescriptionEnrichmentAgent(
                timeout_seconds=int(CALL_TIMEOUT_SECONDS), purpose="pm_logline_description"
            )
        self.agent = agent
        # ``None`` resolves from the weekly config and the operator's key at each run.
        self.fallback_agent = fallback_agent
        self._presentation: Callable[[dict[str, Any]], None] | None = None
        self._failure_recorder: Callable[[str, str], None] | None = None

    def set_snapshot_progress_presentation(self, presentation: Callable[[dict[str, Any]], None] | None) -> None:
        self._presentation = presentation

    def set_progress_snapshot_failure_recorder(self, recorder: Callable[[str, str], None] | None) -> None:
        self._failure_recorder = recorder

    def describe(
        self,
        config_path: Path,
        output_dir: Path,
        state_dir: Path,
        effective_date: str,
        *,
        state_store: StateStore | None = None,
    ) -> dict[str, Any]:
        from autoscout.progress_snapshot import CandidateProgressSnapshot

        tracker = CandidateProgressSnapshot(
            output_dir,
            stage=WEEKLY_STAGE_NAME,
            stage_budget_seconds=STAGE_BUDGET_SECONDS,
            failure_recorder=self._failure_recorder,
            presentation=self._presentation,
        )
        fallback = self.fallback_agent
        fallback_wanted = fallback is not None or fallback_enabled(config_path)
        if fallback is None and fallback_wanted:
            fallback = deepseek_fallback(config_path)
        result = describe_from_loglines(
            config_path,
            output_dir,
            state_dir,
            effective_date=effective_date,
            agent=self.agent,
            progress=tracker,
            fallback_agent=fallback,
            state_store=state_store,
        )
        if fallback_wanted and fallback is None and result.get("status") == "ok":
            # The switch is on and no key was found: the run had no fallback.
            result["fallback_unavailable"] = "deepseek_key_missing"
        return result
