"""Audited reconsideration for rejected Source Proposals (Issue #580).

Shared prerequisite path, exercised with synthetic accepted evidence only.
A fresh accepted probe does not reopen a rejected proposal by itself.
Reconsideration is an explicit, confirmation-bound path through the existing
proposal machinery: it preserves the old rejection, binds to fresh accepted
evidence, requires an exact preview plus explicit editor confirmation, and
refuses stale or incompatible inputs before mutation. Nothing here claims the
synthetic evidence is live Indie Book Awards data; the live 2026-09-06 probe
rejected and is recorded in docs/issue-301-australian-source-feasibility.md.
"""

from __future__ import annotations

import hashlib as _hashlib

import pytest

from autoscout import source_proposals


def _rejected_indie_proposal() -> dict[str, object]:
    return {
        "id": "source-indiebookawards-com-au-2026-shortlist",
        "source_url": "https://www.indiebookawards.com.au/2026-shortlist",
        "normalized_source": "https://indiebookawards.com.au/2026-shortlist",
        "proposed_by": "agent",
        "proposal_date": "2026-07-18",
        "reason": "Issue #301 evaluated",
        "supporting_evidence": [],
        "suggested_signal_family": "award",
        "source_name": "Indie Book Awards 2026 — Non-Fiction",
        "access_status": "open",
        "caveats": "mixed page, no links",
        "editor_endorsement": None,
        "status": "rejected",
        "decision_reason": "no exact category URL; no book-level title links",
    }


EVIDENCE_PATH = "/tmp/auto-scout-580-isolated/ledger.json"
EVIDENCE_SHA = _hashlib.sha256(b"indie-2026-09-06-ledger-entry").hexdigest()
OTHER_SHA = _hashlib.sha256(b"indie-2026-09-06-other-capture").hexdigest()


def _accepted_evidence() -> dict[str, object]:
    return {
        "source_url": "https://www.indiebookawards.com.au/2026-shortlist",
        "outcome": "accept",
        "reason_code": "ok",
        "probe_date": "2026-09-06",
        "bounded_entry_count": 4,
        "detected_heading": "2026 Shortlist",
        "contract_family": "exact-book-list",
        "evidence_path": EVIDENCE_PATH,
        "evidence_sha256": EVIDENCE_SHA,
    }


def test_preview_preserves_rejection_and_binds_fresh_evidence() -> None:
    preview, token = source_proposals.prepare_proposal_reconsideration(
        _rejected_indie_proposal(),
        fresh_evidence=_accepted_evidence(),
        reconsideration_reason="Fresh bounded probe accepts linked exact-list route",
        reconsidered_by="agent",
        reconsideration_date="2026-09-06",
    )
    assert preview["confirmation_required"] is True
    assert token
    assert preview["confirmation_token"] == token
    assert preview["existing_status"] == "rejected"
    assert preview["existing_proposal_date"] == "2026-07-18"
    assert preview["existing_decision_reason"] == "no exact category URL; no book-level title links"
    assert preview["fresh_probe_date"] == "2026-09-06"
    assert preview["fresh_outcome"] == "accept"
    assert preview["fresh_evidence_path"] == EVIDENCE_PATH
    assert preview["fresh_evidence_sha256"] == EVIDENCE_SHA


def test_confirm_requires_exact_token_and_explicit_confirmation() -> None:
    existing = _rejected_indie_proposal()
    preview, token = source_proposals.prepare_proposal_reconsideration(
        existing,
        fresh_evidence=_accepted_evidence(),
        reconsideration_reason="Fresh bounded probe accepts linked exact-list route",
        reconsidered_by="agent",
        reconsideration_date="2026-09-06",
    )
    with pytest.raises(source_proposals.ProposalReconsiderationError):
        source_proposals.confirm_proposal_reconsideration(
            existing,
            preview_payload=preview,
            confirmation_token="wrong-token",
            confirmed_by_editor=True,
            fresh_evidence=_accepted_evidence(),
        )
    with pytest.raises(source_proposals.ProposalReconsiderationError):
        source_proposals.confirm_proposal_reconsideration(
            existing,
            preview_payload=preview,
            confirmation_token=token,
            confirmed_by_editor=False,
            fresh_evidence=_accepted_evidence(),
        )


