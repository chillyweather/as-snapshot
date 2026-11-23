"""Test fixture helpers.

Each public function copies a named fixture file into a destination path so
tests can load pre-recorded inputs without embedding large inline data.

Fixture files live in subdirectories keyed by kind:

    source-pages/           Source page HTML captures
    extraction-responses/   Agent / Codex extraction JSON responses
    source-configs/         Full pipeline source configurations
    recorded-mentions/      Pre-recorded source mention lists
"""

import json
import shutil
from pathlib import Path

FIXTURES_ROOT = Path(__file__).resolve().parent


def _resolve(kind: str, name: str) -> Path:
    """Return the absolute path to *name* inside *kind* subdirectory.

    Raises FileNotFoundError with a clear message when the fixture is missing.
    """
    fixture_path = FIXTURES_ROOT / kind / name
    if not fixture_path.is_file():
        raise FileNotFoundError(
            f"Test fixture not found: {fixture_path}\n"
            f"Expected a {kind!r} fixture named {name!r} under {FIXTURES_ROOT / kind}."
        )
    return fixture_path


def copy_fixture(kind: str, name: str, dest: Path) -> Path:
    """Copy *name* from *kind* subdirectory into *dest* and return *dest*.

    Typical usage in a temp-directory test::

        page_path = copy_fixture("source-pages", "example-list.json", tmp_path / "page.json")
    """
    source = _resolve(kind, name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return dest


def load_fixture_json(kind: str, name: str) -> dict | list:
    """Load and return the parsed JSON content of *name* in *kind*."""
    source = _resolve(kind, name)
    return json.loads(source.read_text(encoding="utf-8"))


def write_fixture_json(kind: str, name: str, dest: Path) -> Path:
    """Load *name* from *kind*, write it to *dest* as pretty-printed JSON, return *dest*.

    Convenience for tests that need to write fixture content to a path
    (e.g. for config files that point at other fixture files by path).
    """
    data = load_fixture_json(kind, name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return dest
