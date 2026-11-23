"""The Markdown link check of ./scripts/check (issue #1132)."""

from __future__ import annotations

from pathlib import Path

from autoscout.doc_links import anchors_in
from autoscout.doc_links import broken_links
from autoscout.doc_links import heading_slug
from autoscout.doc_links import main


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_heading_slugs_follow_githubs_rule() -> None:
    assert heading_slug("Doctor diagnostics (read-only)") == "doctor-diagnostics-read-only"
    assert heading_slug("Level 1 — Learning (preview → confirm → acknowledge)") == (
        "level-1--learning-preview--confirm--acknowledge"
    )
    assert heading_slug("Authorized recovery (`doctor --recover`) (#319)") == "authorized-recovery-doctor---recover-319"
    assert heading_slug("Проверка «описаний»") == "проверка-описаний"


def test_repeated_headings_are_numbered_and_fenced_headings_do_not_count() -> None:
    source = "# Title\n\n## Output\n\n```bash\n# not a heading\n```\n\n## Output\n"

    assert anchors_in(source) == {"title", "output", "output-1"}


def test_a_missing_file_and_a_missing_heading_are_reported(tmp_path: Path) -> None:
    _write(tmp_path, "docs/target.md", "# Target\n\n## Present\n")
    source = _write(
        tmp_path,
        "docs/source.md",
        "[ok](target.md#present) [gone](missing.md) [bad](target.md#absent) [self](#nowhere)\n"
        "[web](https://example.com/x.md) [line](target.md#L4-L9) `[code](missing.md)`\n"
        "```\n[fenced](missing.md)\n```\n",
    )

    broken = broken_links([source], tmp_path)

    assert [(link.target, link.reason) for link in broken] == [
        ("missing.md", "no such file"),
        ("target.md#absent", "no such heading"),
        ("#nowhere", "no such heading"),
    ]


def test_main_fails_on_a_broken_link_and_passes_without_one(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "docs/a.md", "# A\n\n[b](sub/b.md#b)\n")
    _write(tmp_path, "docs/sub/b.md", "# B\n\n[a](../a.md)\n")
    _write(tmp_path, "AGENTS.md", "[a](docs/a.md#a)\n")

    assert main([]) == 0
    assert "3 Markdown files checked, 0 broken relative links" in capsys.readouterr().out

    _write(tmp_path, "docs/sub/b.md", "# B\n\n[a](../a.md#gone)\n")

    assert main([]) == 1
    assert "docs/sub/b.md:3 ../a.md#gone (no such heading)" in capsys.readouterr().out
