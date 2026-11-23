from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from urllib.request import Request

import autoscout.source_collectors as collectors_module
from autoscout.codex_workflow import finalize_codex_run
from autoscout.codex_workflow import prepare_codex_extraction
from autoscout.config import load_validated_config
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.source_collectors import COLLECTOR_REGISTRY
from autoscout.weekly_workflow import _bind_worklist_to_operation
from autoscout.weekly_workflow import _capture_is_current
from autoscout.weekly_workflow import _operation_id
from tests.fixtures import copy_fixture

RUN_DATE = "2026-07-17"
ROOT = Path(__file__).resolve().parents[1]


class _Response:
    def __init__(self, url: str, body: str) -> None:
        self._url = url
        self._body = body.encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url


def _source(**overrides: object) -> dict[str, object]:
    source: dict[str, object] = {
        "name": "Nonfiction.fr Critiques",
        "family": "media/curated list",
        "url": "https://www.nonfiction.fr/rss-critiques.xml",
        "active": True,
        "access_status": "open",
        "check_frequency": "weekly",
        "language": "fr",
        "extraction_method": "nonfiction-fr-rss-replay",
    }
    source.update(overrides)
    return source


def test_replay_collects_only_complete_book_metadata_without_article_text(tmp_path: Path) -> None:
    rss_path = copy_fixture("nonfiction-fr", "rss-mixed.xml", tmp_path / "rss.xml")
    pages_path = copy_fixture("nonfiction-fr", "review-pages-mixed.json", tmp_path / "pages.json")
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    source = _source(rss_fixture_file=str(rss_path), review_pages_fixture_file=str(pages_path))

    result = COLLECTOR_REGISTRY["nonfiction-fr-rss-replay"].collect(config_path, tmp_path / "output", source, RUN_DATE)

    assert result.raw_mention_count == 3
    assert result.valid_mention_count == 1
    assert result.skipped_item_count == 2
    assert result.mentions == [
        {
            "title": "Cartographier les futurs",
            "primary_authors": ["Camille Exemple"],
            "publisher": "Éditions Test",
            "publication_year": "2026",
            "source_url": "https://www.nonfiction.fr/article-100-complete-book.htm",
            "evidence_url": "https://www.nonfiction.fr/article-100-complete-book.htm",
            "source_check_date": RUN_DATE,
            "signal_type": "media/curated list",
            "signal_strength": "minor mention",
            "evidence_note": "Reviewed by Nonfiction.fr on 17 July 2026; metadata-only evidence.",
            "candidate_category": "adult nonfiction",
        }
    ]
    serialized = json.dumps({"mentions": result.mentions, "debug_metadata": result.debug_metadata}, ensure_ascii=False)
    assert "synthetic review text" not in serialized.lower()
    assert "synthetic full article" not in serialized.lower()
    assert "synthetic review prose" not in serialized.lower()


def test_replay_processes_only_guids_absent_from_seen_state(tmp_path: Path) -> None:
    rss_path = copy_fixture("nonfiction-fr", "rss-mixed.xml", tmp_path / "rss.xml")
    pages_path = copy_fixture("nonfiction-fr", "review-pages-mixed.json", tmp_path / "pages.json")
    seen_path = copy_fixture("nonfiction-fr", "seen-guids.json", tmp_path / "seen.json")
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    source = _source(
        rss_fixture_file=str(rss_path),
        review_pages_fixture_file=str(pages_path),
        seen_guids_fixture_file=str(seen_path),
    )

    result = COLLECTOR_REGISTRY["nonfiction-fr-rss-replay"].collect(config_path, tmp_path / "output", source, RUN_DATE)

    assert result.mentions == []
    assert result.raw_mention_count == 2
    assert result.skipped_item_count == 2
    assert result.debug_metadata["seen_guid_count"] == 1
    assert result.debug_metadata["new_guid_count"] == 2


