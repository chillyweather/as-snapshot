"""Russian presentation vocabulary for editor-facing weekly reviews.

Canonical values remain unchanged in state and CSV artifacts; this module is
only used at the Markdown/HTML presentation boundary.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from collections.abc import Mapping
from typing import Any

from autoscout.content_description import DESCRIPTION_ADJUDICATED_PROVENANCE
from autoscout.content_description import DESCRIPTION_AGENT_UNVERIFIED_PROVENANCE
from autoscout.content_description import DESCRIPTION_HOST_GROUNDED_PROVENANCE
from autoscout.scouting_candidates import INELIGIBLE_CANDIDATE_CATEGORIES
from autoscout.title_evidence import format_editor_english_title
from autoscout.visible_risk_coverage import FAILED
from autoscout.visible_risk_coverage import NOT_RUN
from autoscout.visible_risk_coverage import PARTIAL
from autoscout.visible_risk_coverage import SCREENED
from autoscout.visible_risk_coverage import VISIBLE_RISK_SCREEN_COVERAGE_FIELD
from autoscout.visible_risk_coverage import VISIBLE_RISK_SCREEN_COVERAGES
from autoscout.visible_risk_coverage import recorded_coverage

LIFECYCLE_LABELS = {
    "new": "Новые",
    "upgraded": "Обновлённые",
    "changed": "Обновлённые",
    "unchanged": "Ранее просмотренные",
    "previously_seen": "Ранее просмотренные",
    "New": "Новые",
    "Updated": "Обновлённые",
    "Previously seen": "Ранее просмотренные",
}

EDITOR_STATUS_LABELS = {
    "interested": "Интересно",
    "maybe_watch": "Наблюдать",
    "reject": "Не подходит",
    "already_known": "Уже известно",
    "needs_deeper_analysis": "Нужен углублённый анализ",
}

RUSSIAN_STATUS_LABELS = {
    "found in Russian": "Есть русское издание",
    "not found in Russian": "Русское издание не найдено",
    "needs manual check": "Нужна ручная проверка",
    "unclear": "Статус русского издания неясен",
    "unknown": "Неизвестно",
}

EXCLUSION_CATEGORY_LABELS = {
    "fiction": "художественная литература",
    "children": "детская литература",
    "children's": "детская литература",
    "childrens": "детская литература",
    "ya": "литература для подростков и молодёжи",
    "young adult": "литература для подростков и молодёжи",
    "topic-only": "только тематическое упоминание, не книга",
    "topic only": "только тематическое упоминание, не книга",
    "narrow manual": "узкоспециализированное практическое руководство",
    "manual": "практическое руководство",
    "narrow academic": "узкоспециализированное академическое издание",
    "audiobook-only non-book material": "аудиоматериал без отдельного книжного издания",
    "audio-only non-book material": "аудиоматериал без отдельного книжного издания",
}

RISK_STATUS_LABELS = {
    "clear visible risk": "Есть заметный риск",
    "text check required": "Нужно проверить текст",
    "no visible risk": "Заметных рисков нет",
    "unknown": "Неизвестно",
}

RISK_CATEGORY_LABELS = {
    "Russia/politics": "Россия и современная политика",
    "war/anti-war/Russian Armed Forces": "война, антивоенная тематика и российские вооружённые силы",
    "WWII/USSR/Nazi comparison": "Вторая мировая война, СССР и сравнения с нацистской Германией",
    "non-traditional relationships/gender transition/rejection of childbearing": (
        "нетрадиционные отношения, гендерный переход и отказ от деторождения"
    ),
    "drugs/microdosing/psychoactive substances": "наркотики, микродозинг и психоактивные вещества",
}

RISK_SCOPE_EXPLANATIONS = {
    "book-content": (
        "Публичные данные указывают на содержание самой книги; требуется редакционная или юридическая проверка."
    ),
    "author-context": (
        "Сигнал относится к автору или публичному контексту, а не подтверждённому содержанию книги; "
        "необходимо проверить текст и источники."
    ),
}

# Which sources a confirmed description rests on, in the editor's words.
# The status beside it answers how many owners agreed; this answers what
# those owners were. Only verified descriptions are labelled: any other
# provenance renders as blank, so no implementation term and no wording
# invented for a case outside this contract ever reaches the editor
# (issue #644). The exact words are the editor's call; the rule that no
# machinery name reaches them is not.
DESCRIPTION_PROVENANCE_LABELS = {
    DESCRIPTION_HOST_GROUNDED_PROVENANCE: "Источники из проверенного списка",
    DESCRIPTION_ADJUDICATED_PROVENANCE: "Источники проверены по тексту страниц",
    DESCRIPTION_AGENT_UNVERIFIED_PROVENANCE: (
        "Составлено агентом по найденным страницам; автоматическая проверка страниц не прошла"
    ),
}

# One closed sentence for a retained identity note. The note itself is
# adapter free text in English and never becomes editor copy; this flag
# only says that a cited page raised a question about which work it
# describes (issues #637 and #644).
DESCRIPTION_IDENTITY_NOTE_TEXT = "Одна из цитируемых страниц может описывать другую книгу."


def description_provenance_label(value: Any) -> str:
    """Return the Russian provenance line for a description, or blank.

    Accepts the structured provenance record or the bare provenance name.
    Unknown and unverified provenances render as blank rather than leaking
    an internal value into editor output.
    """
    name = value.get("provenance", "") if isinstance(value, Mapping) else value
    return DESCRIPTION_PROVENANCE_LABELS.get(str(name or "").strip(), "")


def localized(mapping: dict[str, str], value: Any, fallback: str) -> str:
    """Translate a canonical status without exposing unknown internal values."""
    text = str(value or "").strip()
    if not text:
        return ""
    return mapping.get(text, fallback)


def lifecycle_label(value: Any) -> str:
    return localized(LIFECYCLE_LABELS, value, "Неизвестный статус обзора") or "Ранее просмотренные"


def editor_status_label(value: Any) -> str:
    return localized(EDITOR_STATUS_LABELS, value, "Неизвестное решение редактора")


def russian_status_label(value: Any) -> str:
    return localized(RUSSIAN_STATUS_LABELS, value, "Неизвестный статус русского издания")


def risk_status_label(value: Any) -> str:
    return localized(RISK_STATUS_LABELS, value, "Неизвестный статус риска")


def risk_category_label(value: Any) -> str:
    return localized(RISK_CATEGORY_LABELS, value, "Другая категория риска")


def risk_scope_explanation(value: Any) -> str:
    return localized(RISK_SCOPE_EXPLANATIONS, value, "")


# Visible Risk Screen Coverage in the editor's words (issue #714). The JSON
# record stays English; only the review presentation is Russian (ADR 0018).
VISIBLE_RISK_COVERAGE_UNRECORDED_TEXT = "Проверка риска не записана"


def visible_risk_coverage_review_text(value: Any) -> str:
    """Say whether and how completely the screen ran; never blank."""
    coverage = recorded_coverage(value)
    if not coverage:
        return VISIBLE_RISK_COVERAGE_UNRECORDED_TEXT
    if coverage == SCREENED:
        return "Проверено по всем категориям"
    if coverage == PARTIAL:
        fields = value["evidence_fields"]
        if not fields:
            return "Проверено частично: нет публичных данных о книге"
        if "title" not in fields:
            return "Проверено частично: нет названия"
        if "evidence_notes" in fields:
            return "Проверено частично: нет описания, есть только название и заметки об источнике"
        return "Проверено частично: есть только название, нет описания и заметок об источнике"
    if coverage == NOT_RUN:
        return "Проверка не выполнялась: статус риска указан источником"
    return "Проверка завершилась с ошибкой"


def visible_risk_coverage_breakdown(rows: Any) -> dict[str, int]:
    """Count review rows per coverage value, plus rows without a record."""
    counts = dict.fromkeys((*VISIBLE_RISK_SCREEN_COVERAGES, "unrecorded"), 0)
    for row in rows:
        counts[recorded_coverage(row.get(VISIBLE_RISK_SCREEN_COVERAGE_FIELD)) or "unrecorded"] += 1
    return counts


def visible_risk_coverage_summary_text(value: Any) -> str:
    """Render the review-wide coverage counts after their label, or blank when there are no rows."""
    if not isinstance(value, Mapping) or not any(int(count or 0) for count in value.values()):
        return ""

    def count(key: str) -> int:
        return int(value.get(key, 0) or 0)

    unrecorded = f", без записи — {count('unrecorded')}" if count("unrecorded") else ""
    return (
        f"проверено по всем категориям — {count(SCREENED)}, "
        f"частично — {count(PARTIAL)}, "
        f"не выполнялась — {count(NOT_RUN)}, "
        f"с ошибкой — {count(FAILED)}{unrecorded}."
    )


def exclusion_category_label(value: Any) -> str:
    return localized(EXCLUSION_CATEGORY_LABELS, value, "другая неподходящая категория")


def russian_provider_reason_label(provider: dict[str, Any]) -> str:
    """Map bounded provider reasons to safe Russian operator text."""
    status = str(provider.get("outcome") or provider.get("status", "not_run")).strip().lower()
    reason = str(provider.get("reason", "")).strip().lower()
    if "empty fixture" in reason:
        return "пустая фикстура, проверка не запускалась"
    if "candidate" in reason and any(marker in reason for marker in ("not run", "not executed", "scope", "scoped")):
        return "кандидат не входит в область результата провайдера"
    if "timeout" in reason or "timed out" in reason:
        return "тайм-аут при обращении к провайдеру"
    if "captcha" in reason or "access denied" in reason or "rate-limit" in reason:
        return "доступ к провайдеру заблокирован"
    if "blocked" in reason or status == "blocked":
        return "доступ к провайдеру заблокирован"
    if "not implemented" in reason or status == "unimplemented":
        return "адаптер провайдера не реализован"
    if any(marker in reason for marker in ("parse", "parsed", "markup", "malformed", "json")):
        return "ошибка разбора ответа провайдера"
    return {
        "not_run": "проверка не запускалась",
        "blocked": "доступ к провайдеру заблокирован",
        "failed": "проверка завершилась с ошибкой",
        "unimplemented": "адаптер провайдера не реализован",
        "ambiguous": "неоднозначная идентичность",
        "no_match": "совпадение не найдено",
        "matched": "русское издание подтверждено",
    }.get(status, "результат проверки сохранён")


def russian_verification_review_text(value: Any) -> str:
    """Render verification with safe Russian operator reasons."""
    if not isinstance(value, dict) or not value:
        return ""
    coverage_labels = {
        "complete": "полностью",
        "partial": "частично",
        "not_run": "не запускалась",
        "failed": "с ошибкой",
    }
    outcome_labels = {
        "matched": "подтверждено русское издание",
        "no_match": "совпадение не найдено",
        "ambiguous": "неоднозначный результат",
        "blocked": "доступ заблокирован",
        "failed": "проверка завершилась с ошибкой",
        "unimplemented": "провайдер не реализован",
    }
    coverage_value = str(value.get("check_coverage", "")).strip()
    coverage = coverage_labels.get(coverage_value, "неизвестное покрытие")
    aggregate_outcome = value.get("provider_outcome")
    if aggregate_outcome is None and coverage_value == "not_run":
        outcome = "проверка не запускалась"
    else:
        outcome = outcome_labels.get(str(aggregate_outcome).strip(), "результат не определён")
    raw_providers = value.get("providers", [])
    providers: list[str] = []
    if isinstance(raw_providers, list):
        providers = [
            f"{str(provider.get('name', 'провайдер')).strip() or 'провайдер'}: {russian_provider_reason_label(provider)}"
            for provider in raw_providers
            if isinstance(provider, dict)
        ]
    if not providers and "no recognized" in str(value.get("reason", "")).lower():
        providers.append("признанные провайдеры не настроены")
    return f"{coverage}; {outcome}" + ("; " + "; ".join(providers) if providers else "")


def russian_verification_summary_text(value: Any) -> str:
    """Render the Russian verification breakdown for review summaries."""
    if not isinstance(value, dict):
        return ""

    def count(key: str) -> int:
        return int(value.get(key, 0) or 0)

    keys = (
        "verified_russian_edition",
        "verified_complete_no_match",
        "ambiguous",
        "partial",
        "not_run",
        "blocked",
        "failed",
        "unimplemented",
    )
    if not any(count(key) for key in keys):
        return ""
    return (
        "Проверка русского издания: подтверждено русских изданий - "
        f"{count('verified_russian_edition')}, "
        "полная проверка без совпадения - "
        f"{count('verified_complete_no_match')}, "
        "неоднозначно - "
        f"{count('ambiguous')}, "
        "частично - "
        f"{count('partial')}, "
        "не запускалась - "
        f"{count('not_run')}, "
        "заблокировано - "
        f"{count('blocked')}, "
        "с ошибкой - "
        f"{count('failed')}, "
        "без реализации - "
        f"{count('unimplemented')}."
    )


# PM's deal size bands with their published dollar ranges (#948).
_DEAL_SIZE_LABELS = {
    "nice": "nice (до $49 тыс.)",
    "very nice": "very nice ($50–99 тыс.)",
    "good": "good ($100–250 тыс.)",
    "significant": "significant ($251–499 тыс.)",
    "major": "major (от $500 тыс.)",
}
_DEAL_TYPE_LABELS = {
    "auction": "аукцион",
    "pre-empt": "преэмптив",
    "exclusive submission": "эксклюзивная подача",
}
_SEASON_LABELS = {"spring": "весна", "summer": "лето", "fall": "осень", "winter": "зима"}
_MONTH_LABELS = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)
RUSSIAN_RIGHTS_SOLD_TEXT = "русские права уже проданы"


def _texts(value: Any) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


def _book_count_label(count: Any) -> str:
    if type(count) is not int or count < 2:
        return "договор на несколько книг"
    word = "книги" if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14) else "книг"
    return f"договор на {count} {word}"


def _publication_label(publication: Any) -> str:
    if not isinstance(publication, Mapping) or type(publication.get("year")) is not int:
        return ""
    year, season, month = publication["year"], publication.get("season"), publication.get("month")
    if season in _SEASON_LABELS:
        return f"{_SEASON_LABELS[season]} {year}"
    if type(month) is int and 1 <= month <= 12:
        return f"{_MONTH_LABELS[month - 1]} {year}"
    return str(year)


def _includes_russia(territory: str) -> bool:
    """Whether a sold territory names Russia positively: "Russia", not "world excluding Russia"."""
    text = territory.casefold()
    if not re.search(r"\brussia", text):
        return False
    if re.search(r"\b(?:excluding|excl\.?|except|ex|not|without|minus|less|but)\b[^()]*\brussia", text):
        return False
    # The exclusion may follow the country: "world, Russia excluded", "Russia not included".
    return not re.search(r"\brussia\w*\s+(?:excluded|excepted|not\s+included|withheld|reserved)\b", text)


def _rights_deal_text(deal: Mapping[str, Any]) -> str:
    parts = [str(deal.get("publisher", "")).strip()]
    editors = _texts(deal.get("editors"))
    if editors:
        parts[0] += " (редактор: " + ", ".join(editors) + ")"
    agents = _texts(deal.get("agents"))
    agency = str(deal.get("agency", "")).strip()
    if agents:
        parts.append("агент: " + ", ".join(agents) + (f" ({agency})" if agency else ""))
    elif agency:
        parts.append(f"агентство: {agency}")
    contacts = _texts(deal.get("contacts"))
    if contacts:
        parts.append("контакт: " + ", ".join(contacts))
    territory = str(deal.get("territory", "")).strip()
    if territory:
        parts.append(f"территория: {territory}")
    sold = _texts(deal.get("rights_also"))
    if sold:
        parts.append("права уже проданы: " + ", ".join(sold))
        if any(_includes_russia(item) for item in sold):
            parts.append(RUSSIAN_RIGHTS_SOLD_TEXT)
    terms = []
    size = str(deal.get("deal_size", "")).strip()
    if size in _DEAL_SIZE_LABELS:
        terms.append(_DEAL_SIZE_LABELS[size])
    types = _texts(deal.get("deal_types"))
    terms.extend(_DEAL_TYPE_LABELS[kind] for kind in types if kind in _DEAL_TYPE_LABELS)
    if "multi-book" in types:
        terms.append(_book_count_label(deal.get("book_count")))
    if terms:
        parts.append("сделка: " + ", ".join(terms))
    publication = _publication_label(deal.get("publication"))
    if publication:
        parts.append(f"публикация: {publication}")
    category = str(deal.get("category", "")).strip()
    if category:
        parts.append(f"категория PM: {category}")
    earlier = _texts(deal.get("earlier_books"))
    if earlier:
        parts.append("ранее: " + ", ".join(earlier))
    return "; ".join(part for part in parts if part)


def rights_deal_line(source_evidence: Any) -> str:
    """One Russian line per rights deal of a candidate, read as a rights lead (#945, #948).

    Publisher and acquiring editor, agent and agency, contact, territory, the
    territories already sold (said plainly when Russia is among them), deal size
    and type, expected publication, PM category and earlier titles. Built from
    the structured deal fields only, so no deal text reaches the review. '' without a deal.
    """
    lines: list[str] = []
    for evidence in source_evidence if isinstance(source_evidence, list) else []:
        deal = evidence.get("rights_deal") if isinstance(evidence, Mapping) else None
        if not isinstance(deal, Mapping):
            continue
        line = _rights_deal_text(deal)
        if line and line not in lines:
            lines.append(line)
    return " | ".join(lines)


def review_context(value: Any) -> str:
    """Render internal audit context as useful Russian editorial prose."""
    text = str(value or "").strip()
    parts: list[str] = []
    rule = re.search(r"Hard Rule \S+ applied(?: on ([0-9-]+))?", text)
    if rule:
        parts.append("Ранее скрыта редакторским правилом" + (f" ({rule.group(1)})" if rule.group(1) else "") + ".")
    suppressed = re.search(r"Suppressed from current recommendations: editor status '([^']+)'", text)
    if suppressed:
        parts.append(
            f"Не входит в текущие рекомендации: решение редактора — «{editor_status_label(suppressed.group(1))}»."
        )
    previous = re.search(r"Previously suppressed by editor status '([^']+)'", text)
    if previous:
        parts.append(f"Ранее исключена решением редактора — «{editor_status_label(previous.group(1))}».")
    excluded = re.search(r"Excluded from acquisition: (.*?)(?: on ([0-9-]+))?(?:; evidence: (\S+))?\.", text)
    if excluded:
        raw_reason = excluded.group(1).strip()
        reason = (
            exclusion_category_label(raw_reason)
            if raw_reason in INELIGIBLE_CANDIDATE_CATEGORIES
            else russian_status_label(raw_reason)
        )
        rendered = f"Исключена из отбора: {reason}"
        if excluded.group(2):
            rendered += f" ({excluded.group(2)})"
        if excluded.group(3):
            rendered += f"; подтверждение: {excluded.group(3)}"
        parts.append(rendered + ".")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Issue #678: decision-support blockers on the candidate review card.
# ---------------------------------------------------------------------------

DECISION_SUPPORT_UNKNOWN = "нет данных"

_RIGHTS_DIMENSION_LABELS = {
    "available": "права доступны",
    "unavailable": "права недоступны",
    "unknown": "права: неизвестно",
}


def _method_value(row: Mapping[str, Any]) -> str:
    """Original method evidence: the retained narrative classification."""
    return str(row.get("narrative_classification", "")).strip()


def _scope_value(row: Mapping[str, Any]) -> str:
    """Geographical scope evidence.

    The candidate schema retains no field for a book's geographical scope,
    so this blocker currently has no evidence source and always renders as
    explicitly unknown.
    """
    return ""


def _genre_value(row: Mapping[str, Any]) -> str:
    """Genre/treatment evidence: the retained candidate category."""
    return str(row.get("candidate_category", "")).strip()


def _author_relevance_value(row: Mapping[str, Any]) -> str:
    """Author-relevance evidence: matching editorial profile notes.

    ``editorial_profile_notes`` are set only when an active Editorial Taste
    Profile entry matches this candidate, so a nonblank value is genuine
    standing-preference evidence about the author or topic.
    """
    return str(row.get("taste_notes", "")).strip()


def _translated_title_value(row: Mapping[str, Any]) -> str:
    """Usable-translation evidence: the formatted English helper title.

    Uses the same qualifier contract as the review table (``format_editor_english_title``):
    an untyped helper renders as explicitly ``(unverified)`` instead of looking like
    a usable translation (issue #678 class 4).
    """
    return format_editor_english_title(dict(row))


def _russian_edition_value(row: Mapping[str, Any]) -> str:
    """Russian-edition evidence: the retained Russian-edition status."""
    status = str(row.get("russian", "")).strip()
    return russian_status_label(status) if status else ""


def _rights_value(row: Mapping[str, Any]) -> str:
    """Rights/acquisition evidence: the confirmed rights dimension."""
    dimensions = row.get("feedback_dimensions")
    dimensions = dimensions if isinstance(dimensions, Mapping) else {}
    return _RIGHTS_DIMENSION_LABELS.get(str(dimensions.get("rights_availability", "")).strip(), "")


# The seven decision blockers from Issue #678, in display order. Each
# extractor returns the evidence value or an empty string; empty renders as
# explicitly unknown so a missing value is never silently omitted.
_DECISION_SUPPORT_EXTRACTORS: tuple[tuple[str, Callable[[Mapping[str, Any]], str]], ...] = (
    ("Метод", _method_value),
    ("Область", _scope_value),
    ("Жанр/подача", _genre_value),
    ("Релевантность автора", _author_relevance_value),
    ("Перевод названия", _translated_title_value),
    ("Русское издание", _russian_edition_value),
    ("Права/приобретение", _rights_value),
)


def decision_support_items(row: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Return the (label, value) decision-support pairs for one review row.

    The seven decision blockers from the #667 audit are always listed;
    every blocker without evidence is clearly marked unknown
    (``нет данных``) instead of being silently omitted.
    """
    return [(label, extractor(row) or DECISION_SUPPORT_UNKNOWN) for label, extractor in _DECISION_SUPPORT_EXTRACTORS]
