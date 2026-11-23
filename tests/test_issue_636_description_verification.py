"""Offline coverage for issue #636: verify every cited page in code.

The adjudication response is never trusted. Each citation is canonicalized,
re-fetched with the existing bounded HTTP fetcher, checked for its claimed
printed title and authors, assigned a code-computed owner, and digested.
Citations that fail the re-fetch or the printed-text check are dropped with
a bounded reason. No page body is retained. Every test is offline: a fake
transport serves small HTML pages to the real ``BoundedHTTPFetcher``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from adjudication_test_support import AUTHOR
from adjudication_test_support import OPERATION_DATE
from adjudication_test_support import SUBJECT
from adjudication_test_support import TITLE
from adjudication_test_support import FakeTransport as _FakeTransport
from adjudication_test_support import citation as _citation
from adjudication_test_support import fetcher as _fetcher
from adjudication_test_support import page as _page
from adjudication_test_support import record as _record
from adjudication_test_support import response as _response
from adjudication_test_support import workspace as _shared_workspace
from state_store_support import read_state
from state_store_support import seed_state

from autoscout.description_adjudication import MAX_ADJUDICATION_IDENTITY_NOTE_LENGTH
from autoscout.description_adjudication import ReplayDescriptionAdjudicationAdapter
from autoscout.description_adjudication import adjudicate_descriptions
from autoscout.description_adjudication import validate_adjudication_response
from autoscout.description_adjudication import verify_adjudication_response
from autoscout.grounded_description import REASON_AUTHOR_MISMATCH
from autoscout.grounded_description import REASON_FEWER_THAN_TWO
from autoscout.grounded_description import REASON_INVALID_SOURCE
from autoscout.grounded_description import REASON_SEARCH_OR_FETCH_FAILURE
from autoscout.grounded_description import REASON_SOURCE_DENYLISTED
from autoscout.grounded_description import REASON_TITLE_MISMATCH
from autoscout.grounded_description import BoundedHTTPError
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import canonicalize_hostname
from autoscout.grounded_description import canonicalize_url
from autoscout.json_io import read_json

PUBLISHER_URL = "https://www.grasset.fr/livre/work-one"
CATALOGUE_URL = "https://catalogue.example.test/notice/work-one"
RETAIL_URL = "https://www.amazon.example.test/dp/WORKONE1"
SECOND_RETAIL_URL = "https://www.fnac.com/a/work-one"
DENYLISTED_URL = "https://www.reddit.com/r/books/work-one"

# The complete retained record for one surviving citation. No page body,
# article text, review prose, blurb, or book sample appears here, and no
# adapter-supplied field survives except the bounded identity note.
# ``owner_tier`` says how the owner policy derived the owner (#1123).
SURVIVING_KEYS = frozenset(
    {
        "url",
        "host",
        "surface_kind",
        "owner",
        "owner_tier",
        "evidence_digest",
        "describes_this_work",
        "identity_note",
        # Code-computed: whether the printed ISBN or the printed title and
        # authors tied the page to the book (issue #791).
        "identity_proof",
        "printed_metadata_confirmed",
        "counts_toward_independence",
    }
)


def test_successful_verification_counts_two_independent_owners() -> None:
    response = _response(
        "work one|alex author",
        [
            _citation(PUBLISHER_URL, "www.grasset.fr", surface_kind="publisher"),
            _citation(CATALOGUE_URL, "catalogue.example.test", surface_kind="library_catalogue"),
        ],
    )

    verification = verify_adjudication_response(
        response,
        candidate_id="candidate-001",
        identity_key="work one|alex author",
        fetcher=_fetcher({canonicalize_url(PUBLISHER_URL): _page(), canonicalize_url(CATALOGUE_URL): _page()}),
        checked_date=OPERATION_DATE,
    )

    assert verification["owner_count"] == 2
    assert verification["reason"] == ""
    assert verification["counting_urls"] == [canonicalize_url(PUBLISHER_URL), canonicalize_url(CATALOGUE_URL)]
    assert [entry["url"] for entry in verification["surviving"]] == verification["counting_urls"]
    assert verification["dropped"] == []
    for entry in verification["surviving"]:
        assert set(entry) == SURVIVING_KEYS
        assert re.fullmatch(r"[0-9a-f]{64}", entry["evidence_digest"]) is not None
        assert entry["counts_toward_independence"] is True


def test_citation_url_and_host_use_existing_canonicalization() -> None:
    response = _response("work one|alex author", [_citation(PUBLISHER_URL, "www.grasset.fr")])

    verification = verify_adjudication_response(
        response, fetcher=_fetcher({canonicalize_url(PUBLISHER_URL): _page()}), checked_date=OPERATION_DATE
    )

    (survived,) = verification["surviving"]
    assert survived["url"] == canonicalize_url(PUBLISHER_URL)
    assert survived["host"] == "grasset.fr" == canonicalize_hostname("www.grasset.fr")
    assert urlsplit(survived["url"]).hostname == "www.grasset.fr"


def test_invented_title_is_dropped_with_bounded_reason() -> None:
    response = _response(
        "work one|alex author",
        [
            _citation(PUBLISHER_URL, "www.grasset.fr", title="An Invented Title"),
            _citation(CATALOGUE_URL, "catalogue.example.test", surface_kind="library_catalogue"),
        ],
    )

    verification = verify_adjudication_response(
        response,
        fetcher=_fetcher({canonicalize_url(PUBLISHER_URL): _page(), canonicalize_url(CATALOGUE_URL): _page()}),
        checked_date=OPERATION_DATE,
    )

    assert verification["owner_count"] == 1
    assert [(entry["url"], entry["reason"]) for entry in verification["dropped"]] == [
        (canonicalize_url(PUBLISHER_URL), REASON_TITLE_MISMATCH)
    ]
    # One dropped citation leaves insufficient evidence. The record says so
    # instead of claiming that no sources exist.
    assert verification["reason"] == REASON_FEWER_THAN_TWO


def test_invented_author_is_dropped_with_bounded_reason() -> None:
    response = _response(
        "work one|alex author",
        [_citation(PUBLISHER_URL, "www.grasset.fr", authors=["Someone Else"])],
    )

    verification = verify_adjudication_response(
        response, fetcher=_fetcher({canonicalize_url(PUBLISHER_URL): _page()}), checked_date=OPERATION_DATE
    )

    assert verification["surviving"] == []
    assert [(entry["url"], entry["reason"]) for entry in verification["dropped"]] == [
        (canonicalize_url(PUBLISHER_URL), REASON_AUTHOR_MISMATCH)
    ]
    assert verification["owner_count"] == 0
    assert verification["reason"] == REASON_FEWER_THAN_TWO


def test_fetch_failure_is_dropped_with_bounded_reason() -> None:
    response = _response("work one|alex author", [_citation(PUBLISHER_URL, "www.grasset.fr")])

    verification = verify_adjudication_response(
        response, fetcher=_fetcher({canonicalize_url(PUBLISHER_URL): None}), checked_date=OPERATION_DATE
    )

    assert verification["surviving"] == []
    assert [(entry["url"], entry["reason"]) for entry in verification["dropped"]] == [
        (canonicalize_url(PUBLISHER_URL), REASON_SEARCH_OR_FETCH_FAILURE)
    ]
    assert verification["reason"] == REASON_FEWER_THAN_TWO


def test_invalid_url_is_dropped_without_fetch() -> None:
    transport = _FakeTransport({})
    response = _response("work one|alex author", [_citation("not a url", "not a host")])

    verification = verify_adjudication_response(
        response, fetcher=BoundedHTTPFetcher(transport=transport), checked_date=OPERATION_DATE
    )

    assert verification["surviving"] == []
    assert [entry["reason"] for entry in verification["dropped"]] == [REASON_INVALID_SOURCE]
    assert transport.requested_urls == []


def test_two_storefronts_collapse_to_one_owner() -> None:
    response = _response(
        "work one|alex author",
        [
            _citation(RETAIL_URL, "www.amazon.example.test", surface_kind="retail_listing"),
            _citation(SECOND_RETAIL_URL, "www.fnac.com", surface_kind="retail_listing"),
        ],
    )

    verification = verify_adjudication_response(
        response,
        fetcher=_fetcher({canonicalize_url(RETAIL_URL): _page(), canonicalize_url(SECOND_RETAIL_URL): _page()}),
        checked_date=OPERATION_DATE,
    )

    # Different hosts, one voice: both print the publisher's marketing blurb,
    # so the owner policy gives every storefront one shared owner.
    assert len(verification["surviving"]) == 2
    assert [entry["owner"] for entry in verification["surviving"]] == ["publisher-supplied-copy"] * 2
    assert [entry["owner_tier"] for entry in verification["surviving"]] == ["shared_owner"] * 2
    assert verification["owner_count"] == 1
    assert verification["counting_urls"] == [canonicalize_url(RETAIL_URL)]
    counted = [entry["counts_toward_independence"] for entry in verification["surviving"]]
    assert counted == [True, False]
    assert verification["reason"] == REASON_FEWER_THAN_TWO


def test_a_publisher_page_and_a_storefront_are_two_owners() -> None:
    """The publisher's own site is its own owner under the owner policy (#1123).

    The retired policy file gave named publishers the storefronts' shared
    owner, and an unnamed publisher shared one counting slot with any shop.
    The owner policy keeps only storefronts together, as the grounded stage
    has since #689, so a publisher page and a storefront count twice.
    """
    response = _response(
        "work one|alex author",
        [
            _citation(PUBLISHER_URL, "www.grasset.fr", surface_kind="publisher"),
            _citation(RETAIL_URL, "www.amazon.example.test", surface_kind="retail_listing"),
        ],
    )

    verification = verify_adjudication_response(
        response,
        fetcher=_fetcher({canonicalize_url(PUBLISHER_URL): _page(), canonicalize_url(RETAIL_URL): _page()}),
        checked_date=OPERATION_DATE,
    )

    assert [entry["owner"] for entry in verification["surviving"]] == ["grasset.fr", "publisher-supplied-copy"]
    assert verification["owner_count"] == 2
    assert verification["reason"] == ""


def test_hosts_of_one_registrable_domain_collapse_in_code() -> None:
    first_url = "https://publisher-shared.example.test/work-one"
    second_url = "https://retail-shared.example.test/work-one"
    response = _response(
        "work one|alex author",
        [
            _citation(first_url, "publisher-shared.example.test", surface_kind="publisher"),
            _citation(second_url, "retail-shared.example.test", surface_kind="retail_listing"),
        ],
    )

    verification = verify_adjudication_response(
        response,
        fetcher=_fetcher({canonicalize_url(first_url): _page(), canonicalize_url(second_url): _page()}),
        checked_date=OPERATION_DATE,
    )

    assert [entry["owner"] for entry in verification["surviving"]] == ["example.test", "example.test"]
    assert verification["owner_count"] == 1


def test_owner_in_response_is_rejected_by_schema() -> None:
    citation = _citation(PUBLISHER_URL, "www.grasset.fr")
    citation["owner"] = "claimed-owner"

    valid, _reason = validate_adjudication_response(_response("work one|alex author", [citation]))

    # The contract carries no owner field, so verification must recompute
    # the owner in code instead of reading one from the response.
    assert valid is False


def test_describes_false_survives_without_counting_and_keeps_bounded_note() -> None:
    long_note = "x" * 500
    response = _response(
        "work one|alex author",
        [
            _citation(PUBLISHER_URL, "www.grasset.fr", surface_kind="publisher"),
            _citation(
                CATALOGUE_URL,
                "catalogue.example.test",
                surface_kind="library_catalogue",
                describes=False,
                note=long_note,
            ),
        ],
    )

    verification = verify_adjudication_response(
        response,
        fetcher=_fetcher({canonicalize_url(PUBLISHER_URL): _page(), canonicalize_url(CATALOGUE_URL): _page()}),
        checked_date=OPERATION_DATE,
    )

    assert len(verification["surviving"]) == 2
    assert verification["owner_count"] == 1
    assert verification["counting_urls"] == [canonicalize_url(PUBLISHER_URL)]
    noted = next(entry for entry in verification["surviving"] if entry["url"] == canonicalize_url(CATALOGUE_URL))
    assert noted["describes_this_work"] is False
    assert noted["counts_toward_independence"] is False
    assert noted["identity_note"] == "x" * MAX_ADJUDICATION_IDENTITY_NOTE_LENGTH


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    return _shared_workspace(tmp_path, [_record("candidate-001", "work one|alex author")])


def test_preview_reports_surviving_dropped_and_reasons_without_page_bodies(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    adapter = ReplayDescriptionAdjudicationAdapter(
        {
            "work one|alex author": _response(
                "work one|alex author",
                [
                    _citation(PUBLISHER_URL, "www.grasset.fr", surface_kind="publisher"),
                    _citation(CATALOGUE_URL, "catalogue.example.test", surface_kind="library_catalogue"),
                    _citation(RETAIL_URL, "www.amazon.example.test", surface_kind="retail_listing", title="Missing"),
                ],
            )
        }
    )
    pages = {
        canonicalize_url(PUBLISHER_URL): _page(),
        canonicalize_url(CATALOGUE_URL): _page(),
        canonicalize_url(RETAIL_URL): _page(),
    }

    result = adjudicate_descriptions(
        state_dir, output_dir, effective_date=OPERATION_DATE, adapter=adapter, fetcher=_fetcher(pages)
    )

    assert result["status"] == "ok"
    preview = read_json(output_dir / "description-adjudication-preview.json")
    (row,) = preview["preview"]
    verification = row["verification"]
    assert [entry["url"] for entry in verification["surviving"]] == [
        canonicalize_url(PUBLISHER_URL),
        canonicalize_url(CATALOGUE_URL),
    ]
    assert [(entry["url"], entry["reason"]) for entry in verification["dropped"]] == [
        (canonicalize_url(RETAIL_URL), REASON_TITLE_MISMATCH)
    ]
    assert verification["owner_count"] == 2
    # No page body, article text, review prose, blurb, or book sample travels
    # into the preview: only the digest of the subject text is retained.
    raw_preview = (output_dir / "description-adjudication-preview.json").read_bytes()
    assert SUBJECT.encode("utf-8") not in raw_preview
    for key in ("subject_text", "display_text", "page_body", "article_text", "blurb"):
        assert key.encode("utf-8") not in raw_preview
    # The command still writes a preview only: history is untouched.
    assert read_state(state_dir, "candidate_history")[0]["content_description"] == ""


def test_consecutive_verified_runs_write_byte_identical_previews(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    adapter = ReplayDescriptionAdjudicationAdapter(
        {"work one|alex author": _response("work one|alex author", [_citation(PUBLISHER_URL, "www.grasset.fr")])}
    )
    pages = {canonicalize_url(PUBLISHER_URL): _page()}

    adjudicate_descriptions(
        state_dir, output_dir, effective_date=OPERATION_DATE, adapter=adapter, fetcher=_fetcher(pages)
    )
    first = (output_dir / "description-adjudication-preview.json").read_bytes()
    adjudicate_descriptions(
        state_dir, output_dir, effective_date=OPERATION_DATE, adapter=adapter, fetcher=_fetcher(pages)
    )
    second = (output_dir / "description-adjudication-preview.json").read_bytes()

    assert first == second


class _RecordingFetcher:
    """Record exactly what verification asks the real fetcher to do."""

    def __init__(self, pages: dict[str, str | None]) -> None:
        self._inner = BoundedHTTPFetcher(transport=_FakeTransport(pages))
        self.calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def fetch(self, result: dict[str, Any], **kwargs: Any) -> Any:
        self.calls.append((dict(result), dict(kwargs)))
        return self._inner.fetch(result, **kwargs)


def test_fetch_states_the_owner_policy_classification_not_the_adapter_label() -> None:
    """The page metadata the fetch sends comes from the owner policy.

    The adapter calls the page a publisher surface; the owner policy knows
    only that the host is its own owner, and that is what the fetch states.
    The named opt-in stays on the call for a host the classifier leaves
    unknown.
    """
    fetcher = _RecordingFetcher({canonicalize_url(PUBLISHER_URL): _page()})
    response = _response("work one|alex author", [_citation(PUBLISHER_URL, "www.grasset.fr")])

    verification = verify_adjudication_response(response, fetcher=fetcher, checked_date=OPERATION_DATE)

    assert verification["owner_count"] == 1
    (discovery, kwargs) = fetcher.calls[0]
    assert discovery["classification"] == "classified"
    assert kwargs["allow_unclassified_host"] is True
    assert discovery["source_kind"] == "independent"
    assert discovery["source_family"] == "independent"
    assert discovery["owner_id"] == "grasset.fr"


def test_denylisted_host_is_dropped_without_a_fetch() -> None:
    """A denylisted host is no evidence in either description stage (#1123)."""
    fetcher = _RecordingFetcher({canonicalize_url(PUBLISHER_URL): _page(), canonicalize_url(DENYLISTED_URL): _page()})
    response = _response(
        "work one|alex author",
        [_citation(PUBLISHER_URL, "www.grasset.fr"), _citation(DENYLISTED_URL, "www.reddit.com")],
    )

    verification = verify_adjudication_response(response, fetcher=fetcher, checked_date=OPERATION_DATE)

    assert [call[0]["url"] for call in fetcher.calls] == [canonicalize_url(PUBLISHER_URL)]
    assert [(entry["host"], entry["reason"]) for entry in verification["dropped"]] == [
        ("reddit.com", REASON_SOURCE_DENYLISTED)
    ]
    assert verification["owner_count"] == 1


def test_fetcher_refuses_an_unclassified_host_without_the_named_opt_in() -> None:
    """Every other caller still meets the closed allowlist."""
    fetcher = _fetcher({canonicalize_url(PUBLISHER_URL): _page()})

    try:
        fetcher.fetch({"url": PUBLISHER_URL, "classification": "unknown"})
    except BoundedHTTPError as error:
        assert "only classified page URLs may be fetched" in str(error)
    else:  # pragma: no cover - the guard must hold
        raise AssertionError("an unclassified host was fetched without the named opt-in")


def test_a_mislabelled_citation_cannot_buy_a_second_counting_slot() -> None:
    """Both hosts are storefronts, and the adapter calls the second one a
    trade review. The owner comes from the owner policy, so the two still
    collapse; the adapter's label is kept for the preview and decides
    nothing.
    """
    response = _response(
        "work one|alex author",
        [
            _citation(RETAIL_URL, "www.amazon.example.test", surface_kind="retail_listing"),
            _citation(SECOND_RETAIL_URL, "www.fnac.com", surface_kind="trade_review"),
        ],
    )

    verification = verify_adjudication_response(
        response,
        fetcher=_fetcher({canonicalize_url(RETAIL_URL): _page(), canonicalize_url(SECOND_RETAIL_URL): _page()}),
        checked_date=OPERATION_DATE,
    )

    assert [entry["surface_kind"] for entry in verification["surviving"]] == ["retail_listing", "trade_review"]
    assert verification["owner_count"] == 1


def test_publishers_with_different_domains_both_count() -> None:
    """Two publishers on different registrable domains are two owners."""
    first_url = "https://publisher-one.example/work-one"
    second_url = "https://publisher-two.example/work-one"
    response = _response(
        "work one|alex author",
        [
            _citation(first_url, "publisher-one.example", surface_kind="publisher"),
            _citation(second_url, "publisher-two.example", surface_kind="publisher"),
        ],
    )

    verification = verify_adjudication_response(
        response,
        fetcher=_fetcher({canonicalize_url(first_url): _page(), canonicalize_url(second_url): _page()}),
        checked_date=OPERATION_DATE,
    )

    assert [entry["owner_tier"] for entry in verification["surviving"]] == ["derived", "derived"]
    assert verification["owner_count"] == 2
    assert verification["reason"] == ""


def test_claimed_title_with_subtitle_matches_a_page_printing_the_short_title() -> None:
    """A subtitle is presentation metadata, so it must not drop a citation.

    The stage compares titles at work level for exactly this reason. The
    printed-title check must use the same normalization, or a correct
    citation is dropped as an invention.
    """
    response = _response(
        "work one|alex author",
        [_citation(PUBLISHER_URL, "www.grasset.fr", title=f"{TITLE}: A Story of Memory")],
    )

    verification = verify_adjudication_response(
        response,
        fetcher=_fetcher({canonicalize_url(PUBLISHER_URL): _page()}),
        checked_date=OPERATION_DATE,
    )

    assert verification["dropped"] == []
    assert len(verification["surviving"]) == 1


def test_an_invented_title_is_still_dropped_at_work_level() -> None:
    """Work-level matching must not weaken the invention check."""
    response = _response(
        "work one|alex author",
        [_citation(PUBLISHER_URL, "www.grasset.fr", title="Another Book Entirely: A Memoir")],
    )

    verification = verify_adjudication_response(
        response,
        fetcher=_fetcher({canonicalize_url(PUBLISHER_URL): _page()}),
        checked_date=OPERATION_DATE,
    )

    assert verification["surviving"] == []
    assert [entry["reason"] for entry in verification["dropped"]] == [REASON_TITLE_MISMATCH]


def test_oversized_adapter_strings_are_bounded_in_the_preview(tmp_path: Path) -> None:
    """The retained response is bounded, like every other retained string."""
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir()
    output_dir.mkdir()
    seed_state(
        state_dir,
        candidate_history=[
            {
                "candidate_id": "candidate-001",
                "identity_key": "work one|alex author",
                "title": TITLE,
                "primary_authors": [AUTHOR],
                "content_description": "",
            }
        ],
    )
    seed_state(state_dir, candidate_visibility=[])
    citation = _citation(PUBLISHER_URL, "www.grasset.fr", describes=False, note="x" * 5000)
    citation["printed_title"] = "y" * 5000
    adapter = ReplayDescriptionAdjudicationAdapter(
        {
            "work one|alex author": {
                "identity_key": "work one|alex author",
                "resolved": True,
                "unresolved_reason": "z" * 5000,
                "confidence": "high",
                "content_description": "щ" * 9000,
                "citations": [citation],
            }
        }
    )

    adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=adapter,
        fetcher=_fetcher({canonicalize_url(PUBLISHER_URL): _page()}),
    )

    (row,) = read_json(output_dir / "description-adjudication-preview.json")["preview"]
    retained = row["response"]
    assert len(retained["unresolved_reason"]) == 200
    assert len(retained["content_description"]) == 2000
    assert len(retained["citations"][0]["identity_note"]) == MAX_ADJUDICATION_IDENTITY_NOTE_LENGTH
    assert len(retained["citations"][0]["printed_title"]) == 300
