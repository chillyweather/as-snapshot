"""Verification must not discard a correct agent-written description (issue #791).

In the second 2026-10-10 weekly run the agent returned a finished, resolved,
high-confidence Russian description for 29 books, and code verification
dropped every citation, so each book kept its title-derived stub or the
placeholder. The publisher page for *A Brief History of the World in 47
Borders* printed the title, the author, and the ISBN, and was dropped as
``author_unverifiable`` because the author was not where the check looked.

Three rules are pinned here:

- a cited page is this book when it prints the candidate's ISBN, or prints
  the title and, stating no author of its own, an author surname in its
  visible text;
- a failed re-fetch is retried once and is never disproof;
- a resolved high-confidence description whose citations could not be
  confirmed replaces a stub or the placeholder as unverified text, with the
  review's unconfirmed warning, and is not searched again every run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from adjudication_test_support import AUTHOR
from adjudication_test_support import OPERATION_DATE
from adjudication_test_support import TITLE
from adjudication_test_support import VALID_TEXT
from adjudication_test_support import FakeTransport
from adjudication_test_support import citation
from adjudication_test_support import page
from adjudication_test_support import response
from adjudication_test_support import workspace
from state_store_support import read_state
from state_store_support import seed_state

from autoscout.__main__ import main
from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.content_description import DESCRIPTION_UNCONFIRMED_STATUS
from autoscout.content_description import apply_description_artifacts
from autoscout.content_description import description_coverage
from autoscout.content_description import prepare_backfill_work_items
from autoscout.description_adjudication import DECISION_APPLY_UNVERIFIED
from autoscout.description_adjudication import DECISION_FALLBACK
from autoscout.description_adjudication import REASON_AUTHOR_MISMATCH
from autoscout.description_adjudication import REASON_AUTHOR_UNVERIFIABLE
from autoscout.description_adjudication import REASON_FEWER_THAN_TWO
from autoscout.description_adjudication import REASON_SEARCH_OR_FETCH_FAILURE
from autoscout.description_adjudication import REASON_TITLE_MISMATCH
from autoscout.description_adjudication import ReplayDescriptionAdjudicationAdapter
from autoscout.description_adjudication import adjudicate_descriptions
from autoscout.description_adjudication import plan_adjudication_application
from autoscout.description_adjudication import verify_adjudication_response
from autoscout.grounded_description import BoundedHTTPError
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import page_prints_isbn
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.review_rendering import assemble_cumulative_review

PUBLISHER_URL = "https://publisher.example.test/catalog/work-one"
REVIEW_URL = "https://review.example.test/work-one"
ISBN = "9798893030822"
IDENTITY_KEY = "work one|alex author"
STUB = (
    "Мифологический атлас связывает древнегреческие предания с реальными ландшафтами, "
    "городами и островами Греции. Его редакционная особенность — возможность читать мифы "
    "одновременно как истории, культурную географию и маршрут по сохранившимся местам памяти."
)

# The shape of the publisher page the editor reported: a site-name title, no
# author metadata, and the book's title, author, and ISBN only in the body.
PUBLISHER_HTML = (
    "<html><head><title>Example Press</title></head><body>"
    "<h1>Work One</h1><h2>Alex Author</h2>"
    "<div>A history of memory and public life in a small town.</div>"
    "<div>ISBN 979-8-89303-082-2</div></body></html>"
)


def fetcher(pages: dict[str, str | None], transport: Any = None) -> BoundedHTTPFetcher:
    """Return the real fetcher without per-host spacing, so a retry costs no wall clock."""
    return BoundedHTTPFetcher(transport=transport or FakeTransport(pages), min_host_interval_seconds=0)


def _verify(
    citations: list[dict[str, Any]],
    pages: dict[str, str | None],
    *,
    candidate_isbn: str = "",
    transport: Any = None,
    resolved: bool = True,
) -> dict[str, Any]:
    return verify_adjudication_response(
        {"identity_key": IDENTITY_KEY, "resolved": resolved, "confidence": "high", "citations": citations},
        candidate_id="candidate-791",
        identity_key=IDENTITY_KEY,
        fetcher=fetcher(pages, transport),
        checked_date=OPERATION_DATE,
        candidate_isbn=candidate_isbn,
    )


def _reasons(verification: dict[str, Any]) -> list[str]:
    return [item["reason"] for item in verification["dropped"]]


def _stub_record(provenance: str = "codex-generated", text: str = STUB) -> dict[str, Any]:
    return {
        "candidate_id": "candidate-791",
        "identity_key": IDENTITY_KEY,
        "title": TITLE,
        "primary_authors": [AUTHOR],
        "isbn": ISBN,
        "first_seen_date": "2026-09-01",
        "last_seen_date": OPERATION_DATE,
        "content_description": text,
        "description_provenance": {
            "identity_key": IDENTITY_KEY,
            "content_description": text,
            "provenance": provenance,
            "applied_date": "2026-09-01",
            "evidence_basis": [],
        },
    }


def _unfetchable_response(confidence: str = "high") -> dict[str, Any]:
    return response(
        IDENTITY_KEY,
        [
            citation(PUBLISHER_URL, "publisher.example.test", "publisher"),
            citation(REVIEW_URL, "review.example.test", "trade_review"),
        ],
        confidence=confidence,
    )


def _run_stage(tmp_path: Path, history: list[dict[str, Any]], **kwargs: Any) -> tuple[dict[str, Any], Path, Path]:
    state_dir, output_dir = workspace(tmp_path, history)
    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=ReplayDescriptionAdjudicationAdapter({IDENTITY_KEY: _unfetchable_response()}),
        fetcher=fetcher({}),
        apply=True,
        effective_date=OPERATION_DATE,
        operation_id="issue-791",
        **kwargs,
    )
    return result, state_dir, output_dir


# --- Change 1: the ISBN, or the title and a surname in the visible text. ---


def test_a_page_that_prints_the_isbn_is_this_book_without_an_author_match() -> None:
    cited = citation(PUBLISHER_URL, "publisher.example.test", authors=["Someone Unprinted"], title="Another Title")

    verification = _verify([cited], {PUBLISHER_URL: PUBLISHER_HTML}, candidate_isbn=ISBN)

    assert verification["dropped"] == []
    assert [item["identity_proof"] for item in verification["surviving"]] == ["isbn"]
    # The claimed title and author are not on the page, so they are not
    # recorded as confirmed printed metadata.
    assert [item["printed_metadata_confirmed"] for item in verification["surviving"]] == [False]


def test_the_reported_publisher_page_survives_on_its_visible_title_and_author() -> None:
    verification = _verify([citation(PUBLISHER_URL, "publisher.example.test")], {PUBLISHER_URL: PUBLISHER_HTML})

    assert verification["dropped"] == []
    assert [item["identity_proof"] for item in verification["surviving"]] == ["printed_text"]


def test_a_surname_in_a_related_books_section_is_not_this_books_author() -> None:
    html = (
        "<html><head><title>Example Press</title></head><body><h1>Work One</h1>"
        '<section class="related-products">Other Work by Alex Author</section></body></html>'
    )

    verification = _verify([citation(PUBLISHER_URL, "publisher.example.test")], {PUBLISHER_URL: html})

    assert _reasons(verification) == [REASON_AUTHOR_UNVERIFIABLE]


def test_visible_text_never_overrides_an_author_the_page_states() -> None:
    html = page(author="Blake Writer").replace("</main>", "<p>Reviewed by Alex Author.</p></main>")

    verification = _verify([citation(PUBLISHER_URL, "publisher.example.test")], {PUBLISHER_URL: html})

    assert _reasons(verification) == [REASON_AUTHOR_MISMATCH]


def test_a_review_page_that_states_only_its_reviewer_confirms_the_book_author() -> None:
    html = (
        '<html><head><title>Work One</title><meta name="author" content="Riley Reviewer"></head><body><main>'
        "<h1>Work One</h1><p>Riley Reviewer on the new history by Alex Author of memory and public life.</p>"
        "</main></body></html>"
    )

    kept = _verify([citation(REVIEW_URL, "review.example.test", "trade_review")], {REVIEW_URL: html})
    absent = _verify(
        [citation(REVIEW_URL, "review.example.test", "trade_review", authors=["Casey Other"])], {REVIEW_URL: html}
    )

    assert kept["dropped"] == []
    assert _reasons(absent) == [REASON_AUTHOR_MISMATCH]


def test_another_books_isbn_proves_nothing() -> None:
    cited = citation(PUBLISHER_URL, "publisher.example.test", title="Another Title")

    verification = _verify([cited], {PUBLISHER_URL: PUBLISHER_HTML}, candidate_isbn="9780306406157")

    assert _reasons(verification) == [REASON_TITLE_MISMATCH]


@pytest.mark.parametrize(
    ("isbn", "text", "printed"),
    [
        ("9780306406157", "ISBN: 978-0-306-40615-7", True),
        ("9780306406157", "ISBN-10 0-306-40615-2", True),
        ("0306406152", "EAN 9780306406157", True),
        ("9780306406157", "order 19780306406157123", False),
        ("9780306406157", "ISBN 9780306406158", False),
        ("not-an-isbn", "not-an-isbn", False),
    ],
)
def test_a_printed_isbn_matches_either_length_and_stands_alone(isbn: str, text: str, printed: bool) -> None:
    assert page_prints_isbn(isbn, text) is printed


# --- Change 2: a failed re-fetch is retried once and is not disproof. ---


class _FlakyTransport(FakeTransport):
    """Refuse the first ``failures`` requests, then serve the page."""

    def __init__(self, pages: dict[str, str | None], failures: int) -> None:
        super().__init__(pages)
        self._failures = failures

    def request(self, url: str, **kwargs: Any) -> HTTPResponse:
        if len(self.requested_urls) < self._failures:
            with self._lock:
                self.requested_urls.append(url)
            raise BoundedHTTPError("page response was not successful")
        return super().request(url, **kwargs)


def test_a_page_that_answers_the_second_request_is_verified() -> None:
    transport = _FlakyTransport({PUBLISHER_URL: page()}, failures=1)

    verification = _verify([citation(PUBLISHER_URL, "publisher.example.test")], {}, transport=transport)

    assert transport.requested_urls == [PUBLISHER_URL, PUBLISHER_URL]
    assert verification["dropped"] == []


def test_a_page_that_never_answers_is_fetched_twice_and_dropped() -> None:
    transport = FakeTransport({})

    verification = _verify([citation(PUBLISHER_URL, "publisher.example.test")], {}, transport=transport)

    assert transport.requested_urls == [PUBLISHER_URL, PUBLISHER_URL]
    assert verification["dropped"] == [
        {
            "url": PUBLISHER_URL,
            "host": "publisher.example.test",
            "reason": REASON_SEARCH_OR_FETCH_FAILURE,
            "describes_this_work": True,
        }
    ]


# --- Change 3: an unconfirmed high-confidence description is kept. ---


def _plan(agent_response: dict[str, Any], pages: dict[str, str | None], **kwargs: Any) -> dict[str, Any]:
    verification = _verify(agent_response["citations"], pages, resolved=agent_response["resolved"])
    return plan_adjudication_application(
        agent_response, verification, identity_matched=True, candidate_id="candidate-791", **kwargs
    )


def test_an_unconfirmed_high_confidence_description_is_planned_as_unverified() -> None:
    plan = _plan(_unfetchable_response(), {}, allow_unverified=True)

    assert plan["decision"] == DECISION_APPLY_UNVERIFIED
    assert plan["artifact"]["content_description"] == VALID_TEXT
    assert plan["artifact"]["research_sources"] == [PUBLISHER_URL, REVIEW_URL]
    assert plan["artifact"]["owner_count"] == 0
    assert "evidence_digests" not in plan["artifact"]
    assert "stage_outcome" not in plan["artifact"]


def test_unverified_text_is_never_planned_unless_the_caller_allows_it() -> None:
    plan = _plan(_unfetchable_response(), {})

    assert (plan["decision"], plan["reason"]) == (DECISION_FALLBACK, REASON_FEWER_THAN_TWO)


@pytest.mark.parametrize("confidence", ["medium", "low"])
def test_below_high_confidence_stays_at_fallback(confidence: str) -> None:
    plan = _plan(_unfetchable_response(confidence), {}, allow_unverified=True)

    assert (plan["decision"], plan["reason"]) == (DECISION_FALLBACK, REASON_FEWER_THAN_TWO)


def test_a_page_naming_a_different_author_stays_at_fallback() -> None:
    plan = _plan(_unfetchable_response(), {PUBLISHER_URL: page(author="Blake Writer")}, allow_unverified=True)

    assert (plan["decision"], plan["reason"]) == (DECISION_FALLBACK, REASON_FEWER_THAN_TWO)


def test_a_page_naming_a_different_title_and_author_stays_at_fallback() -> None:
    pages = {PUBLISHER_URL: page(title="Different Work", author="Blake Writer")}

    plan = _plan(_unfetchable_response(), pages, allow_unverified=True)

    assert (plan["decision"], plan["reason"]) == (DECISION_FALLBACK, REASON_FEWER_THAN_TWO)


def test_a_page_the_agent_excluded_does_not_veto_unverified_text() -> None:
    agent_response = response(
        IDENTITY_KEY,
        [
            citation(PUBLISHER_URL, "publisher.example.test"),
            citation(REVIEW_URL, "review.example.test", "trade_review", describes=False),
        ],
    )

    plan = _plan(agent_response, {REVIEW_URL: page(author="Blake Writer")}, allow_unverified=True)

    assert plan["decision"] == DECISION_APPLY_UNVERIFIED
    assert plan["artifact"]["research_sources"] == [PUBLISHER_URL]


def test_a_discovery_only_isbn_is_not_identity_proof(tmp_path: Path) -> None:
    learned = {**_stub_record(), "isbn_discovery_only": True}
    state_dir, output_dir = workspace(tmp_path, [learned])
    wrong_book = PUBLISHER_HTML.replace("Work One", "Different Work").replace("Alex Author", "Blake Writer")

    adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=ReplayDescriptionAdjudicationAdapter(
            {IDENTITY_KEY: response(IDENTITY_KEY, [citation(PUBLISHER_URL, "publisher.example.test")])}
        ),
        fetcher=fetcher({PUBLISHER_URL: wrong_book}),
        apply=True,
        effective_date=OPERATION_DATE,
    )

    (row,) = read_json(output_dir / "description-adjudication-preview.json")["preview"]
    assert row["verification"]["surviving"] == []
    assert [item["reason"] for item in row["verification"]["dropped"]] == [REASON_TITLE_MISMATCH]


def test_an_unresolved_or_thin_or_uncited_answer_stays_at_fallback() -> None:
    unresolved = {**_unfetchable_response(), "resolved": False}
    thin = {**_unfetchable_response(), "content_description": "Книга о памяти и обществе."}
    uncited = {**_unfetchable_response(), "citations": []}
    not_this_work = response(IDENTITY_KEY, [citation(PUBLISHER_URL, "publisher.example.test", describes=False)])

    for agent_response in (unresolved, thin, uncited, not_this_work):
        assert _plan(agent_response, {}, allow_unverified=True)["decision"] == DECISION_FALLBACK


@pytest.mark.parametrize("existing", [_stub_record(), {**_stub_record(), "content_description": ""}])
def test_the_stage_replaces_a_stub_or_placeholder_with_unverified_text(tmp_path: Path, existing: dict) -> None:
    existing = {
        key: value for key, value in existing.items() if existing["content_description"] or "description" not in key
    }

    result, state_dir, output_dir = _run_stage(tmp_path, [existing])

    assert result["applied"] == 1
    (saved,) = read_state(state_dir, "candidate_history")
    assert saved["content_description"] == VALID_TEXT
    assert saved["description_provenance"]["provenance"] == "agent-unverified"
    assert saved["description_provenance"]["evidence_basis"] == [PUBLISHER_URL, REVIEW_URL]
    assert saved["description_provenance"]["owner_count"] == 0
    assert "evidence_digests" not in saved["description_provenance"]
    assert "grounded_outcome" not in saved
    (row,) = read_json(output_dir / "description-adjudication-preview.json")["preview"]
    assert row["decision"] == DECISION_APPLY_UNVERIFIED
    assert row["applied_detail"]["provenance"] == "agent-unverified"
    assert row["applied_detail"]["status_text"] == DESCRIPTION_UNCONFIRMED_STATUS
    stage = read_json(output_dir / "description-adjudication-stage.json")
    assert stage["adjudication_counts"][DECISION_APPLY_UNVERIFIED] == 1
    assert stage["counts"]["applied"] == 1


def test_unverified_text_never_replaces_a_sourced_description(tmp_path: Path) -> None:
    # A thin sourced description is a depth repair, so naming it reaches the agent.
    sourced = _stub_record("grounded", "Мемуарная книга о дружбе, любви и трудном личном опыте.")

    result, state_dir, output_dir = _run_stage(tmp_path, [sourced], only=[IDENTITY_KEY])

    assert result["applied"] == 0
    (saved,) = read_state(state_dir, "candidate_history")
    assert saved["content_description"] == sourced["content_description"]
    assert saved["description_provenance"] == sourced["description_provenance"]
    (row,) = read_json(output_dir / "description-adjudication-preview.json")["preview"]
    assert (row["decision"], row["decision_reason"]) == (DECISION_FALLBACK, REASON_FEWER_THAN_TWO)


def test_the_apply_gate_refuses_unverified_text_over_a_sourced_description() -> None:
    sourced = _stub_record("grounded")
    artifact = {"identity_key": IDENTITY_KEY, "content_description": VALID_TEXT}

    history, outcome = apply_description_artifacts(
        [sourced],
        [artifact],
        provenance="agent-unverified",
        allow_overwrite_valid=True,
        allow_repair_replacement=True,
        allow_unsourced_replacement=True,
    )

    assert outcome["applied"] == 0
    assert history == [sourced]


def test_the_review_warns_and_coverage_counts_unverified_text_as_unsourced(tmp_path: Path) -> None:
    _result, state_dir, _output_dir = _run_stage(tmp_path, [_stub_record()])
    history = read_state(state_dir, "candidate_history")

    rows, summary = assemble_cumulative_review([], history, [])

    assert rows[0]["content_description"] == VALID_TEXT
    assert rows[0]["description_status"] == DESCRIPTION_UNCONFIRMED_STATUS
    assert summary["description_coverage"]["unsourced_count"] == 1
    assert summary["description_coverage"]["sourced_count"] == 0
    assert description_coverage(history)["unsourced_count"] == 1


def test_unverified_text_is_not_searched_again_every_run(tmp_path: Path) -> None:
    _result, state_dir, output_dir = _run_stage(tmp_path, [_stub_record()])
    adapter = ReplayDescriptionAdjudicationAdapter({IDENTITY_KEY: _unfetchable_response()})

    adjudicate_descriptions(
        state_dir, output_dir, adapter=adapter, fetcher=fetcher({}), apply=True, effective_date=OPERATION_DATE
    )

    assert adapter.calls == []
    history = read_state(state_dir, "candidate_history")
    assert prepare_backfill_work_items(history, include_depth_repair=True, include_unsourced_stubs=True) == []


def test_a_verified_description_replaces_unverified_text() -> None:
    artifact = {"identity_key": IDENTITY_KEY, "content_description": VALID_TEXT, "stage_outcome": "grounded"}

    (saved,), outcome = apply_description_artifacts(
        [_stub_record("agent-unverified")],
        [artifact],
        provenance="agent-adjudicated",
        allow_unsourced_replacement=True,
    )

    assert outcome["applied"] == 1
    assert saved["description_provenance"]["provenance"] == "agent-adjudicated"
    assert saved["grounded_outcome"] == "grounded"


# --- Change 5: the backfill command replaces a stub without the flag. ---


def _backfill(tmp_path: Path, record: dict[str, Any]) -> dict[str, Any]:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    seed_state(state_dir, candidate_history=[record])
    artifacts = tmp_path / "backfill.json"
    write_json(
        artifacts,
        {"backfill_items": [{"identity_key": IDENTITY_KEY, "content_description": VALID_TEXT, "evidence_basis": []}]},
    )

    assert main(["apply-description-backfill", "--state-dir", str(state_dir), "--artifacts-file", str(artifacts)]) == 0

    (saved,) = read_state(state_dir, "candidate_history")
    return saved


@pytest.mark.parametrize("provenance", ["codex-generated", "agent-unverified"])
def test_the_backfill_command_replaces_unconfirmed_text_without_the_overwrite_flag(
    tmp_path: Path, provenance: str
) -> None:
    saved = _backfill(tmp_path, _stub_record(provenance))

    assert saved["content_description"] == VALID_TEXT
    assert saved["description_provenance"]["provenance"] == "backfilled"


@pytest.mark.parametrize("provenance", ["grounded", "agent-adjudicated", "backfilled", "editor-corrected"])
def test_the_backfill_command_still_protects_a_sourced_description(tmp_path: Path, provenance: str) -> None:
    record = _stub_record(provenance)

    assert _backfill(tmp_path, record) == record


def test_the_placeholder_constant_is_not_what_an_unverified_record_shows(tmp_path: Path) -> None:
    _result, state_dir, _output_dir = _run_stage(
        tmp_path, [{**_stub_record(), "content_description": DESCRIPTION_FALLBACK}]
    )

    (saved,) = read_state(state_dir, "candidate_history")
    assert saved["content_description"] == VALID_TEXT
