"""Check relative links and anchors between the repository's Markdown files (issue #1132).

``./scripts/check`` runs this over ``AGENTS.md`` and every Markdown file under
``docs/``. A link fails when its target file does not exist, or when it names
an anchor (``#section``) that no heading of the target Markdown file produces.
Links to web pages (``https:``, ``mailto:`` and the like) are not followed; the
check reads only local files and never touches the network.

Anchors follow GitHub's rule: the heading text in lower case, with every
character that is not a letter, a digit, a space, a hyphen or an underscore
removed, and spaces turned into hyphens. A repeated heading gets ``-1``,
``-2`` and so on. Headings and links inside fenced code blocks and inline code
do not count. A GitHub line anchor such as ``#L45-L79`` is accepted as it is.

    .venv/bin/python -m autoscout.doc_links
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

DEFAULT_PATHS = ("AGENTS.md", "docs")

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t#]*$")
_INLINE_CODE = re.compile(r"(`+).*?\1")
# [text](target) and ![alt](target), with an optional "title" after the target.
_LINK = re.compile(
    r"!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(\s*<?([^()\s<>]+(?:\([^()\s]*\))?[^()\s<>]*)>?(?:\s+\"[^\"]*\")?\s*\)"
)
_HTML_ANCHOR = re.compile(r"<a\s+(?:name|id)=\"([^\"]+)\"", re.IGNORECASE)
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
# GitHub's anchor for a line or a range of lines of a file, such as #L45-L79.
_LINE_ANCHOR = re.compile(r"^L\d+(-L\d+)?$")


@dataclass(frozen=True)
class BrokenLink:
    path: str
    line: int
    target: str
    reason: str

    def describe(self) -> str:
        return f"{self.path}:{self.line} {self.target} ({self.reason})"


def heading_slug(text: str) -> str:
    """GitHub's anchor for a heading's text, before duplicates are numbered."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # a link in a heading keeps its text
    text = text.replace("`", "").strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def _prose_lines(source: str) -> Iterator[tuple[int, str]]:
    """Every line outside fenced code blocks, numbered from 1."""
    fence: str | None = None
    for number, line in enumerate(source.splitlines(), start=1):
        match = _FENCE.match(line)
        if fence is None:
            if match:
                fence = match.group(1)
                continue
            yield number, line
        elif match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
            fence = None


def anchors_in(source: str) -> set[str]:
    """Every anchor a Markdown document provides: its headings, numbered like GitHub, and HTML anchors."""
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    for _, line in _prose_lines(source):
        anchors.update(_HTML_ANCHOR.findall(line))
        heading = _HEADING.match(line)
        if not heading:
            continue
        slug = heading_slug(heading.group(2))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def links_in(source: str) -> Iterator[tuple[int, str]]:
    """Every relative link target of a Markdown document, with its line number."""
    for number, line in _prose_lines(source):
        for match in _LINK.finditer(_INLINE_CODE.sub("", line)):
            target = match.group(1)
            if not _SCHEME.match(target):
                yield number, target


def broken_links(files: list[Path], root: Path) -> list[BrokenLink]:
    """Every relative link of ``files`` whose target file or anchor does not exist."""
    anchor_cache: dict[Path, set[str]] = {}

    def anchors_of(path: Path) -> set[str]:
        if path not in anchor_cache:
            anchor_cache[path] = anchors_in(path.read_text(encoding="utf-8"))
        return anchor_cache[path]

    broken = []
    for path in files:
        shown = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
        for line, target in links_in(path.read_text(encoding="utf-8")):
            file_part, _, anchor = target.partition("#")
            destination = (path.parent / unquote(file_part)).resolve() if file_part else path
            if not destination.exists():
                broken.append(BrokenLink(shown, line, target, "no such file"))
            elif (
                anchor
                and destination.suffix == ".md"
                and not _LINE_ANCHOR.match(anchor)
                and unquote(anchor) not in anchors_of(destination)
            ):
                broken.append(BrokenLink(shown, line, target, "no such heading"))
    return broken


def markdown_files(paths: list[str], root: Path) -> list[Path]:
    """The Markdown files named by ``paths``, each a file or a directory searched recursively."""
    found: list[Path] = []
    for name in paths:
        path = (root / name).resolve()
        found.extend(sorted(path.rglob("*.md")) if path.is_dir() else [path])
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", default=list(DEFAULT_PATHS), help="Markdown files or directories")
    args = parser.parse_args(argv)
    root = Path.cwd().resolve()
    files = markdown_files(args.paths, root)
    broken = broken_links(files, root)
    for link in broken:
        print(link.describe())
    print(f"{len(files)} Markdown files checked, {len(broken)} broken relative links")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
