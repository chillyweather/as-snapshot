"""Issue #388 - a second run on the same date must not discard its own evidence.

The second 2026-08-05 run captured 29 sources live, extracted 23 artifacts, and
produced a valid replay config holding 16 fresh sources. It then reported the
earlier run's numbers as its own: 0 new and 6 updated, where its own evidence
said 3 new and 33 upgraded.

``run_pipeline`` short-circuits to the editor-feedback path when
``scouting-candidates.json`` exists and the stored evidence date matches the run
date. Both runs fell on 2026-08-05, so the date matched, there was no pending
feedback, and the fresh evidence was thrown away.

The date was standing in for the real question, which is whether *this
operation* has already been scouted. A second run is a different operation, so
it binds on the operation and re-scouts. Where no operation is recorded on
either side, nothing can be proved and the older date guard still applies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest
from state_store_support import read_state
from state_store_support import seed_state

from autoscout.pipeline_path import PipelinePath
from autoscout.russian_workflow import render_workflow_result

OPERATION_ID = "operation-b-2d72a7df8538b4ae"
EARLIER_OPERATION_ID = "operation-a-c3277f424f5a730e"
SOURCE_NAME = "PW Best Books 2025"
SOURCE_URL = "https://example.test/pw-best-books-2025"


def _source(
    *,
    title: str | None = None,
    author: str = "Alex Example",
    extraction_method: str | None = None,
    recorded_mentions_file: str | None = None,
) -> dict:
    source = {
        "name": SOURCE_NAME,
        "family": "media/curated list",
        "url": SOURCE_URL,
        "access_status": "open",
    }
    if title is not None:
        source["fixture_candidates"] = [
            {
                "title": title,
                "primary_authors": [author],
                "source_url": SOURCE_URL,
                "signal_strength": "major media list",
                "evidence_note": "Listed among the year's best.",
            }
        ]
    if extraction_method is not None:
        source["extraction_method"] = extraction_method
    if recorded_mentions_file is not None:
        source["recorded_mentions_file"] = recorded_mentions_file
    return source


def _write_config(
    tmp_path: Path,
    *,
    filename: str,
    sources: list[dict],
    operation_id: str | None = None,
    evidence_digest: str | None = None,
    extra: dict | None = None,
) -> Path:
    config: dict = {"run_date": "2026-08-05", "sources": sources}
    if operation_id is not None:
        config["operation_id"] = operation_id
    if evidence_digest is not None:
        config["evidence_digest"] = evidence_digest
    if extra:
        config.update(extra)
    config_path = tmp_path / filename
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def _scout_config(tmp_path: Path, *, operation_id: str | None, evidence_digest: str = "one-fetch") -> Path:
    """A config the pipeline can actually scout, with one fixture source."""
    return _write_config(
        tmp_path,
        filename="config.json",
        sources=[_source(title="Book One")],
        operation_id=operation_id,
        evidence_digest=evidence_digest if operation_id is not None else None,
    )


class RunSummaryRecordsItsOperationTests:
    def test_run_summary_records_the_operation_that_produced_it(self, tmp_path: Path):
        """Without this, a later run has nothing to compare itself against."""
        from autoscout.pipeline import run_pipeline

        output_dir = tmp_path / "run"
        run_pipeline(_scout_config(tmp_path, operation_id=OPERATION_ID), output_dir)

        run_summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
        assert run_summary["operation_id"] == OPERATION_ID


class PipelineStageReportsItsPathTests:
    """A zero-second pipeline stage has to be readable after the fact.

    The evidence guard stops this particular no-op, but the reason it reached
    the editor at all is that ``_run_pipeline_stage`` flattened every outcome
    into ``{"returncode": 0}``. A no-op that ever happens again must be visible
    in the workflow log rather than indistinguishable from a completed scout.
    """

    def test_the_stage_reports_a_scouted_pipeline(self, tmp_path: Path):
        from autoscout.weekly_workflow import _run_pipeline_stage

        output_dir = tmp_path / "run"
        result = _run_pipeline_stage(
            _scout_config(tmp_path, operation_id=OPERATION_ID), output_dir, output_dir, "2026-08-05"
        )

        assert result["returncode"] == 0
        assert result["pipeline_path"] is PipelinePath.SCOUTED

    def test_the_stage_reports_a_no_op_pipeline(self, tmp_path: Path):
        from autoscout.weekly_workflow import _run_pipeline_stage

        output_dir = tmp_path / "run"
        config_path = _scout_config(tmp_path, operation_id=OPERATION_ID)
        _run_pipeline_stage(config_path, output_dir, output_dir, "2026-08-05")

        repeat = _run_pipeline_stage(config_path, output_dir, output_dir, "2026-08-05")

        assert repeat["returncode"] == 0
        assert repeat["pipeline_path"] is PipelinePath.FEEDBACK_NOOP

    def test_a_no_op_over_fresh_capture_is_not_publishable(self):
        """The combination that produced the 2026-08-05 report.

        The evidence guard closes the paths that reach it, but a guard is not a
        proof. When a run captured fresh evidence this run and its pipeline then
        scouted nothing, whatever is on disk was built by an earlier run, and
        publishing it under this run's identity is the fault itself.
        """
        from autoscout.weekly_workflow import pipeline_result_is_publishable

        assert pipeline_result_is_publishable(PipelinePath.FEEDBACK_NOOP, fresh_evidence_produced=True) is False

    def test_a_no_op_without_fresh_capture_is_publishable(self):
        """Nothing new was fetched, so the stored result is the right one."""
        from autoscout.weekly_workflow import pipeline_result_is_publishable

        assert pipeline_result_is_publishable(PipelinePath.FEEDBACK_NOOP, fresh_evidence_produced=False) is True

    def test_a_scouted_pipeline_is_always_publishable(self):
        from autoscout.weekly_workflow import pipeline_result_is_publishable

        assert pipeline_result_is_publishable(PipelinePath.SCOUTED, fresh_evidence_produced=True) is True
        assert pipeline_result_is_publishable(PipelinePath.SCOUTED, fresh_evidence_produced=False) is True

    def test_applied_feedback_is_publishable(self):
        """Feedback finalization is real work and produces its own artifacts."""
        from autoscout.weekly_workflow import pipeline_result_is_publishable

        assert pipeline_result_is_publishable(PipelinePath.FEEDBACK_APPLIED, fresh_evidence_produced=True) is True

    @pytest.mark.parametrize("pipeline_path", [None, "", "unknown"])
    def test_an_unreported_path_is_never_publishable(self, pipeline_path):
        """Missing and invalid paths must fail closed for every evidence state."""
        from autoscout.weekly_workflow import pipeline_result_is_publishable

        assert pipeline_result_is_publishable(pipeline_path, fresh_evidence_produced=True) is False
        assert pipeline_result_is_publishable(pipeline_path, fresh_evidence_produced=False) is False

    def test_a_missing_pipeline_result_is_a_stage_failure(self, tmp_path: Path, monkeypatch):
        from autoscout.weekly_workflow import _run_pipeline_stage

        monkeypatch.setattr("autoscout.weekly_workflow.run_pipeline", lambda *args, **kwargs: None)

        result = _run_pipeline_stage(
            _scout_config(tmp_path, operation_id=OPERATION_ID), tmp_path / "run", tmp_path / "run", "2026-08-05"
        )

        assert result["returncode"] != 0
        assert "no result" in result["exception"]

    def test_a_no_op_does_not_state_counts_as_this_run_s(self):
        """Criterion 5: an earlier run's counts must not carry this run's name.

        Nothing was fetched and nothing was scouted, so the run is not a
        failure. What it must not do is answer "what did you find this week?"
        with numbers another run produced.
        """
        from autoscout.russian_workflow import completion_message

        message = completion_message(
            outcome="completed/no-attention",
            new_count=3,
            updated_count=33,
            previously_seen=92,
            pipeline_path=PipelinePath.FEEDBACK_NOOP,
        )

        assert "Новых книг" not in message
        assert "Обновлено книг" not in message
        assert "Новый поиск не выполнялся." in message

    @pytest.mark.parametrize("pipeline_path", [PipelinePath.FEEDBACK_NOOP, PipelinePath.FEEDBACK_APPLIED])
    def test_feedback_paths_preserve_the_editor_output_contract(self, pipeline_path):
        from autoscout.russian_workflow import completion_message

        message = completion_message(
            outcome="completed/attention-required",
            new_count=3,
            updated_count=33,
            attention_required=True,
            review_available=True,
            description_coverage={"validated_count": 2, "fallback_count": 1, "total_candidates": 3},
            reused_source_count=2,
            collecting_source_count=3,
            pipeline_path=pipeline_path,
        )

        assert "Новых книг" not in message
        assert "Обновлено книг" not in message
        assert "Описаний на русском" not in message
        assert "Требуют проверки: 1." not in message
        if pipeline_path is PipelinePath.FEEDBACK_APPLIED:
            assert "предыдущего запуска" in message
        # Issue #557: attention text names this run's unresolved facts; the
        # fallback count alone no longer produces the descriptions line.
        assert "не была проверена" in message
        assert "Обзор готов" in message

    def test_feedback_finalization_is_reported_as_feedback(self):
        """Criterion 6: finalizing feedback is not a weekly scout."""
        from autoscout.russian_workflow import completion_message

        message = completion_message(
            outcome="completed/no-attention",
            new_count=3,
            updated_count=33,
            pipeline_path=PipelinePath.FEEDBACK_APPLIED,
        )

        assert "решени" in message.casefold()
        assert "Еженедельный поиск книг завершён" not in message
        assert "Новый поиск не выполнялся" in message
        assert "Новых книг" not in message

    def test_scouted_run_with_feedback_reports_both_effects(self):
        from autoscout.russian_workflow import completion_message

        message = completion_message(
            outcome="completed/no-attention",
            new_count=3,
            updated_count=33,
            pipeline_path=PipelinePath.SCOUTED_WITH_FEEDBACK,
        )

        assert "решени" in message.casefold()
        assert "Новых книг: 3." in message

    def test_a_scouted_run_reports_its_counts_as_before(self):
        from autoscout.russian_workflow import completion_message

        message = completion_message(
            outcome="completed/no-attention",
            new_count=3,
            updated_count=33,
            pipeline_path=PipelinePath.SCOUTED,
        )

        assert "Новых книг: 3." in message
        assert "Обновлено книг: 33." in message

    def test_an_unreported_path_reports_counts_as_before(self):
        """Callers that state no path keep the existing message exactly."""
        from autoscout.russian_workflow import completion_message

        message = completion_message(outcome="completed/no-attention", new_count=3, updated_count=33)

        assert "Новых книг: 3." in message

    def test_the_workflow_log_records_the_path(self, tmp_path: Path):
        """Driven through the real weekly workflow, with no stage stubbed out.

        The 2026-08-05 log showed `pipeline ok` in zero seconds and nothing
        else. Reading it afterwards could not tell a scout from a no-op.
        """
        import sys

        sys.path.insert(0, str(Path(__file__).parent))
        from test_weekly_workflow import _write_capture_fixture
        from test_weekly_workflow import _write_codex_fixture_config
        from test_weekly_workflow import _write_extraction_fixture

        from autoscout.weekly_workflow import deterministic_adapters
        from autoscout.weekly_workflow import run_weekly_workflow

        config_path = _write_codex_fixture_config(tmp_path)
        output_dir = tmp_path / "run"
        output_dir.mkdir()
        _write_capture_fixture(tmp_path, output_dir)
        adapters = deterministic_adapters()
        adapters.capture.prepare(config_path, output_dir, live=False)
        _write_extraction_fixture(output_dir)

        result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

        pipeline_entries = [entry for entry in result.log if entry.get("stage") == "pipeline"]
        assert pipeline_entries, "the pipeline stage must be logged"
        assert pipeline_entries[-1]["detail"]["pipeline_path"] is PipelinePath.SCOUTED

    def test_same_date_retry_does_not_report_completed_weekly_result(self, tmp_path: Path):
        """Drive fresh capture plus a feedback no-op through the full workflow."""
        import sys

        sys.path.insert(0, str(Path(__file__).parent))
        from test_weekly_workflow import _write_capture_fixture
        from test_weekly_workflow import _write_codex_fixture_config
        from test_weekly_workflow import _write_extraction_fixture

        from autoscout.weekly_workflow import deterministic_adapters
        from autoscout.weekly_workflow import run_weekly_workflow

        config_path = _write_codex_fixture_config(tmp_path)
        output_dir = tmp_path / "run"
        output_dir.mkdir()
        _write_capture_fixture(tmp_path, output_dir)
        adapters = deterministic_adapters()
        adapters.enrichment = None
        # Issue #640: this retry pins the pipeline noop path. The new
        # adjudication stage would otherwise mutate history between runs.
        adapters.adjudication = None
        adapters.capture.prepare(config_path, output_dir, live=False)
        _write_extraction_fixture(output_dir)

        first = run_weekly_workflow(config_path, output_dir, output_dir, adapters=adapters)
        assert first.outcome.value != "intervention-required"

        retry_adapters = deterministic_adapters()
        retry_adapters.enrichment = None
        retry_adapters.adjudication = None
        second = run_weekly_workflow(config_path, output_dir, output_dir, adapters=retry_adapters)

        pipeline_entries = [
            entry
            for entry in second.log
            if entry.get("stage") == "pipeline"
            and isinstance(entry.get("detail"), dict)
            and entry["detail"].get("pipeline_path")
        ]
        assert pipeline_entries[-1]["detail"]["pipeline_path"] is PipelinePath.FEEDBACK_NOOP
        assert second.outcome.value == "intervention-required"
        assert second.failed_stage == "pipeline"
        assert second.pipeline_path is PipelinePath.FEEDBACK_NOOP

        finalize_entries = [entry for entry in second.log if entry.get("stage") == "finalize"]
        assert finalize_entries[-1]["detail"]["fresh_evidence"] is True

        source_processing = [entry for entry in second.log if entry.get("stage") == "source-processing"]
        assert source_processing[-1]["status"] == "ok"
        pipeline_failures = [
            entry for entry in second.log if entry.get("stage") == "pipeline" and entry.get("status") == "failed"
        ]
        assert pipeline_failures[-1]["reason"] == (
            "pipeline scouted nothing after fresh capture and extraction; the stored report belongs to an earlier run"
        )

        editor_message = render_workflow_result(second, include_progress=False)
        assert "Еженедельный поиск книг завершён" not in editor_message
        assert "Новых книг" not in editor_message
        assert "Обновлено книг" not in editor_message

    def test_invalid_pipeline_path_records_a_specific_reason(self, tmp_path: Path, monkeypatch):
        import sys

        sys.path.insert(0, str(Path(__file__).parent))
        from test_weekly_workflow import _write_capture_fixture
        from test_weekly_workflow import _write_codex_fixture_config
        from test_weekly_workflow import _write_extraction_fixture

        from autoscout.weekly_workflow import deterministic_adapters
        from autoscout.weekly_workflow import run_weekly_workflow

        config_path = _write_codex_fixture_config(tmp_path)
        output_dir = tmp_path / "run"
        output_dir.mkdir()
        _write_capture_fixture(tmp_path, output_dir)
        adapters = deterministic_adapters()
        adapters.capture.prepare(config_path, output_dir, live=False)
        _write_extraction_fixture(output_dir)
        monkeypatch.setattr(
            "autoscout.weekly_workflow._run_pipeline_stage",
            lambda *args, **kwargs: {"returncode": 0, "pipeline_path": "unknown"},
        )

        result = run_weekly_workflow(config_path, output_dir, output_dir, adapters=adapters)

        pipeline_failures = [entry for entry in result.log if entry.get("stage") == "pipeline"]
        assert pipeline_failures[-1]["status"] == "failed"
        assert pipeline_failures[-1]["reason"] == "pipeline did not report a valid path"


class StampTruthTableTests:
    """Every state of the guard, pinned.

    This rule has been wrong three times, each time by admitting one more state
    than it should: first the date alone, then a stamp only one side carried,
    then two matching operation ids. Each was a narrow hole that looked closed
    from the two conditions in the code. The table is therefore asserted whole
    rather than sampled.
    """

    # One stamp is in exactly one of five states. NEITHER is the pre-stamping
    # world; ONLY_STORED and ONLY_CALLER mean one side can say nothing about the
    # other; AGREE and DIFFER are the two answered cases.
    NEITHER, ONLY_STORED, ONLY_CALLER, AGREE, DIFFER = "neither", "only-stored", "only-caller", "agree", "differ"
    STATES = (NEITHER, ONLY_STORED, ONLY_CALLER, AGREE, DIFFER)

    # Stated independently of the implementation: the short circuit is permitted
    # only when nothing is stamped at all, or when the evidence digest agrees
    # and the operation stamp does not itself disagree. A matching operation
    # stamp never permits anything on its own, because every same-date run has
    # the same one.
    PERMITTED_COMBINATIONS: ClassVar[set[tuple[str, str]]] = {
        (NEITHER, NEITHER),
        (NEITHER, AGREE),
        (AGREE, AGREE),
    }

    @staticmethod
    def _values(state: str, present: str, other: str) -> tuple[str, str]:
        return {
            "neither": ("", ""),
            "only-stored": ("", present),
            "only-caller": (present, ""),
            "agree": (present, present),
            "differ": (present, other),
        }[state]

    @pytest.mark.parametrize("evidence_state", STATES)
    @pytest.mark.parametrize("operation_state", STATES)
    def test_every_combination_of_stamp_states(self, operation_state, evidence_state):
        from autoscout.pipeline_identity import RunIdentity
        from autoscout.pipeline_identity import stamps_permit_short_circuit

        operation_id, stored_operation_id = self._values(operation_state, "OP", "OTHER-OP")
        evidence_digest, stored_evidence_digest = self._values(evidence_state, "E1", "E2")

        permitted = stamps_permit_short_circuit(
            RunIdentity(operation_id=operation_id, evidence_digest=evidence_digest),
            RunIdentity(operation_id=stored_operation_id, evidence_digest=stored_evidence_digest),
        )

        assert permitted is ((operation_state, evidence_state) in self.PERMITTED_COMBINATIONS)


class SameOperationDifferentEvidenceTests:
    """The 2026-08-05 failure exactly: one date, one operation, new captures.

    Both runs carried operation id f4245698…, because it is a hash of the date
    and the portfolio. Only the captures differed, in 20 of 28 sources.
    """

    def _config(self, tmp_path: Path, *, evidence_digest: str, title: str) -> Path:
        return _write_config(
            tmp_path,
            filename=f"config-{evidence_digest}.json",
            sources=[_source(title=title)],
            operation_id=OPERATION_ID,
            evidence_digest=evidence_digest,
        )

    def test_new_captures_under_one_operation_still_rescout(self, tmp_path: Path):
        from autoscout.pipeline import run_pipeline

        output_dir = tmp_path / "run"
        run_pipeline(self._config(tmp_path, evidence_digest="first-fetch", title="Book One"), output_dir, output_dir)
        assert _titles(output_dir) == {"Book One"}

        run_pipeline(self._config(tmp_path, evidence_digest="second-fetch", title="Book Two"), output_dir, output_dir)

        assert "Book Two" in _titles(output_dir)

    def test_unstamped_stored_artifacts_cannot_satisfy_a_stamped_run(self, tmp_path: Path):
        """The state the production run directory is actually in right now.

        Its run-summary.json predates evidence stamping, so it records no
        digest. A stamped run reaching that directory can prove nothing about
        whether those artifacts came from its own evidence, and silently
        assuming they did is the exact bug this issue is about. Unprovable is
        treated as different.
        """
        from autoscout.pipeline import run_pipeline

        output_dir = tmp_path / "run"
        run_pipeline(self._config(tmp_path, evidence_digest="first-fetch", title="Book One"), output_dir, output_dir)

        # Strip the stamps, as an older code version would have left them.
        summary_path = output_dir / "run-summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary.pop("evidence_digest", None)
        summary.pop("operation_id", None)
        summary_path.write_text(json.dumps(summary), encoding="utf-8")

        run_pipeline(self._config(tmp_path, evidence_digest="second-fetch", title="Book Two"), output_dir, output_dir)

        assert "Book Two" in _titles(output_dir)

    def test_legacy_report_with_feedback_reports_full_run_and_feedback(self, tmp_path: Path):
        from autoscout.pipeline import run_pipeline

        output_dir = tmp_path / "run"
        config_path = self._config(tmp_path, evidence_digest="first-fetch", title="Book One")
        run_pipeline(config_path, output_dir, output_dir)
        candidate = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))[0]

        pending = [
            {
                "identity_key": candidate["identity_key"],
                "Editor Status": "reject",
                "Editor Note": "Reject this book.",
            }
        ]
        seed_state(output_dir, pending_editor_rows=pending)
        summary_path = output_dir / "run-summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary.pop("operation_id", None)
        summary.pop("evidence_digest", None)
        summary_path.write_text(json.dumps(summary), encoding="utf-8")

        result = run_pipeline(config_path, output_dir, output_dir)

        assert (result or {}).get("pipeline_path") == "scouted-with-feedback"

    def test_a_matching_operation_cannot_vouch_for_unknown_evidence(self, tmp_path: Path):
        """A partial stamp must not fail open.

        The operation id is the weaker of the two and matches across every
        same-date run by construction. Letting it alone satisfy the guard, while
        the stored artifacts record evidence this caller says nothing about,
        restores exactly the hole the digest was added to close.
        """
        from autoscout.pipeline import run_pipeline

        output_dir = tmp_path / "run"
        run_pipeline(self._config(tmp_path, evidence_digest="first-fetch", title="Book One"), output_dir, output_dir)

        # Same operation, evidence unstated. Nothing is known about whether the
        # stored candidates came from what this caller would scout.
        path = _write_config(
            tmp_path,
            filename="config-unstated.json",
            sources=[_source(title="Book Two")],
            operation_id=OPERATION_ID,
        )

        run_pipeline(path, output_dir, output_dir)

        assert "Book Two" in _titles(output_dir)

    def test_an_operation_stamp_without_a_digest_cannot_short_circuit(self, tmp_path: Path):
        """A stamped caller must be answered on evidence, or not at all.

        The operation id is identical for every same-date run by construction,
        so two matching operation ids and no digest on either side say nothing
        about whether the evidence changed. Accepting that as agreement is the
        original bug with an extra step: the guard looks satisfied while the
        only question that matters was never asked.

        The presence of an operation stamp marks a caller that knows about
        stamping. Such a caller either produces a digest or gets re-scouted.
        """
        from autoscout.pipeline import run_pipeline

        def _operation_only(title: str) -> Path:
            return _write_config(
                tmp_path,
                filename=f"operation-only-{title}.json",
                sources=[_source(title=title)],
                operation_id=OPERATION_ID,
            )

        output_dir = tmp_path / "run"
        run_pipeline(_operation_only("Book One"), output_dir, output_dir)

        run_pipeline(_operation_only("Book Two"), output_dir, output_dir)

        assert "Book Two" in _titles(output_dir)

    def test_the_same_captures_stay_on_the_feedback_path(self, tmp_path: Path):
        from autoscout.pipeline import run_pipeline

        output_dir = tmp_path / "run"
        run_pipeline(self._config(tmp_path, evidence_digest="only-fetch", title="Book One"), output_dir, output_dir)

        # A genuine re-invocation: the same config over the same files.
        repeat = run_pipeline(
            self._config(tmp_path, evidence_digest="only-fetch", title="Book One"), output_dir, output_dir
        )

        assert (repeat or {}).get("pipeline_path") == "feedback-noop"
        assert _titles(output_dir) == {"Book One"}


def _second_source_config(
    tmp_path: Path, *, operation_id: str | None, name: str, evidence_digest: str = "one-fetch"
) -> Path:
    """A config whose evidence differs, so a re-scout is observable."""
    return _write_config(
        tmp_path,
        filename=f"config-{name}.json",
        sources=[_source(title=name, author="Blake Second")],
        operation_id=operation_id,
        evidence_digest=evidence_digest if operation_id is not None else None,
    )


def _titles(output_dir: Path) -> set[str]:
    candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
    return {str(candidate.get("title", "")) for candidate in candidates}


class SecondOperationRescoutsTests:
    """The 2026-08-05 failure, reduced to its smallest reproduction."""

    def test_a_second_operation_on_the_same_date_rescouts(self, tmp_path: Path):
        from autoscout.pipeline import run_pipeline

        output_dir = tmp_path / "run"
        run_pipeline(_scout_config(tmp_path, operation_id=EARLIER_OPERATION_ID), output_dir, state_dir=output_dir)
        assert _titles(output_dir) == {"Book One"}

        run_pipeline(
            _second_source_config(tmp_path, operation_id=OPERATION_ID, name="Book Two"),
            output_dir,
            state_dir=output_dir,
        )

        # The second operation's own evidence must reach the report, rather
        # than the first operation's candidates being served again.
        assert "Book Two" in _titles(output_dir)
        summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
        assert summary["operation_id"] == OPERATION_ID

    def test_re_invoking_the_same_operation_still_short_circuits(self, tmp_path: Path):
        """The feedback path must survive. It is why the guard exists at all."""
        from autoscout.pipeline import run_pipeline

        output_dir = tmp_path / "run"
        run_pipeline(_scout_config(tmp_path, operation_id=OPERATION_ID), output_dir, state_dir=output_dir)

        # The same config again: nothing about what this run would scout has
        # changed, so the feedback path is the right answer.
        run_pipeline(_scout_config(tmp_path, operation_id=OPERATION_ID), output_dir, state_dir=output_dir)

        assert _titles(output_dir) == {"Book One"}

    def test_the_pipeline_reports_which_path_it_took(self, tmp_path: Path):
        """Defence in depth: a zero-second stage must be readable afterwards.

        The operation guard stops this particular no-op, but a no-op reaching
        the workflow indistinguishable from a completed scout is the fault that
        let the 2026-08-05 report through. The caller has to be able to tell.
        """
        from autoscout.pipeline import run_pipeline

        output_dir = tmp_path / "run"
        first = run_pipeline(_scout_config(tmp_path, operation_id=OPERATION_ID), output_dir, state_dir=output_dir)
        assert (first or {}).get("pipeline_path") == "scouted"

        repeat = run_pipeline(_scout_config(tmp_path, operation_id=OPERATION_ID), output_dir, state_dir=output_dir)
        assert (repeat or {}).get("pipeline_path") == "feedback-noop"

    def test_an_unstamped_config_keeps_the_older_date_behaviour(self, tmp_path: Path):
        """Callers that never carried an operation are untouched."""
        from autoscout.pipeline import run_pipeline

        output_dir = tmp_path / "run"
        run_pipeline(_scout_config(tmp_path, operation_id=None), output_dir, state_dir=output_dir)

        run_pipeline(_scout_config(tmp_path, operation_id=None), output_dir, state_dir=output_dir)

        assert _titles(output_dir) == {"Book One"}


class DigestReflectsWhatThisRunWillConsumeTests:
    """A digest declared earlier is a claim; the files are the fact.

    ``finalize-codex-run`` computes the digest and bakes it into the config, but
    the operator workflow documents saving extraction artifacts and re-running
    finalization, so artifacts routinely change after a config exists. A run
    that trusts the declared value cannot notice, and the evidence it actually
    reads is not the evidence it claims to have.
    """

    def _recorded_config(self, tmp_path: Path, *, pilot_row_max: int = 50) -> Path:
        # What finalize-codex-run baked in when the config was written. It is
        # deliberately never updated here, because the declared digest goes
        # stale when an artifact changes under it.
        return _write_config(
            tmp_path,
            filename="replay-config.json",
            sources=[
                _source(
                    extraction_method="recorded",
                    recorded_mentions_file="recorded-mentions.json",
                )
            ],
            operation_id=OPERATION_ID,
            evidence_digest="digest-declared-at-finalize-time",
            extra={"pilot_mode": True, "pilot_row_max": pilot_row_max},
        )

    def _write_mentions(self, tmp_path: Path, titles: list[str]) -> None:
        (tmp_path / "recorded-mentions.json").write_text(
            json.dumps(
                [
                    {
                        "title": title,
                        "primary_authors": ["Alex Example"],
                        "source_url": SOURCE_URL,
                        "evidence_url": SOURCE_URL,
                        "source_check_date": "2026-08-05",
                        "signal_type": "media/curated list",
                        "signal_strength": "major media list",
                        "evidence_note": "Listed among the year's best.",
                    }
                    for title in titles
                ]
            ),
            encoding="utf-8",
        )

    def test_russian_check_file_changes_are_part_of_the_digest(self, tmp_path: Path):
        from autoscout.pipeline import run_evidence_digest

        config = {
            "run_date": "2026-08-05",
            "sources": [],
            "russian_edition_checks": [
                {
                    "retailer": "LitRes",
                    "recorded_results_file": "russian-results.json",
                }
            ],
        }
        config_path = tmp_path / "config.json"
        results_path = tmp_path / "russian-results.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        results_path.write_text(json.dumps([{"candidate_title": "Book One"}]), encoding="utf-8")
        first = run_evidence_digest(config, config_path)

        results_path.write_text(json.dumps([{"candidate_title": "Book Two"}]), encoding="utf-8")

        assert run_evidence_digest(config, config_path) != first

    def test_recorded_source_file_uses_the_collector_path(self, tmp_path: Path):
        from autoscout.pipeline import run_evidence_digest

        config = {
            "run_date": "2026-08-05",
            "sources": [
                {
                    "name": SOURCE_NAME,
                    "family": "media/curated list",
                    "url": SOURCE_URL,
                    "extraction_method": "recorded",
                    "codex_artifact_dir": "codex-artifacts",
                    "recorded_mentions_file": "recorded-mentions.json",
                }
            ],
        }
        config_path = tmp_path / "config.json"
        recorded_path = tmp_path / "recorded-mentions.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        recorded_path.write_text(json.dumps([{"title": "Book One"}]), encoding="utf-8")
        first = run_evidence_digest(config, config_path)

        recorded_path.write_text(json.dumps([{"title": "Book Two"}]), encoding="utf-8")

        assert run_evidence_digest(config, config_path) != first

    def test_effective_date_is_part_of_the_digest(self, tmp_path: Path):
        from autoscout.pipeline import run_evidence_digest

        config = {"run_date": "2026-08-05", "sources": []}
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")

        first = run_evidence_digest(config, config_path, effective_date="2026-08-05")
        second = run_evidence_digest(config, config_path, effective_date="2026-08-06")

        assert first != second

    def test_an_artifact_changed_after_finalization_is_noticed(self, tmp_path: Path):
        """The reported reproduction: a changed recorded-mentions file."""
        from autoscout.pipeline import run_pipeline

        output_dir = tmp_path / "run"
        config_path = self._recorded_config(tmp_path)
        self._write_mentions(tmp_path, ["Book One"])
        run_pipeline(config_path, output_dir, output_dir)
        assert _titles(output_dir) == {"Book One"}

        # The config is untouched, so its declared digest is now a stale claim.
        self._write_mentions(tmp_path, ["Book One", "Book Two"])
        run_pipeline(config_path, output_dir, output_dir)

        assert "Book Two" in _titles(output_dir)

    def test_output_affecting_configuration_is_part_of_the_identity(self, tmp_path: Path):
        """Same evidence, different report. The run still has work to do."""
        from autoscout.pipeline import run_pipeline

        output_dir = tmp_path / "run"
        self._write_mentions(tmp_path, ["Book One"])
        run_pipeline(self._recorded_config(tmp_path, pilot_row_max=50), output_dir, output_dir)

        repeat = run_pipeline(self._recorded_config(tmp_path, pilot_row_max=5), output_dir, output_dir)

        assert (repeat or {}).get("pipeline_path") == "scouted"

    def test_changed_feedback_config_override_is_not_short_circuited(self, tmp_path: Path):
        from autoscout.pipeline import run_pipeline

        output_dir = tmp_path / "run"
        config_path = self._recorded_config(tmp_path)
        self._write_mentions(tmp_path, ["Book One"])
        run_pipeline(config_path, output_dir, output_dir)
        candidate = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))[0]

        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["returned_editor_rows"] = [
            {
                "identity_key": candidate["identity_key"],
                "Editor Status": "reject",
                "Editor Note": "Reject this book.",
            }
        ]
        config_path.write_text(json.dumps(config), encoding="utf-8")

        result = run_pipeline(config_path, output_dir, output_dir)

        assert (result or {}).get("pipeline_path") == "scouted"
        updated = read_state(output_dir, "candidate_history")[0]
        assert updated["editor_status"] == "reject"

    def test_an_unchanged_run_still_reaches_the_feedback_path(self, tmp_path: Path):
        """The guard must still let a genuine re-invocation through."""
        from autoscout.pipeline import run_pipeline

        output_dir = tmp_path / "run"
        config_path = self._recorded_config(tmp_path)
        self._write_mentions(tmp_path, ["Book One"])
        run_pipeline(config_path, output_dir, output_dir)

        repeat = run_pipeline(config_path, output_dir, output_dir)

        assert (repeat or {}).get("pipeline_path") == "feedback-noop"
