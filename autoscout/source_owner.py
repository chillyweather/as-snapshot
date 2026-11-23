"""Tiered source owner derivation (issue #687).

The grounded enrichment stage decided whether a page could be read by
looking its host up in a maintained allowlist. That discarded 91% of the
URLs discovery found, and made two-owner grounding unreachable in four of
seven supported languages (issue #686).

This module derives a source owner from a URL instead of registering one.
Derivation resolves in a fixed order:

1. A host on the **denylist** is refused. That list is a quality filter
   only -- forums, derivative summary sites, pirate mirrors -- and carries
   no crawl-permission meaning.
2. A host in the **shared-owner class** (retail listings, catalogue
   aggregators, storefronts) resolves to one shared owner, because those
   surfaces repeat publisher-supplied copy. They stay readable, but two of
   them can never satisfy the two-owner rule.
3. Anything else resolves to its **registrable domain**, with an
   equivalence table applied on top for genuinely shared ownership.

Derivation is a pure function of its arguments: no network, no file reads,
no global state. Loading the committed policy is a separate concern.

Nothing consumes this module yet. It ships beside the existing
``SourceClassifier`` as the expand step of the wider retirement, so no run
behaviour changes here.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import Any

TIER_DENYLIST = "denylist"
TIER_SHARED_OWNER = "shared_owner"
TIER_DERIVED = "derived"


@dataclass(frozen=True)
class OwnerPolicy:
    """The committed inputs to owner derivation.

    ``multi_label_suffixes`` carries the public suffixes that span more than
    one label (``co.uk``, ``com.br``). The repository deliberately avoids
    general public-suffix logic, so this stays a short, reviewed list of the
    suffixes that actually occur in book publishing. Without it a naive
    two-label rule would collapse every ``*.co.uk`` host onto one owner.
    """

    denylist: tuple[str, ...] = ()
    shared_owner_hosts: tuple[str, ...] = ()
    # Brand labels matched against the first label of the registrable
    # domain, so one entry covers every market a storefront operates in.
    # Enumerating hostnames was whack-a-mole: the markets nobody listed
    # became independent owners and two storefronts could agree.
    shared_owner_brands: tuple[str, ...] = ()
    shared_owner_id: str = "publisher-supplied-copy"
    equivalence: Mapping[str, str] = field(default_factory=dict)
    multi_label_suffixes: tuple[str, ...] = ()


_REQUIRED_POLICY_KEYS = (
    "version",
    "denylist",
    "shared_owner_hosts",
    "shared_owner_brands",
    "equivalence",
    "multi_label_suffixes",
)

# Policy schema versions this loader knows how to interpret. An unknown
# version is refused rather than read under current rules: a policy written
# against a later schema could mean something different by the same keys.
SUPPORTED_POLICY_VERSIONS = frozenset({1})


def load_owner_policy(path: Any) -> OwnerPolicy:
    """Load and validate the committed owner policy.

    Validation is deliberately loud: a policy that silently loses its
    denylist or its shared-owner class would let junk ground a Content
    Description, or let several storefronts look like several independent
    confirmations.
    """
    import json
    from pathlib import Path

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("owner policy must be a JSON object")
    missing = [key for key in _REQUIRED_POLICY_KEYS if key not in raw]
    if missing:
        raise ValueError(f"owner policy is missing required keys: {', '.join(missing)}")
    version = raw["version"]
    # `type(...) is int` rather than isinstance: bool subclasses int, and
    # True == 1 with a matching hash, so `True in {1}` would otherwise pass.
    if type(version) is not int or version not in SUPPORTED_POLICY_VERSIONS:
        supported = ", ".join(str(v) for v in sorted(SUPPORTED_POLICY_VERSIONS))
        raise ValueError(f"owner policy version {version!r} is not supported (supported: {supported})")
    for key in ("denylist", "shared_owner_hosts", "shared_owner_brands", "multi_label_suffixes"):
        if not isinstance(raw[key], list) or any(not isinstance(item, str) or not item.strip() for item in raw[key]):
            raise ValueError(f"owner policy {key} must be a list of nonblank strings")
    equivalence = raw["equivalence"]
    if not isinstance(equivalence, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) or not k.strip() or not v.strip()
        for k, v in equivalence.items()
    ):
        raise ValueError("owner policy equivalence must map nonblank hostnames to nonblank owners")
    shared_owner_id = raw.get("shared_owner_id", "publisher-supplied-copy")
    if not isinstance(shared_owner_id, str) or not shared_owner_id.strip():
        raise ValueError("owner policy shared_owner_id must be a nonblank string")
    return OwnerPolicy(
        denylist=tuple(item.strip().lower() for item in raw["denylist"]),
        shared_owner_hosts=tuple(item.strip().lower() for item in raw["shared_owner_hosts"]),
        shared_owner_brands=tuple(item.strip().lower() for item in raw["shared_owner_brands"]),
        shared_owner_id=shared_owner_id,
        equivalence={k.strip().lower(): v.strip() for k, v in equivalence.items()},
        multi_label_suffixes=tuple(item.strip().lower() for item in raw["multi_label_suffixes"]),
    )


def registrable_domain(host: str, multi_label_suffixes: Sequence[str] = ()) -> str:
    """Return the registrable domain for one canonical hostname."""
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    for suffix in multi_label_suffixes:
        suffix_labels = [label for label in suffix.split(".") if label]
        if labels[-len(suffix_labels) :] == suffix_labels:
            return ".".join(labels[-(len(suffix_labels) + 1) :])
    return ".".join(labels[-2:])


def derive_source_owner(url: str, policy: OwnerPolicy) -> dict[str, Any]:
    """Derive the owner of one source URL under ``policy``."""
    from autoscout.grounded_description import canonicalize_hostname
    from autoscout.grounded_description import canonicalize_url

    canonical = canonicalize_url(url)
    if not canonical:
        return {"admitted": False, "tier": "", "owner_id": "", "host": ""}
    from urllib.parse import urlsplit

    host = canonicalize_hostname(urlsplit(canonical).hostname or "")
    if _host_matches(host, policy.denylist):
        return {"admitted": False, "tier": TIER_DENYLIST, "owner_id": "", "host": host}
    # Brands match any whole label of the host, not the first label of the
    # registrable domain. Deriving the registrable domain needs a complete
    # public-suffix list, and an incomplete one silently turned unlisted
    # markets (amazon.com.be, amazon.com.sg) back into independent owners.
    # Whole-label matching needs no suffix knowledge, and a deceptive host
    # like notamazon.com still has no label equal to "amazon".
    labels = {label for label in host.split(".") if label}
    if _host_matches(host, policy.shared_owner_hosts) or (labels & set(policy.shared_owner_brands)):
        return {
            "admitted": True,
            "tier": TIER_SHARED_OWNER,
            "owner_id": policy.shared_owner_id,
            "host": host,
        }
    owner = registrable_domain(host, policy.multi_label_suffixes)
    # The equivalence table is keyed by either form, so a pair recorded as
    # hostnames still collapses when one of them carries a subdomain.
    owner = policy.equivalence.get(host) or policy.equivalence.get(owner) or owner
    return {"admitted": True, "tier": TIER_DERIVED, "owner_id": owner, "host": host}


def _host_matches(host: str, listed: Sequence[str]) -> bool:
    """True when ``host`` is one of ``listed`` or a subdomain of one.

    Subdomain matching is on whole labels, so ``notreddit.com`` never
    matches ``reddit.com``.
    """
    for entry in listed:
        candidate = entry.strip().lower().removeprefix("www.")
        if not candidate:
            continue
        if host == candidate or host.endswith("." + candidate):
            return True
    return False
