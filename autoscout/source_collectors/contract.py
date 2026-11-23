"""The extraction contract for one source and the model prompt built from it."""

from __future__ import annotations

import json
from typing import Any

from autoscout.autobiographical_context import AUTOBIOGRAPHICAL_SUBJECT_CONTEXT_REQUIREMENT
from autoscout.autobiographical_context import NARRATIVE_CLASSIFICATIONS
from autoscout.ranking_detail_route import RankingDetailRoute
from autoscout.ranking_detail_route import ranking_detail_requested_fields
from autoscout.ranking_freshness import ranking_undated_declared
from autoscout.ranking_identity import ranking_identity_declared
from autoscout.source_collectors.agent_model_input import model_bounded_page_data
from autoscout.source_collectors.base import NON_ENGLISH_STRICT_CHART_SEARCH_TRANSLATION_RULE
from autoscout.source_collectors.base import contract_family
from autoscout.source_collectors.row_families import ROW_FAMILIES
from autoscout.source_collectors.row_families import row_family
from autoscout.source_language import apply_english_helper_contract
from autoscout.source_language import english_helper_authority_rules
from autoscout.source_language import requires_english_helper_fields
from autoscout.source_metadata import declared_source_metadata

# Stated so it matches ``evidence_url_verified`` exactly: that check accepts
# the configured source page itself, and otherwise requires the URL to be one
# of the page's links. Asking for a plain verbatim substring would be stricter
# than the collector and would tell extractors to drop grounded mentions --
# every ranking-table mention cites the configured page, which is not
# necessarily linked from its own markup.
EVIDENCE_URL_GROUNDING_RULE = (
    "Every evidence_url must be either the configured source page URL, matched exactly, or a link "
    "that appears in the provided list-page HTML. For a ranking detail route it must be the exact "
    "row-linked detail URL and its matching captured detail_pages item. Never construct, complete, or "
    "recall a URL; never browse to or follow one yourself; drop any entry whose evidence URL is neither of those."
)


# Issue #740: every qualifying row is returned, a doubtful row is dropped on
# its own, and an empty result is a stated, checkable outcome.
EXTRACTION_COMPLETENESS_RULE = (
    "Return every row the page shows that meets the required fields. Drop only an individual row that breaks a "
    "rule; a doubtful row never justifies dropping the rows that qualify."
)


EXTRACTION_EMPTY_RESULT_RULE = "Return an empty result only when no row on the page meets the required fields."


EMPTY_REASON_RULE = (
    "When entries is empty, add a top-level empty_reason: one short sentence naming what the capture lacks. "
    "Omit empty_reason when entries is not empty."
)


EMPTY_REASON_SHAPE = "Only when entries is empty: one short sentence naming why no row in the capture qualifies"


# The helper metadata an award list and an exact book list always ask for.
_HELPER_FIELDS_RULE = (
    "Every mention must include nonblank english_title, search_translation, and author_transliteration_note "
    "helper metadata. search_translation must differ from the authoritative title."
)
_ENGLISH_TITLE_RULE = (
    "For English sources, english_title must equal the authoritative title; for non-English sources, "
    "english_title is its English rendering, equal to the authoritative title only when the title reads "
    "the same in English, such as a name or a word English shares."
)
_HELPER_FIELDS_SHAPE = {
    "english_title": (
        "English helper title equal to the authoritative title for English sources and its English "
        "rendering for non-English sources"
    ),
    "search_translation": "Nonblank Russian search translation different from the authoritative title",
    "author_transliteration_note": "Nonblank retained Russian author transliteration note",
}

# The fields a ranking, chart or award row never states. A family whose check
# rejects any field outside its allowed set must not offer them in its shape.
_DESCRIPTIVE_FIELDS = ("content_description", "autobiographical_subject_context", "publisher", "publication_year")


def _omit(mention_shape: dict[str, Any], fields: tuple[str, ...]) -> None:
    for field in fields:
        mention_shape.pop(field, None)


def _add_english_helpers(
    source: dict[str, Any], rules: list[str], mention_shape: dict[str, Any], required_fields: list[str]
) -> None:
    """Ask a non-English source for the helper translation fields; an English source is left as it is."""
    if requires_english_helper_fields(source):
        rules.extend(english_helper_authority_rules(source))
        apply_english_helper_contract(mention_shape, required_fields)


