"""Issue #684: grounded verified URLs as search hints for adjudication.

Hints are starting points only, never evidence. Verification still
re-fetches and checks every cited page in code.
"""

from __future__ import annotations

import json
from pathlib import Path

from state_store_support import seed_state

from autoscout.content_description import prepare_backfill_work_items
from autoscout.description_adjudication import ReplayDescriptionAdjudicationAdapter
from autoscout.description_adjudication import adjudicate_descriptions
from autoscout.description_adjudication import build_adjudication_prompt
from autoscout.description_adjudication import load_grounded_search_hints
from autoscout.description_enrichment import _encode_work_items
from autoscout.grounded_description import BoundedHTTPError
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import StageArtifact
from autoscout.grounded_description import SynthesisResult
from autoscout.grounded_description import committed_source_classifier
from autoscout.grounded_description import validate_stage_artifact
from autoscout.json_io import write_json


def _history_record(identity_key: str = "work one|alex author") -> dict:
    return {
        "candidate_id": "candidate-0001",
        "identity_key": identity_key,
        "title": "Work One",
        "primary_authors": ["Alex Author"],
        "content_description": "",
    }


def test_prepare_backfill_attaches_bounded_search_hints() -> None:
    hints = {
        "work one|alex author": [
            "https://publisher.example/book-one",
            "https://retail.example/book-one",
            "https://review.example/book-one",
            "https://extra.example/book-one",
            "not-a-url",
            "",
            123,
        ]
    }
    items = prepare_backfill_work_items([_history_record()], search_hints=hints)
    assert len(items) == 1
    attached = items[0].get("search_hints", [])
    # Bounded to 3, http(s) only, strings only.
    assert attached == [
        "https://publisher.example/book-one",
        "https://retail.example/book-one",
        "https://review.example/book-one",
    ]


def test_prepare_backfill_omits_search_hints_when_absent() -> None:
    items = prepare_backfill_work_items([_history_record()])
    assert len(items) == 1
    assert "search_hints" not in items[0]


def test_encode_forwards_bounded_search_hints() -> None:
    items = [
        {
            "identity_key": "work one|alex author",
            "title": "Work One",
            "metadata": {},
            "search_hints": [
                "https://a.example/1",
                "https://b.example/2",
                " https://c.example/3 ",
                "https://d.example/4",
                "ftp://files.example/5",
                "",
            ],
        }
    ]
    encoded = _encode_work_items(items)[0]
    assert encoded["search_hints"] == [
        "https://a.example/1",
        "https://b.example/2",
        "https://c.example/3",
    ]


def test_encode_omits_search_hints_when_absent() -> None:
    encoded = _encode_work_items([{"identity_key": "k", "title": "T", "metadata": {}}])[0]
    assert "search_hints" not in encoded


def test_encode_finds_valid_hint_past_invalid_entries() -> None:
    items = [
        {
            "identity_key": "k",
            "title": "T",
            "metadata": {},
            "search_hints": [f"ftp://files.example/{index}" for index in range(12)] + ["https://ok.example/book"],
        }
    ]
    encoded = _encode_work_items(items)[0]
    assert encoded["search_hints"] == ["https://ok.example/book"]


def test_load_grounded_search_hints_uses_verified_urls_only() -> None:
    artifact = {
        "stage": "grounded-description",
        "outcomes": [
            {
                "candidate_id": "candidate-0001",
                "identity_key": "work one|alex author",
                "outcome": "attempted_no_description",
                "reason_code": "fewer_than_two_agreeing_sources",
                "research_sources": ["https://publisher.example/book-one"],
                "research_source_records": [{"url": "https://publisher.example/book-one"}],
                "rejected_source_reasons": [
                    {"url": "https://review.example/book-one", "reason_code": "source_denylisted"},
                    {"url": "https://publisher.example/book-one", "reason_code": "source_denylisted"},
                    {"url": "not-a-url", "reason_code": "invalid_source"},
                ],
            }
        ],
    }
    hints = load_grounded_search_hints(artifact)
    assert hints == {"work one|alex author": ["https://publisher.example/book-one"]}


