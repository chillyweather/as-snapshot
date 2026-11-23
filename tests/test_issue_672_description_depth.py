"""Offline coverage for issue #672: mechanical description depth.

Safety validation and depth assessment are separate verdicts. Coverage
partitions safety-valid descriptions into meets-depth and below-depth
without changing validated/fallback semantics; reviews, health, and the
Russian completion message surface depth only from artifacts that
assessed it, and never present it as literary quality.
"""

from __future__ import annotations

from autoscout.content_description import DESCRIPTION_BELOW_DEPTH_STATUS
from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.content_description import description_coverage
from autoscout.description_depth import DEPTH_MIN_CHARS
from autoscout.description_depth import DEPTH_MIN_SENTENCES
from autoscout.description_depth import DEPTH_POLICY_VERSION
from autoscout.description_depth import assess_description_depth
from autoscout.description_depth import meets_description_depth
from autoscout.description_depth import normalize_depth_text
from autoscout.description_depth import split_depth_sentences

RICH_TEXT = (
    "Книга о памяти, обществе и общественной жизни маленького города. "
    "Автор соединяет репортаж с историческим контекстом и показывает, "
    "как прошлое формирует настоящее. "
    "Редакционный крючок — тихий повседневный ракурс вместо громких обобщений."
)
THIN_TEXT = "Сборник эссе о переосмыслении привычных способов думать и смотреть на жизнь."


def test_policy_constants_are_documented() -> None:
    assert DEPTH_POLICY_VERSION == 1
    assert DEPTH_MIN_CHARS == 200
    assert DEPTH_MIN_SENTENCES == 2


def test_threshold_boundaries() -> None:
    two_sentences = "Первое предложение здесь. " + "в" * 173 + "."
    assert len(" ".join(two_sentences.split())) == 200
    assert meets_description_depth(two_sentences) is True
    one_char_short = two_sentences[:-2] + "."
    assert len(" ".join(one_char_short.split())) == 199
    assert meets_description_depth(one_char_short) is False


def test_sentence_count_boundary() -> None:
    single_long = "Книга о памяти, обществе, истории, людях и времени " + "с" * 160
    assert len(single_long) >= 200
    assert assess_description_depth(single_long)["sentence_count"] == 1
    assert meets_description_depth(single_long) is False


def test_non_string_and_blank_inputs_have_no_depth() -> None:
    for value in (None, 42, "", "   ", "\n\t "):
        assessment = assess_description_depth(value)
        assert assessment == {
            "meets_floor": False,
            "char_count": 0,
            "sentence_count": 0,
            "policy_version": DEPTH_POLICY_VERSION,
        }


def test_initials_do_not_split_sentences() -> None:
    text = "А. С. Пушкин пишет о памяти. Второе предложение здесь."
    assert split_depth_sentences(text) == [
        "А. С. Пушкин пишет о памяти",
        "Второе предложение здесь",
    ]


def test_abbreviations_do_not_split_sentences() -> None:
    text = "Он жил в г. Москва на ул. Тверская. Потом он уехал далеко и надолго."
    assert split_depth_sentences(text) == ["Он жил в г. Москва на ул. Тверская", "Потом он уехал далеко и надолго"]


def test_abbreviation_final_period_stays_live() -> None:
    assert split_depth_sentences("Он уехал т.д. И всё тут.") == ["Он уехал т.д", "И всё тут"]


def test_ellipsis_and_punctuation_clusters() -> None:
    assert split_depth_sentences("Ждал… Продолжил путь.") == ["Ждал", "Продолжил путь"]
    assert split_depth_sentences("Слово…слово без пробела.") == ["Слово…слово без пробела"]
    assert split_depth_sentences("Что случилось?! Никто не знает.") == ["Что случилось", "Никто не знает"]
    assert split_depth_sentences("Набрано 11.5 баллов. Второе предложение.") == [
        "Набрано 11.5 баллов",
        "Второе предложение",
    ]


def test_normalization_matches_safety_validator() -> None:
    assert normalize_depth_text("  Книга\tо\nпамяти  ") == "Книга о памяти"
    assert normalize_depth_text(None) == ""


# ── Coverage partition ────────────────────────────────────────────────


def _candidate(text: str, reason: str = "") -> dict[str, object]:
    record: dict[str, object] = {"identity_key": "k", "content_description": text}
    if reason:
        record["content_description_rejection_reason"] = reason
    return record  # type: ignore[return-value]