def _audible_chart(
    source: dict[str, Any], rules: list[str], mention_shape: dict[str, Any], required_fields: list[str]
) -> None:
    rules.extend(
        [
            "Extract the explicit chart entries at exact ranks 1 through 20 only; all ranks must be present exactly once.",
            "Preserve title, primary author, current_rank, narrative_classification, configured category URL, and direct Audible /pd/ product URL only.",
            # Issue #835: the product container shows a subtitle below the
            # linked title, and a title joined with it is not the chart's.
            "The title is the chart entry's linked title text only; never append the subtitle shown below it.",
            f"Set narrative_classification to one of: {', '.join(NARRATIVE_CLASSIFICATIONS)}. "
            "Use a specific class only when the capture states it, and never infer one from the title alone; "
            # Issue #740: the chart shows no genre labels, and without a
            # stated fallback the extractor abstained from the whole chart
            # ("The capture lacks explicit narrative classifications").
            'otherwise use "other nonfiction", which is always available and never a reason to drop a row.',
            "Do not retain descriptions, reviews, promotional prose, or sales units and do not imply print sales.",
        ]
    )
    if str(source.get("language", "")).strip().casefold() == "de":
        rules.append(
            "Rows whose title or bounded entry context explicitly identifies a podcast season (title contains "
            "'Podcast') are excluded from extraction; their chart positions stay validated on the page."
        )
    _omit(mention_shape, _DESCRIPTIVE_FIELDS)
    mention_shape.update(
        {
            "current_rank": "Integer rank 1..20",
            "narrative_classification": (
                'One closed-vocabulary class: "other nonfiction" unless the capture states a more specific one'
            ),
            "candidate_category": source.get("eligible_candidate_category", "adult nonfiction"),
            "underlying_work_type": '"book" or "work"',
            "publication_status": '"published"',
            "source_url": "Configured category chart URL",
            "evidence_url": "Direct Audible /pd/ product URL",
        }
    )
    if requires_english_helper_fields(source):
        apply_english_helper_contract(mention_shape, required_fields, chart_extras=True)
    required_fields += [
        "current_rank",
        "narrative_classification",
        "candidate_category",
        "underlying_work_type",
        "publication_status",
    ]


def _amazon_charts(
    source: dict[str, Any], rules: list[str], mention_shape: dict[str, Any], required_fields: list[str]
) -> None:
    rules.extend(
        [
            "Extract the explicit chart entries at exact ranks 1 through 20 only; all ranks must be present exactly once.",
            "Preserve title, primary author, current_rank, weeks_on_list, the configured weekly list URL, and direct Amazon /dp/ product URL only.",
            "Do not retain descriptions, reviews, promotional prose, or sales units and do not imply print sales.",
            "Evidence URLs must be the direct /dp/ product links visible in the provided HTML.",
        ]
    )
    _omit(mention_shape, _DESCRIPTIVE_FIELDS)
    mention_shape.update(
        {
            "current_rank": "Integer rank 1..20",
            "weeks_on_list": "Positive integer weeks on the list",
            "candidate_category": source.get("eligible_candidate_category", "adult nonfiction"),
            "source_url": "The configured weekly list URL as captured on the dated chart page",
            "evidence_url": "Direct Amazon /dp/ product URL",
        }
    )
    if requires_english_helper_fields(source):
        # Non-English charts carry helper translation fields; the original
        # title and primary author stay authoritative (Issue #366, #378).
        rules.append(
            "The original non-English title and primary author are authoritative; "
            "english_title, english_author_display, english_annotation, search_translation, "
            "and author_transliteration_note are non-authoritative helper metadata only."
        )
        rules.append(NON_ENGLISH_STRICT_CHART_SEARCH_TRANSLATION_RULE)
        apply_english_helper_contract(mention_shape, required_fields, chart_extras=True)
    required_fields += ["current_rank", "weeks_on_list", "candidate_category"]


