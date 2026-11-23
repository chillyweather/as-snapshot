"""What the doctor and run-health say about the State Store (#1076, ADR 0023).

Both take one read-only look (``state_store.inspect_state_store``): the active
backend, the SQLite database's path class and schema version, and whether the
exported files are exactly what the database would export. A database and an
export that disagree are one finding with exactly one recommended action,
named here once so the doctor and run-health give the same command.

The look opens no transaction and creates nothing, on either backend. The
JSON backend has no database and nothing to compare, so it adds only a line
naming it.

Editor-facing text is Russian and follows the clean-output contract: the
database is named by its path class, never by a path.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoscout.state_store import EXPORT_BEHIND
from autoscout.state_store import EXPORT_CHANGED_OUTSIDE
from autoscout.state_store import EXPORT_CURRENT
from autoscout.state_store import EXPORT_NEVER
from autoscout.state_store import EXPORT_NO_DATABASE
from autoscout.state_store import EXPORT_NOT_APPLICABLE
from autoscout.state_store import EXPORT_NOT_CHECKED
from autoscout.state_store import EXPORT_UNREADABLE
from autoscout.state_store import JSON_BACKEND
from autoscout.state_store import SQLITE_BACKEND
from autoscout.state_store import STATE_DIRECTORY_SELECTION
from autoscout.state_store import StateInspection
from autoscout.state_store import StateStoreError
from autoscout.state_store import inspect_state_store
from autoscout.state_store import select_state_backend_from_file
from autoscout.state_store.committed_state import COMMITTED
from autoscout.state_store.committed_state import EDITED
from autoscout.state_store.committed_state import UNKNOWN
from autoscout.state_store.committed_state import state_files_against_head

STATE_EXPORT_COMMAND = "./scripts/state-database export"
STATE_REBUILD_COMMAND = "./scripts/state-database rebuild"
STATE_REBUILD_OVERWRITE_COMMAND = "./scripts/state-database rebuild --overwrite"

_PATH_CLASS_RU = {
    "local": "локальная база",
    "synced": "база в синхронизируемой папке",
    "network": "база на сетевом диске",
    "unknown": "расположение базы не определено",
}

_EXPORT_RU = {
    EXPORT_CURRENT: "экспорт в Git актуален",
    EXPORT_BEHIND: "экспорт в Git отстаёт от базы",
    EXPORT_CHANGED_OUTSIDE: "файлы экспорта изменены вне AutoScout",
    EXPORT_NEVER: "экспорт в Git ещё не записывался",
    EXPORT_NOT_CHECKED: "во время поиска экспорт не проверяется",
}

CONTACT_ACTION = "Свяжитесь с Димой для диагностики хранилища состояния."
CONTACT_QUESTION = "Связаться с Димой?"


@dataclass(frozen=True)
class StateStoreFinding:
    """One finding about the State Store: what is wrong, the one action, the one question."""

    verdict: str
    recommended_action: str
    editor_question: str
    # The command the action names; empty when the action is to contact the operator.
    command: str = ""
    # For files changed outside the export: how they compare with Git's HEAD (``committed``, ``edited`` or ``unknown``).
    files_against_git: str = ""


def look_at_state_store(config_path: Path | None, state_dir: Path | None, *, compare: bool = True) -> StateInspection:
    """Inspect the store that serves *state_dir*, changing nothing.

    The backend is the one the state directory's marker names, else the one the
    weekly *config_path* (or the environment) selects. Without *compare*, only
    the backend is reported (``not_checked`` on SQLite); without a state
    directory there is no marker and no database to look at.
    """
    try:
        selection = select_state_backend_from_file(config_path)
    except StateStoreError:
        return StateInspection(backend="unknown", export_status=EXPORT_UNREADABLE, problem="backend_unknown")
    if state_dir is None:
        not_checked = selection.backend == SQLITE_BACKEND
        return StateInspection(
            backend=selection.backend,
            selected_by=selection.selected_by,
            export_status=EXPORT_NOT_CHECKED if not_checked else EXPORT_NOT_APPLICABLE,
        )
    return inspect_state_store(state_dir, selection, compare=compare)


def inspection_facts(inspection: StateInspection) -> dict[str, Any]:
    """The inspection as doctor facts: no path, only the database's class."""
    facts = inspection.to_dict()
    facts.pop("database", None)
    return facts


