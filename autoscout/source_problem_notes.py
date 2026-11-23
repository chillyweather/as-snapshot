"""Name the sources behind the review's stale and missing data (Issue #825).

The review used to say only how many sources served an earlier run's data,
so the editor could not tell which books were stale or which sources gave
nothing this week. Named source problems are editorial facts under the
normal editor output contract, so both review renderings list them, from
the same source-run records the count is computed from.

Reasons are short Russian phrases in editor terms. The recorded English
reason is matched by fixed substrings, deterministically, and never shown:
it can carry HTTP codes, limits, and diagnostic names that the editor
output must not expose.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from typing import Literal

from autoscout.reporting import source_run_identity
from autoscout.source_run_status import SOURCE_STATUS_FAILED
from autoscout.source_run_status import SOURCE_STATUS_SKIPPED
from autoscout.source_run_status import is_stale_source_run
from autoscout.source_run_status import normalized_source_run_status

SourceProblemKind = Literal["stale", "failed", "skipped"]

# Checked in order; the first rule whose any substring occurs in the
# lower-cased reason (or the safe failure status) names the reason.
_REASON_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("between editions", "between_editions"), "между сезонами: новый список ещё не опубликован"),
    (("unchanged since",), "список не обновился с прошлого запуска"),
    (("ranking_list_not_yet_refreshed",), "новый выпуск списка ещё не вышел"),
    (("no completed week", "bookinfo_stale", "is older than", "stale"), "свежий выпуск списка ещё не вышел"),
    (("http 404",), "страница не найдена"),
    (("http 40", "blocked", "captcha", "source_blocked"), "сайт не пускает сборщик"),
    (("response-size limit", "too large"), "страница слишком большая для сбора"),
    (("bound exhausted", "deadline", "timed out", "timeout"), "сайт не ответил вовремя"),
    (("dropped by validation",), "найденные книги не прошли проверку"),
    (("page shape validation",), "страница изменила вид"),
)

_DEFAULT_REASONS: dict[SourceProblemKind, str] = {
    "failed": "не удалось собрать список",
    "skipped": "пропущен при сборе",
    "stale": "данные предыдущего запуска",
}


@dataclass(frozen=True)
class SourceProblemNote:
    """One named source whose data this week is stale or missing."""

    source_name: str
    kind: SourceProblemKind
    reason: str
    review_row_count: int


def editor_source_reason(run: Mapping[str, Any], kind: SourceProblemKind) -> str:
    """Return the editor-facing Russian reason for one source-run record."""
    debug = run.get("debug_metadata")
    safe_status = str(debug.get("safe_failure_status", "")) if isinstance(debug, Mapping) else ""
    text = f"{run.get('reason', '')} {safe_status}".casefold()
    if kind != "stale":
        for needles, reason in _REASON_RULES:
            if any(needle in text for needle in needles):
                return reason
    return _DEFAULT_REASONS[kind]


def _row_count(run: Mapping[str, Any]) -> int:
    value: object = run.get("editor_facing_count", 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def complete_source_runs(
    capture_runs: Iterable[object] | None, pipeline_runs: Iterable[object] | None
) -> list[dict[str, Any]]:
    """Merge the capture ledger with the replay ledger, one record per source.

    ``source-runs.json`` holds only the sources that reached replay; a source
    that failed or was skipped at capture appears only in the capture ledger.
    The replay record is authoritative for a source in both, as in
    ``capture_source_outcome_counts``, so the names agree with the counts.
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for ledger in (capture_runs or [], pipeline_runs or []):
        for run in ledger:
            if isinstance(run, dict):
                merged[source_run_identity(run)] = run
    return list(merged.values())


def source_problem_notes(
    source_runs: Iterable[object], review_row_counts: Mapping[str, int] | None = None
) -> list[SourceProblemNote]:
    """Return the stale, failed, and skipped sources in source-run order.

    Inactive, ignored, and healthy sources are not listed. A record without a
    source name is skipped rather than shown as a blank entry.

    ``review_row_counts`` maps a source name to the current-run rows the
    review actually shows for it. When supplied it replaces the record's
    ``editor_facing_count``, which counts candidates before editor-hidden
    books are removed and can name rows the review does not show.
    """
    # Stale evidence is classified by the shared source-run predicate, never
    # by comparing the status here (Issue #384).
    kinds: dict[str, SourceProblemKind] = {
        SOURCE_STATUS_FAILED: "failed",
        SOURCE_STATUS_SKIPPED: "skipped",
    }
    notes: list[SourceProblemNote] = []
    for run in source_runs:
        if not isinstance(run, Mapping):
            continue
        status = normalized_source_run_status(run.get("status"))
        if status is None:
            continue
        kind: SourceProblemKind | None = "stale" if is_stale_source_run(status) else kinds.get(status)
        name = str(run.get("source_name", "")).strip()
        if kind is None or not name:
            continue
        notes.append(
            SourceProblemNote(
                source_name=name,
                kind=kind,
                reason=editor_source_reason(run, kind),
                review_row_count=(
                    max(0, int(review_row_counts.get(name, 0))) if review_row_counts is not None else _row_count(run)
                ),
            )
        )
    return notes


def source_problem_lines(notes: Iterable[SourceProblemNote]) -> list[str]:
    """Return the plain-text review lines, one per non-empty problem kind."""
    notes = list(notes)
    lines: list[str] = []
    stale = [note for note in notes if note.kind == "stale"]
    if stale:
        lines.append(
            "Данные предыдущего запуска: "
            + "; ".join(f"{note.source_name} (строк в обзоре — {note.review_row_count})" for note in stale)
            + "."
        )
    failed = [note for note in notes if note.kind == "failed"]
    if failed:
        lines.append("Не удалось собрать: " + "; ".join(f"{note.source_name} — {note.reason}" for note in failed) + ".")
    skipped = [note for note in notes if note.kind == "skipped"]
    if skipped:
        lines.append("Пропущены: " + "; ".join(f"{note.source_name} — {note.reason}" for note in skipped) + ".")
    return lines