def _audiobook_ranking(
    source: dict[str, Any], rules: list[str], mention_shape: dict[str, Any], required_fields: list[str]
) -> None:
    rules.extend(
        [
            'Include underlying_work_type ("book" or "work") for every mention.',
            "Only extract adult nonfiction books/works. Exclude fiction, Kids, Teen/YA, summaries/study guides, courses/lectures, non-book audio, and Coming Soon / forthcoming items.",
            "Use the configured category URL as source_url and the book-level audiobook page URL as evidence_url (or item_url). Both must appear in the provided HTML.",
            "Do not invent numeric ranks, sales units, or chart positions. Bestselling ordering is implicit retailer popularity only.",
            'Use signal_strength "minor mention" - there is no measured top/sales placement.',
            "Write a concise evidence note describing the source page evidence without claiming measured sales or numeric rank.",
        ]
    )
    # Extend mention shape with audiobook-specific required fields.
    mention_shape["underlying_work_type"] = '"book" or "work"'
    mention_shape["candidate_category"] = "Adult nonfiction subject category (history, science, self-help, etc.)"
    mention_shape["publication_status"] = '"published" (never "forthcoming" or "coming soon")'
    mention_shape["source_url"] = "The configured category URL (not a book URL)"
    mention_shape["evidence_url"] = "Book-level audiobook page URL from the provided HTML"
    mention_shape["signal_type"] = "audiobook ranking"
    mention_shape["signal_strength"] = "minor mention"
    _add_english_helpers(source, rules, mention_shape, required_fields)
    required_fields += ["underlying_work_type", "candidate_category", "publication_status"]


def _ranking_table(
    source: dict[str, Any],
    rules: list[str],
    mention_shape: dict[str, Any],
    required_fields: list[str],
    detail_route: RankingDetailRoute | None,
) -> None:
    detail_fields = (
        detail_route.requested_fields if detail_route is not None else ranking_detail_requested_fields(source)
    )
    # The source week is asked for only where the page states one, and the
    # Circana wording only where grounding expects that statement. An
    # undated ranking's week is the capture's observation date, set in
    # code, so the extractor is told to leave it out (Issue #733).
    undated = ranking_undated_declared(source)
    if undated:
        week_rules = [
            "Every mention must include numeric current_rank.",
            "This ranking page states no reporting period. Do not return source_week, and never supply a date, "
            "week, or capture date in its place; AutoScout records the capture's observation date itself.",
        ]
        week_shape: dict[str, str] = {}
        note_rule = (
            "Use signal_type bestseller/ranking and a concise metadata-only note naming the rank, "
            "without dates, units, or copied prose."
        )
    else:
        week_rules = [
            "Every mention must include numeric current_rank and the dated source_week stated by the table source note.",
        ]
        date_selector = str(source.get("ranking_reporting_date_selector", "")).strip()
        if "ranking_source_statement" in source:
            week_description = "Dated week from the configured visible ranking source statement"
        elif source.get("ranking_row_selector") and date_selector:
            # The validator reads the period from this one element and
            # accepts no other form of it.  Without the selector named, the
            # extractor took a calendar-week label from an aria-label on a
            # page whose dated element it never read, and every row was
            # dropped (Issue #787).
            week_description = (
                f"Reporting period captured by ranking_reporting_period_pattern in the visible element "
                f"matching {date_selector!r}"
            )
            week_rules.append(
                f"Read source_week only from the visible text of the element matching the configured "
                f"ranking_reporting_date_selector {date_selector!r}: return exactly the text that "
                "ranking_reporting_period_pattern captures there, without surrounding text such as a time of "
                "day. Never take it from an attribute, an aria-label, a heading, or a week label elsewhere "
                "on the page."
            )
        elif source.get("ranking_row_selector"):
            week_description = "Reporting period exactly as the ranking page states it"
        else:
            week_description = "Dated week from the Circana BookScan source statement"
        week_shape = {"source_week": week_description}
        note_rule = (
            "Use signal_type bestseller/ranking and a concise metadata-only note naming rank and source week, "
            "without units or copied prose."
        )
    rules.extend(
        [
            "Extract only rows in the current configured ranking table; ignore reviews, navigation, and historical rank details.",
            *week_rules,
            "For a detail route, take configured source_url from the ranking list and evidence_url from the exact linked detail page. Use ISBN and category only when grounded in the captured bounded detail scope.",
            "A detail category is raw source evidence checked only against ranking_detail_category_allowlist; candidate_category remains the configured eligible_candidate_category and must not copy the raw category.",
            "Do not return previous rank, weeks on list, publication date, sales history, descriptions, reviews, current-week units, or YTD units.",
            note_rule,
        ]
    )
    mention_shape.update(
        {
            "isbn": (
                "Bare ISBN digits from the matching bounded detail scope, without labels such as ISBN"
                if "isbn" in detail_fields
                else "Bare ISBN digits from the current table row, without labels such as ISBN"
            ),
            "current_rank": "Numeric current rank from the current table row",
            **week_shape,
            "source_url": "The configured ranking page URL",
            "evidence_url": (
                "Exact linked detail-page URL" if detail_route is not None else "The configured ranking page URL"
            ),
        }
    )
    # The ranking-table check rejects any field outside its allowed set,
    # so the shape must not offer publisher or publication_year either.
    _omit(mention_shape, _DESCRIPTIVE_FIELDS)
    required_fields.append("isbn")
    required_fields += ["current_rank"] if undated else ["current_rank", "source_week"]
    _add_english_helpers(source, rules, mention_shape, required_fields)
    eligible_category = str(source.get("eligible_candidate_category", "")).strip()
    if eligible_category:
        mention_shape["candidate_category"] = f'Exactly "{eligible_category}"'
        rules.append(f'Every mention must set candidate_category to "{eligible_category}".')
        required_fields.append("candidate_category")
    if "entry_scope_selector" in source:
        rules.append(
            f"Use only visible direct-child entry containers selected by the configured entry scope selector "
            f"{str(source['entry_scope_selector']).strip()!r} when grounding ranking rows."
        )
    if "ranking_source_statement" in source and not undated:
        rules.append(
            f"Use the configured visible ranking source statement {str(source['ranking_source_statement']).strip()!r} "
            "when grounding source_week."
        )
    if detail_route is not None:
        rules.extend(
            [
                "The list row must expose exactly one valid same-domain detail link matching ranking_detail_url_pattern.",
                "Use the list URL as source_url and the exact row-linked detail URL as evidence_url.",
                "Use only the matching detail_pages item for the row; never follow links or fetch another page.",
                "A missing, omitted, ambiguous, mismatched, redirected, or out-of-scope detail page is not evidence.",
            ]
        )
        if "category" in detail_fields:
            rules.append(
                "Return candidate_category only as the configured eligible_candidate_category after the declared detail category matches the exact allowlist."
            )
        if "isbn" in detail_fields:
            rules.append("Return isbn only when the declared detail identifier is one valid ISBN in the bounded scope.")


