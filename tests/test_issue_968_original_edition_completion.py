"""Original edition completion for translated books (issue #968)."""

from __future__ import annotations

import copy
import json
import subprocess
from datetime import date
from datetime import timedelta
from pathlib import Path
from typing import Any

from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.__main__ import main
from autoscout.original_edition import ORIGINAL_EDITION_KEYS
from autoscout.original_edition import STATUS_CONFIRMED
from autoscout.original_edition import STATUS_NOT_CHECKED
from autoscout.original_edition import STATUS_NOT_FOUND
from autoscout.original_edition import validate_original_edition
from autoscout.original_edition_completion import PROPOSAL_BATCH_SIZE
from autoscout.original_edition_completion import PROPOSAL_CALL_TIMEOUT_SECONDS
from autoscout.original_edition_completion import PROPOSAL_RESPONSE_SCHEMA
from autoscout.original_edition_completion import RETRY_AFTER_DAYS
from autoscout.original_edition_completion import CodexOriginalTitleProposer
from autoscout.original_edition_completion import run_original_edition_completion
from autoscout.title_evidence import TITLE_TYPE_GENERATED_RENDERING
from autoscout.title_evidence import TITLE_TYPE_SOURCED_PUBLISHED
from autoscout.title_evidence import is_valid_sourced_published_title

TODAY = "2026-11-07"
EVIDENCE_URL = "https://www.trampaediciones.es/libros/ahora-que-se-acerca-el-final"
ATHILL_ID = "candidate-001"
ATHILL_KEY = "ahora que se acerca el final|diana athill"
ATHILL_TITLE = "Ahora que se acerca el final"
CREDIT_PAGE = "<p>Traducción de Regina López Muñoz</p>"
OL_PREFIX = "https://openlibrary.org/search.json"
OL_WORK_URL = "https://openlibrary.org/works/OL1W"
SECOND_PAGE = "https://second.example.test/book"
PLAIN_PAGE = "<p>Una novela sobre la memoria.</p>"
ENGLISH_PROPOSAL = {"original_title": "Somewhere Towards the End", "original_language": "en"}
OL_ENGLISH_DOCS = {
    "docs": [
        {
            "key": "/works/OL1W",
            "title": "Somewhere towards the end",
            "author_name": ["Diana Athill"],
            "first_publish_year": 2008,
            "language": ["eng", "cat"],
        }
    ]
}
OL_SPANISH_DOCS = {
    "docs": [
        {
            "key": "/works/OL9W",
            "title": ATHILL_TITLE,
            "author_name": ["Diana Athill"],
            "first_publish_year": 2024,
            "language": ["spa"],
        }
    ]
}
GENERATED_ENGLISH_TITLE = {
    "english_title": "The end is approaching",
    "english_title_type": TITLE_TYPE_GENERATED_RENDERING,
    "english_title_provenance": {
        "kind": TITLE_TYPE_GENERATED_RENDERING,
        "research_sources": [],
        "mapping": {"original_title": ATHILL_TITLE, "original_authors": ["Diana Athill"]},
    },
}


# ── Fakes ────────────────────────────────────────────────────────────────


class FakeGetter:
    def __init__(self, pages: dict[str, str] | None = None, on_get: Any = None) -> None:
        self.pages = pages or {}
        self.on_get = on_get
        self.requests: list[str] = []
        self.timeouts: list[float | None] = []

    def get(self, url: str, *, timeout: float | None = None) -> str | None:
        self.requests.append(url)
        self.timeouts.append(timeout)
        if self.on_get is not None:
            self.on_get(url)
        for prefix, body in self.pages.items():
            if url.startswith(prefix):
                return body
        return None


class FakeProposer:
    def __init__(
        self,
        proposals: dict[str, dict[str, Any]] | None = None,
        *,
        result: Any = ...,
    ) -> None:
        self.proposals = proposals or {}
        self.result = result
        self.calls: list[list[dict[str, Any]]] = []
        self.timeouts: list[float] = []

    def propose(self, items: Any, *, timeout: float) -> Any:
        self.calls.append([dict(item) for item in items])
        self.timeouts.append(timeout)
        if self.result is not ...:
            return self.result
        return self.proposals


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# ── State files ──────────────────────────────────────────────────────────


def _record(
    *,
    key: str = ATHILL_KEY,
    candidate_id: str = ATHILL_ID,
    title: str = ATHILL_TITLE,
    language: str = "es",
    evidence_url: str = EVIDENCE_URL,
    **extra: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "identity_key": key,
        "candidate_id": candidate_id,
        "title": title,
        "primary_authors": ["Diana Athill"],
        "publisher": "Trampa Ediciones",
        "publication_year": 2024,
        "first_seen_date": "2026-11-01",
        "source_evidence": [
            {
                "source_name": "Trampa Ediciones",
                "source_url": "https://www.trampaediciones.es/",
                "evidence_url": evidence_url,
                "language": language,
            }
        ],
        "description_provenance": {"evidence_basis": [evidence_url]},
    }
    record.update(extra)
    return record


