"""Name the reason a Configured Source contributed nothing (Issue #464).

A source that fails is visible: it carries a status and a reason, and the
editor reads it. A source that succeeds and yields nothing was invisible.
Nothing in the record told apart a page that genuinely listed nothing new
from an extraction that produced nothing from a page full of books, and
``run-summary.json`` counted both as healthy.

An empty result is not evidence of an empty page.

This module decides one question: given a source that raised no error and
produced no mention, is that outcome correct or is it a defect, and what is
the reason. Every answer carries a reason. An empty reason is never returned.

The axis is what the source publishes, not what it is called:

* A source that reports only items it has not seen before declares
  ``seen_guids_file``. Zero new items in a week is its normal state.
* Every other source publishes a complete list on every check. A chart, a
  bestseller list, or a bounded recommendation list with no entry is broken,
  because the page it reads republishes its whole list each time.
* A source that publishes a complete list but may legitimately stand empty
  between announcements, such as an annual award page before its winners are
  named, declares that with a recorded reason. The declaration is explicit so
  that turning a correct empty result into a failure never trains the
  operator to ignore failures.

What this cannot catch is stated in the module that needs it: the seam has
the counts each stage produced, not the page itself, so a page that held
rows and an extractor that returned an empty list are told apart by which
stage lost the entries, not by re-reading the capture. A bounded ranking page
that lost its rows is already refused earlier, by the row-selector check in
:func:`autoscout.source_page_identity.validate_page_shape`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# The seen-item ledger is named by one field in a live config and by another in
# a replay config. Reading only one of them made the same source incremental in
# one config and full-list in the other, which is exactly the disagreement a
# single seam exists to prevent.
INCREMENTAL_FEED_FIELDS = ("seen_guids_file", "seen_guids_fixture_file")
INCREMENTAL_FEED_FIELD = INCREMENTAL_FEED_FIELDS[0]
NO_ENTRIES_ALLOWED_FIELD = "may_report_no_entries"
NO_ENTRIES_REASON_FIELD = "may_report_no_entries_reason"

NO_NEW_ITEMS_REASON = "no item newer than the previous run; this source reports only items it has not seen before"
NO_ENTRY_EXTRACTED_REASON = "extraction produced no entry from a source that publishes a complete list on every check"
# Issue #700 part A: the single reason above hid four unrelated conditions
# behind one string. Each reason below names a condition the seam can
# actually detect from the evidence it receives; NO_ENTRY_EXTRACTED_REASON is
# retained only so older run records remain readable, and is no longer
# produced. Two residual ambiguities are documented, not guessed away: a
# genuinely empty undeclared page shares its signature with a persistently
# failed extraction (both read as AGENT_EMPTY_AFTER_RETRY), and a contentful
# shell capture reads the same way (content probes cannot separate them:
# BookPage recovered 11 entries from fewer visible-text chars than the
# Audiobooks shell holds, and Die Zeit recovered 1 from fewer still).
# Defensive: a configured source always reaches this seam with a URL, because
# the portfolio carries it and overlays merge on top (config.resolve_weekly_
# portfolio_or_raise). This does not describe the PW Best Books / PublishNews
# failures — those configure a URL that resolves; they are pinned to a stale
# edition and a stale week, which no field here can detect. Stale-edition
# detection is #680's audit, and until it lands those two report as
# extraction-attributed, which is where the run record leaves them today.
NO_URL_CONFIGURED_REASON = (
    "source has no URL configured, so nothing was fetched for a source that publishes a complete list on every check"
)
# A site-root URL is an observation appended to the attributed reason, never
# a reason of its own. A root path is not evidence of misconfiguration:
# Deutscher Sachbuchpreis reads its current edition from a stable landing
# page deliberately (#679 settled that changing the URL is *not* the fix),
# and TodosTusLibros is wrongly pointed at a homepage — yet both declare
# url_stability "stable", so no configured field separates them. Reporting
# the root shape as the cause would misattribute a real extraction failure
# on a correctly configured landing page, so it is stated as something to
# check beside the reason the seam can actually stand behind.
SITE_ROOT_URL_NOTE = "; the configured URL is the site root ({url}), which may not be the declared list page"
EMPTY_CAPTURE_REASON = "the capture held no page content ({size} bytes); extraction had nothing to read"
AGENT_EMPTY_AFTER_RETRY_REASON = (
    "extraction returned no entries on both attempts from a capture holding {size}; "
    "inspect the capture to tell an empty page from a missed extraction"
)
AGENT_EMPTY_SINGLE_ATTEMPT_REASON = (
    "extraction returned no entries from a capture holding {size} (no retry ran in this path)"
)
ALL_ENTRIES_REFUSED_REASON = "extraction produced {extracted} entries and the contract refused every one"
NO_ADMISSIBLE_MENTION_REASON = "the collector produced {valid} mentions and none was admissible as a source mention"


def source_reports_new_items_only(source: Mapping[str, Any]) -> bool:
    """Say whether a source reports only items it has not seen before.

    The declaration is the seen-item ledger the collector keeps. A source that
    keeps one reports a difference against the previous run, so producing
    nothing means the page published nothing new, not that the read failed.
    """
    return any(str(source.get(field, "")).strip() for field in INCREMENTAL_FEED_FIELDS)


def source_may_report_no_entries(source: Mapping[str, Any]) -> bool:
    """Say whether a full-list source declared that it may stand empty."""
    return source.get(NO_ENTRIES_ALLOWED_FIELD) is True


def declared_no_entries_reason(source: Mapping[str, Any]) -> str:
    return str(source.get(NO_ENTRIES_REASON_FIELD, "")).strip()


def validate_no_entries_declaration(source: Mapping[str, Any]) -> str:
    """Return an error naming an unusable empty-list declaration, or ``""``.

    The reason is required. A silent permission to contribute nothing is the
    very thing this issue removes, so a declaration without a recorded reason
    is refused rather than honoured.
    """
    declared = source.get(NO_ENTRIES_ALLOWED_FIELD)
    reason = declared_no_entries_reason(source)
    if declared is None:
        if reason:
            return f"{NO_ENTRIES_REASON_FIELD} requires {NO_ENTRIES_ALLOWED_FIELD}"
        return ""
    if declared is not True:
        return f"{NO_ENTRIES_ALLOWED_FIELD} must be true when it is declared"
    if not reason:
        return f"{NO_ENTRIES_ALLOWED_FIELD} requires a recorded {NO_ENTRIES_REASON_FIELD}"
    if source_reports_new_items_only(source):
        return (
            f"{NO_ENTRIES_ALLOWED_FIELD} does not apply to a source that declares "
            f"{INCREMENTAL_FEED_FIELD}; such a source already reports only new items"
        )
    return ""


def _drop_reason_summary(debug_metadata: Mapping[str, Any]) -> str:
    reasons = debug_metadata.get("drop_reasons") or []
    named = sorted({str(reason).strip() for reason in reasons if str(reason).strip()})
    if not named:
        return ""
    return "; refused as " + ", ".join(named)


def _has_usable_url(source: Mapping[str, Any]) -> bool:
    """Say whether the source configures a URL worth fetching."""
    return bool(str(source.get("url", "")).strip())


def _is_site_root_url(source: Mapping[str, Any]) -> bool:
    """Say whether the configured URL has no page path.

    Reported as an observation, not a cause: a stable landing page that
    presents its current edition is a legitimate configuration (#679), and
    it is indistinguishable here from a source wrongly pointed at a
    homepage. An unparseable URL is not claimed to be a root."""
    from urllib.parse import urlsplit

    try:
        path = urlsplit(str(source.get("url", "")).strip()).path
    except ValueError:
        return False
    return path in ("", "/")


def _site_root_note(source: Mapping[str, Any]) -> str:
    """Append the site-root observation when the configured URL has no path."""
    if not _is_site_root_url(source):
        return ""
    return SITE_ROOT_URL_NOTE.format(url=str(source.get("url", "")).strip())


EXTRACTOR_REASON_NOTE = '; the extractor said: "{reason}"'


def _extractor_reason_note(debug_metadata: Mapping[str, Any]) -> str:
    """Append the extractor's own reason for returning nothing (Issue #740).

    It is the extractor's claim, quoted, not a verdict: an abstention that
    names a reason the capture contradicts is exactly what this surfaces.
    """
    reason = debug_metadata.get("extractor_empty_reason")
    if not isinstance(reason, str) or not reason.strip():
        return ""
    return EXTRACTOR_REASON_NOTE.format(reason=reason.strip())


def _capture_size_label(debug_metadata: Mapping[str, Any]) -> str:
    """Describe the capture size the collector evidenced, honestly."""
    size = debug_metadata.get("capture_content_bytes", debug_metadata.get("capture_html_bytes"))
    if isinstance(size, bool):
        return "unknown size"
    if isinstance(size, int) and size >= 0:
        return f"{size} bytes"
    return "unknown size"


def should_retry_empty_extraction(source: Mapping[str, Any], *, extracted_count: int) -> bool:
    """Say whether an extraction that produced nothing deserves one retry.

    Extraction is non-deterministic (issue #700): replaying preserved
    captures from run 4090c59a returned a full list where the run itself
    returned none, against byte-identical input and an exit code of 0.

    The retry fires exactly when the zero would otherwise be attributed to
    extraction (the AGENT_EMPTY reasons below): a source that reports only
    new items, that declared it may stand empty, or that configures no URL
    reproduces zero every time, so retrying it buys a second extraction
    weekly for a guaranteed zero.

    A site-root URL is deliberately *not* refused here. Deutscher
    Sachbuchpreis and Magnesia Litera read their current edition from a
    stable landing page by design, and refusing the retry for a root path
    would withdraw #701's recovery from two working sources to spare one
    misconfigured one. A shell capture behind a deep URL likewise still
    costs one bounded retry: no content probe can detect it (see the module
    note on ``capture_content_evidence``).
    """
    if extracted_count > 0:
        return False
    if source_reports_new_items_only(source):
        return False
    if source_may_report_no_entries(source):
        return False
    return _has_usable_url(source)


def zero_contribution_outcome(
    source: Mapping[str, Any],
    *,
    extracted_count: int,
    valid_mention_count: int,
    admissible_mention_count: int,
    debug_metadata: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """Name the status and reason for a source that produced no mention.

    ``extracted_count`` is what extraction returned, ``valid_mention_count``
    what the collector kept, and ``admissible_mention_count`` what became a
    source mention. Naming the stage that lost the entries is the whole point:
    a page that listed nothing and a contract that refused everything are
    different defects and must not read the same.
    """
    metadata = debug_metadata or {}

    if valid_mention_count > 0 and admissible_mention_count == 0:
        return "failed", NO_ADMISSIBLE_MENTION_REASON.format(valid=valid_mention_count)

    if extracted_count > 0:
        return "failed", ALL_ENTRIES_REFUSED_REASON.format(extracted=extracted_count) + _drop_reason_summary(metadata)

    if source_reports_new_items_only(source):
        return "collected", NO_NEW_ITEMS_REASON

    if source_may_report_no_entries(source):
        return "collected", f"no entry published; {declared_no_entries_reason(source)}"

    if not _has_usable_url(source):
        return "failed", NO_URL_CONFIGURED_REASON

    note = _site_root_note(source)

    size = metadata.get("capture_content_bytes", metadata.get("capture_html_bytes"))
    if isinstance(size, int) and not isinstance(size, bool) and size == 0:
        return "failed", EMPTY_CAPTURE_REASON.format(size="0 bytes") + note

    label = _capture_size_label(metadata)
    note += _extractor_reason_note(metadata)
    if metadata.get("empty_extraction_retry") is True and metadata.get("empty_extraction_recovered") is not True:
        return "failed", AGENT_EMPTY_AFTER_RETRY_REASON.format(size=label) + note
    return "failed", AGENT_EMPTY_SINGLE_ATTEMPT_REASON.format(size=label) + note