def _award_list(
    source: dict[str, Any], rules: list[str], mention_shape: dict[str, Any], required_fields: list[str]
) -> None:
    from autoscout.award_list import CATEGORY_LABEL_ELEMENT_FIELD
    from autoscout.award_list import ROW_FORMAT_FIELD
    from autoscout.award_list import TITLE_ELEMENT_FIELD
    from autoscout.award_list import YEAR_FIELD
    from autoscout.award_list import category_headings
    from autoscout.award_list import row_format_fields

    expected_strength = str(source.get("expected_signal_strength", "")).strip()
    eligible_category = str(source.get("eligible_candidate_category", "")).strip()
    try:
        headings = category_headings(source)
    except ValueError:
        headings = ()
    heading_phrase = (
        f"the visible heading {headings[0]!r}"
        if len(headings) == 1
        else "the visible headings " + ", ".join(repr(heading) for heading in headings)
    )
    title_element = str(source.get(TITLE_ELEMENT_FIELD) or "").strip()
    label_element = str(source.get(CATEGORY_LABEL_ELEMENT_FIELD) or "").strip()
    stated_fields = row_format_fields(source)
    if label_element:
        rules.append(
            f"On this page a category heading is a paragraph set entirely in one <{label_element}> element. "
            "The category ends at the next such paragraph or at the next heading."
        )
    if title_element:
        rules.append(
            f"Each row sets its title in one <{title_element}> element. The title is the text of that element "
            "without the separator that follows it, even when the title itself contains that separator."
        )
    rules.extend(
        [
            f"Extract only the rows listed under {heading_phrase} for the "
            f"{source.get(YEAR_FIELD)} award. Rows under any other heading are not entries of this source.",
            f"Each row is written as {str(source.get(ROW_FORMAT_FIELD, '')).strip()!r}. Take the title and the "
            "primary authors from that row exactly as written; never complete them from outside knowledge.",
            "Return every author the row names, in row order, each as one complete name in primary_authors. "
            "Names are separated by a comma, semicolon, slash, ampersand, or the configured "
            "award_author_conjunction word. A partial author list or a shortened name is not grounded.",
            "The rows carry no book links. Use the configured source page as both source_url and evidence_url.",
            "Every mention must preserve the configured signal_type and signal_strength exactly as declared "
            "in source_metadata.",
            "This page states no reporting period. Do not return source_week.",
            (
                "Return current_rank only as the rank the row states."
                if "rank" in stated_fields
                else "The rows are unranked. Do not return current_rank."
            ),
            (
                "Return isbn only as the identifier the row states."
                if "identifier" in stated_fields
                else "The rows state no identifier. Do not return isbn."
            ),
            _HELPER_FIELDS_RULE,
            _ENGLISH_TITLE_RULE,
        ]
    )
    # The award-list check rejects any field outside its allowed set, so
    # the shape must not offer a publisher, a year, or a description.
    _omit(mention_shape, _DESCRIPTIVE_FIELDS)
    rules.append(
        "Return only the fields in the required output shape. Do not return a publisher, a publication year, "
        "a description, or any other fact about the book."
    )
    mention_shape.update(
        {
            "source_url": "The configured source page",
            "evidence_url": "The configured source page",
            "signal_type": source.get("signal_family", source.get("family", "")),
            "signal_strength": expected_strength,
            **_HELPER_FIELDS_SHAPE,
        }
    )
    if "rank" in stated_fields:
        mention_shape["current_rank"] = "Rank exactly as the row states it"
    if "identifier" in stated_fields:
        mention_shape["isbn"] = "ISBN exactly as the row states it"
    required_fields += list(_HELPER_FIELDS_SHAPE)
    if eligible_category:
        mention_shape["candidate_category"] = eligible_category
        rules.append(f'Every mention must set candidate_category to "{eligible_category}".')
        required_fields.append("candidate_category")


