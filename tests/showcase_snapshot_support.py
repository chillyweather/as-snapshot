"""A throwaway repository for the showcase snapshot tests (#930, #1192).

The repository holds one private note in its state, a fixture that quotes it,
a synthetic fixture, and the tests that use each.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from state_store_support import sync_state

NOTE = "Книга интересная, но права уже проданы другому издательству в прошлом году."
PROFILE_QUOTE = "Мемуары неизвестных людей у нас почти никогда не продаются хорошо."
TAILNET_HOST = "search-adapter.example.internal"


def write_file(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def snapshot_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    state = "runs/current-weekly-pilot-russian"
    write_file(
        root,
        f"{state}/candidate-history.json",
        json.dumps([{"identity_key": "a|b", "editor_status": "reject", "editor_note": NOTE}], ensure_ascii=False),
    )
    write_file(
        root,
        f"{state}/editorial-profile.json",
        # A valid profile: the State Store refuses a malformed one (#1071).
        json.dumps(
            {
                "entries": [
                    {
                        "id": "taste-0001",
                        "kind": "soft_preference",
                        "scope": "topic",
                        "subject": "memoirs",
                        "verbatim_quote": PROFILE_QUOTE,
                        "provenance": "conversation",
                        "created_at": "2026-11-01",
                        "created_by": "editor",
                        "status": "active",
                    }
                ]
            },
            ensure_ascii=True,
        ),
    )
    write_file(root, "autoscout/__init__.py", "")
    write_file(root, "autoscout/audit.py", f'EXAMPLE = "{NOTE}"\nOTHER = "kept"\n')
    write_file(root, "README.md", "# Tool\n")
    write_file(root, "docs/ops.md", f"Endpoint: http://{TAILNET_HOST}:8899/search\n")
    write_file(root, "docs/research.md", f"She said: «{PROFILE_QUOTE}»\n")
    write_file(root, "tests/fixtures/real-run/history.json", json.dumps([{"editor_note": NOTE}], ensure_ascii=True))
    write_file(root, "tests/fixtures/real-run/capture.html", "<p>public page</p>\n")
    write_file(root, "tests/fixtures/synthetic/history.json", '[{"editor_note": "Fake note."}]\n')
    write_file(root, "tests/real_run_support.py", 'FIXTURE = "tests/fixtures/real-run"\n')
    write_file(root, "tests/test_real.py", "from real_run_support import FIXTURE\n")
    write_file(root, "tests/test_synthetic.py", 'FIXTURE = "tests/fixtures/synthetic"\n')
    write_file(root, "ru-sources/declaration.md", "publisher policy\n")
    sync_state(root / state)
    for command in (["git", "init", "-q"], ["git", "add", "-A"]):
        subprocess.run(command, cwd=root, check=True)
    return root


def snapshot_files(directory: Path) -> set[str]:
    return {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
