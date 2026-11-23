"""The State Store is the only code that knows where canonical state lives (ADR 0023, #1071).

No module under ``autoscout/`` outside ``autoscout/state_store/`` may name a
canonical state file or the directory that holds Candidate History one file
per book (#1073): not as a string, and not through a constant that holds one
(``PROPOSAL_FILE_NAME``, ``DEFAULT_ALIAS_FILENAME`` and the like). Docstrings
and comments may mention them; code may not.

The modules that still have to are listed below in two groups, each with
the reason. The list is checked both ways: a module outside it that names a
state file fails, and so does a module inside it that no longer names one, so
the list cannot go stale.
"""

from __future__ import annotations

import ast
import importlib
from collections.abc import Iterator
from pathlib import Path
from pathlib import PurePath

from autoscout.state_store.json_backend import FILE_NAMES
from autoscout.state_store.json_backend import LEGACY_PROFILE_FILE_NAME
from autoscout.state_store.layout import CANDIDATE_HISTORY_DIR

PACKAGE = Path(__file__).resolve().parents[1] / "autoscout"
STORE = PACKAGE / "state_store"

# Every file name a canonical collection is or was kept under.
CANONICAL_FILE_NAMES = frozenset({*FILE_NAMES.values(), LEGACY_PROFILE_FILE_NAME})

COMMITTED_STATE = "committed state in Git"

ALLOWLIST: dict[str, str] = {
    # ── The committed JSON export in Git. ──
    # These handle the files as Git holds them: protected names, the commit
    # envelope and parity. Each knows both layouts of Candidate History
    # through autoscout.state_store.layout.
    "autoscout/git_envelope.py": COMMITTED_STATE,
    "autoscout/safe_artifacts.py": COMMITTED_STATE,
    "autoscout/artifact_parity.py": COMMITTED_STATE,
    # Its evidence baseline names the committed profile and alias files with
    # their SHA-256 as Git holds them, so a reader can find the exact version
    # audited; the content is read through the store, and Candidate History's
    # location and digest come from the layout.
    "autoscout/editorial_audit.py": COMMITTED_STATE,
    # Names the Source Proposals file for its documented --proposals-file flag
    # and fingerprints and commits its bytes in the onboarding commit; the
    # proposals themselves, the accepted ones included, go through the store.
    "autoscout/source_onboarding_policy.py": COMMITTED_STATE,
    # ── Run artifacts, by design. ──
    # The weekly report directory holds a run-local copy of Candidate History
    # and the run's pending Source Proposals under the same file names. They
    # are run artifacts, not canonical state, and other modules name them
    # only through these modules' constants.
    "autoscout/weekly_report.py": "run artifact",
    "autoscout/reporting.py": "run artifact",
}

RUN_ARTIFACT_MODULES = frozenset(path for path, group in ALLOWLIST.items() if group == "run artifact")


def _modules() -> Iterator[Path]:
    for path in sorted(PACKAGE.rglob("*.py")):
        if STORE not in path.parents:
            yield path


def _relative(path: Path) -> str:
    return path.relative_to(PACKAGE.parent).as_posix()


