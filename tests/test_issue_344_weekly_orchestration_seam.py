from __future__ import annotations

import inspect
from dataclasses import fields

from autoscout.weekly_workflow import WeeklyWorkflowAdapters
from autoscout.weekly_workflow import deterministic_adapters


def test_dependency_bundle_exposes_only_external_variability() -> None:
    constructor = inspect.signature(WeeklyWorkflowAdapters)
    assert "finalize" not in constructor.parameters
    assert "pipeline" not in constructor.parameters
    assert "review" not in constructor.parameters
    assert "health" not in constructor.parameters

    field_names = {field.name for field in fields(WeeklyWorkflowAdapters)}
    # Stage 0 toolchain probes are external variability for deterministic tests,
    # so the bundle now has nine fields. Updated for #541: previously six,
    # now includes stage_zero_codex_probe and stage_zero_discovery_probe as
    # honest injectable seams. Updated for #640: includes the
    # description-adjudication stage adapter next to enrichment. The set is
    # literal, not computed from the code under test, so a missing seam is
    # not hidden. Updated for #725: includes editorial_fit, the advisory
    # shadow stage that calls an external vendor (TypeSafe Jev).
    # Updated for #816: includes kirkus_precollection, the slow live Kirkus pass
    # the weekly run starts before capture.
    # Updated for #946: includes pm_precollection, the live PM mailbox pass the
    # weekly run starts before capture.
    # Updated for #956: includes pw_precollection, the live PW issue pass.
    # Updated for #968: includes original_edition_completion, the live page,
    # Codex, and Open Library pass that records original editions.
    # Updated for #979: includes isbn_completion, the live page and Open
    # Library reads the weekly run makes after the pipeline.
    # Updated for #950: includes pm_logline, the model call per PM-only book
    # that restates its deal logline when the weekly config switch is on.
    # Updated for #1124: includes code_gate, the run server's ./scripts/check
    # of new code before a scheduled run touches state.
    # Updated for #1138: includes work_age_completion, the live Open Library
    # pass that records each book's first publication year and edition count.
    # Updated for #1181: includes russian_edition_completion, the live
    # LiveLib, Wikidata, State Library, LitRes and Codex pass that looks for
    # Russian editions when the weekly config switch is on.
    assert field_names == {
        "capture",
        "codex",
        "repository",
        "presentation",
        "enrichment",
        "adjudication",
        "clock",
        "editorial_fit",
        "kirkus_precollection",
        "pm_precollection",
        "pw_precollection",
        "isbn_completion",
        "pm_logline",
        "original_edition_completion",
        "work_age_completion",
        "russian_edition_completion",
        "stage_zero_codex_probe",
        "stage_zero_discovery_probe",
        "code_gate",
    }


def test_deterministic_bundle_uses_internal_stable_stages() -> None:
    adapters = deterministic_adapters()
    assert adapters.capture is not None
    assert adapters.codex is not None
    assert adapters.repository is not None
    assert adapters.presentation is not None
    assert adapters.enrichment is not None
    assert adapters.adjudication is not None
    assert adapters.clock is not None
