"""Build a sanitised public snapshot of this repository (issue #930).

The repository is private because it holds the editor's decisions and notes.
The snapshot is a copy of the code, tests and documentation that can be
published as a separate public repository. It is built from the working tree,
never from Git history, so nothing that was ever committed and later removed
can reach it.

The snapshot is also not meant to run as it is (issue #1192): the live source
configuration and the source research stay private, so a copy cloned and run
without a person choosing and configuring sources is not a working scout.

The build is fail-closed, in four steps:

1. Allow-list. Only tracked files under ``INCLUDED_PATHS`` are considered, and
   ``EXCLUDED_PATHS`` and ``EXCLUDED_PATTERNS`` remove known-private files
   inside them: server operations, the source research and feasibility
   reports. Under ``configs/`` only ``PUBLISHED_CONFIGS`` are published.
   Anything new is private until it is added to the list.
2. Private-data filter. Editor-authored text is read from the private state at
   build time and cut into fragments. A test fixture that contains a fragment,
   or that mentions a domain in ``LICENSED_CONTENT_DOMAINS`` (content captured
   from Publishers Weekly or Publishers Marketplace, which may not be
   republished), is dropped as a unit (its whole directory under
   ``tests/fixtures``), and so is every test module that names a dropped
   fixture or imports a dropped test module. Fixtures are retained real runs,
   so a single note or capture in one file taints the whole run.
3. Redaction. In every other file (code, docs, tests) a matching fragment is
   replaced with ``WITHHELD_NOTE`` instead, so the package stays importable.
   Infrastructure addresses are replaced with placeholders.
4. Final gate. The output is scanned again, from the output directory alone. A
   surviving fragment or address, a file outside the allow-list, or a fixture
   that mentions a licensed-content domain fails the build, so a filter bug
   cannot publish private text.

Usage::

    ./scripts/publish-showcase           # preview: build, scan, show what would change
    ./scripts/publish-showcase --push    # publish one new commit to the public repo
    python -m autoscout.showcase_snapshot --output-dir /tmp/showcase   # build only
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from autoscout.repository_root import repository_root
from autoscout.state_store import Collection
from autoscout.state_store import open_state_store

INCLUDED_PATHS: tuple[str, ...] = (
    "autoscout/",
    "tests/",
    "docs/",
    "configs/",
    "scripts/",
    "reqs/",
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "GLOSSARY.md",
    "CODING_STANDARDS.md",
    "PLAN.md",
)

# CI is not copied yet: about 430 of 15,500 tests read live state or data
# that the snapshot leaves out, so the public copy would show a red build.

# Private files inside the allow-list: server operations that name the
# search host. Publisher-specific documents (ru-sources/), live state (runs/,
# data/) and deployment files (deploy/) are outside the allow-list already.
EXCLUDED_PATHS: tuple[str, ...] = (
    "docs/nanode-search-adapter-runbook.md",
    "scripts/deploy-nanode-adapter",
    "scripts/nanode-adapter-direct",
    "scripts/nanode-adapter-set-key",
    "scripts/nanode-adapter-status",
    "scripts/nanode-adapter-tunnel",
    # CEGAL allows todostuslibros.com facts for internal analysis only, with
    # no publication (#1140): its trimmed book pages and the tests that read
    # them stay private. An entry ending in "/" excludes a whole directory.
    "tests/fixtures/issue-1140-todostuslibros/",
    "tests/test_issue_1140_portal_originals.py",
    # The rule-matching fixtures are the editor's own profile, verbatim quotes
    # included, and a sample of her Candidate History (#1165). They are
    # excluded by name, so a build without the live state cannot publish them.
    "tests/fixtures/issue-1165-rule-matching/",
    "tests/fixtures/issue-1165-rule-matching.provenance.md",
    "tests/test_issue_1165_prepared_rule_matcher.py",
    # Source research says which sources are worth configuring and why (#1192).
    "docs/research/",
    "docs/source-quality/",
)

# Private files matched by pattern rather than name: the source feasibility
# reports (#1192), 56 of them when this was written.
EXCLUDED_PATTERNS: tuple[re.Pattern[str], ...] = (re.compile(r"^docs/.*feasibility"),)

# Under configs/ only these example and demo files are published (#1192). The
# live source configuration stays private, so a copy cloned and run as it is
# does not scout; a config added later is private until it is listed here.
CONFIGS_DIR = "configs/"
PUBLISHED_CONFIGS: frozenset[str] = frozenset(
    {
        "configs/mvp-sources.example.json",
        "configs/bookpage-demo.json",
        "configs/rsl-demo.json",
        "configs/weekly-mvp-fixture.json",
    }
)

# Content captured from these sites may not be republished: PW's permission
# (#935) forbids sharing outside the organisation, and PM content arrives in
# members' emails under a written permission. A fixture that mentions one is dropped as a unit (#1192).
LICENSED_CONTENT_DOMAINS: tuple[str, ...] = ("publishersweekly.com", "publishersmarketplace.com")

# Private state that holds the editor's own words, relative to the repository.
# It is read through the State Store, so the redaction applies whichever
# backend holds it (#1071).
PRIVATE_STATE_DIR = "runs/current-weekly-pilot-russian"
PRIVATE_TEXT_KEYS: dict[Collection, frozenset[str]] = {
    Collection.CANDIDATE_HISTORY: frozenset({"editor_note", "editorial_profile_notes"}),
    Collection.EXCLUDED_CANDIDATE_HISTORY: frozenset({"editor_note", "editorial_profile_notes"}),
    Collection.ARCHIVED_EDITOR_ROWS: frozenset({"Editor Note", "editor_note"}),
    Collection.PENDING_EDITOR_ROWS: frozenset({"Editor Note", "editor_note"}),
    Collection.EDITORIAL_PROFILE: frozenset({"verbatim_quote", "description", "subject"}),
}

# Shorter fragments are common phrases that say nothing about the editor and
# would also match product strings ("проверено по всем категориям").
MIN_FRAGMENT_LENGTH = 30
WITHHELD_NOTE = "[editor note withheld]"

REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[A-Za-z0-9-]+\.tail[0-9a-f]+\.ts\.net"), "search-adapter.example.internal"),
    (re.compile(r"\b45\.33\.67\.98\b"), "203.0.113.10"),
)

FIXTURES_DIR = "tests/fixtures/"
GITLEAKS_CONFIG = ".github/showcase-gitleaks.toml"
DEFAULT_TARGET = "git@github.com:chillyweather/as-snapshot.git"
BANNER_FILE = "SNAPSHOT.md"


README_POINTER = (
    "> **Public snapshot.** This copy is generated from a private repository and leaves out the "
    "editor's data. See [SNAPSHOT.md](SNAPSHOT.md).\n\n"
)


# The public README describes the market-screening constraint without naming
# the restricted subjects. The private README keeps the full wording. The
# final gate fails if a rewrite stops matching, so a README edit cannot
# publish the original line by accident.
PUBLIC_README_REWRITES: tuple[tuple[str, str], ...] = (
    (
        "The tool therefore screens for publicly visible signals of those subjects: the war in Ukraine, "
        "LGBT subject matter and gender transition, and a small number of other categories fixed in the "
        "configuration.",
        "The tool therefore screens public metadata for signals of a short list of restricted categories "
        "fixed in the configuration.",
    ),
)
PUBLIC_README_FORBIDDEN: tuple[str, ...] = ("LGBT", "gender transition")


class SnapshotLeakError(RuntimeError):
    """The final gate found private text in the snapshot."""


@dataclass
class SnapshotReport:
    source_commit: str
    copied: list[str] = field(default_factory=list)
    dropped: dict[str, str] = field(default_factory=dict)
    redacted: list[str] = field(default_factory=list)

    @property
    def dropped_test_modules(self) -> int:
        """How many top-level test modules the snapshot leaves out."""
        return sum(1 for name in self.dropped if _test_module_name(name))

    def summary(self) -> str:
        return (
            f"{len(self.copied)} copied, {len(self.dropped)} dropped "
            f"({self.dropped_test_modules} test modules), {len(self.redacted)} redacted"
        )


def normalise(text: str) -> str:
    """Fold case, HTML entities and whitespace so formatting cannot hide a match."""
    return " ".join(html.unescape(text).casefold().split())


def fragments_of(text: str) -> set[str]:
    """Cut editor text into sentence fragments long enough to be specific."""
    pieces = re.split(r"[.!?;\n\r…«»\"“”]+", text)
    return {normalise(piece) for piece in pieces if len(normalise(piece)) >= MIN_FRAGMENT_LENGTH}


def _strings_under_keys(value: object, keys: frozenset[str], inside: bool = False) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _strings_under_keys(item, keys, inside or key in keys)
    elif isinstance(value, list):
        for item in value:
            yield from _strings_under_keys(item, keys, inside)
    elif isinstance(value, str) and inside:
        yield value


def private_fragments(repo_root: Path) -> set[str]:
    """Collect fragments of every editor-authored text in the private state."""
    store = open_state_store(repo_root / PRIVATE_STATE_DIR)
    fragments: set[str] = set()
    for collection, keys in PRIVATE_TEXT_KEYS.items():
        data = store.read(collection)
        for text in _strings_under_keys(data, keys):
            fragments |= fragments_of(text)
    return fragments


def searchable_text(path: Path) -> str | None:
    """Return the file as normalised text, with JSON escapes decoded; None for binary files."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None
    text = raw
    if path.suffix == ".json":
        try:
            text = raw + "\n" + json.dumps(json.loads(raw), ensure_ascii=False)
        except json.JSONDecodeError:
            pass
    return normalise(text)


