"""Russian Content Description validation, fallback, and provenance.

This module centralises the Russian-description contract for every pipeline
stage: source-mention intake, candidate aggregation, cumulative persistence,
health reporting, and backfill.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Any

from autoscout.autobiographical_context import AUTOBIOGRAPHICAL_SUBJECT_CONTEXT_REQUIREMENT
from autoscout.autobiographical_context import autobiographical_subject_context_warning
from autoscout.autobiographical_context import has_grounded_autobiographical_subject_context
from autoscout.autobiographical_context import is_autobiographical_candidate
from autoscout.autobiographical_context import verified_context_from_artifact
from autoscout.description_depth import meets_description_depth
from autoscout.pm_publication import is_pm_evidence
from autoscout.pm_publication import is_pm_only
from autoscout.pm_publication import pm_publication_recheck_pending
from autoscout.source_eligibility import historical_record_source_eligibility_exclusion_reason

DESCRIPTION_FALLBACK = "Описание требует дополнительной проверки"
DESCRIPTION_UNCONFIRMED_STATUS = "⚠ Описание не подтверждено источниками"
DESCRIPTION_GROUNDED_STATUS = "Описание подтверждено двумя источниками"
DESCRIPTION_SINGLE_SOURCE_STATUS = "⚠ Описание по одному источнику"
DESCRIPTION_ATTEMPTED_STATUS = "Источники проверены - описание не подтверждено"
DESCRIPTION_NOT_ATTEMPTED_STATUS = "Поиск источников еще не запускался"
# A backfilled description was written from operator research and lists its
# research sources, but no stage fetched those pages and checked the text
# against them, so it never claims the grounded statuses (issue #912).
DESCRIPTION_BACKFILLED_STATUS = "Описание по исследованию, источники не проверены автоматически"
# The book is announced or offered for rights but not published, so no page
# describes it yet (issue #791). The row keeps the standard placeholder; this
# status says why, and the description stages leave the book alone until the
# re-check date.
DESCRIPTION_NOT_YET_PUBLISHED_STATUS = "Книга ещё не опубликована — описание появится позже"
# What a deal card with no description shows in place of the generic
# placeholder. A Publishers Marketplace deal is one to three years from
# publication (issue #947), so nothing needs checking: the line says the book
# is not out yet and points at the deal facts the card already carries. It is
# display text only. Candidate History and every stage keep
# ``DESCRIPTION_FALLBACK``, so the row still counts as awaiting publication.
DESCRIPTION_PM_DEAL_PENDING_TEXT = (
    "Описания пока нет: книга ещё не опубликована. Что известно о ней — в сведениях о сделке на этой карточке."
)
# The date of the verdict, written on the Candidate History record. It is
# cleared by any later verdict and by any applied description.
DESCRIPTION_NOT_YET_PUBLISHED_DATE_FIELD = "description_not_yet_published_date"
# How long a not-yet-published verdict stands before the stages search again.
DESCRIPTION_NOT_YET_PUBLISHED_RECHECK_DAYS = 14
# A description written from the PM deal logline alone (issue #950). A full
# description is not expected for a book a deal announced years before
# publication, so this one is no gap, but it restates one sentence of the deal
# announcement and is never presented as a description of the book.
DESCRIPTION_PM_LOGLINE_STATUS = "Описание по логлайну сделки PM, не полное описание книги"
DESCRIPTION_PM_LOGLINE_PROVENANCE = "pm-deal-logline"
# Mechanical depth marker (issue #672): the rendered description is safe
# Russian prose but clears neither the character nor the sentence floor.
# It says nothing about literary quality or factual completeness.
DESCRIPTION_BELOW_DEPTH_STATUS = "⚠ Краткое описание"

# The two stage outcomes that carry an accepted description, and so the only
# two an apply artifact may record. ``grounded_description`` owns the outcome
# vocabulary, but it imports this module, so the values are repeated here and
# pinned equal by test.
DESCRIPTION_STAGE_OUTCOME_VALUES = frozenset({"grounded", "single_source"})

DESCRIPTION_PROVENANCE_VALUES = {
    "codex-generated",
    "grounded",
    "fallback",
    "backfilled",
    "editor-corrected",
    "agent-adjudicated",
    "agent-unverified",
    DESCRIPTION_PM_LOGLINE_PROVENANCE,
}

# The provenances whose description replaces a logline one (issue #950): a
# description grounded on its own, by a stage or by operator research. A
# guess from the title or an unverified answer never does.
REPLACES_PM_LOGLINE_PROVENANCES = frozenset({"grounded", "agent-adjudicated", "backfilled", "editor-corrected"})

# Upper bound prevents model-overshoot and copied-source-text vectors.
DESCRIPTION_MAX_LENGTH = 2000

# Copied-text markers: publisher blurbs, jacket copy, review/article prose.
# Short bibliographic names and factual phrases are permitted — full sentences
# of copied English copy are not.
COPIED_TEXT_MARKERS = (
    "publisher weekly",
    "publishers weekly",
    "booklist",
    "library journal",
    "bookpage",
    "npr.org",
    "new york times",
    "the guardian",
    "the atlantic",
    "washington post",
    "wall street journal",
    "los angeles times",
    "san francisco chronicle",
    "usa today",
    "from the publisher",
    "about the author",
    "review copy",
    "advance praise",
    "copyright \u00a9",
    "all rights reserved",
    "published by",
    "jacket copy",
    "book description:",
    "editorial review",
    "product description",
)


def is_nonblank_string(value: Any) -> bool:
    """True when *value* is a non-empty string with meaningful content."""
    return isinstance(value, str) and value.strip() != ""


# Matches a fiction genre token in a separator-folded category, mirroring the
# audiobook-ranking exclusion scan: "science fiction" matches while "non
# fiction" and the single word "nonfiction" stay eligible.
_FICTION_GENRE_PATTERN = re.compile(r"(?<!non )\bfiction\b", re.IGNORECASE)


def normalized_genre_category(value: Any) -> str:
    """Fold a genre or candidate-category value to its canonical form.

    Separators (spaces, underscores, hyphens) collapse to single spaces and
    the value is lowercased, so "Science Fiction", "science-fiction", and
    "science_fiction" all fold to "science fiction" deterministically.
    """
    return re.sub(r"[\s_\-]+", " ", str(value)).strip().casefold()


def is_fiction_genre_category(value: Any) -> bool:
    """True when a folded candidate category names a fiction genre.

    "science fiction" and "science-fiction" match, while "adult
    nonfiction", "non-fiction", and "creative nonfiction" stay eligible.
    """
    return _FICTION_GENRE_PATTERN.search(normalized_genre_category(value)) is not None


def has_nonfallback_description(value: Any) -> bool:
    """True when *value* contains a description rather than the fallback."""
    return is_nonblank_string(value) and value.strip() != DESCRIPTION_FALLBACK


def not_yet_published_date(record: Mapping[str, Any]) -> date | None:
    """Return the date a record was found not yet published, or ``None``."""
    raw = str(record.get(DESCRIPTION_NOT_YET_PUBLISHED_DATE_FIELD, "") or "").strip()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def awaits_publication(record: Mapping[str, Any]) -> bool:
    """True when a record shows the placeholder because the book is not yet published.

    A book whose only signal is a PM rights deal is not published either
    (issue #947): it waits on the same line, outside the headline
    denominator and the per-source caveat, until another source cites it.
    """
    waiting = not_yet_published_date(record) is not None or is_pm_only(record)
    return waiting and not has_nonfallback_description(record.get("content_description", ""))


def publication_recheck_pending(record: Mapping[str, Any], as_of: date) -> bool:
    """True while a book is too far from publication to search again.

    Both description stages skip such a record, so an unpublished book
    costs no search and no agent call until the re-check date: 14 days
    after a not-yet-published verdict (issue #791), or, for a book whose
    only signal is a PM rights deal, shortly before its publication season
    (issue #947).
    """
    marked = not_yet_published_date(record)
    if marked is not None and (as_of - marked).days < DESCRIPTION_NOT_YET_PUBLISHED_RECHECK_DAYS:
        return True
    return pm_publication_recheck_pending(record, as_of)


def is_pm_logline_description(record: Mapping[str, Any]) -> bool:
    """True when a record shows a usable description written from the PM deal logline (issue #950)."""
    provenance = record.get("description_provenance")
    if not isinstance(provenance, Mapping) or provenance.get("provenance") != DESCRIPTION_PM_LOGLINE_PROVENANCE:
        return False
    return record_has_usable_description(record)


def record_has_usable_description(record: Mapping[str, Any]) -> bool:
    """True when a Candidate History record holds a usable description.

    Both description stages report a per-candidate ``final_state``, and the
    two reports are read side by side and compared candidate by candidate.
    So this predicate has one owner: a copy in each stage would agree today
    and drift later, and a drift here silently changes which candidates the
    editor is told about (issue #641).
    """
    value = record.get("content_description", "")
    if not has_nonfallback_description(value):
        return False
    validated, _reason = validate_record_description(record)
    return validated is not None


# The two verified-description provenances the review distinguishes. The
# values are repeated here because ``description_adjudication`` imports this
# module, and pinned equal by test. ``grounded`` is the deterministic
# host-grounded stage; ``agent-adjudicated`` is the repair pass over what
# that stage left at fallback (issue #644).
DESCRIPTION_HOST_GROUNDED_PROVENANCE = "grounded"
DESCRIPTION_ADJUDICATED_PROVENANCE = "agent-adjudicated"
# The agent researched and wrote the description, but no cited page passed
# the re-fetch check in code. It replaces a title-derived stub or the
# placeholder and stays marked ``DESCRIPTION_UNCONFIRMED_STATUS`` (issue
# #791).
DESCRIPTION_AGENT_UNVERIFIED_PROVENANCE = "agent-unverified"

# The shared below-floor reason code, owned by ``grounded_description``.
# Repeated here because that module imports this one, and pinned equal by
# test. The apply gate resolves a below-floor adjudicated description to
# the standard fallback under exactly this reason (issue #673).
DESCRIPTION_INSUFFICIENT_DEPTH_REASON = "insufficient_description_depth"


def description_provenance_counts(records: list[Mapping[str, Any]]) -> dict[str, int]:
    """Count usable descriptions split by verified-description provenance.

    Only records the editor actually sees a description for are counted, so
    a fallback record never contributes even when its provenance names one
    of the two provenances. Unknown records and other provenances are
    ignored, so older callers and other description kinds change nothing.
    """
    counts = {DESCRIPTION_HOST_GROUNDED_PROVENANCE: 0, DESCRIPTION_ADJUDICATED_PROVENANCE: 0}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        provenance = record.get("description_provenance")
        name = str(provenance.get("provenance", "")).strip() if isinstance(provenance, Mapping) else ""
        if name in counts and record_has_usable_description(record):
            counts[name] += 1
    return counts


def has_adjudicated_descriptions(counts: Mapping[str, Any] | None) -> bool:
    """Say whether provenance counts hold an adjudicated description.

    One owner of the gate, so persistence and presentation cannot drift
    apart on when the provenance line appears (issue #644).
    """
    if not isinstance(counts, Mapping):
        return False
    try:
        return int(counts.get(DESCRIPTION_ADJUDICATED_PROVENANCE, 0) or 0) > 0
    except (TypeError, ValueError):
        return False


def is_russian_prose(value: str) -> bool:
    """True when *value* contains meaningful Cyrillic content.

    Requires at least 3 Cyrillic characters or Cyrillic making up >= 20%
    of the non-whitespace character count.  This rejects overwhelmingly
    English text with a token Cyrillic character (e.g. "English except я")
    while permitting Latin-script names inside valid Russian sentences.
    """
    letters = [char for char in value if char.isalpha()]
    if not letters:
        return False
    cyrillic_count = sum(1 for char in letters if "\u0410" <= char <= "\u044f" or char in {"\u0451", "\u0401"})
    return cyrillic_count >= 3 and cyrillic_count / len(letters) >= 0.2


def is_too_long(value: str) -> bool:
    """True when *value* exceeds the safe upper bound."""
    return len(value) > DESCRIPTION_MAX_LENGTH


def has_copied_prose(value: str) -> bool:
    """True when *value* looks like copied publisher or review prose.

    Only full English sentences of known copied-text patterns are flagged;
    short bibliographic names, author mentions, and isolated factual
    phrases pass through.  Russian-majority text with Cyrillic characters
    and incidental bibliographic references is not copied text.
    """
    lowered = value.lower()
    has_cyrillic = is_russian_prose(value)
    sentences = [s for s in lowered.replace("\n", " ").split(". ") if len(s) > 40]
    if not sentences:
        return False
    marker_count = sum(1 for marker in COPIED_TEXT_MARKERS if marker in lowered)
    if has_cyrillic:
        # In Russian prose, require many markers to suspect copied text.
        if marker_count >= 4:
            return True
        # Check English-heavy sentences within Russian text.
        for sentence in sentences:
            words = sentence.split()
            if len(words) < 8:
                continue
            english_words = sum(1 for w in words if all("a" <= c.lower() <= "z" for c in w))
            if english_words > len(words) * 0.7 and any(
                sentence.startswith(prefix) for prefix in ('"', "'", "a ", "an ", "the ", "in ", "this ")
            ):
                return True
        return False
    # English-only text: multiple markers or blurb-like structure.
    if marker_count >= 2:
        return True
    for sentence in sentences:
        words = sentence.split()
        if len(words) < 8:
            continue
        english_words = sum(1 for w in words if all("a" <= c.lower() <= "z" for c in w))
        if english_words > len(words) * 0.5 and any(
            sentence.startswith(prefix) for prefix in ('"', "'", "a ", "an ", "the ", "in ", "this ")
        ):
            return True
    return False


# These are whole-clause templates, not forbidden keywords. A description
# containing even one other clause passes this check, so a substantive account
# can mention the title or chart position in passing. This is deliberately a
# bounded boilerplate check, not a semantic assessment of book content (#868).
_DESCRIPTION_TEMPLATE_SUBJECT = r"(?:(?:это\s+)?(?:книга|нон[- ]фикшн|издание|произведение)\s+)?"
# «в заглавии» is a synonym of «в названии», and the title itself may follow in
# quotes; quoted spans are blanked to «» before matching (issue #914).
_DESCRIPTION_TITLE_TEMPLATE = (
    r"(?:(?:о|посвящена)\s+теме,?\s+(?:обозначенной|указанной|заявленной|отражённой|отраженной)"
    r"|тема(?:\s+книги)?\s+(?:обозначена|указана|заявлена|отражена))\s+в\s+(?:названии|заглавии)(?:\s*«»)?"
)
# A clause that only promises an account of "the stated subject" names no
# subject at all (issue #914).
_DESCRIPTION_STATED_SUBJECT_TEMPLATE = (
    r"(?:автор\s+)?(?:предлагает|даёт|дает|представляет)\s+"
    r"(?:(?:научно[- ])?популярный\s+|краткий\s+|общий\s+)?(?:обзор|рассказ|разбор)\s+"
    r"(?:заявленного|указанного|обозначенного|заявленной|указанной|обозначенной)\s+(?:предмета|темы|вопроса)"
)
_DESCRIPTION_RANK_TEMPLATE = (
    r"(?:(?:занимает\s+|(?:находится\s+)?на\s+)?позици[яюи]\s*(?:№\s*)?\d+"
    r"|(?:(?:находится\s+)?на\s+|занимает\s+)?\d+(?:[-‑–]?(?:е|ое|м|ом|я|ю|й))?\s+(?:мест[ое]|позици[яюи]))"
    r"(?:\s+в\s+(?:рейтинге|чарте|списке(?:\s+бестселлеров)?)"
    r"(?:\s+[a-z0-9][a-z0-9 &'’—–-]*)?)?"
)
_DESCRIPTION_TEMPLATE_CLAUSE = re.compile(
    rf"(?:редакционный\s+хук\s*[:—–-]\s*)?{_DESCRIPTION_TEMPLATE_SUBJECT}"
    rf"(?:{_DESCRIPTION_TITLE_TEMPLATE}|{_DESCRIPTION_RANK_TEMPLATE}|{_DESCRIPTION_STATED_SUBJECT_TEMPLATE})",
    re.IGNORECASE,
)


_QUOTED_SPAN = re.compile(r"«[^«»]*»|“[^“”]*”|\"[^\"]*\"")
# A description that says outright that nothing is known about the book: the
# capture held only a title and author, so the angle or hook is not disclosed
# (issue #914). Bounded phrasings, not a semantic judge.
# The admission is scoped to the capture, its metadata, or the described book,
# so a book about a lost work "of which only the title is known" still passes
# (review of #914). A phrase like «хук не раскрыт» is not matched: a real hook
# can be an unsolved crime, and the capture admission already covers the case.
_ONLY_TITLE = r"(?:зафиксирован[ыо]?|указан[ыо]?|известн[ыо]|есть)\s+только\s+(?:название|заглавие)"
_NO_CONTENT_ADMISSION = re.compile(
    r"(?:в\s+(?:захвате|источнике|строке|данных|карточке|метаданных)|на\s+странице"
    r"|о\s+(?:этой\s+|самой\s+)?книге)"
    # Up to three plain words, no comma and no relative pronoun, so a clause
    # about another work the book discusses still passes (review of #914).
    r"(?:\s+(?!котор|чь[еия])[^\s,.;:!?—–]+){0,3}?\s+" + _ONLY_TITLE,
    re.IGNORECASE,
)


def is_contentless_description_template(value: str) -> bool:
    """True only when every nonempty clause is title/rank/stated-subject boilerplate."""
    blanked = _QUOTED_SPAN.sub("«»", value)
    clauses = [clause.strip() for clause in re.split(r"[;.!?]+", blanked) if clause.strip()]
    return bool(clauses) and all(_DESCRIPTION_TEMPLATE_CLAUSE.fullmatch(clause) for clause in clauses)


def admits_no_book_content(value: str) -> bool:
    """True when a description itself says the capture held no book content (issue #914)."""
    return _NO_CONTENT_ADMISSION.search(value) is not None


def validate_content_description(value: Any) -> tuple[str | None, str | None]:
    """Validate a Content Description value against the Russian-prose contract.

    Returns (validated_value, rejection_reason).  When the value passes
    all checks the returned string is whitespace-normalised and trimmed;
    ``rejection_reason`` is ``None``.  When the value is rejected
    ``validated_value`` is ``None`` and ``rejection_reason`` is a bounded,
    safe diagnostic that never includes copied source bodies.

    This function does not score literary quality.  Its only job is to
    separate safe Russian original prose from unsafe or contentless model output.
    """
    if not isinstance(value, str):
        return None, "non-string value rejected"
    normalized = " ".join(value.split())
    if not normalized.strip():
        return None, "empty or whitespace-only value rejected"
    if is_too_long(normalized):
        return None, f"excessive length ({len(normalized)} chars, max {DESCRIPTION_MAX_LENGTH})"
    if not is_russian_prose(normalized):
        return None, "no Cyrillic content — English-only or insufficiently Russian text rejected"
    if has_copied_prose(normalized):
        return None, "rejected as copied publisher/review prose"
    if is_contentless_description_template(normalized):
        return None, "contentless title/rank template rejected"
    if admits_no_book_content(normalized):
        return None, "description admits it has no book content"
    return normalized, None


# An English Description is machine-facing helper text: the same subject as
# the Russian Content Description, written in the same model call from the
# same evidence, for consumers that read English best (issue #727). It is
# never editor copy, so it never blocks, delays, or changes the Russian text.
ENGLISH_DESCRIPTION_FIELD = "english_description"
_ENGLISH_MIN_LATIN_SHARE = 0.8
PW_LOCAL_ENGLISH_SOURCE = "pw-local-history-backfill"


def validate_english_description(value: Any) -> tuple[str | None, str | None]:
    """Validate an English Description, the helper beside the Russian one.

    Returns ``(validated_value, rejection_reason)`` like
    ``validate_content_description``. The Russian validator's copied-prose
    heuristic treats any English sentence as suspect, so it cannot be reused
    here; the review-outlet and blurb markers still apply, and the grounded
    stage additionally checks the text against the accepted source prose.
    """
    if not isinstance(value, str):
        return None, "non-string value rejected"
    normalized = " ".join(value.split())
    if not normalized:
        return None, "empty or whitespace-only value rejected"
    if is_too_long(normalized):
        return None, f"excessive length ({len(normalized)} chars, max {DESCRIPTION_MAX_LENGTH})"
    letters = [char for char in normalized if char.isalpha()]
    latin = sum(1 for char in letters if "a" <= char.casefold() <= "z")
    if not letters or latin / len(letters) < _ENGLISH_MIN_LATIN_SHARE:
        return None, "not English prose"
    lowered = normalized.casefold()
    if sum(1 for marker in COPIED_TEXT_MARKERS if marker in lowered) >= 2:
        return None, "rejected as copied publisher/review prose"
    return normalized, None


def validate_record_description(record: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Validate a durable description, including the one-time PW English exception."""
    value = record.get("content_description", "")
    provenance = record.get("description_provenance")
    if (
        isinstance(provenance, Mapping)
        and provenance.get("language") == "en"
        and provenance.get("source") == PW_LOCAL_ENGLISH_SOURCE
        and str(provenance.get("content_description", "")).strip() == str(value).strip()
    ):
        return validate_english_description(value)
    return validate_content_description(value)


def current_english_description(record: Mapping[str, Any]) -> str:
    """Return the English Description that belongs to the current Russian text.

    The English is stored inside ``description_provenance``, which also holds
    the Russian text it was written with. A writer that replaces the Russian
    without producing English leaves a record whose provenance text differs,
    so the English reads as absent rather than stale.
    """
    provenance = record.get("description_provenance")
    if not isinstance(provenance, Mapping):
        return ""
    english = provenance.get(ENGLISH_DESCRIPTION_FIELD)
    if not isinstance(english, str) or not english.strip():
        return ""
    current = str(record.get("content_description", "")).strip()
    if not current or str(provenance.get("content_description", "")).strip() != current:
        return ""
    return english


def is_unconfirmed_codex_description(value: Any, provenance_record: Any) -> bool:
    """Return whether a displayed description needs a provenance warning.

    The warning is deliberately narrower than description validity: it applies
    only to valid, non-fallback text whose structured provenance record names
    exactly ``codex-generated``.
    """
    if not has_nonfallback_description(value):
        return False
    validated, _reason = validate_content_description(value)
    if validated is None or not isinstance(provenance_record, dict):
        return False
    return provenance_record.get("provenance") == "codex-generated"


def is_unconfirmed_description(value: Any, provenance_record: Any) -> bool:
    """Return whether a displayed description was confirmed by no source.

    Two provenances qualify: a ``codex-generated`` guess from the title, and
    an ``agent-unverified`` description whose cited pages did not pass the
    re-fetch check (issue #791). The review marks both with
    ``DESCRIPTION_UNCONFIRMED_STATUS`` and coverage counts both as unsourced.
    """
    if is_unconfirmed_codex_description(value, provenance_record):
        return True
    if not has_nonfallback_description(value) or not isinstance(provenance_record, dict):
        return False
    validated, _reason = validate_content_description(value)
    return validated is not None and provenance_record.get("provenance") == DESCRIPTION_AGENT_UNVERIFIED_PROVENANCE


def has_unconfirmed_description(record: Mapping[str, Any]) -> bool:
    """True when a record shows the editor a description no source confirmed."""
    return is_unconfirmed_description(
        record.get("content_description", ""),
        record.get("description_provenance"),
    )


def is_unsourced_stub(record: Mapping[str, Any]) -> bool:
    """True when a record shows the editor a guess from the title.

    The agentic stage selects exactly these rows every run (issue #788).
    Length does not matter: a stub that clears the depth floor is still a
    guess from the title. An ``agent-unverified`` description carries the
    same review warning but is not a stub: the agent already researched it,
    so it is not searched again every run (issue #791).
    """
    return is_unconfirmed_codex_description(
        record.get("content_description", ""),
        record.get("description_provenance"),
    )


def candidate_source_names(candidate: dict[str, Any]) -> list[str]:
    """Return the distinct sources citing one candidate, in evidence order."""
    evidence = candidate.get("source_evidence")
    if not isinstance(evidence, list):
        return []
    names: list[str] = []
    for item in evidence:
        name = str(item.get("source_name", "")).strip() if isinstance(item, dict) else ""
        if name and name not in names:
            names.append(name)
    return names


def description_coverage(
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute description coverage statistics for a set of candidates.

    Returns a dict with validated_count, fallback_count, invalid_reason_counts,
    and coverage_classification.

    Safety-validated descriptions are additionally partitioned by the
    mechanical depth floor (issue #672): ``meets_depth_count`` plus
    ``below_depth_count`` always sum to ``validated_count`` minus
    ``pm_logline_count`` (issue #950). ``validated``,
    ``fallback``, and ``coverage`` keep their exact historical semantics;
    depth is a separate verdict, never a redefinition of safety.
    ``depth_coverage`` is ``complete`` only when every candidate carries a
    safety-valid description that also clears the depth floor, ``partial``
    when any fallback or below-floor text remains, and ``unknown`` when
    there are no candidates at all. Missing depth data in a hand-built
    dict must never be read as ``complete`` — consumers default it to
    absent, not to zero debt.

    ``fallback_by_source`` names the sources whose candidates remain on the
    fallback (issue #759). One total hides a source that loses most of its
    books at the description stage: in the 2026-10-05 run ten of the fourteen
    fallbacks came from one source, and finding it took a manual join of
    three artifacts. A candidate cited by several sources counts once under
    each of them, so the per-source counts can sum to more than
    ``fallback_count``. Sources without a fallback are omitted, and the key
    is an empty list when there is none.

    ``unsourced_count`` counts the safety-valid descriptions no source
    confirmed (issue #788): the rows the review marks with
    ``DESCRIPTION_UNCONFIRMED_STATUS``. ``sourced_count`` is the rest of
    ``validated_count``, and the two always sum to it. ``validated_count``
    keeps its safety-only meaning; editor-facing totals read
    ``sourced_count``, so a guess from the title is never reported as a
    description the editor has. ``sourced_below_depth_count`` is the
    below-floor share of ``sourced_count``, so a review line that says
    "of the verified ones" counts the same population it names.

    ``not_yet_published_count`` is the share of ``fallback_count`` that
    waits for the book to be published (issue #791): nothing failed for
    those rows, so editor-facing totals report them apart from the rows
    that still need a description.

    ``coverage_denominator`` is ``total_candidates`` without those books
    (issue #944): a book that is not out yet cannot have a description, so
    it is not a gap in the headline «X из N», and ``fallback_by_source``
    leaves it out of both its counts, so a source whose books simply are not
    published yet is never named as losing its descriptions.

    ``pm_logline_count``, present only when nonzero, counts the descriptions
    written from a PM deal logline (issue #950). They are reported on their own line: such a book
    is no gap, and its text is no verified description either, so it is left
    out of ``coverage_denominator``, out of ``sourced_count``, and out of both
    counts of ``fallback_by_source``.
    """
    from autoscout.description_depth import DEPTH_POLICY_VERSION
    from autoscout.description_depth import meets_description_depth

    validated = 0
    unsourced = 0
    not_yet_published = 0
    pm_logline = 0
    sourced_below_depth = 0
    meets_depth = 0
    below_depth = 0
    fallback = 0
    invalid_reasons: dict[str, int] = {}
    fallback_ids: list[str] = []
    source_totals: dict[str, int] = {}
    source_fallbacks: dict[str, int] = {}
    source_reasons: dict[str, dict[str, int]] = {}

    def count_fallback_sources(source_names: list[str], reason: str) -> None:
        for source_name in source_names:
            source_fallbacks[source_name] = source_fallbacks.get(source_name, 0) + 1
            if reason:
                reasons = source_reasons.setdefault(source_name, {})
                reasons[reason] = reasons.get(reason, 0) + 1

    for candidate in candidates:
        description = str(candidate.get("content_description", "")).strip()
        intake_reason = str(candidate.get("content_description_rejection_reason", "")).strip()
        waiting = awaits_publication(candidate)
        from_logline = is_pm_logline_description(candidate)
        # A book that is not out yet is no source's loss (issue #944), and
        # neither is one described from its deal logline (issue #950).
        source_names = [] if waiting or from_logline else candidate_source_names(candidate)
        for source_name in source_names:
            source_totals[source_name] = source_totals.get(source_name, 0) + 1
        if not has_nonfallback_description(description):
            fallback += 1
            if waiting:
                not_yet_published += 1
            if intake_reason:
                invalid_reasons[intake_reason] = invalid_reasons.get(intake_reason, 0) + 1
            count_fallback_sources(source_names, intake_reason)
            identity_key = str(candidate.get("identity_key", ""))
            if identity_key:
                fallback_ids.append(identity_key)
            continue
        validated_result, reason = validate_record_description(candidate)
        if validated_result is not None:
            validated += 1
            candidate_unsourced = has_unconfirmed_description(candidate)
            if candidate_unsourced:
                unsourced += 1
            if from_logline:
                # One restated sentence is short by design and is no depth
                # debt: it is left out of both depth counts (issue #950).
                pm_logline += 1
            elif meets_description_depth(validated_result):
                meets_depth += 1
            else:
                below_depth += 1
                if not candidate_unsourced:
                    sourced_below_depth += 1
        else:
            fallback += 1
            reason_key = intake_reason or reason or "unknown"
            invalid_reasons[reason_key] = invalid_reasons.get(reason_key, 0) + 1
            count_fallback_sources(source_names, reason_key)
            identity_key = str(candidate.get("identity_key", ""))
            if identity_key:
                fallback_ids.append(identity_key)

    total = len(candidates)
    coverage = "complete" if fallback == 0 else "partial"
    # Stored only once one exists, so a week without a logline description
    # keeps its exact coverage bytes (issue #950); readers default it to 0.
    logline_fields = {"pm_logline_count": pm_logline} if pm_logline else {}
    if total == 0:
        depth_coverage = "unknown"
    elif fallback == 0 and below_depth == 0:
        depth_coverage = "complete"
    else:
        depth_coverage = "partial"
    return {
        "total_candidates": total,
        "validated_count": validated,
        "sourced_count": validated - unsourced - pm_logline,
        "unsourced_count": unsourced,
        "sourced_below_depth_count": sourced_below_depth,
        "fallback_count": fallback,
        "not_yet_published_count": not_yet_published,
        "coverage_denominator": total - not_yet_published - pm_logline,
        "invalid_reason_counts": invalid_reasons,
        "coverage": coverage,
        "fallback_ids": fallback_ids,
        "fallback_by_source": [
            {
                "source_name": source_name,
                "candidate_count": source_totals[source_name],
                "fallback_count": source_fallbacks[source_name],
                "reason_counts": dict(sorted(source_reasons.get(source_name, {}).items())),
            }
            # Largest loss first; the name breaks ties so the order is stable.
            for source_name in sorted(source_fallbacks, key=lambda name: (-source_fallbacks[name], name))
        ],
        "meets_depth_count": meets_depth,
        "below_depth_count": below_depth,
        "depth_policy_version": DEPTH_POLICY_VERSION,
        "depth_coverage": depth_coverage,
        **logline_fields,
    }


def coverage_denominator(coverage: Mapping[str, Any]) -> int:
    """Return the N of «X из N»: the candidates a description is expected for.

    Books awaiting publication are reported on their own line, never as a
    gap (issue #944). Coverage written before ``coverage_denominator``
    existed is read as ``total_candidates`` minus ``not_yet_published_count``
    and ``pm_logline_count`` (issue #950), and coverage written before #791 as
    its plain total, so every surface states the same N for the same artifact.
    """

    def count(key: str) -> int | None:
        try:
            return int(coverage[key] or 0) if key in coverage else None
        except (TypeError, ValueError):
            return None

    explicit = count("coverage_denominator")
    if explicit is not None:
        return max(0, explicit)
    total = count("total_candidates")
    if total is None:
        total = (count("validated_count") or 0) + (count("fallback_count") or 0)
    return max(0, total - (count("not_yet_published_count") or 0) - (count("pm_logline_count") or 0))


def _coverage_count(coverage: Mapping[str, Any], key: str) -> int:
    try:
        return max(0, int(coverage.get(key, 0) or 0))
    except (TypeError, ValueError):
        return 0


def pm_logline_count(coverage: Mapping[str, Any]) -> int:
    """The descriptions written from a PM deal logline; 0 for coverage written before #950."""
    return _coverage_count(coverage, "pm_logline_count")


def verified_description_count(coverage: Mapping[str, Any]) -> int:
    """Return the X of «X из N»: descriptions the editor has from checked sources.

    Unsourced stubs (issue #788) and PM logline descriptions (issue #950)
    each have their own line, so neither counts here.
    """
    validated = _coverage_count(coverage, "validated_count")
    return max(0, validated - _coverage_count(coverage, "unsourced_count") - pm_logline_count(coverage))


# One retained evidence URL is one line of provenance, so the retained set
# stays small and each URL stays bounded.
MAX_RETAINED_EVIDENCE_URLS = 10
MAX_RETAINED_EVIDENCE_URL_LENGTH = 500


def _paired_evidence_basis(raw_sources: Any, raw_digests: Any) -> tuple[list[str], list[str] | None]:
    """Return the retained evidence URLs, with their digests when they pair.

    ``evidence_digests`` is index-aligned with ``evidence_basis``: digest
    ``i`` belongs to URL ``i``, and the evidence-unchanged gate re-fetches
    the pair. The two lists are therefore bounded and filtered together, so
    they can never fall out of step and leave the gate comparing one page
    against another page's digest. Digests are kept only when the artifact
    supplies exactly one nonblank digest per nonblank URL; anything else
    returns ``None``, which writes the record a caller that sent no digests
    at all would write (issue #639).
    """
    sources = raw_sources if isinstance(raw_sources, list) else []
    digests = raw_digests if isinstance(raw_digests, list) else []
    bounded_sources = [str(source).strip()[:MAX_RETAINED_EVIDENCE_URL_LENGTH] for source in sources]
    bounded_digests = [str(digest).strip() for digest in digests]
    evidence_basis = [source for source in bounded_sources[:MAX_RETAINED_EVIDENCE_URLS] if source]
    if len(bounded_digests) != len(bounded_sources) or not all(bounded_sources) or not all(bounded_digests):
        return evidence_basis, None
    return evidence_basis, bounded_digests[:MAX_RETAINED_EVIDENCE_URLS]


def description_provenance_record(
    identity_key: str,
    description: str,
    provenance: str,
    evidence_basis: list[str] | None = None,
    applied_date: str | None = None,
    grounding_evidence_url: str | None = None,
    candidate_id: str | None = None,
    owner_count: int | None = None,
    confidence: str | None = None,
    evidence_digests: list[str] | None = None,
    identity_note_present: bool = False,
    english_description: str | None = None,
) -> dict[str, Any]:
    """Build a provenance record for a Content Description update.

    Provenance distinguishes codex-generated, fallback, backfilled, and
    editor-corrected descriptions.  Evidence-basis identifiers are bounded
    references - never copied prose.  ``grounding_evidence_url`` names the
    exact supporting Evidence URL a grounded description was built from.

    ``candidate_id``, ``owner_count``, and ``confidence`` are recorded when
    the caller knows them.  An agent-adjudicated description carries all
    three, so the applied record itself names the exact Candidate ID, how
    many independent owners agreed, and how sure the adapter was (issue
    #637).  A caller that does not supply them writes the same record as
    before, so no existing artifact changes.

    ``evidence_digests`` holds one content digest per ``evidence_basis``
    URL, in the same order.  The adjudication stage retains it so the next
    run can report ``evidence_unchanged`` without calling the agent again
    (issue #639).  Callers that do not supply it write no such key, so no
    existing artifact changes.

    ``identity_note_present`` records that a counting citation carried an
    adapter identity note, without retaining the note itself.  The note is
    adapter free text in English, so it stays in the preview and never
    becomes editor copy; the review renders one closed Russian sentence
    from this flag (issues #637 and #644).  A note on a citation the
    verification dropped is not evidence the description rests on, so only
    the plan step sets this flag, and only from counting citations.  The
    default writes no such key, so no existing artifact changes.

    ``english_description`` is the validated English Description written in
    the same call as ``description`` (issue #727). It lives here, beside the
    Russian text it matches, so every writer that replaces this record also
    drops English that no longer matches. Callers validate it first; the
    default writes no such key.
    """
    record: dict[str, Any] = {
        "identity_key": identity_key,
        "content_description": description,
        "provenance": provenance if provenance in DESCRIPTION_PROVENANCE_VALUES else "backfilled",
        "applied_date": applied_date or date.today().isoformat(),
        "evidence_basis": [str(ref).strip() for ref in (evidence_basis or []) if ref],
    }
    if grounding_evidence_url:
        record["grounding_evidence_url"] = str(grounding_evidence_url).strip()
    if candidate_id:
        record["candidate_id"] = str(candidate_id).strip()[:200]
    if owner_count is not None:
        record["owner_count"] = max(0, int(owner_count))
    if confidence:
        record["confidence"] = str(confidence).strip()[:20]
    if evidence_digests is not None:
        record["evidence_digests"] = [str(digest).strip() for digest in evidence_digests if str(digest).strip()]
    if identity_note_present:
        record["identity_note_present"] = True
    if english_description:
        record[ENGLISH_DESCRIPTION_FIELD] = english_description
    return record


# Safe metadata fields that can be included in backfill work items.
# Excludes raw HTML, article/review bodies, publisher blurbs, etc.
BACKFILL_SAFE_FIELDS = (
    "title",
    "primary_authors",
    "publisher",
    "publication_year",
    "candidate_category",
    "short_description",
    "autobiographical_subject_context",
    "source_evidence",
    "signal_summary",
    # A search key only (issue #788): an ISBN query resolves a non-English
    # title straight to its bookshop and publisher pages. It is never
    # evidence; verification still checks every cited page in code.
    "isbn",
)

# Evidence-reference fields from source_evidence that are safe.
BACKFILL_SAFE_EVIDENCE_FIELDS = (
    "source_name",
    "source_family",
    "source_url",
    "evidence_url",
    "evidence_locator",
    "evidence_note",
    "autobiographical_subject_context",
)

# Shared search-hint bounds (issue #684): hints are URL-only starting
# points for the adjudication agent, never evidence. The count mirrors the
# grounded stage's per-candidate fetch budget; the length matches the shared
# URL bound.
MAX_SEARCH_HINTS = 3
MAX_SEARCH_HINT_URL_LENGTH = 500


def _bounded_search_hints(
    value: Any,
    *,
    max_hints: int = MAX_SEARCH_HINTS,
    max_url_length: int = MAX_SEARCH_HINT_URL_LENGTH,
) -> list[str]:
    """Bound one raw search-hint list to URL-only starting points."""
    if not isinstance(value, list | tuple):
        return []
    try:
        limit = max(0, int(max_hints))
    except (TypeError, ValueError):
        return []
    try:
        url_limit = max(0, int(max_url_length))
    except (TypeError, ValueError):
        return []
    hints: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            continue
        url = raw.strip()
        if not url or url in seen:
            continue
        lowered = url.casefold()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            continue
        if len(url) > url_limit:
            continue
        seen.add(url)
        hints.append(url)
        if len(hints) >= limit:
            break
    return hints


def _is_repair_candidate_record(record: dict[str, Any]) -> bool:
    """True when a history record qualifies for a scoped repair replacement.

    Local import: ``description_repair`` owns the policy but imports this
    module, so the dependency runs one way at load time and is resolved
    here at call time.
    """
    from autoscout.description_repair import is_repair_candidate

    return is_repair_candidate(record)


def _prov_string(record: dict[str, Any]) -> str:
    """Extract provenance string from either a structured record or plain string."""
    prov = record.get("description_provenance", "")
    if isinstance(prov, dict):
        return str(prov.get("provenance", "")).strip()
    return str(prov or "").strip()


def prepare_backfill_work_items(
    candidate_history: list[dict[str, Any]],
    visibility_records: list[dict[str, Any]] | None = None,
    *,
    include_depth_repair: bool = False,
    include_unsourced_stubs: bool = False,
    repair_as_of: Any = None,
    search_hints: Mapping[str, Any] | None = None,
    include_awaiting_publication: bool = False,
) -> list[dict[str, Any]]:
    """Prepare metadata-only work items for description quality gaps.

    Visible candidates are selected when descriptions are blank/fallback or
    when an autobiographical opening still lacks verified subject context.
    Editor-corrected descriptions are skipped.
    Hidden candidates (via visibility state) are excluded.

    With ``include_depth_repair``, thin-but-usable descriptions of the
    approved legacy provenances are also selected under the shared repair
    policy (due on ``repair_as_of``, default today): the residual
    adjudication route repairs them, while the legacy enrichment default
    keeps repairing nothing but missing text. Repair items carry
    ``depth_repair=True`` alongside ``replaces_existing_description``.

    With ``include_unsourced_stubs``, every visible unsourced stub is
    selected too, every run, whatever its length (issue #788). A stub is
    a guess from the title, so the repair policy does not schedule it
    here: no cooldown and no exhaustion stands between it and the agentic
    search. Stub items carry ``unsourced_stub=True``; a thin stub also
    carries ``depth_repair=True`` so it is counted as a depth repair.

    A record found not yet published is left out until its re-check date
    (issue #791), measured from ``repair_as_of``. ``include_awaiting_publication``
    selects it anyway, for an operator who names the record explicitly.

    ``search_hints`` maps an identity key to URL-only starting points from
    the grounded stage (issue #684). Hints are attached bounded to the work
    item and are never evidence: verification still re-fetches and checks
    every cited page in code. ``None`` (the default) attaches nothing, so
    existing callers and previews stay byte-identical.

    Work items carry exact Candidate IDs, safe structured metadata, and
    bounded Source Evidence identifiers. Raw HTML, article/review bodies,
    publisher blurbs, jacket copy, and book samples are excluded.
    """
    from datetime import date as _date

    from autoscout.description_repair import is_repair_candidate
    from autoscout.description_repair import pending_repair_handoff
    from autoscout.description_repair import repair_due

    if repair_as_of is None:
        repair_day = _date.today()
    elif isinstance(repair_as_of, _date):
        repair_day = repair_as_of
    else:
        try:
            repair_day = _date.fromisoformat(str(repair_as_of).strip())
        except ValueError:
            repair_day = _date.today()
    hidden_ids: set[str] = {
        str(record.get("identity_key", "")).strip()
        for record in (visibility_records or [])
        if isinstance(record, dict) and str(record.get("visibility", "")).strip().lower() == "hidden"
    }
    items: list[dict[str, Any]] = []
    for record in candidate_history:
        identity_key = str(record.get("identity_key", "")).strip()
        if not identity_key:
            continue
        if identity_key in hidden_ids:
            continue
        provenance = _prov_string(record)
        if provenance == "editor-corrected":
            continue
        if not include_awaiting_publication and publication_recheck_pending(record, repair_day):
            continue
        # A record the source eligibility rules exclude (a fixture, non-book
        # material) never reaches the review, so no stage describes it (#915).
        if historical_record_source_eligibility_exclusion_reason(record):
            continue
        description = str(record.get("content_description", "")).strip()
        has_description = has_nonfallback_description(description)
        context_warning = autobiographical_subject_context_warning(record)
        unsourced_stub = bool(include_unsourced_stubs and is_unsourced_stub(record))
        # A logline description waits for a description grounded on its own
        # (issue #950); the pending check above holds it until the book's
        # publication season or another source cites it.
        logline_replacement = bool(include_unsourced_stubs and is_pm_logline_description(record))
        depth_repair = bool(
            include_depth_repair
            and has_description
            and is_repair_candidate(record)
            and (unsourced_stub or repair_due(record, repair_day) or pending_repair_handoff(record, repair_day))
        )
        if has_description and (not context_warning or not has_grounded_autobiographical_subject_context(record)):
            if not depth_repair and not unsourced_stub and not logline_replacement:
                continue
        # Build a safe metadata-only work item.
        safe_metadata: dict[str, Any] = {}
        for field in BACKFILL_SAFE_FIELDS:
            value = record.get(field)
            if value:
                safe_metadata[field] = value
        # Include only safe evidence-reference fields.
        source_evidence = record.get("source_evidence", [])
        if isinstance(source_evidence, list):
            safe_evidence = []
            for ev in source_evidence:
                # A PM deal page describes a deal, not the book, so the agent
                # is never offered it as a place to find a description
                # (issue #947).
                if isinstance(ev, dict) and not is_pm_evidence(ev):
                    safe_ev = {field: ev[field] for field in BACKFILL_SAFE_EVIDENCE_FIELDS if ev.get(field)}
                    if safe_ev:
                        safe_evidence.append(safe_ev)
            safe_metadata["source_evidence"] = safe_evidence
        item: dict[str, Any] = {
            "identity_key": identity_key,
            "title": str(record.get("title", "")),
            "metadata": safe_metadata,
        }
        if search_hints is not None:
            bounded_hints = _bounded_search_hints(search_hints.get(identity_key, []))
            if bounded_hints:
                item["search_hints"] = bounded_hints
        if is_autobiographical_candidate(record):
            item["description_requirements"] = [AUTOBIOGRAPHICAL_SUBJECT_CONTEXT_REQUIREMENT]
        if has_description:
            item["replaces_existing_description"] = True
        if depth_repair:
            item["depth_repair"] = True
        if unsourced_stub:
            item["unsourced_stub"] = True
        items.append(item)
    return items


def apply_description_artifacts(
    candidate_history: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    *,
    provenance: str = "backfilled",
    effective_date: str | None = None,
    allow_overwrite_valid: bool = False,
    allow_repair_replacement: bool = False,
    allow_unsourced_replacement: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply validated description artifacts through one shared policy gate.

    Each artifact is handled independently. Unknown identities are counted
    without mutating history, and replaying an already-applied artifact is
    idempotent.

    ``allow_repair_replacement`` permits overwriting a usable description
    only when the record itself is a depth-repair candidate: thin usable
    text of an approved legacy provenance (issue #675). It never enables
    general valid-description overwrites — every other valid record is
    still skipped — and editor-corrected records stay protected above.

    ``allow_unsourced_replacement`` permits overwriting a description no
    source confirmed, whatever its length (issues #788 and #791). Such text
    has no source to protect, and a rejected artifact still leaves it in
    place: only a validated replacement is ever written over it.

    An ``agent-unverified`` artifact is itself unconfirmed, so it only fills
    a missing description or replaces a title-derived stub. It never
    overwrites any other usable description, whatever the flags (issue
    #791).

    Missing autobiographical context is reviewable metadata debt, not a reason
    to destroy an otherwise valid Russian description. A generated context is
    accepted only when the exact structured value was already verified into
    Source Evidence.
    """
    history_by_identity: dict[str, dict[str, Any]] = {
        str(record.get("identity_key", "")).strip(): dict(record) for record in candidate_history
    }
    applied = 0
    skipped = 0
    invalid = 0
    fallback = 0
    unknown = 0

    for artifact in artifacts:
        identity_key = str(artifact.get("identity_key", "")).strip()
        if not identity_key:
            invalid += 1
            continue
        record = history_by_identity.get(identity_key)
        if record is None:
            unknown += 1
            continue

        # Skip editor-corrected — authoritative.
        existing_prov = _prov_string(record)
        if existing_prov == "editor-corrected":
            skipped += 1
            continue

        existing_desc = str(record.get("content_description", "")).strip()
        existing_valid = has_nonfallback_description(existing_desc)
        repairs_autobiographical_context = bool(existing_valid and autobiographical_subject_context_warning(record))
        repairs_thin_description = bool(
            allow_repair_replacement and existing_valid and _is_repair_candidate_record(record)
        )
        replaces_unsourced_stub = bool(allow_unsourced_replacement and has_unconfirmed_description(record))
        # A description grounded on its own replaces a logline one (issue #950).
        replaces_pm_logline = bool(
            existing_valid and provenance in REPLACES_PM_LOGLINE_PROVENANCES and is_pm_logline_description(record)
        )
        if provenance == DESCRIPTION_AGENT_UNVERIFIED_PROVENANCE and existing_valid and not is_unsourced_stub(record):
            skipped += 1
            continue
        if (
            existing_valid
            and not allow_overwrite_valid
            and not repairs_autobiographical_context
            and not repairs_thin_description
            and not replaces_unsourced_stub
            and not replaces_pm_logline
        ):
            skipped += 1
            continue

        # Retained evidence digests travel with the artifact only from the
        # adjudication stage (issue #639), and each one belongs to the
        # evidence URL at its own index, so both lists are derived together.
        evidence_basis, evidence_digests = _paired_evidence_basis(
            artifact.get("research_sources", artifact.get("evidence_basis", [])),
            artifact.get("evidence_digests"),
        )
        # A fallback record gains an evidence basis only where digests came
        # with it, so every other caller writes the same fallback record as
        # before: no digests means no digest key and no basis.
        fallback_basis = evidence_basis if evidence_digests is not None else []

        # Validate the new description.
        new_description = artifact.get("content_description", "")
        validated_desc, _ = validate_content_description(new_description)
        if validated_desc is not None and provenance in (
            DESCRIPTION_ADJUDICATED_PROVENANCE,
            DESCRIPTION_AGENT_UNVERIFIED_PROVENANCE,
        ):
            # Depth acceptance backstop (issue #673): no replay, service, or
            # direct application path may persist a below-floor adjudicated
            # description, even when the planning step that normally rejects
            # it is bypassed. The artifact falls through to the standard
            # fallback below under the shared depth reason. Other
            # provenances are untouched here: legacy repair eligibility is
            # issue #674's scope, not this gate's.
            if not meets_description_depth(validated_desc):
                validated_desc = None
                artifact = {**artifact, "rejection_reason": DESCRIPTION_INSUFFICIENT_DEPTH_REASON}
        if validated_desc is None:
            if existing_valid:
                skipped += 1
                continue
            # Rejected — resolve to fallback.
            record["content_description"] = DESCRIPTION_FALLBACK
            record["description_provenance"] = description_provenance_record(
                identity_key,
                DESCRIPTION_FALLBACK,
                "fallback",
                evidence_basis=fallback_basis,
                applied_date=effective_date,
                evidence_digests=evidence_digests,
            )
            # Persist a bounded explicit reason with the standard fallback so
            # review and description coverage can surface it. Rejected source
            # prose never reaches state - only the bounded reason token.
            rejection_reason = str(artifact.get("rejection_reason", "")).strip()
            if rejection_reason:
                record["content_description_rejection_reason"] = rejection_reason[:200]
            fallback += 1
            continue

        grounding_evidence_url = str(artifact.get("grounding_evidence_url", "")).strip()
        subject_context = verified_context_from_artifact(
            record,
            artifact.get("autobiographical_subject_context"),
        )
        proposed_warning = autobiographical_subject_context_warning(
            record,
            content_description=validated_desc,
            subject_context=subject_context,
        )
        # A context-only repair that brings no context changes nothing worth
        # writing. Replacing an unsourced stub or a PM logline description is
        # a different repair: the verified text is worth having even while
        # the context is still missing, and the review keeps its
        # missing-context warning (issues #788 and #950).
        if (
            not allow_overwrite_valid
            and repairs_autobiographical_context
            and proposed_warning
            and not replaces_unsourced_stub
            and not replaces_pm_logline
        ):
            skipped += 1
            continue

        existing_context = record.get("autobiographical_subject_context")
        context_changed = subject_context is not None and existing_context != subject_context
        if record.get("content_description") == validated_desc and not context_changed and existing_prov == provenance:
            skipped += 1
            continue

        record["content_description"] = validated_desc
        if subject_context:
            record["autobiographical_subject_context"] = subject_context
        # An invalid English Description is dropped; the Russian text above
        # is applied exactly as it would be without one (issue #727).
        english_description, _english_reason = validate_english_description(artifact.get(ENGLISH_DESCRIPTION_FIELD, ""))
        artifact_owner_count = artifact.get("owner_count")
        record["description_provenance"] = description_provenance_record(
            identity_key,
            validated_desc,
            provenance,
            evidence_basis=evidence_basis,
            applied_date=effective_date,
            grounding_evidence_url=grounding_evidence_url,
            candidate_id=str(artifact.get("candidate_id", "")).strip() or None,
            owner_count=artifact_owner_count if isinstance(artifact_owner_count, int) else None,
            confidence=str(artifact.get("confidence", "")).strip() or None,
            evidence_digests=evidence_digests,
            identity_note_present=bool(artifact.get("identity_note_present", False)),
            english_description=english_description,
        )
        # The editor must see a one-owner description as a one-owner
        # description. The review derives that mark from the stage outcome,
        # so an artifact that knows its own outcome records it here rather
        # than leaving the row indistinguishable from a two-source row
        # (issue #637).
        stage_outcome = str(artifact.get("stage_outcome", "")).strip()
        if stage_outcome in DESCRIPTION_STAGE_OUTCOME_VALUES:
            record["grounded_outcome"] = stage_outcome
        elif provenance == DESCRIPTION_AGENT_UNVERIFIED_PROVENANCE:
            # No source confirmed this text, so an outcome an earlier
            # description earned must not mark it as verified (issue #791).
            record.pop("grounded_outcome", None)
        # A valid accepted description clears any stale rejection reason.
        record.pop("content_description_rejection_reason", None)
        # A book that has a description is no longer waiting to be published.
        record.pop(DESCRIPTION_NOT_YET_PUBLISHED_DATE_FIELD, None)
        # Authoritative genre/work-type metadata travels with an accepted
        # grounded description. The authoritative genre replaces the retailer
        # category, folded deterministically; the canonical exclusion rule
        # recognizes fiction genres in the folded form. The free-form subject
        # statement is evidence for the acceptance gate only and is never
        # persisted.
        authoritative_work_type = str(artifact.get("authoritative_work_type", "")).strip()
        if authoritative_work_type:
            record["authoritative_work_type"] = authoritative_work_type[:50]
        authoritative_genre = str(artifact.get("authoritative_genre", "")).strip()
        if authoritative_genre:
            record["authoritative_genre"] = authoritative_genre[:100]
            record["candidate_category"] = normalized_genre_category(authoritative_genre)
        applied += 1

    results = {
        "applied": applied,
        "skipped": skipped,
        "invalid": invalid,
        "fallback": fallback,
        "unknown_identity": unknown,
    }
    return list(history_by_identity.values()), results