def finding_for(inspection: StateInspection, state_dir: Path | None = None) -> StateStoreFinding | None:
    """The one finding an inspection amounts to, or ``None`` when the database and the export agree.

    *state_dir* lets a finding about files changed outside the export compare
    them with Git's HEAD, which decides the action (see ``_changed_outside_finding``).
    """
    if inspection.problem:
        return _problem_finding(inspection.problem, marked=inspection.selected_by == STATE_DIRECTORY_SELECTION)
    status = inspection.export_status
    if status == EXPORT_BEHIND:
        return StateStoreFinding(
            verdict="Экспорт состояния в репозитории отстаёт от базы SQLite: после последнего экспорта база менялась.",
            recommended_action=f"Запишите экспорт состояния командой {STATE_EXPORT_COMMAND}.",
            editor_question="Записать экспорт состояния сейчас?",
            command=STATE_EXPORT_COMMAND,
        )
    if status == EXPORT_NEVER:
        return StateStoreFinding(
            verdict="Экспорт состояния ещё не записывался, и файлы в репозитории не совпадают с базой SQLite.",
            recommended_action=f"Запишите экспорт состояния командой {STATE_EXPORT_COMMAND}.",
            editor_question="Записать экспорт состояния сейчас?",
            command=STATE_EXPORT_COMMAND,
        )
    if status == EXPORT_CHANGED_OUTSIDE:
        return _changed_outside_finding(state_dir)
    return None


def _changed_outside_finding(state_dir: Path | None) -> StateStoreFinding:
    """Files that changed since the last export: the action depends on which side Git says is newer (#1098).

    The export would write the database over them, so it is never the
    action. Files identical to the committed ones are the committed side (a
    pull brought them), and the database is rebuilt from them. Files that
    differ from the committed ones carry a hand edit, which is undone first;
    the next look then finds the committed files and names the rebuild. When
    Git cannot say, the doctor does not guess: it names what each command
    would discard and asks for the operator.
    """
    found = state_files_against_head(state_dir) if state_dir is not None else None
    relation = found.relation if found is not None else UNKNOWN
    if found is not None and relation == COMMITTED:
        return StateStoreFinding(
            verdict=(
                "Файлы состояния совпадают с версией в Git, но не совпадают с базой SQLite: "
                "база на этом компьютере старее закоммиченного состояния."
            ),
            recommended_action=f"Постройте базу заново из файлов командой {STATE_REBUILD_OVERWRITE_COMMAND}.",
            editor_question=(
                "Построить базу заново из файлов? Данные в локальной базе, которых нет в этих файлах, будут потеряны."
            ),
            command=STATE_REBUILD_OVERWRITE_COMMAND,
            files_against_git=COMMITTED,
        )
    if found is not None and relation == EDITED and found.has_added_files:
        # A restore from HEAD does not remove a file Git does not have, so it would not settle this.
        return StateStoreFinding(
            verdict=(
                "Файлы состояния изменены вручную, и среди них есть новые файлы, которых нет в Git: "
                "они не совпадают ни с версией в Git, ни с базой SQLite."
            ),
            recommended_action=CONTACT_ACTION,
            editor_question=(
                "Связаться с Димой? Возврат файлов к версии в Git не удалит новые файлы, "
                "команда export --force сотрёт файлы состояния, а rebuild --overwrite — базу на этом компьютере."
            ),
            files_against_git=EDITED,
        )
    if found is not None and relation == EDITED:
        # Only the state files that differ: the run artifacts beside them in a report directory stay as they are.
        prefix = "" if found.repository_path == "." else f"{found.repository_path}/"
        targets = " ".join(shlex.quote(f"{prefix}{name}") for name in found.differing_paths)
        restore = f"git restore --source=HEAD --staged --worktree -- {targets}"
        return StateStoreFinding(
            verdict=("Файлы состояния изменены вручную: они не совпадают ни с версией в Git, ни с базой SQLite."),
            recommended_action=(
                f"Верните файлы состояния к версии в Git командой {restore}, затем повторите проверку."
            ),
            editor_question=(
                "Вернуть файлы состояния к версии в Git? Несохранённые правки в этих файлах состояния будут потеряны, "
                "а закоммиченное состояние останется нетронутым."
            ),
            command=restore,
            files_against_git=EDITED,
        )
    return StateStoreFinding(
        verdict=("Файлы состояния не совпадают с базой SQLite, а Git не позволяет определить, какая из сторон новее."),
        recommended_action=CONTACT_ACTION,
        editor_question=(
            "Связаться с Димой? Команда export --force стёрла бы файлы состояния, "
            "а команда rebuild --overwrite — базу на этом компьютере."
        ),
        files_against_git=UNKNOWN,
    )


