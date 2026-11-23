"""Presentation of a stored Russian-edition finding, shared by review formats (#1181).

The finding never removes a book from the review: it is a mark the editor
checks against the linked record. Russian wording lives only here.
"""

import re
from typing import Any
from urllib.parse import urlsplit

from autoscout.russian_edition import EVIDENCE_HOSTS
from autoscout.russian_edition import STATUS_CONFIRMED
from autoscout.russian_edition import STATUS_UNCONFIRMED
from autoscout.russian_edition import validate_russian_edition
from autoscout.russian_edition_sources import SOURCE_LITRES

_CATALOGUE_LABEL = "Каталог РГБ"
_LITRES_LABEL = "ЛитРес"
_LOCATOR_LABEL = "LiveLib"
# Any run of whitespace, line breaks of every kind included, so the mark is
# always one line in the Markdown row, the CSV field and the HTML card.
_WHITESPACE = re.compile(r"\s+")


def _one_line(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _evidence_label(url: str) -> str:
    host = (urlsplit(url).hostname or "").casefold()
    return _LITRES_LABEL if host in EVIDENCE_HOSTS[SOURCE_LITRES] else _CATALOGUE_LABEL


def russian_edition_mark_status(candidate: dict[str, Any]) -> str:
    """``confirmed`` or ``unconfirmed`` when the book carries a mark, else an empty string."""
    record: Any = candidate.get("russian_edition")
    if not validate_russian_edition(record) or record["status"] not in (STATUS_CONFIRMED, STATUS_UNCONFIRMED):
        return ""
    return str(record["status"])


def russian_edition_short_mark(candidate: dict[str, Any]) -> str:
    """The mark for a crowded row: that a finding exists, without the edition's details."""
    return {STATUS_CONFIRMED: "Есть русское издание", STATUS_UNCONFIRMED: "Возможно, есть русское издание"}.get(
        russian_edition_mark_status(candidate), ""
    )


def format_russian_edition(candidate: dict[str, Any]) -> tuple[str, str, str]:
    """Return display text, link URL and link label, or three empty strings."""
    status = russian_edition_mark_status(candidate)
    if not status:
        return "", "", ""
    record = candidate["russian_edition"]
    title = _one_line(record["russian_title"])
    url = record["evidence_url"]
    if status == STATUS_UNCONFIRMED:
        return f"Возможно, есть русское издание: «{title}» (каталоги не подтвердили)", url, _LOCATOR_LABEL
    text = f"Есть русское издание: «{title}»"
    details = [
        part for part in (_one_line(record["publisher"]), "" if record["year"] is None else str(record["year"])) if part
    ]
    if details:
        text += " — " + ", ".join(details)
    return text, url, _evidence_label(url)
