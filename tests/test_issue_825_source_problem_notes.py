"""Issue #825 — the review names stale, failed, and skipped sources.

The 2026-10-15 review said only "4 из 31 источников — данные предыдущего
запуска". It did not say which four, and it did not name the six sources
that failed or the two that were skipped, although the run summary counted
them. The records below are taken from that run's ``source-runs.json`` and
the capture ledger in its ``workflow-log.json``, trimmed to the fields read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autoscout.json_io import write_json
from autoscout.review_rendering import render_review
from autoscout.source_problem_notes import complete_source_runs
from autoscout.source_problem_notes import editor_source_reason
from autoscout.source_problem_notes import source_problem_lines
from autoscout.source_problem_notes import source_problem_notes

OPERATION_ID = "d07f0435"


def _run(name: str, status: str, reason: str = "", *, url: str = "", rows: int = 0, safe: str = "") -> dict:
    run = {
        "source_name": name,
        "status": status,
        "reason": reason,
        "url": url or f"https://example.test/{name.casefold().replace(' ', '-')}",
        "editor_facing_count": rows,
    }
    if safe:
        run["debug_metadata"] = {"safe_failure_status": safe}
    return run


REUSED = "served from an artifact bound to an earlier operation, not freshly collected"

# The replay ledger: only sources that reached replay.
PIPELINE_RUNS = [
    _run("National Book Awards 2025 Nonfiction", "reused", REUSED, rows=7),
    _run("Goodreads Choice Awards 2025 Nonfiction", "collected", rows=15),
    _run("Andrew Carnegie Medals for Excellence - Nonfiction 2026", "reused", REUSED, rows=17),
    _run(
        "Börsenblatt Sachbuch Bestseller",
        "failed",
        "Codex extraction failed: all extracted mentions were dropped by validation guardrails",
    ),
    _run(
        "Cúspide 100 más vendidos",
        "failed",
        "undated ranking list is unchanged since 2026-10-14 and cannot be reported as current",
    ),
]

# The capture ledger: every configured source, including those that never reached replay.
CAPTURE_RUNS = [
    _run(
        "Current Weekly Nonfiction Agent Extraction",
        "skipped",
        "Agent extraction live collection blocked by HTTP 403",
        safe="source_blocked",
    ),
    _run("National Book Awards 2025 Nonfiction", "codex-captured"),
    _run("Goodreads Choice Awards 2025 Nonfiction", "codex-captured"),
    _run(
        "Deutscher Sachbuchpreis",
        "skipped",
        "between editions: the homepage presents the next edition and lists no nominees yet",
        safe="between_editions",
    ),
    _run("Die Zeit Sachbuch-Bestenliste", "inactive"),
    _run(
        "TodosTusLibros Librerías Recomiendan No Ficción",
        "failed",
        "capture per-source bound exhausted after 10.0s",
        safe="capture_deadline",
    ),
    _run("Andrew Carnegie Medals for Excellence - Nonfiction 2026", "codex-captured"),
    _run("Börsenblatt Sachbuch Bestseller", "codex-captured"),
    _run("Cúspide 100 más vendidos", "codex-captured"),
    _run(
        "BookInfo Weekly Nonfiction",
        "failed",
        "BookInfo has no completed week within the 14-day start-age limit",
        safe="bookinfo_stale",
    ),
    _run(
        "Independent Press Top 40 Nonfiction",
        "failed",
        "Agent extraction live page exceeds the response-size limit",
    ),
]


def test_the_merged_ledgers_name_every_stale_failed_and_skipped_source() -> None:
    notes = source_problem_notes(complete_source_runs(CAPTURE_RUNS, PIPELINE_RUNS))

    by_kind: dict[str, list[str]] = {}
    for note in notes:
        by_kind.setdefault(note.kind, []).append(note.source_name)
    assert by_kind == {
        "stale": [
            "National Book Awards 2025 Nonfiction",
            "Andrew Carnegie Medals for Excellence - Nonfiction 2026",
        ],
        "failed": [
            "TodosTusLibros Librerías Recomiendan No Ficción",
            "Börsenblatt Sachbuch Bestseller",
            "Cúspide 100 más vendidos",
            "BookInfo Weekly Nonfiction",
            "Independent Press Top 40 Nonfiction",
        ],
        "skipped": [
            "Current Weekly Nonfiction Agent Extraction",
            "Deutscher Sachbuchpreis",
        ],
    }


def test_a_replay_record_overrides_the_capture_record_for_the_same_source() -> None:
    """A source captured fine and then failed in replay is reported once, as failed."""
    merged = complete_source_runs(CAPTURE_RUNS, PIPELINE_RUNS)
    boersenblatt = [run for run in merged if run["source_name"] == "Börsenblatt Sachbuch Bestseller"]

    assert [run["status"] for run in boersenblatt] == ["failed"]


@pytest.mark.parametrize(
    ("source_name", "reason"),
    [
        ("Current Weekly Nonfiction Agent Extraction", "сайт не пускает сборщик"),
        ("Deutscher Sachbuchpreis", "между сезонами: новый список ещё не опубликован"),
        ("TodosTusLibros Librerías Recomiendan No Ficción", "сайт не ответил вовремя"),
        ("BookInfo Weekly Nonfiction", "свежий выпуск списка ещё не вышел"),
        ("Independent Press Top 40 Nonfiction", "страница слишком большая для сбора"),
        ("Börsenblatt Sachbuch Bestseller", "найденные книги не прошли проверку"),
        ("Cúspide 100 más vendidos", "список не обновился с прошлого запуска"),
    ],
)
def test_each_recorded_reason_reads_in_editor_terms(source_name: str, reason: str) -> None:
    notes = {note.source_name: note for note in source_problem_notes(complete_source_runs(CAPTURE_RUNS, PIPELINE_RUNS))}

    assert notes[source_name].reason == reason


def test_an_unrecognized_reason_falls_back_to_a_plain_phrase_and_never_leaks() -> None:
    run = _run("Some Source", "failed", "ValueError: unexpected token at /tmp/x.json line 3")

    assert editor_source_reason(run, "failed") == "не удалось собрать список"


def test_the_lines_carry_no_diagnostic_codes_or_paths() -> None:
    text = "\n".join(source_problem_lines(source_problem_notes(complete_source_runs(CAPTURE_RUNS, PIPELINE_RUNS))))

    for leaked in ("HTTP", "403", "10.0s", "bookinfo_stale", "capture_deadline", "/", "guardrails"):
        assert leaked not in text


def test_reused_sources_state_their_review_rows() -> None:
    lines = source_problem_lines(source_problem_notes(PIPELINE_RUNS))

    assert lines[0] == (
        "Данные предыдущего запуска: National Book Awards 2025 Nonfiction (строк в обзоре — 7); "
        "Andrew Carnegie Medals for Excellence - Nonfiction 2026 (строк в обзоре — 17)."
    )


def test_a_fully_fresh_run_has_no_lines() -> None:
    runs = [_run("Goodreads Choice Awards 2025 Nonfiction", "collected", rows=15), _run("Die Zeit", "inactive")]

    assert source_problem_lines(source_problem_notes(complete_source_runs(runs, runs))) == []


def _candidate(identity_key: str, title: str, source_name: str) -> dict:
    return {
        "identity_key": identity_key,
        "title": title,
        "primary_authors": ["Alex Example"],
        "update_delta": "new",
        "priority_label": "A",
        "independent_signal_count": 1,
        "russian_edition_status": "not found in Russian",
        "content_description": "Описание книги.",
        "source_evidence": [
            {
                "source_name": source_name,
                "source_url": "https://example.test/source",
                "evidence_url": f"https://example.test/source/{identity_key}",
                "evidence_note": "Listed.",
            }
        ],
    }


NBA = "National Book Awards 2025 Nonfiction"
CARNEGIE = "Andrew Carnegie Medals for Excellence - Nonfiction 2026"


def _write_run(output_dir: Path, *, capture_runs: list[dict] | None) -> None:
    write_json(
        output_dir / "scouting-candidates.json",
        [
            _candidate("book one|alex example", "Book One", NBA),
            _candidate("book two|alex example", "Book Two", NBA),
            _candidate("book three|alex example", "Book Three", CARNEGIE),
        ],
    )
    # The editor hid Book Two, so the review shows one NBA row, not the two
    # the pipeline credited to the source.
    write_json(
        output_dir / "candidate-visibility.json",
        [{"identity_key": "book two|alex example", "visibility": "hidden"}],
    )
    write_json(
        output_dir / "run-summary.json",
        {"run_date": "2026-10-15", "editor_facing_count": 3, "operation_id": OPERATION_ID},
    )
    write_json(output_dir / "source-runs.json", PIPELINE_RUNS)
    if capture_runs is not None:
        write_json(
            output_dir / "workflow-log.json",
            [{"stage": "capture", "operation_id": OPERATION_ID, "detail": {"source_runs": capture_runs}}],
        )


def test_both_renderings_name_the_same_sources(tmp_path: Path) -> None:
    _write_run(tmp_path, capture_runs=CAPTURE_RUNS)

    assert render_review(tmp_path) == 0

    markdown = (tmp_path / "weekly-review.md").read_text(encoding="utf-8")
    html = (tmp_path / "weekly-review.html").read_text(encoding="utf-8")
    for name in (
        NBA,
        CARNEGIE,
        "TodosTusLibros Librerías Recomiendan No Ficción",
        "BookInfo Weekly Nonfiction",
        "Independent Press Top 40 Nonfiction",
        "Börsenblatt Sachbuch Bestseller",
        "Current Weekly Nonfiction Agent Extraction",
        "Deutscher Sachbuchpreis",
    ):
        assert f"{name} — " in markdown or f"{name} (" in markdown, name
    header = markdown.split("## ")[0]
    source_lines = [line for line in header.splitlines() if line.startswith("**⚠ Источники**: ")]
    assert len(source_lines) == 3
    for line in source_lines:
        assert line.removeprefix("**⚠ Источники**: ").replace("'", "&#x27;") in html
    assert "Die Zeit" not in header, "an inactive source is not a problem this week"


def test_a_stale_source_is_credited_only_with_rows_the_review_shows(tmp_path: Path) -> None:
    """The pipeline credited NBA with 7 rows; the review shows one (Book Two is hidden)."""
    _write_run(tmp_path, capture_runs=CAPTURE_RUNS)

    assert render_review(tmp_path) == 0

    for name in ("weekly-review.md", "weekly-review.html"):
        text = (tmp_path / name).read_text(encoding="utf-8")
        assert f"{NBA} (строк в обзоре — 1)" in text
        assert f"{CARNEGIE} (строк в обзоре — 1)" in text
        assert "строк в обзоре — 7" not in text


def test_without_a_capture_ledger_the_replay_ledger_is_still_named(tmp_path: Path) -> None:
    """A replay-only render (no capture stage) names what source-runs.json holds."""
    _write_run(tmp_path, capture_runs=None)

    assert render_review(tmp_path) == 0

    markdown = (tmp_path / "weekly-review.md").read_text(encoding="utf-8")
    assert f"{NBA} (строк в обзоре — 1)" in markdown
    assert "Börsenblatt Sachbuch Bestseller — найденные книги не прошли проверку" in markdown
    assert "TodosTusLibros" not in markdown
