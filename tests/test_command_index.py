"""The command list printed by `./scripts/help` names every command there is."""

import re
import subprocess
from pathlib import Path

from autoscout.command_index import GROUPS
from autoscout.command_index import main
from autoscout.command_index import render

REPO_ROOT = Path(__file__).resolve().parents[1]
USAGES = [command.usage for group in GROUPS for command in group.commands]


def _project_slash_commands() -> dict[str, str]:
    """Map each tracked, user-invoked skill to its usage line.

    Skills reached through a symlink are general development skills shared
    across projects; they are Git-ignored and not AutoScout commands.
    """
    commands = {}
    for skill_dir in sorted((REPO_ROOT / ".claude" / "skills").iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if skill_dir.is_symlink() or not skill_file.is_file():
            continue
        frontmatter = skill_file.read_text(encoding="utf-8").split("---")[1]
        if "disable-model-invocation: true" not in frontmatter:
            continue
        name = re.search(r"^name: (.+)$", frontmatter, re.MULTILINE)
        hint = re.search(r'^argument-hint: "(.+)"$', frontmatter, re.MULTILINE)
        assert name is not None, skill_file
        usage = f"/{name.group(1).strip()}"
        commands[usage] = f"{usage} {hint.group(1)}" if hint else usage
    return commands


def test_every_launcher_is_listed() -> None:
    launchers = {path.name for path in (REPO_ROOT / "scripts").iterdir() if path.is_file()}
    listed = {usage.split()[0].removeprefix("./scripts/") for usage in USAGES if usage.startswith("./scripts/")}
    assert listed == launchers


def test_every_project_slash_command_is_listed_with_its_arguments() -> None:
    listed = {usage for usage in USAGES if usage.startswith("/")}
    assert listed == set(_project_slash_commands().values())


def test_no_command_is_listed_twice() -> None:
    assert len(USAGES) == len(set(USAGES))


def test_every_entry_says_what_it_does_and_touches() -> None:
    for group in GROUPS:
        assert group.commands, group.title
        for command in group.commands:
            assert command.summary.strip(), command.usage
            assert command.effect.strip(), command.usage


def test_main_prints_the_list(capsys) -> None:
    assert main([]) == 0
    assert capsys.readouterr().out == render()


def test_launcher_prints_the_list_from_any_directory(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(REPO_ROOT / "scripts" / "help")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == render()