def test_confirm_preserves_old_rejection_in_history() -> None:
    existing = _rejected_indie_proposal()
    preview, token = source_proposals.prepare_proposal_reconsideration(
        existing,
        fresh_evidence=_accepted_evidence(),
        reconsideration_reason="Fresh bounded probe accepts linked exact-list route",
        reconsidered_by="agent",
        reconsideration_date="2026-09-06",
    )
    updated = source_proposals.confirm_proposal_reconsideration(
        existing,
        preview_payload=preview,
        confirmation_token=token,
        confirmed_by_editor=True,
        fresh_evidence=_accepted_evidence(),
    )
    assert updated["status"] == "proposed"
    assert updated["id"] == existing["id"]
    assert updated["source_url"] == existing["source_url"]
    assert updated["normalized_source"] == existing["normalized_source"]
    prior = updated["prior_decisions"]
    assert isinstance(prior, list) and len(prior) == 1
    assert prior[0]["status"] == "rejected"
    assert prior[0]["proposal_date"] == "2026-07-18"
    assert prior[0]["decision_reason"] == "no exact category URL; no book-level title links"
    assert updated["proposal_date"] == "2026-09-06"
    # Original mapping is not mutated by preview or confirm.
    assert existing["status"] == "rejected"
    assert "prior_decisions" not in existing


def test_preview_refuses_non_rejected_proposal() -> None:
    proposed = {**_rejected_indie_proposal(), "status": "proposed"}
    with pytest.raises(source_proposals.ProposalReconsiderationError) as exc:
        source_proposals.prepare_proposal_reconsideration(
            proposed,
            fresh_evidence=_accepted_evidence(),
            reconsideration_reason="reason",
            reconsidered_by="agent",
            reconsideration_date="2026-09-06",
        )
    assert exc.value.reason_code == "proposal_not_rejected"


def test_preview_refuses_non_accepted_evidence() -> None:
    rejected_evidence = {**_accepted_evidence(), "outcome": "reject", "reason_code": "unbounded_entries"}
    with pytest.raises(source_proposals.ProposalReconsiderationError) as exc:
        source_proposals.prepare_proposal_reconsideration(
            _rejected_indie_proposal(),
            fresh_evidence=rejected_evidence,
            reconsideration_reason="reason",
            reconsidered_by="agent",
            reconsideration_date="2026-09-06",
        )
    assert exc.value.reason_code == "evidence_not_accepted"


def test_preview_refuses_url_mismatch() -> None:
    other = {**_accepted_evidence(), "source_url": "https://example.test/other"}
    with pytest.raises(source_proposals.ProposalReconsiderationError) as exc:
        source_proposals.prepare_proposal_reconsideration(
            _rejected_indie_proposal(),
            fresh_evidence=other,
            reconsideration_reason="reason",
            reconsidered_by="agent",
            reconsideration_date="2026-09-06",
        )
    assert exc.value.reason_code == "evidence_url_mismatch"


def test_preview_refuses_stale_evidence() -> None:
    stale = {**_accepted_evidence(), "probe_date": "2026-07-18"}
    with pytest.raises(source_proposals.ProposalReconsiderationError) as exc:
        source_proposals.prepare_proposal_reconsideration(
            _rejected_indie_proposal(),
            fresh_evidence=stale,
            reconsideration_reason="reason",
            reconsidered_by="agent",
            reconsideration_date="2026-07-18",
        )
    assert exc.value.reason_code == "evidence_stale"


def test_preview_is_zero_mutation() -> None:
    existing = _rejected_indie_proposal()
    snapshot = dict(existing)
    source_proposals.prepare_proposal_reconsideration(
        existing,
        fresh_evidence=_accepted_evidence(),
        reconsideration_reason="reason",
        reconsidered_by="agent",
        reconsideration_date="2026-09-06",
    )
    assert existing == snapshot


