"""Shared offline fixtures for the description adjudication suites.

Issues #636, #637, #639, and #642 all drive the same stage: a fake transport
serves small HTML pages to the real ``BoundedHTTPFetcher``, a replay adapter
answers with a canned adjudication response, and the stage runs against a
temporary state directory. One copy of those fixtures lives here, so a
change to the page shape or to the fake transport reaches every suite at
once. Each suite keeps its own URLs, records, and assertions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from state_store_support import seed_state

from autoscout.grounded_description import BoundedHTTPError
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import HTTPResponse

OPERATION_DATE = "2026-09-14"

TITLE = "Work One"
AUTHOR = "Alex Author"
SUBJECT = "A novel about memory, society and public life in a small town."
VALID_TEXT = (
    "Книга о памяти, обществе и общественной жизни маленького города. "
    "Автор соединяет репортаж с историческим контекстом и показывает, "
    "как прошлое формирует настоящее. "
    "Редакционный крючок — тихий повседневный ракурс вместо громких обобщений."
)


def page(title: str = TITLE, author: str = AUTHOR, subject: str = SUBJECT, language: str = "") -> str:
    """Build one small book page with a schema.org record and a subject line.

    ``language`` writes the document language declaration the page makes for
    itself. An empty value writes no declaration, which is the page shape a
    host that names no language serves (issue #642).
    """
    document_tag = f'<html lang="{language}">' if language else "<html>"
    return (
        document_tag
        + "<head><title>"
        + f"{title} by {author}</title>"
        + '<script type="application/ld+json">{"@type": "Book", '
        + f'"name": "{title}", "author": {{"@type": "Person", "name": "{author}"}}, '
        + f'"description": "{subject}"}}</script>'
        + "</head><body><main><p>"
        + subject
        + "</p></main></body></html>"
    )


class FakeTransport:
    """Serve canned pages by canonical URL, or fail like a dead page.

    A URL that the page map does not hold, and a URL mapped to ``None``,
    both fail the way an unreachable page fails. Every requested URL is
    recorded, so a test can prove which pages the stage fetched.
    """

    def __init__(self, pages: dict[str, str | None]) -> None:
        import threading

        self.pages = pages
        self.requested_urls: list[str] = []
        self._lock = threading.Lock()

    def request(self, url: str, **_kwargs: Any) -> HTTPResponse:
        with self._lock:
            self.requested_urls.append(url)
        if url not in self.pages or self.pages[url] is None:
            raise BoundedHTTPError("page response was not successful")
        body = str(self.pages[url]).encode("utf-8")
        return HTTPResponse(url=url, status=200, headers={"content-type": "text/html; charset=utf-8"}, body=body)


def fetcher(pages: dict[str, str | None]) -> BoundedHTTPFetcher:
    """Return the real bounded fetcher over a fake transport."""
    return BoundedHTTPFetcher(transport=FakeTransport(pages))


def citation(
    url: str,
    host: str,
    surface_kind: str = "publisher",
    *,
    title: str = TITLE,
    authors: list[str] | None = None,
    describes: bool = True,
    note: str = "",
) -> dict[str, Any]:
    """Build one adapter citation in the exact response contract shape."""
    return {
        "url": url,
        "host": host,
        "surface_kind": surface_kind,
        "printed_title": title,
        "printed_authors": [AUTHOR] if authors is None else authors,
        "describes_this_work": describes,
        "identity_note": note,
    }


def response(
    identity_key: str,
    citations: list[dict[str, Any]],
    *,
    text: str = VALID_TEXT,
    resolved: bool = True,
    unresolved_reason: str = "",
    confidence: str = "high",
) -> dict[str, Any]:
    """Build one complete adapter response for the replay adapter."""
    return {
        "identity_key": identity_key,
        "resolved": resolved,
        "unresolved_reason": unresolved_reason,
        "confidence": confidence,
        "content_description": text,
        "citations": citations,
    }


def record(candidate_id: str, identity_key: str, description: str = "") -> dict[str, Any]:
    """Build one Candidate History record that needs description work."""
    return {
        "candidate_id": candidate_id,
        "identity_key": identity_key,
        "title": TITLE,
        "primary_authors": [AUTHOR],
        "publisher": "Example Press",
        "publication_year": "2025",
        "editor_status": "",
        "editor_note": "",
        "content_description": description,
    }


def workspace(tmp_path: Path, records: list[dict[str, Any]]) -> tuple[Path, Path]:
    """Write a temporary state directory and return it with an output directory."""
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir()
    output_dir.mkdir()
    seed_state(state_dir, candidate_history=records)
    seed_state(state_dir, candidate_visibility=[])
    return state_dir, output_dir
