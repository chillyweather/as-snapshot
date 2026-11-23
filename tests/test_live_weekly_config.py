import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from state_store_support import read_state

import autoscout.cli.weekly as cli
import autoscout.source_collectors as source_collectors
from autoscout.config import resolve_weekly_portfolio
from autoscout.source_page_identity import parse_visible_page_identity
from autoscout.source_page_identity import ranking_page_identity
from autoscout.source_page_identity import ranking_row_bounds

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_CONFIG = REPO_ROOT / "configs" / "current-weekly-live.json"
REHEARSAL_CONFIG = REPO_ROOT / "configs" / "current-weekly-pilot.json"
PM_DEALS_SOURCE_NAME = "Publishers Marketplace Deals"
PM_DEALS_REPLAY_RESULT = "data/current-weekly-pilot/pm-deals-replay-collection.json"
PW_ISSUE_SOURCE_NAME = "Publishers Weekly Nonfiction Reviews"
PW_ISSUE_REPLAY_RESULT = "data/current-weekly-pilot/pw-issue-replay-collection.json"
BOKLISTA_SOURCE_NAME = "Bokhandlerforeningen Boklista - Generell litteratur voksne"

_PUBLISHNEWS_RETAINED_HELPER = {
    "english_title": "Good Morning, Winter",
    "search_translation": "Доброе утро, зима",
    "author_transliteration_note": "Тамара Клинк",
}


def _valid_isbn13(index: int) -> str:
    prefix = f"978000000{index:03d}"
    checksum = (
        10 - sum(int(digit) * (1 if position % 2 == 0 else 3) for position, digit in enumerate(prefix)) % 10
    ) % 10
    return prefix + str(checksum)


_EXACT_TEST_BOOKS = {
    "en": {
        "title": "The Cartographer's Lantern",
        "english_title": "The Cartographer's Lantern",
        "search_translation": "Фонарь картографа",
        "author": "Mara Ellison",
        "author_transliteration_note": "Мара Эллисон",
    },
    "de": {
        "title": "Die Laterne des Kartografen",
        "english_title": "The Cartographer's Lantern",
        "search_translation": "Фонарь картографа",
        "author": "Mara Ellison",
        "author_transliteration_note": "Мара Эллисон",
    },
    "it": {
        "title": "La lanterna del cartografo",
        "english_title": "The Cartographer's Lantern",
        "search_translation": "Фонарь картографа",
        "author": "Mara Ellison",
        "author_transliteration_note": "Мара Эллисон",
    },
    "es": {
        "title": "La linterna del cartógrafo",
        "english_title": "The Cartographer's Lantern",
        "search_translation": "Фонарь картографа",
        "author": "Mara Ellison",
        "author_transliteration_note": "Мара Эллисон",
    },
    "cs": {
        "title": "Kartografova lucerna",
        "english_title": "The Cartographer's Lantern",
        "search_translation": "Фонарь картографа",
        "author": "Mara Ellison",
        "author_transliteration_note": "Мара Эллисон",
    },
}


def _exact_test_book(source: dict) -> dict[str, str]:
    language = str(source.get("language", "en")).strip().casefold()
    return _EXACT_TEST_BOOKS.get(language, _EXACT_TEST_BOOKS["en"])


def _circana_week(page_data: dict) -> str:
    """The week the proof page's source note states, as the page writes it."""
    match = re.search(r"Source: Circana BookScan for ([A-Z][a-z]{2}\. \d{1,2}, \d{4})", str(page_data["html"]))
    if match is None:
        raise AssertionError("ranking proof page must state its Circana week")
    return match.group(1)


