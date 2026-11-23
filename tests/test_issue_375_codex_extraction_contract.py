"""Tests for Issue #375: Audiobooks.com and PEOPLE Books live extraction failures.

The 2026-08-01 weekly run produced zero valid mentions for both sources.
Both failures are Codex extraction-quality failures on the live path:

* Audiobooks.com emitted entries without the eligibility fields the
  audiobook-ranking contract requires (``underlying_work_type``,
  ``publication_status``) and with a signal type/strength the contract does
  not accept, so every entry was dropped as ineligible.
* PEOPLE Books emitted book-article evidence URLs that do not occur in the
  captured landing page, so every entry failed the grounding gate.

The Codex worklist never told Codex what shape to produce. These tests pin
the fix: each work item now carries the machine-generated extraction
contract (the same required output shape and rules the validator enforces),
and the contract demands capture-grounded evidence URLs. The committed
2026-08-01 capture and extraction artifacts are replayed directly so the
old shape stays rejected and the contract-conformant shape stays accepted.

Fixture dependency: these tests read the run artifacts named below from
``runs/current-weekly-pilot-russian/``. They are the real recorded failure,
which is the point -- a synthetic stand-in would not prove the old shape is
still rejected. Pruning that run directory breaks this module, so copy the
four named files into ``tests/fixtures/`` first if the run is ever cleaned up.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from autoscout.codex_recovery import _artifact_exact_list_metadata_failure_status
from autoscout.codex_recovery import _exact_list_metadata_failure_status
from autoscout.config import load_validated_config
from autoscout.source_collection import SourceCollection
from autoscout.source_collection import prepare_codex_extraction_worklist
from autoscout.source_collectors import CollectorRunError
from autoscout.source_collectors import codex_extraction_collector_result
from autoscout.source_collectors import contract_family
from autoscout.source_collectors import evidence_url_verified
from autoscout.source_collectors import extraction_contract
from autoscout.source_collectors import extraction_contract_violation

ROOT = Path(__file__).resolve().parents[1]
# These tests pin the 2026-08-01 evidence, so they must read immutable fixtures.
# Pointing them at the live weekly run directory made them fail whenever a later
# weekly run replaced the artifacts with valid ones.
RUN_DIR = Path(__file__).resolve().parent / "fixtures" / "issue-375-2026-08-01"

AUDIOBOOKS_NAME = "Audiobooks.com Nonfiction Audiobook Bestsellers"
AUDIOBOOKS_URL = "https://www.audiobooks.com/browse/non-fiction"
AUDIOBOOKS_CAPTURE = "codex-capture-audiobooks-com-nonfiction-audiobook-bestsellers-4e469401.json"
AUDIOBOOKS_EXTRACTION = "audiobooks-com-nonfiction-audiobook-bestsellers-4e469401-codex-extraction.json"

PEOPLE_NAME = "PEOPLE Books"
PEOPLE_URL = "https://people.com/books/"
PEOPLE_CAPTURE = "codex-capture-people-books-9659179a.json"
PEOPLE_EXTRACTION = "people-books-9659179a-codex-extraction.json"

RUN_DATE = "2026-08-01"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _portfolio_source(name: str) -> dict[str, Any]:
    config = load_validated_config(ROOT / "configs/current-weekly-portfolio.json")
    return dict(next(item for item in config["sources"] if item["name"] == name))


def _replay_source(name: str, capture: Path, extraction: Path) -> dict[str, Any]:
    return {
        **_portfolio_source(name),
        "extraction_method": "codex-extraction-replay",
        "source_page_fixture_file": str(capture),
        "extraction_response_file": str(extraction),
    }


def _collect(tmp_path: Path, source: dict[str, Any]) -> Any:
    return codex_extraction_collector_result(tmp_path / "config.json", tmp_path, source, RUN_DATE)


def _ranking_table_source() -> dict[str, Any]:
    config = load_validated_config(ROOT / "configs/current-weekly-portfolio.json")
    return dict(next(item for item in config["sources"] if item.get("metadata_ranking_table") is True))


# ── The Codex worklist carries the extraction contract ──────────────────


def _worklist_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, capture: Path) -> dict[str, Any]:
    source = _portfolio_source(name)
    config = tmp_path / "config.json"
    _write_json(config, {"run_date": RUN_DATE, "sources": [source]})
    captured = _read_json(capture)

    import autoscout.source_collection as collection_module

    def _replay_capture(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        page = {
            "source_page": captured.get("source_page", source["url"]),
            "resolved_url": captured.get("resolved_url", source["url"]),
            "html": captured["html"],
        }
        return page, {"resolved_url": page["resolved_url"]}

    monkeypatch.setattr(collection_module, "codex_source_page", _replay_capture)
    worklist = prepare_codex_extraction_worklist(config, tmp_path, RUN_DATE)
    assert len(worklist.work_items) == 1, worklist.source_runs
    return worklist.work_items[0]


def test_worklist_states_the_audiobook_eligibility_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = _worklist_item(tmp_path, monkeypatch, AUDIOBOOKS_NAME, RUN_DIR / AUDIOBOOKS_CAPTURE)
    contract = item["extraction_contract"]
    # A capture-bound contract names the mention array "entries", inside the
    # identity envelope the validator binds to the operation (Issue #386).
    shape = contract["required_output_shape"]["entries"][0]
    # The 2026-08-01 artifact omitted exactly these three fields.
    for field in ("underlying_work_type", "publication_status", "candidate_category"):
        assert field in shape, f"contract must declare {field}"
        assert field in contract["required_fields"], f"contract must require {field}"
    # It also used signal_type retailer_bestseller_ordering and strength strong,
    # neither of which the audiobook-ranking contract accepts.
    assert shape["signal_type"] == "audiobook ranking"
    assert shape["signal_strength"] == "minor mention"


def test_contract_requires_only_fields_the_validator_enforces() -> None:
    """Conditional fields must stay out of required_fields.

    ``publisher``, ``publication_year``, and ``autobiographical_subject_context``
    depend on what the page shows; demanding them would push an extractor into
    inventing them, which is the failure mode this contract exists to prevent.
    """
    contract = extraction_contract(_portfolio_source(PEOPLE_NAME))
    assert contract["required_fields"] == [
        "title",
        "primary_authors",
        "source_url",
        "evidence_url",
        "signal_type",
        "signal_strength",
        "evidence_note",
    ]
    shape = contract["required_output_shape"]["mentions"][0]
    for optional in ("publisher", "publication_year", "autobiographical_subject_context"):
        assert optional in shape
        assert optional not in contract["required_fields"]


def test_worklist_demands_capture_grounded_evidence_urls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = _worklist_item(tmp_path, monkeypatch, PEOPLE_NAME, RUN_DIR / PEOPLE_CAPTURE)
    rules = item["extraction_contract"]["rules"]
    joined = " ".join(rules).casefold()
    # The 2026-08-01 artifact invented people.com article URLs that are absent
    # from the capture; the contract must forbid that outright, not "prefer".
    assert "prefer evidence urls" not in joined
    assert "never construct, complete, or recall a url" in joined
    assert item["extraction_contract"]["capture_file"] == item["capture_file"]
    assert item["capture_file"] in " ".join(rules)


def test_grounding_rule_matches_what_the_collector_accepts() -> None:
    """The stated rule must not be stricter than ``evidence_url_verified``.

    That check accepts the configured source page itself, so a rule demanding a
    verbatim substring of the HTML would tell extractors to drop every
    ranking-table mention -- each of which cites the configured page.
    """
    joined = " ".join(extraction_contract(_ranking_table_source())["rules"]).casefold()
    assert "the configured source page url, matched exactly, or a link" in joined
    page = {"source_page": "https://example.test/list", "html": "<p>no links</p>"}
    assert evidence_url_verified(page, "https://example.test/list") == (True, "")


def test_grounding_rule_states_www_tolerance_only_where_it_applies() -> None:
    """The www. fallback covers page links, not the source-page match."""
    page = {
        "source_page": "https://example.test/list",
        "html": '<a href="https://www.example.test/book-a">Book A</a>',
    }
    # A www. variant of a captured link is accepted.
    assert evidence_url_verified(page, "https://example.test/book-a") == (True, "")
    # A www. variant of the source page itself is not, unless it is linked.
    verified, _reason = evidence_url_verified(page, "https://www.example.test/list")
    assert verified is False


def test_ranking_table_shape_offers_no_field_the_collector_prohibits() -> None:
    """A shape field the validator rejects is the same bug in the other direction.

    ``extraction_contract_violation`` drops a whole ranking-table mention for
    any field outside its allowed set, so offering ``publisher`` or
    ``publication_year`` in the shape invites a zero-mention run.
    """
    source = _ranking_table_source()
    shape = extraction_contract(source)["required_output_shape"]["mentions"][0]
    filled = {field: "x" for field in shape}
    reason = extraction_contract_violation(filled, filled, source, {"source_page": source["url"], "html": ""})
    assert "prohibited fields" not in reason, reason


@pytest.mark.parametrize(
    ("name", "evidence_hint"),
    [
        ("Audible Bestselling Science & Engineering Audiobooks", "/pd/"),
        ("Audible Bestselling Business & Careers Audiobooks", "/pd/"),
        ("Audible Bestselling Computers & Technology Audiobooks", "/pd/"),
        ("Audible.de Bestselling Biografien & Erinnerungen Hoerbuecher", "/pd/"),
        ("Amazon Charts Most Read Nonfiction", "/dp/"),
    ],
)
def test_chart_sources_get_only_their_chart_contract(name: str, evidence_hint: str) -> None:
    """A chart flag and metadata_exact_book_list on the same source must not mix.

    All five of these portfolio sources set both. The validator returns from
    the chart branch and never consults the exact-book-list checks, so a
    contract that described the exact-book-list shape would ask for a
    book-level anchor link where the collector demands a product URL - the
    zero-mention failure class this issue is about.
    """
    source = _portfolio_source(name)
    assert source["metadata_exact_book_list"] is True, "premise: this source sets both flags"

    contract = extraction_contract(source)

    shape = contract["required_output_shape"]["mentions"][0]
    assert evidence_hint in shape["evidence_url"]
    joined = " ".join(contract["rules"])
    assert "anchor text" not in joined, "exact-book-list rules must not reach a chart source"
    assert "exact book-level link" not in joined.casefold()


def test_contract_family_is_the_precedence_the_validator_uses() -> None:
    """One resolver, so the contract and the validator cannot disagree."""
    both = {"family": "audiobook ranking", "audible_explicit_chart": True, "metadata_exact_book_list": True}
    assert contract_family(both) == "audible-chart"
    assert contract_family({"amazon_charts_weekly": True, "metadata_exact_book_list": True}) == "amazon-charts"
    assert contract_family({"metadata_ranking_table": True}) == "ranking-table"
    assert contract_family({"family": "Audiobook Ranking"}) == "audiobook-ranking"
    assert contract_family({"metadata_exact_book_list": True}) == "exact-book-list"
    assert contract_family({"family": "award"}) == "generic"


def test_worklist_contract_matches_the_shared_prompt_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One producer, so the two paths cannot ask for divergent mention shapes.

    The two paths do differ at the top level, and must. A capture-bound
    extraction writes an artifact that is read back and bound to an operation,
    so it carries the identity envelope; the inline path returns its mentions
    to the caller in memory and has nothing to bind. What must never diverge is
    the mention shape itself, which is what Issue #375 is about.

    This assertion used to demand full equality, which forced the capture-bound
    contract to declare the bare shape the validator then refused (Issue #386).
    """
    item = _worklist_item(tmp_path, monkeypatch, AUDIOBOOKS_NAME, RUN_DIR / AUDIOBOOKS_CAPTURE)
    shared = extraction_contract(_portfolio_source(AUDIOBOOKS_NAME))
    worklist_contract = item["extraction_contract"]

    assert worklist_contract["required_output_shape"]["entries"] == shared["required_output_shape"]["mentions"]
    assert worklist_contract["required_fields"] == shared["required_fields"]
    # The Codex worklist adds capture-binding rules on top of the shared ones.
    assert set(shared["rules"]).issubset(set(worklist_contract["rules"]))


