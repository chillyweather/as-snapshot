"""Offline contract tests for issue #687 derived source owner identity.

These test the tiered owner derivation that replaces the grounded source
allowlist. Derivation is a pure function of a URL plus a policy, so every
test here runs without network access or fixture files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autoscout.source_owner import OwnerPolicy
from autoscout.source_owner import derive_source_owner
from autoscout.source_owner import load_owner_policy

REPO_ROOT = Path(__file__).resolve().parents[1]


def _policy(**overrides: object) -> OwnerPolicy:
    """Build a small explicit policy so each test states what it relies on."""
    fields: dict[str, object] = {
        "denylist": (),
        "shared_owner_hosts": (),
        "shared_owner_id": "publisher-supplied-copy",
        "equivalence": {},
        "multi_label_suffixes": ("co.uk",),
    }
    fields.update(overrides)
    return OwnerPolicy(**fields)  # type: ignore[arg-type]


def test_publisher_url_derives_its_own_registrable_domain_as_owner() -> None:
    result = derive_source_owner("https://www.versobooks.com/products/3329-medium-hot", _policy())

    assert result["admitted"] is True
    assert result["tier"] == "derived"
    assert result["owner_id"] == "versobooks.com"


def test_denylisted_host_is_refused_and_names_the_denylist_tier() -> None:
    policy = _policy(denylist=("reddit.com",))

    result = derive_source_owner("https://www.reddit.com/r/books/comments/abc/", policy)

    assert result["admitted"] is False
    assert result["tier"] == "denylist"
    assert result["owner_id"] == ""


def test_storefronts_share_one_owner_so_they_cannot_agree_with_each_other() -> None:
    """Retail surfaces repeat publisher-supplied copy.

    They stay readable, because a storefront is often the only page
    carrying a blurb, but several of them must never look like several
    independent confirmations. This is the guarantee the old allowlist
    hand-coded for 17 retail hosts and lost entirely for every other
    market's storefront.
    """
    policy = _policy(shared_owner_hosts=("amazon.com", "amazon.fr", "waterstones.com", "fnac.com"))

    owners = {
        derive_source_owner(url, policy)["owner_id"]
        for url in (
            "https://www.amazon.com/dp/1804298026",
            "https://www.amazon.fr/dp/2130879861",
            "https://www.waterstones.com/book/erik-satie-three-piece-suite/ian-penman/9781804271537",
            "https://www.fnac.com/a22191038/Anne-Augereau-Une-prehistoire-des-femmes",
        )
    }

    assert owners == {"publisher-supplied-copy"}


def test_shared_owner_pages_are_still_admitted() -> None:
    policy = _policy(shared_owner_hosts=("amazon.com",))

    result = derive_source_owner("https://www.amazon.com/dp/1804298026", policy)

    assert result["admitted"] is True
    assert result["tier"] == "shared_owner"


def test_equivalence_table_collapses_genuinely_shared_ownership() -> None:
    """The two pairs the retired policy file recorded as one owner each."""
    policy = _policy(
        equivalence={
            "bookmarks.reviews": "literary-hub",
            "lithub.com": "literary-hub",
            "slj.com": "media-source-inc",
            "libraryjournal.com": "media-source-inc",
        }
    )

    def owner(url: str) -> str:
        return derive_source_owner(url, policy)["owner_id"]

    assert owner("https://bookmarks.reviews/reviews/work") == "literary-hub"
    assert owner("https://lithub.com/category/non-fiction/") == "literary-hub"
    assert owner("https://www.slj.com/story/review") == "media-source-inc"
    assert owner("https://www.libraryjournal.com/story/review") == "media-source-inc"


def test_bare_and_www_forms_reach_the_same_owner() -> None:
    policy = _policy()

    assert (
        derive_source_owner("https://versobooks.com/products/3329", policy)["owner_id"]
        == derive_source_owner("https://www.versobooks.com/products/3329", policy)["owner_id"]
    )


def test_multi_label_suffix_hosts_do_not_collapse_onto_the_suffix() -> None:
    """Without the reviewed suffix list every .co.uk host shares one owner.

    That would silently block two genuinely independent British sources
    from ever grounding a Content Description together.
    """
    policy = _policy(multi_label_suffixes=("co.uk", "com.br"))

    def owner(url: str) -> str:
        return derive_source_owner(url, policy)["owner_id"]

    assert owner("https://www.penguin.co.uk/books/12345") == "penguin.co.uk"
    assert owner("https://www.harpercollins.co.uk/products/x") == "harpercollins.co.uk"
    assert owner("https://www.penguin.co.uk/a") != owner("https://www.harpercollins.co.uk/b")
    assert owner("https://www.sextante.com.br/products/y") == "sextante.com.br"


def test_deceptive_suffix_does_not_inherit_the_host_it_imitates() -> None:
    policy = _policy(denylist=("reddit.com",), shared_owner_hosts=("amazon.com",))

    assert derive_source_owner("https://notamazon.com/dp/1", policy)["tier"] == "derived"
    assert derive_source_owner("https://notreddit.com/r/books", policy)["admitted"] is True


def test_subdomains_of_a_listed_host_inherit_its_tier() -> None:
    policy = _policy(denylist=("reddit.com",), shared_owner_hosts=("amazon.com",))

    assert derive_source_owner("https://old.reddit.com/r/books", policy)["admitted"] is False
    assert derive_source_owner("https://smile.amazon.com/dp/1", policy)["tier"] == "shared_owner"


def test_committed_policy_loads_and_admits_the_baseline_run_failures() -> None:
    """Regression fixture from run 4090c59a (2026-09-13).

    Every URL below was found by discovery and discarded unread as
    off-policy. Two of them were then used by hand to write the Content
    Description the run failed to produce. Under the committed policy they
    are admitted, and the publisher and the review resolve to different
    owners, so the pair can satisfy the two-owner rule.
    """
    policy = load_owner_policy(REPO_ROOT / "configs" / "source-owner-policy.json")

    publisher = derive_source_owner("https://www.versobooks.com/products/3329-medium-hot", policy)
    review = derive_source_owner(
        "https://www.artnews.com/art-in-america/aia-reviews/hito-steyerl-medium-hot-images-heat-verso-1234744204/",
        policy,
    )
    french_publisher = derive_source_owner(
        "https://www.editionsladecouverte.fr/une_prehistoire_des_femmes-9782348080654", policy
    )
    french_review = derive_source_owner(
        "https://www.telerama.fr/livre/une-prehistoire-des-femmes-d-anne-augereau.php", policy
    )

    assert publisher["admitted"] and review["admitted"]
    assert publisher["owner_id"] != review["owner_id"]
    assert french_publisher["admitted"] and french_review["admitted"]
    assert french_publisher["owner_id"] != french_review["owner_id"]


def test_committed_policy_still_refuses_junk_and_collapses_storefronts() -> None:
    policy = load_owner_policy(REPO_ROOT / "configs" / "source-owner-policy.json")

    assert derive_source_owner("https://www.reddit.com/r/books/x", policy)["admitted"] is False
    assert derive_source_owner("https://www.ebay.com/itm/318595133701", policy)["admitted"] is False

    storefronts = {
        derive_source_owner(url, policy)["owner_id"]
        for url in (
            "https://www.amazon.fr/dp/2130879861",
            "https://books.google.com/books/about/Medium_Hot.html?id=g9UWEQAAQBAJ",
            "https://www.fnac.com/a22191038/x",
        )
    }
    assert len(storefronts) == 1


def test_malformed_policy_file_fails_loudly(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text('{"version": 1}', encoding="utf-8")

    with pytest.raises(ValueError):
        load_owner_policy(broken)


def test_subdomains_of_one_organisation_are_one_owner() -> None:
    """Deliberate deviation from #687's "subdomains stay distinct owners".

    That wording was inherited from the retired policy file, where
    ``us.macmillan.com`` and ``en.wikipedia.org`` were distinct *policy
    hosts* because they needed distinct path rules. As an *owner* rule it is
    wrong and unsafe: two Macmillan pages are not two independent
    confirmations, and treating them as such is exactly the fake agreement
    the two-owner rule exists to prevent. Collapsing to the registrable
    domain errs toward refusing agreement, which is the safe direction.
    """
    policy = _policy()

    def owner(url: str) -> str:
        return derive_source_owner(url, policy)["owner_id"]

    assert owner("https://us.macmillan.com/books/9780374610425/") == owner("https://macmillan.com/x")
    assert owner("https://en.wikipedia.org/wiki/X") == owner("https://fr.wikipedia.org/wiki/Y")


def test_policy_version_must_be_present_and_supported(tmp_path: Path) -> None:
    """Review finding 3.

    #687 requires the configuration be versioned. The loader accepted both a
    missing version and an unknown future one, which means a policy written
    against a later, differently-interpreted schema would be read under the
    current rules instead of refused.
    """
    import json

    base = json.loads((REPO_ROOT / "configs" / "source-owner-policy.json").read_text(encoding="utf-8"))

    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps({k: v for k, v in base.items() if k != "version"}), encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        load_owner_policy(missing)

    future = tmp_path / "future.json"
    future.write_text(json.dumps({**base, "version": 999}), encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        load_owner_policy(future)

    committed = load_owner_policy(REPO_ROOT / "configs" / "source-owner-policy.json")
    assert committed.denylist


def test_policy_version_must_be_an_integer_not_a_bool_or_string(tmp_path: Path) -> None:
    """Review finding: ``True == 1`` and they share a hash in Python, so a
    boolean version passed the supported-version membership test."""
    import json

    base = json.loads((REPO_ROOT / "configs" / "source-owner-policy.json").read_text(encoding="utf-8"))

    for bad in (True, False, "1", 1.0, None, [1]):
        path = tmp_path / f"v-{type(bad).__name__}-{bad}.json"
        path.write_text(json.dumps({**base, "version": bad}), encoding="utf-8")
        with pytest.raises(ValueError, match="version"):
            load_owner_policy(path)
