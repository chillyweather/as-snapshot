"""Issue #337: mutually grounded replay fixtures from retained probe evidence.

Scope: turn retained probe evidence (``probe-source-candidate --keep-captures``)
for one accepted exact-book-list Source Candidate into a replay fixture pair:

* a codex-capture page fixture whose HTML is a synthesized bounded page that
  contains only the representative metadata derived from the retained parse
  (detected heading, exact visible titles, primary authors, and book-level
  links), never the original page prose; and
* a codex-extraction-replay envelope whose canonical ``mentions`` array
  carries the exact visible titles, primary authors, grounded evidence URLs,
  the configured signal values, and the required configured candidate
  category, bound to the generated capture via a deterministic
  ``capture_sha256`` and a self-contained ``operation_id``.

Replay evidence is deliberately bounded: at most ``MAX_REPLAY_ENTRIES`` (three)
representative entries are selected, in document order, however long the
bounded source list is. The retained helper metadata sidecar must therefore
carry exactly those selected entries, in the same order, and nothing else.

The command is fully offline: it consumes one retained probe capture and never
fetches the network again. All validation and representative selection reuse
the Issue #334 policy seam (``evaluate_captured_source_evidence``), so the
acceptance verdict, the representative entries, and the grounding checks all
come from the same parser run over the retained HTML. It never writes weekly
state, configuration, Candidate History, Source Mentions, or Scouting
Candidates.
"""

from __future__ import annotations

import hashlib
import html as _html
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.capture_sanitization import sanitize_captured_html
from autoscout.helper_metadata import HELPER_METADATA_FIELDS
from autoscout.helper_metadata import validate_helper_metadata
from autoscout.http_decoding import MAX_RESPONSE_BYTES
from autoscout.json_io import read_json
from autoscout.ranking_author_policy import ranking_author_optional_declared
from autoscout.ranking_detail_route import RankingDetailRoute
from autoscout.ranking_detail_route import ranking_detail_fields_from_html
from autoscout.ranking_evidence import RankingCaptureError
from autoscout.ranking_evidence import RankingPageEvidence
from autoscout.ranking_freshness import ranking_undated_declared
from autoscout.ranking_identity import ranking_identity_declared
from autoscout.ranking_request import ranking_request_from_source
from autoscout.ranking_request import validate_ranking_request_declaration
from autoscout.ranking_response import RANKING_RESPONSE_HTML_FIELD
from autoscout.ranking_response import RANKING_RESPONSE_LAST_PAGE_FIELD
from autoscout.ranking_response import unwrap_ranking_response
from autoscout.ranking_response import validate_ranking_response_declaration
from autoscout.safe_artifacts import SafeArtifactError
from autoscout.safe_artifacts import pair_write_lock
from autoscout.safe_artifacts import publish_pair
from autoscout.safe_artifacts import resolve_storage_destination
from autoscout.source_collectors.base import evidence_url_verified
from autoscout.source_feasibility import OUTCOME_ACCEPT
from autoscout.source_feasibility import EntrySample
from autoscout.source_feasibility import FeasibilityVerdict
from autoscout.source_feasibility import evaluate_source_feasibility
from autoscout.source_feasibility import validate_retained_summary
from autoscout.source_language import is_english_language
from autoscout.source_page_identity import exact_book_list_mention_is_grounded
from autoscout.source_page_identity import parse_visible_page_identity
from autoscout.source_page_identity import ranking_freshness_declared
from autoscout.source_page_identity import ranking_mention_is_grounded
from autoscout.source_page_identity import ranking_page_identity
from autoscout.source_page_identity import ranking_raw_row_count_error
from autoscout.source_page_identity import ranking_rows
from autoscout.source_page_identity import validate_page_shape
from autoscout.source_page_identity import visible_title_selector_classes
from autoscout.source_probe import normalized_url
from autoscout.source_week_freshness import lenient_reference_date

DEFAULT_OUTPUT_DIR = Path("data/replay-fixtures")
PROBE_COLLECTION_MODE = "source-candidate-probe"
REPLAY_COLLECTION_MODE = "codex-capture"
# The retained probe capture records at most MAX_SAMPLES grounded samples, and
# the representative extraction is capped at exactly that bound (Issue #334).
MAX_REPLAY_ENTRIES = 3
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
# Split semicolons and slashes used as author separators only when the slash
# touches whitespace, so ordinary names such as AC/DC remain intact.
_AUTHOR_DISPLAY_SEPARATOR_RE = re.compile(r";|(?<!\S)/|/(?=\s|$)")
# Concise metadata-only evidence note: it describes the page evidence without
# copying any title, author, or prose from the source page.
EVIDENCE_NOTE = "Exact-list entry on the captured source page: linked title with visible primary author."
_HELPER_METADATA_ENTRY_FIELDS = frozenset({"title", "author", "evidence_url", *HELPER_METADATA_FIELDS})
_HELPER_METADATA_TOP_LEVEL_FIELDS = frozenset({"version", "source_url", "language", "entries"})
# Correctable helper-sidecar input classes. The offline onboarding driver maps
# each one to a specific operator reason code and one concrete next action, so
# a disagreeing sidecar is never reported as an unexpected failure (#409).
REASON_HELPER_MISSING = "helper_metadata_missing"
REASON_HELPER_UNREADABLE = "helper_metadata_unreadable"
REASON_HELPER_SCHEMA_INVALID = "helper_metadata_schema_invalid"
REASON_HELPER_ENTRY_INVALID = "helper_metadata_entry_invalid"
REASON_HELPER_DUPLICATE_ENTRY = "helper_metadata_duplicate_entry"
REASON_HELPER_IDENTITY_MISMATCH = "helper_metadata_identity_mismatch"
REASON_REPRESENTATIVE_ENTRIES_MISSING = "representative_entries_missing"


class ReplayFixtureError(ValueError):
    """A replay fixture pair cannot be generated or written safely.

    *reason_code* names the correctable input class when one exists, so a
    caller that reports structured exit codes (the offline ``add-source``
    driver) can turn a generation refusal into a specific operator reason
    instead of a generic unexpected failure (Issue #409)."""

    def __init__(self, message: str, *, reason_code: str = "") -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class ReplayFixturePair:
    """One fully generated, mutually grounded fixture pair.

    *page_bytes* and *extraction_bytes* are the exact file bytes that the
    writer publishes, so an exact retry can compare existing files against
    them byte for byte and a conflicting retry can be refused without
    touching the existing pair. *capture_sha256* is the digest of
    *page_bytes* and is embedded in the extraction envelope."""

    url: str
    slug: str
    page: dict[str, Any]
    extraction: dict[str, Any]
    page_bytes: bytes
    extraction_bytes: bytes
    capture_sha256: str
    operation_id: str
    entry_count: int


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__


def _url_hash(url: str) -> str:
    """Return the 8-char URL disambiguator shared with capture naming."""
    return hashlib.sha256(normalized_url(url).encode("utf-8")).hexdigest()[:8]


def _output_names(slug: str, url: str) -> tuple[str, str]:
    """Standard naming style for the pair, driven by the explicit slug.

    The style mirrors ``codex_capture_filename`` / ``codex_extraction_slug``
    (slug plus the 8-char URL hash) with the operator-supplied slug in place
    of the source-name slug."""
    url_hash = _url_hash(url)
    return f"codex-capture-{slug}-{url_hash}.json", f"{slug}-{url_hash}-codex-extraction.json"


def _identifier_digits(value: str) -> str:
    return "".join(character for character in value if character.isdigit())


def _placeholder_isbn(position: int, reserved_identifiers: set[str]) -> str:
    """Return a deterministic valid ISBN-13 that is not a representative ID."""
    candidate = max(1, position)
    while candidate <= 999_999_999:
        body = f"979{candidate:09d}"
        checksum = sum((1 if index % 2 == 0 else 3) * int(value) for index, value in enumerate(body))
        identifier = body + str((10 - checksum % 10) % 10)
        if identifier not in reserved_identifiers:
            return identifier
        candidate += 1
    raise ReplayFixtureError("cannot allocate a deterministic placeholder ISBN")


def _declared_row_floor(metadata: Mapping[str, Any]) -> int:
    """Return the smallest raw-row count the declared bound admits.

    A rendered replay fixture must itself satisfy the source's raw-row bound,
    otherwise regenerating the fixture makes the source reject its own
    evidence.  An exact declaration is its own floor.  A two-sided bound
    floors at its minimum, which is the shape a list whose length varies by
    design must declare -- see the Boklista Top 25, where one tie week made an
    exact count reject every ordinary week.
    """
    for field in ("ranking_expected_raw_row_count", "ranking_min_raw_row_count"):
        value = metadata.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return 0


