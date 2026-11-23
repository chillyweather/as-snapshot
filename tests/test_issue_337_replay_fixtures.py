"""Tests for Issue #337: mutually grounded replay fixtures from probe evidence.

The command turns one retained probe capture (``probe-source-candidate
--keep-captures`` output) for an accepted exact-book-list Source Candidate
into a codex-capture page fixture and a codex-extraction-replay envelope,
without any live network call. Pinned here:

* Accepted retained evidence creates both fixtures; the page capture is the
  codex-capture shape and the extraction is the codex-extraction envelope
  with a deterministic operation_id and capture_sha256, and its mention
  array uses the canonical ``mentions`` key the exact-book-list contract
  reads.
* Extraction mentions carry the exact original visible titles, primary
  authors, grounded evidence URLs, the configured signal values, and the
  required configured candidate category.
* The generated page capture contains only bounded representative metadata:
  original page prose is never copied, and forbidden copied-text fields are
  never emitted.
* Every generated evidence URL is present in the generated page and passes
  exact_book_list_mention_is_grounded plus the existing collector contract
  validation; the codex-extraction-replay collector accepts the whole pair.
* The page source_url, source_page, and resolved_url all equal the requested
  URL exactly, never a retained resolved URL that differs only by a trailing
  slash.
* Malformed, non-accepted, deferred, and URL-mismatched evidence produce no
  output at all (checked against the actual deterministic output names).
* An exact retry is idempotent (``unchanged``, identical bytes); a
  conflicting retry is refused without corrupting the existing pair. The
  complete pair operation runs under a cross-process advisory pair lock, so
  concurrent writers of one destination pair serialize: at most one creates
  the pair and the surviving pair never mixes page and extraction halves.
* The CLI emits one structured JSON result and a nonzero error code on
  failure, and requires --candidate-category.
* The CLI imports and builds its parsers without any platform-specific
  module: an import hook forbids ``fcntl`` while both modules are imported
  fresh, so a Windows regression needs no Windows runner.

All fixtures are local: no live network calls.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from autoscout.__main__ import main
from autoscout.replay_fixtures import EVIDENCE_NOTE
from autoscout.replay_fixtures import MAX_REPLAY_ENTRIES
from autoscout.replay_fixtures import PROBE_COLLECTION_MODE
from autoscout.replay_fixtures import REPLAY_COLLECTION_MODE
from autoscout.replay_fixtures import ReplayFixtureError
from autoscout.replay_fixtures import _output_names
from autoscout.replay_fixtures import _ranking_primary_authors
from autoscout.replay_fixtures import build_replay_fixture_pair
from autoscout.replay_fixtures import run_capture_source_fixtures
from autoscout.replay_fixtures import write_replay_fixture_pair
from autoscout.russian_editions import russian_query_attempts
from autoscout.source_collectors import FORBIDDEN_COLLECTOR_RESPONSE_FIELDS
from autoscout.source_collectors import codex_extraction_collector_result
from autoscout.source_collectors import evidence_url_verified
from autoscout.source_collectors import extraction_contract_violation
from autoscout.source_collectors import normalized_agent_mention
from autoscout.source_page_identity import exact_book_list_mention_is_grounded
from autoscout.source_page_identity import parse_visible_page_identity
from autoscout.source_page_identity import ranking_mention_is_grounded
from autoscout.source_page_identity import ranking_rows

SOURCE_URL = "https://example.test/the-prize-announces-2025-shortlist"
SOURCE_NAME = "Example Prize Shortlist"
SIGNAL_FAMILY = "award"
SIGNAL_STRENGTH = "shortlist"
CATEGORY = "adult nonfiction"
SLUG = "example-prize-shortlist"
RUN_DATE = "2026-07-02"

ENTRIES = [
    (
        "The Revolutionists: The Story of the Extremists Who Hijacked the 1970s",
        "Jason Burke",
        "https://example.test/books-and-authors/the-revolutionists",
    ),
    (
        "How to End a Story: Collected Diaries",
        "Helen Garner",
        "https://example.test/books-and-authors/how-to-end-a-story",
    ),
    (
        "The Boundless Deep: Young Tennyson, Science and the Crisis of Belief",
        "Richard Holmes",
        "https://example.test/books-and-authors/the-boundless-deep",
    ),
    (
        "Captives and Companions: A History of Slavery and the Slave Trade in the Islamic World",
        "Justin Marozzi",
        "https://example.test/books-and-authors/captives-and-companions",
    ),
]

NON_ENGLISH_ENTRIES = [
    ("Die Revolutionäre", "Jason Burke", ENTRIES[0][2]),
    ("Wie man eine Geschichte beendet", "Helen Garner", ENTRIES[1][2]),
    ("Die grenzenlose Tiefe", "Richard Holmes", ENTRIES[2][2]),
]

_HELPERS = {
    ENTRIES[0][0]: ("The Revolutionists", "Революционисты", "Джейсон Бёрк"),
    ENTRIES[1][0]: ("How to End a Story", "Как закончить историю", "Хелен Гарнер"),
    ENTRIES[2][0]: ("The Boundless Deep", "Безграничная глубина", "Ричард Холмс"),
    ENTRIES[3][0]: ("Captives and Companions", "Пленники и спутники", "Джастин Мароцци"),
}
_NON_ENGLISH_HELPERS = {
    NON_ENGLISH_ENTRIES[0][0]: ("The Revolutionists", "Революционеры", "Джейсон Бёрк"),
    NON_ENGLISH_ENTRIES[1][0]: ("How to End a Story", "Как закончить историю", "Хелен Гарнер"),
    NON_ENGLISH_ENTRIES[2][0]: ("The Boundless Deep", "Безграничная глубина", "Ричард Холмс"),
}


def _helper_metadata(entries: list[tuple[str, str, str]], language: str) -> dict[str, Any]:
    values = _HELPERS if language.casefold() in {"en", "english"} else _NON_ENGLISH_HELPERS
    return {
        "version": 1,
        "source_url": SOURCE_URL,
        "language": language,
        "entries": [
            {
                "title": title,
                "author": author,
                "evidence_url": url,
                "english_title": title if language.casefold() in {"en", "english"} else values[title][0],
                "search_translation": values[title][1],
                "author_transliteration_note": values[title][2],
            }
            for title, author, url in entries[:3]
        ],
    }


# Prose from the original page that must never be copied into the capture.
ORIGINAL_PROSE = "The Prize for Non-Fiction reveals its 2025 shortlist of six books."


def _probe_html(entries: tuple[tuple[str, str, str], ...] = tuple(ENTRIES), sort_label: str = "") -> str:
    items = "".join(
        f'<li><a href="{url}">{title}</a><span class="author"> {author} </span></li>' for title, author, url in entries
    )
    sort_markup = f'<p class="sort-label">{sort_label}</p>' if sort_label else ""
    return (
        "<!DOCTYPE html><html><head><title>The Prize announces 2025 shortlist</title></head>"
        f'<body><main><h1>The Prize announces 2025 shortlist</h1><p class="standfirst">{ORIGINAL_PROSE}</p>'
        f"{sort_markup}"
        f'<ul class="shortlist">{items}</ul></main></body></html>'
    )


def _evidence(
    *, entries: list[tuple[str, str, str]] = ENTRIES, sort_label: str = "", **overrides: Any
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "collection_mode": PROBE_COLLECTION_MODE,
        "source_url": SOURCE_URL,
        "source_name": SOURCE_NAME,
        "source_page": SOURCE_URL,
        "name": SOURCE_NAME,
        "signal_family": SIGNAL_FAMILY,
        "resolved_url": SOURCE_URL,
        "head_status": 200,
        "get_status": 200,
        "html": _probe_html(tuple(entries), sort_label=sort_label),
        "detected_heading": "The Prize announces 2025 shortlist",
        "bounded_entry_count": len(entries),
    }
    evidence.update(overrides)
    return evidence


def _write_evidence(tmp_path: Path, evidence: dict[str, Any]) -> Path:
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _write_helper_metadata(tmp_path: Path, entries: list[tuple[str, str, str]] = ENTRIES, language: str = "en") -> Path:
    path = tmp_path / f"helper-metadata-{language}.json"
    path.write_text(
        json.dumps(_helper_metadata(entries, language), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _pair_paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "page.json", tmp_path / "extraction.json"


def _standard_pair_paths(output_dir: Path) -> tuple[Path, Path]:
    """The actual deterministic output names the command writes."""
    page_name, extraction_name = _output_names(SLUG, SOURCE_URL)
    return output_dir / page_name, output_dir / extraction_name


def _build_pair(**kwargs: Any) -> Any:
    evidence = kwargs.pop("evidence", _evidence())
    language = kwargs.get("language", "en")
    if "helper_metadata" not in kwargs:
        kwargs["helper_metadata"] = _helper_metadata(ENTRIES, language)
    return build_replay_fixture_pair(
        SOURCE_URL,
        SLUG,
        evidence,
        signal_strength=SIGNAL_STRENGTH,
        candidate_category=CATEGORY,
        **kwargs,
    )


def _replay_source(page_path: Path, extraction_path: Path) -> dict[str, Any]:
    return {
        "name": SOURCE_NAME,
        "url": SOURCE_URL,
        "family": SIGNAL_FAMILY,
        "signal_family": SIGNAL_FAMILY,
        "metadata_exact_book_list": True,
        "expected_signal_strength": SIGNAL_STRENGTH,
        "eligible_candidate_category": CATEGORY,
        "extraction_method": "codex-extraction-replay",
        "source_page_fixture_file": str(page_path),
        "extraction_response_file": str(extraction_path),
        "active": True,
        "access_status": "open",
        "language": "en",
    }


# ── Accepted retained evidence creates both fixtures ────────────────────


def test_accepted_retained_capture_creates_both_fixtures(tmp_path: Path) -> None:
    evidence_path = _write_evidence(tmp_path, _evidence())
    helper_metadata_path = _write_helper_metadata(tmp_path)
    page_path, extraction_path = _pair_paths(tmp_path)
    report, code = run_capture_source_fixtures(
        SOURCE_URL,
        SLUG,
        evidence_path,
        output_dir=tmp_path,
        signal_strength=SIGNAL_STRENGTH,
        candidate_category=CATEGORY,
        helper_metadata_file=helper_metadata_path,
    )
    assert code == 0
    assert report["status"] == "created"
    assert report["entry_count"] == 3
    page_path = Path(report["page_output"])
    extraction_path = Path(report["extraction_output"])
    assert page_path.exists() and extraction_path.exists()

    page = json.loads(page_path.read_text(encoding="utf-8"))
    assert page["collection_mode"] == REPLAY_COLLECTION_MODE
    assert page["source_name"] == SOURCE_NAME
    assert page["source_url"] == SOURCE_URL
    assert page["source_page"] == SOURCE_URL
    assert page["resolved_url"] == SOURCE_URL
    assert "html" in page

    extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
    assert set(extraction) == {"source_name", "source_url", "operation_id", "capture_sha256", "mentions"}
    assert extraction["source_name"] == SOURCE_NAME
    assert extraction["source_url"] == SOURCE_URL
    assert extraction["operation_id"] == report["operation_id"]
    assert extraction["capture_sha256"] == report["capture_sha256"]
    assert extraction["capture_sha256"] == _sha256(page_path.read_bytes())
    assert len(extraction["mentions"]) == 3


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def test_deterministic_pair_and_default_output_names(tmp_path: Path) -> None:
    first = _build_pair()
    second = _build_pair()
    assert first.page_bytes == second.page_bytes
    assert first.extraction_bytes == second.extraction_bytes
    assert first.capture_sha256 == second.capture_sha256
    assert first.operation_id == second.operation_id
    assert first.operation_id == f"replay-fixture:{SLUG}:{_sha256(SOURCE_URL.encode())[:8]}"

    report, _code = run_capture_source_fixtures(
        SOURCE_URL,
        SLUG,
        _write_evidence(tmp_path, _evidence()),
        output_dir=tmp_path,
        signal_strength=SIGNAL_STRENGTH,
        candidate_category=CATEGORY,
        helper_metadata_file=_write_helper_metadata(tmp_path),
    )
    page_path = Path(report["page_output"])
    extraction_path = Path(report["extraction_output"])
    assert page_path.name == f"codex-capture-{SLUG}-{_sha256(SOURCE_URL.encode())[:8]}.json"
    assert extraction_path.name == f"{SLUG}-{_sha256(SOURCE_URL.encode())[:8]}-codex-extraction.json"


def test_representative_entries_are_bounded_and_capped() -> None:
    pair = _build_pair()
    assert pair.entry_count <= MAX_REPLAY_ENTRIES
    assert pair.entry_count == 3
    assert [entry["title"] for entry in pair.extraction["mentions"]] == [ENTRIES[i][0] for i in range(3)]


# ── Exact metadata and configured signal values ──────────────────────────


def test_extraction_entries_carry_exact_metadata_and_configured_signals() -> None:
    pair = _build_pair()
    entries = pair.extraction["mentions"]
    assert len(entries) == 3
    for entry, (title, author, url) in zip(entries, ENTRIES[:3], strict=True):
        assert entry["title"] == title
        assert entry["primary_authors"] == [author]
        assert entry["evidence_url"] == url
        assert entry["source_url"] == SOURCE_URL
        assert entry["signal_type"] == SIGNAL_FAMILY
        assert entry["signal_strength"] == SIGNAL_STRENGTH
        assert entry["candidate_category"] == CATEGORY
        assert entry["evidence_note"] == EVIDENCE_NOTE
        assert entry["evidence_note"].strip()
        assert len(entry["evidence_note"]) <= 240


def test_generated_helper_fields_are_explicit_for_english_and_non_english() -> None:
    english = _build_pair(language="en")
    for entry, (title, _author, _url) in zip(english.extraction["mentions"], ENTRIES[:3], strict=True):
        assert entry["english_title"] == title
        assert entry["search_translation"] and entry["search_translation"] != title
        assert entry["author_transliteration_note"]

    german = _build_pair(
        evidence=_evidence(entries=NON_ENGLISH_ENTRIES),
        language="de",
        helper_metadata=_helper_metadata(NON_ENGLISH_ENTRIES, "de"),
    )
    for entry, (title, _author, _url) in zip(german.extraction["mentions"], NON_ENGLISH_ENTRIES, strict=True):
        assert entry["english_title"]
        assert entry["english_title"] != title
        assert entry["search_translation"] and entry["search_translation"] != title
        assert entry["author_transliteration_note"]


def test_supplied_sort_label_must_be_grounded_in_retained_evidence() -> None:
    sort_label = "Sorted by editorial priority"
    pair = _build_pair(evidence=_evidence(sort_label=sort_label), expected_sort_label=sort_label)
    assert sort_label in pair.page["html"]

    with pytest.raises(ReplayFixtureError, match="expected sort label"):
        _build_pair(expected_sort_label=sort_label)


def test_missing_helper_metadata_is_rejected_before_fixture_writes(tmp_path: Path) -> None:
    evidence_path = _write_evidence(tmp_path, _evidence())
    output_dir = tmp_path / "fixtures"
    with pytest.raises(ReplayFixtureError, match="helper metadata file does not exist"):
        run_capture_source_fixtures(
            SOURCE_URL,
            SLUG,
            evidence_path,
            output_dir=output_dir,
            signal_strength=SIGNAL_STRENGTH,
            candidate_category=CATEGORY,
            helper_metadata_file=tmp_path / "missing-helper-metadata.json",
        )
    assert not output_dir.exists()


def test_mismatched_helper_identity_is_rejected_before_generation() -> None:
    helper_metadata = _helper_metadata(ENTRIES, "en")
    helper_metadata["entries"][0]["title"] = "Stale title"
    with pytest.raises(ReplayFixtureError, match="identities do not match"):
        _build_pair(helper_metadata=helper_metadata)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("english_title", "Helper unavailable: translation not retained"),
        ("search_translation", "N/A"),
        ("author_transliteration_note", "TODO"),
        ("search_translation", "not available"),
        ("english_title", "unknown"),
    ],
)
def test_explicit_helper_placeholders_are_rejected(field: str, value: str) -> None:
    helper_metadata = _helper_metadata(ENTRIES, "en")
    helper_metadata["entries"][0][field] = value
    with pytest.raises(ReplayFixtureError, match="is a placeholder"):
        _build_pair(helper_metadata=helper_metadata)


@pytest.mark.parametrize("value", [{"retained": "value"}, ["retained"], True, None])
def test_non_string_helper_values_are_rejected_before_fixture_writes(tmp_path: Path, value: Any) -> None:
    evidence_path = _write_evidence(tmp_path, _evidence())
    helper_metadata = _helper_metadata(ENTRIES, "en")
    helper_metadata["entries"][0]["english_title"] = value
    helper_metadata_path = tmp_path / "helper-metadata.json"
    helper_metadata_path.write_text(json.dumps(helper_metadata), encoding="utf-8")
    output_dir = tmp_path / "fixtures"

    with pytest.raises(ReplayFixtureError, match="helper metadata"):
        run_capture_source_fixtures(
            SOURCE_URL,
            SLUG,
            evidence_path,
            output_dir=output_dir,
            signal_strength=SIGNAL_STRENGTH,
            candidate_category=CATEGORY,
            helper_metadata_file=helper_metadata_path,
        )
    assert not output_dir.exists()


def test_helper_text_containing_unknown_is_allowed() -> None:
    helper_metadata = _helper_metadata(ENTRIES, "en")
    helper_metadata["entries"][0]["search_translation"] = "The Unknown Archive"
    pair = _build_pair(helper_metadata=helper_metadata)
    assert pair.extraction["mentions"][0]["search_translation"] == "The Unknown Archive"


def test_supplied_sort_label_rejection_writes_no_fixture_files(tmp_path: Path) -> None:
    evidence_path = _write_evidence(tmp_path, _evidence())
    helper_metadata_path = _write_helper_metadata(tmp_path)
    output_dir = tmp_path / "fixtures"
    with pytest.raises(ReplayFixtureError, match="expected sort label"):
        run_capture_source_fixtures(
            SOURCE_URL,
            SLUG,
            evidence_path,
            output_dir=output_dir,
            signal_strength=SIGNAL_STRENGTH,
            candidate_category=CATEGORY,
            expected_sort_label="Sorted by editorial priority",
            helper_metadata_file=helper_metadata_path,
        )
    assert not output_dir.exists()


def test_generated_helpers_create_only_real_russian_queries() -> None:
    pair = _build_pair(language="en")
    for entry in pair.extraction["mentions"]:
        attempts = russian_query_attempts(entry)
        by_kind = {attempt["kind"]: attempt["query"] for attempt in attempts}
        assert by_kind["search_translation"] == entry["search_translation"]
        assert by_kind["author_transliteration_note"] == entry["author_transliteration_note"]
        assert all("unavailable" not in attempt["query"].casefold() for attempt in attempts)


def test_signal_values_default_from_probe_evidence_and_override(tmp_path: Path) -> None:
    # Signal family and source name come from the retained probe evidence.
    pair = _build_pair()
    assert pair.page["source_name"] == SOURCE_NAME
    assert pair.extraction["mentions"][0]["signal_type"] == SIGNAL_FAMILY

    # Explicit options win over the retained values.
    overridden = build_replay_fixture_pair(
        SOURCE_URL,
        SLUG,
        _evidence(),
        source_name="Renamed Source",
        signal_family="media list",
        signal_strength="major media list",
        candidate_category=CATEGORY,
        helper_metadata=_helper_metadata(ENTRIES, "en"),
    )
    assert overridden.page["source_name"] == "Renamed Source"
    assert overridden.extraction["mentions"][0]["signal_type"] == "media list"
    assert overridden.extraction["mentions"][0]["signal_strength"] == "major media list"
    assert overridden.extraction["mentions"][0]["candidate_category"] == CATEGORY


# ── No copied prose, no forbidden fields ─────────────────────────────────


def test_generated_capture_has_no_copied_prose(tmp_path: Path) -> None:
    evidence_path = _write_evidence(tmp_path, _evidence())
    helper_metadata_path = _write_helper_metadata(tmp_path)
    report, _code = run_capture_source_fixtures(
        SOURCE_URL,
        SLUG,
        evidence_path,
        output_dir=tmp_path,
        signal_strength=SIGNAL_STRENGTH,
        candidate_category=CATEGORY,
        helper_metadata_file=helper_metadata_path,
    )
    page = json.loads(Path(report["page_output"]).read_text(encoding="utf-8"))
    html = page["html"]
    assert ORIGINAL_PROSE not in html
    # Only the bounded representative metadata is present.
    for title, author, url in ENTRIES[:3]:
        assert title in html
        assert author in html
        assert url in html
    # The fourth entry and its prose never appear.
    assert ENTRIES[3][0] not in html
    assert ENTRIES[3][1] not in html
    assert "standfirst" not in html


def test_extraction_entries_never_emit_forbidden_copied_text_fields() -> None:
    pair = _build_pair()
    for entry in pair.extraction["mentions"]:
        assert not (FORBIDDEN_COLLECTOR_RESPONSE_FIELDS & set(entry))


# ── Grounding in the generated page and the replay collector ─────────────


def test_every_evidence_url_is_grounded_in_generated_capture() -> None:
    pair = _build_pair()
    page_data = dict(pair.page)
    source = _replay_source(Path("page.json"), Path("extraction.json"))
    for entry in pair.extraction["mentions"]:
        assert exact_book_list_mention_is_grounded(
            page_data["html"],
            page_data["source_page"],
            title=entry["title"],
            authors=entry["primary_authors"],
            evidence_url=entry["evidence_url"],
        )
        verified, _reason = evidence_url_verified(page_data, entry["evidence_url"])
        assert verified
        mention = normalized_agent_mention(dict(entry), source, RUN_DATE)
        violation = extraction_contract_violation(dict(entry), mention, source, page_data)
        assert violation == ""


def test_replay_collector_accepts_generated_pair(tmp_path: Path) -> None:
    evidence_path = _write_evidence(tmp_path, _evidence())
    helper_metadata_path = _write_helper_metadata(tmp_path)
    report, code = run_capture_source_fixtures(
        SOURCE_URL,
        SLUG,
        evidence_path,
        output_dir=tmp_path,
        signal_strength=SIGNAL_STRENGTH,
        candidate_category=CATEGORY,
        helper_metadata_file=helper_metadata_path,
    )
    assert code == 0
    page_path = Path(report["page_output"])
    extraction_path = Path(report["extraction_output"])
    result = codex_extraction_collector_result(
        tmp_path / "config.json", tmp_path, _replay_source(page_path, extraction_path), RUN_DATE
    )
    assert len(result.mentions) == 3
    for mention, (title, author, url) in zip(result.mentions, ENTRIES[:3], strict=True):
        assert mention["title"] == title
        assert mention["primary_authors"] == [author]
        assert mention["evidence_url"] == url
        assert mention["signal_type"] == SIGNAL_FAMILY
        assert mention["signal_strength"] == SIGNAL_STRENGTH
        assert mention["candidate_category"] == CATEGORY
        assert result.debug_metadata["validated_mention_count"] == 3
        assert result.debug_metadata["dropped_mention_count"] == 0


# ── Rejected evidence produces no output ─────────────────────────────────


def test_malformed_evidence_produces_no_output(tmp_path: Path) -> None:
    malformed = [
        b"not json",
        json.dumps(["not", "an", "object"]).encode(),
        json.dumps(_evidence(collection_mode="codex-capture")).encode(),
        json.dumps(_evidence(head_status=None, get_status=None)).encode(),
        json.dumps(_evidence(html="")).encode(),
        json.dumps(_evidence(head_status=403)).encode(),
    ]
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    for index, content in enumerate(malformed):
        evidence_path = evidence_dir / f"evidence-{index}.json"
        evidence_path.write_bytes(content)
        # Every case uses its own isolated output dir, and the check targets
        # the actual deterministic output names the command would write.
        output_dir = tmp_path / f"out-{index}"
        page_path, extraction_path = _standard_pair_paths(output_dir)
        with pytest.raises(ReplayFixtureError):
            run_capture_source_fixtures(
                SOURCE_URL,
                SLUG,
                evidence_path,
                output_dir=output_dir,
                signal_strength=SIGNAL_STRENGTH,
                candidate_category=CATEGORY,
            )
        assert not page_path.exists()
        assert not extraction_path.exists()
        assert not output_dir.exists()


def test_non_accepted_and_deferred_evidence_produce_no_output(tmp_path: Path) -> None:
    # A rejected verdict: HEAD 403 never falls back.
    rejected = _evidence(head_status=403)
    evidence_path = _write_evidence(tmp_path, rejected)
    output_dir = tmp_path / "out"
    page_path, extraction_path = _standard_pair_paths(output_dir)
    with pytest.raises(ReplayFixtureError, match="not an accepted capture"):
        run_capture_source_fixtures(
            SOURCE_URL,
            SLUG,
            evidence_path,
            output_dir=output_dir,
            signal_strength=SIGNAL_STRENGTH,
            candidate_category=CATEGORY,
        )
    assert not page_path.exists()
    assert not extraction_path.exists()
    assert not output_dir.exists()

    # A deferred verdict: JS-only page without server-rendered entries.
    js_html = '<html><body><h1>Current list</h1><script id="__NEXT_DATA__">{"props":{}}</script></body></html>'
    deferred = _evidence(html=js_html)
    evidence_path = _write_evidence(tmp_path, deferred)
    with pytest.raises(ReplayFixtureError, match="not an accepted capture"):
        run_capture_source_fixtures(
            SOURCE_URL,
            SLUG,
            evidence_path,
            output_dir=output_dir,
            signal_strength=SIGNAL_STRENGTH,
            candidate_category=CATEGORY,
        )
    assert not page_path.exists()
    assert not extraction_path.exists()
    assert not output_dir.exists()


def test_url_mismatched_evidence_produces_no_output(tmp_path: Path) -> None:
    evidence_path = _write_evidence(tmp_path, _evidence(source_url="https://example.test/other-page"))
    output_dir = tmp_path / "out"
    page_path, extraction_path = _standard_pair_paths(output_dir)
    with pytest.raises(ReplayFixtureError, match="different URL"):
        run_capture_source_fixtures(
            SOURCE_URL,
            SLUG,
            evidence_path,
            output_dir=output_dir,
            signal_strength=SIGNAL_STRENGTH,
            candidate_category=CATEGORY,
        )
    assert not page_path.exists()
    assert not extraction_path.exists()
    assert not output_dir.exists()


def test_invalid_slug_and_blank_signal_strength_are_rejected() -> None:
    for bad_slug in ("", "..", "a/b", "a b", "A-B", "a.b"):
        with pytest.raises(ReplayFixtureError, match="filesystem-safe"):
            build_replay_fixture_pair(
                SOURCE_URL, bad_slug, _evidence(), signal_strength=SIGNAL_STRENGTH, candidate_category=CATEGORY
            )
    with pytest.raises(ReplayFixtureError, match="nonblank"):
        build_replay_fixture_pair(SOURCE_URL, SLUG, _evidence(), signal_strength="   ", candidate_category=CATEGORY)
    with pytest.raises(ReplayFixtureError, match="candidate-category must be nonblank"):
        build_replay_fixture_pair(
            SOURCE_URL, SLUG, _evidence(), signal_strength=SIGNAL_STRENGTH, candidate_category="  "
        )


# ── Retry semantics: idempotent exact retry, safe conflict refusal ───────


def test_exact_retry_is_unchanged_and_conflict_does_not_corrupt(tmp_path: Path) -> None:
    pair = _build_pair()
    page_path, extraction_path = _pair_paths(tmp_path)
    assert write_replay_fixture_pair(pair, page_path, extraction_path) == "created"
    page_bytes = page_path.read_bytes()
    extraction_bytes = extraction_path.read_bytes()

    # An exact retry succeeds without touching the pair.
    assert write_replay_fixture_pair(pair, page_path, extraction_path) == "unchanged"
    assert page_path.read_bytes() == page_bytes
    assert extraction_path.read_bytes() == extraction_bytes

    # A conflicting retry is refused and never corrupts either file.
    tampered = b'{"tampered": true}\n'
    extraction_path.write_bytes(tampered)
    with pytest.raises(ReplayFixtureError, match="refusing to overwrite"):
        write_replay_fixture_pair(pair, page_path, extraction_path)
    assert extraction_path.read_bytes() == tampered
    assert page_path.read_bytes() == page_bytes

    # A conflicting page half is refused the same way.
    page_path.write_bytes(b'{"tampered": true}\n')
    with pytest.raises(ReplayFixtureError, match="refusing to overwrite"):
        write_replay_fixture_pair(pair, page_path, extraction_path)
    assert page_path.read_bytes() == b'{"tampered": true}\n'
    assert extraction_path.read_bytes() == tampered


def test_half_present_pair_is_completed_without_overwriting_matching_half(tmp_path: Path) -> None:
    pair = _build_pair()
    page_path, extraction_path = _pair_paths(tmp_path)
    assert write_replay_fixture_pair(pair, page_path, extraction_path) == "created"
    page_bytes = page_path.read_bytes()
    extraction_bytes = extraction_path.read_bytes()

    extraction_path.unlink()
    assert write_replay_fixture_pair(pair, page_path, extraction_path) == "completed"
    assert page_path.read_bytes() == page_bytes
    assert extraction_path.read_bytes() == extraction_bytes


def test_identical_output_paths_are_rejected_before_any_write(tmp_path: Path) -> None:
    pair = _build_pair()
    output_dir = tmp_path / "not-yet-created"
    same_path = output_dir / "same.json"
    # The exact same path passed twice must be refused before the directory
    # or file is created, so a pair can never collapse into one file.
    with pytest.raises(ReplayFixtureError, match="must be different files"):
        write_replay_fixture_pair(pair, same_path, same_path)
    assert not output_dir.exists()
    assert not same_path.exists()

    # Lexically different paths that resolve to the same file are refused
    # the same way, and a pre-existing file at that path stays untouched.
    existing = tmp_path / "existing.json"
    existing.write_bytes(b"pre-existing bytes\n")
    alias = tmp_path / "." / "existing.json"
    with pytest.raises(ReplayFixtureError, match="must be different files"):
        write_replay_fixture_pair(pair, existing, alias)
    assert existing.read_bytes() == b"pre-existing bytes\n"

    # Distinct paths still work.
    page_path, extraction_path = _pair_paths(tmp_path)
    assert write_replay_fixture_pair(pair, page_path, extraction_path) == "created"


def test_protected_weekly_state_directories_are_rejected_before_writing(tmp_path: Path) -> None:
    state = tmp_path / "runs" / "current-weekly-pilot-russian"
    state.mkdir(parents=True)
    protected = state / "candidate-history.json"
    protected.write_bytes(b"protected weekly state\n")
    before = protected.read_bytes()
    pair = _build_pair()
    with pytest.raises(ReplayFixtureError):
        write_replay_fixture_pair(pair, state / "page.json", state / "extraction.json")
    assert protected.read_bytes() == before
    assert list(state.glob("*.json")) == [protected]


# ── CLI structured output ────────────────────────────────────────────────


def test_cli_emits_structured_result(tmp_path: Path, capsys: Any) -> None:
    evidence_path = _write_evidence(tmp_path, _evidence())
    helper_metadata_path = _write_helper_metadata(tmp_path)
    code = main(
        [
            "capture-source-fixtures",
            "--url",
            SOURCE_URL,
            "--slug",
            SLUG,
            "--probe-evidence",
            str(evidence_path),
            "--helper-metadata",
            str(helper_metadata_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--signal-strength",
            SIGNAL_STRENGTH,
            "--candidate-category",
            CATEGORY,
        ]
    )
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["version"] == 1
    assert report["status"] == "created"
    assert report["url"] == SOURCE_URL
    assert report["slug"] == SLUG
    assert report["entry_count"] == 3
    assert report["exit_code"] == 0
    assert Path(report["page_output"]).is_file()
    assert Path(report["extraction_output"]).is_file()
    assert report["capture_sha256"] == _sha256(Path(report["page_output"]).read_bytes())

    # The second identical invocation (same signal options) reports unchanged.
    code = main(
        [
            "capture-source-fixtures",
            "--url",
            SOURCE_URL,
            "--slug",
            SLUG,
            "--probe-evidence",
            str(evidence_path),
            "--helper-metadata",
            str(helper_metadata_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--signal-strength",
            SIGNAL_STRENGTH,
            "--candidate-category",
            CATEGORY,
        ]
    )
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "unchanged"

    # A retry with different signal options is a different pair and is
    # refused without corrupting the existing pair.
    code = main(
        [
            "capture-source-fixtures",
            "--url",
            SOURCE_URL,
            "--slug",
            SLUG,
            "--probe-evidence",
            str(evidence_path),
            "--helper-metadata",
            str(helper_metadata_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--signal-strength",
            "top ranking",
            "--candidate-category",
            CATEGORY,
        ]
    )
    assert code == 1
    report = json.loads(capsys.readouterr().out)
    assert "refusing to overwrite" in report["error"]


def test_cli_emits_nonzero_error_for_invalid_input(tmp_path: Path, capsys: Any) -> None:
    evidence_path = _write_evidence(tmp_path, _evidence(source_url="https://example.test/other-page"))
    helper_metadata_path = _write_helper_metadata(tmp_path)
    code = main(
        [
            "capture-source-fixtures",
            "--url",
            SOURCE_URL,
            "--slug",
            SLUG,
            "--probe-evidence",
            str(evidence_path),
            "--helper-metadata",
            str(helper_metadata_path),
            "--signal-strength",
            SIGNAL_STRENGTH,
            "--candidate-category",
            CATEGORY,
        ]
    )
    assert code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["version"] == 1
    assert "error" in report
    assert "different URL" in report["error"]

    missing = tmp_path / "missing-evidence.json"
    code = main(
        [
            "capture-source-fixtures",
            "--url",
            SOURCE_URL,
            "--slug",
            SLUG,
            "--probe-evidence",
            str(missing),
            "--helper-metadata",
            str(helper_metadata_path),
            "--signal-strength",
            SIGNAL_STRENGTH,
            "--candidate-category",
            CATEGORY,
        ]
    )
    assert code == 1
    report = json.loads(capsys.readouterr().out)
    assert "does not exist" in report["error"]

    # The CLI requires the configured candidate category.
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "capture-source-fixtures",
                "--url",
                SOURCE_URL,
                "--slug",
                SLUG,
                "--probe-evidence",
                str(evidence_path),
                "--helper-metadata",
                str(helper_metadata_path),
                "--signal-strength",
                SIGNAL_STRENGTH,
            ]
        )
    assert exit_info.value.code == 2
    assert "--candidate-category" in capsys.readouterr().err


def test_cli_imports_and_builds_parsers_without_fcntl(capsys: Any) -> None:
    """Windows regression: CLI import and parser setup never import fcntl.

    ``autoscout.__main__`` imports ``autoscout.replay_fixtures`` (through ``autoscout.cli``)
    unconditionally, so a module-load ``import fcntl`` there would break the
    CLI on Windows before argument parsing. An import hook forbids
    ``fcntl`` while both modules are imported fresh, then the argument
    parser must still register the fixture command."""
    import importlib
    import importlib.abc
    import sys

    import autoscout

    class _ForbidFcntl(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
            if fullname == "fcntl" or fullname.startswith("fcntl."):
                raise AssertionError("autoscout CLI module import must not import fcntl")
            return None

    hook = _ForbidFcntl()
    sys.meta_path.insert(0, hook)
    missing_package_main = object()
    saved_package_main = getattr(autoscout, "__main__", missing_package_main)
    # The command modules under autoscout.cli import replay_fixtures (#1019),
    # so they are imported fresh too.
    cli_modules = [name for name in sys.modules if name == "autoscout.cli" or name.startswith("autoscout.cli.")]
    saved_package_cli = getattr(autoscout, "cli", missing_package_main)
    saved_modules: dict[str, Any] = {}
    try:
        for name in ("autoscout.replay_fixtures", "autoscout.__main__", "fcntl", *cli_modules):
            saved_modules[name] = sys.modules.pop(name, None)
        if saved_package_cli is not missing_package_main:
            delattr(autoscout, "cli")
        main_module = importlib.import_module("autoscout.__main__")
        with pytest.raises(SystemExit) as exit_info:
            main_module.main(["--help"])
        assert exit_info.value.code == 0
        assert "capture-source-fixtures" in capsys.readouterr().out
    finally:
        for name, module in saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if saved_package_main is missing_package_main:
            delattr(autoscout, "__main__")
        else:
            autoscout.__main__ = saved_package_main
        if saved_package_cli is not missing_package_main:
            autoscout.cli = saved_package_cli
        sys.meta_path.remove(hook)


# ── Page identity uses the requested URL exactly ─────────────────────────


def test_page_identity_uses_requested_url_not_evidence_resolved_url() -> None:
    # The retained probe capture records a resolved URL that differs from the
    # requested URL only by a trailing slash. The generated page must carry
    # the requested URL exactly in source_url, source_page, and resolved_url,
    # and the extraction source_url must be the requested URL as well.
    pair = build_replay_fixture_pair(
        SOURCE_URL,
        SLUG,
        _evidence(resolved_url=SOURCE_URL + "/"),
        signal_strength=SIGNAL_STRENGTH,
        candidate_category=CATEGORY,
        helper_metadata=_helper_metadata(ENTRIES, "en"),
    )
    assert pair.page["source_url"] == SOURCE_URL
    assert pair.page["source_page"] == SOURCE_URL
    assert pair.page["resolved_url"] == SOURCE_URL
    assert pair.extraction["source_url"] == SOURCE_URL
    for entry in pair.extraction["mentions"]:
        assert entry["source_url"] == SOURCE_URL


# ── Cross-process pair lock: concurrent writers never corrupt a pair ──────


def _write_concurrently(pairs: list[Any], page_path: Path, extraction_path: Path) -> list[Any]:
    """Race several writers at one destination pair and return their results."""
    barrier = threading.Barrier(len(pairs))
    outcomes: list[Any] = []
    outcomes_lock = threading.Lock()

    def worker(pair: Any) -> None:
        barrier.wait()
        try:
            status = write_replay_fixture_pair(pair, page_path, extraction_path)
            result: Any = ("ok", status)
        except ReplayFixtureError as exc:
            result = ("error", str(exc))
        with outcomes_lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker, args=(pair,)) for pair in pairs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


def test_concurrent_exact_retries_serialize_to_created_and_unchanged(tmp_path: Path) -> None:
    pair = _build_pair()
    page_path, extraction_path = _pair_paths(tmp_path)
    outcomes = _write_concurrently([pair, pair], page_path, extraction_path)
    assert sorted(status for result, status in outcomes if result == "ok") == ["created", "unchanged"]
    assert page_path.read_bytes() == pair.page_bytes
    assert extraction_path.read_bytes() == pair.extraction_bytes
    extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
    assert extraction["capture_sha256"] == _sha256(page_path.read_bytes())


def test_replay_separates_different_same_depth_title_and_detail_paths() -> None:
    url = "https://example.test/same-depth-ranking/"
    detail_url = "https://example.test/product/one/"
    metadata = {
        "url": url,
        "eligible_candidate_category": "adult nonfiction",
        "metadata_ranking_table": True,
        "ranking_author_selector": ".author",
        "ranking_category_marker": "Weekly ranking",
        "ranking_detail_category_allowlist": ["Adult nonfiction"],
        "ranking_detail_category_pattern": r"Category:\s*(?P<category>[^<]+)",
        "ranking_detail_category_selector": ".posted_in",
        "ranking_detail_fields": ["isbn", "category"],
        "ranking_detail_identifier_selector": ".sku",
        "ranking_detail_link_selector": ".detail",
        "ranking_detail_max_pages": 1,
        "ranking_detail_scope_selector": ".product_meta",
        "ranking_detail_url_pattern": r"https://example\.test/product/[^?#]+/?",
        "ranking_evidence_url": "detail_page",
        "ranking_expected_raw_row_count": 1,
        "ranking_rank_selector": ".rank",
        "ranking_rank_source": "selector",
        "ranking_reporting_period_undated": True,
        "ranking_row_selector": ".row",
        "ranking_title_selector": ".title",
    }
    evidence = {
        "collection_mode": PROBE_COLLECTION_MODE,
        "source_url": url,
        "source_page": url,
        "resolved_url": url,
        "source_name": "Synthetic same-depth ranking",
        "signal_family": "bestseller/ranking",
        "contract_family": "ranking-table",
        "contract_metadata": metadata,
        "head_status": 200,
        "get_status": 200,
        "html": (
            f'<h1>Weekly ranking</h1><div class="row"><span class="rank">1</span>'
            f'<span class="title">Book One</span><a class="detail" href="{detail_url}"></a>'
            '<span class="author">Author One</span></div>'
        ),
        "detected_heading": "Weekly ranking",
        "bounded_entry_count": 1,
        "raw_row_count": 1,
        "reference_date": "2026-08-24",
        "detail_pages": [
            {
                "source_page": detail_url,
                "detail_url": detail_url,
                "resolved_url": detail_url,
                "html": (
                    '<div class="product_meta"><span class="sku">9780306406157</span>'
                    '<span class="posted_in">Category: Adult nonfiction</span></div>'
                ),
            }
        ],
    }
    helper = {
        "version": 1,
        "source_url": url,
        "language": "en",
        "entries": [
            {
                "title": "Book One",
                "author": "Author One",
                "evidence_url": detail_url,
                "english_title": "Book One",
                "search_translation": "Книга один",
                "author_transliteration_note": "Автор Один",
            }
        ],
    }

    pair = build_replay_fixture_pair(
        url,
        "same-depth-ranking",
        evidence,
        signal_strength="top ranking",
        candidate_category="adult nonfiction",
        helper_metadata=helper,
    )

    assert '<span class="title">Book One</span><a class="detail"' in pair.page["html"]
    assert ranking_rows(pair.page["html"], metadata)[0].title == "Book One"
    assert ranking_rows(pair.page["html"], metadata)[0].detail_url == detail_url


def _semicolon_ranking_case(author: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    url = "https://example.test/semicolon-ranking/"
    metadata = {
        "eligible_candidate_category": "adult nonfiction",
        "metadata_ranking_table": True,
        "ranking_author_selector": ".author",
        "ranking_category_marker": "Weekly ranking",
        "ranking_evidence_url": "source_page",
        "ranking_expected_raw_row_count": 2,
        "ranking_identifier_selector": ".isbn",
        "ranking_rank_selector": ".rank",
        "ranking_rank_source": "selector",
        "ranking_reporting_period_date_format": "%Y-%m-%d",
        "ranking_reporting_period_pattern": r"(?P<period>2026-08-24 - 2026-08-24)",
        "ranking_row_selector": ".row",
        "ranking_title_selector": ".title",
        "visible_title_selector": ".page-title",
    }
    html = (
        '<h1 class="page-title">Weekly ranking 2026-08-24 - 2026-08-24</h1>'
        '<div class="row"><span class="rank">1</span><span class="title">Book One</span>'
        f'<span class="author">{author}</span><span class="isbn">9780306406157</span></div>'
        '<div class="row"><span class="rank">2</span><span class="title">Book Two</span>'
        '<span class="author">Author Two</span><span class="isbn">9780140328721</span></div>'
    )
    entries = [
        ("Book One", author),
        ("Book Two", "Author Two"),
    ]
    evidence = {
        "collection_mode": PROBE_COLLECTION_MODE,
        "source_name": "Synthetic semicolon ranking",
        "source_url": url,
        "source_page": url,
        "resolved_url": url,
        "signal_family": "bestseller/ranking",
        "contract_family": "ranking-table",
        "contract_metadata": metadata,
        "head_status": 200,
        "get_status": 200,
        "html": html,
        "detected_heading": "Weekly ranking 2026-08-24 - 2026-08-24",
        "bounded_entry_count": 2,
        "raw_row_count": 2,
        "reference_date": "2026-08-24",
    }
    helper = {
        "version": 1,
        "source_url": url,
        "language": "es",
        "entries": [
            {
                "title": title,
                "author": entry_author,
                "evidence_url": url,
                "english_title": f"English {title}",
                "search_translation": f"Перевод {title}",
                "author_transliteration_note": f"Транслитерация {entry_author}",
            }
            for title, entry_author in entries
        ],
    }
    return url, evidence, helper


@pytest.mark.parametrize(
    ("author_display", "expected"),
    [
        ("A / B", ["A", "B"]),
        ("A; B", ["A", "B"]),
        ("A  /  B", ["A", "B"]),
        ("AC/DC", ["AC/DC"]),
    ],
)
def test_ranking_primary_authors_preserves_order_and_normal_author_names(
    author_display: str, expected: list[str]
) -> None:
    assert _ranking_primary_authors(author_display) == expected


@pytest.mark.parametrize(
    "author_display",
    ["A / ", " / B", "A; ", "; B"],
)
def test_ranking_primary_authors_rejects_empty_segments(author_display: str) -> None:
    with pytest.raises(ReplayFixtureError, match="empty"):
        _ranking_primary_authors(author_display)


def test_ranking_replay_splits_ordered_semicolon_authors_and_grounds() -> None:
    author = "HOYER, KATHERINE; R.G, YULIBET..."
    url, evidence, helper = _semicolon_ranking_case(author)

    pair = build_replay_fixture_pair(
        url,
        "semicolon-ranking",
        evidence,
        signal_strength="top ranking",
        candidate_category="adult nonfiction",
        language="es",
        helper_metadata=helper,
    )

    identity = parse_visible_page_identity(pair.page["html"], ".page-title")
    assert identity.identity_texts == ("Weekly ranking 2026-08-24 - 2026-08-24",)

    mention = pair.extraction["mentions"][0]
    assert mention["primary_authors"] == ["HOYER, KATHERINE", "R.G, YULIBET..."]
    assert helper["entries"][0]["author"] == author
    assert ranking_mention_is_grounded(
        pair.page["html"],
        title=mention["title"],
        isbn=mention["isbn"],
        current_rank=mention["current_rank"],
        source_week=mention["source_week"],
        primary_authors=mention["primary_authors"],
        ranking_source=evidence["contract_metadata"],
        evidence_url=mention["evidence_url"],
    )


@pytest.mark.parametrize(
    "author",
    [
        "HOYER, KATHERINE; ; R.G, YULIBET...",
        "HOYER, KATHERINE;;R.G, YULIBET...",
    ],
)
def test_ranking_replay_rejects_empty_or_ambiguous_semicolon_author_segments(author: str) -> None:
    url, evidence, helper = _semicolon_ranking_case(author)

    with pytest.raises(ReplayFixtureError, match="semicolon"):
        build_replay_fixture_pair(
            url,
            "semicolon-ranking",
            evidence,
            signal_strength="top ranking",
            candidate_category="adult nonfiction",
            language="es",
            helper_metadata=helper,
        )


def test_undated_ranking_fixture_generation_uses_reference_date_as_observation_check_label(
    tmp_path: Path,
) -> None:
    """The retained date labels the observation/check, not a published week."""
    url = "https://example.test/undated-ranking/"
    first_detail = "https://example.test/product/one/"
    second_detail = "https://example.test/product/two/"
    metadata = {
        "ranking_author_selector": ".author",
        "ranking_category_marker": "Weekly ranking",
        "ranking_detail_category_allowlist": ["Adult nonfiction"],
        "ranking_detail_category_pattern": r"Category:\s*(?P<category>[^<]+)",
        "ranking_detail_category_selector": ".posted_in",
        "ranking_detail_fields": ["isbn", "category"],
        "ranking_detail_identifier_selector": ".sku",
        "ranking_detail_link_selector": ".detail",
        "ranking_detail_max_pages": 2,
        "ranking_detail_scope_selector": ".product_meta",
        "ranking_detail_url_pattern": r"https://example\.test/product/[^?#]+/?",
        "ranking_evidence_url": "detail_page",
        "ranking_expected_raw_row_count": 2,
        "ranking_rank_selector": ".rank",
        "ranking_rank_source": "selector",
        "ranking_reporting_period_undated": True,
        "ranking_row_selector": ".outer > .row",
        "ranking_title_selector": ".detail",
        "eligible_candidate_category": "adult nonfiction",
    }
    html = (
        '<h1>Weekly ranking</h1><div class="outer">'
        '<div class="row"><span class="rank">#1 ranking</span>'
        f'<a class="detail" href="{first_detail}"><span class="title">Book One</span></a>'
        '<span class="author">Author One</span></div>'
        '<div class="row"><span class="rank">#2 ranking</span>'
        f'<a class="detail" href="{second_detail}"><span class="title">Book Two</span></a>'
        '<span class="author">Author Two</span></div></div>'
    )
    evidence = {
        "collection_mode": PROBE_COLLECTION_MODE,
        "source_url": url,
        "source_page": url,
        "resolved_url": url,
        "source_name": "Synthetic undated ranking",
        "signal_family": "bestseller/ranking",
        "contract_family": "ranking-table",
        "contract_metadata": metadata,
        "head_status": 200,
        "get_status": 200,
        "html": html,
        "detected_heading": "Weekly ranking",
        "bounded_entry_count": 2,
        "raw_row_count": 2,
        "reference_date": "2026-08-24",
        "detail_pages": [
            {
                "source_page": first_detail,
                "detail_url": first_detail,
                "resolved_url": first_detail,
                "html": '<div class="product_meta"><span class="sku">9780306406157</span>'
                '<span class="posted_in">Category: Adult nonfiction</span></div>',
            },
            {
                "source_page": second_detail,
                "detail_url": second_detail,
                "resolved_url": second_detail,
                "html": '<div class="product_meta"><span class="sku">9780140328721</span>'
                '<span class="posted_in">Category: Adult nonfiction</span></div>',
            },
        ],
    }
    evidence_path = _write_evidence(tmp_path, evidence)
    helper_path = tmp_path / "helper.json"
    helper_path.write_text(
        json.dumps(
            {
                "version": 1,
                "source_url": url,
                "language": "en",
                "entries": [
                    {
                        "title": "Book One",
                        "author": "Author One",
                        "evidence_url": first_detail,
                        "english_title": "Book One",
                        "search_translation": "Книга один",
                        "author_transliteration_note": "Автор Один",
                    },
                    {
                        "title": "Book Two",
                        "author": "Author Two",
                        "evidence_url": second_detail,
                        "english_title": "Book Two",
                        "search_translation": "Книга два",
                        "author_transliteration_note": "Автор Два",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report, code = run_capture_source_fixtures(
        url,
        "synthetic-undated-ranking",
        evidence_path,
        output_dir=tmp_path / "out",
        source_name="Synthetic undated ranking",
        signal_family="bestseller/ranking",
        contract_family="ranking-table",
        signal_strength="top ranking",
        candidate_category="adult nonfiction",
        language="en",
        helper_metadata_file=helper_path,
    )

    assert code == 0
    assert report["entry_count"] == 2
    generated_page = json.loads(Path(report["page_output"]).read_text(encoding="utf-8"))
    assert len(ranking_rows(generated_page["html"], metadata)) == metadata["ranking_expected_raw_row_count"]
    extraction = json.loads(Path(report["extraction_output"]).read_text(encoding="utf-8"))
    assert [mention["source_week"] for mention in extraction["mentions"]] == ["2026-08-24", "2026-08-24"]


def test_concurrent_conflicting_writes_never_produce_mismatched_pair(tmp_path: Path) -> None:
    # Two different generated pairs (different page bytes and different
    # extraction bytes) race for the same destination pair. The advisory pair
    # lock serializes them: exactly one creates the pair, the other refuses,
    # and the surviving page/extraction pair is always mutually consistent -
    # never a page from one generation with an extraction from the other.
    first = _build_pair()
    second = build_replay_fixture_pair(
        SOURCE_URL,
        SLUG,
        _evidence(),
        source_name="Conflicting Source",
        signal_strength=SIGNAL_STRENGTH,
        candidate_category="fiction",
        helper_metadata=_helper_metadata(ENTRIES, "en"),
    )
    assert first.page_bytes != second.page_bytes
    assert first.extraction_bytes != second.extraction_bytes

    for round_index in range(5):
        page_path, extraction_path = _pair_paths(tmp_path / f"round-{round_index}")
        ordered = [first, second] if round_index % 2 == 0 else [second, first]
        outcomes = _write_concurrently(ordered, page_path, extraction_path)
        ok_statuses = sorted(status for result, status in outcomes if result == "ok")
        errors = [reason for result, reason in outcomes if result == "error"]
        assert ok_statuses == ["created"]
        assert len(errors) == 1
        assert "refusing to overwrite" in errors[0]

        page_bytes = page_path.read_bytes()
        extraction_bytes = extraction_path.read_bytes()
        extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
        # The final capture digest always matches the final page bytes, and
        # both halves always come from the same generation.
        assert extraction["capture_sha256"] == _sha256(page_bytes)
        if page_bytes == first.page_bytes:
            assert extraction_bytes == first.extraction_bytes
        else:
            assert page_bytes == second.page_bytes
            assert extraction_bytes == second.extraction_bytes