def test_load_grounded_search_hints_ignores_rejected_urls() -> None:
    artifact = {
        "outcomes": [
            {
                "candidate_id": "candidate-0001",
                "identity_key": "work one|alex author",
                "outcome": "attempted_no_description",
                "reason_code": "source_identity_disagreement",
                "research_sources": ["https://publisher.example/verified-one"],
                "rejected_source_reasons": [
                    {"url": "https://unlisted.example/book-one", "reason_code": "source_denylisted"},
                    {"url": "https://wrong.example/other-book", "reason_code": "title_mismatch"},
                    {"url": "https://wrong.example/other-author", "reason_code": "author_mismatch"},
                    {"url": "https://thin.example/empty", "reason_code": "insufficient_subject_text"},
                ],
            }
        ],
    }
    hints = load_grounded_search_hints(artifact)
    assert hints == {"work one|alex author": ["https://publisher.example/verified-one"]}


def test_load_grounded_search_hints_reads_artifact_file(tmp_path: Path) -> None:
    artifact = {
        "outcomes": [
            {
                "candidate_id": "candidate-0001",
                "identity_key": "work one|alex author",
                "outcome": "attempted_no_description",
                "reason_code": "synthesis_failure",
                "research_sources": ["https://review.example/book-one"],
            }
        ]
    }
    stage_file = tmp_path / "grounded-description-stage.json"
    stage_file.write_text(json.dumps(artifact), encoding="utf-8")
    hints = load_grounded_search_hints(tmp_path / "missing.json")
    assert hints == {}
    hints = load_grounded_search_hints(stage_file)
    assert hints == {"work one|alex author": ["https://review.example/book-one"]}


def test_adjudication_prompt_states_hints_are_non_binding() -> None:
    lowered = build_adjudication_prompt().casefold()
    assert "search_hints" in lowered
    assert "starting point" in lowered
    assert "not evidence" in lowered or "not count as" in lowered
    assert "two-owner" in lowered


class _UnreachableTransport:
    def request(self, url: str, **_kwargs) -> object:
        raise BoundedHTTPError("offline hint test never fetches")


class _RecordingAdapter(ReplayDescriptionAdjudicationAdapter):
    def __init__(self) -> None:
        super().__init__({})
        self.seen: list[dict] = []

    def adjudicate(self, work_item) -> dict:
        self.seen.append(dict(work_item))
        return super().adjudicate(work_item)


def _hint_workspace(tmp_path: Path) -> tuple[Path, Path]:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir()
    output_dir.mkdir()
    seed_state(
        state_dir,
        candidate_history=[
            {
                "candidate_id": "candidate-0001",
                "identity_key": "work one|alex author",
                "title": "Work One",
                "primary_authors": ["Alex Author"],
                "content_description": "",
            }
        ],
    )
    return state_dir, output_dir


def test_adjudicate_loads_hints_from_grounded_artifact(tmp_path: Path) -> None:
    state_dir, output_dir = _hint_workspace(tmp_path)
    write_json(
        output_dir / "grounded-description-stage.json",
        {
            "stage": "grounded-description",
            "outcomes": [
                {
                    "candidate_id": "candidate-0001",
                    "identity_key": "work one|alex author",
                    "outcome": "attempted_no_description",
                    "reason_code": "synthesis_failure",
                    "research_sources": ["https://publisher.example/book-one"],
                }
            ],
        },
    )
    adapter = _RecordingAdapter()
    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date="2026-09-14",
        adapter=adapter,
        fetcher=BoundedHTTPFetcher(transport=_UnreachableTransport()),  # type: ignore[arg-type]
    )
    assert result["status"] == "ok"
    assert len(adapter.seen) == 1
    assert adapter.seen[0]["search_hints"] == ["https://publisher.example/book-one"]


def test_adjudicate_without_grounded_artifact_attaches_no_hints(tmp_path: Path) -> None:
    state_dir, output_dir = _hint_workspace(tmp_path)
    adapter = _RecordingAdapter()
    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date="2026-09-14",
        adapter=adapter,
        fetcher=BoundedHTTPFetcher(transport=_UnreachableTransport()),  # type: ignore[arg-type]
    )
    assert result["status"] == "ok"
    assert len(adapter.seen) == 1
    assert "search_hints" not in adapter.seen[0]