def _render_heading_markup(heading: str, classes: tuple[str, ...]) -> str:
    escaped_heading = _html.escape(heading.strip())
    if classes:
        heading_markup = f'<h1 class="{_html.escape(classes[-1])}">{escaped_heading}</h1>'
        for parent_class in reversed(classes[:-1]):
            heading_markup = f'<div class="{_html.escape(parent_class)}">{heading_markup}</div>'
        return heading_markup
    return f"<h1>{escaped_heading}</h1>"


def _visible_title_selector_classes(selector: str) -> tuple[str, ...]:
    """Validate and split an optional visible title selector."""
    if not selector.strip():
        return ()
    try:
        return visible_title_selector_classes(selector)
    except ValueError as exc:
        raise ReplayFixtureError(f"declared visible title selector is invalid: {exc}") from exc


def _render_identity_capture(heading: str, visible_title_selector: str = "") -> str:
    """Render metadata-only identity evidence for retention.

    The renderer emits only bounded metadata markup. The retention seam in
    :class:`RankingPageEvidence` owns the capture size boundary.
    """
    if not heading.strip():
        raise ReplayFixtureError("accepted ranking identity evidence has no visible heading")
    classes = _visible_title_selector_classes(visible_title_selector)
    heading_markup = _render_heading_markup(heading, classes)
    return f"<!DOCTYPE html>\n<html>\n<body>\n{heading_markup}\n</body>\n</html>\n"


def _render_capture_html(
    heading: str,
    samples: tuple[EntrySample, ...],
    expected_sort_label: str = "",
    *,
    contract_family: str = "",
    contract_metadata: Mapping[str, Any] | None = None,
    detail_route: RankingDetailRoute | None = None,
    representative_samples: tuple[EntrySample, ...] | None = None,
    reporting_date_text: str = "",
    identifier_literals: Mapping[str, str] | None = None,
) -> str:
    """Render a bounded replay page from representative metadata only.

    Ranking-table fixtures deliberately use unlinked div rows and the shared
    source page URL for every row.  Other families retain the established
    linked-list fixture shape.
    """
    escaped_heading = _html.escape(heading)
    sort_label = _html.escape(expected_sort_label)
    sort_markup = f'<p class="sort-label">{sort_label}</p>\n' if sort_label else ""
    items: list[str] = []
    metadata = dict(contract_metadata or {})
    detail_route = detail_route or RankingDetailRoute.from_source(metadata)
    visible_title_selector = str(metadata.get("visible_title_selector", ""))
    heading_markup = _render_heading_markup(heading, _visible_title_selector_classes(visible_title_selector))
    if contract_family == "ranking-table":

        def selector_path(field: str, fallback: str) -> tuple[str, ...]:
            value = str(metadata.get(field, "")).strip()
            classes = tuple(part.strip().removeprefix(".") for part in value.split(">") if part.strip().startswith("."))
            return classes or (fallback,)

        row_selector_path = selector_path("ranking_row_selector", "ranking-row")
        row_class = row_selector_path[-1]

        def field_structure(field: str, fallback: str) -> tuple[str, tuple[str, ...]]:
            classes = selector_path(field, fallback)
            parents = classes[:-1]
            for shared_count in range(min(len(parents), len(row_selector_path)), 0, -1):
                if parents[:shared_count] == row_selector_path[-shared_count:]:
                    parents = parents[shared_count:]
                    break
            return classes[-1], parents

        def wrap_field(markup: str, parent_classes: tuple[str, ...]) -> str:
            for parent_class in reversed(parent_classes):
                markup = f'<div class="{_html.escape(parent_class)}">{markup}</div>'
            return markup

        def render_field(
            field: str,
            fallback: str,
            content: str,
            *,
            tag: str = "span",
            attributes: str = "",
            hidden: bool = False,
        ) -> str:
            leaf_class, parent_classes = field_structure(field, fallback)
            hidden_attribute = " hidden" if hidden else ""
            return wrap_field(
                f'<{tag} class="{_html.escape(leaf_class)}"{attributes}{hidden_attribute}>{content}</{tag}>',
                parent_classes,
            )

        title_selector_path = selector_path("ranking_title_selector", "title")
        detail_link_selector_path = selector_path("ranking_detail_link_selector", "detail-link")
        identifier_attribute = str(metadata.get("ranking_identifier_attribute", "")).strip()
        document_order = str(metadata.get("ranking_rank_source", "selector")).strip().casefold() == "document_order"
        render_samples = list(samples)
        if document_order:
            by_rank = {
                int(sample.rank): sample for sample in samples if sample.rank.isdigit() and int(sample.rank) >= 1
            }
            if by_rank:
                reserved_identifiers = {
                    _identifier_digits(sample.identifier) for sample in samples if _identifier_digits(sample.identifier)
                }
                target_count = max(max(by_rank), _declared_row_floor(metadata))
                representative_keys = {
                    (sample.rank, sample.title, sample.author, _identifier_digits(sample.identifier))
                    for sample in representative_samples or ()
                }
                positions_by_identifier: dict[str, list[int]] = {}
                for position, sample in by_rank.items():
                    identifier = _identifier_digits(sample.identifier)
                    if identifier:
                        positions_by_identifier.setdefault(identifier, []).append(position)
                for identifier, positions in positions_by_identifier.items():
                    representative_positions = {
                        position
                        for position in positions
                        if (
                            by_rank[position].rank,
                            by_rank[position].title,
                            by_rank[position].author,
                            identifier,
                        )
                        in representative_keys
                    }
                    keep_positions = representative_positions or {positions[-1]}
                    for position in positions:
                        if position in keep_positions:
                            continue
                        original = by_rank[position]
                        placeholder = _placeholder_isbn(position, reserved_identifiers)
                        reserved_identifiers.add(placeholder)
                        by_rank[position] = EntrySample(
                            title=original.title,
                            author=original.author,
                            url=original.url,
                            rank=original.rank,
                            identifier=placeholder,
                            category=original.category,
                            reporting_period=original.reporting_period,
                        )
                render_samples = []
                for position in range(1, target_count + 1):
                    selected = by_rank.get(position)
                    if selected is None:
                        identifier = _placeholder_isbn(position, reserved_identifiers)
                        reserved_identifiers.add(identifier)
                        selected = EntrySample(
                            title=f"Unselected ranking row {position}",
                            author="Unselected source row",
                            url=samples[0].url,
                            rank=str(position),
                            identifier=identifier,
                            category="",
                            reporting_period=samples[0].reporting_period,
                        )
                    render_samples.append(selected)
        for sample in render_samples:
            rank = _html.escape(sample.rank, quote=False)
            title = _html.escape(sample.title, quote=False)
            author = _html.escape(sample.author, quote=False)
            identifier = _html.escape(
                (identifier_literals or {}).get(sample.identifier, sample.identifier), quote=False
            )
            identifier_markup = (
                ""
                if identifier_attribute
                else render_field("ranking_identifier_selector", "identifier", identifier, hidden=True)
            )
            row_attribute = f' {identifier_attribute}="{identifier}"' if identifier_attribute else ""
            rank_markup = "" if document_order else render_field("ranking_rank_selector", "rank", rank)
            detail_link_markup = ""
            if detail_route is not None:
                detail_href = _html.escape(sample.url, quote=True)
                if title_selector_path == detail_link_selector_path:
                    title_markup = render_field(
                        "ranking_title_selector",
                        "title",
                        title,
                        tag="a",
                        attributes=f' href="{detail_href}"',
                    )
                else:
                    title_markup = render_field("ranking_title_selector", "title", title)
                    detail_link_markup = render_field(
                        "ranking_detail_link_selector",
                        "detail-link",
                        "",
                        tag="a",
                        attributes=f' href="{detail_href}"',
                    )
            else:
                title_markup = render_field("ranking_title_selector", "title", title)
            author_markup = render_field("ranking_author_selector", "author", author)
            row_markup = (
                f'<div class="{row_class}"{row_attribute}>{rank_markup}'
                f"{title_markup}{detail_link_markup}{author_markup}{identifier_markup}</div>"
            )
            for wrapper_class in reversed(row_selector_path[:-1]):
                row_markup = f'<div class="{_html.escape(wrapper_class)}">{row_markup}</div>'
            items.append(row_markup)
        row_markup = "\n".join(items)
        statement = str(metadata.get("ranking_source_statement", "")).strip()
        date_markup = ""
        if reporting_date_text:
            date_markup = _html.escape(reporting_date_text)
            for class_name in reversed(selector_path("ranking_reporting_date_selector", "reporting-date")):
                date_markup = f'<div class="{_html.escape(class_name)}">{date_markup}</div>'
            date_markup += "\n"
        return (
            "<!DOCTYPE html>\n<html>\n<head>\n<title>"
            + escaped_heading
            + "</title>\n</head>\n<body>\n<main>\n"
            + heading_markup
            + "\n"
            + sort_markup
            + date_markup
            + row_markup
            + (f"\n<p>{_html.escape(statement)}</p>" if statement else "")
            + "\n</main>\n</body>\n</html>\n"
        )
    for sample in samples:
        href = _html.escape(sample.url, quote=True)
        title = _html.escape(sample.title, quote=False)
        author = _html.escape(sample.author, quote=False)
        items.append(f'<li><a href="{href}">{title}</a><span class="author">{author}</span></li>')
    return (
        "<!DOCTYPE html>\n<html>\n<head>\n<title>"
        + escaped_heading
        + "</title>\n</head>\n<body>\n<main>\n"
        + heading_markup
        + "\n"
        + sort_markup
        + "<ul>\n"
        + "\n".join(items)
        + "\n</ul>\n</main>\n</body>\n</html>\n"
    )