def test_live_fixture_replay_persists_only_processed_guids_and_is_incremental(tmp_path: Path) -> None:
    rss_path = copy_fixture("nonfiction-fr", "rss-mixed.xml", tmp_path / "rss.xml")
    pages_path = copy_fixture("nonfiction-fr", "review-pages-mixed.json", tmp_path / "pages.json")
    seen_path = tmp_path / "state" / "nonfiction-fr-seen-guids.json"
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    source = _source(
        extraction_method="nonfiction-fr-rss-live",
        enable_live=True,
        rss_fixture_file=str(rss_path),
        review_pages_fixture_file=str(pages_path),
        seen_guids_file=str(seen_path),
    )

    first = COLLECTOR_REGISTRY["nonfiction-fr-rss-live"].collect(config_path, tmp_path / "output", source, RUN_DATE)
    second = COLLECTOR_REGISTRY["nonfiction-fr-rss-live"].collect(config_path, tmp_path / "output", source, RUN_DATE)

    assert [mention["title"] for mention in first.mentions] == ["Cartographier les futurs"]
    assert second.mentions == []
    assert second.raw_mention_count == 0
    state = read_json(seen_path)
    assert [entry["guid"] for entry in state["seen_guids"]] == [
        "https://www.nonfiction.fr/article-100-complete-book.htm",
        "https://www.nonfiction.fr/article-101-incomplete-book.htm",
        "https://www.nonfiction.fr/article-102-not-a-book.htm",
    ]
    assert {entry["first_seen_date"] for entry in state["seen_guids"]} == {RUN_DATE}
    serialized = json.dumps(state, ensure_ascii=False).lower()
    assert "synthetic" not in serialized
    assert set(state) == {"source", "updated_at", "seen_guids"}


def test_live_collection_fetches_rss_then_review_pages_at_the_http_boundary(tmp_path: Path, monkeypatch) -> None:
    rss = (Path(__file__).parent / "fixtures/nonfiction-fr/rss-mixed.xml").read_text(encoding="utf-8")
    page_fixture = json.loads(
        (Path(__file__).parent / "fixtures/nonfiction-fr/review-pages-mixed.json").read_text(encoding="utf-8")
    )["pages"]

    def fake_urlopen(request: Request, timeout: float) -> _Response:
        assert timeout == 5.0
        url = request.full_url
        if url == "https://www.nonfiction.fr/rss-critiques.xml":
            return _Response(url, rss)
        return _Response(url, page_fixture[url]["html"])

    monkeypatch.setattr(collectors_module, "urlopen", fake_urlopen)
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    source = _source(
        extraction_method="nonfiction-fr-rss-live",
        enable_live=True,
        timeout_seconds=5,
        seen_guids_file=str(tmp_path / "seen.json"),
    )

    result = COLLECTOR_REGISTRY["nonfiction-fr-rss-live"].collect(config_path, tmp_path / "output", source, RUN_DATE)

    assert [mention["title"] for mention in result.mentions] == ["Cartographier les futurs"]
    assert result.debug_metadata["live_fetch"] is True
    assert result.debug_metadata["new_guid_count"] == 3


