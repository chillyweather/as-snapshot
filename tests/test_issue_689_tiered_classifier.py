"""Offline contract tests for issue #689: the tiered classifier becomes authoritative.

The classifier is the single choke point. Three admission gates read its
``classification`` verdict, so making it tiered is what lets enrichment read
the pages discovery already finds.

Issue #691 retired the off-policy and language-admission reason codes: a
denylist refusal carries its own reason code.
"""

from __future__ import annotations

from pathlib import Path

from state_store_support import read_state
from state_store_support import seed_state

from autoscout.grounded_description import SourceClassifier
from autoscout.source_owner import load_owner_policy

REPO_ROOT = Path(__file__).resolve().parents[1]


def _classifier() -> SourceClassifier:
    return SourceClassifier(owner_policy=load_owner_policy(REPO_ROOT / "configs" / "source-owner-policy.json"))


def test_a_publisher_the_allowlist_refused_is_now_classified() -> None:
    """Verso was found by discovery in run 4090c59a and discarded unread."""
    result = _classifier().classify("https://www.versobooks.com/products/3329-medium-hot")

    assert result["classification"] == "classified"
    assert result["owner_id"] == "versobooks.com"
    assert result["tier"] == "derived"


def test_denylisted_hosts_are_the_only_refusals_left() -> None:
    classifier = _classifier()

    assert classifier.classify("https://www.reddit.com/r/books/x")["classification"] == "unknown"
    assert classifier.classify("https://www.ebay.com/itm/1")["classification"] == "unknown"
    assert classifier.classify("https://clio-cr.clionautes.org/x.html")["classification"] == "classified"


def test_storefronts_share_one_owner_so_they_cannot_agree() -> None:
    classifier = _classifier()
    owners = {
        classifier.classify(url)["owner_id"]
        for url in (
            "https://www.amazon.fr/dp/2130879861",
            "https://www.fnac.com/a22191038/x",
            "https://www.decitre.fr/livres/y.html",
        )
    }

    assert owners == {"publisher-supplied-copy"}


def test_no_candidate_is_refused_before_discovery_for_its_language() -> None:
    """Italian and Czech had one admissible owner each, so the two-owner
    rule was unsatisfiable by construction. Nothing short-circuits now."""
    from autoscout.grounded_description import GroundedDescriptionStage

    class RecordingDiscovery:
        classifier = _classifier()

        def __init__(self) -> None:
            self.languages: list[str] = []

        def discover(self, candidate):
            self.languages.append(str(candidate.get("source_language", "")))
            return []

    discovery = RecordingDiscovery()
    languages = ("it", "cs", "fr", "es", "pt", "ko", "ja", "")
    candidates = [
        {
            "candidate_id": f"book-{index}|author",
            "identity_key": f"book-{index}|author",
            "title": f"Book {index}",
            "primary_authors": ["An Author"],
            "source_language": language,
        }
        for index, language in enumerate(languages)
    ]
    artifact = GroundedDescriptionStage(discovery=discovery, max_workers=1).run(
        candidates, operation_id="issue-691", operation_date="2026-09-13", force_candidates=True
    )

    assert sorted(discovery.languages) == sorted(languages)
    assert {outcome.reason_code for outcome in artifact.outcomes} == {"no_results"}


def test_previously_unreachable_languages_can_now_reach_two_owners() -> None:
    """Two owner-distinct Italian sources, neither of which the allowlist knew."""
    classifier = _classifier()

    publisher = classifier.classify("https://www.einaudi.it/catalogo-libri/x/")
    review = classifier.classify("https://www.ilpost.it/2026/01/01/recensione-y/")

    assert publisher["classification"] == "classified"
    assert review["classification"] == "classified"
    assert publisher["owner_id"] != review["owner_id"]


