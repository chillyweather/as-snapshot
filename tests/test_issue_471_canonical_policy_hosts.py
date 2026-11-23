"""Offline contract tests for issue #471 canonical policy hosts.

Bare and ``www.`` forms reaching one owner, meaningful subdomains, and
deceptive suffixes are covered for owner derivation in
``test_issue_687_source_owner.py`` (issue #691). The policy file loader whose
host validation this file also covered was retired in issue #1123.
"""

from __future__ import annotations

from pathlib import Path

from autoscout.grounded_description import AcceptedSource
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import SourceClassifier
from autoscout.grounded_description import _validate_classification_evidence
from autoscout.source_owner import load_owner_policy

ROOT = Path(__file__).resolve().parents[1]


def _classifier() -> SourceClassifier:
    return SourceClassifier(owner_policy=load_owner_policy(ROOT / "configs" / "source-owner-policy.json"))


def test_www_classification_evidence_survives_discovery_fetch_and_acceptance_validation() -> None:
    url = "https://www.amazon.de/work"
    classification = _classifier().classify(url)
    discovery = DiscoveryResult(
        url=url,
        source_kind=classification["source_kind"],
        source_family=classification["source_family"],
        owner_id=classification["owner_id"],
        classification_evidence=classification,
    )
    fetched = FetchedSource(
        url=discovery.url,
        source_kind=discovery.source_kind,
        source_family=discovery.source_family,
        owner_id=discovery.owner_id,
        source_language="en",
        title="The Work",
        primary_authors=("Alex Author",),
        subject_text="Memory and migration.",
        classification_evidence=discovery.classification_evidence,
    )
    accepted = AcceptedSource(
        canonical_url=fetched.url,
        source_kind=fetched.source_kind,
        source_family=fetched.source_family,
        owner_id=fetched.owner_id,
        source_language=fetched.source_language,
        classification_evidence=fetched.classification_evidence,
    )

    assert accepted.classification_evidence["host"] == "amazon.de"
    assert _validate_classification_evidence(accepted)


def test_hostname_canonicalization_removes_exactly_one_leading_www_label() -> None:
    classifier = _classifier()

    result = classifier.classify("https://www.www.amazon.de/work")

    assert result["classification"] == "classified"
    assert result["host"] == "www.amazon.de"
