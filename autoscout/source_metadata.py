"""Canonical reusable metadata vocabulary for configured External Signal sources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from autoscout.ranking_identity import RANKING_IDENTITY_FIELDS_FIELD
from autoscout.ranking_identity import RANKING_IDENTITY_URL_TEMPLATE_FIELD
from autoscout.ranking_request import RANKING_REQUEST_BODY_FIELD
from autoscout.ranking_request import RANKING_REQUEST_MAX_PAGES_FIELD
from autoscout.ranking_request import RANKING_REQUEST_METHOD_FIELD
from autoscout.ranking_request import RANKING_REQUEST_METHODS
from autoscout.ranking_request import RANKING_REQUEST_PAGE_FIELD
from autoscout.ranking_response import RANKING_RESPONSE_HTML_FIELD
from autoscout.ranking_response import RANKING_RESPONSE_LAST_PAGE_FIELD

SOURCE_METADATA_FIELDS: tuple[str, ...] = (
    "language",
    "country",
    "market",
    "expected_category",
    "signal_family",
    "refresh_cadence",
    "url_stability",
    "extraction_notes",
)

SOURCE_PAGE_IDENTITY_FIELDS: tuple[str, ...] = (
    "expected_page_heading",
    "expected_sort_label",
)

SOURCE_LAYOUT_FIELDS: tuple[str, ...] = (
    "entry_scope_selector",
    "ranking_source_statement",
    "visible_title_selector",
    "expected_reporting_period",
    "ranking_expected_path",
)

# Feasibility and collection contracts are separate from the External Signal
# family.  The field is optional for legacy configured sources and for the
# generic probe path.
SOURCE_CONTRACT_FIELDS: tuple[str, ...] = ("contract_family",)
SOURCE_HELPER_METADATA_FIELDS: tuple[str, ...] = ("helper_metadata_file", "helper_metadata_sha256")

# Declarative ranking-row fields. These fields are intentionally separate
# from the general source metadata vocabulary so a ranking adapter cannot
# weaken the generic linked-list evaluator.
RANKING_RANK_SOURCES: tuple[str, ...] = ("selector", "document_order")
RANKING_DETAIL_REQUESTED_FIELDS: tuple[str, ...] = ("isbn", "category")

# A recurring ranking whose page prints no date declares this instead of the
# dated reporting-period trio.  See ``autoscout.ranking_freshness``.
RANKING_UNDATED_FRESHNESS_FIELD = "ranking_reporting_period_undated"

# A ranking row may legitimately carry no author.  See
# ``autoscout.ranking_author_policy``.
RANKING_AUTHOR_OPTIONAL_FIELD = "ranking_author_optional"


@dataclass(frozen=True)
class RankingMetadataFieldSpec:
    name: str
    value_kind: str
    repeatable: bool = False
    detail_route: bool = False
    probe_option: str = ""
    probe_dest: str = ""
    probe_scope: str = "aligned"
    probe_choices: tuple[str, ...] = ()
    probe_help: str = ""


RANKING_TABLE_FIELD_SPECS: tuple[RankingMetadataFieldSpec, ...] = (
    RankingMetadataFieldSpec(
        "bookinfo_period_evidence",
        "string",
        probe_option="--bookinfo-period-evidence",
        probe_dest="bookinfo_period_evidence",
        probe_choices=("week-options-and-request",),
        probe_help="Explicit BookInfo period evidence policy.",
    ),
    RankingMetadataFieldSpec(
        "bookinfo_max_age_days",
        "int",
        probe_option="--bookinfo-max-age-days",
        probe_dest="bookinfo_max_age_days",
        probe_help="BookInfo period-start age limit (14 days).",
    ),
    RankingMetadataFieldSpec(
        "ipc_top40_period_evidence",
        "string",
        probe_option="--ipc-top40-period-evidence",
        probe_dest="ipc_top40_period_evidence",
        probe_choices=("hosted-file-upload-date",),
        probe_help="Explicit Independent Press Top 40 period evidence policy.",
    ),
    RankingMetadataFieldSpec(
        "ipc_top40_max_age_days",
        "int",
        probe_option="--ipc-top40-max-age-days",
        probe_dest="ipc_top40_max_age_days",
        probe_help="Independent Press Top 40 file age limit (14 days).",
    ),
    RankingMetadataFieldSpec(
        "perlentaucher_max_daily_pages",
        "int",
        probe_option="--perlentaucher-max-daily-pages",
        probe_dest="perlentaucher_max_daily_pages",
        probe_help="Perlentaucher digest cap on daily pages read in one run (1 to 8).",
    ),
    RankingMetadataFieldSpec(
        "perlentaucher_max_age_days",
        "int",
        probe_option="--perlentaucher-max-age-days",
        probe_dest="perlentaucher_max_age_days",
        probe_help="Perlentaucher digest daily page age limit in days (1 to 14).",
    ),
    RankingMetadataFieldSpec(
        "perlentaucher_translation_policy",
        "string",
        probe_option="--perlentaucher-translation-policy",
        probe_dest="perlentaucher_translation_policy",
        probe_choices=("exclude-translator-note",),
        probe_help="Explicit Perlentaucher digest rule for rows whose blurb opens with a translator note.",
    ),
    RankingMetadataFieldSpec(
        "kirkus_max_listing_pages",
        "int",
        probe_option="--kirkus-max-listing-pages",
        probe_dest="kirkus_max_listing_pages",
        probe_help="Kirkus listing cap on listing pages read in one pass (1 to 40).",
    ),
    RankingMetadataFieldSpec(
        "kirkus_max_detail_pages",
        "int",
        probe_option="--kirkus-max-detail-pages",
        probe_dest="kirkus_max_detail_pages",
        probe_help="Kirkus listing cap on review pages opened in one pass (1 to 60).",
    ),
    RankingMetadataFieldSpec(
        "kirkus_request_delay_seconds",
        "int",
        probe_option="--kirkus-request-delay-seconds",
        probe_dest="kirkus_request_delay_seconds",
        probe_help="Kirkus listing pause before every request after the first, in seconds (20 to 120).",
    ),
    RankingMetadataFieldSpec(
        "kirkus_max_collection_age_days",
        "int",
        probe_option="--kirkus-max-collection-age-days",
        probe_dest="kirkus_max_collection_age_days",
        probe_help="Kirkus listing age limit of the pre-collected result a run may read, in days (1 to 14).",
    ),
    RankingMetadataFieldSpec(
        "kirkus_max_review_age_days",
        "int",
        probe_option="--kirkus-max-review-age-days",
        probe_dest="kirkus_max_review_age_days",
        probe_help="Kirkus listing age limit of a review at the pass that listed it, in days (1 to 366).",
    ),
    RankingMetadataFieldSpec(
        "kirkus_genre_labels",
        "list",
        repeatable=True,
        probe_option="--kirkus-genre-label",
        probe_dest="kirkus_genre_labels",
        probe_scope="first",
        probe_help="Kirkus genre label a listing card must carry to be collected. Repeatable.",
    ),
    # Issue #945: the PM deals source reads the result of its own pass and is never probed.
    RankingMetadataFieldSpec("pm_max_collection_age_days", "int"),
    RankingMetadataFieldSpec("pm_category_labels", "list", repeatable=True),
    # Issue #948: the optional minimum deal size band; absent means every deal passes.
    RankingMetadataFieldSpec("pm_min_deal_size_band", "string"),
    # Issue #955: the PW issue reviews source reads the result of its own pass and is never probed.
    RankingMetadataFieldSpec("pw_max_issue_age_days", "int"),
    RankingMetadataFieldSpec("pw_excluded_subsections", "list", repeatable=True),
    # Issue #577: the bounded annual award list. These declare a year, a
    # category heading, and a row format instead of a reporting period.
    RankingMetadataFieldSpec(
        "award_year",
        "int",
        probe_option="--award-year",
        probe_dest="award_years",
        probe_help="Award-list edition year the page must state.",
    ),
    RankingMetadataFieldSpec(
        "award_year_selector",
        "string",
        probe_option="--award-year-selector",
        probe_dest="award_year_selectors",
        probe_help="Optional class selector of the one element stating the award year. Default: visible headings.",
    ),
    RankingMetadataFieldSpec(
        "award_category_heading",
        "string",
        probe_option="--award-category-heading",
        probe_dest="award_category_headings",
        probe_help="Exact visible heading that bounds the award-list rows.",
    ),
    RankingMetadataFieldSpec(
        "award_row_selector",
        "string",
        probe_option="--award-row-selector",
        probe_dest="award_row_selectors",
        probe_help="Award-list row element: li, p, tr, dd, div, or a .class selector.",
    ),
    RankingMetadataFieldSpec(
        "award_row_format",
        "string",
        probe_option="--award-row-format",
        probe_dest="award_row_formats",
        probe_help="Award-list row written with placeholders, for example '{title}, {author} ({ignored})'.",
    ),
    RankingMetadataFieldSpec(
        "award_expected_row_count",
        "int",
        probe_option="--award-expected-row-count",
        probe_dest="award_expected_row_counts",
        probe_help="Exact number of rows under the award category heading.",
    ),
    RankingMetadataFieldSpec(
        "award_author_conjunction",
        "string",
        probe_option="--award-author-conjunction",
        probe_dest="award_author_conjunctions",
        probe_help="Optional word this award page puts between the last two author names, for example 'and'.",
    ),
    RankingMetadataFieldSpec(
        "award_row_split_first",
        "bool",
        probe_option="--award-row-split-first",
        probe_dest="award_row_split_firsts",
        probe_help="Explicit award-list rule: when a row repeats a separator, the first one divides title and author.",
    ),
    # Issue #578: one award page is one source, whatever it lists.
    RankingMetadataFieldSpec(
        "award_additional_category_headings",
        "list",
        repeatable=True,
        probe_option="--award-additional-category-heading",
        probe_dest="award_additional_category_headings",
        probe_scope="first",
        probe_help="Exact visible heading of one more category of the same award page. Repeatable.",
    ),
    RankingMetadataFieldSpec(
        "award_row_title_element",
        "string",
        probe_option="--award-row-title-element",
        probe_dest="award_row_title_elements",
        probe_help="Optional emphasis element each award row sets its title in: em, i, cite, b, or strong.",
    ),
    # Issue #795: a page may label its categories with bold paragraphs.
    RankingMetadataFieldSpec(
        "award_category_label_element",
        "string",
        probe_option="--award-category-label-element",
        probe_dest="award_category_label_elements",
        probe_help="Optional bold element a paragraph that labels an award category is set in: b or strong.",
    ),
    RankingMetadataFieldSpec("metadata_ranking_table", "bool"),
    RankingMetadataFieldSpec(
        "ranking_head_404_get_fallback",
        "bool",
        probe_option="--ranking-head-404-get-fallback",
        probe_dest="ranking_head_404_get_fallback",
        probe_help="Explicit ranking-source exception: one bounded GET after HEAD 404. Other denied statuses remain refused.",
    ),
    RankingMetadataFieldSpec(
        "ranking_author_evidence_json",
        "string",
        probe_option="--ranking-author-evidence-json",
        probe_dest="ranking_author_evidence_json",
        probe_help="Retained DNB ISBN author-check JSON, used only for a missing row author. No network lookup during probe.",
    ),
    RankingMetadataFieldSpec(
        "ranking_row_selector", "string", probe_option="--ranking-row-selector", probe_dest="ranking_row_selectors"
    ),
    RankingMetadataFieldSpec(
        "ranking_rank_source",
        "string",
        probe_option="--ranking-rank-source",
        probe_dest="ranking_rank_sources",
        probe_choices=RANKING_RANK_SOURCES,
        probe_help="Ranking rank source, selector or one-based document order, aligned by URL order. Repeatable.",
    ),
    RankingMetadataFieldSpec(
        "ranking_rank_selector", "string", probe_option="--ranking-rank-selector", probe_dest="ranking_rank_selectors"
    ),
    RankingMetadataFieldSpec(
        "ranking_title_selector",
        "string",
        probe_option="--ranking-title-selector",
        probe_dest="ranking_title_selectors",
    ),
    RankingMetadataFieldSpec(
        "ranking_author_selector",
        "string",
        probe_option="--ranking-author-selector",
        probe_dest="ranking_author_selectors",
    ),
    RankingMetadataFieldSpec(
        RANKING_AUTHOR_OPTIONAL_FIELD,
        "bool",
        probe_option="--ranking-author-optional",
        probe_dest="ranking_author_optional",
        probe_help="Declare that a bounded ranking row may legitimately carry no author.",
    ),
    RankingMetadataFieldSpec(
        RANKING_REQUEST_METHOD_FIELD,
        "string",
        probe_option="--ranking-request-method",
        probe_dest="ranking_request_methods",
        probe_choices=RANKING_REQUEST_METHODS,
        probe_help="How the ranking page is requested. POST needs --ranking-request-field. Aligned by URL order.",
    ),
    RankingMetadataFieldSpec(
        RANKING_REQUEST_PAGE_FIELD,
        "string",
        probe_option="--ranking-request-page-field",
        probe_dest="ranking_request_page_fields",
        probe_help="Declared form field carrying the 1-based page number. Aligned by URL order.",
    ),
    RankingMetadataFieldSpec(
        RANKING_REQUEST_MAX_PAGES_FIELD,
        "int",
        probe_option="--ranking-request-max-pages",
        probe_dest="ranking_request_max_pages",
        probe_help="Hard cap on declared ranking pages. Aligned by URL order.",
    ),
    RankingMetadataFieldSpec(
        RANKING_RESPONSE_LAST_PAGE_FIELD,
        "string",
        probe_option="--ranking-response-last-page-field",
        probe_dest="ranking_response_last_page_fields",
        probe_help="JSON envelope boolean field that is true on the final page. Aligned by URL order.",
    ),
    RankingMetadataFieldSpec(
        RANKING_IDENTITY_URL_TEMPLATE_FIELD,
        "string",
        probe_option="--ranking-identity-url-template",
        probe_dest="ranking_identity_url_templates",
        probe_help="URL template of the page whose heading proves the list identity. Aligned by URL order.",
    ),
    RankingMetadataFieldSpec(
        RANKING_IDENTITY_FIELDS_FIELD,
        "list",
        repeatable=True,
        probe_option="--ranking-identity-field",
        probe_dest="ranking_identity_fields",
        probe_scope="first",
        probe_help="One 'placeholder=envelope_field' mapping for the identity URL template. Repeatable.",
    ),
    RankingMetadataFieldSpec(
        RANKING_RESPONSE_HTML_FIELD,
        "string",
        probe_option="--ranking-response-html-field",
        probe_dest="ranking_response_html_fields",
        probe_help="JSON envelope field holding the ranking HTML. Aligned by URL order. Repeatable.",
    ),
    RankingMetadataFieldSpec(
        RANKING_REQUEST_BODY_FIELD,
        "list",
        repeatable=True,
        probe_option="--ranking-request-field",
        probe_dest="ranking_request_body",
        probe_scope="first",
        probe_help="One declared 'name=value' form field for a ranking POST route. Repeatable.",
    ),
    RankingMetadataFieldSpec(
        "ranking_identifier_selector",
        "string",
        probe_option="--ranking-identifier-selector",
        probe_dest="ranking_identifier_selectors",
    ),
    RankingMetadataFieldSpec(
        "ranking_identifier_attribute",
        "string",
        probe_option="--ranking-identifier-attribute",
        probe_dest="ranking_identifier_attributes",
    ),
    RankingMetadataFieldSpec(
        "ranking_identifier_pattern",
        "string",
        probe_option="--ranking-identifier-pattern",
        probe_dest="ranking_identifier_patterns",
    ),
    RankingMetadataFieldSpec(
        "ranking_category_marker",
        "string",
        probe_option="--ranking-category-marker",
        probe_dest="ranking_category_markers",
    ),
    RankingMetadataFieldSpec(
        "ranking_reporting_date_selector",
        "string",
        probe_option="--ranking-reporting-date-selector",
        probe_dest="ranking_reporting_date_selectors",
        probe_help="One visible page element carrying the list's single reporting date; no heading fallback.",
    ),
    RankingMetadataFieldSpec(
        "ranking_reporting_period_pattern",
        "string",
        probe_option="--ranking-reporting-period-pattern",
        probe_dest="ranking_reporting_period_patterns",
    ),
    RankingMetadataFieldSpec(
        "ranking_reporting_period_date_format",
        "string",
        probe_option="--ranking-reporting-period-date-format",
        probe_dest="ranking_reporting_period_date_formats",
    ),
    RankingMetadataFieldSpec(
        "ranking_reporting_period_max_age_days",
        "int",
        probe_option="--ranking-reporting-period-max-age-days",
        probe_dest="ranking_reporting_period_max_age_days",
    ),
    RankingMetadataFieldSpec(
        RANKING_UNDATED_FRESHNESS_FIELD,
        "bool",
        probe_option="--ranking-reporting-period-undated",
        probe_dest="ranking_reporting_period_undated",
        probe_help="Declare that the ranking page prints no date, so freshness comes from row movement.",
    ),
    RankingMetadataFieldSpec(
        "ranking_expected_raw_row_count",
        "int",
        probe_option="--ranking-expected-raw-row-count",
        probe_dest="ranking_expected_raw_row_counts",
    ),
    RankingMetadataFieldSpec(
        "ranking_min_raw_row_count",
        "int",
        probe_option="--ranking-min-raw-row-count",
        probe_dest="ranking_min_raw_row_counts",
    ),
    RankingMetadataFieldSpec(
        "ranking_max_raw_row_count",
        "int",
        probe_option="--ranking-max-raw-row-count",
        probe_dest="ranking_max_raw_row_counts",
    ),
    RankingMetadataFieldSpec(
        "ranking_accept_incomplete",
        "bool",
        probe_help="Accept a short but non-empty ranking capture as incomplete with a visible warning instead of rejecting it.",
    ),
    RankingMetadataFieldSpec(
        "ranking_evidence_url", "string", probe_option="--ranking-evidence-url", probe_dest="ranking_evidence_urls"
    ),
    RankingMetadataFieldSpec(
        "adult_nonfiction_exclusions",
        "list",
        repeatable=True,
        probe_option="--ranking-exclusion-term",
        probe_dest="adult_nonfiction_exclusions",
        probe_scope="first",
        probe_help="Explicit adult-nonfiction title term excluded by the ranking-table contract. Repeatable.",
    ),
    RankingMetadataFieldSpec(
        "ranking_detail_link_selector",
        "string",
        detail_route=True,
        probe_option="--ranking-detail-link-selector",
        probe_dest="ranking_detail_link_selectors",
    ),
    RankingMetadataFieldSpec(
        "ranking_detail_url_pattern",
        "string",
        detail_route=True,
        probe_option="--ranking-detail-url-pattern",
        probe_dest="ranking_detail_url_patterns",
    ),
    RankingMetadataFieldSpec(
        "ranking_detail_scope_selector",
        "string",
        detail_route=True,
        probe_option="--ranking-detail-scope-selector",
        probe_dest="ranking_detail_scope_selectors",
    ),
    RankingMetadataFieldSpec(
        "ranking_detail_fields",
        "list",
        repeatable=True,
        detail_route=True,
        probe_option="--ranking-detail-fields",
        probe_dest="ranking_detail_fields",
        probe_scope="first",
    ),
    RankingMetadataFieldSpec(
        "ranking_detail_identifier_selector",
        "string",
        detail_route=True,
        probe_option="--ranking-detail-identifier-selector",
        probe_dest="ranking_detail_identifier_selectors",
    ),
    RankingMetadataFieldSpec(
        "ranking_detail_identifier_attribute",
        "string",
        detail_route=True,
        probe_option="--ranking-detail-identifier-attribute",
        probe_dest="ranking_detail_identifier_attributes",
    ),
    RankingMetadataFieldSpec(
        "ranking_detail_identifier_pattern",
        "string",
        detail_route=True,
        probe_option="--ranking-detail-identifier-pattern",
        probe_dest="ranking_detail_identifier_patterns",
    ),
    RankingMetadataFieldSpec(
        "ranking_detail_category_selector",
        "string",
        detail_route=True,
        probe_option="--ranking-detail-category-selector",
        probe_dest="ranking_detail_category_selectors",
    ),
    RankingMetadataFieldSpec(
        "ranking_detail_category_pattern",
        "string",
        detail_route=True,
        probe_option="--ranking-detail-category-pattern",
        probe_dest="ranking_detail_category_patterns",
    ),
    RankingMetadataFieldSpec(
        "ranking_detail_category_allowlist",
        "list",
        repeatable=True,
        detail_route=True,
        probe_option="--ranking-detail-category-allowlist",
        probe_dest="ranking_detail_category_allowlist",
        probe_scope="first",
    ),
    RankingMetadataFieldSpec(
        "ranking_detail_max_pages",
        "int",
        detail_route=True,
        probe_option="--ranking-detail-max-pages",
        probe_dest="ranking_detail_max_pages",
    ),
    # Issue #1163: how many detail pages one capture may lose to a fetch,
    # redirect or scope failure before the whole source fails. Absent means 0,
    # so every route that does not declare it stays strict.
    RankingMetadataFieldSpec(
        "ranking_detail_max_failed_pages",
        "int",
        detail_route=True,
    ),
)
RANKING_TABLE_FIELDS: tuple[str, ...] = tuple(spec.name for spec in RANKING_TABLE_FIELD_SPECS)
RANKING_DETAIL_CONFIG_FIELDS: tuple[str, ...] = tuple(
    spec.name for spec in RANKING_TABLE_FIELD_SPECS if spec.detail_route
)
RANKING_FIELD_SPECS_BY_NAME: dict[str, RankingMetadataFieldSpec] = {
    spec.name: spec for spec in RANKING_TABLE_FIELD_SPECS
}

# Operator-supplied fields for the offline probation-source scaffold. The
# scaffold maps ``source_name`` to the portfolio ``name`` field, while the
# remaining fields are copied into the canonical source metadata. Forced
# lifecycle and contract values are intentionally not operator choices.
# ``expected_sort_label`` is optional because not every exact list exposes a
# stable sort marker. Keep the complete vocabulary as a separate tuple so CLI
# and metadata validation can share the same field names.
SOURCE_SCAFFOLD_OPTIONAL_METADATA_FIELDS: tuple[str, ...] = ("expected_sort_label",)
SOURCE_SCAFFOLD_REQUIRED_METADATA_FIELDS: tuple[str, ...] = (
    "source_name",
    "url",
    "family",
    "access_status",
    "check_frequency",
    *SOURCE_METADATA_FIELDS,
    "expected_page_heading",
    "expected_signal_strength",
    "eligible_candidate_category",
    "lifecycle_note",
    "notes",
)
SOURCE_SCAFFOLD_METADATA_FIELDS: tuple[str, ...] = (
    *SOURCE_SCAFFOLD_REQUIRED_METADATA_FIELDS,
    *SOURCE_SCAFFOLD_OPTIONAL_METADATA_FIELDS,
)

EXACT_BOOK_LIST_FIELDS: tuple[str, ...] = (
    "metadata_exact_book_list",
    "expected_signal_strength",
    "eligible_candidate_category",
    "entry_section_heading",
)
AUDIBLE_EXPLICIT_CHART_FIELDS: tuple[str, ...] = (
    "audible_explicit_chart",
    "expected_range_marker",
)
AMAZON_CHARTS_FIELDS: tuple[str, ...] = (
    "amazon_charts_weekly",
    "chart_path_segment",
    "author_marker",
)
SOURCE_TRANSPORT_FIELDS: tuple[str, ...] = ("max_response_bytes",)

SOURCE_LIFECYCLE_FIELDS: tuple[str, ...] = ("lifecycle_status", "lifecycle_note")
SOURCE_ARTIFACT_METADATA_FIELDS: tuple[str, ...] = (
    "headline_card_titles",
    "children_byline_markers",
    "eligibility_outside_knowledge",
    "family",
    "metadata_only",
    "check_frequency",
    "expected_table_headers",
    "expected_visible_markers",
    "forbidden_visible_markers",
    *SOURCE_METADATA_FIELDS,
    *SOURCE_PAGE_IDENTITY_FIELDS,
    *SOURCE_LAYOUT_FIELDS,
    *SOURCE_CONTRACT_FIELDS,
    *SOURCE_HELPER_METADATA_FIELDS,
    *RANKING_TABLE_FIELDS,
    *EXACT_BOOK_LIST_FIELDS,
    *AUDIBLE_EXPLICIT_CHART_FIELDS,
    *AMAZON_CHARTS_FIELDS,
    *SOURCE_TRANSPORT_FIELDS,
    *SOURCE_LIFECYCLE_FIELDS,
    # Issue #700: the declarations that make an empty extraction legitimate.
    # The empty-extraction retry reads them off the work item, so they must
    # travel with it; without them every legitimately empty source is
    # retried once a week for a guaranteed zero.
    "may_report_no_entries",
    "may_report_no_entries_reason",
    # Issue #763: the declared age limit has to reach replay and finalize, or
    # the week is aged at capture and accepted unaged everywhere after it.
    "source_week_max_age_days",
    "seen_guids_file",
    "seen_guids_fixture_file",
    # The extraction worklist must retain the configured model input budget.
    "max_model_input_chars",
)


def declared_source_metadata(source: dict[str, Any]) -> dict[str, Any]:
    """Return explicitly declared source and contract metadata."""
    fields = (
        *SOURCE_METADATA_FIELDS,
        *SOURCE_CONTRACT_FIELDS,
        *RANKING_TABLE_FIELDS,
        "headline_card_titles",
        "children_byline_markers",
        "eligibility_outside_knowledge",
    )
    return {field: source[field] for field in fields if field in source}


def portable_source_metadata(
    source: dict[str, Any],
    *,
    helper_metadata_file: str | None = None,
    helper_metadata_sha256: str | None = None,
) -> dict[str, Any]:
    """Return canonical and extended fields needed across capture/finalize.

    The configured helper path is resolved from the input config and is not
    portable into a run output directory. Callers that have copied the
    validated sidecar may supply its output-relative filename and digest.
    """
    metadata = {
        field: source[field]
        for field in SOURCE_ARTIFACT_METADATA_FIELDS
        if field in source and field != "helper_metadata_file"
    }
    if helper_metadata_file:
        metadata["helper_metadata_file"] = helper_metadata_file
    if helper_metadata_sha256:
        metadata["helper_metadata_sha256"] = helper_metadata_sha256
    return metadata