# ── Audiobooks.com: the 2026-08-01 artifact shape ───────────────────────


def test_2026_08_01_audiobooks_artifact_is_rejected(tmp_path: Path) -> None:
    source = _replay_source(AUDIOBOOKS_NAME, RUN_DIR / AUDIOBOOKS_CAPTURE, RUN_DIR / AUDIOBOOKS_EXTRACTION)
    with pytest.raises(CollectorRunError) as excinfo:
        _collect(tmp_path, source)
    debug = excinfo.value.debug_metadata
    assert debug["validated_mention_count"] == 0
    assert debug["dropped_mention_count"] == 17
    reasons = {drop["reason"] for drop in debug["drop_reasons"]}
    assert reasons == {"underlying work is not an eligible book/work"}


def _contract_conformant_audiobooks_entries() -> list[dict[str, Any]]:
    """Repair the 2026-08-01 entries to the contract the worklist now states."""
    entries = _read_json(RUN_DIR / AUDIOBOOKS_EXTRACTION)["entries"]
    repaired = []
    for entry in entries:
        repaired.append(
            {
                **entry,
                "candidate_category": "adult nonfiction",
                "underlying_work_type": "book",
                "publication_status": "published",
                "signal_type": "audiobook ranking",
                "signal_strength": "minor mention",
                "evidence_note": (
                    "Listed in the captured Non-Fiction category under Bestselling ordering; "
                    "implicit retailer popularity only."
                ),
            }
        )
    return repaired


