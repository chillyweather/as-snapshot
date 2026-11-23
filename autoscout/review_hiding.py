"""Cards the cumulative review withholds from the editor (#1025).

Two standing decisions keep a card out of the review: an active Hard Rule
that matches it, and the retirement of every source that reported it. Both
are judged again on every render from the stored record, the Editorial Taste
Profile and the source configuration. Nothing is written to Candidate
History, so revoking the rule or restoring the source brings the card back
without any repair step.

A third reason is the editor's own decision on another edition of the book:
an undecided card whose linked records she marked already known or rejected
is withheld too, on the same terms. Dismissing the link, or changing that
decision, brings the card back.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from autoscout.config import resolve_weekly_portfolio_or_raise
from autoscout.editorial_taste_profile import PreparedRule
from autoscout.editorial_taste_profile import hard_rule_hidden_record
from autoscout.editorial_taste_profile import rule_matchable_candidate
from autoscout.identity import normalize_identity_text
from autoscout.json_io import read_json_or_default

HIDDEN_FROM_REVIEW_ARTIFACT = "hidden-from-review.json"
RETIRED_CARDS_HIDDEN_FIELD = "retired_cards_hidden"
# Candidate History keeps only the evidence of the last run that reported a
# book. The names of the sources that reported it before are kept here.
EARLIER_SOURCE_NAMES_FIELD = "earlier_source_names"
HIDDEN_BY_HARD_RULE = "hard_rule"
HIDDEN_BY_RETIRED_SOURCE = "retired_source"
HIDDEN_BY_DECIDED_EDITION = "decided_other_edition"

# A decision that settles the book whatever the edition: she knows it, or she
# does not want it. A book she is interested in is different: a new edition of
# it is news, so its card stays.
EDITOR_STATUSES_THAT_SETTLE_A_BOOK = frozenset({"already_known", "reject"})
# How the Markdown and the HTML review name these cards in the withheld count.
DECIDED_EDITION_HIDDEN_LABEL = "уже решённых в другом издании"

# A card the editor is still working with stays in the review after its
# sources are retired: her own decision is a reason to keep seeing the book.
EDITOR_STATUSES_KEPT_AFTER_RETIREMENT = frozenset({"interested", "maybe_watch", "needs_deeper_analysis"})


def withdrawn_source_names(sources: Iterable[Any]) -> frozenset[str]:
    """Names of the retired sources whose cards leave the review.

    Retirement alone only stops collection. A source withdraws its cards
    when it is retired, inactive, and marked ``retired_cards_hidden``; making
    it active again returns the cards even while the mark stays.
    """
    names: set[str] = set()
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        if (
            source.get("active") is False
            and str(source.get("lifecycle_status", "")).strip() == "retired"
            and source.get(RETIRED_CARDS_HIDDEN_FIELD) is True
        ):
            name = str(source.get("name", "")).strip()
            if name:
                names.add(name)
    return frozenset(names)


def withdrawn_source_names_from_config(config_path: Path) -> frozenset[str]:
    """Read the withdrawn sources from a weekly configuration file.

    Raises ``ValueError`` when the file is absent or is not a usable
    configuration, so a render never mistakes a wrong path for "no source
    is retired".
    """
    if not config_path.is_file():
        raise ValueError(f"configuration file not found: {config_path}")
    raw = read_json_or_default(config_path, {})
    if not isinstance(raw, dict):
        raise ValueError(f"configuration is not a JSON object: {config_path}")
    sources = resolve_weekly_portfolio_or_raise(raw, config_path).get("sources", [])
    return withdrawn_source_names(sources if isinstance(sources, list) else [])


def evidence_source_names(evidence: Any) -> set[str]:
    """Names of the sources in a ``source_evidence`` value of any stored shape."""
    items = [evidence] if isinstance(evidence, Mapping) else evidence if isinstance(evidence, list) else []
    return {name for item in items if isinstance(item, Mapping) and (name := str(item.get("source_name", "")).strip())}


def remembered_source_names(record: Mapping[str, Any], new_evidence: Any) -> list[str]:
    """Sources that reported *record* before and are absent from *new_evidence*.

    Called before a snapshot replaces the record's evidence, so every source
    that ever reported the book stays known to the review.
    """
    stored = record.get(EARLIER_SOURCE_NAMES_FIELD)
    earlier = {str(name).strip() for name in (stored if isinstance(stored, list) else []) if str(name).strip()}
    earlier |= evidence_source_names(record.get("source_evidence"))
    return sorted(earlier - evidence_source_names(new_evidence))


def review_hidden_record(
    record: Mapping[str, Any],
    hard_rules: Sequence[Mapping[str, Any] | PreparedRule],
    withdrawn_sources: frozenset[str],
    *,
    editor_status: str = "",
) -> dict[str, Any] | None:
    """Return why *record* is withheld from the review, or ``None`` when it is shown.

    A Hard Rule is the editor's own "never", so it applies whatever she
    decided about the single book. Source retirement is weaker: it spares a
    card that carries one of ``EDITOR_STATUSES_KEPT_AFTER_RETIREMENT``.

    A caller that judges many records passes *hard_rules* prepared once with
    ``prepare_rules`` (#1165).
    """
    candidate = rule_matchable_candidate(record)
    if hard_rules:
        hidden = hard_rule_hidden_record(candidate, hard_rules)
        if hidden is not None:
            return {**hidden, "hidden_by": HIDDEN_BY_HARD_RULE}
    if not withdrawn_sources or editor_status.strip() in EDITOR_STATUSES_KEPT_AFTER_RETIREMENT:
        return None
    source_names = sorted(evidence_source_names(candidate.data["source_evidence"]))
    # A source that reported the book in an earlier run counts like a current one.
    earlier_names = remembered_source_names(record, candidate.data["source_evidence"])
    if not source_names or not {*source_names, *earlier_names} <= withdrawn_sources:
        return None
    return {
        "identity_key": candidate.identity_key(),
        "title": str(candidate.data.get("title", "")),
        "primary_authors": list(candidate.data.get("primary_authors", [])),
        "hidden_by": HIDDEN_BY_RETIRED_SOURCE,
        "retired_sources": source_names,
        "reason": "Every source that reported this card is retired with its cards withdrawn: "
        + "; ".join(source_names),
    }


def known_titles(record: Mapping[str, Any]) -> list[str]:
    """Every title a Candidate History record gives its book: its own, the English one, the original one."""
    original = record.get("original_edition")
    titles = [
        record.get("title"),
        record.get("english_title"),
        original.get("original_title") if isinstance(original, Mapping) else None,
    ]
    return [title for title in titles if isinstance(title, str) and title.strip()]


def _subtitles(titles: Sequence[str]) -> set[str]:
    subtitles = set()
    for title in titles:
        parts = re.split(r"[:：]", title, maxsplit=1)
        if len(parts) == 2 and (subtitle := normalize_identity_text(parts[1])):
            subtitles.add(subtitle)
    return subtitles


def link_names_the_same_book(signals: Sequence[str], titles: tuple[Sequence[str], Sequence[str]] = ((), ())) -> bool:
    """Whether a probable-duplicate pair is linked strongly enough to withhold a card.

    A link is a hint for the editor; withholding a card needs more. The two
    records must name the same person, by given name and surname, and their
    titles must be equal or tied by title evidence. A missing author, a
    shared surname alone, an initial, or one title merely containing the
    other (a sequel, a companion volume) stays a hint.

    Titles are matched up to the colon, so the volumes of a series match
    too. *titles* holds the known titles of each record: when both sides
    carry subtitles and share none, the link stays a hint, unless the two
    records have the same ISBN.
    """
    if not {"same_given_name", "nickname"} & set(signals) or "author_missing" in signals:
        return False
    if "title_containment" in signals:
        return False
    if "isbn_match" in signals:
        return True
    first, second = (_subtitles(side) for side in titles)
    return not (first and second and first.isdisjoint(second))


def decided_edition_hidden_record(
    row: Mapping[str, Any], decided: Sequence[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """Return why the undecided review *row* is withheld for a decision on another edition, or ``None``.

    *decided* holds the linked records of the same book that carry an editor
    decision, each with the ``signals`` and the ``linked_titles`` of its link. The card is withheld
    only when every one of those decisions is in
    ``EDITOR_STATUSES_THAT_SETTLE_A_BOOK`` and at least one of them is linked
    strongly enough (``link_names_the_same_book``); one edition she is
    interested in keeps it in the review.
    """
    if str(row.get("editor_status", "")).strip() or not decided:
        return None
    if any(str(other.get("editor_status", "")).strip() not in EDITOR_STATUSES_THAT_SETTLE_A_BOOK for other in decided):
        return None
    settling = [
        other
        for other in decided
        if link_names_the_same_book(other.get("signals") or [], other.get("linked_titles") or ((), ()))
    ]
    if not settling:
        return None
    decided_records = sorted(
        (
            {
                "identity_key": str(other.get("identity_key", "")),
                "title": str(other.get("title", "")),
                "editor_status": str(other.get("editor_status", "")).strip(),
            }
            for other in settling
        ),
        key=lambda other: (other["title"], other["identity_key"]),
    )
    return {
        "identity_key": str(row.get("identity_key", "")),
        "title": str(row.get("title", "")),
        "authors": str(row.get("authors", "")),
        "hidden_by": HIDDEN_BY_DECIDED_EDITION,
        "decided_records": decided_records,
        "reason": "The editor already decided on another edition of this book: "
        + "; ".join(f"{other['title']} ({other['editor_status']})" for other in decided_records),
    }