def _problem_finding(problem: str, *, marked: bool = False) -> StateStoreFinding:
    if problem == "backend_conflict":
        return StateStoreFinding(
            verdict="Папка состояния переведена на SQLite, а в настройках выбран JSON.",
            recommended_action=(
                "Уберите выбор JSON из настроек (state_backend или AUTOSCOUT_STATE_BACKEND): "
                "папка состояния сама указывает, что она хранится в SQLite."
            ),
            editor_question="Убрать выбор JSON из настроек?",
        )
    if problem == "database_missing":
        return StateStoreFinding(
            verdict=(
                "Папка состояния переведена на SQLite, но базы на этом компьютере нет."
                if marked
                else "В папке состояния есть JSON-файлы, но базы SQLite нет."
            ),
            recommended_action=f"Постройте базу из JSON командой {STATE_REBUILD_COMMAND}.",
            editor_question="Построить базу из JSON сейчас?",
            command=STATE_REBUILD_COMMAND,
        )
    verdicts = {
        "backend_unknown": "В настройках указано неизвестное хранилище состояния.",
        "backend_marker_unreadable": "Отметка хранилища в папке состояния повреждена.",
        "database_location": "База SQLite лежит там, где её нельзя хранить: в синхронизируемой папке, на сетевом диске или в репозитории.",
        "database_unusable": "Базу SQLite не удалось прочитать.",
        "schema_version_mismatch": "Схема базы SQLite не совпадает с версией AutoScout.",
        "other_state_directory": "База SQLite принадлежит другой папке состояния.",
        "state_unreadable": "Состояние не удалось сравнить: файлы экспорта или база повреждены.",
    }
    return StateStoreFinding(
        verdict=verdicts.get(problem, "Хранилище состояния не удалось проверить."),
        recommended_action=CONTACT_ACTION,
        editor_question=CONTACT_QUESTION,
    )


def state_store_line(facts: dict[str, Any], *, with_export: bool = True) -> str:
    """The one line that names the backend, for the editor's doctor output."""
    backend = str(facts.get("backend", ""))
    if backend == JSON_BACKEND:
        return "Хранилище состояния: JSON."
    if backend != SQLITE_BACKEND:
        return "Хранилище состояния: не определено."
    if facts.get("export_status") == EXPORT_NO_DATABASE:
        return "Хранилище состояния: SQLite, база ещё не создана."
    parts = []
    path_class = facts.get("database_path_class")
    if path_class:
        parts.append(_PATH_CLASS_RU.get(str(path_class), _PATH_CLASS_RU["unknown"]))
    if facts.get("schema_version") is not None:
        parts.append(f"схема {facts['schema_version']}")
    detail = ", ".join(parts)
    export = _EXPORT_RU.get(str(facts.get("export_status", ""))) if with_export and not facts.get("problem") else None
    line = "Хранилище состояния: SQLite"
    if detail:
        line += f" ({detail})"
    if facts.get("selected_by") == STATE_DIRECTORY_SELECTION:
        line += ", задано отметкой в папке состояния"
    if export:
        line += f"; {export}"
    return line + "."