def test_confirm_refuses_stale_preview_payload() -> None:
    existing = _rejected_indie_proposal()
    preview, token = source_proposals.prepare_proposal_reconsideration(
        existing,
        fresh_evidence=_accepted_evidence(),
        reconsideration_reason="original reason",
        reconsidered_by="agent",
        reconsideration_date="2026-09-06",
    )
    tampered = dict(preview, reconsideration_reason="changed reason")
    with pytest.raises(source_proposals.ProposalReconsiderationError) as exc:
        source_proposals.confirm_proposal_reconsideration(
            existing,
            preview_payload=tampered,
            confirmation_token=token,
            confirmed_by_editor=True,
            fresh_evidence=_accepted_evidence(),
        )
    assert exc.value.reason_code == "confirmation_payload_mismatch"


def test_prepare_requires_evidence_identity_and_digest() -> None:
    for field in ("evidence_path", "evidence_sha256"):
        evidence = _accepted_evidence()
        del evidence[field]
        with pytest.raises(source_proposals.ProposalReconsiderationError) as exc:
            source_proposals.prepare_proposal_reconsideration(
                _rejected_indie_proposal(),
                fresh_evidence=evidence,
                reconsideration_reason="reason",
                reconsidered_by="agent",
                reconsideration_date="2026-09-06",
            )
        assert exc.value.reason_code == "evidence_identity_missing"
    bad_digest = {**_accepted_evidence(), "evidence_sha256": "not-a-digest"}
    with pytest.raises(source_proposals.ProposalReconsiderationError) as exc:
        source_proposals.prepare_proposal_reconsideration(
            _rejected_indie_proposal(),
            fresh_evidence=bad_digest,
            reconsideration_reason="reason",
            reconsidered_by="agent",
            reconsideration_date="2026-09-06",
        )
    assert exc.value.reason_code == "evidence_identity_missing"


def test_tokens_differ_for_different_evidence_digests() -> None:
    other_sha = OTHER_SHA
    assert other_sha != EVIDENCE_SHA
    _, token_a = source_proposals.prepare_proposal_reconsideration(
        _rejected_indie_proposal(),
        fresh_evidence=_accepted_evidence(),
        reconsideration_reason="reason",
        reconsidered_by="agent",
        reconsideration_date="2026-09-06",
    )
    _, token_b = source_proposals.prepare_proposal_reconsideration(
        _rejected_indie_proposal(),
        fresh_evidence={**_accepted_evidence(), "evidence_sha256": other_sha},
        reconsideration_reason="reason",
        reconsidered_by="agent",
        reconsideration_date="2026-09-06",
    )
    assert token_a != token_b


def _forged_preview(field: str, value: object) -> tuple[dict[str, object], str]:
    existing = _rejected_indie_proposal()
    preview, _ = source_proposals.prepare_proposal_reconsideration(
        existing,
        fresh_evidence=_accepted_evidence(),
        reconsideration_reason="reason",
        reconsidered_by="agent",
        reconsideration_date="2026-09-06",
    )
    payload = {k: v for k, v in preview.items() if k not in {"confirmation_token", "confirmation_required"}}
    payload[field] = value
    forged_token = source_proposals.reconsideration_token_for(payload)
    forged_preview = {**payload, "confirmation_required": True, "confirmation_token": forged_token}
    return forged_preview, forged_token


def test_confirm_revalidates_forged_reject_outcome() -> None:
    forged_preview, forged_token = _forged_preview("fresh_outcome", "reject")
    with pytest.raises(source_proposals.ProposalReconsiderationError) as exc:
        source_proposals.confirm_proposal_reconsideration(
            _rejected_indie_proposal(),
            preview_payload=forged_preview,
            confirmation_token=forged_token,
            confirmed_by_editor=True,
            fresh_evidence=_accepted_evidence(),
        )
    assert exc.value.reason_code == "evidence_not_accepted"


