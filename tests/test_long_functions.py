"""The long-function stake of ./scripts/check (issue #1020)."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoscout.long_functions import MAX_FUNCTION_LINES
from autoscout.long_functions import long_functions
from autoscout.long_functions import long_functions_in_source
from autoscout.long_functions import main


def _function(name: str, length: int, indent: str = "") -> str:
    """A function of exactly ``length`` lines, its ``def`` line included."""
    body = "".join(f"{indent}    x{index} = {index}\n" for index in range(length - 1))
    return f"{indent}def {name}():\n{body}"


def test_a_function_one_line_over_the_maximum_counts_and_one_at_it_does_not() -> None:
    source = _function("at_limit", MAX_FUNCTION_LINES) + _function("over_limit", MAX_FUNCTION_LINES + 1)

    found = long_functions_in_source(source, "module.py")

    assert [(function.name, function.length) for function in found] == [("over_limit", MAX_FUNCTION_LINES + 1)]


def test_methods_and_nested_functions_count() -> None:
    method = "class Holder:\n" + _function("method", MAX_FUNCTION_LINES + 1, indent="    ")
    outer = "def outer():\n" + _function("inner", MAX_FUNCTION_LINES + 1, indent="    ")
    asynchronous = "async " + _function("coroutine", MAX_FUNCTION_LINES + 1)

    found = long_functions_in_source(method + outer + asynchronous, "module.py")

    assert sorted(function.name for function in found) == ["coroutine", "inner", "method", "outer"]


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "package"
    (root / "sub").mkdir(parents=True)
    (root / "short.py").write_text(_function("short", 3), encoding="utf-8")
    (root / "sub" / "long.py").write_text(
        _function("longer", MAX_FUNCTION_LINES + 5) + _function("long", MAX_FUNCTION_LINES + 1), encoding="utf-8"
    )
    return root


def test_the_tree_is_listed_longest_first(tmp_path: Path) -> None:
    found = long_functions(_tree(tmp_path))

    assert [function.name for function in found] == ["longer", "long"]
    assert found[0].describe().endswith(f"long.py:1 longer ({MAX_FUNCTION_LINES + 5} lines)")


@pytest.mark.parametrize(
    ("limit", "code", "last_line"),
    [
        (2, 0, f"2 functions over {MAX_FUNCTION_LINES} lines (limit 2)"),
        (3, 0, f"2 functions over {MAX_FUNCTION_LINES} lines (limit 3): lower the limit"),
        (1, 1, f"2 functions over {MAX_FUNCTION_LINES} lines (limit 1)"),
    ],
)
def test_the_stake_fails_only_above_its_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], limit: int, code: int, last_line: str
) -> None:
    assert main([str(_tree(tmp_path)), "--limit", str(limit)]) == code

    lines = capsys.readouterr().out.splitlines()
    assert lines[-1] == last_line
    if code:
        assert lines[0].endswith(f"longer ({MAX_FUNCTION_LINES + 5} lines)")
        assert lines[1].endswith(f"long ({MAX_FUNCTION_LINES + 1} lines)")
    else:
        assert len(lines) == 1