def test_independent_hosts_are_fetched_before_storefronts() -> None:
    """The per-candidate fetch budget is small, so ordering decides whether
    a second distinct owner is ever reached."""
    from autoscout.grounded_description import _SOURCE_PREFERENCE

    classifier = _classifier()
    independent = classifier.classify("https://www.versobooks.com/products/1")["source_kind"]
    storefront = classifier.classify("https://www.amazon.fr/dp/2")["source_kind"]

    assert _SOURCE_PREFERENCE[independent] < _SOURCE_PREFERENCE[storefront]


def test_the_configured_production_stage_classifies_by_derived_owner() -> None:
    """The flip: the allowlist no longer gates a live run."""
    from autoscout.weekly_workflow import _make_production_enrichment

    enrichment = _make_production_enrichment(repo_path=REPO_ROOT, discovery_endpoint="https://search.example.test")
    classifier = enrichment.stage.discovery.classifier

    assert classifier.owner_policy is not None
    assert classifier.classify("https://www.versobooks.com/products/1")["classification"] == "classified"
    assert classifier.classify("https://www.telerama.fr/livre/x.php")["classification"] == "classified"
    assert classifier.classify("https://www.reddit.com/r/books/y")["classification"] == "unknown"


def _discovery_result_for(url: str):
    """Build a discovery result the way HTTPDiscoveryProvider does."""
    from autoscout.grounded_description import DiscoveryResult

    classification = _classifier().classify(url)
    return DiscoveryResult(
        url=url,
        source_kind=classification.get("source_kind", ""),
        source_family=classification.get("source_family", ""),
        owner_id=classification.get("owner_id", ""),
        classification=classification.get("classification", "unknown"),
        classification_evidence=classification,
    )


def test_a_discovered_source_carries_its_derived_owner_and_tier() -> None:
    """#689 requires the tier be recoverable from the run artifact alone, so
    a wrong derivation is diagnosable without re-running anything."""
    result = _discovery_result_for("https://www.versobooks.com/products/1")
    metadata = result.classification_evidence["source_metadata"]

    assert result.owner_id == "versobooks.com"
    assert metadata["owner_id"] == "versobooks.com"
    assert metadata["tier"] == "derived"

    storefront = _discovery_result_for("https://www.amazon.fr/dp/2")
    assert storefront.classification_evidence["source_metadata"]["tier"] == "shared_owner"


def test_regression_the_baseline_run_failures_are_now_admitted() -> None:
    """Every URL below was found by discovery in run 4090c59a and discarded
    unread as off-policy. Two of them were later used by hand to write the
    Content Description the run failed to produce."""
    classifier = _classifier()
    cases = {
        "medium hot|hito steyerl": (
            "https://www.versobooks.com/products/3329-medium-hot",
            "https://www.artnews.com/art-in-america/aia-reviews/hito-steyerl-medium-hot-1234744204/",
        ),
        "une prehistoire des femmes|anne augereau": (
            "https://www.editionsladecouverte.fr/une_prehistoire_des_femmes-9782348080654",
            "https://www.telerama.fr/livre/une-prehistoire-des-femmes.php",
        ),
        "erik satie three piece suite|ian penman": (
            "https://mitpress.mit.edu/9781635902532/erik-satie-three-piece-suite/",
            "https://www.nybooks.com/articles/2026/01/15/erik-satie-three-piece-suite-penman/",
        ),
    }

    for identity_key, (first, second) in cases.items():
        one = classifier.classify(first)
        two = classifier.classify(second)
        assert one["classification"] == "classified", identity_key
        assert two["classification"] == "classified", identity_key
        assert one["owner_id"] != two["owner_id"], identity_key


