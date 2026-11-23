"""Row evidence is bounded, classified evidence, never a signal-own second source."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from state_store_support import seed_state

from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import SourceClassifier
from autoscout.grounded_description import StageArtifact
from autoscout.grounded_description import SynthesisResult
from autoscout.grounded_description import evaluate_grounding_gate
from autoscout.grounded_description import read_stage_artifact
from autoscout.grounded_description import validate_stage_artifact
from autoscout.grounded_description import write_stage_artifact
from autoscout.grounded_description_enrichment import enrich_grounded_descriptions
from autoscout.json_io import read_json
from autoscout.json_io import write_json

SEED = "https://nuevoslibros.com/libro/galaxia-gutenberg-la-tormenta-que-viene/"
PROFILE = "https://profilebooks.com/work/augustine-the-african/"
PEER = "https://review.example/book"
WRONG = [
    "https://www.librouro.com/libro/ver/1614510-guerra-fria-20.html",
    "https://www.librouro.com/libro/ver/324488-atrapados-en-la-discordia.html",
]
DESCRIPTION = (
    "Книга исследует историю международных отношений и политических конфликтов. "
    "Автор рассматривает причины противостояния государств, сопоставляя решения политиков "
    "и их последствия для общества. Исследование показывает, как исторический опыт помогает "
    "понять современную международную политику и изменения мирового порядка."
)


class Classifier(SourceClassifier):
    def __init__(self) -> None:
        pass

    def classify(self, url: str, **_: Any) -> dict[str, Any]:
        host = urlsplit(url).hostname
        if not host or host == "unknown.example":
            return {"classification": "unknown"}
        return {
            "classification": "classified",
            "source_kind": "publisher",
            "source_family": "publisher",
            "owner_id": host,
            "host": host,
        }


class Discovery:
    def __init__(self, urls: list[str], *, error: bool = False) -> None:
        self.classifier = Classifier()
        self.urls = urls
        self.error = error
        self.calls = 0

    def discover(self, _: Any, **__: Any) -> list[DiscoveryResult]:
        self.calls += 1
        if self.error:
            raise RuntimeError("offline discovery failure")
        return [DiscoveryResult.from_mapping({**self.classifier.classify(url), "url": url}) for url in self.urls]


class Transport:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def request(self, url: str, **_: Any) -> HTTPResponse:
        self.urls.append(url)
        title = "Other book" if url in WRONG else "La tormenta que viene"
        author = "Other Writer" if url in WRONG else "Odd Arne Westad"
        isbn = "" if url in WRONG else "9791388019821"
        html = f'''<html><head><title>{title}</title><script type="application/ld+json">
        {{"@type":"Book","name":"{title}","author":{{"name":"{author}"}},"isbn":"{isbn}",
        "description":"Una historia de los conflictos internacionales y la política mundial.
        El libro examina las relaciones entre los estados y sus consecuencias históricas."}}
        </script></head><body><main><h1>{title}</h1><p>{author}</p></main></body></html>'''
        return HTTPResponse(url, 200, {"content-type": "text/html; charset=utf-8"}, html.encode())


class Synthesizer:
    def synthesize(self, *_: Any, **__: Any) -> SynthesisResult:
        return SynthesisResult(DESCRIPTION)


def candidate(entries: list[dict[str, str]] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "candidate_id": "la-tormenta|westad",
        "identity_key": "La tormenta que viene|Odd Arne Westad",
        "title": "La tormenta que viene",
        "primary_authors": ["Odd Arne Westad"],
        "isbn": "9791388019821",
        "update_delta": "new",
    }
    if entries is not None:
        result["source_evidence"] = entries
    return result


def evidence(url: str = SEED, source_url: str = SEED) -> dict[str, str]:
    return {"evidence_url": url, "source_url": source_url}


def run(
    rows: list[dict[str, Any]],
    urls: list[str] | None = None,
    *,
    error: bool = False,
    max_fetches: int = 3,
    history: list[dict[str, Any]] | None = None,
) -> tuple[StageArtifact, Transport, Discovery]:
    discovery = Discovery(urls or [], error=error)
    transport = Transport()
    stage = GroundedDescriptionStage(
        discovery,
        BoundedHTTPFetcher(transport=transport, min_host_interval_seconds=0),
        Synthesizer(),
        max_workers=1,
        max_fetches=max_fetches,
    )
    artifact = stage.run(
        rows, operation_id="issue-760", operation_date="2026-11-09", force_candidates=True, history=history or []
    )
    return artifact, transport, discovery


def test_la_tormenta_replay() -> None:
    artifact, transport, discovery = run([candidate([evidence()])], WRONG)
    outcome = artifact.outcomes[0]
    assert transport.urls == [SEED, *WRONG]
    assert discovery.calls == 1
    assert outcome.outcome == "single_source"
    assert [(s.canonical_url, s.origin) for s in outcome.research_sources] == [(SEED, "row_evidence")]
    assert artifact.report == {"row_evidence_fetched": 1, "row_evidence_accepted": 1}


@pytest.mark.parametrize("signal_url", [SEED, "", "https://unknown.example/list"])
def test_signal_own_seed_is_only_single_source(signal_url: str) -> None:
    artifact, _, _ = run([candidate([evidence(source_url=signal_url)])], [PEER])
    assert artifact.outcomes[0].outcome == "single_source"
    assert artifact.outcomes[0].research_sources[0].canonical_url == SEED


@pytest.mark.parametrize("own_first", [False, True])
@pytest.mark.parametrize("signal_url", [SEED, "", "https://unknown.example/list"])
def test_duplicate_seed_aggregates_signal_ownership(own_first: bool, signal_url: str) -> None:
    cross = evidence(source_url="https://prize.example/list")
    own = evidence(SEED + "#fragment", source_url=signal_url)
    entries = [own, cross] if own_first else [cross, own]
    artifact, transport, _ = run([candidate(entries)], [PEER])
    outcome = artifact.outcomes[0]
    assert transport.urls == [SEED, PEER]
    assert outcome.outcome == "single_source"
    assert outcome.research_sources[0].canonical_url == SEED
    assert outcome.research_sources[0].signal_own is True


def test_cross_owner_seed_can_join_independent_set() -> None:
    artifact, transport, _ = run([candidate([evidence(PROFILE, "https://prize.example/list")])], [PEER])
    outcome = artifact.outcomes[0]
    assert transport.urls == [PROFILE, PEER]
    assert outcome.outcome == "grounded"
    assert [s.canonical_url for s in outcome.research_sources] == [PROFILE, PEER]
    assert not outcome.research_sources[0].signal_own


@pytest.mark.parametrize(
    "url",
    [
        "https://www.indiepubs.org/s/independentpresstop40nonfiction-dhwm.csv",
        "https://bokhandlerforeningen.no/wp-admin/admin-ajax.php",
        "https://example.com/",
        "https://example.com",
        "ftp://example.com/book",
        "https://example.com/book.PDF",
        "https://example.com/wp-json/book",
        "https://example.com/api/book",
        "https://example.com/feed/books",
        "https://example.com/feed",
        "https://example.com/feed/",
        "https://example.com/blog/feed/",
        "https://example.com/api/books/1",
        "https://example.com/wp-json/wp/v2/posts",
        "https://example.com/wp-admin/admin-ajax.php",
        "https://example.com/RSS/",
        "https://example.com/ADMIN-AJAX/",
    ],
)
def test_list_endpoints_never_fetched(url: str) -> None:
    artifact, transport, _ = run([candidate([evidence(url)])])
    assert transport.urls == []
    assert artifact.outcomes[0].reason_code == "no_results"


def test_shared_evidence_never_fetched() -> None:
    shared = "https://best-books.publishersweekly.com/pw/best-books/2026"
    first = candidate([evidence(shared)])
    second = {**first, "candidate_id": "other", "title": "Other"}
    _, transport, _ = run([first, second])
    assert transport.urls == []


@pytest.mark.parametrize("error,reason", [(False, "no_results"), (True, "search_or_fetch_failure")])
def test_discovery_failure_preserves_seed_only_success(error: bool, reason: str) -> None:
    artifact, transport, _ = run([candidate([evidence()])], error=error)
    assert transport.urls == [SEED]
    assert artifact.outcomes[0].outcome == "single_source"
    artifact, transport, _ = run([candidate()], error=error)
    assert transport.urls == []
    assert artifact.outcomes[0].reason_code == reason


def test_seed_deduplicates_discovery_and_record_urls() -> None:
    artifact, transport, _ = run([candidate([evidence(), evidence(SEED + "#fragment")])], [SEED])
    assert transport.urls == [SEED]
    assert artifact.outcomes[0].outcome == "single_source"


@pytest.mark.parametrize("max_fetches,expected", [(1, [SEED]), (2, [SEED, PROFILE]), (3, [SEED, PROFILE, PEER])])
def test_two_seeds_occupy_existing_fetch_slots(max_fetches: int, expected: list[str]) -> None:
    _, transport, _ = run(
        [candidate([evidence(), evidence(PROFILE), evidence("https://third.example/book")])],
        [PEER],
        max_fetches=max_fetches,
    )
    assert transport.urls == expected


def test_primary_signal_fallback_and_absent_classifier() -> None:
    row = candidate()
    row["primary_signal"] = evidence()
    artifact, transport, _ = run([row])
    assert transport.urls == [SEED]
    assert artifact.outcomes[0].outcome == "single_source"
    row["source_evidence"] = []
    assert run([row])[1].urls == []
    stage = GroundedDescriptionStage()
    assert stage._evidence_seeds(candidate([evidence()])) == []


@pytest.mark.parametrize(
    "url",
    [
        PROFILE,
        SEED,
        "https://example.com/work/feedback/",
        "https://example.com/libro/feeding-the-world/",
        "https://www.nonfiction.fr/article-12907-comprendre-ivan-illich-a-travers-ses-amities.htm",
        "https://people.com/imomsohard-the-middle-school-moms-survival-guide-excerpt-exclusive-12202628",
    ],
)
def test_book_level_paths_are_kept(url: str) -> None:
    assert run([candidate([evidence(url)])])[1].urls == [url]


def test_artifact_roundtrip_and_legacy_origin(tmp_path: Path) -> None:
    artifact, _, _ = run([candidate([evidence(PROFILE, "https://prize.example/list")])], [PEER])
    assert validate_stage_artifact(artifact)
    path = tmp_path / "stage.json"
    write_stage_artifact(path, artifact)
    restored = read_stage_artifact(path)
    assert restored is not None and validate_stage_artifact(restored)
    assert restored.to_dict() == artifact.to_dict()
    legacy, _, _ = run([candidate()], [PEER])
    payload = legacy.to_dict()
    for source in payload["outcomes"][0]["research_source_records"]:
        source.pop("origin")
        source.pop("signal_own")
    restored = StageArtifact.from_mapping(payload)
    assert validate_stage_artifact(restored)
    assert restored.outcomes[0].research_sources[0].origin == "discovery"


def test_gate_never_accepts_signal_own_in_multi_source_set() -> None:
    def source(url: str, own: bool = False) -> FetchedSource:
        return FetchedSource(
            url,
            "publisher",
            "publisher",
            str(urlsplit(url).hostname),
            "es",
            "La tormenta que viene",
            ("Odd Arne Westad",),
            "International politics and history",
            checked_date="2026-11-09",
            origin="row_evidence" if own else "discovery",
            signal_own=own,
        )

    seed = source(SEED, True)
    peer = source(PEER)
    gate = evaluate_grounding_gate(candidate(), [seed, peer])
    assert gate.accepted and len(gate.accepted_sources) == 1
    assert gate.accepted_sources[0].canonical_url == SEED
    gate = evaluate_grounding_gate(candidate(), [seed, peer, source(PROFILE)])
    assert len(gate.accepted_sources) == 2
    assert all(not s.signal_own for s in gate.accepted_sources)
    assert any(
        s.canonical_url == SEED and s.reason_code == "fewer_than_two_agreeing_sources" for s in gate.rejected_sources
    )
    conflict = replace(seed, primary_authors=("Conflicting Author",))
    assert not evaluate_grounding_gate(candidate(), [conflict, peer]).accepted


def test_refused_discovery_result_does_not_discard_seed() -> None:
    artifact, transport, _ = run([candidate([evidence()])], ["https://unknown.example/book"])
    assert transport.urls == [SEED]
    assert artifact.outcomes[0].outcome == "single_source"
    assert artifact.outcomes[0].rejected_source_reasons[0].reason_code == "source_denylisted"


def test_unclassified_seed_is_not_fetched() -> None:
    _, transport, _ = run([candidate([evidence("https://unknown.example/book")])])
    assert transport.urls == []


def test_signal_own_cannot_enter_legacy_subject_corroboration() -> None:
    seed = FetchedSource(
        SEED,
        "publisher",
        "publisher",
        "nuevoslibros.com",
        "es",
        "La tormenta que viene",
        ("Odd Arne Westad",),
        "International politics and history",
        checked_date="2026-11-09",
        origin="row_evidence",
        signal_own=True,
    )
    authorless = replace(
        seed, url=PEER, owner_id="review.example", primary_authors=(), origin="discovery", signal_own=False
    )
    gate = evaluate_grounding_gate(candidate(), [seed, authorless])
    assert gate.accepted
    assert len(gate.accepted_sources) == 1
    assert gate.accepted_sources[0].canonical_url == SEED


def test_named_singleton_preference_is_preserved() -> None:
    seed = FetchedSource(
        SEED,
        "publisher",
        "publisher",
        "nuevoslibros.com",
        "es",
        "La tormenta que viene",
        (),
        "Odd Arne Westad examines international politics and history",
        checked_date="2026-11-09",
        origin="row_evidence",
        signal_own=True,
    )
    named = replace(
        seed,
        url=PEER,
        owner_id="review.example",
        primary_authors=("Odd Arne Westad",),
        origin="discovery",
        signal_own=False,
    )
    gate = evaluate_grounding_gate(candidate(), [seed, named])
    assert gate.accepted and len(gate.accepted_sources) == 1
    assert gate.accepted_sources[0].canonical_url == PEER


@pytest.mark.parametrize("history_has_current", [False, True])
@pytest.mark.parametrize("other_has_id", [False, True])
def test_shared_list_in_full_history_is_not_fetched(history_has_current: bool, other_has_id: bool) -> None:
    shared = "https://best-books.publishersweekly.com/pw/best-books/2026"
    row = candidate([evidence(shared)])
    other = {**row, "candidate_id": "other", "identity_key": "Other|Writer"}
    if not other_has_id:
        other.pop("candidate_id")
    # Weekly subsets can omit the other row; standalone calls include the
    # current row again in durable history.
    history = [row, other] if history_has_current else [other]
    assert run([row], history=history)[1].urls == []


def test_same_candidate_history_does_not_make_book_page_shared() -> None:
    row = candidate([evidence()])
    prior = {**row, "identity_key": "Earlier display|Same author"}
    assert run([row], history=[prior])[1].urls == [SEED]


def test_enrichment_publishes_row_evidence_counters(tmp_path: Path) -> None:
    row = candidate([evidence(PROFILE, "https://prize.example/list")])
    write_json(tmp_path / "scouting-candidates.json", [row])
    seed_state(tmp_path, candidate_history=[row])
    transport = Transport()
    stage = GroundedDescriptionStage(
        Discovery([PEER]),
        BoundedHTTPFetcher(transport=transport, min_host_interval_seconds=0),
        Synthesizer(),
        max_workers=1,
    )
    result = enrich_grounded_descriptions(stage, tmp_path, effective_date="2026-11-09", operation_id="issue-760")
    published = read_json(tmp_path / "grounded-description-stage.json")
    assert result["status"] == "ok"
    assert published["outcomes"][0]["outcome"] == "grounded"
    for report in (published["report"], result["description_stage_report"]):
        assert report["row_evidence_fetched"] == 1
        assert report["row_evidence_accepted"] == 1


def test_discovery_alias_of_signal_own_seed_cannot_form_pair() -> None:
    artifact, transport, _ = run([candidate([evidence()])], [SEED.rstrip("/"), PEER])
    outcome = artifact.outcomes[0]
    assert transport.urls == [SEED, SEED.rstrip("/"), PEER]
    assert outcome.outcome == "single_source"
    assert all(source.signal_own for source in outcome.research_sources)


@pytest.mark.parametrize(
    "signal_url,discovered_url,expected",
    [
        (SEED, SEED, "single_source"),
        (SEED, PROFILE, "grounded"),
        ("https://unknown.example/list", SEED, "grounded"),
    ],
)
def test_discovery_ownership_without_seed(signal_url: str, discovered_url: str, expected: str) -> None:
    row = candidate([{"source_url": signal_url}])
    artifact, _, _ = run([row], [discovered_url, PEER])
    assert artifact.outcomes[0].outcome == expected


@pytest.mark.parametrize("with_seed", [False, True])
def test_duplicate_seed_retains_discovered_translated_title_evidence(with_seed: bool) -> None:
    row = candidate([evidence()] if with_seed else None)
    row["title"] = "The Coming Storm"
    row.pop("isbn")
    mapping = {
        "title": row["title"],
        "primary_authors": row["primary_authors"],
        "english_title": "La tormenta que viene",
        "english_title_type": "sourced_published",
        "english_title_provenance": {
            "kind": "sourced_published",
            "research_sources": ["https://mapping.example/book"],
            "mapping": {
                "original_title": row["title"],
                "original_authors": row["primary_authors"],
                "published_title": "La tormenta que viene",
                "published_authors": row["primary_authors"],
            },
        },
    }

    class TitleDiscovery(Discovery):
        def discover(self, _: Any, **__: Any) -> list[DiscoveryResult]:
            return [
                DiscoveryResult.from_mapping(
                    {
                        **self.classifier.classify(SEED),
                        "url": SEED,
                        "title": "La tormenta que viene",
                        "title_evidence_route": "verified_published_title",
                        "title_evidence": mapping,
                        "route_kind": "translated_title",
                    }
                )
            ]

    class NoISBNTransport(Transport):
        def request(self, url: str, **kwargs: Any) -> HTTPResponse:
            response = super().request(url, **kwargs)
            return replace(response, body=response.body.replace(b"9791388019821", b""))

    transport = NoISBNTransport()
    stage = GroundedDescriptionStage(
        TitleDiscovery([]),
        BoundedHTTPFetcher(transport=transport, min_host_interval_seconds=0),
        Synthesizer(),
        max_workers=1,
    )
    artifact = stage.run([row], operation_id="translated", operation_date="2026-11-09", force_candidates=True)
    outcome = artifact.outcomes[0]
    assert transport.urls == [SEED]
    assert outcome.outcome == "single_source"
    assert outcome.research_sources[0].origin == ("row_evidence" if with_seed else "discovery")
    assert outcome.research_sources[0].title_evidence_route == "verified_published_title"