def test_confirm_revalidates_forged_evidence_url() -> None:
    forged_preview, forged_token = _forged_preview("fresh_source_url", "https://example.test/other")
    with pytest.raises(source_proposals.ProposalReconsiderationError) as exc:
        source_proposals.confirm_proposal_reconsideration(
            _rejected_indie_proposal(),
            preview_payload=forged_preview,
            confirmation_token=forged_token,
            confirmed_by_editor=True,
            fresh_evidence=_accepted_evidence(),
        )
    assert exc.value.reason_code == "evidence_url_mismatch"


def test_confirm_revalidates_forged_probe_date() -> None:
    forged_preview, forged_token = _forged_preview("fresh_probe_date", "2026-07-18")
    with pytest.raises(source_proposals.ProposalReconsiderationError) as exc:
        source_proposals.confirm_proposal_reconsideration(
            _rejected_indie_proposal(),
            preview_payload=forged_preview,
            confirmation_token=forged_token,
            confirmed_by_editor=True,
            fresh_evidence=_accepted_evidence(),
        )
    assert exc.value.reason_code == "evidence_stale"


def test_confirm_refuses_changed_evidence_digest() -> None:
    existing = _rejected_indie_proposal()
    preview, token = source_proposals.prepare_proposal_reconsideration(
        existing,
        fresh_evidence=_accepted_evidence(),
        reconsideration_reason="reason",
        reconsidered_by="agent",
        reconsideration_date="2026-09-06",
    )
    changed = {**_accepted_evidence(), "evidence_sha256": OTHER_SHA}
    with pytest.raises(source_proposals.ProposalReconsiderationError) as exc:
        source_proposals.confirm_proposal_reconsideration(
            existing,
            preview_payload=preview,
            confirmation_token=token,
            confirmed_by_editor=True,
            fresh_evidence=changed,
        )
    assert exc.value.reason_code == "evidence_changed"


def test_confirm_revalidates_current_reject_evidence() -> None:
    existing = _rejected_indie_proposal()
    preview, token = source_proposals.prepare_proposal_reconsideration(
        existing,
        fresh_evidence=_accepted_evidence(),
        reconsideration_reason="reason",
        reconsidered_by="agent",
        reconsideration_date="2026-09-06",
    )
    current_reject = {**_accepted_evidence(), "outcome": "reject", "reason_code": "unbounded_entries"}
    with pytest.raises(source_proposals.ProposalReconsiderationError) as exc:
        source_proposals.confirm_proposal_reconsideration(
            existing,
            preview_payload=preview,
            confirmation_token=token,
            confirmed_by_editor=True,
            fresh_evidence=current_reject,
        )
    assert exc.value.reason_code == "evidence_not_accepted"


def test_confirmed_audit_preserves_evidence_identity() -> None:
    existing = _rejected_indie_proposal()
    preview, token = source_proposals.prepare_proposal_reconsideration(
        existing,
        fresh_evidence=_accepted_evidence(),
        reconsideration_reason="reason",
        reconsidered_by="agent",
        reconsideration_date="2026-09-06",
    )
    updated = source_proposals.confirm_proposal_reconsideration(
        existing,
        preview_payload=preview,
        confirmation_token=token,
        confirmed_by_editor=True,
        fresh_evidence=_accepted_evidence(),
    )
    entry = updated["supporting_evidence"][-1]
    assert entry["evidence_path"] == EVIDENCE_PATH
    assert entry["evidence_sha256"] == EVIDENCE_SHA


def test_confirmed_audits_differ_for_different_captures() -> None:
    existing = _rejected_indie_proposal()
    preview_a, token_a = source_proposals.prepare_proposal_reconsideration(
        existing,
        fresh_evidence=_accepted_evidence(),
        reconsideration_reason="reason",
        reconsidered_by="agent",
        reconsideration_date="2026-09-06",
    )
    preview_b, token_b = source_proposals.prepare_proposal_reconsideration(
        existing,
        fresh_evidence={**_accepted_evidence(), "evidence_sha256": OTHER_SHA},
        reconsideration_reason="reason",
        reconsidered_by="agent",
        reconsideration_date="2026-09-06",
    )
    updated_a = source_proposals.confirm_proposal_reconsideration(
        existing,
        preview_payload=preview_a,
        confirmation_token=token_a,
        confirmed_by_editor=True,
        fresh_evidence=_accepted_evidence(),
    )
    updated_b = source_proposals.confirm_proposal_reconsideration(
        existing,
        preview_payload=preview_b,
        confirmation_token=token_b,
        confirmed_by_editor=True,
        fresh_evidence={**_accepted_evidence(), "evidence_sha256": OTHER_SHA},
    )
    assert updated_a["supporting_evidence"][-1] != updated_b["supporting_evidence"][-1]


