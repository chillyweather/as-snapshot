"""Offline tests for issue #725: fit-v2 editorial-fit scoring in shadow mode.

Covers the Jev adapter contract (validation, pinning, bounded failure
handling, replay), the model-input allowlist, the scoring artifact, the
report arithmetic (ties, empty classes, failed calls counted), and the
weekly stage's non-effect on ordering, labels, and state. No network and no
model are used.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from state_store_support import seed_state
from state_store_support import state_databases

from autoscout import editorial_fit_shadow as fit_shadow
from autoscout import jev_adapter
from autoscout.__main__ import main as cli_main
from autoscout.fit_scores import FIT_SCORE_COLUMN
from autoscout.jev_adapter import JevQuestion
from autoscout.jev_adapter import JevTransportError
from autoscout.jev_adapter import LiveJevAdapter
from autoscout.jev_adapter import ReplayJevAdapter
from autoscout.weekly_workflow import bind_editorial_fit_shadow
from autoscout.weekly_workflow import deterministic_adapters
from autoscout.weekly_workflow import run_weekly_workflow
from autoscout.weekly_workflow import weekly_adapters

RUN_DATE = "2026-10-10"
EDITOR_NOTE = "Мне ужасно нравится, надо срочно запросить."


def _candidate(index: int, **extra: Any) -> dict[str, Any]:
    candidate = {
        "identity_key": f"book {index}|author {index}",
        "candidate_id": f"candidate-{index:04d}",
        "title": f"Book {index}",
        "primary_authors": [f"Author {index}"],
        "candidate_category": "adult nonfiction",
        "content_description": f"Описание книги {index}.",
        "independent_signal_families": ["bestseller/ranking"],
        "source_evidence": [
            {
                "source_name": "Test Ranking",
                "signal_type": "bestseller/ranking",
                "signal_strength": "top ranking",
                "evidence_note": f"Rank {index}.",
                "evidence_url": "https://example.test/ranking",
            }
        ],
        "priority_label": "C",
        "publisher_fit": "",
    }
    candidate.update(extra)
    return candidate


def _response(fit_level: int, audience: str = "mass_market", reach: str = "unclear") -> dict[str, Any]:
    def one_hot(labels: list[str], chosen: str) -> dict[str, float]:
        return {label: 1.0 if label == chosen else 0.0 for label in labels}

    return {
        "model": jev_adapter.JEV_PINNED_MODEL,
        "usage": {"input_tokens": 900},
        "latency_ms": 700,
        "answers": {
            "fit": {"probabilities": one_hot(["0", "1", "2", "3", "4"], str(fit_level)), "confidence": 0.9},
            "audience": {
                "probabilities": one_hot([label for label, _ in fit_shadow.AUDIENCE_OPTIONS], audience),
                "confidence": 0.8,
            },
            "author_reach": {
                "probabilities": one_hot([label for label, _ in fit_shadow.AUTHOR_REACH_OPTIONS], reach),
                "confidence": 0.7,
            },
        },
    }


def _digest(candidate: dict[str, Any]) -> str:
    return jev_adapter.request_digest(fit_shadow.fit_model_state(candidate), fit_shadow.FIT_QUESTIONS)


def _replay(pairs: list[tuple[dict[str, Any], Any]]) -> ReplayJevAdapter:
    return ReplayJevAdapter({_digest(candidate): response for candidate, response in pairs})


def _write_run(tmp_path: Path, candidates: list[dict[str, Any]], history: list[dict[str, Any]] | None = None) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    (run_dir / "scouting-candidates.json").write_text(json.dumps(candidates, ensure_ascii=False), encoding="utf-8")
    seed_state(run_dir, candidate_history=history or [])
    return run_dir


def _snapshot(run_dir: Path) -> dict[str, str]:
    files = {
        str(path.relative_to(run_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(run_dir.rglob("*"))
        if path.is_file() and fit_shadow.SHADOW_DIRECTORY not in path.parts
    }
    return {**files, **{name: checksum.hex() for name, checksum in state_databases().items()}}


def _scores(run_dir: Path, run_date: str = RUN_DATE) -> dict[str, Any]:
    return json.loads(fit_shadow.scores_path(run_dir, run_date).read_text(encoding="utf-8"))


# ── Jev adapter ───────────────────────────────────────────────────────────


class JevAdapterTests:
    def test_valid_response_becomes_typed_answers(self) -> None:
        assessment = jev_adapter.validate_response(_response(3), fit_shadow.FIT_QUESTIONS, adapter_kind="replay")

        assert assessment.available
        assert assessment.returned_model == "jev-1.13.0"
        assert assessment.input_tokens == 900
        assert assessment.answers["fit"].probabilities["3"] == 1.0

    def test_noul_answer_is_a_yes_no_distribution(self) -> None:
        question = JevQuestion("memoir", "noul", "Is this a memoir?")
        payload = {"model": jev_adapter.JEV_PINNED_MODEL, "answers": {"memoir": {"noul": 0.25}}}

        assessment = jev_adapter.validate_response(payload, [question], adapter_kind="replay")

        assert question.wire() == {"type": "noul", "instructions": "Is this a memoir?"}
        assert assessment.answers["memoir"].probabilities == {"yes": 0.25, "no": 0.75}

    @pytest.mark.parametrize(
        ("mutate", "reason"),
        [
            (lambda r: r.update(model="jev-latest"), "model_mismatch"),
            (lambda r: r.update(model="jev-1.14.0"), "model_mismatch"),
            (lambda r: r["answers"].pop("audience"), "malformed_response"),
            (lambda r: r["answers"]["fit"]["probabilities"].update({"5": 0.0}), "malformed_response"),
            (lambda r: r["answers"]["fit"]["probabilities"].pop("0"), "malformed_response"),
            (lambda r: r["answers"]["fit"]["probabilities"].update({"3": 0.4}), "malformed_response"),
            (lambda r: r["answers"]["fit"]["probabilities"].update({"3": "1.0"}), "malformed_response"),
            (
                lambda r: r["answers"]["audience"]["probabilities"].update({"mass_market": float("nan")}),
                "malformed_response",
            ),
            (lambda r: r.update(answers=[]), "malformed_response"),
        ],
    )
    def test_unusable_response_is_rejected_whole(self, mutate: Any, reason: str) -> None:
        payload = _response(3)
        mutate(payload)

        assessment = jev_adapter.validate_response(payload, fit_shadow.FIT_QUESTIONS, adapter_kind="live")

        assert not assessment.available
        assert assessment.reason_code == reason
        assert assessment.answers == {}

    def test_live_adapter_without_key_makes_no_call(self) -> None:
        calls: list[bytes] = []
        adapter = LiveJevAdapter(None, transport=lambda key, body, timeout: calls.append(body))

        assessment = adapter.assess({"title": "Book"}, fit_shadow.FIT_QUESTIONS)

        assert assessment.reason_code == "credential_missing"
        assert calls == []

    def test_live_adapter_requests_the_pinned_model_and_sends_the_key_only_to_transport(self) -> None:
        seen: list[tuple[str, dict[str, Any], float]] = []

        def transport(key: str, body: bytes, timeout: float) -> Any:
            seen.append((key, json.loads(body.decode("utf-8")), timeout))
            return _response(4)

        assessment = LiveJevAdapter("secret-key", transport=transport).assess(
            {"title": "Book"}, fit_shadow.FIT_QUESTIONS
        )

        assert assessment.available
        key, payload, timeout = seen[0]
        assert key == "secret-key"
        assert payload["model"] == "jev-1.13.0"
        assert "secret-key" not in json.dumps(payload)
        assert timeout == jev_adapter.DEFAULT_TIMEOUT_SECONDS
        assert set(payload["questions"]) == {"fit", "audience", "author_reach"}

    def test_retryable_failure_is_retried_with_bounded_backoff(self) -> None:
        outcomes: list[Any] = [JevTransportError("rate_limited"), JevTransportError("overloaded"), _response(2)]
        sleeps: list[float] = []

        def transport(key: str, body: bytes, timeout: float) -> Any:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        assessment = LiveJevAdapter("k", transport=transport, sleep=sleeps.append).assess({}, fit_shadow.FIT_QUESTIONS)

        assert assessment.available
        assert sleeps == [1.0, 2.0]

    def test_retries_stop_at_the_attempt_bound(self) -> None:
        calls: list[int] = []

        def transport(key: str, body: bytes, timeout: float) -> Any:
            calls.append(1)
            raise JevTransportError("timeout")

        assessment = LiveJevAdapter("k", transport=transport, sleep=lambda _: None).assess({}, fit_shadow.FIT_QUESTIONS)

        assert assessment.reason_code == "timeout"
        assert len(calls) == jev_adapter.DEFAULT_MAX_ATTEMPTS

    def test_authentication_failure_is_not_retried(self) -> None:
        calls: list[int] = []

        def transport(key: str, body: bytes, timeout: float) -> Any:
            calls.append(1)
            raise JevTransportError("auth_failed")

        assessment = LiveJevAdapter("k", transport=transport, sleep=lambda _: None).assess({}, fit_shadow.FIT_QUESTIONS)

        assert assessment.reason_code == "auth_failed"
        assert len(calls) == 1

    def test_truncated_http_response_is_one_retryable_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import http.client

        class Truncated:
            def __enter__(self) -> Any:
                return self

            def __exit__(self, *exc: Any) -> None:
                return None

            def read(self, size: int) -> bytes:
                raise http.client.IncompleteRead(b"{")

        opened: list[int] = []

        def urlopen(request: Any, timeout: float) -> Any:
            opened.append(1)
            return Truncated()

        monkeypatch.setattr(jev_adapter.urllib.request, "urlopen", urlopen)

        assessment = LiveJevAdapter("k", sleep=lambda _: None).assess({}, fit_shadow.FIT_QUESTIONS)

        assert assessment.reason_code == "network_error"
        assert len(opened) == jev_adapter.DEFAULT_MAX_ATTEMPTS

    def test_unexpected_transport_error_never_escapes_the_adapter(self, tmp_path: Path) -> None:
        candidates = [_candidate(1), _candidate(2)]
        run_dir = _write_run(tmp_path, candidates)

        def transport(key: str, body: bytes, timeout: float) -> Any:
            if "Book 1" in body.decode("utf-8"):
                raise RuntimeError("unexpected")
            return _response(3)

        result = fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, LiveJevAdapter("k", transport=transport))

        assert result["scored"] == 1
        assert result["unavailable_reason_counts"] == {"network_error": 1}

    @pytest.mark.parametrize(
        ("status", "reason"),
        [
            (401, "auth_failed"),
            (422, "invalid_request"),
            (429, "rate_limited"),
            (529, "overloaded"),
            (418, "http_error"),
        ],
    )
    def test_http_statuses_map_to_closed_reason_codes(self, status: int, reason: str) -> None:
        assert jev_adapter._http_reason(status) == reason

    def test_replay_without_a_recording_never_reaches_the_network(self) -> None:
        assessment = ReplayJevAdapter({}).assess({"title": "Book"}, fit_shadow.FIT_QUESTIONS)

        assert assessment.reason_code == "replay_missing"

    def test_request_digest_changes_with_state_and_question_wording(self) -> None:
        base = jev_adapter.request_digest({"title": "Book"}, fit_shadow.FIT_QUESTIONS)
        reworded = (
            JevQuestion("fit", "score", "Different wording.", fit_shadow.FIT_LEVELS),
            *fit_shadow.FIT_QUESTIONS[1:],
        )

        assert base == jev_adapter.request_digest({"title": "Book"}, fit_shadow.FIT_QUESTIONS)
        assert base != jev_adapter.request_digest({"title": "Other"}, fit_shadow.FIT_QUESTIONS)
        assert base != jev_adapter.request_digest({"title": "Book"}, reworded)

    def test_key_is_read_from_environment_then_key_file(self, tmp_path: Path) -> None:
        key_file = tmp_path / "typesafe.env"
        key_file.write_text("# comment\nTYPESAFE_API_KEY=from-file\n", encoding="utf-8")

        assert jev_adapter.load_api_key({"TYPESAFE_API_KEY": "from-env"}, key_file) == "from-env"
        assert jev_adapter.load_api_key({}, key_file) == "from-file"
        assert jev_adapter.load_api_key({}, tmp_path / "absent.env") is None

    def test_suite_cannot_see_the_operator_key(self) -> None:
        assert jev_adapter.load_api_key() is None


# ── Model input ───────────────────────────────────────────────────────────


class ModelInputTests:
    def test_editor_decisions_and_internal_fields_never_enter_model_input(self) -> None:
        candidate = _candidate(
            1,
            editor_status="interested",
            editor_note=EDITOR_NOTE,
            priority_label="A",
            publisher_fit="strong",
            editorial_profile_notes="taste-0003: мемуары",
            visible_risk_status="clear visible risk",
            visible_risk_comment="risk comment",
            russian_edition_status="unclear",
            update_delta="new",
            signal_summary="1 independent signal",
            unexpected_field={"nested": "secret"},
        )

        payload = fit_shadow.preview_request(candidate)
        serialized = json.dumps(payload, ensure_ascii=False)

        assert set(payload["state"]) <= {
            "title",
            "english_title",
            "authors",
            "category",
            "publisher",
            "publication_year",
            "english_annotation",
            "description_en",
            "description_ru",
            "independent_signal_families",
            "signals",
        }
        for leaked in (EDITOR_NOTE, "interested", "strong", "taste-0003", "risk comment", "secret", "example.test"):
            assert leaked not in serialized

    def test_questions_quote_no_editor_text(self) -> None:
        wording = json.dumps([question.wire() for question in fit_shadow.FIT_QUESTIONS], ensure_ascii=False)

        assert all(ord(char) < 0x400 or ord(char) > 0x4FF for char in wording)

    def test_long_text_is_bounded(self) -> None:
        state = fit_shadow.fit_model_state(_candidate(1, content_description="я" * 5000))

        assert len(state["description_ru"]) == 1200

    def test_english_description_is_sent_only_when_it_matches_the_current_text(self) -> None:
        current = _candidate(
            1,
            description_provenance={"content_description": "Описание книги 1.", "english_description": "About one."},
        )
        stale = _candidate(
            1,
            description_provenance={"content_description": "Старый текст.", "english_description": "About one."},
        )

        assert fit_shadow.fit_model_state(current)["description_en"] == "About one."
        assert "description_en" not in fit_shadow.fit_model_state(stale)


# ── Scoring ───────────────────────────────────────────────────────────────


class ScoringTests:
    def test_scores_and_components_are_recorded_beside_each_candidate(self, tmp_path: Path) -> None:
        candidates = [_candidate(1), _candidate(2)]
        run_dir = _write_run(tmp_path, candidates)
        adapter = _replay(
            [
                (candidates[0], _response(4, "mass_market", "widely_known")),
                (candidates[1], _response(1, "niche_specialist")),
            ]
        )

        result = fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, adapter)

        artifact = _scores(run_dir)
        first, second = artifact["records"]
        assert result["scored"] == 2
        assert artifact["advisory"] is True
        assert artifact["question_set_version"] == "fit-v2"
        assert artifact["requested_model"] == "jev-1.13.0"
        assert first["components"]["fit"]["expected"] == 1.0
        assert first["components"]["audience"]["top"] == "mass_market"
        assert first["components"]["author_reach"]["top"] == "widely_known"
        assert first["composite"] == pytest.approx(1.25)
        assert second["composite"] == pytest.approx(0.25)
        assert [record["canonical_rank"] for record in artifact["records"]] == [1, 2]
        assert artifact["input_tokens"] == 1800
        assert artifact["estimated_cost_usd"] == pytest.approx(1800 / 1e6 * 0.042, abs=1e-6)

    def test_failed_calls_are_recorded_not_dropped(self, tmp_path: Path) -> None:
        candidates = [_candidate(1), _candidate(2), _candidate(3)]
        run_dir = _write_run(tmp_path, candidates)
        adapter = _replay([(candidates[0], _response(3)), (candidates[1], {"unavailable": "timeout"})])

        result = fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, adapter)

        artifact = _scores(run_dir)
        assert result["candidates"] == 3
        assert artifact["counts"] == {"candidates": 3, "scored": 1, "unavailable": 2, "reused": 0}
        assert artifact["unavailable_reason_counts"] == {"replay_missing": 1, "timeout": 1}
        assert [record["status"] for record in artifact["records"]] == ["scored", "unavailable", "unavailable"]
        assert "composite" not in artifact["records"][1]

    def test_shadow_scoring_leaves_ordering_labels_and_state_unchanged(self, tmp_path: Path) -> None:
        candidates = [_candidate(1), _candidate(2)]
        history = [
            {"identity_key": candidates[0]["identity_key"], "editor_status": "reject", "editor_note": EDITOR_NOTE}
        ]
        run_dir = _write_run(tmp_path, candidates, history)
        seed_state(run_dir, editorial_profile={"entries": []}, candidate_visibility=[])
        before = _snapshot(run_dir)

        fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, _replay([(c, _response(4)) for c in candidates]))
        fit_shadow.write_shadow_report(run_dir, run_dir)

        assert _snapshot(run_dir) == before
        after = json.loads((run_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
        assert [c["priority_label"] for c in after] == ["C", "C"]
        assert [c["publisher_fit"] for c in after] == ["", ""]
        assert sorted(p.name for p in fit_shadow.shadow_dir(run_dir).iterdir()) == [
            "report.json",
            "report.md",
            f"scores-{RUN_DATE}.json",
        ]

    def test_editor_note_never_reaches_the_adapter(self, tmp_path: Path) -> None:
        candidates = [_candidate(1, editor_note=EDITOR_NOTE)]
        history = [
            {"identity_key": candidates[0]["identity_key"], "editor_status": "interested", "editor_note": EDITOR_NOTE}
        ]
        run_dir = _write_run(tmp_path, candidates, history)
        sent: list[str] = []

        class Recording:
            kind = "replay"

            def assess(self, state: Any, questions: Any) -> Any:
                sent.append(json.dumps(jev_adapter.request_payload(state, questions), ensure_ascii=False))
                return jev_adapter.validate_response(_response(2), questions, adapter_kind="replay")

        fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, Recording())

        assert len(sent) == 1
        assert EDITOR_NOTE not in sent[0]
        assert "interested" not in sent[0]
        assert _scores(run_dir)["records"][0]["decided_before_scoring"] is True

    def test_hidden_candidates_are_not_scored(self, tmp_path: Path) -> None:
        candidates = [_candidate(1), _candidate(2)]
        run_dir = _write_run(tmp_path, candidates)
        seed_state(
            run_dir, candidate_visibility=[{"identity_key": candidates[0]["identity_key"], "visibility": "hidden"}]
        )

        fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, _replay([(c, _response(3)) for c in candidates]))

        assert [record["identity_key"] for record in _scores(run_dir)["records"]] == [candidates[1]["identity_key"]]

    def test_rerun_reuses_unchanged_scores_and_retries_failures(self, tmp_path: Path) -> None:
        candidates = [_candidate(1), _candidate(2)]
        run_dir = _write_run(tmp_path, candidates)
        fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, _replay([(candidates[0], _response(3))]))
        # The editor decides between the two runs; the first score must keep
        # its pre-decision marker.
        seed_state(
            run_dir, candidate_history=[{"identity_key": candidates[0]["identity_key"], "editor_status": "reject"}]
        )

        result = fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, _replay([(candidates[1], _response(1))]))

        first, second = _scores(run_dir)["records"]
        assert result["reused"] == 1
        assert first["status"] == "scored" and first["reused"] is True
        assert first["decided_before_scoring"] is False
        assert second["status"] == "scored" and second["reused"] is False

    def test_baseline_rank_follows_the_displayed_review_order(self, tmp_path: Path) -> None:
        from autoscout.review_rendering import assemble_cumulative_review

        candidates = [
            _candidate(1, title="Zebra", priority_label="C", update_delta="unchanged"),
            _candidate(2, title="Yak", priority_label="B", update_delta="unchanged"),
            _candidate(3, title="Xerus", priority_label="D", update_delta="new"),
            _candidate(4, title="Wombat", priority_label="B", update_delta="unchanged", independent_signal_count=3),
        ]
        run_dir = _write_run(tmp_path, candidates)

        fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, _replay([(c, _response(3)) for c in candidates]))

        rows, _ = assemble_cumulative_review(candidates, [], [])
        displayed = [row["identity_key"] for row in rows]
        records = _scores(run_dir)["records"]
        assert [record["identity_key"] for record in records] == displayed
        assert displayed == [candidates[index]["identity_key"] for index in (2, 3, 1, 0)]
        assert [record["canonical_rank"] for record in records] == [1, 2, 3, 4]

    def test_rerun_keeps_books_that_left_the_review_after_scoring(self, tmp_path: Path) -> None:
        candidates = [_candidate(1), _candidate(2), _candidate(3)]
        keys = [c["identity_key"] for c in candidates]
        run_dir = _write_run(tmp_path, candidates)
        fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, _replay([(c, _response(3)) for c in candidates]))
        # The editor rejects and hides the first book; the second drops out
        # of the current candidates.
        seed_state(
            run_dir,
            candidate_history=[{"identity_key": keys[0], "editor_status": "reject"}],
            candidate_visibility=[{"identity_key": keys[0], "visibility": "hidden"}],
        )
        (run_dir / "scouting-candidates.json").write_text(json.dumps([candidates[2]]), encoding="utf-8")

        result = fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, ReplayJevAdapter({}))

        records = {record["identity_key"]: record for record in _scores(run_dir)["records"]}
        assert set(records) == set(keys)
        assert result["scored"] == 3 and result["reused"] == 3
        assert records[keys[0]]["decided_before_scoring"] is False
        week = fit_shadow.build_shadow_report(run_dir, run_dir)["weeks"][0]
        assert week["labeled_books"] == 1 and week["rejects"] == 1

    def test_a_pre_decision_score_is_frozen_once_the_editor_decides(self, tmp_path: Path) -> None:
        original = _candidate(1)
        run_dir = _write_run(tmp_path, [original])
        fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, _replay([(original, _response(4))]))
        changed = _candidate(1, content_description="Новое описание.")
        _write_run(tmp_path, [changed], [{"identity_key": original["identity_key"], "editor_status": "reject"}])

        result = fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, _replay([(changed, _response(0))]))

        record = _scores(run_dir)["records"][0]
        assert result["reused"] == 1
        assert record["components"]["fit"]["expected"] == 1.0
        assert record["decided_before_scoring"] is False

    def test_a_same_day_rerun_after_an_identity_correction_keeps_the_observation(self, tmp_path: Path) -> None:
        original = _candidate(1)
        run_dir = _write_run(tmp_path, [original])
        fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, _replay([(original, _response(4))]))
        # The identity key is corrected and the editor decides, then scoring
        # runs again on the same date.
        renamed = _candidate(1, identity_key="book one corrected|author 1")
        _write_run(
            tmp_path,
            [renamed],
            [
                {
                    "identity_key": renamed["identity_key"],
                    "candidate_id": original["candidate_id"],
                    "editor_status": "reject",
                }
            ],
        )

        result = fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, _replay([(renamed, _response(0))]))

        records = _scores(run_dir)["records"]
        assert len(records) == 1 and result["reused"] == 1
        assert records[0]["decided_before_scoring"] is False
        assert records[0]["components"]["fit"]["expected"] == 1.0
        week = fit_shadow.build_shadow_report(run_dir, run_dir)["weeks"][0]
        assert week["labeled_books"] == 1 and week["rejects"] == 1

    def test_a_pre_decision_failure_is_frozen_once_the_editor_decides(self, tmp_path: Path) -> None:
        candidate = _candidate(1)
        run_dir = _write_run(tmp_path, [candidate])
        fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, ReplayJevAdapter({}))
        _write_run(tmp_path, [candidate], [{"identity_key": candidate["identity_key"], "editor_status": "interested"}])

        result = fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, _replay([(candidate, _response(4))]))

        record = _scores(run_dir)["records"][0]
        assert result["unavailable"] == 1 and result["reused"] == 1
        assert record["status"] == "unavailable" and record["decided_before_scoring"] is False
        week = fit_shadow.build_shadow_report(run_dir, run_dir)["weeks"][0]
        assert week["labeled_books"] == 1 and week["failed_calls_among_labeled"] == 1

    def test_replay_scores_are_never_relabelled_as_live(self, tmp_path: Path) -> None:
        candidates = [_candidate(1)]
        run_dir = _write_run(tmp_path, candidates)
        fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, _replay([(candidates[0], _response(1))]))
        calls: list[int] = []

        def transport(key: str, body: bytes, timeout: float) -> Any:
            calls.append(1)
            return _response(4)

        result = fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, LiveJevAdapter("k", transport=transport))

        artifact = _scores(run_dir)
        assert len(calls) == 1 and result["reused"] == 0
        assert artifact["records"][0]["adapter"] == "live"
        assert artifact["records"][0]["components"]["fit"]["expected"] == 1.0
        assert artifact["record_adapters"] == ["live"]
        assert fit_shadow.build_shadow_report(run_dir, run_dir)["weeks"][0]["adapters"] == ["live"]

    def test_changed_evidence_is_scored_again(self, tmp_path: Path) -> None:
        original = _candidate(1)
        run_dir = _write_run(tmp_path, [original])
        fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, _replay([(original, _response(3))]))
        changed = _candidate(1, content_description="Новое описание.")
        _write_run(tmp_path, [changed])

        result = fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, _replay([(changed, _response(0))]))

        assert result["reused"] == 0
        assert _scores(run_dir)["records"][0]["components"]["fit"]["expected"] == 0.0

    def test_authentication_failure_stops_further_calls(self, tmp_path: Path) -> None:
        candidates = [_candidate(index) for index in range(1, 6)]
        run_dir = _write_run(tmp_path, candidates)
        calls: list[int] = []

        class Rejecting:
            kind = "live"

            def assess(self, state: Any, questions: Any) -> Any:
                calls.append(1)
                return jev_adapter.unavailable("live", "auth_failed")

        result = fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, Rejecting(), workers=1)

        assert len(calls) == 1
        assert result["unavailable"] == 5
        assert result["unavailable_reason_counts"] == {"auth_failed": 5}

    def test_stage_budget_bounds_the_calls(self, tmp_path: Path) -> None:
        candidates = [_candidate(index) for index in range(1, 4)]
        run_dir = _write_run(tmp_path, candidates)
        clock = iter([0.0, 0.0, 100.0, 100.0, 100.0])

        result = fit_shadow.score_weekly_run(
            run_dir,
            run_dir,
            RUN_DATE,
            _replay([(c, _response(3)) for c in candidates]),
            workers=1,
            budget_seconds=10.0,
            monotonic=lambda: next(clock),
        )

        assert result["scored"] == 1
        assert result["unavailable_reason_counts"] == {"budget_exhausted": 2}

    def test_missing_candidates_file_is_an_input_error(self, tmp_path: Path) -> None:
        with pytest.raises(fit_shadow.EditorialFitShadowError):
            fit_shadow.score_weekly_run(tmp_path, tmp_path, RUN_DATE, ReplayJevAdapter({}))


# ── Report arithmetic ─────────────────────────────────────────────────────


class ReportArithmeticTests:
    def test_auc_counts_ties_as_half(self) -> None:
        assert fit_shadow.auc([2.0, 1.0], [1.0, 0.0]) == pytest.approx(0.875)
        assert fit_shadow.auc([1.0], [1.0]) == 0.5
        assert fit_shadow.auc([3.0, 2.0], [1.0, 0.0]) == 1.0
        assert fit_shadow.auc([0.0], [1.0]) == 0.0

    def test_auc_is_undefined_for_an_empty_class(self) -> None:
        assert fit_shadow.auc([], [1.0]) is None
        assert fit_shadow.auc([1.0], []) is None
        assert fit_shadow.auc_interval([], [1.0]) is None

    def test_failed_call_ranks_below_every_scored_book(self) -> None:
        assert fit_shadow.auc([None], [0.0]) == 0.0
        assert fit_shadow.auc([None], [None]) == 0.5

    def test_bootstrap_interval_is_deterministic_and_brackets_the_estimate(self) -> None:
        positives = [0.9, 0.8, 0.4, 0.7]
        negatives = [0.5, 0.3, 0.2, 0.6, 0.1]

        first = fit_shadow.auc_interval(positives, negatives)
        second = fit_shadow.auc_interval(positives, negatives)
        area = fit_shadow.auc(positives, negatives)

        assert first == second
        assert first is not None and area is not None
        assert first[0] <= area <= first[1]

    def test_top_fraction_splits_a_straddling_tie_group(self) -> None:
        # Ten books, top 20% is two places. One clear leader, then four tied
        # books (two positive) compete for the one remaining place.
        scored = [(0.9, True)] + [(0.5, True), (0.5, True), (0.5, False), (0.5, False)] + [(0.1, False)] * 5

        hits, top = fit_shadow.top_fraction_hits(scored, 0.2)

        assert top == 2
        assert hits == pytest.approx(1.5)

    def test_all_tied_scores_give_chance_level_recall(self) -> None:
        scored = [(1.0, index < 2) for index in range(10)]

        metrics = fit_shadow.ranking_metrics(scored)

        assert metrics["auc"] == 0.5
        assert metrics["recall_at_20"] == pytest.approx(0.2)
        assert metrics["recall_at_40"] == pytest.approx(0.4)
        assert metrics["precision_at_20"] == pytest.approx(0.2)

    def test_metrics_with_no_positives_are_null_not_zero(self) -> None:
        metrics = fit_shadow.ranking_metrics([(0.4, False), (0.2, False)])

        assert metrics["auc"] is None
        assert metrics["auc_ci95"] is None
        assert metrics["recall_at_20"] is None
        assert metrics["precision_at_20"] == 0.0

    def test_empty_slice_reports_nulls(self) -> None:
        metrics = fit_shadow.ranking_metrics([])

        assert metrics == {
            "auc": None,
            "auc_ci95": None,
            "recall_at_20": None,
            "precision_at_20": None,
            "recall_at_40": None,
            "precision_at_40": None,
        }


class ShadowReportTests:
    def _scored_week(self, tmp_path: Path, run_date: str, start: int, fits: list[int | None]) -> list[dict[str, Any]]:
        candidates = [_candidate(start + offset) for offset in range(len(fits))]
        run_dir = _write_run(tmp_path, candidates)
        pairs = [
            (candidate, _response(fit)) for candidate, fit in zip(candidates, fits, strict=True) if fit is not None
        ]
        fit_shadow.score_weekly_run(run_dir, run_dir, run_date, _replay(pairs))
        return candidates

    def _decide(self, tmp_path: Path, decisions: dict[str, str]) -> None:
        history = [
            {"identity_key": key, "editor_status": status, "editor_note": EDITOR_NOTE}
            for key, status in decisions.items()
        ]
        seed_state(tmp_path / "run", candidate_history=history)

    def test_report_compares_shadow_scores_with_later_decisions(self, tmp_path: Path) -> None:
        candidates = self._scored_week(tmp_path, RUN_DATE, 1, [4, 3, 1, 0])
        keys = [c["identity_key"] for c in candidates]
        self._decide(tmp_path, {keys[0]: "interested", keys[1]: "reject", keys[2]: "reject", keys[3]: "reject"})
        run_dir = tmp_path / "run"

        report = fit_shadow.write_shadow_report(run_dir, run_dir)

        week = report["weeks"][0]
        assert week["positives"] == 1 and week["rejects"] == 3
        assert week["base_rate"] == 0.25
        assert week["scorers"]["fit_v2_composite"]["auc"] == 1.0
        assert week["scorers"]["fit_v2_fit_only"]["recall_at_20"] == 1.0
        # Every book carries the same priority label, so the current label
        # cannot separate them.
        assert week["scorers"]["current_priority_label"]["auc"] == 0.5
        assert week["scorers"]["current_review_order"]["auc"] == 1.0
        assert report["cumulative"]["scorers"] == week["scorers"]
        assert week["positive_rate_by_audience"] == [{"value": "mass_market", "books": 4, "positive_rate": 0.25}]
        assert {row["value"]: row["positive_rate"] for row in week["positive_rate_by_fit_level"]} == {
            "0": 0.0,
            "1": 0.0,
            "3": 0.0,
            "4": 1.0,
        }
        assert report["operations"]["calls"] == 4
        assert report["operations"]["input_tokens"] == 3600
        assert report["operations"]["latency_ms"] == {"p50": 700, "p95": 700}
        assert EDITOR_NOTE not in (run_dir / fit_shadow.SHADOW_DIRECTORY / "report.json").read_text(encoding="utf-8")
        assert "fit-v2 composite" in (run_dir / fit_shadow.SHADOW_DIRECTORY / "report.md").read_text(encoding="utf-8")

    def test_failed_calls_are_counted_and_ranked_last(self, tmp_path: Path) -> None:
        candidates = self._scored_week(tmp_path, RUN_DATE, 1, [None, 2, 1])
        keys = [c["identity_key"] for c in candidates]
        self._decide(tmp_path, {keys[0]: "interested", keys[1]: "reject", keys[2]: "reject"})
        run_dir = tmp_path / "run"

        report = fit_shadow.build_shadow_report(run_dir, run_dir)

        week = report["weeks"][0]
        assert week["labeled_books"] == 3
        assert week["failed_calls_among_labeled"] == 1
        assert week["unavailable"] == 1
        assert report["operations"]["failed_calls"] == 1
        # The one positive had a failed call: it ranks below both rejects.
        assert week["scorers"]["fit_v2_composite"]["auc"] == 0.0
        assert week["scorers"]["fit_v2_composite"]["recall_at_40"] == 0.0

    def test_books_decided_before_scoring_and_already_known_are_left_out(self, tmp_path: Path) -> None:
        candidates = [_candidate(index) for index in range(1, 5)]
        keys = [c["identity_key"] for c in candidates]
        run_dir = _write_run(tmp_path, candidates, [{"identity_key": keys[0], "editor_status": "maybe_watch"}])
        fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, _replay([(c, _response(3)) for c in candidates]))
        self._decide(tmp_path, {keys[0]: "maybe_watch", keys[1]: "already_known", keys[2]: "reject"})

        week = fit_shadow.build_shadow_report(run_dir, run_dir)["weeks"][0]

        assert week["excluded_decided_before_scoring"] == 1
        assert week["excluded_already_known"] == 1
        assert week["awaiting_editor_decision"] == 1
        assert week["labeled_books"] == 1 and week["rejects"] == 1

    def test_a_book_counts_once_on_its_first_shadow_score(self, tmp_path: Path) -> None:
        candidates = self._scored_week(tmp_path, "2026-10-03", 1, [0, 4])
        self._scored_week(tmp_path, "2026-10-10", 1, [4, 0])
        keys = [c["identity_key"] for c in candidates]
        self._decide(tmp_path, {keys[0]: "interested", keys[1]: "reject"})
        run_dir = tmp_path / "run"

        report = fit_shadow.build_shadow_report(run_dir, run_dir)

        assert [week["labeled_books"] for week in report["weeks"]] == [2, 0]
        assert report["cumulative"]["labeled_books"] == 2
        assert report["cumulative"]["scorers"]["fit_v2_composite"]["auc"] == 0.0

    def test_an_identity_correction_does_not_count_a_book_twice(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        old = _candidate(1)
        _write_run(tmp_path, [old])
        fit_shadow.score_weekly_run(run_dir, run_dir, "2026-10-03", _replay([(old, _response(4))]))
        # The identity key is corrected; the second book's ID is retired onto
        # the first by a merge.
        renamed = _candidate(1, identity_key="book one corrected|author 1")
        merged = _candidate(2, candidate_id="candidate-retired")
        _write_run(tmp_path, [renamed, merged])
        fit_shadow.score_weekly_run(
            run_dir, run_dir, "2026-10-10", _replay([(renamed, _response(0)), (merged, _response(0))])
        )
        seed_state(
            run_dir,
            candidate_identity_aliases={
                "version": 2,
                "aliases": {},
                "candidates": {},
                "redirects": {"candidate-retired": old["candidate_id"]},
            },
            candidate_history=[
                {
                    "identity_key": renamed["identity_key"],
                    "candidate_id": old["candidate_id"],
                    "editor_status": "reject",
                }
            ],
        )

        report = fit_shadow.build_shadow_report(run_dir, run_dir)

        assert [week["labeled_books"] for week in report["weeks"]] == [1, 0]
        assert report["shadow_weeks_with_decisions"] == 1
        assert report["cumulative"]["labeled_books"] == 1
        assert report["weeks"][0]["positive_rate_by_fit_level"][0]["value"] == "4"

    def test_activation_needs_three_decided_shadow_weeks_and_never_activates_itself(self, tmp_path: Path) -> None:
        decisions: dict[str, str] = {}
        run_dir = tmp_path / "run"
        for week_index, run_date in enumerate(("2026-10-03", "2026-10-10", "2026-10-17")):
            candidates = self._scored_week(tmp_path, run_date, week_index * 10 + 1, [4, 1])
            decisions[candidates[0]["identity_key"]] = "needs_deeper_analysis"
            decisions[candidates[1]["identity_key"]] = "reject"
            self._decide(tmp_path, decisions)
            report = fit_shadow.build_shadow_report(run_dir, run_dir)
            expected = "ready_for_editor_decision" if week_index == 2 else "insufficient"
            assert report["activation"] == {
                "status": "not_activated",
                "evidence": expected,
                "note": report["activation"]["note"],
            }

        assert report["shadow_weeks_scored"] == 3
        assert report["shadow_weeks_with_decisions"] == 3
        assert report["cumulative"]["positives"] == 3

    def test_a_week_with_only_failed_calls_is_not_a_decided_shadow_week(self, tmp_path: Path) -> None:
        candidates = self._scored_week(tmp_path, RUN_DATE, 1, [None, None])
        keys = [c["identity_key"] for c in candidates]
        self._decide(tmp_path, {keys[0]: "interested", keys[1]: "reject"})
        run_dir = tmp_path / "run"

        report = fit_shadow.build_shadow_report(run_dir, run_dir)

        assert report["shadow_weeks_scored"] == 1
        assert report["shadow_weeks_with_decisions"] == 0

    def test_report_with_no_scores_is_empty_and_insufficient(self, tmp_path: Path) -> None:
        report = fit_shadow.build_shadow_report(tmp_path, tmp_path)

        assert report["shadow_weeks_scored"] == 0
        assert report["activation"]["evidence"] == "insufficient"
        assert report["cumulative"]["scorers"]["fit_v2_composite"]["auc"] is None
        assert "n/a" in fit_shadow.render_shadow_report(report)


# ── CLI ───────────────────────────────────────────────────────────────────


class CommandTests:
    def _args(self, run_dir: Path, *extra: str) -> list[str]:
        return ["editorial-fit-shadow", *extra, "--output-dir", str(run_dir), "--state-dir", str(run_dir)]

    def test_replay_score_writes_scores_and_report(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        candidates = [_candidate(1), _candidate(2)]
        run_dir = _write_run(tmp_path, candidates)
        replay_file = tmp_path / "replay.json"
        replay_file.write_text(
            json.dumps({"responses": {_digest(candidates[0]): _response(3)}}, ensure_ascii=False), encoding="utf-8"
        )

        code = cli_main(
            self._args(
                run_dir, "score", "--run-date", RUN_DATE, "--adapter", "replay", "--replay-file", str(replay_file)
            )
        )

        out = capsys.readouterr().out
        assert code == 0
        assert "scored=1 unavailable=1 reused=0 candidates=2" in out
        assert "unavailable_reason=replay_missing count=1" in out
        assert (run_dir / fit_shadow.SHADOW_DIRECTORY / "report.md").is_file()

    def test_live_score_without_a_key_attempts_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_dir = _write_run(tmp_path, [_candidate(1)])

        code = cli_main(self._args(run_dir, "score", "--run-date", RUN_DATE, "--adapter", "live"))

        assert code == 2
        assert "TYPESAFE_API_KEY is not set" in capsys.readouterr().out
        assert not fit_shadow.shadow_dir(run_dir).exists()

    def test_score_requires_an_explicit_adapter(self, tmp_path: Path) -> None:
        run_dir = _write_run(tmp_path, [_candidate(1)])

        with pytest.raises(SystemExit):
            cli_main(self._args(run_dir, "score", "--run-date", RUN_DATE))

    def test_replay_without_a_file_and_bad_date_are_input_errors(self, tmp_path: Path) -> None:
        run_dir = _write_run(tmp_path, [_candidate(1)])

        assert cli_main(self._args(run_dir, "score", "--run-date", RUN_DATE, "--adapter", "replay")) == 2
        assert cli_main(self._args(run_dir, "score", "--run-date", "next week", "--adapter", "replay")) == 2
        assert cli_main(self._args(tmp_path / "absent", "preview")) == 2

    def test_preview_prints_the_request_and_writes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_dir = _write_run(tmp_path, [_candidate(1, editor_note=EDITOR_NOTE)])

        code = cli_main(self._args(run_dir, "preview"))

        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["model"] == "jev-1.13.0"
        assert payload["state"]["title"] == "Book 1"
        assert EDITOR_NOTE not in json.dumps(payload, ensure_ascii=False)
        assert not fit_shadow.shadow_dir(run_dir).exists()

    def test_report_command_prints_and_writes_nothing_by_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_dir = _write_run(tmp_path, [_candidate(1)])
        before = _snapshot(run_dir)

        code = cli_main(self._args(run_dir, "report"))

        assert code == 0
        assert "Shadow weeks scored: 0" in capsys.readouterr().out
        assert not fit_shadow.shadow_dir(run_dir).exists()
        assert _snapshot(run_dir) == before

    def test_report_command_writes_only_on_request(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        run_dir = _write_run(tmp_path, [_candidate(1)])

        code = cli_main(self._args(run_dir, "report", "--write"))

        assert code == 0
        assert "shadow_weeks_scored=0" in capsys.readouterr().out
        assert (fit_shadow.shadow_dir(run_dir) / "report.md").is_file()


# ── Weekly stage ──────────────────────────────────────────────────────────


def _weekly_run(
    tmp_path: Path, *, flag: bool | None, editorial_fit: Any, ordering: bool | None = None
) -> tuple[Any, Path]:
    from tests.test_weekly_workflow import _write_capture_fixture
    from tests.test_weekly_workflow import _write_codex_fixture_config
    from tests.test_weekly_workflow import _write_extraction_fixture

    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = _write_codex_fixture_config(tmp_path)
    if flag is not None:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["editorial_fit_shadow"] = flag
        if ordering is not None:
            config["editorial_fit_ordering"] = ordering
        config_path.write_text(json.dumps(config), encoding="utf-8")
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture_fixture(tmp_path, output_dir)
    adapters = deterministic_adapters()
    adapters.editorial_fit = editorial_fit
    adapters.capture.prepare(config_path, output_dir, live=False)
    _write_extraction_fixture(output_dir)
    return run_weekly_workflow(config_path, output_dir, adapters=adapters), output_dir


class _ReplayStage:
    """Score every candidate with one fixed answer, through the real scorer."""

    def __init__(self) -> None:
        self.calls = 0

    def score(
        self,
        output_dir: Path,
        state_dir: Path,
        run_date: str,
        *,
        state_store: Any = None,
        review_ordered_by_fit: bool = False,
    ) -> dict[str, Any]:
        self.calls += 1
        candidates = fit_shadow.read_candidates(output_dir)
        result = fit_shadow.score_weekly_run(
            output_dir,
            state_dir,
            run_date,
            _replay([(candidate, _response(4)) for candidate in candidates]),
            state_store=state_store,
            review_ordered_by_fit=review_ordered_by_fit,
        )
        fit_shadow.write_shadow_report(output_dir, state_dir, state_store=state_store)
        return result


class _FailingStage:
    def score(
        self, output_dir: Path, state_dir: Path, run_date: str, *, state_store: Any = None, **_: Any
    ) -> dict[str, Any]:
        raise RuntimeError("vendor exploded")


_EDITOR_FACING = ("editor-facing.csv", "scouting-candidates.json", "candidate-history.json")


def _without_fit_column(review: str) -> str:
    """The Markdown review with the fit score column (#1105) taken out of its table."""
    lines = review.splitlines(keepends=True)
    header = next(line for line in lines if line.startswith("| # |"))
    column = [cell.strip() for cell in header.split("|")].index(FIT_SCORE_COLUMN)
    return "".join(
        "|".join(cells[:column] + cells[column + 1 :])
        if len(cells := line.split("|")) > column and line[0] == "|"
        else line
        for line in lines
    )


class WeeklyStageTests:
    def test_shadow_stage_scores_final_candidates_and_adds_only_the_score_column(self, tmp_path: Path) -> None:
        # Until #1105 the scores changed nothing in the review. Since #1105
        # (operator's decision of 2026-11-15) the review shows them in one
        # column; the order, the labels and every other cell stay as they were.
        stage = _ReplayStage()

        baseline, baseline_dir = _weekly_run(tmp_path / "baseline", flag=None, editorial_fit=None)
        shadowed, shadow_dir = _weekly_run(tmp_path / "shadow", flag=True, editorial_fit=stage)

        assert stage.calls == 1
        assert shadowed.outcome == baseline.outcome
        assert shadowed.review_available
        for name in _EDITOR_FACING:
            assert (shadow_dir / name).read_bytes() == (baseline_dir / name).read_bytes(), name
        review = (shadow_dir / "weekly-review.md").read_text(encoding="utf-8")
        assert review != (baseline_dir / "weekly-review.md").read_text(encoding="utf-8")
        assert _without_fit_column(review) == (baseline_dir / "weekly-review.md").read_text(encoding="utf-8")
        (scores_file,) = fit_shadow.shadow_dir(shadow_dir).glob("scores-*.json")
        artifact = json.loads(scores_file.read_text(encoding="utf-8"))
        candidates = json.loads((shadow_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
        assert [record["identity_key"] for record in artifact["records"]] == [c["identity_key"] for c in candidates]
        assert artifact["counts"]["scored"] == len(candidates) > 0
        assert all(c["publisher_fit"] == "" for c in candidates)
        entry = next(e for e in shadowed.log if e["stage"] == "editorial-fit-shadow")
        assert entry["status"] == "ok"
        assert not (baseline_dir / fit_shadow.SHADOW_DIRECTORY).exists()

    def test_the_scores_record_whether_the_review_was_ordered_by_them(self, tmp_path: Path) -> None:
        _, plain_dir = _weekly_run(tmp_path / "plain", flag=True, editorial_fit=_ReplayStage())
        _, ordered_dir = _weekly_run(tmp_path / "ordered", flag=True, editorial_fit=_ReplayStage(), ordering=True)

        (plain_scores,) = fit_shadow.shadow_dir(plain_dir).glob("scores-*.json")
        (ordered_scores,) = fit_shadow.shadow_dir(ordered_dir).glob("scores-*.json")
        assert "review_ordered_by_fit" not in json.loads(plain_scores.read_text(encoding="utf-8"))
        assert json.loads(ordered_scores.read_text(encoding="utf-8"))["review_ordered_by_fit"] is True

    def test_stage_needs_the_explicit_config_flag(self, tmp_path: Path) -> None:
        for name, flag in (("absent", None), ("off", False)):
            stage = _ReplayStage()

            result, output_dir = _weekly_run(tmp_path / name, flag=flag, editorial_fit=stage)

            assert stage.calls == 0
            assert not (output_dir / fit_shadow.SHADOW_DIRECTORY).exists()
            assert all(entry["stage"] != "editorial-fit-shadow" for entry in result.log)

    def test_a_failing_shadow_stage_never_stops_the_run(self, tmp_path: Path) -> None:
        baseline, _ = _weekly_run(tmp_path / "baseline", flag=None, editorial_fit=None)

        result, _ = _weekly_run(tmp_path / "failing", flag=True, editorial_fit=_FailingStage())

        assert result.outcome == baseline.outcome
        assert result.review_available
        entry = next(e for e in result.log if e["stage"] == "editorial-fit-shadow")
        assert entry["status"] == "failed"
        assert entry["detail"] == {"exception_type": "RuntimeError"}

    def test_only_the_weekly_command_binds_the_live_stage(self, tmp_path: Path) -> None:
        assert deterministic_adapters().editorial_fit is None
        adapters = weekly_adapters(tmp_path)
        assert adapters.editorial_fit is None

        bind_editorial_fit_shadow(adapters)

        assert isinstance(adapters.editorial_fit, fit_shadow.ProductionEditorialFitShadowStage)

    def test_live_stage_without_a_key_skips_and_writes_nothing(self, tmp_path: Path) -> None:
        run_dir = _write_run(tmp_path, [_candidate(1)])

        result = fit_shadow.ProductionEditorialFitShadowStage().score(run_dir, run_dir, RUN_DATE)

        assert result == {"status": "skipped", "reason": "credential_missing"}
        assert not fit_shadow.shadow_dir(run_dir).exists()

    def test_live_stage_with_a_key_scores_through_the_live_adapter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_dir = _write_run(tmp_path, [_candidate(1)])
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
        monkeypatch.setattr(jev_adapter, "_urllib_transport", lambda key, body, timeout: _response(3))

        result = fit_shadow.ProductionEditorialFitShadowStage().score(run_dir, run_dir, RUN_DATE)

        assert result["status"] == "ok" and result["scored"] == 1 and result["adapter"] == "live"
        assert (fit_shadow.shadow_dir(run_dir) / "report.json").is_file()
        assert "test-key" not in fit_shadow.scores_path(run_dir, RUN_DATE).read_text(encoding="utf-8")

    def test_weekly_config_accepts_the_flag_and_rejects_a_non_boolean(self) -> None:
        from autoscout.config import validate_config

        base = {"run_date": "2026-07-13", "sources": []}

        assert not [e for e in validate_config({**base, "editorial_fit_shadow": True}) if "editorial_fit_shadow" in e]
        assert [e for e in validate_config({**base, "editorial_fit_shadow": "live"}) if "editorial_fit_shadow" in e]
