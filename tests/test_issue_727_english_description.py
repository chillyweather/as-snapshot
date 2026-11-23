"""Issue #727: a machine-facing English Description beside the Russian one."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from state_store_support import seed_state

from autoscout.content_description import ENGLISH_DESCRIPTION_FIELD
from autoscout.content_description import apply_description_artifacts
from autoscout.content_description import current_english_description
from autoscout.content_description import description_provenance_record
from autoscout.content_description import validate_english_description
from autoscout.description_adjudication import ADJUDICATION_RESPONSE_SCHEMA
from autoscout.description_adjudication import _bounded_response
from autoscout.description_adjudication import plan_adjudication_application
from autoscout.description_adjudication import validate_adjudication_response
from autoscout.description_enrichment import _sanitize_agent_response
from autoscout.grounded_description import OUTCOME_GROUNDED
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import StageArtifact
from autoscout.grounded_description import SynthesisResult
from autoscout.html_review import build_html_review
from autoscout.json_io import write_json
from autoscout.review_rendering import assemble_cumulative_review
from autoscout.review_rendering import render_review
from autoscout.weekly_report_row import write_editor_csv

IDENTITY_KEY = "work 1|alex author"

RUSSIAN = (
    "Книга прослеживает, как миграция меняет семейную память на протяжении трёх поколений. "
    "Автор соединяет архивные письма с интервью и показывает, как забытые истории возвращаются в семью. "
    "Редакционный крючок — личная история, которая объясняет большой исторический сдвиг."
)
ENGLISH = (
    "The book traces how migration reshapes family memory across three generations. "
    "Letters from archives meet interviews, showing forgotten stories returning to a family. "
    "Its hook is a personal story that explains a large historical shift."
)


def _history_record(**extra: Any) -> dict[str, Any]:
    return {
        "identity_key": IDENTITY_KEY,
        "candidate_id": "candidate-001",
        "title": "Work 1",
        "primary_authors": ["Alex Author"],
        "content_description": "",
        **extra,
    }


# -- validation -------------------------------------------------------------


def test_english_prose_passes_and_is_whitespace_normalized() -> None:
    assert validate_english_description(f"  {ENGLISH}\n") == (ENGLISH, None)


def test_non_english_and_unsafe_values_are_rejected() -> None:
    assert validate_english_description(RUSSIAN)[0] is None
    assert validate_english_description("")[0] is None
    assert validate_english_description(None)[0] is None
    assert validate_english_description("word " * 500)[0] is None
    copied = "From the publisher: a gripping story. Advance praise from Booklist for this remarkable debut."
    assert validate_english_description(copied)[0] is None


# -- storage beside the Russian text ----------------------------------------


def test_provenance_record_stores_english_only_when_given() -> None:
    with_english = description_provenance_record(IDENTITY_KEY, RUSSIAN, "grounded", english_description=ENGLISH)
    without = description_provenance_record(IDENTITY_KEY, RUSSIAN, "grounded")
    assert with_english[ENGLISH_DESCRIPTION_FIELD] == ENGLISH
    assert ENGLISH_DESCRIPTION_FIELD not in without


def test_english_reads_as_absent_once_the_russian_text_changes() -> None:
    provenance = description_provenance_record(IDENTITY_KEY, RUSSIAN, "grounded", english_description=ENGLISH)
    record = _history_record(content_description=RUSSIAN, description_provenance=provenance)
    assert current_english_description(record) == ENGLISH

    edited = {**record, "content_description": "Редактор переписал описание этой книги целиком."}
    assert current_english_description(edited) == ""
    assert current_english_description(_history_record(content_description=RUSSIAN)) == ""


# -- the shared apply gate (adjudication and Codex enrichment) --------------


def test_apply_gate_stores_valid_english_with_the_russian() -> None:
    history, _ = apply_description_artifacts(
        [_history_record()],
        [{"identity_key": IDENTITY_KEY, "content_description": RUSSIAN, "english_description": ENGLISH}],
        provenance="codex-generated",
        effective_date="2026-09-25",
    )
    assert history[0]["content_description"] == RUSSIAN
    assert current_english_description(history[0]) == ENGLISH


def test_apply_gate_drops_invalid_english_and_applies_russian_unchanged() -> None:
    baseline, _ = apply_description_artifacts(
        [_history_record()],
        [{"identity_key": IDENTITY_KEY, "content_description": RUSSIAN}],
        provenance="codex-generated",
        effective_date="2026-09-25",
    )
    with_bad_english, _ = apply_description_artifacts(
        [_history_record()],
        [{"identity_key": IDENTITY_KEY, "content_description": RUSSIAN, "english_description": RUSSIAN}],
        provenance="codex-generated",
        effective_date="2026-09-25",
    )
    assert with_bad_english == baseline
    assert ENGLISH_DESCRIPTION_FIELD not in with_bad_english[0]["description_provenance"]


def test_codex_enrichment_response_carries_english_only_with_a_description() -> None:
    sanitized = _sanitize_agent_response(
        {
            IDENTITY_KEY: {"content_description": RUSSIAN, "english_description": ENGLISH},
            "work 2|alex author": {"content_description": "", "english_description": ENGLISH},
        },
        {IDENTITY_KEY, "work 2|alex author"},
    )
    assert sanitized[IDENTITY_KEY]["english_description"] == ENGLISH
    assert "work 2|alex author" not in sanitized


# -- adjudication contract --------------------------------------------------


def _adjudication_response(**extra: Any) -> dict[str, Any]:
    return {
        "identity_key": IDENTITY_KEY,
        "resolved": True,
        "unresolved_reason": "",
        "confidence": "high",
        "content_description": RUSSIAN,
        "citations": [],
        **extra,
    }


def test_adjudication_schema_requires_english_but_the_validator_keeps_it_optional() -> None:
    assert "english_description" in ADJUDICATION_RESPONSE_SCHEMA["required"]
    assert validate_adjudication_response(_adjudication_response()) == (True, "")
    assert validate_adjudication_response(_adjudication_response(english_description=ENGLISH)) == (True, "")


def test_a_malformed_english_value_never_rejects_the_russian_response() -> None:
    for malformed in (None, 3, {"text": ENGLISH}, [ENGLISH]):
        response = _adjudication_response(english_description=malformed)
        assert validate_adjudication_response(response) == (True, ""), malformed
        bounded = _bounded_response(response, identity_key=IDENTITY_KEY)
        assert "english_description" not in bounded, malformed
        assert bounded["content_description"] == response["content_description"]


def test_accepted_adjudication_plan_carries_english_to_the_apply_gate() -> None:
    plan = plan_adjudication_application(
        _adjudication_response(english_description=ENGLISH),
        {
            "resolved": True,
            "owner_count": 2,
            "counting_urls": ["https://one.example.test/work", "https://two.example.test/work"],
            "counting_digests": ["a" * 64, "b" * 64],
        },
        identity_matched=True,
        candidate_id="candidate-001",
    )
    assert plan["artifact"]["english_description"] == ENGLISH
    history, _ = apply_description_artifacts(
        [_history_record()],
        [plan["artifact"]],
        provenance="agent-adjudicated",
        effective_date="2026-09-25",
    )
    assert current_english_description(history[0]) == ENGLISH


# -- grounded stage ---------------------------------------------------------


class _Discovery:
    def discover(self, candidate: Any, **_kwargs: Any) -> list[Any]:
        return [
            {
                "url": f"https://{owner}.example.test/work",
                "source_kind": owner,
                "source_family": owner,
                "owner_id": owner,
                "classification": "classified",
            }
            for owner in ("review", "publisher")
        ]


SOURCE_PROSE = (
    "Across three generations this moving account follows letters kept in a shoebox and the "
    "grandmother who refused to speak about the crossing until the very end of her life"
)


class _Fetch:
    def fetch(self, result: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "url": result.url,
            "source_kind": result.source_kind,
            "source_family": result.source_family,
            "owner_id": result.owner_id,
            "source_language": "en",
            "title": "Work 1",
            "primary_authors": ["Alex Author"],
            "subject_text": SOURCE_PROSE,
            "subject_key": "memory migration",
            "checked_date": "2026-09-01",
        }


class _Synthesizer:
    def __init__(self, english: str) -> None:
        self.english = english

    def synthesize(self, candidate: Any, sources: Any) -> SynthesisResult:
        return SynthesisResult(RUSSIAN, english_description=self.english)


class _Clock:
    def __call__(self) -> float:
        return 0.0


def _run_stage(english: str) -> StageArtifact:
    return GroundedDescriptionStage(
        discovery=_Discovery(),
        fetch=_Fetch(),
        synthesizer=_Synthesizer(english),
        monotonic=_Clock(),
    ).run(
        [_history_record(update_delta="new", priority_label="D", first_seen_date="2026-09-01")],
        operation_id="op",
        operation_date="2026-09-01",
    )


def test_grounded_stage_keeps_english_in_the_provenance_and_round_trips() -> None:
    artifact = _run_stage(ENGLISH)
    outcome = artifact.outcomes[0]
    assert outcome.outcome == OUTCOME_GROUNDED
    assert outcome.content_description == RUSSIAN
    serialized = outcome.to_dict()
    assert serialized["description_provenance"][ENGLISH_DESCRIPTION_FIELD] == ENGLISH
    assert ENGLISH_DESCRIPTION_FIELD not in serialized

    restored = StageArtifact.from_mapping(json.loads(json.dumps(artifact.to_dict())))
    assert restored.outcomes[0].english_description == ENGLISH


def test_grounded_stage_drops_english_copied_from_source_but_keeps_the_russian() -> None:
    copied = f"{SOURCE_PROSE}. It is a family history told through letters."
    outcome = _run_stage(copied).outcomes[0]
    assert outcome.outcome == OUTCOME_GROUNDED
    assert outcome.content_description == RUSSIAN
    assert outcome.english_description == ""
    assert ENGLISH_DESCRIPTION_FIELD not in outcome.to_dict()["description_provenance"]


def test_a_hand_edited_invalid_english_value_is_dropped_on_load() -> None:
    raw = _run_stage(ENGLISH).to_dict()
    raw["outcomes"][0]["description_provenance"][ENGLISH_DESCRIPTION_FIELD] = RUSSIAN
    assert StageArtifact.from_mapping(raw).outcomes[0].english_description == ""


def test_english_is_dropped_on_load_when_the_outcome_russian_changed() -> None:
    raw = _run_stage(ENGLISH).to_dict()
    assert StageArtifact.from_mapping(raw).outcomes[0].english_description == ENGLISH
    raw["outcomes"][0]["content_description"] = RUSSIAN + " Дополнение."
    assert StageArtifact.from_mapping(raw).outcomes[0].english_description == ""


# -- never editor-facing ----------------------------------------------------


def test_editor_facing_review_never_shows_the_english(tmp_path: Path) -> None:
    provenance = description_provenance_record(
        IDENTITY_KEY,
        RUSSIAN,
        "grounded",
        evidence_basis=["https://review.example.test/work", "https://publisher.example.test/work"],
        english_description=ENGLISH,
    )
    candidate = {
        **_history_record(content_description=RUSSIAN, description_provenance=provenance),
        "update_delta": "new",
        "priority_label": "C",
        "first_seen_date": "2026-09-01",
    }
    rows, summary = assemble_cumulative_review([candidate], [dict(candidate)], [])
    assert RUSSIAN in json.dumps(rows, ensure_ascii=False)
    for row in rows:
        for key, value in row.items():
            if isinstance(value, str):
                assert ENGLISH not in value, key
        assert ENGLISH not in json.dumps(row["editor_row"], ensure_ascii=False)

    html = build_html_review(rows, {"total_count": 1, "cumulative_counts": summary})
    assert ENGLISH not in html

    csv_path = tmp_path / "review.csv"
    write_editor_csv(csv_path, [row["editor_row"] for row in rows])
    with csv_path.open(encoding="utf-8", newline="") as handle:
        assert ENGLISH not in handle.read()
    with csv_path.open(encoding="utf-8", newline="") as handle:
        assert list(csv.DictReader(handle))


def test_rendered_markdown_and_html_reviews_never_show_the_english(tmp_path: Path) -> None:
    provenance = description_provenance_record(
        IDENTITY_KEY,
        RUSSIAN,
        "grounded",
        evidence_basis=["https://review.example.test/work", "https://publisher.example.test/work"],
        english_description=ENGLISH,
    )
    candidate = {
        **_history_record(content_description=RUSSIAN, description_provenance=provenance),
        "update_delta": "new",
        "priority_label": "C",
        "first_seen_date": "2026-09-01",
    }
    write_json(tmp_path / "scouting-candidates.json", [candidate])
    seed_state(tmp_path, candidate_history=[dict(candidate)])
    write_json(tmp_path / "run-summary.json", {"run_date": "2026-09-01", "editor_facing_count": 1})

    assert render_review(tmp_path) == 0
    for name in ("weekly-review.md", "weekly-review.html"):
        review = (tmp_path / name).read_text(encoding="utf-8")
        assert "Work 1" in review, name
        assert ENGLISH not in review, name