def contains_fragment(text: str, fragments: set[str]) -> str | None:
    for fragment in fragments:
        if fragment in text:
            return fragment
    return None


def tracked_files(repo_root: Path) -> list[str]:
    output = subprocess.run(["git", "ls-files", "-z"], cwd=repo_root, check=True, capture_output=True).stdout.decode(
        "utf-8"
    )
    return sorted(name for name in output.split("\0") if name)


def is_allowed(relative: str) -> bool:
    if any(relative == entry or (entry.endswith("/") and relative.startswith(entry)) for entry in EXCLUDED_PATHS):
        return False
    if any(pattern.search(relative) for pattern in EXCLUDED_PATTERNS):
        return False
    if relative.startswith(CONFIGS_DIR) and relative not in PUBLISHED_CONFIGS:
        return False
    return any(relative == entry or (entry.endswith("/") and relative.startswith(entry)) for entry in INCLUDED_PATHS)


def fixture_unit(relative: str) -> str | None:
    """Name the fixture a file belongs to: its first path part below tests/fixtures/."""
    if not relative.startswith(FIXTURES_DIR):
        return None
    rest = relative[len(FIXTURES_DIR) :]
    return rest.split("/", 1)[0] if "/" in rest else rest


def licensed_domain_in(text: str) -> str | None:
    """Name the licensed-content domain a normalised text mentions, if any."""
    for domain in LICENSED_CONTENT_DOMAINS:
        if domain in text:
            return domain
    return None