def _docstrings(tree: ast.Module) -> set[int]:
    """The ids of the string nodes that are docstrings, which may mention the files."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def _holds_a_state_file(value: object) -> bool:
    if isinstance(value, str):
        return value in CANONICAL_FILE_NAMES or value == CANDIDATE_HISTORY_DIR
    if isinstance(value, PurePath):
        return value.name in CANONICAL_FILE_NAMES or value.name == CANDIDATE_HISTORY_DIR
    return False


def _state_file_constants() -> frozenset[str]:
    """Module-level constants anywhere in the package that hold a canonical file name.

    Constants of the run-artifact modules name the run's copies and are left out.
    """
    names: set[str] = set()
    for path in _modules():
        if _relative(path) in RUN_ARTIFACT_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assigned = [
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign | ast.AnnAssign)
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name) and target.id.isupper()
        ]
        if not assigned:
            continue
        module = importlib.import_module(".".join(path.relative_to(PACKAGE.parent).with_suffix("").parts))
        names.update(name for name in assigned if _holds_a_state_file(getattr(module, name, None)))
    return frozenset(names)


def _is_collection(node: ast.expr) -> bool:
    """``Collection.CANDIDATE_HISTORY`` names a collection of the store, not a file."""
    return isinstance(node, ast.Name) and node.id == "Collection"


def _names_the_shard_directory(value: str) -> bool:
    """The directory name alone or as a path component; ``candidate-history-suppression`` and the like are other words."""
    return (
        value == CANDIDATE_HISTORY_DIR
        or f"{CANDIDATE_HISTORY_DIR}/" in value
        or value.endswith(f"/{CANDIDATE_HISTORY_DIR}")
    )


def _names_a_state_file(path: Path, constants: frozenset[str]) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = _docstrings(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            found.extend(f"line {node.lineno}: {name!r}" for name in sorted(CANONICAL_FILE_NAMES) if name in node.value)
            if _names_the_shard_directory(node.value):
                found.append(f"line {node.lineno}: {node.value!r}")
        elif isinstance(node, ast.Name) and node.id in constants:
            found.append(f"line {node.lineno}: {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in constants and not _is_collection(node.value):
            found.append(f"line {node.lineno}: {node.attr}")
        elif isinstance(node, ast.ImportFrom):
            found.extend(f"line {node.lineno}: {alias.name}" for alias in node.names if alias.name in constants)
    return found


def test_only_the_state_store_names_a_canonical_state_file() -> None:
    constants = _state_file_constants()
    offenders = {
        _relative(path): found
        for path in _modules()
        if _relative(path) not in ALLOWLIST and (found := _names_a_state_file(path, constants))
    }
    assert not offenders, (
        "These modules name a canonical state file, which only autoscout/state_store/ may do (ADR 0023). "
        "Read and write the collection through the State Store instead: open_state_store(state_dir), or the "
        "store the entry point opened, then store.read(Collection.X) / store.replace(Collection.X, value), "
        "inside store.transaction() when a read decides a write. A run-local copy in the report directory "
        "is named through autoscout.weekly_report's RUN_* constants.\n"
        + "\n".join(f"  {module}: {', '.join(found)}" for module, found in sorted(offenders.items()))
    )


def test_every_allowlisted_module_still_names_a_canonical_state_file() -> None:
    constants = _state_file_constants()
    stale = [module for module in ALLOWLIST if not _names_a_state_file(PACKAGE.parent / module, constants)]
    assert not stale, (
        "These allowlisted modules no longer name a canonical state file. Remove them from ALLOWLIST in "
        "tests/test_state_store_stake.py, so the stake keeps them off the files:\n"
        + "\n".join(f"  {module} ({ALLOWLIST[module]})" for module in stale)
    )


def test_the_stake_sees_a_file_name_and_a_constant_that_holds_one(tmp_path: Path) -> None:
    module = tmp_path / "example.py"
    module.write_text(
        '"""Mentions candidate-history.json in a docstring, which is allowed."""\n'
        "from autoscout.source_onboarding_policy import PROPOSAL_FILE_NAME\n"
        'PATH = "runs/x/candidate-visibility.json"\n'
        "OTHER = PROPOSAL_FILE_NAME\n"
        'SHARDS = "runs/x/candidate-history/ab"\n'
        'GATE = "candidate-history-suppression"\n',
        encoding="utf-8",
    )

    found = _names_a_state_file(module, _state_file_constants())

    assert found == [
        "line 2: PROPOSAL_FILE_NAME",
        "line 3: 'candidate-visibility.json'",
        "line 4: PROPOSAL_FILE_NAME",
        "line 5: 'runs/x/candidate-history/ab'",
    ]