def test_identity_verification_is_untouched_by_admission() -> None:
    """Opening the source policy must not trade thin descriptions for wrong
    ones. The grounding gate still refuses a mismatched author even when
    both sources are admitted and owner-distinct."""
    from autoscout.grounded_description import REASON_AUTHOR_MISMATCH
    from autoscout.grounded_description import DurableTarget
    from autoscout.grounded_description import evaluate_grounding_gate

    target = DurableTarget("candidate-1", "Medium Hot", ("Hito Steyerl",), {})

    def source(url: str, author: str) -> dict[str, object]:
        classification = _classifier().classify(url)
        return {
            "url": url,
            "source_kind": classification["source_kind"],
            "source_family": classification["source_family"],
            "owner_id": classification["owner_id"],
            "title": "Medium Hot",
            "primary_authors": [author],
            "subject_text": "images in the age of heat",
            "subject_key": "images in the age of heat",
            "checked_date": "2026-09-13",
        }

    agreeing = evaluate_grounding_gate(
        target,
        [
            source("https://www.versobooks.com/products/1", "Hito Steyerl"),
            source("https://www.artnews.com/x/", "Hito Steyerl"),
        ],
    )
    assert agreeing.accepted

    mismatched = evaluate_grounding_gate(
        target,
        [
            source("https://www.versobooks.com/products/1", "Someone Else"),
            source("https://www.artnews.com/x/", "Another Person"),
        ],
    )
    assert not mismatched.accepted
    assert mismatched.reason_code == REASON_AUTHOR_MISMATCH


def test_storefronts_in_unlisted_markets_still_share_one_owner() -> None:
    """Review finding 1.

    Enumerating storefront hostnames is whack-a-mole: every market Amazon,
    Audible or Fnac operates in is another entry someone has to remember.
    The ones nobody listed became independent owners, so two storefront
    pages for the same book could satisfy the two-owner rule -- the exact
    fake agreement that rule exists to prevent, now live rather than
    shadow-only.
    """
    classifier = _classifier()

    for url in (
        "https://www.amazon.it/dp/1",
        "https://www.amazon.com.br/dp/2",
        "https://www.amazon.nl/dp/3",
        "https://www.amazon.co.jp/dp/4",
        "https://www.audible.fr/pd/5",
        "https://www.fnac.pt/x",
        "https://www.thalia.at/shop/y",
    ):
        result = classifier.classify(url)
        assert result["tier"] == "shared_owner", url
        assert result["owner_id"] == "publisher-supplied-copy", url


def test_two_unlisted_storefronts_cannot_ground_a_description() -> None:
    """The consequence the owner rule exists to prevent, asserted end to end
    at the grounding gate rather than only at classification."""
    from autoscout.grounded_description import DurableTarget
    from autoscout.grounded_description import evaluate_grounding_gate

    classifier = _classifier()
    target = DurableTarget("candidate-1", "Medium Hot", ("Hito Steyerl",), {})

    def source(url: str) -> dict[str, object]:
        c = classifier.classify(url)
        return {
            "url": url,
            "source_kind": c["source_kind"],
            "source_family": c["source_family"],
            "owner_id": c["owner_id"],
            "title": "Medium Hot",
            "primary_authors": ["Hito Steyerl"],
            "subject_text": "images in the age of heat",
            "subject_key": "images in the age of heat",
            "checked_date": "2026-09-13",
        }

    result = evaluate_grounding_gate(
        target, [source("https://www.amazon.it/dp/1"), source("https://www.amazon.com.br/dp/2")]
    )

    assert not result.accepted or len(result.accepted_sources) < 2


def test_a_rejected_source_records_the_tier_that_refused_it() -> None:
    """Review finding 2.

    #689 requires the tier be recorded so a wrong derivation is diagnosable
    from the run artifact alone. Admitted sources carried it; refusals
    serialized only a URL and a reason code, so a denylist hit was
    indistinguishable from a URL that failed to parse.
    """
    from autoscout.grounded_description import RejectedSource

    classification = _classifier().classify("https://www.reddit.com/r/books/x")
    rejected = RejectedSource(
        "https://www.reddit.com/r/books/x",
        "source_denylisted",
        tier=classification.get("tier", ""),
    )

    assert rejected.to_dict() == {
        "url": "https://www.reddit.com/r/books/x",
        "reason_code": "source_denylisted",
        "tier": "denylist",
    }
    assert RejectedSource("https://x.test/y", "no_results").to_dict() == {
        "url": "https://x.test/y",
        "reason_code": "no_results",
    }