def _schema_error(message: str) -> ReplayFixtureError:
    """One malformed sidecar header, language, or entries container."""
    return ReplayFixtureError(message, reason_code=REASON_HELPER_SCHEMA_INVALID)


def _entry_error(index: int, message: str) -> ReplayFixtureError:
    """One malformed or unusable helper entry, named by its sidecar index."""
    return ReplayFixtureError(f"helper metadata entry[{index}] {message}", reason_code=REASON_HELPER_ENTRY_INVALID)


def _load_helper_metadata_file(path: Path) -> dict[str, Any]:
    """Load one retained helper-metadata sidecar without creating artifacts."""
    if not path.exists():
        raise ReplayFixtureError(f"helper metadata file does not exist: {path}", reason_code=REASON_HELPER_UNREADABLE)
    try:
        value = read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayFixtureError(
            f"cannot read helper metadata: {_error_text(exc)}", reason_code=REASON_HELPER_UNREADABLE
        ) from exc
    if not isinstance(value, Mapping):
        raise ReplayFixtureError("helper metadata must be a JSON object", reason_code=REASON_HELPER_UNREADABLE)
    return dict(value)


def validate_helper_sidecar(
    helper_metadata_file: Path,
    *,
    url: str,
    language: str,
    samples: tuple[EntrySample, ...],
    author_note_optional: bool = False,
) -> None:
    """Agree one retained helper sidecar file with the selected representatives.

    This is the single public helper-agreement seam. The offline ``add-source``
    preview calls it so that it refuses exactly what confirmed replay-fixture
    generation refuses, with the same specific reason code (Issue #409). It
    reads the retained sidecar bytes only: no network call, no model call, and
    no filesystem mutation."""
    _helper_metadata_by_identity(
        _load_helper_metadata_file(helper_metadata_file),
        url=url,
        language=language,
        samples=samples,
        author_note_optional=author_note_optional,
    )


def select_representative_entries(verdict: FeasibilityVerdict) -> tuple[EntrySample, ...]:
    """Return the deterministic representative entries for one accepted verdict.

    This is the single selection seam that the offline preview, the confirmed
    onboarding operation, and direct fixture generation all use, so the
    identities an editor confirms are exactly the identities the confirmed
    replay fixtures carry (Issue #409). A bounded list longer than
    ``MAX_REPLAY_ENTRIES`` keeps only its first ``MAX_REPLAY_ENTRIES`` entries
    in document order."""
    samples = tuple(verdict.samples[:MAX_REPLAY_ENTRIES])
    if not samples:
        raise ReplayFixtureError(
            "accepted probe evidence exposes no representative entries",
            reason_code=REASON_REPRESENTATIVE_ENTRIES_MISSING,
        )
    return samples


def _helper_metadata_by_identity(
    metadata: Mapping[str, Any] | None,
    *,
    url: str,
    language: str,
    samples: tuple[EntrySample, ...],
    author_note_optional: bool = False,
) -> dict[tuple[str, str, str], dict[str, str]]:
    """Validate retained helper metadata for exactly the generated samples.

    Helper values are never translated, inferred, or filled in here. The
    sidecar is an offline retained input that must carry exactly one entry per
    selected representative sample, in the same deterministic order, with the
    exact title, author, and book URL identity of that sample. When
    *author_note_optional* is true, a blank author identity may omit the
    author transliteration note. A sidecar with a stale, missing, extra,
    duplicated, reordered, or mismatched identity is
    refused, and every refusal names a specific correctable reason code
    (Issue #409).
    """
    if metadata is None:
        raise ReplayFixtureError(
            "helper metadata is required for generated exact-list fixtures", reason_code=REASON_HELPER_MISSING
        )
    unknown_top_level = set(metadata) - _HELPER_METADATA_TOP_LEVEL_FIELDS
    if unknown_top_level:
        names = ", ".join(sorted(str(value) for value in unknown_top_level))
        raise _schema_error(f"helper metadata has unknown fields: {names}")
    if isinstance(metadata.get("version"), bool) or metadata.get("version") != 1:
        raise _schema_error("helper metadata version must be 1")
    sidecar_url = metadata.get("source_url")
    if not isinstance(sidecar_url, str) or not sidecar_url.strip():
        raise _schema_error("helper metadata source_url must be nonblank")
    if normalized_url(sidecar_url.strip()) != normalized_url(url):
        raise _schema_error("helper metadata is for a different URL than the requested --url")
    sidecar_language = metadata.get("language")
    if not isinstance(sidecar_language, str) or not sidecar_language.strip():
        raise _schema_error("helper metadata language must be nonblank")
    if sidecar_language.strip().casefold() != language.casefold():
        raise _schema_error("helper metadata language does not match the requested source language")
    entries = metadata.get("entries")
    if not isinstance(entries, list) or not entries:
        raise _schema_error("helper metadata entries must be a non-empty list")

    # The sidecar checks above refuse a blank sidecar language and a language
    # mismatch before this line, so `language` is provably nonblank here.
    is_english = is_english_language(language)
    raw_by_identity: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    identity_order: list[tuple[str, str, str]] = []
    for index, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, Mapping):
            raise _entry_error(index, "must be an object")
        unknown_fields = set(raw_entry) - _HELPER_METADATA_ENTRY_FIELDS
        if unknown_fields:
            names = ", ".join(sorted(str(value) for value in unknown_fields))
            raise _entry_error(index, f"has unknown fields: {names}")
        identity_values: dict[str, str] = {}
        for field in ("title", "author", "evidence_url"):
            value = raw_entry.get(field)
            if field == "author" and author_note_optional and isinstance(value, str) and not value.strip():
                identity_values[field] = ""
                continue
            if not isinstance(value, str) or not value.strip():
                raise _entry_error(index, f"{field} must be nonblank")
            identity_values[field] = value.strip()
        identity = (identity_values["title"], identity_values["author"], identity_values["evidence_url"])
        if author_note_optional and not identity[1] and "author_transliteration_note" in raw_entry:
            raise _entry_error(index, "author_transliteration_note is not allowed for an authorless entry")
        if identity in raw_by_identity:
            raise ReplayFixtureError(
                f"helper metadata contains duplicate entry identity: {identity[0]}",
                reason_code=REASON_HELPER_DUPLICATE_ENTRY,
            )
        raw_by_identity[identity] = raw_entry
        identity_order.append(identity)

    _assert_identity_agreement(identity_order, samples)

    by_identity: dict[tuple[str, str, str], dict[str, str]] = {}
    for index, identity in enumerate(identity_order):
        raw_entry = raw_by_identity[identity]
        helper_error = validate_helper_metadata(
            raw_entry,
            title_relation=not is_english,
            author_note_required=not (author_note_optional and not identity[1]),
        )
        if helper_error:
            raise _entry_error(index, helper_error)
        by_identity[identity] = {
            field: str(raw_entry[field]).strip()
            for field in HELPER_METADATA_FIELDS
            if isinstance(raw_entry.get(field), str) and raw_entry[field].strip()
        }
    return by_identity


def _assert_identity_agreement(identity_order: list[tuple[str, str, str]], samples: tuple[EntrySample, ...]) -> None:
    """Refuse any sidecar that is not the exact ordered representative set."""
    expected_order = [(sample.title, sample.author, sample.url) for sample in samples]
    if identity_order == expected_order:
        return
    expected_identities = set(expected_order)
    actual_identities = set(identity_order)
    missing = expected_identities - actual_identities
    stale = actual_identities - expected_identities
    details: list[str] = []
    if missing:
        details.append("missing " + ", ".join(sorted(identity[0] for identity in missing)))
    if stale:
        details.append("stale " + ", ".join(sorted(identity[0] for identity in stale)))
    if not details:
        details.append("entry order differs from the deterministic representative order")
    raise ReplayFixtureError(
        f"helper metadata identities do not match the {len(expected_order)} selected representative entries "
        f"(at most {MAX_REPLAY_ENTRIES}): " + "; ".join(details),
        reason_code=REASON_HELPER_IDENTITY_MISMATCH,
    )


