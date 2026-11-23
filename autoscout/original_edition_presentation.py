"""Presentation of a stored original edition, shared by review formats."""

from typing import Any
from urllib.parse import urlsplit

from autoscout.original_edition import validate_original_edition

_LANGUAGES = dict(
    zip(
        "en es pt de fr it nl ca no sv da fi pl cs hu ru ja zh ko he ar".split(),
        "английский испанский португальский немецкий французский итальянский нидерландский каталанский норвежский шведский датский финский польский чешский венгерский русский японский китайский корейский иврит арабский".split(),
        strict=True,
    )
)


def _is_portal_page(url: str) -> bool:
    try:
        return (urlsplit(url).hostname or "").casefold() == "www.todostuslibros.com"
    except ValueError:
        return False


def format_original_edition(candidate: dict[str, Any]) -> tuple[str, str, str]:
    """Return display text, link URL and link label, or three empty strings."""
    block: Any = candidate.get("original_edition")
    if not validate_original_edition(block):
        return "", "", ""
    year = block["original_first_published_year"]
    if block["status"] == "confirmed":
        title = block["original_title"].strip()
        language = block["original_language"]
        url = block["confirmation_url"]
        text = f"«{title}» — {_LANGUAGES.get(language, language)}"
        if not block["language_confirmed"]:
            text += " (язык не подтверждён)"
        if year is not None:
            text += f", {year}"
        return text, url, "Open Library"
    if block["status"] == "library_stated":
        # A national library's record states the original (#1139); Open
        # Library did not confirm it, so the library record is the link.
        title = block["original_title"].strip()
        language = _LANGUAGES.get(block["original_language"], block["original_language"])
        text = f"«{title}» — {language}" if title else f"Перевод, язык оригинала — {language}"
        url = block["translation_evidence_url"]
        # The booksellers' portal states it too (#1140); its page is the link.
        label = "Todos tus libros" if _is_portal_page(url) else "Каталог национальной библиотеки"
        return text, url, label
    if block["is_translation"] == "yes":
        url = block["translation_evidence_url"]
        return "Перевод, оригинал не найден", url, "Страница с переводчиком"
    return "", "", ""