def _load_resolved(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return resolve_weekly_portfolio(raw, path)


class CurrentWeeklyLiveConfigTests(unittest.TestCase):
    def test_live_config_is_separate_configured_source_only_real_live_shape(self):
        live_config = _load_resolved(LIVE_CONFIG)
        rehearsal_config = _load_resolved(REHEARSAL_CONFIG)

        self.assertTrue(REHEARSAL_CONFIG.exists())
        self.assertGreater(len(live_config["sources"]), 0)
        self.assertEqual(
            [source["name"] for source in live_config["sources"]],
            [source["name"] for source in rehearsal_config["sources"]],
        )

        deterministic_fields = {
            "source_page_fixture_file",
            "extraction_response_file",
            "real_extraction_response_file",
            "fake_real_extraction_response_file",
            "fake_extraction_response_file",
            "real_extraction_response",
        }
        for source in live_config["sources"]:
            with self.subTest(source=source["name"]):
                if source["active"] is not True:
                    # Retired sources stay in the config so re-onboarding is
                    # reversible, but the live shape rules do not apply (#597).
                    continue
                if source["name"] == BOKLISTA_SOURCE_NAME:
                    self.assertEqual(source["visible_title_selector"], ".page-title")
                    self.assertEqual(source["ranking_title_selector"], ".col1 > .book-title")
                    self.assertEqual(source["ranking_author_selector"], ".col1 > .author-name")
                if source["name"] == "Nonfiction.fr Critiques":
                    self.assertEqual(source["extraction_method"], "nonfiction-fr-rss-live")
                    self.assertTrue(source["enable_live"])
                    self.assertTrue(source["seen_guids_file"])
                    self.assertGreater(int(source["max_new_items_per_run"]), 0)
                    self.assertFalse(deterministic_fields & set(source))
                    continue
                if source["name"] == PM_DEALS_SOURCE_NAME:
                    # Issue #945: no model and no fetch; the run reads the PM deals pass result.
                    self.assertEqual(source["extraction_method"], "pm-deal-email-live")
                    self.assertTrue(source["pm_collection_file"])
                    self.assertTrue(source["seen_guids_file"])
                    continue
                if source["name"] == PW_ISSUE_SOURCE_NAME:
                    # Issue #955: no model and no fetch; the run reads the PW issue pass result.
                    self.assertEqual(source["extraction_method"], "pw-issue-pdf-live")
                    self.assertTrue(source["pw_collection_file"])
                    self.assertTrue(source["seen_guids_file"])
                    continue
                self.assertEqual(source["extraction_method"], "agent-extraction-live")
                self.assertTrue(source["enable_live"])
                self.assertEqual(source["extraction_backend"], "real")
                self.assertTrue(source["enable_real_backend"])
                self.assertEqual(source["credential_env"], "AUTOSCOUT_AGENT_EXTRACTION_API_KEY")
                self.assertTrue(source["url"])
                self.assertGreater(int(source["max_model_input_chars"]), 0)
                self.assertGreater(int(source["model_max_output_tokens"]), 0)
                self.assertGreater(float(source["model_timeout_seconds"]), 0)
                self.assertTrue(source["capture_live_response_file"])
                self.assertTrue(source["capture_extraction_response_file"])
                self.assertFalse(deterministic_fields & set(source))

    def test_live_capture_retains_reference_date_for_ranking_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_collectors.capture_live_response(
                tmp_path / "config.json",
                tmp_path / "output",
                {
                    "name": "Ranking fixture",
                    "url": "https://example.test/ranking",
                    "language": "en",
                    "capture_live_response_file": "ranking-page.json",
                },
                {
                    "source_page": "https://example.test/ranking",
                    "html": "<h1>Ranking</h1>",
                    "reference_date": "2026-06-10",
                },
                "real-live",
            )
            captured = json.loads((tmp_path / "output" / "ranking-page.json").read_text(encoding="utf-8"))
            self.assertEqual(captured["reference_date"], "2026-06-10")

    def test_live_config_run_mvp_health_and_capture_replay_round_trip_offline(self):
        live_config = _load_resolved(LIVE_CONFIG)
        rehearsal_config = _load_resolved(REHEARSAL_CONFIG)
        rehearsal_sources_by_name = {source["name"]: source for source in rehearsal_config["sources"]}

        def is_bounded_ranking_source(source):
            """Whether the source is a ranking table with a declared raw-row bound.

            An earlier version of these assertions used the presence of
            ``ranking_expected_raw_row_count`` as the predicate.  That made an
            exact-count declaration load-bearing for something it does not
            mean, and a source that correctly moved to a two-sided bound
            silently lost every ranking declaration from its replay overlay.
            The bound kind is the source's choice; being a bounded ranking
            table is the property these assertions are about.
            """
            if source_collectors.contract_family(source) != "ranking-table":
                return False
            return any(bound is not None for bound in ranking_row_bounds(source))

        def is_undated_ranking_source(source):
            return (
                source.get("active") is True
                and source_collectors.contract_family(source) == "ranking-table"
                and source.get("ranking_reporting_period_undated") is True
            )

        def load_rehearsal_fixture(source, field):
            rehearsal_source = rehearsal_sources_by_name.get(source["name"])
            if rehearsal_source is None:
                raise AssertionError(f"missing rehearsal source for {source['name']!r}")
            raw_path = rehearsal_source.get(field)
            if not isinstance(raw_path, str) or not raw_path:
                raise AssertionError(f"rehearsal source {source['name']!r} must declare {field}")
            fixture_path = (REHEARSAL_CONFIG.parent / raw_path).resolve()
            if not fixture_path.is_file():
                raise AssertionError(f"missing rehearsal fixture for {source['name']!r}: {fixture_path}")
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
            if not isinstance(fixture, dict):
                raise AssertionError(f"rehearsal fixture for {source['name']!r} must be an object")
            return fixture

        self_outer = self

        class DeterministicTransport:
            name = "deterministic-test"
            mode = "real-live"

            def __init__(self):
                self.calls = []

            def extract(
                self,
                *,
                api_key,
                backend_name,
                source,
                page_data,
                max_chars,
                max_output_tokens,
                timeout_seconds,
            ):
                link = source_link(source)
                self.calls.append(
                    {
                        "api_key": api_key,
                        "backend_name": backend_name,
                        "source_name": source["name"],
                        "source_page": page_data["source_page"],
                        "max_chars": max_chars,
                        "max_output_tokens": max_output_tokens,
                        "timeout_seconds": timeout_seconds,
                    }
                )
                if source.get("contract_family") == "bookinfo-weekly-json":
                    rows = json.loads(page_data["text"])
                    fixture = load_rehearsal_fixture(source, "extraction_response_file")
                    mentions = [
                        dict(mention, source_week=rows[0]["source_week"], evidence_url=rows[0]["evidence_url"])
                        for mention in fixture["mentions"]
                    ]
                    self_outer.assertEqual(len(rows), 20)
                    return {"mentions": mentions}, {
                        "real_extraction_transport": "deterministic-test",
                        "model": "test-model",
                    }
                if source.get("contract_family") == "ipc-top40-csv":
                    rows = json.loads(page_data["text"])
                    fixture = load_rehearsal_fixture(source, "extraction_response_file")
                    mentions = [
                        dict(mention, source_week=rows[0]["source_week"], evidence_url=rows[0]["evidence_url"])
                        for mention in fixture["mentions"]
                    ]
                    self_outer.assertEqual(len(rows), 40)
                    return {"mentions": mentions}, {
                        "real_extraction_transport": "deterministic-test",
                        "model": "test-model",
                    }
                if source.get("contract_family") == "perlentaucher-daily-digest":
                    # #809: each retained mention takes the date its row carries on the re-dated pages.
                    rows = {row["isbn"]: row for row in json.loads(page_data["text"])}
                    fixture = load_rehearsal_fixture(source, "extraction_response_file")
                    mentions = [
                        dict(mention, source_week=rows[mention["isbn"]]["source_week"])
                        for mention in fixture["mentions"]
                    ]
                    self_outer.assertEqual(len(rows), 31)
                    return {"mentions": mentions}, {
                        "real_extraction_transport": "deterministic-test",
                        "model": "test-model",
                    }
                if source.get("contract_family") == "kirkus-coming-soon":
                    rows = {row["isbn"]: row for row in json.loads(page_data["text"])}
                    fixture = load_rehearsal_fixture(source, "extraction_response_file")
                    mentions = [
                        dict(
                            mention,
                            source_week=rows[mention["isbn"]]["source_week"],
                            publication_year=rows[mention["isbn"]]["publication_year"],
                        )
                        for mention in fixture["mentions"]
                    ]
                    self_outer.assertEqual(len(rows), 27)
                    return {"mentions": mentions}, {
                        "real_extraction_transport": "deterministic-test",
                        "model": "test-model",
                    }
                if source.get("contract_family") in {"nuevoslibros-frontlist", "publisher-frontlist"}:
                    fixture = load_rehearsal_fixture(source, "extraction_response_file")
                    return {"mentions": fixture["mentions"]}, {
                        "real_extraction_transport": "deterministic-test",
                        "model": "test-model",
                    }
                if source.get("contract_family") == "award-list":
                    # #578: the rows are unlinked, so the retained mentions cite the page itself.
                    fixture = load_rehearsal_fixture(source, "extraction_response_file")
                    return {"mentions": fixture["mentions"]}, {
                        "real_extraction_transport": "deterministic-test",
                        "model": "test-model",
                    }
                if is_undated_ranking_source(source):
                    raw_reference_date = page_data.get("reference_date")
                    if not isinstance(raw_reference_date, str):
                        raise AssertionError("undated ranking live page must retain its production reference_date")
                    extraction_fixture = load_rehearsal_fixture(source, "extraction_response_file")
                    raw_mentions = extraction_fixture.get("mentions")
                    if not isinstance(raw_mentions, list):
                        raise AssertionError("undated ranking extraction fixture must contain mentions")
                    mentions = []
                    for raw_mention in raw_mentions:
                        if not isinstance(raw_mention, dict):
                            raise AssertionError("undated ranking extraction mentions must be objects")
                        mention = dict(raw_mention)
                        mention["source_week"] = raw_reference_date
                        mentions.append(mention)
                    return {"mentions": mentions}, {
                        "real_extraction_transport": "deterministic-test",
                        "model": "test-model",
                    }
                if source.get("ranking_reporting_date_selector"):
                    identity = ranking_page_identity(
                        str(page_data["html"]),
                        source,
                        reference_date=date.fromisoformat(page_data["reference_date"]),
                    )
                    if identity is None:
                        raise AssertionError("separate-date ranking must expose a current page identity")
                    extraction = load_rehearsal_fixture(source, "extraction_response_file")
                    mentions = [
                        dict(mention, source_week=identity.reporting_period) for mention in extraction["mentions"]
                    ]
                    return {"mentions": mentions}, {
                        "real_extraction_transport": "deterministic-test",
                        "model": "test-model",
                    }
                if source.get("audible_explicit_chart") is True:
                    mentions = [
                        {
                            "title": _exact_test_book(source)["title"],
                            "primary_authors": [_exact_test_book(source)["author"]],
                            "source_url": source["url"],
                            "evidence_url": f"{source['url'].rstrip('/')}/pd/live-proof-{rank}/B{rank:03d}",
                            "signal_type": "audiobook ranking",
                            "signal_strength": source["expected_signal_strength"],
                            "evidence_note": f"Current rank {rank}.",
                            "current_rank": rank,
                            "candidate_category": "adult nonfiction",
                            "narrative_classification": "reported nonfiction",
                            "underlying_work_type": "book",
                            "publication_status": "published",
                            **helper_metadata(source, _exact_test_book(source)["title"], str(rank)),
                        }
                        for rank in range(1, 21)
                    ]
                    return {"mentions": mentions}, {
                        "real_extraction_transport": "deterministic-test",
                        "model": "test-model",
                    }
                if is_bounded_ranking_source(source):
                    raw_reference_date = page_data.get("reference_date")
                    if not isinstance(raw_reference_date, str):
                        raise AssertionError("ranking live page must retain its production reference_date")
                    identity = ranking_page_identity(
                        str(page_data["html"]),
                        source,
                        reference_date=date.fromisoformat(raw_reference_date),
                    )
                    if identity is None:
                        raise AssertionError("deterministic ranking page must expose a current page identity")
                    title = "Bom dia, inverno"
                    author = "Tamara Klink"
                    isbn = "9788535944679"
                    rank = "2"
                    return {
                        "mentions": [
                            {
                                "title": title,
                                "primary_authors": [author],
                                "isbn": isbn,
                                "current_rank": rank,
                                "source_week": identity.reporting_period,
                                "source_url": page_data["source_page"],
                                "evidence_url": page_data["source_page"],
                                "signal_type": "bestseller/ranking",
                                "signal_strength": source["expected_signal_strength"],
                                "evidence_note": f"Current rank {rank} for {identity.reporting_period}.",
                                "candidate_category": source.get("eligible_candidate_category", "adult nonfiction"),
                                **helper_metadata(source, title, rank),
                            }
                        ]
                    }, {
                        "real_extraction_transport": "deterministic-test",
                        "model": "test-model",
                    }
                if source.get("amazon_charts_weekly") is True:
                    host = urlsplit(source["url"]).netloc
                    mentions = [
                        {
                            "title": _exact_test_book(source)["title"],
                            "primary_authors": [_exact_test_book(source)["author"]],
                            "source_url": page_data["source_page"],
                            "evidence_url": f"https://{host}/dp/B0000000{rank:02d}",
                            "signal_type": "bestseller/ranking",
                            "signal_strength": source["expected_signal_strength"],
                            "evidence_note": f"Current rank {rank}.",
                            "current_rank": rank,
                            "candidate_category": "adult nonfiction",
                            "weeks_on_list": 1,
                            **helper_metadata(source, _exact_test_book(source)["title"], str(rank)),
                        }
                        for rank in range(1, 21)
                    ]
                    return {"mentions": mentions}, {
                        "real_extraction_transport": "deterministic-test",
                        "model": "test-model",
                    }
                if source.get("metadata_exact_book_list") is True:
                    mention = {
                        "title": _exact_test_book(source)["title"],
                        "primary_authors": [_exact_test_book(source)["author"]],
                        "source_url": source["url"],
                        "evidence_url": link,
                        "signal_type": source.get("signal_family") or source["family"],
                        "signal_strength": source.get("expected_signal_strength", "shortlist"),
                        "evidence_note": "Metadata-only offline proof.",
                        "candidate_category": source.get("eligible_candidate_category", "adult nonfiction"),
                        **helper_metadata(source, _exact_test_book(source)["title"]),
                    }
                else:
                    mention = {
                        "title": f"{source['name']} Live Proof",
                        "primary_authors": ["AutoScout Test"],
                        "source_url": source["url"] if source["family"] == "audiobook ranking" else link,
                        "evidence_url": link,
                        "signal_type": source["family"],
                        "signal_strength": "minor mention"
                        if source["family"] == "audiobook ranking"
                        else "major media list",
                        "evidence_note": "Metadata-only offline proof from implicit retailer ordering.",
                        **(
                            {}
                            if source.get("metadata_ranking_table") is True
                            else {"candidate_category": "adult nonfiction"}
                        ),
                        **(
                            {"underlying_work_type": "book", "publication_status": "published"}
                            if source["family"] == "audiobook ranking"
                            else {}
                        ),
                        **(
                            {
                                "isbn": "978-1-4028-9462-6",
                                "current_rank": 1,
                                "source_week": _circana_week(page_data),
                                "source_url": source["url"],
                                "evidence_url": source["url"],
                                "signal_type": "bestseller/ranking",
                                "evidence_note": f"Current table rank 1 for {_circana_week(page_data)}.",
                            }
                            if source.get("metadata_ranking_table") is True
                            else {}
                        ),
                        # Non-English generic sources must carry the English
                        # helper fields the contract requests (Issue #645).
                        **(
                            {
                                "english_title": f"{source['name']} Live Proof (English edition)",
                                "search_translation": f"Русский поисковый перевод: {source['name']}",
                                "author_transliteration_note": "АвтоСкаут Тест",
                            }
                            if str(source.get("language", "")).casefold() != "en"
                            else {}
                        ),
                    }
                return {"mentions": [mention]}, {
                    "real_extraction_transport": "deterministic-test",
                    "model": "test-model",
                }

        def source_link(source):
            suffix = (
                "audiobook/autoscout-live-proof/1"
                if source["family"] == "audiobook ranking"
                else "autoscout-live-proof"
            )
            return f"{str(source['url']).rstrip('/')}/{suffix}"

        def helper_metadata(source, title, rank=""):
            if is_bounded_ranking_source(source):
                return _PUBLISHNEWS_RETAINED_HELPER if title == "Bom dia, inverno" else {}
            if source.get("metadata_exact_book_list") is not True:
                return {}
            strict_chart = source.get("audible_explicit_chart") is True or source.get("amazon_charts_weekly") is True
            is_english = str(source.get("language", "")).casefold() == "en"
            if strict_chart and is_english:
                return {}
            book = _exact_test_book(source)
            return {
                "english_title": title if is_english else book["english_title"],
                "search_translation": book["search_translation"],
                "author_transliteration_note": book["author_transliteration_note"],
            }

        def fake_fetch(config_path, output_dir, source, *, reference_date=None):
            link = source_link(source)
            heading = source.get("expected_page_heading", "")
            if source.get("contract_family") == "bookinfo-weekly-json":
                from autoscout.bookinfo_ranking import capture
                from autoscout.bookinfo_ranking import page_data as bookinfo_page_data

                retained = load_rehearsal_fixture(source, "source_page_fixture_file")
                self.assertIsNotNone(reference_date)
                monday = reference_date - timedelta(days=reference_date.weekday() + 7)
                timestamp = f"{monday.isoformat()}T03:00:00.000Z"

                def fetch(url):
                    if "/filtro/" in url:
                        values = [{"data_inicio_semana": timestamp}] if f"mes={monday:%m}%2F{monday:%Y}" in url else []
                        body = json.dumps(values)
                    else:
                        body = retained["bookinfo_evidence"]["ranking"]["body"]
                    return {"body": body, "status": 200, "resolved_url": url}

                bundle = capture(source, reference_date, fetch)
                return bookinfo_page_data(bundle, source), {"live_fetch": True}
            if source.get("contract_family") == "ipc-top40-csv":
                from autoscout.ipc_top40_ranking import SOURCE_URL as ipc_top40_source_url
                from autoscout.ipc_top40_ranking import capture as ipc_top40_capture
                from autoscout.ipc_top40_ranking import page_data as ipc_top40_page_data

                retained = load_rehearsal_fixture(source, "source_page_fixture_file")["ipc_top40_evidence"]
                self.assertIsNotNone(reference_date)
                # The hosted-file URL carries the upload time; publish the retained file two days before the run.
                uploaded = datetime.combine(reference_date - timedelta(days=2), time(12), UTC)
                hosted_url = re.sub(
                    r"/[0-9]{13}/", f"/{int(uploaded.timestamp() * 1000)}/", retained["file"]["resolved_url"]
                )

                def fetch_ipc_top40(url):
                    if url == ipc_top40_source_url:
                        link = f'<a href="{retained["page"]["link_href"]}">{retained["page"]["link_label"]}</a>'
                        return {"body": link, "status": 200, "resolved_url": url}
                    return {"body": retained["file"]["body"], "status": 200, "resolved_url": hosted_url}

                bundle = ipc_top40_capture(source, reference_date, fetch_ipc_top40)
                return ipc_top40_page_data(bundle, source), {"live_fetch": True}
            if source.get("contract_family") == "perlentaucher-daily-digest":
                from autoscout.perlentaucher_digest import daily_page_url
                from autoscout.perlentaucher_digest import page_data as perlentaucher_page_data

                retained = load_rehearsal_fixture(source, "source_page_fixture_file")["perlentaucher_evidence"]
                self.assertIsNotNone(reference_date)
                # #809: the retained daily pages are dated; serve them as if their capture ran on the run date.
                shift = reference_date - date.fromisoformat(retained["reference_date"])
                bundle = json.loads(json.dumps(retained))
                bundle["reference_date"] = reference_date.isoformat()
                for observed in bundle["daily_pages"]:
                    observed["date"] = (date.fromisoformat(observed["date"]) + shift).isoformat()
                    observed["url"] = daily_page_url(observed["date"])
                bundle["index"]["daily_pages"] = [observed["date"] for observed in bundle["daily_pages"]]
                return perlentaucher_page_data(bundle, source), {"live_fetch": True}
            if source.get("contract_family") == "kirkus-coming-soon":
                from autoscout.kirkus_listing import page_data as kirkus_page_data

                bundle = load_rehearsal_fixture(source, "source_page_fixture_file")["kirkus_evidence"]
                self.assertIsNotNone(reference_date)
                # #816: serve the metadata-only pre-collection as a pass of the active run date.
                shift = reference_date - date.fromisoformat(bundle["reference_date"])
                bundle["reference_date"] = reference_date.isoformat()
                collection = bundle["collection"]
                collection["collected_date"] = (date.fromisoformat(collection["collected_date"]) + shift).isoformat()
                for row in collection["rows"]:
                    for field in ("card_publication_date", "page_fetched_date"):
                        if row[field]:
                            row[field] = (date.fromisoformat(row[field]) + shift).isoformat()
                    if row["detail"] is not None:
                        for field in ("publication_date", "review_date"):
                            row["detail"][field] = (date.fromisoformat(row["detail"][field]) + shift).isoformat()
                return kirkus_page_data(bundle, source), {"live_fetch": True}
            if source.get("contract_family") in {"nuevoslibros-frontlist", "publisher-frontlist"}:
                page_data = load_rehearsal_fixture(source, "source_page_fixture_file")
                if source.get("contract_family") == "nuevoslibros-frontlist":
                    # #993: a live NuevosLibros capture carries the active run date as its reference date.
                    # The retained pilot pages predate that field, so they are served as fetched today.
                    if reference_date is None:
                        raise AssertionError("NuevosLibros live fetch must receive the active run date")
                    page_data["reference_date"] = reference_date.isoformat()
                return page_data, {"source_url": source["url"]}
            if source.get("contract_family") == "award-list":
                # #578: an award page states a year, not a period, so the retained page is served as fetched today.
                if reference_date is None:
                    raise AssertionError("award-list live fetch must receive the active run date")
                page_data = load_rehearsal_fixture(source, "source_page_fixture_file")
                page_data["reference_date"] = reference_date.isoformat()
                return page_data, {"source_url": page_data["source_page"]}
            if is_undated_ranking_source(source):
                if reference_date is None:
                    raise AssertionError("undated ranking live fetch must receive the active run date")
                page_data = load_rehearsal_fixture(source, "source_page_fixture_file")
                page_data["reference_date"] = reference_date.isoformat()
                return page_data, {"source_url": page_data["source_page"]}
            if source.get("ranking_reporting_date_selector"):
                if reference_date is None:
                    raise AssertionError("separate-date ranking fetch must receive the active run date")
                page_data = load_rehearsal_fixture(source, "source_page_fixture_file")
                identity = parse_visible_page_identity(page_data["html"], source["ranking_reporting_date_selector"])
                if identity.failure_cause is not None or len(identity.identity_texts) != 1:
                    raise AssertionError("retained ranking fixture must have one visible date element")
                old_date_text = identity.identity_texts[0]
                if source["ranking_reporting_period_date_format"] == "iso-publication-week-wednesday":
                    # #776: use the latest published Wednesday edition, including year boundaries.
                    publication_date = reference_date - timedelta(days=(reference_date.weekday() - 2) % 7)
                    iso_year, iso_week, _ = publication_date.isocalendar()
                    period = f"Week {iso_week} - {iso_year}"
                else:
                    period = (reference_date - timedelta(days=1)).strftime(
                        source["ranking_reporting_period_date_format"]
                    )
                if page_data["html"].count(old_date_text) != 1:
                    raise AssertionError("retained date text must occur exactly once")
                old_identity = ranking_page_identity(page_data["html"], source)
                if old_identity is None or old_date_text.count(old_identity.reporting_period) != 1:
                    raise AssertionError("retained date element must contain its reporting period exactly once")
                new_date_text = old_date_text.replace(old_identity.reporting_period, period, 1)
                page_data["html"] = page_data["html"].replace(old_date_text, new_date_text, 1)
                page_data["reference_date"] = reference_date.isoformat()
                return page_data, {"source_url": page_data["source_page"]}
            # This branch synthesises a dated ranking page row by row, so it
            # needs one concrete count and not a range.  It therefore reads the
            # exact declaration on purpose, unlike the bounded-ranking checks
            # above.  A source declaring a two-sided bound is served from a
            # retained fixture by the undated branch instead.
            if (
                source_collectors.contract_family(source) == "ranking-table"
                and source.get("ranking_expected_raw_row_count") is not None
            ):
                if reference_date is None:
                    raise AssertionError("ranking live fetch must receive the active run date")
                raw_row_count = int(source["ranking_expected_raw_row_count"])
                period_end = reference_date - timedelta(days=1)
                period_start = period_end - timedelta(days=6)
                period = f"{period_start:%d/%m/%Y} a {period_end:%d/%m/%Y}"
                heading_template = str(source["expected_page_heading"])
                ranking_pattern = str(source["ranking_reporting_period_pattern"])
                dynamic_heading, replacements = re.subn(
                    ranking_pattern,
                    period,
                    heading_template,
                    count=1,
                )
                if replacements != 1:
                    raise AssertionError("ranking heading must contain one declared reporting period")

                def selector_class(field: str) -> str:
                    selector = str(source[field])
                    if not selector.startswith(".") or any(character in selector[1:] for character in " .>#"):
                        raise AssertionError(f"unsupported deterministic selector shape: {selector!r}")
                    return selector[1:]

                row_class = selector_class("ranking_row_selector")
                title_class = selector_class("ranking_title_selector")
                author_class = selector_class("ranking_author_selector")
                identifier_selector = str(source.get("ranking_identifier_selector", "")).strip()
                identifier_class = selector_class("ranking_identifier_selector") if identifier_selector else ""
                identifier_pattern = str(source.get("ranking_identifier_pattern", "")).strip()
                if not identifier_class and not identifier_pattern:
                    raise AssertionError("ranking contract must declare an identifier selector or pattern")
                rows = []
                for rank in range(1, raw_row_count + 1):
                    if rank == 1:
                        title, author, isbn = (
                            "Eu duvido você terminar este livro",
                            "Giuliana Mafra",
                            "9780306406157",
                        )
                    elif rank == 2:
                        title, author, isbn = "Bom dia, inverno", "Tamara Klink", "9788535944679"
                    elif rank == 3:
                        title, author, isbn = "A psicologia financeira", "Morgan Housel", "9786555111101"
                    elif rank == 4:
                        title, author, isbn = "Pai, me conta a sua história?", "Elma Van Vliet", "9788543107790"
                    else:
                        title = f"Deterministic adult nonfiction row {rank}"
                        author = f"Deterministic fixture author {rank}"
                        isbn = _valid_isbn13(rank)
                    identifier_markup = (
                        f'<span class="{identifier_class}" hidden>{isbn}</span>'
                        if identifier_class
                        else f"<span hidden>{isbn}</span>"
                    )
                    rows.append(
                        f'<div class="{row_class}"><div class="{title_class}">{title}</div>'
                        f'<div class="{author_class}">{author}</div>{identifier_markup}</div>'
                    )
                return {
                    "source_page": source["url"],
                    "resolved_url": source["url"],
                    "reference_date": reference_date.isoformat(),
                    "html": (
                        "<!doctype html><html><body>"
                        '<div class="pn-colunas-06x07"><div class="pn-colunas-corpo"><div class="pn-editoria">'
                        f'<div class="pn-editoria-cabeca"><div class="pn-editoria-titulo">{dynamic_heading}</div></div>'
                        "</div></div></div>" + "".join(rows) + "</body></html>"
                    ),
                }, {"source_url": source["url"]}
            ranking_reference_date = ""
            if source.get("audible_explicit_chart") is True:
                author_marker = "Von:" if str(source.get("language", "")).casefold() == "de" else "By:"
                book = _exact_test_book(source)
                markers = (
                    f"<h1>{source['expected_page_heading']}</h1>"
                    f"<p>{source['expected_range_marker']}</p>"
                    + "".join(
                        f'<li><span>{rank}</span><a href="{source["url"].rstrip("/")}/pd/live-proof-{rank}/B{rank:03d}">{book["title"]}</a><span>{author_marker} {book["author"]}</span></li>'
                        for rank in range(1, 21)
                    )
                )
            else:
                markers = (
                    "<h1>Non-Fiction</h1><div>Sort by: Bestselling</div>"
                    if source["family"] == "audiobook ranking"
                    else f"<h1>{heading}</h1>"
                    if heading
                    else ""
                )
            if source.get("amazon_charts_weekly") is True:
                host = urlsplit(source["url"]).netloc
                segment = str(source.get("chart_path_segment", "mostread/nonfiction")).strip("/")
                marker = str(source.get("author_marker", "by"))
                dated_url = f"https://{host}/charts/2026-07-26/{segment}"
                book = _exact_test_book(source)
                blocks = "".join(
                    f'<div id="rank{rank}" data-rank="{rank}">{rank}'
                    f'<a href="/dp/B0000000{rank:02d}/ref=chrt_bk_rd_nf_{rank}_ci_lp"></a>'
                    f'<div class="kc-rank-card-title">{book["title"]}</div>'
                    f'<div class="kc-rank-card-author">{marker} {book["author"]}</div></div>'
                    for rank in range(1, 21)
                )
                return {
                    "source_page": dated_url,
                    "resolved_url": dated_url,
                    "html": f"<title>{source['expected_page_heading']} | Amazon Charts</title>{blocks}",
                }, {"source_url": dated_url}
            if source.get("metadata_ranking_table") is True:
                # The source-note week is aged against the run date (Issue
                # #763), so the proof page states last week's chart and keeps
                # the reference date the live fetch would record.
                if reference_date is None:
                    raise AssertionError("ranking live fetch must receive the active run date")
                week_ended = reference_date - timedelta(days=7)
                ranking_reference_date = reference_date.isoformat()
                markers = (
                    "Hardcover Frontlist Nonfiction <table><tr><th>Rank</th><th>Last Week</th>"
                    "<th>Weeks on List</th><th>Bibliographical Information</th><th>Pub Date</th>"
                    "<th>Units</th></tr><tr><td>1</td><td>"
                    + source["name"]
                    + " Live Proof AutoScout Test 978-1-4028-9462-6</td></tr></table>"
                    f"Source: Circana BookScan for {week_ended:%b}. {week_ended.day}, {week_ended.year} "
                )
            if source.get("metadata_exact_book_list") is True:
                markers += "".join(f"<span>{marker}</span>" for marker in source.get("expected_visible_markers", []))
                book = _exact_test_book(source)
                markers += f"<span> {book['author']} </span>"
                title = book["title"]
            else:
                # A generic source can declare visible markers too: the
                # Goodreads year marker (#996).
                markers += "".join(f"<span>{marker}</span>" for marker in source.get("expected_visible_markers", []))
                title = f"{source['name']} Live Proof"
                # The extraction names this author; the page must too (#850).
                markers += "<span> AutoScout Test </span>"
            body = f'{markers}<a href="{link}">{title}</a>'
            section_heading = source.get("entry_section_heading")
            if section_heading:
                # A declared entry section bounds the list, so the proof entry
                # sits inside the one section carrying that heading.
                body = f"<section><h2>{section_heading}</h2>{body}</section>"
            page = {
                "source_page": source["url"],
                "resolved_url": source["url"],
                "html": body,
            }
            if ranking_reference_date:
                page["reference_date"] = ranking_reference_date
            return page, {"source_url": source["url"]}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            live_output = tmp_path / "live-run"
            transport = DeterministicTransport()
            original_transport = source_collectors.REAL_AGENT_EXTRACTION_TRANSPORT
            original_fetch = source_collectors.fetch_agent_source_page
            original_urlopen = source_collectors.urlopen
            original_secret = os.environ.get("AUTOSCOUT_AGENT_EXTRACTION_API_KEY")

            nonfiction_rss = (REPO_ROOT / "data/current-weekly-pilot/nonfiction-fr-rss.xml").read_text(encoding="utf-8")
            nonfiction_pages = json.loads(
                (REPO_ROOT / "data/current-weekly-pilot/nonfiction-fr-review-pages.json").read_text(encoding="utf-8")
            )["pages"]

            class HttpResponse:
                def __init__(self, url, body):
                    self.url = url
                    self.body = body.encode("utf-8")

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

                def read(self, _limit=-1):
                    return self.body

                def geturl(self):
                    return self.url

            def fake_urlopen(request, timeout):
                self.assertGreater(float(timeout), 0)
                if request.full_url == "https://www.nonfiction.fr/rss-critiques.xml":
                    return HttpResponse(request.full_url, nonfiction_rss)
                return HttpResponse(request.full_url, nonfiction_pages[request.full_url]["html"])

            temp_live = dict(live_config)
            temp_live["sources"] = [dict(source) for source in live_config["sources"]]
            for source in temp_live["sources"]:
                helper_path = source.get("helper_metadata_file")
                if isinstance(helper_path, str) and helper_path:
                    source["helper_metadata_file"] = str((LIVE_CONFIG.parent / helper_path).resolve())
            nonfiction_source = next(
                source for source in temp_live["sources"] if source["name"] == "Nonfiction.fr Critiques"
            )
            nonfiction_source["seen_guids_file"] = str(tmp_path / "nonfiction-fr-seen.json")
            # The PM deals pass result is machine-local; the run reads a recorded one dated today.
            pm_result = json.loads((REPO_ROOT / PM_DEALS_REPLAY_RESULT).read_text(encoding="utf-8"))
            pm_result["collected_at"] = f"{live_config['run_date']}T06:00:00+00:00"
            (tmp_path / "pm-deals.json").write_text(json.dumps(pm_result), encoding="utf-8")
            pm_source = next(source for source in temp_live["sources"] if source["name"] == PM_DEALS_SOURCE_NAME)
            pm_source["pm_collection_file"] = str(tmp_path / "pm-deals.json")
            pm_source["seen_guids_file"] = str(tmp_path / "pm-seen.json")
            # Likewise the PW issue pass result: its newest issue is dated the run date.
            pw_result = json.loads((REPO_ROOT / PW_ISSUE_REPLAY_RESULT).read_text(encoding="utf-8"))
            for row in pw_result["rows"]:
                row["issue_date"] = live_config["run_date"]
            pw_result["issues"] = [{"issue_date": live_config["run_date"], "issue_id": "1"}]
            (tmp_path / "pw-issue.json").write_text(json.dumps(pw_result), encoding="utf-8")
            pw_source = next(source for source in temp_live["sources"] if source["name"] == PW_ISSUE_SOURCE_NAME)
            pw_source["pw_collection_file"] = str(tmp_path / "pw-issue.json")
            pw_source["seen_guids_file"] = str(tmp_path / "pw-seen.json")
            temp_live_config = tmp_path / "live-config.json"
            temp_live_config.write_text(json.dumps(temp_live), encoding="utf-8")

            try:
                os.environ["AUTOSCOUT_AGENT_EXTRACTION_API_KEY"] = "secret-value"
                source_collectors.REAL_AGENT_EXTRACTION_TRANSPORT = transport
                source_collectors.fetch_agent_source_page = fake_fetch
                source_collectors.urlopen = fake_urlopen

                cli.run_mvp(temp_live_config, live_output, state_dir=tmp_path / "live-state")
            finally:
                source_collectors.REAL_AGENT_EXTRACTION_TRANSPORT = original_transport
                source_collectors.fetch_agent_source_page = original_fetch
                source_collectors.urlopen = original_urlopen
                if original_secret is None:
                    os.environ.pop("AUTOSCOUT_AGENT_EXTRACTION_API_KEY", None)
                else:
                    os.environ["AUTOSCOUT_AGENT_EXTRACTION_API_KEY"] = original_secret

            agent_live_sources = [
                source
                for source in live_config["sources"]
                if source.get("extraction_method") == "agent-extraction-live" and source["active"] is True
            ]
            self.assertEqual(len(transport.calls), len(agent_live_sources))
            self.assertEqual(
                [call["source_name"] for call in transport.calls],
                [source["name"] for source in agent_live_sources],
            )
            self.assertTrue(all(call["api_key"] == "secret-value" for call in transport.calls))
            undated_sources = [source for source in live_config["sources"] if is_undated_ranking_source(source)]
            for source in live_config["sources"]:
                strict_all_rank = (
                    source.get("audible_explicit_chart") is True
                    or source.get("amazon_charts_weekly") is True
                    or is_bounded_ranking_source(source)
                )
                if strict_all_rank:
                    self.assertGreaterEqual(
                        source.get("model_max_output_tokens", 0),
                        8000,
                        f"strict all-rank chart {source['name']} must carry a full-rank output budget",
                    )

            source_runs = json.loads((live_output / "source-runs.json").read_text(encoding="utf-8"))
            run_summary = json.loads((live_output / "run-summary.json").read_text(encoding="utf-8"))
            live_mentions = json.loads((live_output / "source-mentions.json").read_text(encoding="utf-8"))

            for source in undated_sources:
                source_calls = [call for call in transport.calls if call["source_name"] == source["name"]]
                self.assertEqual(len(source_calls), 1, source["name"])
                source_mentions = [mention for mention in live_mentions if mention["source_name"] == source["name"]]
                self.assertTrue(source_mentions, source["name"])
                self.assertTrue(all("category" not in mention for mention in source_mentions))

            self.assertEqual(run_summary["collection_mode"], "mixed")
            self.assertEqual(run_summary["collection_modes"], ["live", "real-live"])
            inactive_names = {s["name"] for s in live_config["sources"] if s["active"] is not True}
            for run in source_runs:
                expected = "inactive" if run["source_name"] in inactive_names else "collected"
                self.assertEqual(run["status"], expected, run["source_name"])
            for run in source_runs:
                if run["source_name"] in inactive_names:
                    continue
                if run["source_name"] == "Nonfiction.fr Critiques":
                    self.assertEqual(run["collection_mode"], "live")
                    self.assertEqual(run["debug_metadata"]["collector_kind"], "nonfiction-fr")
                elif run["source_name"] == PM_DEALS_SOURCE_NAME:
                    self.assertEqual(run["collection_mode"], "live")
                    self.assertEqual(run["debug_metadata"]["collector_kind"], "pm-deal-email")
                elif run["source_name"] == PW_ISSUE_SOURCE_NAME:
                    self.assertEqual(run["collection_mode"], "live")
                    self.assertEqual(run["debug_metadata"]["collector_kind"], "pw-issue-pdf")
                else:
                    self.assertEqual(run["collection_mode"], "real-live")
                    self.assertEqual(run["debug_metadata"]["real_extraction_transport"], "deterministic-test")

            health_output = io.StringIO()
            with contextlib.redirect_stdout(health_output):
                health_status = cli.run_health(live_output)
            self.assertEqual(health_status, 0)
            self.assertIn("collection_mode=mixed", health_output.getvalue())

            replay_sources = []
            for source in live_config["sources"]:
                if source["name"] in inactive_names:
                    # A retired source captures nothing, so it replays nothing (#597).
                    continue
                if source["name"] in {"Nonfiction.fr Critiques", PM_DEALS_SOURCE_NAME, PW_ISSUE_SOURCE_NAME}:
                    # Deterministic collectors replay their recorded mentions.
                    recorded = tmp_path / f"recorded-{len(replay_sources)}.json"
                    recorded.write_text(
                        json.dumps(
                            {
                                "mentions": [
                                    mention for mention in live_mentions if mention["source_name"] == source["name"]
                                ]
                            }
                        ),
                        encoding="utf-8",
                    )
                    replay_sources.append(
                        {
                            "name": source["name"],
                            "family": source["family"],
                            "url": source["url"],
                            "active": source["active"],
                            "access_status": source["access_status"],
                            "extraction_method": "recorded",
                            "check_frequency": source["check_frequency"],
                            "language": source["language"],
                            "recorded_mentions_file": str(recorded),
                            **{
                                key: source[key]
                                for key in (
                                    "contract_family",
                                    "pm_max_collection_age_days",
                                    "pm_category_labels",
                                    "pw_max_issue_age_days",
                                    "pw_excluded_subsections",
                                )
                                if key in source
                            },
                        }
                    )
                    continue
                page_capture = live_output / source["capture_live_response_file"]
                extraction_capture = live_output / source["capture_extraction_response_file"]
                self.assertTrue(page_capture.exists(), page_capture)
                self.assertTrue(extraction_capture.exists(), extraction_capture)
                captured_page = json.loads(page_capture.read_text(encoding="utf-8"))
                self.assertEqual(captured_page["collection_mode"], "real-live")
                if is_bounded_ranking_source(source):
                    self.assertEqual(captured_page["reference_date"], live_config["run_date"])
                captured_extraction = json.loads(extraction_capture.read_text(encoding="utf-8"))
                self.assertEqual(captured_extraction["collection_mode"], "real-live")
                if is_undated_ranking_source(source):
                    self.assertEqual(captured_page["reference_date"], live_config["run_date"])
                    if source["name"] == BOKLISTA_SOURCE_NAME:
                        self.assertNotIn("detail_pages", captured_page)
                    else:
                        detail_pages = captured_page.get("detail_pages")
                        expected_detail_pages = load_rehearsal_fixture(source, "source_page_fixture_file").get(
                            "detail_pages"
                        )
                        self.assertIsInstance(detail_pages, list)
                        self.assertIsInstance(expected_detail_pages, list)
                        self.assertEqual(len(detail_pages), len(expected_detail_pages))
                        self.assertGreater(len(detail_pages), 0)
                        captured_mentions = captured_extraction["response"]["mentions"]
                        self.assertTrue(all("category" not in mention for mention in captured_mentions))
                replay_sources.append(
                    {
                        "name": source["name"],
                        "family": source["family"],
                        "url": source["url"],
                        "active": source["active"],
                        "access_status": source["access_status"],
                        "extraction_method": "agent-extraction-replay",
                        "check_frequency": source["check_frequency"],
                        "language": source["language"],
                        "source_page_fixture_file": str(page_capture),
                        **(
                            {
                                key: source[key]
                                for key in (
                                    "contract_family",
                                    "eligible_candidate_category",
                                    "expected_signal_strength",
                                    "expected_page_heading",
                                )
                                if key in source
                            }
                            if source.get("contract_family") in {"nuevoslibros-frontlist", "publisher-frontlist"}
                            else {}
                        ),
                        "extraction_response_file": str(extraction_capture),
                        **(
                            {
                                key: source[key]
                                for key in (
                                    "contract_family",
                                    "bookinfo_period_evidence",
                                    "bookinfo_max_age_days",
                                    "adult_nonfiction_exclusions",
                                    "eligible_candidate_category",
                                    "expected_signal_strength",
                                    "expected_page_heading",
                                )
                                if key in source
                            }
                            if source.get("contract_family") == "bookinfo-weekly-json"
                            else {}
                        ),
                        **(
                            {
                                key: source[key]
                                for key in (
                                    "contract_family",
                                    "ipc_top40_period_evidence",
                                    "ipc_top40_max_age_days",
                                    "adult_nonfiction_exclusions",
                                    "eligible_candidate_category",
                                    "expected_signal_strength",
                                    "expected_page_heading",
                                )
                                if key in source
                            }
                            if source.get("contract_family") == "ipc-top40-csv"
                            else {}
                        ),
                        **(
                            {
                                key: source[key]
                                for key in (
                                    "contract_family",
                                    "metadata_ranking_table",
                                    "ranking_row_selector",
                                    "ranking_rank_source",
                                    "ranking_rank_selector",
                                    "ranking_title_selector",
                                    "ranking_author_selector",
                                    "ranking_identifier_selector",
                                    "ranking_identifier_attribute",
                                    "ranking_identifier_pattern",
                                    "ranking_category_marker",
                                    "ranking_request_method",
                                    "ranking_request_body",
                                    "ranking_request_page_field",
                                    "ranking_request_max_pages",
                                    "ranking_response_html_field",
                                    "ranking_response_last_page_field",
                                    "ranking_identity_url_template",
                                    "ranking_identity_fields",
                                    "ranking_reporting_period_pattern",
                                    "ranking_reporting_period_date_format",
                                    "ranking_reporting_period_max_age_days",
                                    "ranking_reporting_period_undated",
                                    "ranking_expected_raw_row_count",
                                    "ranking_min_raw_row_count",
                                    "ranking_max_raw_row_count",
                                    "ranking_evidence_url",
                                    "ranking_expected_path",
                                    "adult_nonfiction_exclusions",
                                    "visible_title_selector",
                                    "entry_scope_selector",
                                    "ranking_source_statement",
                                    "eligible_candidate_category",
                                    "expected_signal_strength",
                                    "max_response_bytes",
                                    "expected_page_heading",
                                    "language",
                                    "ranking_author_optional",
                                    "ranking_author_evidence_json",
                                    "ranking_reporting_date_selector",
                                    "ranking_head_404_get_fallback",
                                    "ranking_detail_link_selector",
                                    "ranking_detail_url_pattern",
                                    "ranking_detail_scope_selector",
                                    "ranking_detail_fields",
                                    "ranking_detail_identifier_selector",
                                    "ranking_detail_identifier_attribute",
                                    "ranking_detail_identifier_pattern",
                                    "ranking_detail_category_selector",
                                    "ranking_detail_category_pattern",
                                    "ranking_detail_category_allowlist",
                                    "ranking_detail_max_pages",
                                )
                                if key in source
                            }
                            if is_bounded_ranking_source(source)
                            else {}
                        ),
                        **(
                            {
                                "audible_explicit_chart": True,
                                "metadata_exact_book_list": True,
                                "eligible_candidate_category": source["eligible_candidate_category"],
                                "max_response_bytes": source["max_response_bytes"],
                                "expected_signal_strength": source["expected_signal_strength"],
                                "expected_page_heading": source["expected_page_heading"],
                                "expected_range_marker": source["expected_range_marker"],
                            }
                            if source.get("audible_explicit_chart") is True
                            else {}
                        ),
                        **(
                            {
                                "amazon_charts_weekly": True,
                                "metadata_exact_book_list": True,
                                "eligible_candidate_category": source["eligible_candidate_category"],
                                "max_response_bytes": source["max_response_bytes"],
                                "expected_signal_strength": source["expected_signal_strength"],
                                "expected_page_heading": source["expected_page_heading"],
                                "chart_path_segment": source["chart_path_segment"],
                                "author_marker": source["author_marker"],
                                "language": source["language"],
                            }
                            if source.get("amazon_charts_weekly") is True
                            else {}
                        ),
                        **(
                            {
                                "language": source["language"],
                            }
                            if source.get("audible_explicit_chart") is True
                            else {}
                        ),
                    }
                )

            replay_config = tmp_path / "replay-config.json"
            replay_config.write_text(
                json.dumps(
                    {
                        "run_date": live_config["run_date"],
                        "pilot_mode": live_config["pilot_mode"],
                        "editor_row_target": live_config["editor_row_target"],
                        "pilot_row_min": live_config["pilot_row_min"],
                        "pilot_row_max": live_config["pilot_row_max"],
                        "sources": replay_sources,
                        "russian_edition_checks": live_config["russian_edition_checks"],
                    }
                ),
                encoding="utf-8",
            )
            replay_output = tmp_path / "replay-run"
            cli.run_mvp(replay_config, replay_output, state_dir=tmp_path / "replay-state")

            replay_mentions = json.loads((replay_output / "source-mentions.json").read_text(encoding="utf-8"))
            self.assertEqual(
                sorted((m["source_name"], m["title"]) for m in live_mentions),
                sorted((m["source_name"], m["title"]) for m in replay_mentions),
            )
            replay_summary = json.loads((replay_output / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(replay_summary["collection_mode"], "replay")
            if undated_sources:
                live_freshness = read_state(tmp_path / "live-state", "ranking_freshness")
                replay_freshness = read_state(tmp_path / "replay-state", "ranking_freshness")
                for source in undated_sources:
                    live_records = [record for record in live_freshness if record["source_name"] == source["name"]]
                    replay_records = [record for record in replay_freshness if record["source_name"] == source["name"]]
                    self.assertEqual(len(live_records), 1, source["name"])
                    self.assertEqual(len(replay_records), 1, source["name"])
                    self.assertEqual(live_records[0]["first_seen"], live_config["run_date"])
                    self.assertEqual(replay_records[0]["first_seen"], live_config["run_date"])
            for source in undated_sources:
                replay_source_mentions = [
                    mention for mention in replay_mentions if mention["source_name"] == source["name"]
                ]
                self.assertTrue(replay_source_mentions, source["name"])
                self.assertTrue(all("category" not in mention for mention in replay_source_mentions))


if __name__ == "__main__":
    unittest.main()
