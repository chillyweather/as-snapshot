"""Integration checks for the local CI launcher."""

import os
import subprocess
from pathlib import Path


def test_local_gate_rejects_an_unrelated_console_script(tmp_path: Path) -> None:
    """A PATH entry from another install cannot stand in for this interpreter's script."""
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    launcher = scripts / "check"
    launcher.write_bytes((Path(__file__).resolve().parents[1] / "scripts" / "check").read_bytes())
    launcher.chmod(0o755)

    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_python = venv_bin / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then\n'
        '  case "$2" in\n'
        '    *autoscout.__file__*) printf "%s\\n" "$MOCK_REPO_ROOT";;\n'
        '    *sysconfig.get_path*) printf "%s\\n" "$MOCK_REPO_ROOT/.venv/bin/autoscout";;\n'
        "  esac\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    unrelated_bin = tmp_path / "unrelated-bin"
    unrelated_bin.mkdir()
    unrelated_script = unrelated_bin / "autoscout"
    unrelated_script.write_text('#!/bin/sh\ntouch "$MOCK_MARKER"\n', encoding="utf-8")
    unrelated_script.chmod(0o755)
    marker = tmp_path / "wrong-script-ran"

    result = subprocess.run(
        [str(launcher)],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{unrelated_bin}:{os.environ['PATH']}",
            "MOCK_REPO_ROOT": str(repo),
            "MOCK_MARKER": str(marker),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "console script not found for the selected interpreter" in result.stderr
    assert not marker.exists()