def _ranking_primary_authors(author_display: str) -> list[str]:
    """Split the declared author display separators without reordering."""
    display = author_display.strip()
    if not display:
        return []
    authors = [segment.strip() for segment in _AUTHOR_DISPLAY_SEPARATOR_RE.split(display)]
    if any(not author for author in authors):
        raise ReplayFixtureError("ranking author display has an empty or ambiguous semicolon or slash segment")
    return authors


def _extraction_entry(
    sample: EntrySample,
    *,
    source_url: str,
    signal_type: str,
    signal_strength: str,
    candidate_category: str,
    helper_values: Mapping[str, str],
    contract_family: str = "",
    source_week: str | None = None,
) -> dict[str, Any]:
    """One metadata-only extraction entry grounded in the generated page.

    *candidate_category* is the required configured eligible candidate
    category; it is always emitted because the canonical exact-book-list
    contract requires it on every mention."""
    value = {
        "title": sample.title,
        "primary_authors": (
            _ranking_primary_authors(sample.author) if contract_family == "ranking-table" else [sample.author]
        ),
        "source_url": source_url,
        "evidence_url": sample.url,
        "signal_type": signal_type,
        "signal_strength": signal_strength,
        "candidate_category": candidate_category,
        "evidence_note": (
            "Bounded ranking row on the captured source page." if contract_family == "ranking-table" else EVIDENCE_NOTE
        ),
        **dict(helper_values),
    }
    if contract_family == "ranking-table":
        value.update(
            {
                "isbn": sample.identifier,
                "current_rank": sample.rank,
                "source_week": sample.reporting_period if source_week is None else source_week,
            }
        )
    return value


