"""The recorded fit score of each book, read for the review (issue #1105).

The editorial-fit shadow stage (#725) records a ``fit-v2`` score beside each
candidate of a run, and scores the undecided books of earlier runs into
backlog artifacts. This module reads those artifacts back: the newest
recorded score of a book, by identity key or durable Candidate ID. Reading
calls no model and writes nothing.

It also holds what the shadow stage and its readers share about the
artifacts: their directory, their type, and how a record names its book.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from autoscout.candidate_identity import effective_candidate_id
from autoscout.candidate_identity import stored_candidate_id_for_alias
from autoscout.json_io import read_json

QUESTION_SET_VERSION = "fit-v2"
SHADOW_DIRECTORY = "editorial-fit-shadow"
SCORES_ARTIFACT_TYPE = "editorial-fit-shadow-scores"
# Scores of undecided books that no run of their own scored. The review shows
# them; the shadow report and the ranking benchmark never read them.
BACKLOG_ARTIFACT_TYPE = "editorial-fit-shadow-backlog-scores"
# The weekly config value that orders the review by the fit score. Off unless true.
ORDERING_CONFIG_KEY = "editorial_fit_ordering"

# Editor-facing wording, shared by the Markdown and the HTML review.
FIT_SCORE_COLUMN = "Оценка"
FIT_ORDER_NOTE = (
    "внутри каждой даты книги идут по оценке соответствия издательству (от 0 до 4, выше — лучше); "
    "книги без оценки идут после них, книги с приоритетом D — в конце."
)


class FitScoreArtifactError(ValueError):
    """A score artifact cannot be read as JSON."""


def shadow_dir(output_dir: Path) -> Path:
    return output_dir / SHADOW_DIRECTORY


def load_score_artifacts(output_dir: Path) -> list[dict[str, Any]]:
    """The ``fit-v2`` score artifacts of the runs of *output_dir*, oldest run first."""
    return _load_artifacts(output_dir, "scores-*.json", SCORES_ARTIFACT_TYPE)


def load_backlog_artifacts(output_dir: Path) -> list[dict[str, Any]]:
    """The ``fit-v2`` backlog score artifacts of *output_dir*, oldest first."""
    return _load_artifacts(output_dir, "backlog-scores-*.json", BACKLOG_ARTIFACT_TYPE)


def _load_artifacts(output_dir: Path, pattern: str, artifact_type: str) -> list[dict[str, Any]]:
    artifacts = []
    directory = shadow_dir(output_dir)
    for path in sorted(directory.glob(pattern)) if directory.is_dir() else []:
        try:
            data = read_json(path, default=None)
        except (OSError, ValueError) as exc:
            raise FitScoreArtifactError(f"{path.name} is not readable JSON") from exc
        if (
            isinstance(data, dict)
            and data.get("artifact_type") == artifact_type
            and data.get("question_set_version") == QUESTION_SET_VERSION
            and isinstance(data.get("records"), list)
            and isinstance(data.get("run_date"), str)
        ):
            artifacts.append(data)
    return sorted(artifacts, key=lambda artifact: artifact["run_date"])


def durable_id(record: Mapping[str, Any], aliases: Mapping[str, Any]) -> str:
    """The surviving durable Candidate ID of a record, or "" when it has none.

    An identity correction can rename the identity key or retire a Candidate
    ID. The alias table keeps both resolvable, so one book stays one book.
    """
    candidate_id = (
        stored_candidate_id_for_alias(dict(aliases), str(record.get("identity_key", "")).strip())
        or str(record.get("candidate_id", "")).strip()
    )
    return effective_candidate_id(candidate_id, dict(aliases)) if candidate_id else ""


def book_keys(record: Mapping[str, Any], aliases: Mapping[str, Any]) -> set[str]:
    identity = str(record.get("identity_key", "")).strip()
    durable = durable_id(record, aliases)
    return {key for key in (f"identity:{identity}" if identity else "", f"id:{durable}" if durable else "") if key}


class FitScores:
    """The newest recorded fit score of each book, on the scale of the fit question: 0 to 4."""

    def __init__(self, by_key: Mapping[str, tuple[int, float]], aliases: Mapping[str, Any]) -> None:
        # Each key holds (the position of its artifact, oldest first; the score).
        self._by_key = dict(by_key)
        self._aliases = aliases

    def __bool__(self) -> bool:
        return bool(self._by_key)

    def score_for(self, record: Mapping[str, Any]) -> float | None:
        """The fit score of the book *record* names, or ``None`` when none was recorded."""
        # After an identity correction the identity key and the durable ID can
        # name records of different runs: the newer one is the book's score.
        found = [self._by_key[key] for key in book_keys(record, self._aliases) if key in self._by_key]
        return max(found)[1] if found else None


NO_FIT_SCORES = FitScores({}, {})


def read_fit_scores(output_dir: Path, aliases: Mapping[str, Any]) -> FitScores:
    """The newest scored record of each book in the shadow artifacts of *output_dir*.

    An unreadable artifact gives no scores at all, so the review renders as it
    does without them and never fails over an advisory file.
    """
    try:
        return recorded_fit_scores(output_dir, aliases)
    except FitScoreArtifactError:
        return NO_FIT_SCORES


def recorded_fit_scores(output_dir: Path, aliases: Mapping[str, Any]) -> FitScores:
    """The newest scored record of each book, from the run and the backlog artifacts.

    A failed call is no score: an older score of the same book stands. On one
    date the record of the run is the newer one.
    """
    dated = [(artifact["run_date"], 0, artifact) for artifact in load_backlog_artifacts(output_dir)]
    dated += [(artifact["run_date"], 1, artifact) for artifact in load_score_artifacts(output_dir)]
    artifacts = [artifact for _, _, artifact in sorted(dated, key=lambda entry: entry[:2])]
    by_key: dict[str, tuple[int, float]] = {}
    for position, artifact in enumerate(artifacts):
        for record in artifact["records"]:
            if not isinstance(record, dict) or record.get("status") != "scored":
                continue
            components = record.get("components")
            fit = components.get("fit") if isinstance(components, dict) else None
            # The expected level, 0 to 4: the scale the fit question itself uses.
            level = fit.get("expected_level") if isinstance(fit, dict) else None
            if isinstance(level, bool) or not isinstance(level, int | float):
                continue
            for key in book_keys(record, aliases):
                by_key[key] = (position, float(level))
    return FitScores(by_key, aliases)


def fit_ordering_enabled(config_path: Path | None) -> bool:
    """Whether the weekly config at *config_path* orders the review by the fit score."""
    if config_path is None:
        return False
    try:
        config = read_json(config_path, default={})
    except (OSError, ValueError):
        return False
    return isinstance(config, dict) and config.get(ORDERING_CONFIG_KEY) is True


def fit_score_text(score: float | None) -> str:
    """The score as the review shows it: one decimal, "" when the book has none."""
    return "" if score is None else f"{score:.1f}"