def fixture_drop_reason(relative: str, text: str | None, fragments: set[str]) -> str | None:
    """Say why a fixture file makes its whole fixture private, or None."""
    if text is None or not fixture_unit(relative):
        return None
    if contains_fragment(text, fragments):
        return "fixture contains editor-authored text"
    if licensed_domain_in(text):
        return "fixture contains Publishers Weekly or Publishers Marketplace content"
    return None


def allowed_files(repo_root: Path) -> list[str]:
    """List the tracked files the allow-list lets into the snapshot."""
    return [name for name in tracked_files(repo_root) if is_allowed(name) and (repo_root / name).is_file()]


def _test_module_name(relative: str) -> str | None:
    if relative.startswith("tests/") and relative.endswith(".py") and "/" not in relative[len("tests/") :]:
        return Path(relative).stem
    return None


def select_files(repo_root: Path, candidates: Sequence[str], fragments: set[str]) -> tuple[list[str], dict[str, str]]:
    """Split allowed files into kept and dropped, with a reason for every drop."""
    dropped: dict[str, str] = {}
    texts: dict[str, str | None] = {name: searchable_text(repo_root / name) for name in candidates}

    for name, text in texts.items():
        if (reason := fixture_drop_reason(name, text, fragments)) is not None:
            dropped[name] = reason

    dropped_units = {unit for name in dropped if (unit := fixture_unit(name))}
    for name in candidates:
        unit = fixture_unit(name)
        if unit in dropped_units and name not in dropped:
            dropped[name] = f"part of dropped fixture {unit}"

    # A test module that names a dropped fixture, or imports a dropped test
    # module, cannot run without it. Repeat until nothing more falls.
    changed = True
    while changed:
        changed = False
        dropped_modules = {module for name in dropped if (module := _test_module_name(name))}
        for name in candidates:
            if name in dropped or not name.startswith("tests/") or not name.endswith(".py"):
                continue
            source = (repo_root / name).read_text(encoding="utf-8")
            reason = None
            for unit in dropped_units:
                if unit in source:
                    reason = f"uses dropped fixture {unit}"
                    break
            if reason is None:
                for module in dropped_modules:
                    if re.search(rf"^\s*(from|import)\s+(tests\.)?{re.escape(module)}\b", source, re.MULTILINE):
                        reason = f"imports dropped module {module}"
                        break
            if reason is not None:
                dropped[name] = reason
                changed = True

    kept = [name for name in candidates if name not in dropped]
    return kept, dropped


