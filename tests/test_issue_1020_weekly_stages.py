"""The weekly run's stages and their order (issue #1020).

Adding, removing or reordering a stage changes this list, so it is a
deliberate act reviewed here rather than a side effect of an edit elsewhere.
"""

from __future__ import annotations

from autoscout.weekly_workflow import WeeklyWorkflow
from autoscout.weekly_workflow import deterministic_adapters


def test_the_weekly_run_has_these_stages_in_this_order() -> None:
    stages = WeeklyWorkflow(deterministic_adapters())._stages()

    assert [stage.__name__ for stage in stages] == [
        "_check_retry_evidence",
        "_update_repository",
        "_start_operation",
        "_check_toolchain",
        "_start_kirkus_pass",
        "_run_pm_pass",
        "_run_pw_pass",
        "_capture",
        "_extract_main_batch",
        "_join_kirkus_pass",
        "_record_source_processing",
        "_finalize",
        "_run_pipeline",
        "_complete_isbns",
        "_enrich_descriptions",
        "_adjudicate_descriptions",
        "_write_pm_logline_descriptions",
        "_complete_original_editions",
        "_complete_work_age",
        # Updated for #1181: the Russian-edition stage, after work age and before the refresh.
        "_complete_russian_editions",
        "_refresh_after_descriptions",
        "_score_editorial_fit",
        "_render_review",
        "_record_source_failure_streaks",
        "_check_health",
        "_record_completion_facts",
        "_decide_outcome",
        "_export_state",
        "_scan_artifacts_for_secrets",
        "_open_review",
        "_finalize_repository",
    ]