def _exact_book_list(
    source: dict[str, Any], rules: list[str], mention_shape: dict[str, Any], required_fields: list[str]
) -> None:
    expected_strength = str(source.get("expected_signal_strength", "")).strip()
    eligible_category = str(source.get("eligible_candidate_category", "")).strip()
    rules.extend(
        [
            "Extract only exact list-linked entries from the configured page. Use the configured source page as source_url and each exact book-level link URL as evidence_url. Both must appear in the provided HTML.",
            "Every mention must preserve the configured signal_type and signal_strength exactly as declared in source_metadata.",
            "The authoritative title must exactly match the visible anchor text of its evidence link. Every primary author must appear visibly within the same bounded entry context (e.g. same list item or article block) as the linked title.",
            f'Reject any entry whose title is not a match for that link\'s anchor text, whose author is not in the same entry context, or whose candidate_category is not "{eligible_category}".'
            if eligible_category
            else "Reject any entry whose title is not a match for that link's anchor text or whose author is not in the same entry context.",
            "Reject fiction, children/YA, narrow manuals, topic-only/navigation/historical references, and entries lacking grounded book-level evidence.",
            _HELPER_FIELDS_RULE,
        ]
    )
    entry_section_heading = str(source.get("entry_section_heading", "")).strip()
    if entry_section_heading:
        rules.append(
            f"Extract only the linked entries inside the page section headed {entry_section_heading!r}. "
            "Books linked in any other section of the page are not entries of this source."
        )
    mention_shape["candidate_category"] = eligible_category
    mention_shape["source_url"] = "The configured source page"
    mention_shape["evidence_url"] = "Exact book-level link URL from the provided HTML"
    mention_shape["signal_type"] = source.get("signal_family", source.get("family", ""))
    mention_shape["signal_strength"] = expected_strength
    mention_shape.update(_HELPER_FIELDS_SHAPE)
    rules.append(_ENGLISH_TITLE_RULE)
    required_fields += list(_HELPER_FIELDS_SHAPE)
    if eligible_category:
        rules.append(f'Every mention must set candidate_category to "{eligible_category}".')
        required_fields.append("candidate_category")


