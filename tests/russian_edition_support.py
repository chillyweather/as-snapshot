"""Fakes and recorded answers shared by the Russian-edition completion tests (#1181)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus

from state_store_support import read_state
from state_store_support import seed_state

from autoscout.russian_edition_completion import OUTCOME_ANSWERED
from autoscout.russian_edition_completion import OUTCOME_BLOCKED
from autoscout.russian_edition_completion import OUTCOME_FAILED
from autoscout.russian_edition_completion import SourceAnswer
from autoscout.russian_edition_completion import run_russian_edition_completion
from autoscout.russian_edition_sources import SOURCE_LITRES
from autoscout.russian_edition_sources import SOURCE_LIVELIB
from autoscout.russian_edition_sources import SOURCE_RSL
from autoscout.russian_edition_sources import SOURCE_WIKIDATA

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "issue-1181-russian-editions"
TODAY = "2026-11-21"

LIVELIB_EMPTY = '<html><body><span class="not-found-text">По вашему запросу ничего не найдено.</span></body></html>'
LITRES_EMPTY = json.dumps({"status": 200, "error": None, "payload": {"data": []}})
RSL_EMPTY = json.dumps({"TotalHits": 0, "content": '<div class="row rsl-search-results"></div>'})
WIKIDATA_EMPTY = json.dumps({"search": [], "success": 1})


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def answered(body: str) -> SourceAnswer:
    return SourceAnswer(OUTCOME_ANSWERED, body)


BLOCKED = SourceAnswer(OUTCOME_BLOCKED)
FAILED = SourceAnswer(OUTCOME_FAILED)


def livelib_page(*results: tuple[str, str, str]) -> str:
    """A LiveLib search page in the recorded markup: (title, author, path) per result."""
    blocks = [
        f'<div class="object-wrapper object-edition ll-redirect" data-link="{path}">\n'
        f'<a href="{path}" class="title">{title}</a>\n'
        f'<a class="description" href="/author/1-x" title="{author}">{author}</a>\n</div>'
        for title, author, path in results
    ]
    return '<html><body><div id="objects-block">\n' + "\n".join(blocks) + "\n</div></body></html>"


def litres_answer(*books: dict[str, Any]) -> str:
    """A LitRes search answer in the recorded shape; each book gives title, authors and optional fields."""
    data = []
    for index, book in enumerate(books, start=1):
        persons = [{"full_name": name, "role": "author"} for name in book["authors"]]
        instance = {
            "id": 1000 + index,
            "url": f"/book/x/book-{1000 + index}/",
            "title": book["title"],
            "language_code": book.get("language", "ru"),
            "date_written_at": book.get("date_written_at"),
            "persons": persons,
        }
        data.append({"type": "text_book", "instance": instance})
    return json.dumps({"status": 200, "error": None, "payload": {"data": data}}, ensure_ascii=False)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


Answer = SourceAnswer | str | None


class FakeFetcher:
    """Answers per source, by a callable or a mapping; records every request."""

    def __init__(
        self,
        answers: Callable[[str, str], Answer] | dict[str, Answer],
        *,
        clock: FakeClock | None = None,
        step: float = 0.0,
    ) -> None:
        self.answers = answers
        self.clock = clock
        self.step = step
        self.requests: list[tuple[str, str]] = []

    def fetch(self, source: str, target: str, *, timeout: float) -> SourceAnswer:
        self.requests.append((source, target))
        if self.clock is not None:
            self.clock.now += self.step
        answer = self.answers(source, target) if callable(self.answers) else self.answers.get(source)
        if answer is None:
            return FAILED
        return answered(answer) if isinstance(answer, str) else answer

    def asked(self, source: str) -> list[str]:
        return [target for asked, target in self.requests if asked == source]


def recorded(slug: str, **overrides: Answer) -> Callable[[str, str], Answer]:
    """The recorded answers of one book; ``overrides`` replace a source's answer."""

    def answer(source: str, target: str) -> Answer:
        if source in overrides:
            return overrides[source]
        if source == SOURCE_WIKIDATA:
            kind = "wikidata-search-ru.json"
        elif source == SOURCE_LITRES and "/arts/" in target:
            kind = "litres-art-detail.json"
        else:
            kind = {
                SOURCE_LIVELIB: "livelib-search.html",
                SOURCE_RSL: "rsl-author.json",
                SOURCE_LITRES: "litres-author.json",
            }[source]
        path = FIXTURES / f"{slug}.{kind}"
        if path.exists():
            return path.read_text(encoding="utf-8")
        if source == SOURCE_WIKIDATA:
            return SourceAnswer(OUTCOME_BLOCKED, fixture("wikidata-rate-limited.txt"))
        return FAILED

    return answer


