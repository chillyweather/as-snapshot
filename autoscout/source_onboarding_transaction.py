"""Staging, validation runners, and exact local persistence for onboarding."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoscout.json_io import read_json_or_default
from autoscout.safe_artifacts import SafeArtifactError
from autoscout.safe_artifacts import publish_bundle
from autoscout.source_onboarding_models import ConfigPaths
from autoscout.source_onboarding_policy import CanonicalPaths
from autoscout.source_onboarding_policy import OnboardingContext
from autoscout.source_onboarding_policy import SourceOnboardingError
from autoscout.source_onboarding_policy import error_text
from autoscout.source_onboarding_policy import is_safe_regular_file
from autoscout.source_onboarding_policy import proposal_state_dir
from autoscout.source_onboarding_policy import relative_to_repo
from autoscout.source_proposal_commands import staged_proposals_store
from autoscout.state_store import Collection
from autoscout.state_store import StateStoreError
from autoscout.state_store import open_state_store
from autoscout.state_store import replace_and_export
from autoscout.state_store.marker import BACKEND_MARKER_FILE


@dataclass(frozen=True)
class OnboardingRunners:
    """Validation adapters with an injection seam for deterministic tests."""

    replay: Callable[[Path, Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]
    contracts: Callable[[Path, Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]
    full_suite: Callable[[Path], Mapping[str, Any]]


@dataclass(frozen=True)
class StagedPaths:
    """Repository-relative paths inside one temporary onboarding copy."""

    root: Path
    proposal_file: Path
    config_paths: ConfigPaths
    probe_evidence: Path
    helper_input: Path
    helper_metadata: Path
    page_fixture: Path
    extraction_fixture: Path
    feasibility_input: Path
    feasibility_doc: Path
    source_specific_test: Path | None
    onboarding_artifacts: tuple[Path, ...] = ()

    @property
    def mapping(self) -> dict[str, Any]:
        return {
            "proposal_file": self.proposal_file,
            "config_paths": self.config_paths,
            "probe_evidence": self.probe_evidence,
            "page_fixture": self.page_fixture,
            "extraction_fixture": self.extraction_fixture,
            "helper_metadata": self.helper_metadata,
            "feasibility_doc": self.feasibility_doc,
            "source_specific_test": self.source_specific_test,
            "onboarding_artifacts": self.onboarding_artifacts,
            "helper_input": self.helper_input,
            "feasibility_input": self.feasibility_input,
        }


def stage_copy(repo_root: Path) -> Path:
    temporary = Path(tempfile.mkdtemp(prefix="autoscout-add-source-"))

    def ignore(directory: str, names: list[str]) -> set[str]:
        del directory
        # The stage keeps its state as the JSON it copied: the state database
        # is not copied, so a backend marker would point the stage at a
        # database it does not have (#1093).
        ignored = {".git", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache", BACKEND_MARKER_FILE}
        return {name for name in names if name in ignored or name.startswith(".auto-scout-")}

    shutil.copytree(repo_root, temporary, dirs_exist_ok=True, ignore=ignore, symlinks=True)
    try:
        stage_git_context(temporary)
    except BaseException:
        # A stage that cannot be prepared is never handed to a caller, so the
        # temporary directory must not outlive the failure.
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return temporary


# Git environment variables that relocate the repository, the work tree, the
# index, or the object store. A caller that sets one of these would silently
# redirect an onboarding Git command onto metadata the operation never chose,
# so they are removed before every Git call in this module (#404).
GIT_LOCATION_ENVIRONMENT_VARIABLES = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_INDEX_VERSION",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_TEMPLATE_DIR",
        "GIT_WORK_TREE",
    }
)
# Names inside the stage that Git must resolve to, and that never exist. The
# ``.auto-scout-`` prefix is the established convention for stage-only paths,
# so ``stage_copy`` never copies one and the staged suite never sees one.
_STAGE_GIT_HOME_NAME = ".auto-scout-stage-git-home"
_STAGE_GIT_HOOKS_NAME = ".auto-scout-stage-git-hooks"
_STAGE_GIT_TEMPLATE_NAME = ".auto-scout-stage-git-template"


def canonical_git_environment() -> dict[str, str]:
    """Return the host environment without repository-relocating Git variables.

    Canonical commits still need the operator's own Git identity, so global
    configuration is deliberately preserved here. Only the variables that
    could point a command at a different repository, work tree, index, or
    object store are removed, which makes ``cwd`` the single authority for
    which repository a command touches (#404)."""
    environment = dict(os.environ)
    for name in list(environment):
        if name in GIT_LOCATION_ENVIRONMENT_VARIABLES or name.startswith("GIT_CONFIG"):
            del environment[name]
    return environment


def stage_process_environment() -> dict[str, str]:
    """Return the environment for one staged validation subprocess.

    A staged validation run must never be able to reach canonical Git
    metadata, and the staged suite runs ordinary ``git`` commands of its own.
    Removing the repository-relocating variables makes the staged working
    directory the only repository any of those commands can resolve, so an
    inherited ``GIT_DIR``, ``GIT_WORK_TREE``, or ``GIT_INDEX_FILE`` cannot
    redirect a staged test onto the canonical repository (#404).

    Everything else is inherited unchanged, because the staged suite is the
    production suite and must run in the operator's normal environment."""
    return canonical_git_environment()


def stage_git_environment(stage_root: Path) -> dict[str, str]:
    """Return a fully isolated Git environment for one staged repository.

    Every ``GIT_*`` variable is dropped, so no inherited value can relocate
    the repository or inject configuration. System and global configuration
    are switched off, and ``HOME`` and ``XDG_CONFIG_HOME`` resolve to a
    stage-only path that is never created, so no user configuration file and
    no credential helper can be read. Terminal and askpass prompting are
    disabled, so a staged command can never wait for or obtain a credential
    (#404)."""
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    unreachable_home = stage_root / _STAGE_GIT_HOME_NAME
    environment.update(
        {
            "HOME": str(unreachable_home),
            "XDG_CONFIG_HOME": str(unreachable_home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
        }
    )
    return environment


def stage_git_settings(stage_root: Path) -> tuple[tuple[str, str], ...]:
    """Return the isolation settings that every staged repository carries.

    ``credential.helper`` is deliberately set to the empty value: that resets
    the inherited helper list, so no helper from system or global
    configuration can run for any command in the stage. ``core.hooksPath``
    resolves to a stage-only path that is never created, so no hook can run
    either (#404)."""
    return (
        ("user.name", "AutoScout Onboarding Stage"),
        ("user.email", "onboarding-stage@autoscout.invalid"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
        ("credential.helper", ""),
        ("core.hooksPath", str(stage_root / _STAGE_GIT_HOOKS_NAME)),
        ("core.fsmonitor", "false"),
        ("gc.auto", "0"),
        ("protocol.file.allow", "never"),
    )


def stage_git_configuration(stage_root: Path) -> tuple[str, ...]:
    """Return the explicit ``-c`` overrides for every staged Git command.

    These carry the same settings that are also written into the staged
    repository's local configuration, so a staged command is isolated both
    before the local configuration exists and after it does (#404)."""
    overrides: list[str] = []
    for name, value in stage_git_settings(stage_root):
        overrides.extend(("-c", f"{name}={value}"))
    return tuple(overrides)


def staged_git(stage_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one Git command that can only ever touch the staged repository."""
    command = ["git", *stage_git_configuration(stage_root), *args]
    return subprocess.run(
        command,
        cwd=stage_root,
        check=False,
        capture_output=True,
        text=True,
        env=stage_git_environment(stage_root),
    )


def stage_git_context(stage_root: Path) -> None:
    """Give the staged copy its own isolated throwaway Git repository (#404).

    The canonical ``.git`` directory is deliberately not copied, so the stage
    starts without any Git context. Repository tests that assert Git facts -
    ``git check-ignore`` on a derived output path, or a clean ``git status``
    around a launcher call - then fail for a reason that has nothing to do
    with the source being onboarded, and the confirmed operation can never
    reach persistence.

    A throwaway repository restores those facts without dropping coverage:
    ``.gitignore`` was copied with the tree, and one commit makes the staged
    working tree clean, exactly like the canonical repository at the moment
    the operator runs the command.

    The repository is created in isolation. It inherits no system, global, or
    user Git configuration, no credential helper, and no hook; it has no
    remote, no worktree registration, no alternate object store, and no
    access to canonical Git metadata; and it is removed with the stage. The
    template directory is a stage-only path that is never created, so not
    even a sample hook is installed.

    The same isolation settings are written into the staged repository's own
    local configuration, so every later Git command inside the stage - the
    staged test suite included - is isolated too, whatever environment it
    runs with."""
    settings = tuple(("config", "--local", name, value) for name, value in stage_git_settings(stage_root))
    steps: tuple[tuple[str, ...], ...] = (
        ("init", "-q", f"--template={stage_root / _STAGE_GIT_TEMPLATE_NAME}"),
        *settings,
        ("add", "-A"),
        ("commit", "-q", "--no-verify", "-m", "staged onboarding validation baseline"),
    )
    for step in steps:
        result = staged_git(stage_root, *step)
        if result.returncode != 0:
            detail = f"git {step[0]} {step[2]}" if step[0] == "config" else f"git {step[0]}"
            raise SourceOnboardingError(
                "validation",
                "stage_git_context_failed",
                f"cannot prepare the staged Git context: {detail} failed",
            )


def stage_input(stage_root: Path, path: Path, label: str, repo_root: Path) -> Path:
    try:
        relative = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
        relative = Path(".auto-scout-onboarding-inputs") / f"{digest}-{path.name}"
    destination = stage_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(path, destination)
    except OSError as exc:
        raise SourceOnboardingError("inputs", "input_copy_failed", f"cannot copy {label}: {error_text(exc)}") from exc
    return destination


def stage_paths(canonical: CanonicalPaths, stage_root: Path, inputs: Mapping[str, Path]) -> StagedPaths:
    def staged(path: Path) -> Path:
        return stage_root / relative_to_repo(path, canonical.repo_root)

    return StagedPaths(
        root=stage_root,
        proposal_file=staged(canonical.proposal_file),
        config_paths=ConfigPaths(*(staged(path) for path in canonical.config_paths.all)),
        probe_evidence=inputs["probe_evidence"],
        helper_input=inputs["helper_input"],
        helper_metadata=staged(canonical.helper_metadata),
        page_fixture=staged(canonical.page_fixture),
        extraction_fixture=staged(canonical.extraction_fixture),
        feasibility_input=inputs["feasibility_input"],
        feasibility_doc=staged(canonical.feasibility_doc),
        source_specific_test=staged(canonical.source_specific_test) if canonical.source_specific_test else None,
        onboarding_artifacts=tuple(staged(path) for path in canonical.onboarding_artifacts),
    )


def copy_exact_to_stage(source: Path, destination: Path, label: str) -> None:
    content = source.read_bytes()
    if destination.exists() and destination.read_bytes() != content:
        raise SourceOnboardingError("inputs", f"{label}_conflict", f"existing {label} differs from supplied bytes")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        destination.write_bytes(content)


_REPLAY_DIAGNOSTIC_MAX_TEXT = 160
_REPLAY_DIAGNOSTIC_MAX_DROP_REASONS = 5
_REPLAY_SENSITIVE_TEXT = re.compile(
    r"(?:<[^>]*>|(?:^|[\s=])(?:/|\\\\|\.\.?/)|/(?:private|tmp|Users|var|home|etc|opt)/|"
    r"https?://|(?:api[_ -]?key|credential|password|token))",
    re.IGNORECASE,
)


def _bounded_replay_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if not text or _REPLAY_SENSITIVE_TEXT.search(text):
        return "[redacted]" if text else ""
    return text[:_REPLAY_DIAGNOSTIC_MAX_TEXT]


def _bounded_replay_count(*values: Any) -> int:
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def _bounded_drop_reasons(source_run: Mapping[str, Any], debug_metadata: Mapping[str, Any]) -> list[str]:
    raw_reasons = source_run.get("drop_reasons", debug_metadata.get("drop_reasons", []))
    if not isinstance(raw_reasons, list):
        return []
    reasons: list[str] = []
    for item in raw_reasons[:_REPLAY_DIAGNOSTIC_MAX_DROP_REASONS]:
        value = item.get("reason", "") if isinstance(item, Mapping) else item
        reason = _bounded_replay_text(value)
        if reason:
            reasons.append(reason)
    return reasons


def _replay_source_diagnostics(source_run: Mapping[str, Any]) -> dict[str, Any]:
    debug_value = source_run.get("debug_metadata", {})
    debug_metadata = debug_value if isinstance(debug_value, Mapping) else {}
    return {
        "source_run_status": _bounded_replay_text(source_run.get("status", "")),
        "source_run_reason": _bounded_replay_text(source_run.get("reason", "")),
        "valid_mention_count": _bounded_replay_count(
            source_run.get("valid_mention_count"), debug_metadata.get("validated_mention_count")
        ),
        "skipped_item_count": _bounded_replay_count(
            source_run.get("skipped_item_count"), debug_metadata.get("skipped_item_count")
        ),
        "safe_failure_status": _bounded_replay_text(
            debug_metadata.get("safe_failure_status", source_run.get("safe_failure_status", ""))
        ),
        "drop_reasons": _bounded_drop_reasons(source_run, debug_metadata),
    }


def default_replay_runner(stage_root: Path, paths: Mapping[str, Any], source: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        from autoscout.cli.weekly import run_mvp

        output_dir = stage_root / "tmp" / "issue-339-replay-validation"
        state_dir = stage_root / "tmp" / "issue-339-replay-validation-state"
        run_mvp(paths["config_paths"].pilot, output_dir, state_dir=state_dir)
        source_runs = read_json_or_default(output_dir / "source-runs.json", [])
        if not isinstance(source_runs, list):
            return {"status": "failed", "reason": "source-runs.json is not a list"}
        matching = [
            run for run in source_runs if isinstance(run, Mapping) and run.get("source_name") == source.get("name")
        ]
        if len(matching) != 1:
            return {
                "status": "failed",
                "reason": "onboarded source did not produce exactly one replay source-run",
                "diagnostics": {"matching_source_run_count": len(matching)},
            }
        source_run = matching[0]
        if source_run.get("status") != "collected":
            return {
                "status": "failed",
                "reason": "onboarded source did not collect in replay",
                "diagnostics": _replay_source_diagnostics(source_run),
            }
        return {"status": "passed", "source_run": source_run}
    except (OSError, ValueError, SystemExit) as exc:
        return {"status": "failed", "reason": error_text(exc)}


def pytest_result(stage_root: Path, test_paths: tuple[str, ...]) -> dict[str, Any]:
    command = [sys.executable, "-m", "pytest", *test_paths]
    result = subprocess.run(
        command,
        cwd=stage_root,
        check=False,
        capture_output=True,
        text=True,
        env=stage_process_environment(),
    )
    stdout_tail = result.stdout[-2000:]
    stderr_tail = result.stderr[-2000:]
    report: dict[str, Any] = {
        "status": "passed" if result.returncode == 0 else "failed",
        "returncode": result.returncode,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }
    if result.returncode != 0:
        report["reason"] = (stdout_tail + "\n" + stderr_tail).strip() or "pytest failed"
    return report


def default_contract_runner(stage_root: Path, paths: Mapping[str, Any], source: Mapping[str, Any]) -> Mapping[str, Any]:
    """Run the production source contract suites in the staged repository."""
    del paths, source
    return pytest_result(
        stage_root,
        ("tests/test_source_onboarding_contract.py", "tests/test_exact_book_list_source_contract.py"),
    )


def default_full_suite(stage_root: Path) -> Mapping[str, Any]:
    if not (stage_root / "tests").exists():
        return {"status": "failed", "reason": "tests directory is missing", "returncode": 1}
    return pytest_result(stage_root, ("tests",))


def default_onboarding_runners() -> OnboardingRunners:
    return OnboardingRunners(default_replay_runner, default_contract_runner, default_full_suite)


def runner_passed(result: Mapping[str, Any]) -> bool:
    """Accept only a non-failing runner result with an explicit success signal."""
    return_code = result.get("returncode")
    if return_code is not None and return_code != 0:
        return False
    if "status" not in result:
        return return_code == 0
    return str(result.get("status", "")).casefold() in {"passed", "pass", "ok", "success"}


def git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one canonical Git command with *repo_root* as the only authority."""
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        env=canonical_git_environment(),
    )


def git_bytes(repo_root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Byte-mode counterpart of :func:`git`, with the same environment policy."""
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        env=canonical_git_environment(),
    )


def status_paths(repo_root: Path, git_runner: Callable[..., subprocess.CompletedProcess[str]]) -> list[str]:
    result = git_runner(repo_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if result.returncode != 0:
        raise SourceOnboardingError("persistence", "git_status_failed", "cannot inspect repository status")
    values = [value for value in result.stdout.split("\0") if value]
    paths: list[str] = []
    index = 0
    while index < len(values):
        entry = values[index]
        path = entry[3:]
        if path:
            paths.append(path)
        if "R" in entry[:2] or "C" in entry[:2]:
            index += 1
            if index < len(values):
                paths.append(values[index])
        index += 1
    return paths


def relative_status(paths: Iterable[str], repo_root: Path) -> set[Path]:
    return {(repo_root / path).resolve() for path in paths}


_DEFAULT_PROBE_LEDGER_RELATIVE_PATHS = (
    Path("data/source-candidate-ledger.json"),
    Path("data/source-candidate-ledger.md"),
)


def allowed_probe_output_paths(repo_root: Path) -> set[Path]:
    """Validate and return only the canonical default probe ledger paths."""
    root = repo_root.resolve()
    paths = {root / relative for relative in _DEFAULT_PROBE_LEDGER_RELATIVE_PATHS}
    for path in paths:
        if not is_safe_regular_file(path, allow_missing=True):
            raise SourceOnboardingError(
                "persistence",
                "probe_output_invalid",
                "canonical probe ledger output must be a safe regular file",
            )
    return paths


def cached_paths(repo_root: Path, git_runner: Callable[..., subprocess.CompletedProcess[str]]) -> set[Path]:
    result = git_runner(repo_root, "diff", "--cached", "--name-only", "-z")
    if result.returncode != 0:
        raise SourceOnboardingError("persistence", "git_index_failed", "cannot inspect the Git index")
    return {(repo_root / value).resolve() for value in result.stdout.split("\0") if value}


def git_index_snapshot(
    repo_root: Path, git_runner: Callable[..., subprocess.CompletedProcess[str]]
) -> tuple[Path, bytes | None]:
    result = git_runner(repo_root, "rev-parse", "--git-path", "index")
    if result.returncode != 0:
        raise SourceOnboardingError("persistence", "git_index_failed", "cannot locate the Git index")
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve(), path.read_bytes() if path.exists() else None


def restore_index(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def git_preflight(
    canonical: CanonicalPaths,
    allowed_input_paths: Iterable[Path],
    *,
    git_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> set[Path]:
    run_git = git_runner or git
    repo = canonical.repo_root
    check = run_git(repo, "rev-parse", "--is-inside-work-tree")
    if check.returncode != 0 or check.stdout.strip() != "true":
        raise SourceOnboardingError("persistence", "not_a_repository", "onboarding requires a Git repository")
    branch = run_git(repo, "symbolic-ref", "--short", "HEAD")
    if branch.returncode != 0 or not branch.stdout.strip():
        raise SourceOnboardingError("persistence", "detached_head", "onboarding requires an attached branch")
    allowed = {
        canonical.proposal_file.resolve(),
        *(path.resolve() for path in allowed_input_paths),
        *allowed_probe_output_paths(repo),
    }
    dirty = relative_status(status_paths(repo, run_git), repo)
    generated = {
        *canonical.configs,
        canonical.page_fixture,
        canonical.extraction_fixture,
        canonical.helper_metadata,
        canonical.feasibility_doc,
    }
    dirty_destinations = (dirty & generated) - allowed
    if dirty_destinations:
        names = ", ".join(sorted(str(path.relative_to(repo)) for path in dirty_destinations))
        raise SourceOnboardingError(
            "persistence", "canonical_destination_dirty", f"canonical destinations are dirty: {names}"
        )
    unrelated = dirty - allowed
    if unrelated:
        names = ", ".join(sorted(str(path.relative_to(repo)) for path in unrelated))
        raise SourceOnboardingError("persistence", "unrelated_dirty_files", f"unrelated dirty files: {names}")
    return dirty


def commit_message(context: OnboardingContext) -> str:
    operation = f" operation={context.operation_id}" if context.operation_id else ""
    return f"auto-scout: onboard source {context.metadata['source_name']} ({context.onboarding_issue.reference}){operation}"


def commit_paths(
    repo_root: Path,
    commit: str,
    git_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> set[str]:
    result = git_runner(repo_root, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", commit)
    if result.returncode != 0:
        raise SourceOnboardingError("persistence", "git_history_failed", "cannot inspect the onboarding commit")
    return {value for value in result.stdout.split("\0") if value}


def existing_onboarding_commit(
    repo_root: Path,
    message: str,
    expected: Mapping[Path, bytes],
    *,
    expected_paths: set[str],
    git_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    git_bytes_runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
) -> str | None:
    """Find only an exact operation retry, while allowing earlier onboarding commits."""
    run_git = git_runner or git
    run_git_bytes = git_bytes_runner or git_bytes
    result = run_git(repo_root, "log", "--all", "--format=%H%x00%s")
    if result.returncode != 0:
        raise SourceOnboardingError("persistence", "git_log_failed", "cannot inspect onboarding history")
    for line in result.stdout.splitlines():
        commit, separator, subject = line.partition("\x00")
        if not separator or subject != message:
            continue
        committed_paths = commit_paths(repo_root, commit, run_git)
        if committed_paths != expected_paths:
            raise SourceOnboardingError(
                "persistence", "onboarding_commit_mismatch", "existing operation paths do not match"
            )
        by_relative = {
            str(path.resolve().relative_to(repo_root.resolve())): content for path, content in expected.items()
        }
        for relative in committed_paths:
            shown = run_git_bytes(repo_root, "show", f"{commit}:{relative}")
            if shown.returncode != 0 or shown.stdout != by_relative.get(relative):
                raise SourceOnboardingError(
                    "persistence", "onboarding_commit_mismatch", "existing operation content does not match"
                )
        return commit
    return None


def head_bytes(
    repo_root: Path, path: Path, git_bytes_runner: Callable[..., subprocess.CompletedProcess[bytes]]
) -> bytes | None:
    relative_path = str(path.resolve().relative_to(repo_root.resolve()))
    shown = git_bytes_runner(repo_root, "show", f"HEAD:{relative_path}")
    return shown.stdout if shown.returncode == 0 else None


def path_bytes(path: Path) -> bytes | None:
    if is_safe_regular_file(path, allow_missing=True):
        if not path.exists():
            return None
        return path.read_bytes()
    raise SourceOnboardingError("persistence", "unsafe_destination", f"destination is not a safe regular file: {path}")


def restore_snapshot(snapshot: Mapping[Path, bytes | None]) -> None:
    existing = [(path, content) for path, content in snapshot.items() if content is not None]
    if existing:
        publish_bundle([path for path, _ in existing], [content for _, content in existing])
    for path, content in snapshot.items():
        if content is None:
            path.unlink(missing_ok=True)


def snapshot_paths(paths: Iterable[Path]) -> dict[Path, bytes | None]:
    return {path: path_bytes(path) for path in paths}


def assert_snapshot_unchanged(snapshot: Mapping[Path, bytes | None], stage: str) -> None:
    for path, before in snapshot.items():
        if path_bytes(path) != before:
            raise SourceOnboardingError(
                stage, "canonical_mutation", "canonical or staged publication destination changed during validation"
            )


def publication_mapping(canonical: CanonicalPaths, staged: StagedPaths) -> dict[Path, Path]:
    generated: dict[Path, Path] = {
        canonical.proposal_file: staged.proposal_file,
        **dict(zip(canonical.config_paths.all, staged.config_paths.all, strict=True)),
        canonical.page_fixture: staged.page_fixture,
        canonical.extraction_fixture: staged.extraction_fixture,
        canonical.helper_metadata: staged.helper_metadata,
        canonical.feasibility_doc: staged.feasibility_doc,
    }
    if staged.source_specific_test is not None and canonical.source_specific_test is not None:
        generated[canonical.source_specific_test] = staged.source_specific_test
    generated.update(zip(canonical.onboarding_artifacts, staged.onboarding_artifacts, strict=True))
    return generated


@contextmanager
def _canonical_proposals(canonical: CanonicalPaths, staged: StagedPaths, *, publish: bool) -> Iterator[None]:
    """Write the accepted staged Source Proposals to canonical state through its State Store for the block (#1093).

    The staged proposals are JSON the staging wrote in the State Store's own
    form, so the canonical file the store leaves behind (the JSON backend's
    file, or the SQLite backend's export of it) holds exactly the bytes the
    onboarding fingerprinted; the caller verifies that before it commits.
    Under SQLite the database keeps the write only if the block completes:
    an exception rolls it back, and the caller restores the files.
    """
    if not publish:
        yield
        return
    completed = False
    try:
        proposals = staged_proposals_store(proposal_state_dir(staged.proposal_file)).read(Collection.SOURCE_PROPOSALS)
        store = open_state_store(proposal_state_dir(canonical.proposal_file))
        with replace_and_export(store, Collection.SOURCE_PROPOSALS, proposals):
            yield
            completed = True
    except Exception as exc:
        if completed:
            # The block made the onboarding commit, so the committed JSON holds
            # the accepted proposals and the database failed to keep them; a
            # rebuild from that JSON brings the two together again.
            raise SourceOnboardingError(
                "persistence",
                "state_database_commit_failed",
                f"the onboarding commit is made but the state database did not keep it: {error_text(exc)}",
            ) from exc
        if isinstance(exc, StateStoreError):
            raise SourceOnboardingError("persistence", "state_store_refused", error_text(exc)) from exc
        raise


def publish_and_commit(
    canonical: CanonicalPaths,
    staged: StagedPaths,
    context: OnboardingContext,
    *,
    allowed_unpublished: Iterable[Path] = (),
    git_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    run_git = git_runner or git
    run_git_bytes = git_bytes
    unpublished = {path.resolve() for path in allowed_unpublished}
    unpublished.update(allowed_probe_output_paths(canonical.repo_root))
    generated = publication_mapping(canonical, staged)
    if len(generated) != len(set(generated)):
        raise SourceOnboardingError("persistence", "duplicate_destination", "onboarding destinations are not unique")
    contents = {destination: source.read_bytes() for destination, source in generated.items()}
    message = commit_message(context)
    expected_commit = {
        path: content
        for path, content in contents.items()
        if head_bytes(canonical.repo_root, path, run_git_bytes) != content
    }
    existing = existing_onboarding_commit(
        canonical.repo_root,
        message,
        contents,
        expected_paths={str(path.relative_to(canonical.repo_root)) for path in expected_commit},
        git_runner=run_git,
        git_bytes_runner=run_git_bytes,
    )
    if existing is not None:
        dirty = relative_status(status_paths(canonical.repo_root, run_git), canonical.repo_root)
        if dirty - unpublished:
            raise SourceOnboardingError(
                "persistence", "onboarding_commit_mismatch", "existing operation has dirty destinations"
            )
        for path, content in contents.items():
            if path_bytes(path) != content:
                raise SourceOnboardingError("persistence", "onboarding_commit_mismatch", "existing files differ")
        return {"committed": False, "already_present": True, "commit_hash": existing, "paths": []}

    # A destination that already holds its exact bytes is left untouched, so a
    # declared artifact or source-specific test keeps its file mode (#664).
    destinations = [path for path in generated if path_bytes(path) != contents[path]]
    snapshot = {path: path_bytes(path) for path in destinations}
    # The accepted Source Proposals are canonical state, so they reach it
    # through the State Store and every other destination as a file (#1093).
    publishes_proposals = canonical.proposal_file in destinations
    files = [path for path in destinations if path != canonical.proposal_file]
    index_path, index_snapshot = git_index_snapshot(canonical.repo_root, run_git)
    committed = False
    try:
        with _canonical_proposals(canonical, staged, publish=publishes_proposals):
            for destination in files:
                destination.parent.mkdir(parents=True, exist_ok=True)
            if files:
                publish_bundle(files, [contents[path] for path in files])
            for destination, content in contents.items():
                if path_bytes(destination) != content:
                    raise SourceOnboardingError(
                        "persistence", "published_content_mismatch", "published content is not exact"
                    )
            dirty = relative_status(status_paths(canonical.repo_root, run_git), canonical.repo_root)
            if dirty - unpublished - set(generated):
                raise SourceOnboardingError(
                    "persistence", "unexpected_published_paths", "published paths are not exact"
                )
            changed = {
                path
                for path, content in contents.items()
                if head_bytes(canonical.repo_root, path, run_git_bytes) != content
            }
            if not changed:
                raise SourceOnboardingError(
                    "persistence", "nothing_to_commit", "onboarding bundle has no changed files"
                )
            cached_before = cached_paths(canonical.repo_root, run_git)
            if cached_before - changed:
                raise SourceOnboardingError(
                    "persistence", "preexisting_staged_paths", "Git index contains unexpected staged paths"
                )
            relative = [str(path.relative_to(canonical.repo_root)) for path in sorted(changed)]
            add = run_git(canonical.repo_root, "add", "--", *relative)
            if add.returncode != 0:
                raise SourceOnboardingError("persistence", "git_stage_failed", "cannot stage the onboarding bundle")
            cached = cached_paths(canonical.repo_root, run_git)
            if cached != changed:
                raise SourceOnboardingError("persistence", "unexpected_staged_paths", "staged paths are not exact")
            for path in changed:
                relative_path = str(path.relative_to(canonical.repo_root))
                shown = run_git_bytes(canonical.repo_root, "show", f":{relative_path}")
                if shown.returncode != 0 or shown.stdout != contents[path]:
                    raise SourceOnboardingError("persistence", "staged_content_mismatch", "staged content is not exact")
            commit = run_git(canonical.repo_root, "commit", "-m", message)
            if commit.returncode != 0:
                raise SourceOnboardingError("persistence", "git_commit_failed", "cannot commit the onboarding bundle")
            # From here the commit holds the files, so they are not restored;
            # leaving the block keeps the State Store's write too.
            committed = True
        commit_hash_result = run_git(canonical.repo_root, "rev-parse", "HEAD")
        if commit_hash_result.returncode != 0 or not commit_hash_result.stdout.strip():
            raise SourceOnboardingError("persistence", "git_commit_failed", "cannot identify the onboarding commit")
        commit_hash = commit_hash_result.stdout.strip()
        if commit_paths(canonical.repo_root, commit_hash, run_git) != {
            str(path.relative_to(canonical.repo_root)) for path in changed
        }:
            raise SourceOnboardingError("persistence", "commit_paths_mismatch", "onboarding commit paths are not exact")
        return {"committed": True, "already_present": False, "commit_hash": commit_hash, "paths": relative}
    except Exception:
        if not committed:
            try:
                restore_snapshot(snapshot)
                restore_index(index_path, index_snapshot)
            except (OSError, SafeArtifactError):
                pass
        raise