def _generic(
    source: dict[str, Any], rules: list[str], mention_shape: dict[str, Any], required_fields: list[str]
) -> None:
    # The fallback family carries every source without a routing flag,
    # which is where a newly onboarded source lands by default. A
    # non-English source must request the helper fields here exactly as
    # it does from the routed families (Issue #645).
    _add_english_helpers(source, rules, mention_shape, required_fields)


_FAMILY_CONTRACTS = {
    "audible-chart": _audible_chart,
    "amazon-charts": _amazon_charts,
    "audiobook-ranking": _audiobook_ranking,
    "award-list": _award_list,
    "exact-book-list": _exact_book_list,
    "generic": _generic,
}


def extraction_contract(
    source: dict[str, Any],
    *,
    source_page: str = "",
    capture_file: str = "",
    detail_route: RankingDetailRoute | None = None,
) -> dict[str, Any]:
    """Return the extraction contract for one configured source.

    The contract is the machine-generated statement of what an extraction of
    this source must return: the required output shape, the fields that must
    always be present, and the family-specific rules that
    ``extraction_contract_violation`` enforces at collection time. It is the
    single source of truth for both extraction paths -- the agent-extraction
    model prompt built by :func:`agent_extraction_prompt` and the Codex
    worklist item written by ``prepare_codex_extraction_worklist``. Keeping one
    producer is what stops an extractor from being asked for a shape the
    validator will then reject (Issue #375).

    *capture_file* names the saved capture an extraction must work from. Codex
    extracts from a saved file rather than from a page handed to it inline, so
    passing it adds the two rules that bind an extraction to that capture.
    """
    # One family governs a source, resolved with the validator's precedence:
    # five portfolio sources set both a chart flag and metadata_exact_book_list,
    # and describing the exact-book-list shape for them would ask for evidence
    # URLs the chart check rejects.
    family = contract_family(source)
    if family in {"publisher-frontlist", "nuevoslibros-frontlist"}:
        # Same agent extraction contract and helper requirements as exact
        # lists; the family-specific validator additionally binds all authors
        # and the release year to one dated card.
        family = "exact-book-list"
    row = ROW_FAMILIES.get(family)
    retained_author_evidence = family == "ranking-table" and bool(source.get("ranking_author_evidence_json"))
    rules: list[str] = [
        (
            "Use only the configured source page HTML and explicitly retained ISBN author evidence in "
            "source_metadata, and obey the source_metadata constraints."
            if retained_author_evidence
            else "Use only the configured source page HTML and obey the source_metadata constraints."
        ),
        (
            "Use only the captured list HTML, captured detail_pages when that route is declared, "
            "and explicitly retained ISBN author evidence."
            if retained_author_evidence
            else "Use only the captured list HTML plus captured detail_pages when that route is declared."
        ),
        "Do not browse, search the web, infer from outside knowledge, or follow links yourself.",
        "Return metadata only. Do not include article bodies, reviews, blurbs, excerpts, samples, or copied long text.",
        EVIDENCE_URL_GROUNDING_RULE,
        "Every field listed in required_fields must be present and non-empty in every returned mention.",
        # Issue #740: this used to read "Return an empty mentions array if no
        # eligible adult nonfiction book/work mentions are present", and the
        # extractor took it whenever any rule felt uncertain, returning nothing
        # from captures whose rows it had already parsed.
        EXTRACTION_COMPLETENESS_RULE,
        EXTRACTION_EMPTY_RESULT_RULE,
    ]
    if source.get("headline_card_titles"):
        rules.append(
            "Use the book title named on the card, never its article headline; drop round-ups and cards without a specific book."
        )
    if source.get("children_byline_markers"):
        rules.append("Drop children's and YA books identified by the byline, imprint or review line.")
    if source.get("eligibility_outside_knowledge"):
        rules[2] = (
            "Do not browse, search the web, or follow links. The retailer category is not reliable for genre. "
            "Outside knowledge may be used for one decision only: whether a row is adult nonfiction. "
            "Never use it to change, complete or add a field."
        )
    if row is not None:
        for position, rule in row.lead_rules.items():
            rules[position] = rule
    if retained_author_evidence:
        rules.append(
            "For a missing row author, use the author field from ranking_author_evidence_json only for an "
            "exact ISBN and title match; never replace a visible author. The catalog_author field is a separate "
            "authority form, not the primary author display. An unmatched row has no author fallback; "
            "do not query a catalog or infer an author."
        )
    if ranking_identity_declared(source):
        rules.append(
            "Use identity_html only for the visible ranking category and reporting period. "
            "Never use identity_html as a source of ranking rows or book links."
        )
    source_signal_family = (
        str(source.get("signal_family") or source.get("family") or source.get("source_family", "")).strip().casefold()
    )
    if source_signal_family == "award":
        rules.extend(
            [
                "When one prize page presents multiple laureates for one work, return one Agent-Extracted Mention "
                "with every named primary author in one primary_authors list, and never split the work into one "
                "entry per laureate.",
                "Reject prize or lifetime citations that name no specific work, including Ensemble de son œuvre "
                "and conservative close variants.",
            ]
        )
    # The fields an extraction must always supply. Fields outside this list
    # stay conditional on what the page shows.
    #
    # This is what an extraction is asked for, and it is deliberately no looser
    # than enforcement -- but it is not identical to it. The family-specific
    # additions below are checked by ``extraction_contract_violation`` on both
    # paths. These base seven are checked only on the Codex path, by
    # ``_validate_codex_raw_required_fields``, which also accepts any one of
    # source_url / evidence_url / item_url for the pair and treats authors as
    # primary_authors. Agent extraction defaults a missing signal_strength or
    # evidence_note in ``normalized_agent_mention`` instead of dropping the
    # mention. Closing either gap changes collection behaviour on paths outside
    # Issue #375, so both stay as they are and are documented in
    # docs/operator/capture-and-extraction.md.
    required_fields: list[str] = [
        "title",
        "primary_authors",
        "source_url",
        "evidence_url",
        "signal_type",
        "signal_strength",
        "evidence_note",
    ]
    if family in {"audiobook-ranking", "exact-book-list", "generic"}:
        rules.append(
            "Write every content_description as concise original Russian prose (2-4 sentences). "
            "Do not copy English source blurbs - summarise the book in your own Russian words."
        )
        rules.append(AUTOBIOGRAPHICAL_SUBJECT_CONTEXT_REQUIREMENT)
        rules.append(
            "For this extraction, supplied source metadata means facts explicitly present in the provided HTML; "
            "never infer autobiographical_subject_context from outside knowledge."
        )
    # Base mention shape shared by all source families.
    mention_shape: dict[str, Any] = {
        "title": "Book title",
        "primary_authors": ["Author name"],
        "source_url": "Book or evidence URL from the provided page",
        "evidence_url": "Book or evidence URL from the provided page",
        "signal_type": source.get("family", ""),
        "signal_strength": (
            "winner | shortlist | top ranking | sustained bestseller |"
            " reader favorite | major media list | rights/deal mention | minor mention"
        ),
        "evidence_note": "Short metadata-only note explaining the page evidence",
        "publisher": "Publisher or imprint when explicitly shown on the configured source page",
        "publication_year": "Publication year when explicitly shown on the configured source page",
        "content_description": (
            "Concise original Russian-language scouting description of the book's subject, angle, and editorial hook"
        ),
        "autobiographical_subject_context": {
            "opening_phrase": "Exact inflected Russian public-role/notability phrase used in the opening sentence",
            "source_phrase": "Exact supporting phrase present in the provided HTML",
            "evidence_url": "Matching book or evidence URL from the provided HTML",
        },
    }
    if row is not None:
        rules.extend(row.rules)
        _omit(mention_shape, row.omitted_fields)
        mention_shape.update(row.shape(source))
        required_fields += row.required_fields
        _add_english_helpers(source, rules, mention_shape, required_fields)
    elif family == "ranking-table":
        _ranking_table(
            source,
            rules,
            mention_shape,
            required_fields,
            detail_route or RankingDetailRoute.from_source(source),
        )
    elif family in _FAMILY_CONTRACTS:
        _FAMILY_CONTRACTS[family](source, rules, mention_shape, required_fields)
    contract_source_metadata = declared_source_metadata(source)
    for field in ("entry_scope_selector", "ranking_source_statement"):
        if field in source:
            contract_source_metadata[field] = source[field]
    contract: dict[str, Any] = {
        "source_name": source.get("name", ""),
        "source_family": source.get("family", ""),
        "source_url": source.get("url", source_page),
        "source_metadata": contract_source_metadata,
        "required_output_shape": {"mentions": [mention_shape]},
        "required_fields": list(dict.fromkeys(required_fields)),
    }
    if capture_file:
        contract["capture_file"] = capture_file
        # A capture-bound extraction writes a file that the validator reads back
        # and binds to this operation, so its artifact needs the identity
        # envelope. The inline agent path returns a bare mentions array to its
        # caller in memory and has nothing to bind, so it keeps the shape above.
        #
        # This branch exists because the contract previously declared the bare
        # shape on both paths while the Codex prompt asked for the envelope in
        # prose. An extractor that believed the machine-readable shape produced
        # an artifact the validator then refused as identity_mismatch, and the
        # grounded mentions inside it were discarded (Issue #386). The contract
        # is the single producer of the required shape (Issue #375), so the
        # envelope is stated here rather than left to the prose.
        contract["required_output_shape"] = {
            "source_name": contract["source_name"],
            "source_url": contract["source_url"],
            "operation_id": "Copy operation_id exactly from the work item",
            "capture_sha256": "Copy capture_sha256 exactly from the work item",
            "entries": [mention_shape],
            "empty_reason": EMPTY_REASON_SHAPE,
        }
        contract["required_top_level_fields"] = [
            "source_name",
            "source_url",
            "operation_id",
            "capture_sha256",
            "entries",
        ]
        if row is not None:
            rules += row.capture_rules(capture_file)
        else:
            rules += [
                f"Extract only from the saved capture file {capture_file}; its html field is the "
                "authoritative list-page snapshot for this run, and its detail_pages list is the only "
                "per-row second-page evidence when the declared ranking detail route is present.",
                "Before returning a mention, confirm its evidence_url is the configured source page URL "
                f"or occurs as a row link in the html field of {capture_file}; for a detail route, use "
                "only the matching detail_pages item and never browse or follow links.",
            ]
        rules += [
            "Return every field listed in required_top_level_fields at the top level of the artifact. "
            "Copy source_name, source_url, operation_id, and capture_sha256 exactly from the work item; "
            "an artifact missing any of them is refused whole, and the mentions inside it are discarded.",
            "Name the mention array entries at the top level.",
            EMPTY_REASON_RULE,
        ]
    contract["rules"] = rules
    return contract