def test_contract_conformant_audiobooks_artifact_replays_clean(tmp_path: Path) -> None:
    extraction = tmp_path / "audiobooks-extraction.json"
    _write_json(extraction, {"mentions": _contract_conformant_audiobooks_entries()})
    source = _replay_source(AUDIOBOOKS_NAME, RUN_DIR / AUDIOBOOKS_CAPTURE, extraction)

    result = _collect(tmp_path, source)

    assert result.valid_mention_count == 17
    assert result.skipped_item_count == 0
    capture_html = _read_json(RUN_DIR / AUDIOBOOKS_CAPTURE)["html"]
    for mention in result.mentions:
        assert mention["evidence_url"] in capture_html


def test_every_audiobook_required_field_is_one_the_collector_demands(tmp_path: Path) -> None:
    """Guard the contract against drifting away from the validator.

    ``required_fields`` and the checks in ``extraction_contract_violation`` are
    written separately. For this source, dropping any one required field from an
    otherwise clean extraction must cost the mention, or the contract is asking
    for something the collector does not need -- or, worse, has stopped asking
    for something it does. The base-field gap this does not cover is pinned by
    ``test_collector_url_check_is_looser_than_the_contract_asks``.
    """
    entries = _contract_conformant_audiobooks_entries()
    required = extraction_contract(_portfolio_source(AUDIOBOOKS_NAME))["required_fields"]

    for field in required:
        extraction = tmp_path / f"without-{field}.json"
        _write_json(extraction, {"mentions": [{k: v for k, v in e.items() if k != field} for e in entries]})
        source = _replay_source(AUDIOBOOKS_NAME, RUN_DIR / AUDIOBOOKS_CAPTURE, extraction)
        with pytest.raises(CollectorRunError):
            _collect(tmp_path, source)