def test_coverage_partitions_validated_by_depth() -> None:
    coverage = description_coverage(
        [
            _candidate(RICH_TEXT),
            _candidate(THIN_TEXT),
            _candidate(DESCRIPTION_FALLBACK, reason="fewer_than_two_agreeing_sources"),
        ]
    )
    assert coverage["total_candidates"] == 3
    assert coverage["validated_count"] == 2
    assert coverage["fallback_count"] == 1
    assert coverage["meets_depth_count"] == 1
    assert coverage["below_depth_count"] == 1
    assert coverage["validated_count"] == coverage["meets_depth_count"] + coverage["below_depth_count"]
    assert coverage["depth_policy_version"] == DEPTH_POLICY_VERSION
    assert coverage["depth_coverage"] == "partial"
    # Historical keys keep their exact semantics.
    assert coverage["coverage"] == "partial"


def test_coverage_complete_requires_depth_too() -> None:
    all_rich = description_coverage([_candidate(RICH_TEXT), _candidate(RICH_TEXT + " Ещё.")])
    assert all_rich["coverage"] == "complete"
    assert all_rich["depth_coverage"] == "complete"
    thin_but_valid = description_coverage([_candidate(RICH_TEXT), _candidate(THIN_TEXT)])
    assert thin_but_valid["coverage"] == "complete"
    assert thin_but_valid["depth_coverage"] == "partial"


def test_coverage_empty_input_is_unknown_depth() -> None:
    coverage = description_coverage([])
    assert coverage["total_candidates"] == 0
    assert coverage["validated_count"] == 0
    assert coverage["meets_depth_count"] == 0
    assert coverage["below_depth_count"] == 0
    assert coverage["depth_coverage"] == "unknown"


# ── Legacy artifacts claim nothing about depth ────────────────────────


def test_health_skips_depth_for_legacy_artifacts(capsys: object) -> None:
    import json
    import tempfile
    from pathlib import Path

    from autoscout.run_health import run_health

    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        run_summary = {"run_date": "2026-09-14", "operation_id": "op-1", "editor_facing_count": 2}
        (output_dir / "run-summary.json").write_text(json.dumps(run_summary), encoding="utf-8")
        (output_dir / "source-runs.json").write_text("[]", encoding="utf-8")
        (output_dir / "pilot-status.json").write_text(
            json.dumps({"ready_for_editor": True, "editor_facing_count": 2, "run_date": "2026-09-14"}),
            encoding="utf-8",
        )
        legacy_coverage = {"coverage": "complete", "validated_count": 2, "fallback_count": 0}
        (output_dir / "description-coverage.json").write_text(json.dumps(legacy_coverage), encoding="utf-8")
        run_health(output_dir, effective_date="2026-09-14")
        out = capsys.readouterr().out  # type: ignore[union-attr]
    assert "description_coverage=complete" in out
    assert "description_depth_coverage" not in out
    assert "description_below_depth_count" not in out


def test_health_reports_depth_when_present(capsys: object) -> None:
    import json
    import tempfile
    from pathlib import Path

    from autoscout.run_health import run_health

    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        run_summary = {"run_date": "2026-09-14", "operation_id": "op-1", "editor_facing_count": 2}
        (output_dir / "run-summary.json").write_text(json.dumps(run_summary), encoding="utf-8")
        (output_dir / "source-runs.json").write_text("[]", encoding="utf-8")
        (output_dir / "pilot-status.json").write_text(
            json.dumps({"ready_for_editor": True, "editor_facing_count": 2, "run_date": "2026-09-14"}),
            encoding="utf-8",
        )
        coverage = description_coverage([_candidate(RICH_TEXT), _candidate(THIN_TEXT)])
        (output_dir / "description-coverage.json").write_text(json.dumps(coverage), encoding="utf-8")
        run_health(output_dir, effective_date="2026-09-14")
        out = capsys.readouterr().out  # type: ignore[union-attr]
    assert "description_depth_coverage=partial" in out
    assert "description_meets_depth_count=1" in out
    assert "description_below_depth_count=1" in out
    assert "description_depth_policy_version=1" in out


def test_completion_message_skips_depth_for_legacy_coverage() -> None:
    from autoscout.russian_workflow import completion_message

    legacy = {"validated_count": 2, "fallback_count": 0, "total_candidates": 2}
    message = completion_message(outcome="completed/no-attention", description_coverage=legacy)
    assert "Описаний на русском: 2 из 2." in message
    assert "порога глубины" not in message