def agent_extraction_prompt(source: dict[str, Any], page_data: dict[str, Any], max_chars: int) -> str:
    detail_route = RankingDetailRoute.from_source(source)
    bounded_page, _debug = model_bounded_page_data(page_data, max_chars, source=source, detail_route=detail_route)
    contract = extraction_contract(source, source_page=str(page_data.get("source_page", "")), detail_route=detail_route)
    detail_pages = bounded_page.get("detail_pages", [])
    detail_json = json.dumps(detail_pages, ensure_ascii=False, indent=2) if detail_pages else "[]"
    completeness = str(bounded_page.get("detail_pages_complete", True)).lower()
    identity_section = ""
    if ranking_identity_declared(source):
        identity_section = (
            "Captured ranking identity-page HTML (separate from row HTML; use it only for category and reporting period):\n"
            f"{bounded_page.get('identity_html', '')}\n\n"
        )
    row = row_family(source)
    if row is not None:
        # These contracts have no page HTML: the validated rows are the whole evidence.
        return (
            "Extract Source Mention metadata as JSON.\n\n"
            f"Contract:\n{json.dumps(contract, ensure_ascii=False, indent=2)}\n\n"
            f"Captured validated {row.prompt_rows_label} JSON (the only evidence; each row carries its own "
            "evidence_url; never browse or follow links):\n"
            f"{bounded_page.get('text', '')}"
        )
    return (
        "Extract Source Mention metadata as JSON.\n\n"
        f"Contract:\n{json.dumps(contract, ensure_ascii=False, indent=2)}\n\n"
        f"Captured configured list-page HTML (source_url is this configured list URL):\n{bounded_page.get('html', '')}\n\n"
        f"{identity_section}"
        "Captured detail_pages JSON (use only the matching row-linked item; never browse or follow links):\n"
        f"{detail_json}\n"
        f"detail_pages_complete={completeness}. Omitted or incomplete pages are not evidence."
    )