class _BaselineDiscovery:
    """Discovery replaying the URLs run 4090c59a found and discarded unread.

    Results are classified by the real tiered classifier rather than
    hand-labelled, so the test exercises admission rather than assuming it.
    """

    def __init__(self, urls: tuple[str, ...]) -> None:
        self.urls = urls

    def discover(self, candidate, **_kwargs):
        classifier = _classifier()
        out = []
        for url in self.urls:
            c = classifier.classify(url)
            out.append(
                {
                    "url": url,
                    "source_kind": c.get("source_kind", ""),
                    "source_family": c.get("source_family", ""),
                    "owner_id": c.get("owner_id", ""),
                    "classification": c.get("classification", "unknown"),
                    "classification_evidence": c,
                    "title": "Medium Hot",
                }
            )
        return out


class _MatchingFetch:
    def fetch(self, result, **_kwargs):
        return {
            "url": result.url,
            "source_kind": result.source_kind,
            "source_family": result.source_family,
            "owner_id": result.owner_id,
            "classification": result.classification,
            "classification_evidence": result.classification_evidence,
            "title": "Medium Hot",
            "primary_authors": ["Hito Steyerl"],
            "subject_text": "images in the age of heat and artificial intelligence",
            "subject_key": "images in the age of heat",
            "checked_date": "2026-09-13",
        }


class _RussianSynthesizer:
    def synthesize(self, candidate, sources, **_kwargs):
        from autoscout.grounded_description import SynthesisResult

        return SynthesisResult(
            "Одиннадцать эссе художницы о том, что происходит с изображением в эпоху "
            "искусственного интеллекта и климатического перегрева. Штайерль разбирает, "
            "как алгоритмическая генерация картинок меняет представление о видимом. "
            "Редакционный крючок — вопрос, может ли искусство создаваться для машин.",
            "",
        )


def test_baseline_failures_now_ground_end_to_end(tmp_path: Path) -> None:
    """Review finding 3.

    The earlier regression stopped at admission. #689 requires the fixture
    assert these candidates *ground*, which means fetching, synthesis,
    serialized evidence, and the search-hint channel that #684 feeds from.
    """
    import json

    from autoscout.grounded_description import GroundedDescriptionStage
    from autoscout.grounded_description import GroundedDescriptionStageService

    candidate = {
        "candidate_id": "candidate-001",
        "identity_key": "medium hot|hito steyerl",
        "title": "Medium Hot",
        "primary_authors": ["Hito Steyerl"],
        "update_delta": "new",
        "priority_label": "D",
        "first_seen_date": "2026-09-13",
    }
    output_dir = tmp_path / "output"
    state_dir = tmp_path / "state"
    output_dir.mkdir()
    state_dir.mkdir()
    (output_dir / "scouting-candidates.json").write_text(json.dumps([candidate]), encoding="utf-8")
    seed_state(state_dir, candidate_history=[candidate])
    seed_state(state_dir, candidate_identity_aliases={"version": 1, "aliases": {}, "candidates": {}})

    service = GroundedDescriptionStageService(
        stage=GroundedDescriptionStage(
            discovery=_BaselineDiscovery(
                (
                    "https://www.versobooks.com/products/3329-medium-hot",
                    "https://www.artnews.com/art-in-america/aia-reviews/hito-steyerl-medium-hot-1234744204/",
                )
            ),
            fetch=_MatchingFetch(),
            synthesizer=_RussianSynthesizer(),
        )
    )
    result = service.enrich(output_dir, state_dir, effective_date="2026-09-13")

    assert result["status"] == "ok"
    assert result["grounded"] == 1, result

    history = read_state(state_dir, "candidate_history")
    assert history[0]["content_description"].startswith("Одиннадцать эссе")

    artifact = json.loads((output_dir / "grounded-description-stage.json").read_text(encoding="utf-8"))
    (outcome,) = artifact["outcomes"]
    assert outcome["outcome"] == "grounded"

    owners = {record["owner_id"] for record in outcome["research_source_records"]}
    assert owners == {"versobooks.com", "artnews.com"}
    tiers = {
        record["classification_evidence"]["source_metadata"]["tier"] for record in outcome["research_source_records"]
    }
    assert tiers == {"derived"}

    from autoscout.description_adjudication import load_grounded_search_hints

    hints = load_grounded_search_hints(artifact)
    assert hints["medium hot|hito steyerl"]