def test_adjudicate_explicit_empty_hints_disables_auto_load(tmp_path: Path) -> None:
    state_dir, output_dir = _hint_workspace(tmp_path)
    write_json(
        output_dir / "grounded-description-stage.json",
        {
            "outcomes": [
                {
                    "candidate_id": "candidate-0001",
                    "identity_key": "work one|alex author",
                    "outcome": "attempted_no_description",
                    "reason_code": "synthesis_failure",
                    "research_sources": ["https://publisher.example/book-one"],
                }
            ],
        },
    )
    adapter = _RecordingAdapter()
    adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date="2026-09-14",
        adapter=adapter,
        fetcher=BoundedHTTPFetcher(transport=_UnreachableTransport()),  # type: ignore[arg-type]
        search_hints={},
    )
    assert len(adapter.seen) == 1
    assert "search_hints" not in adapter.seen[0]


_GROUNDED_FIXTURES = Path(__file__).parent / "fixtures" / "issue-502-agreeing-subset"
_GROUNDED_URL = (
    "https://bookshop.org/p/books/exhumed-unearthing-the-history-of-the-american-vampire-aaron-mahnke/c7e72bebb737534a"
)
_GROUNDED_CANDIDATE = {
    "identity_key": "exhumed|aaron mahnke",
    "candidate_id": "exhumed|aaron mahnke",
    "title": "Exhumed",
    "primary_authors": ["Aaron Mahnke"],
    "first_seen_date": "2026-08-17",
}


class _GroundedTransport:
    def request(self, url: str, **_kwargs: object) -> HTTPResponse:
        return HTTPResponse(
            url,
            200,
            {"content-type": "text/html; charset=utf-8"},
            (_GROUNDED_FIXTURES / "bookshop-exhumed.html").read_bytes(),
        )


class _GroundedDiscovery:
    def discover(self, _candidate: object, **_kwargs: object) -> list[DiscoveryResult]:
        classifier = committed_source_classifier()
        metadata = classifier.classify(_GROUNDED_URL)
        return [
            DiscoveryResult(
                _GROUNDED_URL, metadata["source_kind"], metadata["source_family"], metadata["owner_id"], "en"
            )
        ]


class _FailingSynthesizer:
    def synthesize(self, *_args: object, **_kwargs: object) -> SynthesisResult:
        raise RuntimeError("synthesis is unavailable in this test")


def test_grounded_post_gate_refusal_carries_verified_sources() -> None:
    artifact = GroundedDescriptionStage(
        _GroundedDiscovery(), BoundedHTTPFetcher(transport=_GroundedTransport()), _FailingSynthesizer()
    ).run([_GROUNDED_CANDIDATE], operation_id="op", operation_date="2026-08-17", force_candidates=True)
    outcome = artifact.outcomes[0]
    assert outcome.outcome == "attempted_no_description"
    assert outcome.reason_code == "synthesis_failure"
    assert [source.canonical_url for source in outcome.research_sources] == [_GROUNDED_URL]
    assert validate_stage_artifact(artifact)
    raw = outcome.to_dict()
    assert raw["research_sources"] == [_GROUNDED_URL]
    assert [record["url"] for record in raw["research_source_records"]] == [_GROUNDED_URL]
    # A provenance record claims a description exists: a refusal carries
    # the verified URLs and records, never a provenance claim.
    assert "description_provenance" not in raw
    assert "source_independence_evidence" not in raw
    restored = StageArtifact.from_mapping(artifact.to_dict())
    assert validate_stage_artifact(restored)
    assert [source.canonical_url for source in restored.outcomes[0].research_sources] == [_GROUNDED_URL]
    assert load_grounded_search_hints(artifact.to_dict()) == {"exhumed|aaron mahnke": [_GROUNDED_URL]}


def test_grounded_pre_gate_refusal_carries_no_sources() -> None:
    class _EmptyDiscovery:
        def discover(self, _candidate: object, **_kwargs: object) -> list[DiscoveryResult]:
            return []

    artifact = GroundedDescriptionStage(
        _EmptyDiscovery(), BoundedHTTPFetcher(transport=_GroundedTransport()), _FailingSynthesizer()
    ).run([_GROUNDED_CANDIDATE], operation_id="op", operation_date="2026-08-17", force_candidates=True)
    outcome = artifact.outcomes[0]
    assert outcome.outcome == "attempted_no_description"
    assert outcome.reason_code == "no_results"
    assert outcome.research_sources == ()
    assert "research_sources" not in outcome.to_dict()
    assert validate_stage_artifact(artifact)
    assert load_grounded_search_hints(artifact.to_dict()) == {}
