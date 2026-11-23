"""Regression coverage for the clean editor-chat boundary (#284)."""

from types import SimpleNamespace

import pytest

from autoscout.russian_workflow import intervention_message
from autoscout.russian_workflow import render_workflow_result

FORBIDDEN_OUTPUT = (
    "MEMORY.md",
    "memory citation",
    "According to memory",
    "[memory",
    "::git-stage",
    "::git-commit",
    "::git-push",
    "::tool",
    "/Users/",
    "/home/",
    "/tmp/",
)


def _result(*, outcome="completed/no-attention", failed_stage=None, log=None):
    return SimpleNamespace(
        outcome=outcome,
        failed_stage=failed_stage,
        readiness_facts={"attention_required": False},
        delta_counts={"new": 1, "upgraded": 0, "changed": 0, "unchanged": 0},
        source_problems=[],
        review_available=True,
        description_coverage=None,
        log=log or [],
    )


def test_weekly_success_plainly_reports_proven_persistence_without_artifacts():
    message = render_workflow_result(
        _result(
            log=[
                {"stage": "repository-finalize", "status": "ok", "detail": {"commit": "secret"}},
                {"stage": "repository-finalize", "status": "noop", "detail": {"pushed": True}},
                {"stage": "open-review", "status": "ok", "detail": {"opened": True}},
            ]
        ),
        include_progress=False,
    )
    assert "Результаты сохранены." in message
    assert "Обзор готов. Открываю" in message
    assert all(artifact not in message for artifact in FORBIDDEN_OUTPUT)
    assert "secret" not in message


def test_weekly_does_not_claim_persistence_without_structured_evidence():
    message = render_workflow_result(_result(), include_progress=False)
    assert "Результаты сохранены." not in message


def test_review_open_failure_uses_friendly_action_without_path():
    message = render_workflow_result(
        _result(log=[{"stage": "open-review", "status": "failed", "detail": {"opened": False}}]),
        include_progress=False,
    )
    assert "Откройте или обновите файл обзора weekly-review.html" in message
    assert "AutoScout" not in message
    assert "/" not in message


@pytest.mark.parametrize("failed_stage", ["repository-preflight", "repository-update", "repository-finalize"])
def test_repository_failures_are_level_3_clean_output(failed_stage):
    message = render_workflow_result(
        _result(
            outcome="intervention-required",
            failed_stage=failed_stage,
            log=[
                {
                    "stage": failed_stage,
                    "status": "failed",
                    "detail": {"diagnostic": "According to memory MEMORY.md ::git-push ::tool /Users/x /home/x /tmp/x"},
                }
            ],
        ),
        include_progress=False,
    )
    assert message == intervention_message(failed_stage)
    assert message.count("Рекомендую") == 1
    assert message.count("?") == 1
    assert "связаться с Димой" in message
    assert all(artifact not in message for artifact in FORBIDDEN_OUTPUT)