@pytest.mark.parametrize(
    "category",
    ["adult nonfiction", "Adult Nonfiction", "adult_nonfiction", "non-fiction", "Non-Fiction", "history", "self-help"],
)
def test_plausible_audiobook_categories_collect(tmp_path: Path, category: str) -> None:
    """The contract asks for a free-text category, so plausible values must pass.

    ``candidate_category`` is required but has no closed vocabulary, and the
    audiobook branch never compares it to a configured value -- it only scans
    for excluded terms. Audiobooks.com titles its own category "Non-Fiction"
    and its URL ends in /browse/non-fiction, so copying that label off the page
    is the most natural reading of the contract. It must not cost every
    mention (Issue #375).
    """
    entries = [{**entry, "candidate_category": category} for entry in _contract_conformant_audiobooks_entries()]
    extraction = tmp_path / "audiobooks-extraction.json"
    _write_json(extraction, {"mentions": entries})
    source = _replay_source(AUDIOBOOKS_NAME, RUN_DIR / AUDIOBOOKS_CAPTURE, extraction)

    result = _collect(tmp_path, source)

    assert result.valid_mention_count == 17
    assert result.skipped_item_count == 0


@pytest.mark.parametrize("category", ["adult fiction", "adult_fiction", "kids books", "kids_books", "coming_soon"])
def test_ineligible_audiobook_categories_still_drop(tmp_path: Path, category: str) -> None:
    """Folding must not blunt the exclusion; ``_`` is a word character.

    Before the fold, ``\\bfiction\\b`` never matched ``adult_fiction`` at all.
    This PR blesses underscore spellings elsewhere in the validator, so the
    exclusion has to see them too.
    """
    entries = [{**entry, "candidate_category": category} for entry in _contract_conformant_audiobooks_entries()]
    extraction = tmp_path / "audiobooks-extraction.json"
    _write_json(extraction, {"mentions": entries})
    source = _replay_source(AUDIOBOOKS_NAME, RUN_DIR / AUDIOBOOKS_CAPTURE, extraction)

    with pytest.raises(CollectorRunError) as excinfo:
        _collect(tmp_path, source)
    reasons = {drop["reason"] for drop in excinfo.value.debug_metadata["drop_reasons"]}
    assert reasons == {"excluded or missing adult nonfiction category"}


