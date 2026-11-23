"""When a ranking row may legitimately carry no author.

``ranking-table`` treated a row author as mandatory, so one author-less row
rejected a whole page.  That is wrong for an ordinary bookshop list, where
anonymous, collective, and institutional titles appear: the Cúspide list
rejected on one row of a hundred, recorded in
``docs/issue-584-argentina-source-feasibility.md``.

A source may now declare ``ranking_author_optional``.  The declaration is
narrow by construction:

* It is a declaration on the source, never an inference.  No author is guessed
  from a neighbouring row, from a search, or from a detail page.
* The author selector stays mandatory, so the page shape stays pinned.  An
  absent author is allowed; a wrong or ungrounded one is not.
* A row that carries author text is grounded exactly as before.
* Every source that does not declare it keeps the unchanged contract, including
  the shared requirement that a source mention carries ``primary_authors``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

RANKING_AUTHOR_OPTIONAL_FIELD = "ranking_author_optional"


def ranking_author_optional_declared(source: Mapping[str, Any]) -> bool:
    """Return whether this source declares that an author-less row is valid."""
    return source.get(RANKING_AUTHOR_OPTIONAL_FIELD) is True


def validate_ranking_author_optional_declaration(source: Mapping[str, Any]) -> str:
    """Return the declaration error for the author-optional flag, if any."""
    value = source.get(RANKING_AUTHOR_OPTIONAL_FIELD)
    if value is None or value is False:
        return ""
    if value is not True:
        return f"{RANKING_AUTHOR_OPTIONAL_FIELD} must be true or false"
    # The selector stays mandatory: without it the page shape is unpinned, and
    # "no author" would become "we never looked".
    if not str(source.get("ranking_author_selector", "")).strip():
        return f"{RANKING_AUTHOR_OPTIONAL_FIELD} requires ranking_author_selector"
    return ""


def ranking_row_author_required(source: Mapping[str, Any]) -> bool:
    """Return whether a bounded ranking row must carry a nonblank author."""
    return not ranking_author_optional_declared(source)


def mention_author_exempt(source: Mapping[str, Any]) -> bool:
    """Return whether one source may publish a mention with no author.

    The exemption is limited to a ranking-table source that declares it.  Every
    other source keeps ``primary_authors`` as a required mention field.
    """
    family = str(source.get("contract_family", "")).strip().casefold()
    if family != "ranking-table" and source.get("metadata_ranking_table") is not True:
        return False
    return ranking_author_optional_declared(source)


def work_item_author_exempt(item: Mapping[str, Any]) -> bool:
    """``mention_author_exempt`` for a Codex work item, read from its source metadata.

    The artifact gate and the strict resume check both use it, so an artifact
    the gate accepts is never refused on resume (Issue #839).
    """
    metadata = item.get("source_metadata")
    return mention_author_exempt(metadata if isinstance(metadata, Mapping) else {})