def test_storefront_brands_do_not_depend_on_knowing_the_suffix() -> None:
    """Review finding 1, second pass.

    Brand matching was gated on registrable-domain extraction, which is
    gated on a public-suffix list that will always be incomplete. Any market
    whose suffix nobody listed -- amazon.com.be, amazon.com.sg -- fell back
    to a two-label owner and became independent again. Matching the brand
    against any label of the host removes that dependency entirely.
    """
    classifier = _classifier()

    for url in (
        "https://www.amazon.com.be/dp/1",
        "https://www.amazon.com.sg/dp/2",
        "https://www.amazon.com.tr/dp/3",
        "https://www.amazon.se/dp/4",
        "https://www.audible.co.uk/pd/5",
    ):
        result = classifier.classify(url)
        assert result["tier"] == "shared_owner", url
        assert result["owner_id"] == "publisher-supplied-copy", url

    # A deceptive label must not inherit the brand.
    assert classifier.classify("https://notamazon.com/dp/9")["tier"] == "derived"


def test_a_denylist_refusal_records_its_tier_through_the_real_stage(tmp_path: Path) -> None:
    """Review finding 2, second pass.

    The previous test handed RejectedSource a tier directly, which bypassed
    the path that actually drops it: the denylist classification carried the
    tier only at the top level, while the stage reads it from source
    metadata. This drives the real stage and asserts the serialized artifact.
    """
    import json

    from autoscout.grounded_description import GroundedDescriptionStage
    from autoscout.grounded_description import GroundedDescriptionStageService

    candidate = {
        "candidate_id": "candidate-001",
        "identity_key": "medium hot|hito steyerl",
        "title": "Medium Hot",
        "primary_authors": ["Hito Steyerl"],
        "update_delta": "new",
        "priority_label": "D",
        "first_seen_date": "2026-09-13",
    }
    output_dir = tmp_path / "output"
    state_dir = tmp_path / "state"
    output_dir.mkdir()
    state_dir.mkdir()
    (output_dir / "scouting-candidates.json").write_text(json.dumps([candidate]), encoding="utf-8")
    seed_state(state_dir, candidate_history=[candidate])
    seed_state(state_dir, candidate_identity_aliases={"version": 1, "aliases": {}, "candidates": {}})

    service = GroundedDescriptionStageService(
        stage=GroundedDescriptionStage(
            discovery=_BaselineDiscovery(("https://www.reddit.com/r/books/x",)),
            fetch=_MatchingFetch(),
            synthesizer=_RussianSynthesizer(),
        )
    )
    service.enrich(output_dir, state_dir, effective_date="2026-09-13")

    artifact = json.loads((output_dir / "grounded-description-stage.json").read_text(encoding="utf-8"))
    (outcome,) = artifact["outcomes"]
    (rejected,) = outcome["rejected_source_reasons"]

    assert rejected["reason_code"] == "source_denylisted"
    assert rejected["tier"] == "denylist"


def test_a_serialized_rejection_round_trips_its_tier() -> None:
    """Deserialization rebuilt RejectedSource without the tier, so a
    reloaded artifact silently lost what the run recorded."""
    from autoscout.grounded_description import RejectedSource

    original = RejectedSource("https://www.reddit.com/r/books/x", "source_denylisted", tier="denylist")
    restored = RejectedSource.from_mapping(original.to_dict())

    assert restored.tier == "denylist"
    assert restored.to_dict() == original.to_dict()