def test_collector_url_check_is_looser_than_the_contract_asks(tmp_path: Path) -> None:
    """Pin the one place the contract asks for more than the collector checks.

    ``required_fields`` names both ``source_url`` and ``evidence_url``, but the
    raw check accepts any one of source_url / evidence_url / item_url and
    ``normalized_agent_mention`` fills the rest. The contract is deliberately
    the stricter of the two, so this is safe -- but a passing run is not proof
    every required field was supplied, and the operator docs say so. Tightening
    the collector changes collection behaviour beyond Issue #375.
    """
    urls = _captured_people_article_urls(1)
    entry = {
        "title": "Captured Book",
        "primary_authors": ["Captured Author"],
        "source_url": urls[0],
        "signal_type": "media/curated list",
        "signal_strength": "minor mention",
        "evidence_note": "Book-level article linked from the captured PEOPLE Books landing page.",
    }
    assert "evidence_url" in extraction_contract(_portfolio_source(PEOPLE_NAME))["required_fields"]
    extraction = tmp_path / "people-no-evidence-url.json"
    _write_json(extraction, {"mentions": [entry]})
    capture = _people_capture_naming(tmp_path, ["Captured Author"])
    source = _replay_source(PEOPLE_NAME, capture, extraction)

    result = _collect(tmp_path, source)

    assert result.valid_mention_count == 1
    assert result.mentions[0]["evidence_url"] == urls[0]


# ── PEOPLE Books: the 2026-08-01 artifact grounding failure ─────────────


def test_2026_08_01_people_books_artifact_is_rejected(tmp_path: Path) -> None:
    source = _replay_source(PEOPLE_NAME, RUN_DIR / PEOPLE_CAPTURE, RUN_DIR / PEOPLE_EXTRACTION)
    with pytest.raises(CollectorRunError) as excinfo:
        _collect(tmp_path, source)
    debug = excinfo.value.debug_metadata
    assert debug["safe_failure_status"] == "unverifiable_evidence_urls"
    assert debug["validated_mention_count"] == 0
    assert debug["dropped_mention_count"] == 4
    assert {drop["reason"] for drop in debug["drop_reasons"]} == {"evidence URL not found on source page"}
    # The invented URLs are genuinely absent from the committed capture.
    capture_html = _read_json(RUN_DIR / PEOPLE_CAPTURE)["html"]
    for drop in debug["drop_reasons"]:
        assert drop["evidence_url"] not in capture_html


def _people_capture_naming(tmp_path: Path, authors: list[str]) -> Path:
    """Copy the committed PEOPLE capture with a byline naming *authors*.

    Admission refuses an author the capture does not name (Issue #850); these
    tests are about evidence URLs, so the copy names the invented authors.
    """
    capture = _read_json(RUN_DIR / PEOPLE_CAPTURE)
    capture["html"] += "<p>by " + ", ".join(authors) + "</p>"
    path = tmp_path / "people-capture-with-byline.json"
    _write_json(path, capture)
    return path


def _captured_people_article_urls(limit: int) -> list[str]:
    capture_html = _read_json(RUN_DIR / PEOPLE_CAPTURE)["html"]
    hrefs = re.findall(r'href="(https://people\.com/[a-z0-9-]+-\d{7,})"', capture_html)
    unique = list(dict.fromkeys(hrefs))
    assert len(unique) >= limit, "capture must contain book-article links to ground against"
    return unique[:limit]


def test_capture_grounded_people_books_artifact_replays_clean(tmp_path: Path) -> None:
    urls = _captured_people_article_urls(4)
    entries = [
        {
            "title": f"Captured Book {index}",
            "primary_authors": [f"Captured Author {index}"],
            "source_url": PEOPLE_URL,
            "evidence_url": url,
            "signal_type": "media/curated list",
            "signal_strength": "minor mention",
            "evidence_note": "Book-level article linked from the captured PEOPLE Books landing page.",
            "candidate_category": "adult nonfiction",
        }
        for index, url in enumerate(urls, start=1)
    ]
    extraction = tmp_path / "people-extraction.json"
    _write_json(extraction, {"mentions": entries})
    capture = _people_capture_naming(tmp_path, [entry["primary_authors"][0] for entry in entries])
    source = _replay_source(PEOPLE_NAME, capture, extraction)

    result = _collect(tmp_path, source)

    assert result.valid_mention_count == 4
    assert result.skipped_item_count == 0
    capture_html = _read_json(RUN_DIR / PEOPLE_CAPTURE)["html"]
    for mention in result.mentions:
        assert mention["evidence_url"] in capture_html