def fragment_pattern(fragment: str) -> re.Pattern[str]:
    """Match a normalised fragment in raw text, whatever its case and spacing."""
    return re.compile(r"\s+".join(re.escape(word) for word in fragment.split()), re.IGNORECASE)


def redact(text: str, fragments: set[str]) -> str:
    folded = normalise(text)
    for fragment in sorted(fragments, key=len, reverse=True):
        if fragment in folded:
            text = fragment_pattern(fragment).sub(WITHHELD_NOTE, text)
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def final_gate(output_dir: Path, fragments: set[str]) -> None:
    """Fail if any private file, fragment or infrastructure address survived."""
    problems: list[str] = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file() or ".git" in path.relative_to(output_dir).parts:
            continue
        relative = path.relative_to(output_dir).as_posix()
        if relative != BANNER_FILE and not is_allowed(relative):
            problems.append(f"{relative}: outside the published allow-list")
        text = searchable_text(path)
        if text is None:
            continue
        if fixture_unit(relative) and (domain := licensed_domain_in(text)):
            problems.append(f"{relative}: fixture with licensed content ({domain})")
        if contains_fragment(text, fragments):
            problems.append(f"{relative}: editor-authored text")
        raw = path.read_text(encoding="utf-8")
        if any(pattern.search(raw) for pattern, _ in REDACTIONS):
            problems.append(f"{relative}: infrastructure address")
    readme = output_dir / "README.md"
    if readme.is_file():
        readme_text = readme.read_text(encoding="utf-8").casefold()
        for term in PUBLIC_README_FORBIDDEN:
            if term.casefold() in readme_text:
                problems.append(f"README.md: names a restricted subject ({term}); update PUBLIC_README_REWRITES")
    if problems:
        raise SnapshotLeakError("private data in snapshot:\n  " + "\n  ".join(problems))


def banner(report: SnapshotReport) -> str:
    return (
        "# About this repository\n\n"
        "This is a public snapshot of AutoScout, a private tool in weekly production use with a "
        "real book editor. It is regenerated automatically from the private repository on every "
        "change to `main`.\n\n"
        f"Source commit: `{report.source_commit}`\n\n"
        "To protect the editor's privacy, the snapshot leaves out live scouting state, the "
        "editor's decisions and notes, server operations, and the test fixtures and tests that "
        f"depend on real editorial data ({len(report.dropped)} files). Quotes of the editor's "
        f"notes elsewhere are replaced with `{WITHHELD_NOTE}`. Everything else is copied as it is.\n\n"
        "The live source configuration and the source research are also withheld, and test "
        "fixtures that hold licensed third-party content are left out with the tests that use "
        "them. This copy shows how the tool is built; it is not meant to run as it is.\n"
    )


def build_snapshot(repo_root: Path, output_dir: Path, source_commit: str | None = None) -> SnapshotReport:
    repo_root = repo_root.resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    commit = (
        source_commit
        or subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, check=True, capture_output=True, text=True
        ).stdout.strip()
    )
    report = SnapshotReport(source_commit=commit)

    fragments = private_fragments(repo_root)
    allowed = allowed_files(repo_root)
    kept, report.dropped = select_files(repo_root, allowed, fragments)

    for name in kept:
        source = repo_root / name
        target = output_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = source.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            shutil.copy2(source, target)
        else:
            redacted = redact(text, fragments)
            if redacted != text:
                report.redacted.append(name)
            target.write_text(redacted, encoding="utf-8")
            shutil.copymode(source, target)
        report.copied.append(name)

    (output_dir / BANNER_FILE).write_text(banner(report), encoding="utf-8")
    readme = output_dir / "README.md"
    if readme.is_file():
        # The private CI badge renders as a broken image for public readers.
        lines = [
            line
            for line in readme.read_text(encoding="utf-8").splitlines(keepends=True)
            if not line.startswith("[![CI]")
        ]
        text = "".join(lines)
        for original, public in PUBLIC_README_REWRITES:
            text = text.replace(original, public)
        readme.write_text(README_POINTER + text, encoding="utf-8")
    final_gate(output_dir, fragments)
    return report


