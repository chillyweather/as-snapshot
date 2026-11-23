"""`./scripts/night-scout` runs the scout-sources skill unattended in Codex (#896)."""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / ".claude" / "skills" / "scout-sources"


def _run_launcher(tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> list[str]:
    """Run the launcher against a fake codex that records its arguments, one per line."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    record = tmp_path / "argv"
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{record}"\n', encoding="utf-8")
    fake_codex.chmod(0o755)
    result = subprocess.run(
        [str(REPO_ROOT / "scripts" / "night-scout"), *args],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}", **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return record.read_text(encoding="utf-8").splitlines()


def test_launcher_invokes_the_skill_unattended_with_web_search(tmp_path: Path) -> None:
    argv = _run_launcher(tmp_path)

    assert argv[:2] == ["--search", "exec"]
    assert argv[argv.index("--cd") + 1] == str(REPO_ROOT)
    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"
    assert "approval_policy=never" in argv
    assert argv[argv.index("--model") + 1] == "gpt-6.1-sol"
    assert "model_reasoning_effort=high" in argv
    assert argv[-1] == "$scout-sources"


def test_launcher_passes_urls_and_note_as_the_skill_arguments(tmp_path: Path) -> None:
    argv = _run_launcher(tmp_path, "https://example.es/novedades", "only the Ensayo section")

    assert argv[-1] == "$scout-sources https://example.es/novedades only the Ensayo section"


def test_launcher_model_and_effort_can_be_overridden(tmp_path: Path) -> None:
    argv = _run_launcher(tmp_path, env={"NIGHT_SCOUT_MODEL": "gpt-6-astra", "NIGHT_SCOUT_EFFORT": "medium"})

    assert argv[argv.index("--model") + 1] == "gpt-6-astra"
    assert "model_reasoning_effort=medium" in argv


def test_codex_reaches_the_same_skill_and_never_starts_it_on_its_own() -> None:
    codex_skill = REPO_ROOT / ".agents" / "skills" / "scout-sources"

    assert codex_skill.is_symlink()
    assert codex_skill.resolve() == SKILL_DIR.resolve()
    policy = (SKILL_DIR / "agents" / "openai.yaml").read_text(encoding="utf-8")
    assert policy.splitlines() == ["policy:", "  allow_implicit_invocation: false"]