# ── candidate_category separator normalization ──────────────────────────


def _exact_book_list_source() -> dict[str, Any]:
    return {
        "name": "Exact List",
        "family": "media/curated list",
        "url": "https://example.test/list",
        "language": "en",
        "metadata_exact_book_list": True,
        "expected_signal_strength": "major media list",
        "eligible_candidate_category": "adult nonfiction",
    }


def _exact_book_list_page() -> dict[str, Any]:
    return {
        "source_page": "https://example.test/list",
        "html": (
            '<ul><li><a href="https://example.test/book-a">A Field Guide to Memory</a><span>by Nora Vale</span></li></ul>'
        ),
    }


def _exact_book_list_entry(category: str) -> dict[str, Any]:
    return {
        "title": "A Field Guide to Memory",
        "primary_authors": ["Nora Vale"],
        "source_url": "https://example.test/list",
        "evidence_url": "https://example.test/book-a",
        "signal_type": "media/curated list",
        "signal_strength": "major media list",
        "evidence_note": "Linked list entry.",
        "candidate_category": category,
        "english_title": "A Field Guide to Memory",
        "search_translation": "Путеводитель по памяти",
        "author_transliteration_note": "Нора Вейл",
    }


@pytest.mark.parametrize(
    "category", ["adult nonfiction", "adult_nonfiction", "Adult-Nonfiction", " adult  nonfiction "]
)
def test_candidate_category_separator_forms_are_equivalent(category: str) -> None:
    entry = _exact_book_list_entry(category)
    assert extraction_contract_violation(entry, entry, _exact_book_list_source(), _exact_book_list_page()) == ""


def test_candidate_category_normalization_does_not_accept_a_different_category() -> None:
    entry = _exact_book_list_entry("adult_fiction")
    reason = extraction_contract_violation(entry, entry, _exact_book_list_source(), _exact_book_list_page())
    assert "candidate_category" in reason


def test_resume_validator_folds_candidate_category_like_the_collector() -> None:
    """The recovery path must not quarantine what the collector accepts.

    ``_validate_extraction_artifact`` runs this check before replay and forces
    a re-extraction on failure, so a stricter comparison here would bypass the
    collector's tolerance for every exact-book-list source.
    """
    source = _exact_book_list_source()
    work_item = {
        "source_family": source["family"],
        "source_metadata": {
            "metadata_exact_book_list": True,
            "language": "en",
            "family": source["family"],
            "expected_signal_strength": source["expected_signal_strength"],
            "eligible_candidate_category": source["eligible_candidate_category"],
        },
    }
    underscored = _exact_book_list_entry("adult_nonfiction")
    assert extraction_contract_violation(underscored, underscored, source, _exact_book_list_page()) == ""
    assert _exact_list_metadata_failure_status(underscored, work_item) == ""

    different = _exact_book_list_entry("adult_fiction")
    assert _exact_list_metadata_failure_status(different, work_item) == "invalid_candidate_category"


# ── Committed pilot replay stays healthy for both sources ───────────────


@pytest.mark.parametrize("value", [{"retained": "value"}, ["retained"], True, None, "unknown", "TODO", "N/A"])
def test_recovery_artifact_rejects_invalid_helper_values(tmp_path: Path, value: Any) -> None:
    source = _exact_book_list_source()
    work_item = {
        "source_family": source["family"],
        "source_metadata": {
            "metadata_exact_book_list": True,
            "language": "en",
            "family": source["family"],
            "expected_signal_strength": source["expected_signal_strength"],
            "eligible_candidate_category": source["eligible_candidate_category"],
        },
    }
    entry = _exact_book_list_entry("adult nonfiction")
    entry["english_title"] = value
    artifact = tmp_path / "extraction.json"
    _write_json(artifact, {"mentions": [entry]})
    before = artifact.read_bytes()

    assert _artifact_exact_list_metadata_failure_status(artifact, work_item) == "invalid_helper_metadata"
    assert artifact.read_bytes() == before


def test_pilot_replay_collects_both_sources(tmp_path: Path) -> None:
    config_path = ROOT / "configs/current-weekly-pilot.json"
    config = load_validated_config(config_path)
    sources = [s for s in config["sources"] if s.get("name") in {AUDIOBOOKS_NAME, PEOPLE_NAME}]
    assert len(sources) == 2

    result = SourceCollection(config_path, tmp_path, str(config["run_date"])).collect(sources)

    for run in result.source_runs:
        assert run["status"] == "collected", run
        assert run["valid_mention_count"] > 0, run