EMPTY_ANSWERS: dict[str, Answer] = {
    SOURCE_LIVELIB: LIVELIB_EMPTY,
    SOURCE_WIKIDATA: WIKIDATA_EMPTY,
    SOURCE_RSL: RSL_EMPTY,
    SOURCE_LITRES: LITRES_EMPTY,
}


def routed(routes: dict[str, str], **overrides: Answer) -> Callable[[str, str], Answer]:
    """Several books' recorded answers: a request goes to the first slug whose key its decoded target contains.

    Keys are English titles and names, or Cyrillic surnames; a request no key
    matches gets an empty answer. ``overrides`` replace a source's answer.
    """

    def answer(source: str, target: str) -> Answer:
        if source in overrides:
            return overrides[source]
        decoded = unquote_plus(target)
        slug = next((slug for key, slug in routes.items() if key in decoded), None)
        return EMPTY_ANSWERS[source] if slug is None else recorded(slug)(source, target)

    return answer


class FakeMatcher:
    """Names, per candidate id, the first numbered title starting with the given text; ``None`` names none."""

    def __init__(self, picks: dict[str, str | None] | None = None, *, result: Any = ...) -> None:
        self.picks = picks or {}
        self.result = result
        self.calls: list[list[dict[str, Any]]] = []

    def match(self, items: Any, *, timeout: float) -> Any:
        self.calls.append(json.loads(json.dumps(list(items), ensure_ascii=False)))
        if self.result is not ...:
            return self.result
        answer: dict[str, int | None] = {}
        for item in items:
            wanted = self.picks.get(item["candidate_id"])
            answer[item["candidate_id"]] = next(
                (entry["number"] for entry in item["russian_titles"] if wanted and entry["title"].startswith(wanted)),
                None,
            )
        return answer


def book(candidate_id: str, title: str, author: str, *, first_year: int | None = None, **extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "identity_key": f"{title.casefold()}|{author.casefold()}",
        "candidate_id": candidate_id,
        "title": title,
        "primary_authors": [author],
        "publisher": "Example",
        "publication_year": 2025,
        "first_seen_date": "2026-11-01",
        "russian_edition_status": "unclear",
        "content_description": "Книга о привычках и о том, как их менять.",
    }
    if first_year is not None:
        record["work_age"] = {
            "status": "found",
            "first_publish_year": first_year,
            "edition_count": 3,
            "basis": "title_author",
            "source": "open_library",
            "source_url": "https://openlibrary.org/works/OL1W",
            "lookup_date": "2026-11-19",
        }
    record.update(extra)
    return record


def workspace(
    tmp_path: Path, history: list[dict[str, Any]], current_keys: list[str] | None = None
) -> tuple[Path, Path]:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir(exist_ok=True)
    output_dir.mkdir(exist_ok=True)
    keys = current_keys if current_keys is not None else [str(record["identity_key"]) for record in history]
    seed_state(state_dir, candidate_history=history)
    seed_state(state_dir, latest_scouting_candidates=[{"identity_key": key} for key in keys])
    (output_dir / "scouting-candidates.json").write_text(
        json.dumps([{"identity_key": key} for key in keys]), encoding="utf-8"
    )
    return state_dir, output_dir


def history(state_dir: Path) -> list[dict[str, Any]]:
    return read_state(state_dir, "candidate_history")


def by_id(state_dir: Path) -> dict[str, dict[str, Any]]:
    return {str(record["candidate_id"]): record for record in history(state_dir)}


def run(state_dir: Path, output_dir: Path, **kwargs: Any) -> dict[str, Any]:
    clock = kwargs.pop("clock", None) or FakeClock()
    kwargs.setdefault("effective_date", TODAY)
    kwargs.setdefault("monotonic", clock)
    kwargs.setdefault("sleeper", clock.sleep)
    return run_russian_edition_completion(output_dir, state_dir, **kwargs)
