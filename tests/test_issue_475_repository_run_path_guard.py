"""Static protection against reading the moving weekly-run alias from tests."""

from __future__ import annotations

import ast
from pathlib import Path

REPOSITORY_RUN_COMPONENTS = ("runs", "current-weekly-pilot-russian")
_REPOSITORY_ROOT = "<repository-root>"


def _is_path_constructor(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "Path") or (
        isinstance(node, ast.Attribute)
        and node.attr == "Path"
        and isinstance(node.value, ast.Name)
        and node.value.id == "pathlib"
    )


def _is_module_file_path(node: ast.AST) -> bool:
    """Recognize the bounded Path(__file__) root idioms used in test modules."""
    if isinstance(node, ast.Call) and _is_path_constructor(node.func):
        return len(node.args) == 1 and isinstance(node.args[0], ast.Name) and node.args[0].id == "__file__"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "resolve":
        return _is_module_file_path(node.func.value)
    if isinstance(node, ast.Attribute) and node.attr in {"parent", "parents"}:
        return _is_module_file_path(node.value)
    if isinstance(node, ast.Subscript) and _is_module_file_path(node.value):
        return True
    return False


def _literal_path_parts(node: ast.AST) -> tuple[str, ...] | None:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return None
    return tuple(part for part in node.value.split("/") if part)


def _path_parts(node: ast.AST, root_names: set[str]) -> tuple[str, ...] | None:
    if isinstance(node, ast.Name) and node.id in root_names:
        return (_REPOSITORY_ROOT,)
    if _is_module_file_path(node):
        return (_REPOSITORY_ROOT,)

    literal_parts = _literal_path_parts(node)
    if literal_parts is not None:
        return literal_parts

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _path_parts(node.left, root_names)
        right = _path_parts(node.right, root_names)
        if left is not None and right is not None:
            return left + right
        return None

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "joinpath":
        base = _path_parts(node.func.value, root_names)
        if base is None:
            return None
        joined = [*base]
        for argument in node.args:
            parts = _literal_path_parts(argument)
            if parts is None:
                return None
            joined.extend(parts)
        return tuple(joined)

    return None


def _assigned_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Tuple | ast.List):
        names: set[str] = set()
        for element in node.elts:
            names.update(_assigned_names(element))
        return names
    return set()


def _root_names(tree: ast.Module) -> set[str]:
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr)]
    root_names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for assignment in assignments:
            if isinstance(assignment, ast.Assign):
                value = assignment.value
                targets = assignment.targets
            else:
                value = assignment.value
                targets = [assignment.target]
            if value is None:
                continue
            parts = _path_parts(value, root_names)
            if _is_module_file_path(value) or (parts is not None and _REPOSITORY_ROOT in parts):
                for target in targets:
                    new_names = _assigned_names(target) - root_names
                    if new_names:
                        root_names.update(new_names)
                        changed = True
    return root_names


def _forbidden_path_constructions(source: str) -> list[tuple[int, int]]:
    tree = ast.parse(source)
    root_names = _root_names(tree)
    violations: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        parts = _path_parts(node, root_names)
        if parts is None or _REPOSITORY_ROOT not in parts:
            continue
        for index in range(len(parts) - 1):
            if parts[index : index + 2] == REPOSITORY_RUN_COMPONENTS:
                violations.add((node.lineno, node.col_offset))
                break
    return sorted(violations)


def test_guard_catches_repository_root_alias_as_one_path_component() -> None:
    source = """
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "runs/current-weekly-pilot-russian"
"""
    assert _forbidden_path_constructions(source)


def test_guard_catches_repository_root_alias_as_split_path_components() -> None:
    source = """
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "runs" / "current-weekly-pilot-russian"
"""
    assert _forbidden_path_constructions(source)


def test_guard_allows_temporary_run_alias_paths() -> None:
    source = """
def build_state(tmp_path):
    return tmp_path / "runs" / "current-weekly-pilot-russian"
"""
    assert _forbidden_path_constructions(source) == []


def test_guard_allows_plain_string_alias_assertions() -> None:
    source = """
def test_launcher_text(launcher_text):
    assert "runs/current-weekly-pilot-russian" in launcher_text
"""
    assert _forbidden_path_constructions(source) == []


def test_real_tests_do_not_construct_repository_root_weekly_alias() -> None:
    tests_dir = Path(__file__).resolve().parent
    violations = [
        f"{path.relative_to(tests_dir)}:{line}:{column}"
        for path in sorted(tests_dir.rglob("test_*.py"))
        for line, column in _forbidden_path_constructions(path.read_text(encoding="utf-8"))
    ]
    assert violations == []