def test_confirm_revalidates_empty_reconsideration_reason() -> None:
    forged_preview, forged_token = _forged_preview("reconsideration_reason", "  ")
    with pytest.raises(source_proposals.ProposalReconsiderationError) as exc:
        source_proposals.confirm_proposal_reconsideration(
            _rejected_indie_proposal(),
            preview_payload=forged_preview,
            confirmation_token=forged_token,
            confirmed_by_editor=True,
            fresh_evidence=_accepted_evidence(),
        )
    assert exc.value.reason_code == "reason_required"


def test_confirm_revalidates_empty_reconsidered_by() -> None:
    forged_preview, forged_token = _forged_preview("reconsidered_by", "")
    with pytest.raises(source_proposals.ProposalReconsiderationError) as exc:
        source_proposals.confirm_proposal_reconsideration(
            _rejected_indie_proposal(),
            preview_payload=forged_preview,
            confirmation_token=forged_token,
            confirmed_by_editor=True,
            fresh_evidence=_accepted_evidence(),
        )
    assert exc.value.reason_code == "reconsidered_by_required"


def test_preview_refuses_nonstring_reason_and_by() -> None:
    for kwargs, code in (
        ({"reconsideration_reason": None, "reconsidered_by": "agent"}, "reason_required"),
        ({"reconsideration_reason": [], "reconsidered_by": "agent"}, "reason_required"),
        ({"reconsideration_reason": {}, "reconsidered_by": "agent"}, "reason_required"),
        ({"reconsideration_reason": "reason", "reconsidered_by": None}, "reconsidered_by_required"),
        ({"reconsideration_reason": "reason", "reconsidered_by": []}, "reconsidered_by_required"),
        ({"reconsideration_reason": "reason", "reconsidered_by": {}}, "reconsidered_by_required"),
    ):
        with pytest.raises(source_proposals.ProposalReconsiderationError) as exc:
            source_proposals.prepare_proposal_reconsideration(
                _rejected_indie_proposal(),
                fresh_evidence=_accepted_evidence(),
                reconsideration_date="2026-09-06",
                **kwargs,  # type: ignore[arg-type]
            )
        assert exc.value.reason_code == code


def test_confirm_revalidates_nonstring_reason_and_by() -> None:
    for field, value, code in (
        ("reconsideration_reason", None, "reason_required"),
        ("reconsideration_reason", [], "reason_required"),
        ("reconsideration_reason", {}, "reason_required"),
        ("reconsidered_by", None, "reconsidered_by_required"),
        ("reconsidered_by", [], "reconsidered_by_required"),
        ("reconsidered_by", {}, "reconsidered_by_required"),
    ):
        forged_preview, forged_token = _forged_preview(field, value)
        with pytest.raises(source_proposals.ProposalReconsiderationError) as exc:
            source_proposals.confirm_proposal_reconsideration(
                _rejected_indie_proposal(),
                preview_payload=forged_preview,
                confirmation_token=forged_token,
                confirmed_by_editor=True,
                fresh_evidence=_accepted_evidence(),
            )
        assert exc.value.reason_code == code


def test_normal_repropose_still_preserves_rejection() -> None:
    from autoscout.source_proposals import merge_proposal_attempt
    from autoscout.source_proposals import proposal_from_attempt

    existing = _rejected_indie_proposal()
    attempt = proposal_from_attempt(
        source_url="https://www.indiebookawards.com.au/2026-shortlist",
        proposed_by="editor",
        reason="Try again.",
    )
    proposals, proposal, created = merge_proposal_attempt([existing], attempt)
    assert created is False
    assert proposal["status"] == "rejected"
    assert len(proposals) == 1