def test_weekly_prepare_finalize_replays_only_precollected_metadata(tmp_path: Path) -> None:
    rss_path = copy_fixture("nonfiction-fr", "rss-mixed.xml", tmp_path / "rss.xml")
    pages_path = copy_fixture("nonfiction-fr", "review-pages-mixed.json", tmp_path / "pages.json")
    config_path = tmp_path / "live.json"
    config_path.write_text(
        json.dumps(
            {
                "run_date": RUN_DATE,
                "sources": [
                    _source(
                        extraction_method="nonfiction-fr-rss-live",
                        enable_live=True,
                        rss_fixture_file=str(rss_path),
                        review_pages_fixture_file=str(pages_path),
                        seen_guids_file=str(tmp_path / "seen.json"),
                    )
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"

    assert prepare_codex_extraction(config_path, output_dir, effective_date=RUN_DATE) == 0
    worklist = read_json(output_dir / "codex-extraction-worklist.json")
    assert worklist["work_items"] == []
    assert len(worklist["precollected_sources"]) == 1
    artifact_path = output_dir / worklist["precollected_sources"][0]["recorded_mentions_file"]
    artifact_text = artifact_path.read_text(encoding="utf-8")
    assert "Cartographier les futurs" in artifact_text
    assert "synthetic" not in artifact_text.lower()

    assert finalize_codex_run(output_dir, config_path, effective_date=RUN_DATE) == 0
    replay_source = read_json(output_dir / "codex-replay-config.json")["sources"][0]
    assert replay_source["extraction_method"] == "recorded"
    assert replay_source["recorded_mentions_file"] == artifact_path.name


def test_weekly_resume_requires_the_exact_precollected_metadata_artifact(tmp_path: Path) -> None:
    rss_path = copy_fixture("nonfiction-fr", "rss-mixed.xml", tmp_path / "rss.xml")
    pages_path = copy_fixture("nonfiction-fr", "review-pages-mixed.json", tmp_path / "pages.json")
    config_path = tmp_path / "live.json"
    config_path.write_text(
        json.dumps(
            {
                "run_date": RUN_DATE,
                "sources": [
                    _source(
                        extraction_method="nonfiction-fr-rss-live",
                        enable_live=True,
                        rss_fixture_file=str(rss_path),
                        review_pages_fixture_file=str(pages_path),
                        seen_guids_file=str(tmp_path / "seen.json"),
                    )
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"

    assert prepare_codex_extraction(config_path, output_dir, effective_date=RUN_DATE) == 0
    operation_id = _operation_id(config_path, date.fromisoformat(RUN_DATE))
    _bind_worklist_to_operation(output_dir, operation_id)
    write_json(
        output_dir / "codex-workflow-resume.json",
        {"run_id": operation_id, "completed_stages": ["capture"]},
    )

    assert _capture_is_current(output_dir, config_path, date.fromisoformat(RUN_DATE)) is True

    worklist = read_json(output_dir / "codex-extraction-worklist.json")
    artifact = output_dir / worklist["precollected_sources"][0]["recorded_mentions_file"]
    artifact.write_text('{"mentions": []}\n', encoding="utf-8")

    assert _capture_is_current(output_dir, config_path, date.fromisoformat(RUN_DATE)) is False


def test_current_weekly_configs_add_only_nonfiction_fr_as_an_established_french_source() -> None:
    live = load_validated_config(ROOT / "configs/current-weekly-pilot-live.json")
    replay = load_validated_config(ROOT / "configs/current-weekly-pilot.json")
    live_source = next(source for source in live["sources"] if source["name"] == "Nonfiction.fr Critiques")
    replay_source = next(source for source in replay["sources"] if source["name"] == "Nonfiction.fr Critiques")

    assert live_source["extraction_method"] == "nonfiction-fr-rss-live"
    assert live_source["enable_live"] is True
    assert live_source["lifecycle_status"] == "established"
    assert live_source["language"] == "fr"
    assert live_source["expected_category"] == "adult nonfiction"
    assert replay_source["extraction_method"] == "nonfiction-fr-rss-replay"
    assert replay_source["rss_fixture_file"].endswith("nonfiction-fr-rss.xml")
    assert replay_source["review_pages_fixture_file"].endswith("nonfiction-fr-review-pages.json")
    assert all("Livres Hebdo" not in source["name"] for source in live["sources"])
    assert all("Livres Hebdo" not in source["name"] for source in replay["sources"])


def test_duplicate_guid_in_one_feed_is_processed_once(tmp_path: Path) -> None:
    rss_path = copy_fixture("nonfiction-fr", "rss-mixed.xml", tmp_path / "rss.xml")
    rss = rss_path.read_text(encoding="utf-8")
    first_item = rss[rss.index("    <item>") : rss.index("    </item>") + len("    </item>")]
    rss_path.write_text(rss.replace("  </channel>", f"{first_item}\n  </channel>"), encoding="utf-8")
    pages_path = copy_fixture("nonfiction-fr", "review-pages-mixed.json", tmp_path / "pages.json")
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    result = COLLECTOR_REGISTRY["nonfiction-fr-rss-replay"].collect(
        config_path,
        tmp_path / "output",
        _source(rss_fixture_file=str(rss_path), review_pages_fixture_file=str(pages_path)),
        RUN_DATE,
    )

    assert [mention["title"] for mention in result.mentions] == ["Cartographier les futurs"]
    assert result.debug_metadata["feed_item_count"] == 4
    assert result.debug_metadata["new_guid_count"] == 3


def _write_helper_sidecar(tmp_path: Path) -> Path:
    sidecar = {
        "version": 1,
        "source_url": "https://www.nonfiction.fr/rss-critiques.xml",
        "language": "fr",
        "entries": [
            {
                "title": "Cartographier les futurs",
                "author": "Camille Exemple",
                "evidence_url": "https://www.nonfiction.fr/article-100-complete-book.htm",
                "english_title": "Mapping Futures",
                "search_translation": "Картографирование будущего",
                "author_transliteration_note": "Камиль Экзампль",
            }
        ],
    }
    path = tmp_path / "helper.json"
    path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_helper_sidecar_attaches_english_helpers_to_french_mentions(tmp_path: Path) -> None:
    """A curated sidecar gives the RSS collector the same helper fields (Issue #645)."""
    import hashlib

    rss_path = copy_fixture("nonfiction-fr", "rss-mixed.xml", tmp_path / "rss.xml")
    pages_path = copy_fixture("nonfiction-fr", "review-pages-mixed.json", tmp_path / "pages.json")
    sidecar_path = _write_helper_sidecar(tmp_path)
    digest = hashlib.sha256(sidecar_path.read_bytes()).hexdigest()
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    source = _source(
        rss_fixture_file=str(rss_path),
        review_pages_fixture_file=str(pages_path),
        helper_metadata_file=str(sidecar_path),
        helper_metadata_sha256=digest,
    )

    result = COLLECTOR_REGISTRY["nonfiction-fr-rss-replay"].collect(config_path, tmp_path / "output", source, RUN_DATE)

    assert len(result.mentions) == 1
    mention = result.mentions[0]
    # The authoritative French metadata is untouched; helpers are additive.
    assert mention["title"] == "Cartographier les futurs"
    assert mention["primary_authors"] == ["Camille Exemple"]
    assert mention["english_title"] == "Mapping Futures"
    assert mention["search_translation"] == "Картографирование будущего"
    assert mention["author_transliteration_note"] == "Камиль Экзампль"
    assert result.debug_metadata["helper_attached_count"] == 1


def test_replay_without_sidecar_reports_zero_attached_helpers(tmp_path: Path) -> None:
    """Without curated data the mentions flow without helpers (Issue #645 deferral)."""
    rss_path = copy_fixture("nonfiction-fr", "rss-mixed.xml", tmp_path / "rss.xml")
    pages_path = copy_fixture("nonfiction-fr", "review-pages-mixed.json", tmp_path / "pages.json")
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    source = _source(rss_fixture_file=str(rss_path), review_pages_fixture_file=str(pages_path))

    result = COLLECTOR_REGISTRY["nonfiction-fr-rss-replay"].collect(config_path, tmp_path / "output", source, RUN_DATE)

    assert len(result.mentions) == 1
    assert "english_title" not in result.mentions[0]
    assert result.debug_metadata["helper_attached_count"] == 0


ARTICLE_URL = "https://www.nonfiction.fr/article-200-review-card.htm"


def _mention(
    title: str,
    authors: list[str],
    year: str,
    publisher: str,
) -> dict[str, object]:
    return {
        "title": title,
        "primary_authors": authors,
        "publisher": publisher,
        "publication_year": year,
        "source_url": ARTICLE_URL,
        "evidence_url": ARTICLE_URL,
        "source_check_date": RUN_DATE,
        "signal_type": "media/curated list",
        "signal_strength": "minor mention",
        "evidence_note": "Reviewed by Nonfiction.fr on 17 July 2026; metadata-only evidence.",
        "candidate_category": "adult nonfiction",
    }


def _collect_joint_review(tmp_path: Path, pages_fixture: str):
    rss_path = copy_fixture("nonfiction-fr", "rss-joint-review.xml", tmp_path / "rss.xml")
    pages_path = copy_fixture("nonfiction-fr", pages_fixture, tmp_path / "pages.json")
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    source = _source(rss_fixture_file=str(rss_path), review_pages_fixture_file=str(pages_path))
    return COLLECTOR_REGISTRY["nonfiction-fr-rss-replay"].collect(config_path, tmp_path / "output", source, RUN_DATE)


def test_joint_review_card_emits_one_mention_per_book(tmp_path: Path) -> None:
    """A joint review keeps every reviewed book as its own mention (#695 defect 1)."""
    result = _collect_joint_review(tmp_path, "review-pages-joint-two-books.json")

    assert result.mentions == [
        _mention("Une préhistoire des femmes", ["Anne Augereau"], "2026", "La Découverte"),
        _mention("Aux origines de la domination masculine", ["Claudine Cohen"], "2025", "Passés composés"),
    ]
    assert result.raw_mention_count == 2
    assert result.valid_mention_count == 2
    assert result.raw_mention_count >= result.valid_mention_count
    assert result.skipped_item_count == 0


def test_book_blocks_ignore_card_content_before_the_first_title() -> None:
    """Author links before the first <h6> belong to no book; each block keeps its own facts."""
    from autoscout.source_collectors import nonfiction_fr_book_blocks

    markup = (
        '<aside class="post-content__book">'
        '<p class="ma0"><a class="tc-grey no-underline" href="fiche-auteur-1.htm">Auteur Fantôme</a></p>'
        "<div><h6>Premier livre</h6>"
        '<p><a href="fiche-auteur-2.htm">Auteur Un</a></p>'
        '<p class="f-pn">2026</p><p class="f-pn">Éditions Un</p><p class="f-pn">100 pages</p></div>'
        "<div><h6>Second livre</h6>"
        '<p><a href="fiche-auteur-3.htm">Auteur Deux</a></p>'
        '<p class="f-pn">2025</p><p class="f-pn">Éditions Deux</p><p class="f-pn">200 pages</p></div>'
        "</aside>"
    )

    assert nonfiction_fr_book_blocks(markup) == [
        {
            "title": "Premier livre",
            "primary_authors": ["Auteur Un"],
            "publication_year": "2026",
            "publisher": "Éditions Un",
        },
        {
            "title": "Second livre",
            "primary_authors": ["Auteur Deux"],
            "publication_year": "2025",
            "publisher": "Éditions Deux",
        },
    ]


def test_single_book_with_two_authors_keeps_both_authors(tmp_path: Path) -> None:
    """One block with several author links is one book with genuine co-authors."""
    result = _collect_joint_review(tmp_path, "review-pages-single-coauthors.json")

    assert result.mentions == [
        _mention("The Rivers North of the Future", ["Ivan Illich", "David Cayley"], "2005", "House of Anansi Press"),
    ]
    assert result.raw_mention_count == 1
    assert result.valid_mention_count == 1
    assert result.skipped_item_count == 0


def test_incomplete_block_in_a_joint_card_is_dropped_its_complete_sibling_kept(tmp_path: Path) -> None:
    """Failing closed is per book: the complete sibling still becomes a mention."""
    result = _collect_joint_review(tmp_path, "review-pages-joint-incomplete-sibling.json")

    assert result.mentions == [
        _mention("Une préhistoire des femmes", ["Anne Augereau"], "2026", "La Découverte"),
    ]
    assert result.debug_metadata["drop_reasons"] == [{"guid": ARTICLE_URL, "reason": "incomplete_book_in_card"}]
    assert result.raw_mention_count == 2
    assert result.valid_mention_count == 1
    assert result.skipped_item_count == 1


def test_joint_card_without_any_complete_book_counts_each_dropped_book(tmp_path: Path) -> None:
    """Every counted book block has a drop when none can become a mention."""
    result = _collect_joint_review(tmp_path, "review-pages-joint-no-complete.json")

    assert result.mentions == []
    assert result.debug_metadata["drop_reasons"] == [
        {"guid": ARTICLE_URL, "reason": "not_a_complete_book_review"},
        {"guid": ARTICLE_URL, "reason": "not_a_complete_book_review"},
    ]
    assert result.debug_metadata["processed_guid_count"] == 1
    assert result.raw_mention_count == 2
    assert result.valid_mention_count == 0
    assert result.skipped_item_count == 2


def test_extra_image_inside_a_book_container_does_not_split_its_metadata() -> None:
    """A secondary image inside a book cannot separate its title and author."""
    from autoscout.source_collectors import nonfiction_fr_book_blocks

    markup = (
        '<aside class="post-content__book">'
        '<div class="flex items-start"><img src="cover-1.jpg"><h6>Premier livre</h6>'
        '<img src="badge.png"><a href="fiche-auteur-1.htm">Auteur Un</a>'
        '<p class="f-pn">2026</p><p class="f-pn">Éditions Un</p></div>'
        '<div class="flex items-start"><img src="cover-2.jpg"><h6>Second livre</h6>'
        '<a href="fiche-auteur-2.htm">Auteur Deux</a>'
        '<p class="f-pn">2025</p><p class="f-pn">Éditions Deux</p></div>'
        "</aside>"
    )

    assert nonfiction_fr_book_blocks(markup) == [
        {
            "title": "Premier livre",
            "primary_authors": ["Auteur Un"],
            "publication_year": "2026",
            "publisher": "Éditions Un",
        },
        {
            "title": "Second livre",
            "primary_authors": ["Auteur Deux"],
            "publication_year": "2025",
            "publisher": "Éditions Deux",
        },
    ]


def test_untitled_sibling_never_folds_into_the_preceding_book(tmp_path: Path) -> None:
    """A book container without an <h6> must not lend its author or facts to its neighbour."""
    result = _collect_joint_review(tmp_path, "review-pages-joint-untitled-sibling.json")

    assert result.mentions == [
        _mention("Une préhistoire des femmes", ["Anne Augereau"], "2026", "La Découverte"),
        _mention("Cartographier les futurs", ["Camille Exemple"], "2024", "Éditions Test"),
    ]
    assert result.debug_metadata["drop_reasons"] == [{"guid": ARTICLE_URL, "reason": "incomplete_book_in_card"}]
    assert result.raw_mention_count == 3
    assert result.valid_mention_count == 2


def test_untitled_sibling_with_only_an_author_stays_out_of_the_preceding_book() -> None:
    """Without a repeated year or page count, the cover still separates the two books."""
    from autoscout.source_collectors import nonfiction_fr_book_blocks

    markup = (
        '<aside class="post-content__book">'
        '<div class="flex"><img src="cover-1.jpg" alt=""><div><h6>Premier livre</h6>'
        '<p><a href="fiche-auteur-2.htm">Auteur Un</a></p>'
        '<p class="f-pn">2026</p><p class="f-pn">Éditions Un</p></div></div>'
        '<div class="flex"><img src="cover-2.jpg" alt=""><div>'
        '<p><a href="fiche-auteur-3.htm">Auteur Deux</a></p>'
        '<p class="f-pn">Éditions Deux</p></div></div>'
        "</aside>"
    )

    assert nonfiction_fr_book_blocks(markup) == [
        {
            "title": "Premier livre",
            "primary_authors": ["Auteur Un"],
            "publication_year": "2026",
            "publisher": "Éditions Un",
        },
        {
            "title": "",
            "primary_authors": ["Auteur Deux"],
            "publication_year": "",
            "publisher": "Éditions Deux",
        },
    ]


def test_cover_container_with_two_titles_yields_no_complete_book() -> None:
    """Two titles under one cover cannot be told apart, so neither book is emitted."""
    from autoscout.source_collectors import nonfiction_fr_book_blocks

    markup = (
        '<aside class="post-content__book">'
        '<div class="flex"><img src="cover-1.jpg" alt=""><div>'
        '<h6>Premier livre</h6><p><a href="fiche-auteur-2.htm">Auteur Un</a></p>'
        '<h6>Second livre</h6><p><a href="fiche-auteur-3.htm">Auteur Deux</a></p>'
        '<p class="f-pn">2026</p><p class="f-pn">Éditions Un</p></div></div>'
        "</aside>"
    )

    blocks = nonfiction_fr_book_blocks(markup)

    assert [block["title"] for block in blocks] == ["Premier livre", "Second livre"]
    assert all(not block["primary_authors"] and not block["publisher"] for block in blocks)