def test_completion_message_reports_depth_when_present() -> None:
    from autoscout.russian_workflow import completion_message

    coverage = description_coverage([_candidate(RICH_TEXT), _candidate(THIN_TEXT)])
    message = completion_message(outcome="completed/no-attention", description_coverage=coverage)
    assert "Описаний на русском: 2 из 2." in message
    assert "Кратких описаний (ниже порога глубины): 1." in message
    # Mechanical wording only: never a quality verdict.
    assert "качеств" not in message


# ── End-to-end rendering: thin, rich, fallback, legacy ────────────────


def _render_rows(rows: list[dict[str, object]]) -> tuple[list[dict[str, object]], dict[str, object]]:
    from autoscout.review_rendering import _render_neutral_row

    rendered = []
    for row in rows:
        candidate = dict(row)
        editor_row = {
            "Title": candidate.get("title", ""),
            "Author": "Author",
            "Editor status": "",
            "Editor note": "",
            "Candidate ID": candidate.get("identity_key", ""),
            "Update delta": "new",
            "Lifecycle": "",
            "Review context": "",
        }
        rendered.append(_render_neutral_row(candidate, editor_row))  # type: ignore[arg-type]
    return rendered, {}


def _row(text: str) -> dict[str, object]:
    return {
        "identity_key": "k|a",
        "title": "Title",
        "primary_authors": ["Author"],
        "content_description": text,
        "update_delta": "new",
    }


def test_rendered_rows_mark_only_below_floor_descriptions() -> None:
    rendered, _ = _render_rows([_row(RICH_TEXT), _row(THIN_TEXT), _row(DESCRIPTION_FALLBACK), _row("")])
    assert rendered[0]["description_meets_depth"] is True
    assert rendered[0]["description_depth_status"] == ""
    assert "Description depth" not in rendered[0]["editor_row"]
    assert rendered[1]["description_meets_depth"] is False
    assert rendered[1]["description_depth_status"] == DESCRIPTION_BELOW_DEPTH_STATUS
    assert rendered[1]["editor_row"]["Description depth"] == DESCRIPTION_BELOW_DEPTH_STATUS
    # Fallback and empty rows carry no depth marker: their status already
    # says the description is missing.
    assert rendered[2]["description_depth_status"] == ""
    assert rendered[3]["description_depth_status"] == ""


def test_markdown_review_distinguishes_depth() -> None:
    import tempfile
    from pathlib import Path

    from autoscout.json_io import write_json
    from autoscout.review_rendering import render_review

    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        write_json(output_dir / "run-summary.json", {"run_date": "2026-09-14", "editor_facing_count": 2})
        write_json(
            output_dir / "scouting-candidates.json",
            [
                {
                    "candidate_id": "candidate-001",
                    "identity_key": "rich|author",
                    "title": "Rich",
                    "primary_authors": ["Author"],
                    "content_description": RICH_TEXT,
                    "update_delta": "new",
                },
                {
                    "candidate_id": "candidate-002",
                    "identity_key": "thin|author",
                    "title": "Thin",
                    "primary_authors": ["Author"],
                    "content_description": THIN_TEXT,
                    "update_delta": "new",
                },
            ],
        )
        write_json(output_dir / "candidate-history.json", [])
        assert render_review(output_dir) == 0
        markdown = (output_dir / "weekly-review.md").read_text(encoding="utf-8")
        html = (output_dir / "weekly-review.html").read_text(encoding="utf-8")
    assert "краткими остаются — 1" in markdown
    assert "Глубина описания" in markdown
    assert DESCRIPTION_BELOW_DEPTH_STATUS in markdown
    assert "краткими остаются — 1" in html
    assert "Глубина описания" in html
    assert DESCRIPTION_BELOW_DEPTH_STATUS in html


def test_markdown_review_without_thin_keeps_exact_shape() -> None:
    import tempfile
    from pathlib import Path

    from autoscout.json_io import write_json
    from autoscout.review_rendering import render_review

    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        write_json(output_dir / "run-summary.json", {"run_date": "2026-09-14", "editor_facing_count": 2})
        write_json(
            output_dir / "scouting-candidates.json",
            [
                {
                    "candidate_id": "candidate-001",
                    "identity_key": "rich|author",
                    "title": "Rich",
                    "primary_authors": ["Author"],
                    "content_description": RICH_TEXT,
                    "update_delta": "new",
                },
            ],
        )
        write_json(output_dir / "candidate-history.json", [])
        assert render_review(output_dir) == 0
        markdown = (output_dir / "weekly-review.md").read_text(encoding="utf-8")
        html = (output_dir / "weekly-review.html").read_text(encoding="utf-8")
    assert "краткими остаются" not in markdown
    assert "Глубина описания" not in markdown
    assert "краткими остаются" not in html
    assert "Глубина описания" not in html