class PublishRefusedError(RuntimeError):
    """Publishing stopped before anything was pushed."""


def _git(args: Sequence[str], cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def secret_scan(snapshot_dir: Path, repo_root: Path) -> str:
    """Run gitleaks over the snapshot when it is installed; say what happened."""
    gitleaks = shutil.which("gitleaks")
    if gitleaks is None:
        return "gitleaks not installed: secret scan skipped (brew install gitleaks)"
    result = subprocess.run(
        [gitleaks, "dir", str(snapshot_dir), "--config", str(repo_root / GITLEAKS_CONFIG), "--redact", "--no-banner"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PublishRefusedError("gitleaks found possible secrets:\n" + result.stdout + result.stderr)
    return "gitleaks: no secrets found"


def publish_snapshot(repo_root: Path, target: str, workdir: Path, push: bool) -> list[str]:
    """Build the snapshot and sync it into a clone of the public repository.

    Without ``push`` this is a preview: the clone is updated locally and the
    change summary is returned, but nothing is committed or pushed.
    """
    repo_root = repo_root.resolve()
    if _git(["status", "--porcelain", "--untracked-files=no"], repo_root).strip():
        raise PublishRefusedError("tracked files have uncommitted changes; commit or stash them first")

    lines: list[str] = []
    snapshot_dir = workdir / "snapshot"
    report = build_snapshot(repo_root, snapshot_dir)
    lines.append(f"built {report.source_commit[:12]}: {report.summary()}")
    lines.append(secret_scan(snapshot_dir, repo_root))

    clone = workdir / "target"
    if clone.exists():
        shutil.rmtree(clone)
    subprocess.run(["git", "clone", "-q", "--depth", "1", target, str(clone)], check=True, capture_output=True)
    _git(["checkout", "-q", "-B", "main"], clone)
    for entry in clone.iterdir():
        if entry.name != ".git":
            shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
    shutil.copytree(snapshot_dir, clone, dirs_exist_ok=True, symlinks=True)
    _git(["add", "-A"], clone)

    changes = _git(["diff", "--cached", "--shortstat"], clone).strip()
    if not changes:
        lines.append("public repository already matches this snapshot; nothing to publish")
        return lines
    lines.append(f"changes: {changes}")
    if not push:
        lines.append(f"preview only: nothing pushed. Inspect {clone}, then rerun with --push")
        return lines
    _git(["commit", "-q", "-m", f"Snapshot of {report.source_commit[:12]}"], clone)
    _git(["push", "-q", "origin", "main"], clone)
    lines.append(f"pushed to {target}")
    return lines


def _write_report(path: Path, report: SnapshotReport) -> None:
    payload = {
        "source_commit": report.source_commit,
        "dropped_test_modules": report.dropped_test_modules,
        "dropped": report.dropped,
        "redacted": report.redacted,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build, and optionally publish, a sanitised public snapshot.")
    parser.add_argument("--repo-root", type=Path, default=repository_root())
    parser.add_argument("--output-dir", type=Path, help="build the snapshot into this directory and stop")
    parser.add_argument("--report", type=Path, help="with --output-dir: write the drop and redaction report as JSON")
    parser.add_argument(
        "--publish",
        metavar="GIT_URL",
        nargs="?",
        const=DEFAULT_TARGET,
        help=f"sync the snapshot into a public repository (default {DEFAULT_TARGET})",
    )
    parser.add_argument("--workdir", type=Path, help="with --publish: working directory (default: a temporary one)")
    parser.add_argument("--push", action="store_true", help="with --publish: commit and push; without it, preview")
    args = parser.parse_args(argv)
    if (args.output_dir is None) == (args.publish is None):
        parser.error("give exactly one of --output-dir or --publish")

    try:
        if args.publish:
            workdir = args.workdir or Path(tempfile.mkdtemp(prefix="autoscout-showcase-"))
            for line in publish_snapshot(args.repo_root, args.publish, workdir, args.push):
                print(f"showcase: {line}")
            return 0
        report = build_snapshot(args.repo_root, args.output_dir)
    except (SnapshotLeakError, PublishRefusedError) as error:
        print(f"showcase: FAILED\n{error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(f"showcase: FAILED: {' '.join(error.cmd)}\n{error.stderr or ''}", file=sys.stderr)
        return 1
    if args.report:
        _write_report(args.report, report)
    print(f"showcase: {report.summary()} -> {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