def _serialize(data: Any) -> bytes:
    """The exact deterministic bytes the writer publishes."""
    return (json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _validate_requested_url(url: str) -> None:
    if not url or not normalized_url(url).startswith(("http://", "https://")):
        raise ReplayFixtureError("--url must be a valid HTTP(S) URL")


def _validate_slug(slug: str) -> None:
    if not slug or _SLUG_RE.fullmatch(slug) is None:
        raise ReplayFixtureError(
            "slug must be filesystem-safe: lowercase letters, digits, hyphens, and underscores only"
        )


def _load_evidence(path: Path) -> dict[str, Any]:
    """Read one retained probe capture, rejecting unreadable or non-object files."""
    if not path.exists():
        raise ReplayFixtureError(f"probe evidence file does not exist: {path}")
    try:
        data = read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayFixtureError(f"cannot read probe evidence: {_error_text(exc)}") from exc
    if not isinstance(data, Mapping):
        raise ReplayFixtureError("probe evidence must be a JSON object")
    return dict(data)


def _validated_evidence(url: str, evidence: Mapping[str, Any]) -> tuple[dict[str, Any], FeasibilityVerdict]:
    """Validate that *evidence* is an accepted probe capture for *url*.

    Malformed evidence (not an object, wrong collection mode, missing body or
    status evidence), non-accepted evidence (rejected or deferred verdict),
    and evidence captured for a different URL all fail before any output is
    written. The acceptance verdict comes from the Issue #334 policy seam, so
    the retained HTML is parsed exactly once here and its representative
    samples feed both fixtures."""
    if not isinstance(evidence, Mapping):
        raise ReplayFixtureError("probe evidence must be a JSON object")
    if evidence.get("collection_mode") != PROBE_COLLECTION_MODE:
        raise ReplayFixtureError(f"probe evidence is not a retained {PROBE_COLLECTION_MODE} capture")
    evidence_source_url = str(evidence.get("source_url", evidence.get("url", evidence.get("source_page", "")))).strip()
    if not evidence_source_url:
        raise ReplayFixtureError("probe evidence does not record the candidate URL")
    if normalized_url(evidence_source_url) != normalized_url(url):
        raise ReplayFixtureError("probe evidence is for a different URL than the requested --url")
    contract_metadata = evidence.get("contract_metadata")
    source_metadata = dict(contract_metadata) if isinstance(contract_metadata, Mapping) else {}
    source_metadata.update(
        {
            "url": url,
            "contract_family": str(evidence.get("contract_family", "")).strip(),
            "visible_title_selector": str(
                evidence.get("visible_title_selector") or source_metadata.get("visible_title_selector", "")
            ),
            "entry_scope_selector": str(
                evidence.get("entry_scope_selector") or source_metadata.get("entry_scope_selector", "")
            ),
        }
    )
    try:
        verdict = evaluate_source_feasibility(source_metadata, evidence)
    except (TypeError, ValueError) as exc:
        raise ReplayFixtureError(f"probe evidence is malformed: {_error_text(exc)}") from exc
    if verdict.outcome != OUTCOME_ACCEPT:
        raise ReplayFixtureError(
            f"probe evidence is not an accepted capture (outcome={verdict.outcome}, "
            f"reason_code={verdict.reason_code}, reason={verdict.reason})"
        )
    summary_error = validate_retained_summary(evidence, verdict)
    if summary_error is not None:
        reason_code, reason = summary_error
        raise ReplayFixtureError(reason, reason_code=reason_code)
    if not verdict.samples:
        raise ReplayFixtureError("accepted probe evidence exposes no representative entries")
    return dict(evidence), verdict


def _retained_ranking_reference_date(
    evidence: Mapping[str, Any], contract_family: str, contract_metadata: Mapping[str, Any]
) -> str:
    """Return the retained observation/check date for ranking replay labels."""
    if contract_family != "ranking-table" or (
        not ranking_freshness_declared(contract_metadata) and not ranking_undated_declared(contract_metadata)
    ):
        return ""
    raw_reference_date = evidence.get("reference_date")
    if not isinstance(raw_reference_date, str) or not raw_reference_date.strip():
        raise ReplayFixtureError("accepted ranking evidence must include a retained reference_date")
    try:
        parsed = date.fromisoformat(raw_reference_date.strip())
    except ValueError as exc:
        raise ReplayFixtureError("accepted ranking evidence reference_date must be a valid ISO date") from exc
    if parsed.isoformat() != raw_reference_date.strip():
        raise ReplayFixtureError("accepted ranking evidence reference_date must be a canonical ISO date")
    return parsed.isoformat()


def build_replay_fixture_pair(
    url: str,
    slug: str,
    evidence: Mapping[str, Any],
    *,
    signal_strength: str,
    candidate_category: str,
    source_name: str = "",
    signal_family: str = "",
    contract_family: str = "",
    contract_metadata: Mapping[str, Any] | None = None,
    language: str = "en",
    expected_sort_label: str = "",
    helper_metadata: Mapping[str, Any] | None = None,
    helper_metadata_file: Path | None = None,
) -> ReplayFixturePair:
    """Generate one mutually grounded fixture pair from retained probe evidence.

    Pure seam: no network, no writes, no weekly/config/state access. *evidence*
    must be a retained probe capture (``collection_mode``
    ``source-candidate-probe``) that the Issue #334 evaluator accepts for
    *url*; malformed, rejected, deferred, and URL-mismatched evidence raises
    :class:`ReplayFixtureError` before anything is generated. *signal_strength*
    and *candidate_category* are mandatory configured values because probe
    evidence never records them; *source_name* and *signal_family* default to
    the values retained in the probe capture. *helper_metadata* or
    *helper_metadata_file* is also required for retained non-authoritative
    helper values; no helper is translated or synthesized.

    The page capture is a synthesized bounded page derived from the same parse
    that accepted the evidence (heading, exact titles, primary authors, and
    book-level links only), and its ``source_url``, ``source_page``, and
    ``resolved_url`` are all exactly the requested *url* - never a retained
    resolved URL that may differ only by a trailing slash. The extraction
    envelope is bound to that page by a deterministic ``capture_sha256`` and a
    self-contained ``operation_id`` and carries the canonical ``mentions``
    array that the exact-book-list contract reads. As a final self-check every
    extraction entry is verified grounded in the generated page before the
    pair is returned."""
    _validate_requested_url(url)
    _validate_slug(slug)
    signal_strength = signal_strength.strip()
    if not signal_strength:
        raise ReplayFixtureError("--signal-strength must be nonblank")
    candidate_category = candidate_category.strip()
    if not candidate_category:
        raise ReplayFixtureError("--candidate-category must be nonblank")
    language = language.strip()
    if not language:
        raise ReplayFixtureError("--language must be nonblank")
    expected_sort_label = expected_sort_label.strip()
    evidence, verdict = _validated_evidence(url, evidence)
    if expected_sort_label:
        page_shape_error = validate_page_shape(
            {
                "expected_page_heading": verdict.detected_heading,
                "expected_sort_label": expected_sort_label,
            },
            {"html": str(evidence.get("html", ""))},
        )
        if page_shape_error:
            raise ReplayFixtureError(page_shape_error)
    source_name = source_name.strip() or str(evidence.get("source_name") or evidence.get("name") or "").strip()
    selected_contract_family = (
        contract_family.strip().casefold() or str(evidence.get("contract_family", "")).strip().casefold() or "generic"
    )
    if selected_contract_family not in {
        "generic",
        "exact-book-list",
        "nuevoslibros-frontlist",
        "publisher-frontlist",
        "ranking-table",
        "award-list",
        "bookinfo-weekly-json",
        "ipc-top40-csv",
        "perlentaucher-daily-digest",
        "kirkus-coming-soon",
        "audible-chart",
        "amazon-charts",
        "audiobook-ranking",
    }:
        raise ReplayFixtureError(f"unsupported contract family: {selected_contract_family}")
    selected_contract_metadata = dict(contract_metadata or evidence.get("contract_metadata", {}))
    ranking_source_metadata = (
        {**selected_contract_metadata, "url": url}
        if selected_contract_family == "ranking-table"
        else selected_contract_metadata
    )
    detail_route = RankingDetailRoute.from_source(ranking_source_metadata)
    retained_reference_date = _retained_ranking_reference_date(
        evidence, selected_contract_family, selected_contract_metadata
    )
    if not source_name:
        raise ReplayFixtureError("source name must be supplied with --source-name or retained in probe evidence")
    signal_type = signal_family.strip() or str(evidence.get("signal_family") or "").strip()
    if not signal_type:
        raise ReplayFixtureError("signal family must be supplied with --signal-family or retained in probe evidence")
    samples = select_representative_entries(verdict)
    capture_samples = samples
    raw_detail_pages_for_route: list[Any] | None = None
    retained_rows: tuple[EntrySample, ...] = ()
    if selected_contract_family == "ranking-table":
        retained_rows = tuple(
            EntrySample(
                title=row.title,
                author=row.author,
                url=row.detail_url if detail_route is not None else url,
                rank=row.rank,
                identifier=row.identifier,
                reporting_period=verdict.reporting_period,
            )
            for row in ranking_rows(str(evidence.get("html", "")), ranking_source_metadata, detail_route=detail_route)
        )
        # Keep catalog-verified authors as derived metadata, rather than
        # fabricating visible author text in a replay of the source page.
        visible_row_metadata = dict(ranking_source_metadata)
        visible_row_metadata.pop("ranking_author_evidence_json", None)
        capture_samples = tuple(
            EntrySample(
                title=row.title,
                author=row.author,
                url=row.detail_url if detail_route is not None else url,
                rank=row.rank,
                identifier=row.identifier,
                reporting_period=verdict.reporting_period,
            )
            for row in ranking_rows(str(evidence.get("html", "")), visible_row_metadata, detail_route=detail_route)
        )
    if detail_route is not None:
        raw_detail_pages = evidence.get("detail_pages")
        if not isinstance(raw_detail_pages, list) or not raw_detail_pages:
            raise ReplayFixtureError("accepted ranking detail evidence must include complete detail_pages")
        raw_detail_pages_for_route = raw_detail_pages
    if helper_metadata is not None and helper_metadata_file is not None:
        raise ReplayFixtureError("provide helper metadata as a mapping or a file, not both")
    if helper_metadata_file is not None:
        helper_metadata = _load_helper_metadata_file(helper_metadata_file)
    helper_values = _helper_metadata_by_identity(
        helper_metadata,
        url=url,
        language=language,
        samples=samples,
        author_note_optional=(
            selected_contract_family == "ranking-table" and ranking_author_optional_declared(selected_contract_metadata)
        ),
    )

    if selected_contract_family == "bookinfo-weekly-json":
        from autoscout.bookinfo_ranking import mention_violation
        from autoscout.bookinfo_ranking import page_data as bookinfo_page_data

        bookinfo_source = {**selected_contract_metadata, "url": url, "eligible_candidate_category": candidate_category}
        bookinfo_page = bookinfo_page_data(evidence["bookinfo_evidence"], bookinfo_source)
        bookinfo_page["contract_metadata"] = selected_contract_metadata
        page_bytes = _serialize(bookinfo_page)
        capture_sha256 = hashlib.sha256(page_bytes).hexdigest()
        operation_id = f"replay-fixture:{slug}:{_url_hash(url)}"
        mentions = []
        for sample in samples:
            bookinfo_entry = _extraction_entry(
                sample,
                source_url=url,
                signal_type=signal_type,
                signal_strength=signal_strength,
                candidate_category=candidate_category,
                helper_values=helper_values[(sample.title, sample.author, sample.url)],
            )
            bookinfo_entry.update(
                isbn=sample.identifier, current_rank=int(sample.rank), source_week=sample.reporting_period
            )
            error = mention_violation(bookinfo_entry, bookinfo_page, bookinfo_source)
            if error:
                raise ReplayFixtureError(error)
            mentions.append(bookinfo_entry)
        bookinfo_extraction = {
            "source_name": source_name,
            "source_url": url,
            "operation_id": operation_id,
            "capture_sha256": capture_sha256,
            "mentions": mentions,
        }
        return ReplayFixturePair(
            url=url,
            slug=slug,
            page=bookinfo_page,
            extraction=bookinfo_extraction,
            page_bytes=page_bytes,
            extraction_bytes=_serialize(bookinfo_extraction),
            capture_sha256=capture_sha256,
            operation_id=operation_id,
            entry_count=len(mentions),
        )

    if selected_contract_family == "ipc-top40-csv":
        from autoscout.ipc_top40_ranking import mention_violation as ipc_top40_mention_violation
        from autoscout.ipc_top40_ranking import page_data as ipc_top40_page_data
        from autoscout.ipc_top40_ranking import primary_authors as ipc_top40_primary_authors

        ipc_source = {**selected_contract_metadata, "url": url, "eligible_candidate_category": candidate_category}
        ipc_page = ipc_top40_page_data(evidence["ipc_top40_evidence"], ipc_source)
        ipc_page["contract_metadata"] = selected_contract_metadata
        page_bytes = _serialize(ipc_page)
        capture_sha256 = hashlib.sha256(page_bytes).hexdigest()
        operation_id = f"replay-fixture:{slug}:{_url_hash(url)}"
        mentions = []
        for sample in samples:
            ipc_entry = _extraction_entry(
                sample,
                source_url=url,
                signal_type=signal_type,
                signal_strength=signal_strength,
                candidate_category=candidate_category,
                helper_values=helper_values[(sample.title, sample.author, sample.url)],
            )
            ipc_entry.update(
                isbn=sample.identifier,
                current_rank=int(sample.rank),
                source_week=sample.reporting_period,
                primary_authors=ipc_top40_primary_authors(sample.author),
            )
            error = ipc_top40_mention_violation(ipc_entry, ipc_page, ipc_source)
            if error:
                raise ReplayFixtureError(error)
            mentions.append(ipc_entry)
        ipc_extraction = {
            "source_name": source_name,
            "source_url": url,
            "operation_id": operation_id,
            "capture_sha256": capture_sha256,
            "mentions": mentions,
        }
        return ReplayFixturePair(
            url=url,
            slug=slug,
            page=ipc_page,
            extraction=ipc_extraction,
            page_bytes=page_bytes,
            extraction_bytes=_serialize(ipc_extraction),
            capture_sha256=capture_sha256,
            operation_id=operation_id,
            entry_count=len(mentions),
        )

    if selected_contract_family == "kirkus-coming-soon":
        from autoscout import kirkus_listing

        listing_source = {
            **selected_contract_metadata,
            "url": url,
            "eligible_candidate_category": candidate_category,
            "expected_signal_strength": signal_strength,
        }
        listing_page = kirkus_listing.page_data(evidence[kirkus_listing.EVIDENCE_FIELD], listing_source)
        listing_page["contract_metadata"] = selected_contract_metadata
        page_bytes = _serialize(listing_page)
        capture_sha256 = hashlib.sha256(page_bytes).hexdigest()
        operation_id = f"replay-fixture:{slug}:{_url_hash(url)}"
        listing_rows = {
            row["isbn"]: row
            for row in kirkus_listing.qualifying_rows(
                kirkus_listing.validate_bundle(evidence[kirkus_listing.EVIDENCE_FIELD], listing_source),
                listing_source,
            )
        }
        mentions = []
        for sample in samples:
            listing_row = listing_rows.get(sample.identifier)
            if listing_row is None:
                raise ReplayFixtureError("Kirkus sample is not a qualifying captured listing row")
            listing_entry = _extraction_entry(
                sample,
                source_url=url,
                signal_type=signal_type,
                signal_strength=signal_strength,
                candidate_category=candidate_category,
                helper_values=helper_values[(sample.title, sample.author, sample.url)],
            )
            # Identity comes from the captured row, never from the sample's display strings.
            listing_entry.update(
                primary_authors=list(listing_row["primary_authors"]),
                publisher=listing_row["publisher"],
                publication_year=listing_row["publication_year"],
                isbn=listing_row["isbn"],
                source_week=listing_row["source_week"],
                evidence_note="Reviewed book row on the captured Kirkus coming-soon listing; metadata-only evidence.",
            )
            error = kirkus_listing.mention_violation(listing_entry, listing_page, listing_source)
            if error:
                raise ReplayFixtureError(error)
            mentions.append(listing_entry)
        listing_extraction = {
            "source_name": source_name,
            "source_url": url,
            "operation_id": operation_id,
            "capture_sha256": capture_sha256,
            "mentions": mentions,
        }
        return ReplayFixturePair(
            url=url,
            slug=slug,
            page=listing_page,
            extraction=listing_extraction,
            page_bytes=page_bytes,
            extraction_bytes=_serialize(listing_extraction),
            capture_sha256=capture_sha256,
            operation_id=operation_id,
            entry_count=len(mentions),
        )

    if selected_contract_family == "perlentaucher-daily-digest":
        from autoscout.perlentaucher_digest import EVIDENCE_FIELD as PERLENTAUCHER_EVIDENCE_FIELD
        from autoscout.perlentaucher_digest import mention_violation as perlentaucher_mention_violation
        from autoscout.perlentaucher_digest import page_data as perlentaucher_page_data
        from autoscout.perlentaucher_digest import qualifying_rows as perlentaucher_qualifying_rows
        from autoscout.perlentaucher_digest import validate_bundle as validate_perlentaucher_bundle

        digest_source = {
            **selected_contract_metadata,
            "url": url,
            "eligible_candidate_category": candidate_category,
            "expected_signal_strength": signal_strength,
        }
        digest_page = perlentaucher_page_data(evidence[PERLENTAUCHER_EVIDENCE_FIELD], digest_source)
        digest_page["contract_metadata"] = selected_contract_metadata
        page_bytes = _serialize(digest_page)
        capture_sha256 = hashlib.sha256(page_bytes).hexdigest()
        operation_id = f"replay-fixture:{slug}:{_url_hash(url)}"
        digest_rows = {
            row["isbn"]: row
            for row in perlentaucher_qualifying_rows(
                validate_perlentaucher_bundle(evidence[PERLENTAUCHER_EVIDENCE_FIELD], digest_source), digest_source
            )
        }
        mentions = []
        for sample in samples:
            digest_row = digest_rows.get(sample.identifier)
            if digest_row is None:
                raise ReplayFixtureError("Perlentaucher sample is not a qualifying captured daily page row")
            digest_entry = _extraction_entry(
                sample,
                source_url=url,
                signal_type=signal_type,
                signal_strength=signal_strength,
                candidate_category=candidate_category,
                helper_values=helper_values[(sample.title, sample.author, sample.url)],
            )
            # Identity comes from the captured row, never from the sample's display strings.
            digest_entry.update(
                primary_authors=list(digest_row["primary_authors"]),
                publisher=digest_row["publisher"],
                publication_year=digest_row["publication_year"],
                isbn=digest_row["isbn"],
                source_week=digest_row["source_week"],
                evidence_note="Reviewed book row on a captured Perlentaucher daily page; metadata-only evidence.",
            )
            error = perlentaucher_mention_violation(digest_entry, digest_page, digest_source)
            if error:
                raise ReplayFixtureError(error)
            mentions.append(digest_entry)
        digest_extraction = {
            "source_name": source_name,
            "source_url": url,
            "operation_id": operation_id,
            "capture_sha256": capture_sha256,
            "mentions": mentions,
        }
        return ReplayFixturePair(
            url=url,
            slug=slug,
            page=digest_page,
            extraction=digest_extraction,
            page_bytes=page_bytes,
            extraction_bytes=_serialize(digest_extraction),
            capture_sha256=capture_sha256,
            operation_id=operation_id,
            entry_count=len(mentions),
        )

    if selected_contract_family == "award-list":
        from autoscout.award_list import mention_violation as award_list_mention_violation
        from autoscout.award_list import render_replay_html

        award_source = {
            **selected_contract_metadata,
            "url": url,
            "contract_family": selected_contract_family,
            "signal_family": signal_type,
            "expected_signal_strength": signal_strength,
            "eligible_candidate_category": candidate_category,
            "language": language,
            # A probe retains its title selector beside the contract metadata.
            "visible_title_selector": str(
                evidence.get("visible_title_selector") or selected_contract_metadata.get("visible_title_selector", "")
            ),
        }
        reference_date = lenient_reference_date(evidence.get("reference_date"))
        if reference_date is None:
            raise ReplayFixtureError("accepted award-list evidence must include a retained reference_date")
        try:
            _visible_title_selector_classes(award_source["visible_title_selector"])
            award_html = render_replay_html(str(evidence.get("html", "")), award_source, verdict.detected_heading)
        except ValueError as exc:
            raise ReplayFixtureError(f"accepted award-list evidence is not grounded: {exc}") from exc
        award_page_capture: dict[str, Any] = {
            "collection_mode": REPLAY_COLLECTION_MODE,
            "source_name": source_name,
            "source_url": url,
            "source_page": url,
            "resolved_url": url,
            "html": sanitize_captured_html(award_html),
            "contract_family": selected_contract_family,
            "reference_date": reference_date.isoformat(),
        }
        if selected_contract_metadata:
            award_page_capture["contract_metadata"] = selected_contract_metadata
        # The generated page must also be the page the configured source
        # expects, heading included.
        page_shape_error = validate_page_shape(
            {**award_source, "expected_page_heading": verdict.detected_heading},
            award_page_capture,
            reference_date=reference_date,
        )
        if page_shape_error:
            raise ReplayFixtureError(f"generated award-list page is not the accepted list: {page_shape_error}")
        page_bytes = _serialize(award_page_capture)
        capture_sha256 = hashlib.sha256(page_bytes).hexdigest()
        operation_id = f"replay-fixture:{slug}:{_url_hash(url)}"
        mentions = []
        for sample in samples:
            award_entry = _extraction_entry(
                sample,
                source_url=url,
                signal_type=signal_type,
                signal_strength=signal_strength,
                candidate_category=candidate_category,
                helper_values=helper_values[(sample.title, sample.author, sample.url)],
            )
            award_entry["evidence_note"] = "Bounded award row on the captured source page."
            award_entry["primary_authors"] = sample.author.split(" / ")
            if sample.rank:
                award_entry["current_rank"] = int(sample.rank)
            if sample.identifier:
                award_entry["isbn"] = sample.identifier
            error = award_list_mention_violation(award_entry, award_entry, award_page_capture, award_source)
            if error:
                raise ReplayFixtureError(error)
            mentions.append(award_entry)
        award_extraction = {
            "source_name": source_name,
            "source_url": url,
            "operation_id": operation_id,
            "capture_sha256": capture_sha256,
            "mentions": mentions,
        }
        return ReplayFixturePair(
            url=url,
            slug=slug,
            page=award_page_capture,
            extraction=award_extraction,
            page_bytes=page_bytes,
            extraction_bytes=_serialize(award_extraction),
            capture_sha256=capture_sha256,
            operation_id=operation_id,
            entry_count=len(mentions),
        )

    # Pattern-only identifiers may include a grounded label (for example ISBN).
    # Preserve the complete observed match rather than inventing a bare value
    # that cannot satisfy the source's declared pattern during replay.
    identifier_literals: dict[str, str] = {}
    identifier_pattern = str(selected_contract_metadata.get("ranking_identifier_pattern", ""))
    if (
        identifier_pattern
        and not selected_contract_metadata.get("ranking_identifier_selector")
        and not selected_contract_metadata.get("ranking_identifier_attribute")
    ):
        for match in re.finditer(identifier_pattern, str(evidence.get("html", ""))):
            identifier = match.group("identifier") if "identifier" in match.re.groupindex else match.group(1)
            identifier_literals.setdefault(identifier, match.group(0))

    heading = verdict.detected_heading.strip() or "Books"
    reporting_date_text = ""
    date_selector = str(ranking_source_metadata.get("ranking_reporting_date_selector", "")).strip()
    if selected_contract_family == "ranking-table" and date_selector:
        date_identity = parse_visible_page_identity(str(evidence.get("html", "")), date_selector)
        if date_identity.failure_cause is not None or len(date_identity.identity_texts) != 1:
            raise ReplayFixtureError("accepted ranking evidence must retain exactly one visible reporting date")
        reporting_date_text = date_identity.identity_texts[0]
    identity_capture = (
        _render_identity_capture(
            heading,
            str(ranking_source_metadata.get("visible_title_selector", "")),
        )
        if ranking_identity_declared(ranking_source_metadata)
        else ""
    )
    if selected_contract_family == "publisher-frontlist":
        from autoscout.publisher_frontlist import replay_html as publisher_replay_html

        frontlist_replay = publisher_replay_html(str(evidence.get("html", "")), url, [sample.url for sample in samples])
    elif selected_contract_family == "nuevoslibros-frontlist":
        from autoscout.nuevoslibros_frontlist import replay_html

        frontlist_replay = replay_html(str(evidence.get("html", "")), url, [sample.url for sample in samples])
    else:
        frontlist_replay = ""
    try:
        retained_capture = RankingPageEvidence(
            rows_html=frontlist_replay
            or _render_capture_html(
                heading,
                capture_samples,
                expected_sort_label,
                contract_family=selected_contract_family,
                contract_metadata=selected_contract_metadata,
                detail_route=detail_route,
                representative_samples=samples,
                reporting_date_text=reporting_date_text,
                identifier_literals=identifier_literals,
            ),
            identity_html=identity_capture,
        ).sanitized(rows_max_bytes=MAX_RESPONSE_BYTES, identity_max_bytes=MAX_RESPONSE_BYTES)
    except RankingCaptureError as exc:
        raise ReplayFixtureError(str(exc)) from exc
    if selected_contract_family == "ranking-table":
        generated_identity = ranking_page_identity(
            retained_capture.rows_html,
            ranking_source_metadata,
            identity_html=retained_capture.identity_html,
        )
        if ranking_identity_declared(ranking_source_metadata) and generated_identity is None:
            raise ReplayFixtureError("generated ranking identity evidence is not grounded in the accepted heading")
        generated_rows = ranking_rows(retained_capture.rows_html, ranking_source_metadata, detail_route=detail_route)
        if len(generated_rows) != len(retained_rows):
            raise ReplayFixtureError(
                "generated ranking page does not retain every source ranking row "
                f"(expected {len(retained_rows)}, got {len(generated_rows)})"
            )
        try:
            row_count_error = ranking_raw_row_count_error(ranking_source_metadata, len(generated_rows))
        except ValueError as exc:
            raise ReplayFixtureError(str(exc)) from exc
        if row_count_error:
            raise ReplayFixtureError(row_count_error)
    page: dict[str, Any] = {
        "collection_mode": REPLAY_COLLECTION_MODE,
        "source_name": source_name,
        "source_url": url,
        "source_page": url,
        "resolved_url": url,
        "html": retained_capture.rows_html,
    }
    if selected_contract_family != "generic":
        page["contract_family"] = selected_contract_family
        if selected_contract_metadata:
            page["contract_metadata"] = selected_contract_metadata
    if selected_contract_family == "ranking-table":
        request_error = validate_ranking_request_declaration(ranking_source_metadata)
        if request_error:
            raise ReplayFixtureError(f"ranking request declaration is invalid: {request_error}")
        response_error = validate_ranking_response_declaration(ranking_source_metadata)
        if response_error:
            raise ReplayFixtureError(f"ranking response declaration is invalid: {response_error}")
        ranking_request = ranking_request_from_source(ranking_source_metadata)
        response_field = str(ranking_source_metadata.get(RANKING_RESPONSE_HTML_FIELD, "") or "").strip()
        if ranking_request.is_post and response_field:
            envelope: dict[str, Any] = {response_field: page["html"]}
            final_page_field = str(ranking_source_metadata.get(RANKING_RESPONSE_LAST_PAGE_FIELD, "") or "").strip()
            if final_page_field:
                envelope[final_page_field] = True
            unwrapped = unwrap_ranking_response(json.dumps(envelope, ensure_ascii=False), ranking_source_metadata)
            if not unwrapped.ok:
                raise ReplayFixtureError(f"generated ranking response envelope is invalid: {unwrapped.reason}")
            if unwrapped.html != page["html"]:
                raise ReplayFixtureError("generated ranking response envelope does not preserve the page HTML")
            if final_page_field and unwrapped.last_page_value(ranking_source_metadata) is not True:
                raise ReplayFixtureError("generated ranking response envelope has no usable true final-page value")
            page["ranking_response_envelope"] = envelope
    if ranking_identity_declared(ranking_source_metadata):
        if not identity_capture.strip():
            raise ReplayFixtureError("accepted ranking identity evidence must include identity_html")
        page["identity_html"] = retained_capture.identity_html
    detail_page_by_url: dict[str, dict[str, Any]] = {}
    if detail_route is not None:
        raw_detail_pages = raw_detail_pages_for_route
        if raw_detail_pages is None:
            raise ReplayFixtureError("accepted ranking detail evidence must include complete detail_pages")
        selected_urls = {sample.url for sample in samples}
        all_detail_page_by_url: dict[str, dict[str, Any]] = {}
        for raw_detail in raw_detail_pages:
            if not isinstance(raw_detail, Mapping):
                raise ReplayFixtureError("ranking detail evidence item must be an object")
            detail_url = str(raw_detail.get("detail_url", raw_detail.get("resolved_url", ""))).strip()
            if not detail_url or detail_url in all_detail_page_by_url:
                raise ReplayFixtureError("ranking detail evidence has a duplicate detail page")
            resolved_url = str(raw_detail.get("resolved_url", "")).strip()
            if resolved_url != detail_url:
                raise ReplayFixtureError("ranking detail evidence contains a redirect")
            detail_html = raw_detail.get("html")
            if not isinstance(detail_html, str) or not detail_html.strip():
                raise ReplayFixtureError("ranking detail evidence HTML is missing")
            all_detail_page_by_url[detail_url] = {
                "source_page": detail_url,
                "detail_url": detail_url,
                "resolved_url": resolved_url,
                "html": sanitize_captured_html(detail_html),
            }
        detail_page_by_url = {
            detail_url: all_detail_page_by_url[detail_url]
            for detail_url in selected_urls
            if detail_url in all_detail_page_by_url
        }
        if set(detail_page_by_url) != selected_urls:
            raise ReplayFixtureError("ranking detail evidence is missing a selected row detail page")
        page["detail_pages"] = [
            all_detail_page_by_url[str(raw_detail.get("detail_url", "")).strip()]
            for raw_detail in raw_detail_pages
            if isinstance(raw_detail, Mapping) and str(raw_detail.get("detail_url", "")).strip()
        ]
    if retained_reference_date:
        page["reference_date"] = retained_reference_date
    page_bytes = _serialize(page)
    capture_sha256 = hashlib.sha256(page_bytes).hexdigest()
    operation_id = f"replay-fixture:{slug}:{_url_hash(url)}"
    extraction_entries: list[dict[str, Any]] = []
    for sample in samples:
        entry = _extraction_entry(
            sample,
            source_url=url,
            signal_type=signal_type,
            signal_strength=signal_strength,
            candidate_category=candidate_category,
            helper_values=helper_values[(sample.title, sample.author, sample.url)],
            contract_family=selected_contract_family,
            # Undated rankings use the retained observation/check date as
            # source_week, not as a fabricated source-published week.
            source_week=(
                retained_reference_date
                if selected_contract_family == "ranking-table" and ranking_undated_declared(selected_contract_metadata)
                else None
            ),
        )
        if selected_contract_family == "publisher-frontlist":
            from autoscout.publisher_frontlist import frontlist_cards as publisher_frontlist_cards

            publisher_retained_card = next(
                publisher_card
                for publisher_card in publisher_frontlist_cards(page["html"], url)
                if publisher_card.url == sample.url
            )
            entry["primary_authors"] = list(publisher_retained_card.authors)
        if selected_contract_family == "nuevoslibros-frontlist":
            from autoscout.nuevoslibros_frontlist import frontlist_cards

            retained_card = next(card for card in frontlist_cards(page["html"], url) if card.url == sample.url)
            entry["primary_authors"] = list(retained_card.authors)
            entry["publication_year"] = int(retained_card.release_date[:4])
        if detail_route is not None:
            values, detail_error = ranking_detail_fields_from_html(
                detail_page_by_url[sample.url]["html"], detail_route, enforce_category_allowlist=True
            )
            if detail_error:
                raise ReplayFixtureError(f"ranking detail evidence is not grounded: {detail_error}")
            # The raw detail category is evidence for the allowlist gate only.
            # Keep the configured canonical category in the extraction envelope,
            # and copy only the required ISBN from the bounded detail page.
            if detail_route.has_isbn:
                entry["isbn"] = values["isbn"]
        extraction_entries.append(entry)
    extraction: dict[str, Any] = {
        "source_name": source_name,
        "source_url": url,
        "operation_id": operation_id,
        "capture_sha256": capture_sha256,
        "mentions": extraction_entries,
    }
    extraction_bytes = _serialize(extraction)

    # Self-check: every extraction evidence URL must be present and grounded
    # in the generated page, using the same grounding seam the replay
    # collector enforces. This is an internal invariant; a violation means a
    # generation defect, not an operator error.
    grounding_html = page["html"]
    if selected_contract_family == "publisher-frontlist":
        from autoscout.publisher_frontlist import linked_html as publisher_linked_html

        grounding_html = publisher_linked_html(grounding_html, url)
    for entry in extraction["mentions"]:
        if selected_contract_family == "ranking-table":
            if not ranking_mention_is_grounded(
                page["html"],
                title=entry["title"],
                primary_authors=[str(author) for author in entry["primary_authors"]],
                isbn=entry["isbn"],
                current_rank=entry["current_rank"],
                source_week=entry["source_week"],
                ranking_source=ranking_source_metadata,
                ranking_source_statement=str(ranking_source_metadata.get("ranking_source_statement", "")) or None,
                detail_pages=(page.get("detail_pages") if isinstance(page.get("detail_pages"), list) else None),
                evidence_url=str(entry.get("evidence_url", "")),
                candidate_category=str(entry.get("candidate_category", "")),
                detail_route=detail_route,
                identity_html=str(page.get("identity_html", "")),
            ):
                raise ReplayFixtureError("generated ranking extraction entry is not grounded in the generated page")
        else:
            if not exact_book_list_mention_is_grounded(
                grounding_html,
                page["source_page"],
                title=entry["title"],
                authors=entry["primary_authors"],
                evidence_url=entry["evidence_url"],
            ):
                raise ReplayFixtureError("generated extraction entry is not grounded in the generated page")
            verified, _reason = evidence_url_verified(page, entry["evidence_url"])
            if not verified:
                raise ReplayFixtureError("generated extraction evidence URL is not present in the generated page")

    return ReplayFixturePair(
        url=url,
        slug=slug,
        page=page,
        extraction=extraction,
        page_bytes=page_bytes,
        extraction_bytes=extraction_bytes,
        capture_sha256=capture_sha256,
        operation_id=operation_id,
        entry_count=len(extraction["mentions"]),
    )


def _resolve_destination(path: Path, label: str) -> Any:
    """Validate one fixture output destination via the shared public policy."""
    try:
        return resolve_storage_destination(path, "file", label)
    except SafeArtifactError as exc:
        raise ReplayFixtureError(_error_text(exc)) from exc


def _write_pair_under_lock(pair: ReplayFixturePair, page_output: Path, extraction_output: Path) -> str:
    """Resolve, check, and publish one pair inside the held pair lock.

    Destination resolution and validation are re-run inside the critical
    section, so the complete check-then-publish sequence is atomic with
    respect to concurrent writers of the same pair: at most one writer
    creates the pair, and any other writer observes the finished pair and
    either reports ``unchanged`` (identical bytes) or refuses safely."""
    page_destination = _resolve_destination(page_output, "page fixture output")
    extraction_destination = _resolve_destination(extraction_output, "extraction fixture output")
    if page_destination.path == extraction_destination.path:
        raise ReplayFixtureError("page fixture output and extraction fixture output must be different files")
    page_path = page_destination.path
    extraction_path = extraction_destination.path
    page_path.parent.mkdir(parents=True, exist_ok=True)
    extraction_path.parent.mkdir(parents=True, exist_ok=True)
    page_exists = page_path.exists()
    extraction_exists = extraction_path.exists()
    if page_exists and extraction_exists:
        if page_path.read_bytes() == pair.page_bytes and extraction_path.read_bytes() == pair.extraction_bytes:
            return "unchanged"
        raise ReplayFixtureError("existing fixture pair differs from the generated pair; refusing to overwrite")
    if page_exists and page_path.read_bytes() != pair.page_bytes:
        raise ReplayFixtureError("existing page fixture differs from the generated page; refusing to overwrite")
    if extraction_exists and extraction_path.read_bytes() != pair.extraction_bytes:
        raise ReplayFixtureError(
            "existing extraction fixture differs from the generated extraction; refusing to overwrite"
        )
    publish_pair(page_path, extraction_path, pair.page_bytes, pair.extraction_bytes)
    if page_exists or extraction_exists:
        return "completed"
    return "created"


def write_replay_fixture_pair(pair: ReplayFixturePair, page_output: Path, extraction_output: Path) -> str:
    """Publish one fixture pair atomically and idempotently.

    The complete pair operation - resolving and validating both
    destinations, checking the existing bytes of both files, and publishing
    both files - runs under the shared cross-process advisory pair lock from
    ``autoscout.safe_artifacts`` (``pair_write_lock``), so two concurrent
    writers of the same pair can never interleave their publications and
    leave a page/extraction pair with a mismatched ``capture_sha256``.

    Returns ``created`` for a fresh pair, ``unchanged`` for an exact retry,
    and ``completed`` when exactly one half already existed with identical
    bytes and the missing half was written. An existing pair (or half) whose
    bytes differ from the generated pair is never overwritten: the write
    raises :class:`ReplayFixtureError` and leaves the existing files intact,
    so a conflicting retry can never corrupt a valid pair. Outputs that
    resolve to the same file are rejected before any directory or file is
    created, because publishing two fixtures into one destination would
    silently leave only the extraction behind."""
    page_destination = _resolve_destination(page_output, "page fixture output")
    extraction_destination = _resolve_destination(extraction_output, "extraction fixture output")
    if page_destination.path == extraction_destination.path:
        raise ReplayFixtureError("page fixture output and extraction fixture output must be different files")
    with pair_write_lock(page_destination.path, extraction_destination.path):
        return _write_pair_under_lock(pair, page_output, extraction_output)


def run_capture_source_fixtures(
    url: str,
    slug: str,
    probe_evidence: Path,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    page_output: Path | None = None,
    extraction_output: Path | None = None,
    source_name: str = "",
    signal_family: str = "",
    contract_family: str = "",
    contract_metadata: Mapping[str, Any] | None = None,
    signal_strength: str,
    candidate_category: str,
    language: str = "en",
    expected_sort_label: str = "",
    helper_metadata_file: Path | None = None,
) -> tuple[dict[str, Any], int]:
    """Generate and publish one fixture pair; return the structured report.

    *output_dir* supplies the default directory for the standard pair names;
    explicit *page_output* and *extraction_output* paths override either half
    independently. *helper_metadata_file* names the retained helper sidecar
    consumed before fixture publication. The report carries the status
    (``created``,
    ``unchanged``, or ``completed``), the output paths, the entry count, and
    the deterministic capture digest, plus the contract exit code 0."""
    evidence = _load_evidence(probe_evidence)
    pair = build_replay_fixture_pair(
        url,
        slug,
        evidence,
        source_name=source_name,
        signal_family=signal_family,
        contract_family=contract_family,
        contract_metadata=contract_metadata,
        signal_strength=signal_strength,
        candidate_category=candidate_category,
        language=language,
        expected_sort_label=expected_sort_label,
        helper_metadata_file=helper_metadata_file,
    )
    if page_output is None or extraction_output is None:
        page_name, extraction_name = _output_names(slug, url)
        if page_output is None:
            page_output = Path(output_dir) / page_name
        if extraction_output is None:
            extraction_output = Path(output_dir) / extraction_name
    status = write_replay_fixture_pair(pair, page_output, extraction_output)
    report = {
        "version": 1,
        "status": status,
        "url": pair.url,
        "slug": pair.slug,
        "page_output": str(page_output),
        "extraction_output": str(extraction_output),
        "entry_count": pair.entry_count,
        "contract_family": str(pair.page.get("contract_family", "generic")),
        "capture_sha256": pair.capture_sha256,
        "operation_id": pair.operation_id,
        "exit_code": 0,
    }
    return report, 0


def capture_source_fixtures(
    url: str,
    slug: str,
    probe_evidence: Path,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    page_output: Path | None = None,
    extraction_output: Path | None = None,
    source_name: str = "",
    signal_family: str = "",
    contract_family: str = "",
    contract_metadata: Mapping[str, Any] | None = None,
    signal_strength: str,
    candidate_category: str,
    language: str = "en",
    expected_sort_label: str = "",
    helper_metadata_file: Path | None = None,
) -> int:
    """Print one JSON report for a fixture generation and return its exit code."""
    try:
        report, code = run_capture_source_fixtures(
            url,
            slug,
            probe_evidence,
            output_dir=output_dir,
            page_output=page_output,
            extraction_output=extraction_output,
            source_name=source_name,
            signal_family=signal_family,
            contract_family=contract_family,
            contract_metadata=contract_metadata,
            signal_strength=signal_strength,
            candidate_category=candidate_category,
            language=language,
            expected_sort_label=expected_sort_label,
            helper_metadata_file=helper_metadata_file,
        )
    except (OSError, ReplayFixtureError, ValueError) as exc:
        print(json.dumps({"version": 1, "error": _error_text(exc)}, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return code