def _workspace(
    tmp_path: Path, history: list[dict[str, Any]], current_keys: list[str] | None = None
) -> tuple[Path, Path]:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir(exist_ok=True)
    output_dir.mkdir(exist_ok=True)
    seed_state(state_dir, candidate_history=history)
    seed_state(state_dir, latest_scouting_candidates=[{"identity_key": key} for key in current_keys or []])
    (output_dir / "scouting-candidates.json").write_text(
        json.dumps([{"identity_key": key} for key in current_keys or []]), encoding="utf-8"
    )
    return state_dir, output_dir


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _history(state_dir: Path) -> list[dict[str, Any]]:
    return read_state(state_dir, "candidate_history")


def _proposal(title: str = "Somewhere Towards the End", language: str = "en") -> dict[str, dict[str, str]]:
    return {ATHILL_ID: {"original_title": title, "original_language": language}}


def _run(state_dir: Path, output_dir: Path, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("getter", FakeGetter({EVIDENCE_URL: CREDIT_PAGE, OL_PREFIX: json.dumps(OL_ENGLISH_DOCS)}))
    kwargs.setdefault("proposer", FakeProposer(_proposal()))
    kwargs.setdefault("effective_date", TODAY)
    kwargs.setdefault("scope", "undecided")
    return run_original_edition_completion(output_dir, state_dir, **kwargs)


# ── The whole pass ───────────────────────────────────────────────────────


def test_athill_end_to_end_records_a_confirmed_english_original(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    getter = FakeGetter({EVIDENCE_URL: CREDIT_PAGE, OL_PREFIX: json.dumps(OL_ENGLISH_DOCS)})
    proposer = FakeProposer(_proposal())

    result = _run(state_dir, output_dir, getter=getter, proposer=proposer)

    assert result["status"] == "ok"
    assert result["scope"] == "undecided"
    assert result["dry_run"] is False
    assert (result["considered"], result["credited"], result["confirmed"]) == (1, 1, 1)
    assert result["no_credit"] == result["no_proposal"] == result["not_found"] == 0
    assert result["english_title_applied"] == 1
    assert result["projected"] == {"current": 1, "latest": 1}

    item = proposer.calls[0][0]
    assert item == {
        "candidate_id": ATHILL_ID,
        "title": ATHILL_TITLE,
        "authors": ["Diana Athill"],
        "publisher": "Trampa Ediciones",
        "publication_year": 2024,
        "source_languages": ["es"],
        "stated_original_title": "",
        "translator_credited": True,
    }
    assert any(url.startswith(OL_PREFIX) for url in getter.requests)

    record = _history(state_dir)[0]
    edition = record["original_edition"]
    assert set(edition) == set(ORIGINAL_EDITION_KEYS)
    assert validate_original_edition(edition)
    assert edition["status"] == STATUS_CONFIRMED
    assert edition["is_translation"] == "yes"
    assert edition["translation_evidence_url"] == EVIDENCE_URL
    assert edition["original_title"] == "Somewhere towards the end"
    assert edition["original_language"] == "en"
    assert edition["language_confirmed"] is True
    assert edition["original_first_published_year"] == 2008
    assert edition["confirmation_url"] == "https://openlibrary.org/works/OL1W"
    assert record["original_edition_lookup_date"] == TODAY

    assert record["english_title"] == "Somewhere towards the end"
    assert record["english_title_type"] == TITLE_TYPE_SOURCED_PUBLISHED
    assert is_valid_sourced_published_title(record) is True

    for rows in (_read(output_dir / "scouting-candidates.json"), read_state(state_dir, "latest_scouting_candidates")):
        row = rows[0]
        assert row["original_edition"]["status"] == STATUS_CONFIRMED
        assert row["original_edition_lookup_date"] == TODAY
        assert row["english_title_type"] == TITLE_TYPE_SOURCED_PUBLISHED
        assert row["english_title"] == "Somewhere towards the end"

    report = _read(output_dir / "original-edition-completion.json")
    assert report["date"] == TODAY and report["confirmed"] == 1


def test_a_record_found_only_on_english_sources_is_not_considered(tmp_path):
    english = _record(key="english|diana athill", candidate_id="candidate-002", language="en")
    state_dir, output_dir = _workspace(tmp_path, [_record(), english], [ATHILL_KEY, "english|diana athill"])
    proposer = FakeProposer(_proposal())

    result = _run(state_dir, output_dir, proposer=proposer)

    assert result["considered"] == 1
    assert [item["candidate_id"] for call in proposer.calls for item in call] == [ATHILL_ID]
    untouched = next(record for record in _history(state_dir) if record["candidate_id"] == "candidate-002")
    assert "original_edition" not in untouched


# ── Step 1: the translator credit ────────────────────────────────────────


def test_no_credit_still_reaches_the_proposer_and_confirmed_original_proves_translation(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    getter = FakeGetter({EVIDENCE_URL: PLAIN_PAGE, OL_PREFIX: json.dumps(OL_ENGLISH_DOCS)})
    proposer = FakeProposer(_proposal())

    result = _run(state_dir, output_dir, getter=getter, proposer=proposer)

    assert result["no_credit"] == 1
    assert result["credited"] == 0
    assert result["confirmed"] == 1
    assert len(proposer.calls) == 1
    assert proposer.calls[0][0]["translator_credited"] is False
    assert proposer.calls[0][0]["stated_original_title"] == ""
    record = _history(state_dir)[0]
    edition = record["original_edition"]
    assert validate_original_edition(edition)
    assert edition["status"] == STATUS_CONFIRMED
    assert edition["is_translation"] == "yes"
    assert edition["translation_evidence_url"] == OL_WORK_URL
    assert edition["confirmation_url"] == OL_WORK_URL
    assert record["original_edition_lookup_date"] == TODAY


def test_a_confirmed_english_original_without_credit_gets_its_sourced_english_title(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    getter = FakeGetter({EVIDENCE_URL: PLAIN_PAGE, OL_PREFIX: json.dumps(OL_ENGLISH_DOCS)})
    proposer = FakeProposer(_proposal())

    result = _run(state_dir, output_dir, getter=getter, proposer=proposer)

    assert result["confirmed"] == 1
    assert result["english_title_applied"] == 1
    assert result["english_title_not_applied"] == 0
    record = _history(state_dir)[0]
    edition = record["original_edition"]
    assert edition["translation_evidence_url"] == OL_WORK_URL
    assert record["english_title_type"] == TITLE_TYPE_SOURCED_PUBLISHED
    assert record["english_title"] == edition["original_title"] == "Somewhere towards the end"
    assert is_valid_sourced_published_title(record)
    assert record["english_title_provenance"]["research_sources"] == [OL_WORK_URL]


def test_no_credit_and_a_null_proposal_is_not_found_and_unknown(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    getter = FakeGetter({EVIDENCE_URL: PLAIN_PAGE, OL_PREFIX: json.dumps(OL_ENGLISH_DOCS)})

    result = _run(state_dir, output_dir, getter=getter, proposer=FakeProposer({}))

    assert result["no_credit"] == 1
    assert result["no_proposal"] == 1
    assert result["confirmed"] == 0
    assert not any(url.startswith(OL_PREFIX) for url in getter.requests)
    record = _history(state_dir)[0]
    edition = record["original_edition"]
    assert validate_original_edition(edition)
    assert edition["status"] == STATUS_NOT_FOUND
    assert edition["is_translation"] == "unknown"
    assert edition["translation_evidence_url"] == ""
    assert record["original_edition_lookup_date"] == TODAY


def test_a_record_without_evidence_pages_still_reaches_the_proposer(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record(description_provenance={})], [ATHILL_KEY])
    getter = FakeGetter({OL_PREFIX: json.dumps(OL_ENGLISH_DOCS)})
    proposer = FakeProposer(_proposal())

    result = _run(state_dir, output_dir, getter=getter, proposer=proposer)

    assert result["no_credit"] == 1
    assert result["confirmed"] == 1
    assert EVIDENCE_URL not in getter.requests
    assert proposer.calls[0][0]["translator_credited"] is False
    edition = _history(state_dir)[0]["original_edition"]
    assert validate_original_edition(edition)
    assert edition["is_translation"] == "yes"
    assert edition["translation_evidence_url"] == OL_WORK_URL


def test_every_branch_produces_a_valid_original_edition(tmp_path):
    # Eight records, one per outcome: confirmed with and without a credit,
    # no proposal with and without a credit or evidence pages, a proposal
    # the same-language guard drops, and an unconfirmed proposal with and
    # without a credit.
    def shop(number: int) -> str:
        return f"https://shop.example.test/{number}"

    evidence = {1: shop(1), 2: shop(2), 3: shop(3), 4: "", 5: shop(5), 6: shop(6), 7: shop(7), 8: shop(8)}
    records = []
    for number, url in evidence.items():
        provenance = {} if not url else {"evidence_basis": [url]}
        records.append(
            _record(
                key=f"book {number}|diana athill",
                candidate_id=f"candidate-{number}",
                evidence_url=url or shop(number),
                description_provenance=provenance,
            )
        )
    state_dir, output_dir = _workspace(tmp_path, records, [str(record["identity_key"]) for record in records])
    getter = FakeGetter(
        {
            shop(1): CREDIT_PAGE,
            shop(2): PLAIN_PAGE,
            shop(3): CREDIT_PAGE,
            shop(5): CREDIT_PAGE,
            shop(6): CREDIT_PAGE,
            shop(7): PLAIN_PAGE,
            shop(8): PLAIN_PAGE,
            OL_PREFIX: json.dumps(OL_ENGLISH_DOCS),
        }
    )
    proposer = FakeProposer(
        {
            "candidate-1": ENGLISH_PROPOSAL,
            "candidate-2": ENGLISH_PROPOSAL,
            "candidate-5": {"original_title": "Un autre livre", "original_language": "fr"},
            "candidate-6": {"original_title": ATHILL_TITLE, "original_language": "es"},
            "candidate-8": {"original_title": "Un autre livre", "original_language": "fr"},
        }
    )

    result = _run(state_dir, output_dir, getter=getter, proposer=proposer)

    assert (result["considered"], result["credited"], result["no_credit"]) == (8, 4, 4)
    assert (result["confirmed"], result["not_found"], result["no_proposal"]) == (2, 2, 4)
    assert len(proposer.calls) == 1
    assert len(proposer.calls[0]) == 8
    history = {record["candidate_id"]: record for record in _history(state_dir)}
    for candidate_id, record in history.items():
        assert validate_original_edition(record["original_edition"]), candidate_id
        assert record["original_edition_lookup_date"] == TODAY, candidate_id
    assert history["candidate-1"]["original_edition"]["translation_evidence_url"] == shop(1)
    assert history["candidate-2"]["original_edition"]["translation_evidence_url"] == OL_WORK_URL
    assert history["candidate-5"]["original_edition"]["translation_evidence_url"] == shop(5)
    assert history["candidate-8"]["original_edition"]["is_translation"] == "unknown"
    assert history["candidate-4"]["original_edition"]["is_translation"] == "unknown"


# ── Step 2: the model proposal ───────────────────────────────────────────


def test_a_failed_proposal_call_leaves_the_record_untouched(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])

    result = _run(state_dir, output_dir, proposer=None)

    assert result["credited"] == 1
    assert result["proposal_unavailable"] == 1
    record = _history(state_dir)[0]
    assert "original_edition" not in record
    assert "original_edition_lookup_date" not in record


class SequenceProposer:
    """Answers each call from a fixed sequence; ``on_call`` runs before each answer."""

    def __init__(self, answers: list[Any], on_call: Any = None) -> None:
        self.answers = list(answers)
        self.on_call = on_call
        self.calls: list[list[dict[str, Any]]] = []
        self.timeouts: list[float] = []

    def propose(self, items: Any, *, timeout: float) -> Any:
        self.calls.append([dict(item) for item in items])
        self.timeouts.append(timeout)
        if self.on_call is not None:
            self.on_call()
        return self.answers.pop(0)


def test_a_stalled_proposal_call_is_retried_once_and_the_batch_is_processed(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    proposer = SequenceProposer([None, _proposal()])

    result = _run(state_dir, output_dir, proposer=proposer)

    assert len(proposer.calls) == 2
    assert proposer.calls[0] == proposer.calls[1]
    assert result["proposal_unavailable"] == 0
    assert result["confirmed"] == 1
    assert _history(state_dir)[0]["original_edition"]["status"] == STATUS_CONFIRMED


def test_a_proposal_call_that_stalls_twice_counts_the_batch_unavailable(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    proposer = SequenceProposer([None, None])

    result = _run(state_dir, output_dir, proposer=proposer)

    assert len(proposer.calls) == 2
    assert result["proposal_unavailable"] == 1
    record = _history(state_dir)[0]
    assert "original_edition" not in record
    assert "original_edition_lookup_date" not in record


def test_no_proposal_retry_once_the_stage_deadline_has_passed(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    clock = FakeClock()

    def stall() -> None:
        # The first call stalls past the whole stage budget.
        clock.now = 10_000.0

    proposer = SequenceProposer([None, _proposal()], on_call=stall)

    result = _run(state_dir, output_dir, proposer=proposer, budget_seconds=100.0, monotonic=clock)

    assert len(proposer.calls) == 1
    assert result["proposal_unavailable"] == 1
    assert "original_edition" not in _history(state_dir)[0]


def test_proposal_batches_hold_ten_books(tmp_path):
    records = [
        _record(key=f"book {number}|diana athill", candidate_id=f"candidate-{number}") for number in range(1, 12)
    ]
    state_dir, output_dir = _workspace(tmp_path, records, [str(record["identity_key"]) for record in records])
    proposer = FakeProposer({})

    _run(state_dir, output_dir, proposer=proposer)

    assert [len(call) for call in proposer.calls] == [10, 1]


def test_a_null_proposal_is_stored_as_not_found(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    getter = FakeGetter({EVIDENCE_URL: CREDIT_PAGE})

    result = _run(state_dir, output_dir, getter=getter, proposer=FakeProposer({}))

    assert result["no_proposal"] == 1
    assert result["confirmed"] == 0
    assert not any(url.startswith(OL_PREFIX) for url in getter.requests)
    record = _history(state_dir)[0]
    assert validate_original_edition(record["original_edition"])
    assert record["original_edition"]["status"] == STATUS_NOT_FOUND
    assert record["original_edition"]["is_translation"] == "yes"
    assert record["original_edition"]["translation_evidence_url"] == EVIDENCE_URL
    assert record["original_edition_lookup_date"] == TODAY


def test_a_proposal_in_the_lists_own_language_never_reaches_open_library(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    getter = FakeGetter({EVIDENCE_URL: CREDIT_PAGE, OL_PREFIX: json.dumps(OL_SPANISH_DOCS)})
    proposer = FakeProposer({ATHILL_ID: {"original_title": ATHILL_TITLE, "original_language": "ES"}})

    result = _run(state_dir, output_dir, getter=getter, proposer=proposer)

    # A credited edition is a translation, so the Spanish edition itself is
    # never its own original: the proposal counts as no proposal at all.
    assert result["credited"] == 1
    assert result["no_proposal"] == 1
    assert result["not_found"] == result["confirmed"] == 0
    assert not any(url.startswith(OL_PREFIX) for url in getter.requests)
    record = _history(state_dir)[0]
    assert validate_original_edition(record["original_edition"])
    assert record["original_edition"]["status"] == STATUS_NOT_FOUND
    assert record["original_edition"]["is_translation"] == "yes"
    assert record["original_edition"]["translation_evidence_url"] == EVIDENCE_URL
    assert record["original_edition_lookup_date"] == TODAY


# ── Step 3: Open Library confirmation ────────────────────────────────────


def test_an_unusable_catalogue_answer_leaves_the_record_untouched(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    getter = FakeGetter({EVIDENCE_URL: CREDIT_PAGE, OL_PREFIX: "<html>not json</html>"})

    result = _run(state_dir, output_dir, getter=getter)

    assert result["lookup_unavailable"] == 1
    assert result["confirmed"] == 0
    record = _history(state_dir)[0]
    assert "original_edition" not in record
    assert "original_edition_lookup_date" not in record


def test_an_unconfirmed_spanish_original_keeps_the_generated_english_title(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record(**copy.deepcopy(GENERATED_ENGLISH_TITLE))], [ATHILL_KEY])
    getter = FakeGetter({EVIDENCE_URL: CREDIT_PAGE, OL_PREFIX: json.dumps(OL_SPANISH_DOCS)})

    result = _run(state_dir, output_dir, getter=getter)

    assert result["not_found"] == 1
    assert result["confirmed"] == 0
    assert result["english_title_applied"] == 0
    record = _history(state_dir)[0]
    assert record["original_edition"]["status"] == STATUS_NOT_FOUND
    assert record["english_title"] == GENERATED_ENGLISH_TITLE["english_title"]
    assert record["english_title_type"] == TITLE_TYPE_GENERATED_RENDERING
    assert record["english_title_provenance"] == GENERATED_ENGLISH_TITLE["english_title_provenance"]
    assert is_valid_sourced_published_title(record) is False


# ── Retry policy ─────────────────────────────────────────────────────────


def _not_checked(**extra: Any) -> dict[str, Any]:
    edition = {
        "is_translation": "unknown",
        "translation_evidence_url": "",
        "status": STATUS_NOT_CHECKED,
        "original_title": "",
        "original_language": "",
        "language_confirmed": False,
        "original_first_published_year": None,
        "confirmation_url": "",
    }
    edition.update(extra)
    return edition


def test_a_recently_looked_up_record_is_skipped(tmp_path):
    recent = (date.fromisoformat(TODAY) - timedelta(days=RETRY_AFTER_DAYS - 1)).isoformat()
    history = [_record(original_edition=_not_checked(), original_edition_lookup_date=recent)]
    state_dir, output_dir = _workspace(tmp_path, history, [ATHILL_KEY])
    getter = FakeGetter()

    result = _run(state_dir, output_dir, getter=getter)

    assert result["skipped_recent"] == 1
    assert result["considered"] == 1
    assert getter.requests == []


def test_a_confirmed_original_is_never_overwritten_by_a_later_run(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    _run(state_dir, output_dir)
    confirmed = copy.deepcopy(_history(state_dir)[0])
    getter = FakeGetter()
    proposer = FakeProposer(_proposal(title="Another Book", language="fr"))

    result = _run(
        state_dir,
        output_dir,
        getter=getter,
        proposer=proposer,
        effective_date="2027-06-01",
    )

    assert result["skipped_recent"] == 1
    assert proposer.calls == []
    assert getter.requests == []
    assert _history(state_dir)[0]["original_edition"] == confirmed["original_edition"]
    assert _history(state_dir)[0]["english_title"] == confirmed["english_title"]


# ── Projection ───────────────────────────────────────────────────────────


def _snapshot_bytes(state_dir: Path, output_dir: Path) -> dict[str, bytes]:
    return {
        "current": (output_dir / "scouting-candidates.json").read_bytes(),
        "latest": state_snapshot(state_dir),
    }


def test_a_second_run_projects_nothing_onto_unchanged_snapshots(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    getter = FakeGetter({EVIDENCE_URL: CREDIT_PAGE, OL_PREFIX: json.dumps(OL_ENGLISH_DOCS)})
    proposer = FakeProposer(_proposal())

    first = _run(state_dir, output_dir, getter=getter, proposer=proposer)
    before = _snapshot_bytes(state_dir, output_dir)
    second = _run(state_dir, output_dir, getter=getter, proposer=proposer)

    assert first["projected"] == {"current": 1, "latest": 1}
    assert second["skipped_recent"] == 1
    assert second["projected"] == {"current": 0, "latest": 0}
    assert _snapshot_bytes(state_dir, output_dir) == before


def test_an_unchanged_not_found_row_is_not_projected_again(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    getter = FakeGetter({EVIDENCE_URL: CREDIT_PAGE, OL_PREFIX: json.dumps({"docs": []})})
    proposer = FakeProposer(_proposal(title="Un autre livre", language="fr"))

    first = _run(state_dir, output_dir, getter=getter, proposer=proposer)
    before = _snapshot_bytes(state_dir, output_dir)
    second = _run(state_dir, output_dir, getter=getter, proposer=proposer)

    assert first["not_found"] == 1
    assert first["projected"] == {"current": 1, "latest": 1}
    assert second["skipped_recent"] == 1
    assert second["projected"] == {"current": 0, "latest": 0}
    assert _snapshot_bytes(state_dir, output_dir) == before


# ── Budget and dry run ───────────────────────────────────────────────────


def test_budget_exhaustion_still_persists_what_was_learned(tmp_path):
    second_key = "second|diana athill"
    first = _record(key="first|diana athill", candidate_id="candidate-001")
    second = _record(key=second_key, candidate_id="candidate-002", evidence_url=SECOND_PAGE)
    state_dir, output_dir = _workspace(tmp_path, [first, second], [first["identity_key"], second_key])
    clock = FakeClock()

    def on_get(url: str) -> None:
        # The first Open Library answer uses up the whole stage budget.
        if url.startswith(OL_PREFIX):
            clock.now = 10_000.0

    getter = FakeGetter(
        {EVIDENCE_URL: CREDIT_PAGE, SECOND_PAGE: CREDIT_PAGE, OL_PREFIX: json.dumps(OL_ENGLISH_DOCS)},
        on_get=on_get,
    )
    proposer = FakeProposer({"candidate-001": ENGLISH_PROPOSAL, "candidate-002": ENGLISH_PROPOSAL})

    result = run_original_edition_completion(
        output_dir,
        state_dir,
        effective_date=TODAY,
        getter=getter,
        proposer=proposer,
        budget_seconds=100.0,
        monotonic=clock,
    )

    assert result["considered"] == 2
    assert result["credited"] == 2
    assert result["confirmed"] == 1
    assert result["budget_exhausted"] == 1
    history = _history(state_dir)
    assert history[0]["original_edition"]["status"] == STATUS_CONFIRMED
    assert history[0]["original_edition_lookup_date"] == TODAY
    assert "original_edition" not in history[1]


def test_a_dry_run_writes_nothing(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    before = state_snapshot(state_dir)

    result = _run(state_dir, output_dir, dry_run=True)

    assert result["dry_run"] is True
    assert result["confirmed"] == 1
    assert state_snapshot(state_dir) == before
    assert not (output_dir / "original-edition-completion.json").exists()


# ── CLI ──────────────────────────────────────────────────────────────────


def _patch_cli(monkeypatch: Any, getter: FakeGetter, proposer: FakeProposer) -> None:
    monkeypatch.setattr("autoscout.cli.common.weekly_run_in_progress", lambda state_dir: False)
    monkeypatch.setattr("autoscout.original_edition_completion.BoundedPageGetter", lambda: getter)
    monkeypatch.setattr("autoscout.original_edition_completion.CodexOriginalTitleProposer", lambda: proposer)


def test_command_previews_without_writing_and_applies_with_apply(tmp_path, capsys, monkeypatch):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    getter = FakeGetter({EVIDENCE_URL: CREDIT_PAGE, OL_PREFIX: json.dumps(OL_ENGLISH_DOCS)})
    _patch_cli(monkeypatch, getter, FakeProposer(_proposal()))
    before = state_snapshot(state_dir)

    preview = main(
        [
            "complete-originals",
            "--state-dir",
            str(state_dir),
            "--output-dir",
            str(output_dir),
            "--run-date",
            TODAY,
        ]
    )
    preview_output = capsys.readouterr().out
    unchanged = state_snapshot(state_dir) == before
    applied = main(
        [
            "complete-originals",
            "--state-dir",
            str(state_dir),
            "--output-dir",
            str(output_dir),
            "--run-date",
            TODAY,
            "--apply",
        ]
    )
    apply_output = capsys.readouterr().out

    assert preview == 3 and unchanged
    assert applied == 0
    assert "status=ok scope=undecided applied=true" in apply_output
    assert "confirmed=1" in apply_output
    assert _history(state_dir)[0]["original_edition"]["status"] == STATUS_CONFIRMED
    written = preview_output + apply_output
    for title in (ATHILL_TITLE, "Athill", "Somewhere"):
        assert title not in written


# ── The Codex proposer ───────────────────────────────────────────────────


class _Runner:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.commands: list[list[str]] = []
        self.timeouts: list[float | None] = []
        self.items: list[dict[str, Any]] = []
        self.schema: dict[str, Any] = {}

    def __call__(self, command: list[str], **kwargs: Any) -> Any:
        self.commands.append(list(command))
        self.timeouts.append(kwargs.get("timeout"))
        isolated = Path(command[command.index("-C") + 1])
        self.items = json.loads((isolated / "items.json").read_text(encoding="utf-8"))
        self.schema = json.loads((isolated / "response-schema.json").read_text(encoding="utf-8"))
        response_path = Path(command[command.index("--output-last-message") + 1])
        if isinstance(self.outcome, tuple) and self.outcome[0] == "exit":
            return subprocess.CompletedProcess(command, int(self.outcome[1]), stdout="", stderr="")
        if isinstance(self.outcome, tuple) and self.outcome[0] == "raw":
            response_path.write_text(str(self.outcome[1]), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        response_path.write_text(json.dumps(self.outcome), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


ITEM = {
    "candidate_id": ATHILL_ID,
    "title": ATHILL_TITLE,
    "authors": ["Diana Athill"],
    "publisher": "Trampa Ediciones",
    "publication_year": 2024,
    "source_languages": ["es"],
    "stated_original_title": "",
    "translator_credited": True,
}


def test_the_codex_proposer_parses_a_schema_valid_response():
    runner = _Runner(
        {
            "proposals": [
                {
                    "candidate_id": ATHILL_ID,
                    "original_title": "Somewhere Towards the End",
                    "original_language": "en",
                }
            ]
        }
    )
    proposer = CodexOriginalTitleProposer(runner=runner, model="", reasoning_effort="")

    proposals = proposer.propose([ITEM], timeout=PROPOSAL_CALL_TIMEOUT_SECONDS)

    assert proposals == {ATHILL_ID: {"original_title": "Somewhere Towards the End", "original_language": "en"}}
    assert runner.items == [ITEM]
    assert runner.schema == PROPOSAL_RESPONSE_SCHEMA
    assert runner.timeouts == [PROPOSAL_CALL_TIMEOUT_SECONDS]
    command = runner.commands[0]
    assert command[:2] == ["codex", "exec"]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--skip-git-repo-check" in command
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert command.index("-C") < command.index("--output-schema")
    assert command[command.index("--output-schema") + 1].endswith("response-schema.json")
    assert command[command.index("--output-last-message") + 1].endswith("response.json")
    assert command[-1] != "items.json" and "items.json" in command[-1]
    # The proposal reads bounded metadata only; it never searches the web.
    assert not any("web_search" in part or "--search" in part for part in command)


def test_a_non_zero_proposer_exit_answers_none():
    runner = _Runner(("exit", 3))

    assert (
        CodexOriginalTitleProposer(runner=runner, model="", reasoning_effort="").propose(
            [ITEM], timeout=PROPOSAL_CALL_TIMEOUT_SECONDS
        )
        is None
    )


def test_a_malformed_proposer_response_answers_none():
    runner = _Runner(("raw", "{not json"))

    assert (
        CodexOriginalTitleProposer(runner=runner, model="", reasoning_effort="").propose(
            [ITEM], timeout=PROPOSAL_CALL_TIMEOUT_SECONDS
        )
        is None
    )


def test_a_schema_invalid_proposer_response_answers_none():
    runner = _Runner({"proposals": [{"candidate_id": ATHILL_ID, "original_title": "X"}]})

    assert (
        CodexOriginalTitleProposer(runner=runner, model="", reasoning_effort="").propose(
            [ITEM], timeout=PROPOSAL_CALL_TIMEOUT_SECONDS
        )
        is None
    )


# ── Concurrent writes during the lookups ─────────────────────────────────

PUBLISHER_SOURCE = "https://publisher.example.test/somewhere-towards-the-end"
MEANWHILE_RENDERING = {
    "english_title": "The end is near",
    "english_title_type": TITLE_TYPE_GENERATED_RENDERING,
    "english_title_provenance": {
        "kind": TITLE_TYPE_GENERATED_RENDERING,
        "research_sources": [],
        "mapping": {"original_title": ATHILL_TITLE, "original_authors": ["Diana Athill"]},
    },
}
MEANWHILE_SOURCED = {
    "english_title": "Somewhere Towards the End",
    "english_title_type": TITLE_TYPE_SOURCED_PUBLISHED,
    "english_title_provenance": {
        "kind": TITLE_TYPE_SOURCED_PUBLISHED,
        "research_sources": [PUBLISHER_SOURCE],
        "mapping": {
            "original_title": ATHILL_TITLE,
            "original_authors": ["Diana Athill"],
            "published_title": "Somewhere Towards the End",
            "published_authors": ["Diana Athill"],
        },
    },
}


def _write_history_meanwhile(state_dir: Path, fields: dict[str, Any]) -> Any:
    """An other writer that sets English-title fields on the first record while the lookups run."""

    def write() -> None:
        history = _history(state_dir)
        history[0].update(copy.deepcopy(fields))
        seed_state(state_dir, candidate_history=history)

    return write


def test_a_title_written_meanwhile_survives_a_pass_that_set_no_english_title(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    proposer = SequenceProposer([{}], on_call=_write_history_meanwhile(state_dir, MEANWHILE_RENDERING))

    result = _run(state_dir, output_dir, proposer=proposer)

    assert result["no_proposal"] == 1
    assert result["english_title_applied"] == 0
    record = _history(state_dir)[0]
    assert record["english_title"] == MEANWHILE_RENDERING["english_title"]
    assert record["english_title_type"] == TITLE_TYPE_GENERATED_RENDERING
    assert record["english_title_provenance"] == MEANWHILE_RENDERING["english_title_provenance"]
    assert record["original_edition"]["status"] == STATUS_NOT_FOUND
    assert record["original_edition_lookup_date"] == TODAY


def test_a_confirmed_english_original_sets_its_sourced_title_over_a_meanwhile_rendering(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    getter = FakeGetter({EVIDENCE_URL: PLAIN_PAGE, OL_PREFIX: json.dumps(OL_ENGLISH_DOCS)})
    proposer = SequenceProposer([_proposal()], on_call=_write_history_meanwhile(state_dir, MEANWHILE_RENDERING))

    result = _run(state_dir, output_dir, getter=getter, proposer=proposer)

    assert result["confirmed"] == 1
    assert result["english_title_applied"] == 1
    record = _history(state_dir)[0]
    assert record["english_title"] == "Somewhere towards the end"
    assert record["english_title_type"] == TITLE_TYPE_SOURCED_PUBLISHED
    assert is_valid_sourced_published_title(record) is True
    assert record["english_title_provenance"]["research_sources"] == [OL_WORK_URL]


def test_a_sourced_title_written_meanwhile_is_kept(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    getter = FakeGetter({EVIDENCE_URL: PLAIN_PAGE, OL_PREFIX: json.dumps(OL_ENGLISH_DOCS)})
    proposer = SequenceProposer([_proposal()], on_call=_write_history_meanwhile(state_dir, MEANWHILE_SOURCED))

    result = _run(state_dir, output_dir, getter=getter, proposer=proposer)

    assert result["confirmed"] == 1
    record = _history(state_dir)[0]
    assert record["original_edition"]["status"] == STATUS_CONFIRMED
    assert record["english_title"] == MEANWHILE_SOURCED["english_title"]
    assert record["english_title_provenance"] == MEANWHILE_SOURCED["english_title_provenance"]


# ── Proposal batches and the stage budget ────────────────────────────────


class ChunkProposer:
    """Answers every item with the English original; the deadline passes during the second call."""

    def __init__(self, clock: FakeClock, deadline_call: int) -> None:
        self.clock = clock
        self.deadline_call = deadline_call
        self.calls: list[list[dict[str, Any]]] = []

    def propose(self, items: Any, *, timeout: float) -> Any:
        self.calls.append([dict(item) for item in items])
        if len(self.calls) == self.deadline_call:
            self.clock.now = 10_000.0
        return {str(item["candidate_id"]): ENGLISH_PROPOSAL for item in items}


def test_a_deadline_during_the_second_chunk_keeps_the_first_chunk_and_stops(tmp_path):
    total = 2 * PROPOSAL_BATCH_SIZE + 5
    records = [
        _record(
            key=f"book {number}|diana athill",
            candidate_id=f"candidate-{number:03d}",
            description_provenance={},
        )
        for number in range(1, total + 1)
    ]
    state_dir, output_dir = _workspace(tmp_path, records, [str(record["identity_key"]) for record in records])
    clock = FakeClock()
    getter = FakeGetter({OL_PREFIX: json.dumps(OL_ENGLISH_DOCS)})
    proposer = ChunkProposer(clock, deadline_call=2)

    result = _run(state_dir, output_dir, getter=getter, proposer=proposer, budget_seconds=1000.0, monotonic=clock)

    assert len(proposer.calls) == 2
    assert result["considered"] == total
    assert result["confirmed"] == PROPOSAL_BATCH_SIZE
    assert result["budget_exhausted"] == total - PROPOSAL_BATCH_SIZE
    history = _history(state_dir)
    for record in history[:PROPOSAL_BATCH_SIZE]:
        assert record["original_edition"]["status"] == STATUS_CONFIRMED
        assert record["original_edition_lookup_date"] == TODAY
    for record in history[PROPOSAL_BATCH_SIZE:]:
        assert "original_edition" not in record
        assert "original_edition_lookup_date" not in record


def test_a_proposal_call_is_clamped_to_the_budget_left(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    clock = FakeClock()

    def spend_fifty_seconds(url: str) -> None:
        # The first page lookup leaves 50 of the 100 seconds.
        clock.now = 50.0

    getter = FakeGetter(
        {EVIDENCE_URL: PLAIN_PAGE, OL_PREFIX: json.dumps(OL_ENGLISH_DOCS)},
        on_get=spend_fifty_seconds,
    )
    proposer = FakeProposer(_proposal())

    result = _run(state_dir, output_dir, getter=getter, proposer=proposer, budget_seconds=100.0, monotonic=clock)

    assert result["confirmed"] == 1
    assert proposer.timeouts == [50.0]
    assert all(timeout <= 100.0 - 50.0 for timeout in proposer.timeouts)


def test_a_retry_is_not_started_with_less_than_the_proposal_floor_left(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    clock = FakeClock()

    def stall() -> None:
        # The first call returns nothing with 20 seconds of the budget left.
        clock.now = 80.0

    proposer = SequenceProposer([None, _proposal()], on_call=stall)

    result = _run(state_dir, output_dir, proposer=proposer, budget_seconds=100.0, monotonic=clock)

    # The first call starts with the whole budget; its retry would have 20 s left and is not made.
    assert len(proposer.calls) == 1
    assert proposer.timeouts == [100.0]
    assert result["proposal_unavailable"] == 1
    assert "original_edition" not in _history(state_dir)[0]


def test_with_ten_seconds_left_no_proposal_call_is_made(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    proposer = FakeProposer(_proposal())

    result = _run(state_dir, output_dir, proposer=proposer, budget_seconds=10.0, monotonic=FakeClock())

    assert proposer.calls == []
    assert result["budget_exhausted"] == 1
    assert result["proposal_unavailable"] == 0
    assert "original_edition" not in _history(state_dir)[0]


def test_the_codex_proposer_uses_the_given_timeout_under_its_ceiling():
    runner = _Runner({"proposals": []})
    proposer = CodexOriginalTitleProposer(runner=runner, model="", reasoning_effort="")

    proposer.propose([ITEM], timeout=45.0)
    proposer.propose([ITEM], timeout=PROPOSAL_CALL_TIMEOUT_SECONDS * 2)

    assert runner.timeouts == [45.0, PROPOSAL_CALL_TIMEOUT_SECONDS]
