"""Count the functions longer than 150 lines (issue #1020).

``./scripts/check`` runs this with ``--limit`` as a stake: the count may only
go down. Every ``def`` counts, including methods and nested functions, and a
function's length runs from its ``def`` line to its last line, so a decorator
is not counted and a nested function counts in its own right as well as in the
function around it.

Run without ``--limit`` to print the current count:

    .venv/bin/python -m autoscout.long_functions
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_FUNCTION_LINES = 150
DEFAULT_ROOT = "autoscout"


@dataclass(frozen=True)
class LongFunction:
    path: str
    line: int
    name: str
    length: int

    def describe(self) -> str:
        return f"{self.path}:{self.line} {self.name} ({self.length} lines)"


def long_functions_in_source(source: str, path: str, max_lines: int = MAX_FUNCTION_LINES) -> list[LongFunction]:
    """Every function of ``source`` whose length is over ``max_lines``."""
    found = []
    for node in ast.walk(ast.parse(source, filename=path)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            if length > max_lines:
                found.append(LongFunction(path, node.lineno, node.name, length))
    return found


def long_functions(root: Path, max_lines: int = MAX_FUNCTION_LINES) -> list[LongFunction]:
    """Every long function under ``root``, longest first."""
    found = []
    for path in sorted(root.rglob("*.py")):
        found.extend(long_functions_in_source(path.read_text(encoding="utf-8"), path.as_posix(), max_lines))
    return sorted(found, key=lambda function: (-function.length, function.path, function.line))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Count the functions over {MAX_FUNCTION_LINES} lines.")
    parser.add_argument("root", nargs="?", default=DEFAULT_ROOT, help=f"directory to scan (default: {DEFAULT_ROOT})")
    parser.add_argument("--limit", type=int, help="fail when the count is above this number")
    args = parser.parse_args(argv)

    found = long_functions(Path(args.root))
    count = f"{len(found)} functions over {MAX_FUNCTION_LINES} lines"
    if args.limit is None:
        print(count)
        return 0
    if len(found) > args.limit:
        for function in found:
            print(function.describe())
        print(
            f"The count rose above its limit of {args.limit}. Split the function you lengthened or added "
            f"into smaller ones; the limit only goes down."
        )
        print(f"{count} (limit {args.limit})")
        return 1
    if len(found) < args.limit:
        print(f"{count} (limit {args.limit}): lower the limit")
        return 0
    print(f"{count} (limit {args.limit})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
