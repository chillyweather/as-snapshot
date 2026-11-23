"""BookInfo's approved JSON period policy and source-grounded ISBN contract."""

import copy
import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from autoscout.bookinfo_ranking import FAMILY
from autoscout.bookinfo_ranking import SOURCE_URL
from autoscout.bookinfo_ranking import BookInfoError
from autoscout.bookinfo_ranking import capture
from autoscout.bookinfo_ranking import feasibility
from autoscout.bookinfo_ranking import mention_violation
from autoscout.bookinfo_ranking import option_urls
from autoscout.bookinfo_ranking import page_data
from autoscout.bookinfo_ranking import qualifying_rows
from autoscout.bookinfo_ranking import ranking_url
from autoscout.bookinfo_ranking import validate_bundle

SOURCE = {
    "url": SOURCE_URL,
    "contract_family": FAMILY,
    "bookinfo_period_evidence": "week-options-and-request",
    "language": "pt",
    "eligible_candidate_category": "adult nonfiction",
}
ROWS = json.loads((Path(__file__).parent / "fixtures/bookinfo/nonfiction-2026-09-28.json").read_text())
REFERENCE = date(2026, 10, 7)


def captured(source=None, reference=REFERENCE):
    requests = []

    def fetch(url):
        requests.append(url)
        if "/filtro/" in url:
            values = [] if "10%2F2026" in url else [{"data_inicio_semana": "2026-09-28T03:00:00.000Z"}]
        else:
            values = ROWS
        return {"body": json.dumps(values), "status": 200, "resolved_url": url}

    return capture(source or SOURCE, reference, fetch), requests


def rewrite(observation, values):
    observation["body"] = json.dumps(values)
    observation["sha256"] = hashlib.sha256(observation["body"].encode()).hexdigest()


def test_complete_capture_and_shared_feasibility():
    bundle, requests = captured()
    assert requests == [*option_urls(REFERENCE), ranking_url(date(2026, 9, 28))]
    rows = validate_bundle(bundle, SOURCE)
    assert len(rows) == 20
    assert rows[0]["isbn"] == "9786555447538"
    assert rows[2]["current_rank"] == 3
    page = page_data(bundle, SOURCE)
    assert page["html"] == ""
    verdict = feasibility(page, SOURCE)
    assert verdict.accepted and verdict.raw_row_count == 20
    from autoscout.source_feasibility import evaluate_source_feasibility

    assert evaluate_source_feasibility(SOURCE, page) == verdict


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("sku", "9786555447539", "bookinfo_isbn"),
        ("sku", "7898563143097", "bookinfo_isbn"),
        ("titulo", "", "bookinfo_row_shape"),
        ("autor", None, "bookinfo_row_shape"),
        ("editora", "", "bookinfo_row_shape"),
        ("volume", "03", "bookinfo_volume"),
        ("volume", "-1", "bookinfo_volume"),
        ("categoria", "Ficção", "bookinfo_category"),
        ("id_categoria", 3, "bookinfo_category"),
    ],
)
def test_bad_row_fails(field, value, reason):
    bundle, _ = captured()
    rows = copy.deepcopy(ROWS)
    rows[0][field] = value
    rewrite(bundle["ranking"], rows)
    with pytest.raises(BookInfoError) as exc:
        validate_bundle(bundle, SOURCE)
    assert exc.value.reason_code == reason


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("duplicate", "bookinfo_isbn"),
        ("increasing", "bookinfo_volume"),
        ("missing", "bookinfo_row_count"),
        ("empty", "bookinfo_row_count"),
        ("object", "bookinfo_row_count"),
    ],
)
def test_bad_list_fails(mutation, reason):
    bundle, _ = captured()
    rows = copy.deepcopy(ROWS)
    if mutation == "duplicate":
        rows[1]["sku"] = rows[0]["sku"]
    if mutation == "increasing":
        rows[1]["volume"] = "999999"
    if mutation == "missing":
        rows.pop()
    if mutation == "empty":
        rows = []
    if mutation == "object":
        rows = {"rows": rows}
    rewrite(bundle["ranking"], rows)
    with pytest.raises(BookInfoError) as exc:
        validate_bundle(bundle, SOURCE)
    assert exc.value.reason_code == reason


@pytest.mark.parametrize("mutation", ["digest", "query", "period", "reference", "options", "redirect"])
def test_replay_identity_fails(mutation):
    bundle, _ = captured()
    if mutation == "digest":
        bundle["ranking"]["body"] += " "
    if mutation == "query":
        bundle["ranking"]["url"] += "&other=1"
    if mutation == "redirect":
        bundle["ranking"]["resolved_url"] += "/"
    if mutation == "period":
        bundle["period_start"] = "2026-09-21"
    if mutation == "reference":
        bundle.pop("reference_date")
    if mutation == "options":
        bundle["options"] = []
    with pytest.raises(BookInfoError):
        validate_bundle(bundle, SOURCE)


@pytest.mark.parametrize(
    "values",
    [
        [{"data_inicio_semana": "2026-09-29T03:00:00Z"}],
        [{"data_inicio_semana": "2026-09-28"}],
        [{"data_inicio_semana": "bad"}],
        [{"data_inicio_semana": "2026-09-28T03:00:00Z"}] * 2,
        [{"data_inicio_semana": "2026-10-12T03:00:00Z"}],
    ],
)
def test_invalid_options(values):
    bundle, _ = captured()
    rewrite(bundle["options"][1], values)
    with pytest.raises(BookInfoError):
        validate_bundle(bundle, SOURCE)


def test_no_retry_on_http_failure():
    requests = []

    def fetch(url):
        requests.append(url)
        return {"status": 403, "body": "denied", "resolved_url": url}

    with pytest.raises(BookInfoError):
        capture(SOURCE, REFERENCE, fetch)
    assert len(requests) == 1


def test_stale_fails_before_ranking_request():
    requests = []

    def fetch(url):
        requests.append(url)
        return {"status": 200, "body": "[]", "resolved_url": url}

    with pytest.raises(BookInfoError) as exc:
        capture(SOURCE, REFERENCE, fetch)
    assert exc.value.reason_code == "bookinfo_stale"
    assert len(requests) == 2


def test_exclusion_preserves_rank_and_grounding():
    source = {**SOURCE, "adult_nonfiction_exclusions": ["Coragem de transformar"]}
    bundle, _ = captured(source)
    rows = qualifying_rows(validate_bundle(bundle, source), source)
    assert rows[0]["current_rank"] == 2
    row = rows[0]
    entry = {
        **row,
        "primary_authors": [row["author"]],
        "source_url": SOURCE_URL,
        "signal_type": "bestseller/ranking",
        "candidate_category": "adult nonfiction",
    }
    page = page_data(bundle, source)
    assert not mention_violation(entry, page, source)
    entry["current_rank"] = 1
    assert mention_violation(entry, page, source)


def test_model_input_contains_validated_rows_only():
    from autoscout.source_collectors.agent_model_input import model_bounded_page_data

    bundle, _ = captured()
    model, _ = model_bounded_page_data(page_data(bundle, SOURCE), 20000, source=SOURCE)
    assert len(json.loads(model["text"])) == 20
    assert "bookinfo_evidence" not in model
    assert model["html"] == ""


def test_probe_retains_replayable_bundle(tmp_path, monkeypatch):
    import autoscout.source_probe as probe

    class Clock(date):
        @classmethod
        def today(cls):
            return REFERENCE

    class Response:
        status = 200

        def __init__(self, url, body):
            self.headers = {"Content-Type": "application/json; charset=utf-8"}
            self.url = url
            self.body = body.encode()

        def read(self, count):
            return self.body[:count]

        def geturl(self):
            return self.url

        def close(self):
            pass

    bundle, requests = captured()
    bodies = {x["url"]: x["body"] for x in [*bundle["options"], bundle["ranking"]]}
    observed = []

    def opener(request, **kwargs):
        observed.append((request.get_method(), request.full_url))
        return Response(request.full_url, bodies[request.full_url])

    monkeypatch.setattr(probe, "date", Clock)
    report, code = probe.run_source_candidate_probe(
        [SOURCE_URL],
        ["BookInfo"],
        ["bestseller/ranking"],
        contract_families=[FAMILY],
        contract_metadata=[SOURCE],
        opener=opener,
        keep_captures=tmp_path / "captures",
        ledger_path=tmp_path / "ledger.json",
        feasibility_dir=tmp_path / "drafts",
    )
    assert code == 0
    assert observed == [("GET", x) for x in requests]
    assert report["candidates"][0]["resolved_url"] == bundle["ranking"]["url"]
    assert report["candidates"][0]["http_evidence"]["get"]["body_bytes"] == len(bundle["ranking"]["body"].encode())
    retained = json.loads(next((tmp_path / "captures").glob("*.json")).read_text())
    assert retained["bookinfo_evidence"] == bundle
    assert feasibility(retained, SOURCE).accepted

    from autoscout.replay_fixtures import build_replay_fixture_pair

    samples = feasibility(retained, SOURCE).samples
    helpers = {
        "version": 1,
        "source_url": SOURCE_URL,
        "language": "pt",
        "entries": [
            {
                "title": x.title,
                "author": x.author,
                "evidence_url": x.url,
                "english_title": f"English helper {i}",
                "search_translation": f"Поиск книги {i}",
                "author_transliteration_note": f"Автор книги {i}",
            }
            for i, x in enumerate(samples, 1)
        ],
    }
    pair = build_replay_fixture_pair(
        SOURCE_URL,
        "bookinfo",
        retained,
        signal_strength="major signal",
        candidate_category="adult nonfiction",
        language="pt",
        helper_metadata=helpers,
    )
    assert pair.entry_count == 3
    assert pair.page["bookinfo_evidence"] == bundle
    assert pair.page["html"] == ""
    assert pair.extraction["mentions"][2]["current_rank"] == 3
    from autoscout.source_collectors.agent_mentions import agent_extraction_mentions_from_response

    mentions, dropped = agent_extraction_mentions_from_response(pair.page, pair.extraction, SOURCE, "2026-10-07")
    assert len(mentions) == 3 and not dropped


def test_live_capture_uses_same_adapter(tmp_path, monkeypatch):
    import autoscout.source_collectors.agent_fetch as agent

    bundle, _ = captured()
    bodies = {x["url"]: x["body"] for x in [*bundle["options"], bundle["ranking"]]}

    def fetch(source, url, **kwargs):
        assert kwargs["unwrap_response"] is False
        assert kwargs["strict_status"] is True
        return bodies[url], True, url

    monkeypatch.setattr(agent, "_fetch_bounded_page", fetch)
    page, _ = agent.fetch_agent_source_page(tmp_path / "config.json", tmp_path, SOURCE, reference_date=REFERENCE)
    assert page["bookinfo_evidence"] == bundle


def test_json_capture_reports_content_bytes():
    from autoscout.source_collectors.base import capture_content_evidence

    bundle, _ = captured()
    sizes = capture_content_evidence(page_data(bundle, SOURCE))
    assert sizes["capture_html_bytes"] == 0
    assert sizes["capture_content_bytes"] == len(bundle["ranking"]["body"].encode())


def test_supplier_display_whitespace_is_normalized_without_changing_raw_evidence():
    bundle, _ = captured()
    rows = validate_bundle(bundle, SOURCE)
    assert ROWS[10]["autor"].endswith(" ")
    assert rows[10]["author"] == "Carol S. Dweck"
    assert json.loads(bundle["ranking"]["body"])[10]["autor"] == ROWS[10]["autor"]


def test_unknown_supplier_body_field_is_refused():
    bundle, _ = captured()
    rows = copy.deepcopy(ROWS)
    rows[0]["sinopse"] = "Publisher prose must not enter a metadata replay fixture"
    rewrite(bundle["ranking"], rows)
    with pytest.raises(BookInfoError) as exc:
        validate_bundle(bundle, SOURCE)
    assert exc.value.reason_code == "bookinfo_row_shape"


def test_year_crossover_and_completed_week_selection():
    reference = date(2027, 1, 5)
    observed = []

    def fetch(url):
        observed.append(url)
        if "/filtro/" in url and "01%2F2027" in url:
            values = [{"data_inicio_semana": "2027-01-04T03:00:00Z"}]
        elif "/filtro/" in url and "12%2F2026" in url:
            values = [{"data_inicio_semana": "2026-12-28T03:00:00Z"}]
        else:
            values = ROWS
        return {"body": json.dumps(values), "status": 200, "resolved_url": url}

    bundle = capture(SOURCE, reference, fetch)
    assert bundle["period_start"] == "2026-12-28"
    assert bundle["period_end"] == "2027-01-03"
    assert observed[-1] == ranking_url(date(2026, 12, 28))


def test_wrong_month_stops_before_ranking():
    observed = []

    def fetch(url):
        observed.append(url)
        return {"body": '[{"data_inicio_semana":"2026-09-28T03:00:00Z"}]', "status": 200, "resolved_url": url}

    with pytest.raises(BookInfoError) as exc:
        capture(SOURCE, REFERENCE, fetch)
    assert exc.value.reason_code == "bookinfo_week_options"
    assert all("/filtro/" in url for url in observed)


@pytest.mark.parametrize("status,truncated,resolved", [(429, False, True), (200, True, True), (200, False, False)])
def test_transport_refusal_stops_requests(status, truncated, resolved):
    observed = []

    def fetch(url):
        observed.append(url)
        return {"body": "[]", "status": status, "truncated": truncated, "resolved_url": url if resolved else SOURCE_URL}

    with pytest.raises(BookInfoError):
        capture(SOURCE, REFERENCE, fetch)
    assert len(observed) == 1


def test_source_health_accepts_json_identity_and_rejects_tampering():
    from autoscout.source_collection import _validate_page_shape

    source = {**SOURCE, "expected_page_heading": "BookInfo Não Ficção"}
    bundle, _ = captured()
    page = page_data(bundle, source)
    assert _validate_page_shape(source, page, "2026-06-10", freshness_reference="page_reference_date") == ""
    page["source_page"] = "https://example.test/wrong"
    assert _validate_page_shape(source, page, "2026-10-07")


def test_live_capture_preserves_json_bundle(tmp_path):
    from autoscout.source_collectors.base import capture_live_response

    source = {**SOURCE, "capture_live_response_file": "bookinfo.json"}
    bundle, _ = captured()
    capture_live_response(tmp_path / "config.json", tmp_path, source, page_data(bundle, source))
    retained = json.loads((tmp_path / "bookinfo.json").read_text())
    assert retained["bookinfo_evidence"] == bundle
    assert retained["contract_family"] == FAMILY
    assert validate_bundle(retained["bookinfo_evidence"], source)


# Issue #782: the hooks below were fixed for the Top 40 CSV family in #781 and are held to the same rule here.


def _entry(row, **extra):
    return {
        **{k: v for k, v in row.items() if k not in ("publisher", "author", "volume")},
        "primary_authors": [row["author"]],
        "source_url": SOURCE_URL,
        "signal_type": "bestseller/ranking",
        "candidate_category": "adult nonfiction",
        **extra,
    }


@pytest.mark.parametrize(
    "extra,accepted",
    [
        ({}, True),
        ({"publisher": "Editora Gente"}, True),
        ({"publisher": ""}, True),
        ({"publisher": "Editora Sextante"}, False),
        ({"publication_year": "2026"}, False),
        ({"publication_year": 2026}, False),
        ({"publication_year": ""}, True),
    ],
)
def test_supplied_bibliographic_fields_must_come_from_the_json(extra, accepted):
    bundle, _ = captured()
    row = validate_bundle(bundle, SOURCE)[0]
    assert row["publisher"] == "Editora Gente"
    assert bool(mention_violation(_entry(row, **extra), page_data(bundle, SOURCE), SOURCE)) is not accepted


def test_inline_prompt_carries_the_validated_rows():
    from autoscout.source_collectors.contract import agent_extraction_prompt

    bundle, _ = captured()
    prompt = agent_extraction_prompt(SOURCE, page_data(bundle, SOURCE), 120000)
    assert ranking_url(date(2026, 9, 28)) in prompt
    assert "9786555447538" in prompt and "Coragem de transformar" in prompt
    assert "list-page HTML" not in prompt


def test_capture_bound_rules_point_at_the_rows_not_html():
    from autoscout.source_collectors.contract import extraction_contract

    contract = extraction_contract(SOURCE, source_page=SOURCE_URL, capture_file="capture.json")
    rules = " ".join(contract["rules"])
    assert "its text field holds the validated JSON rows" in rules
    assert "html field is the authoritative" not in rules
    assert "occurs as a row link in the html field" not in rules
    assert (
        "publication_year"
        not in extraction_contract(SOURCE, source_page=SOURCE_URL)["required_output_shape"]["mentions"][0]
    )


def test_retained_live_capture_carries_the_validated_rows(tmp_path):
    from autoscout.source_collectors.base import capture_live_response

    source = {**SOURCE, "capture_live_response_file": "bookinfo.json"}
    bundle, _ = captured()
    capture_live_response(tmp_path / "config.json", tmp_path, source, page_data(bundle, source))
    retained = json.loads((tmp_path / "bookinfo.json").read_text())
    assert json.loads(retained["text"]) == validate_bundle(bundle, source)


def test_artifact_validators_verify_the_retained_request_url():
    from autoscout.source_collectors import evidence_url_verified

    bundle, _ = captured()
    page = page_data(bundle, SOURCE)
    observed = ranking_url(date(2026, 9, 28))
    assert evidence_url_verified(page, observed) == (True, "")
    assert evidence_url_verified(page, SOURCE_URL) == (True, "")
    assert not evidence_url_verified(page, ranking_url(date(2026, 9, 21)))[0]
    assert not evidence_url_verified(page, option_urls(REFERENCE)[0])[0]
    assert not evidence_url_verified({**page, "contract_family": "generic"}, observed)[0]
    tampered = copy.deepcopy(page)
    tampered["bookinfo_evidence"]["ranking"]["url"] = ranking_url(date(2026, 9, 21))
    assert not evidence_url_verified(tampered, observed)[0]


def test_empty_extraction_from_a_full_ranking_is_refused(tmp_path):
    from autoscout.extraction_completeness import capture_listing_row_count
    from autoscout.observed_extraction import validate_extraction_artifact
    from autoscout.source_collectors.contract import extraction_contract

    excluding = {**SOURCE, "name": "BookInfo", "adult_nonfiction_exclusions": ["Coragem de transformar"]}
    bundle, _ = captured(excluding)
    page = page_data(bundle, excluding)
    assert capture_listing_row_count(excluding, page) == 19
    # A work item carries the declaration as source_metadata and the page as source_url.
    contract = extraction_contract(excluding, source_page=SOURCE_URL, capture_file="capture.json")
    assert capture_listing_row_count(contract, page) == 19
    assert capture_listing_row_count(contract, {**page, "bookinfo_evidence": {}}) is None

    capture_path = tmp_path / "capture.json"
    capture_path.write_text(json.dumps(page))
    item = {
        **contract,
        "operation_id": "operation-1",
        "capture_sha256": hashlib.sha256(capture_path.read_bytes()).hexdigest(),
    }
    artifact = tmp_path / "extraction.json"
    artifact.write_text(
        json.dumps(
            {k: item[k] for k in ("source_name", "source_url", "operation_id", "capture_sha256")} | {"entries": []}
        )
    )
    observation = validate_extraction_artifact(artifact, item, capture_path)
    assert not observation.valid and observation.diagnostic_class == "unexpected_empty_extraction"


def test_family_declaration_is_normalized_for_page_validation():
    from autoscout.source_collection import _validate_page_shape

    source = {**SOURCE, "contract_family": " BOOKINFO-WEEKLY-JSON ", "expected_page_heading": "BookInfo Não Ficção"}
    bundle, _ = captured(source)
    assert _validate_page_shape(source, page_data(bundle, source), "2026-10-07") == ""


@pytest.mark.parametrize("with_text", [True, False])
def test_worklist_capture_rebuilds_rows_from_the_evidence(tmp_path, with_text):
    from autoscout.source_collection import prepare_codex_extraction_worklist

    bundle, _ = captured()
    page = page_data(bundle, SOURCE)
    if not with_text:
        del page["text"]  # A capture written before #782 carries the evidence but no rows.
    (tmp_path / "page.json").write_text(json.dumps(page))
    source = {
        **SOURCE,
        "name": "BookInfo",
        "family": "bestseller/ranking",
        "source_page_fixture_file": "page.json",
        "active": True,
        "access_status": "open",
    }
    (tmp_path / "config.json").write_text(json.dumps({"sources": [source]}))
    worklist = prepare_codex_extraction_worklist(tmp_path / "config.json", tmp_path / "out", "2026-10-07")
    assert len(worklist.work_items) == 1
    retained = json.loads((tmp_path / "out" / worklist.work_items[0]["capture_file"]).read_text())
    assert json.loads(retained["text"]) == validate_bundle(bundle, SOURCE)
    assert retained["bookinfo_evidence"] == bundle


def test_source_is_retired_in_every_weekly_config():
    # Retired at the editor's request on 2026-11-12 (docs/bookinfo-retirement-2026-11-12.md);
    # the settings stay so the source can be restored.
    from autoscout.config import load_validated_config

    for filename in (
        "current-weekly-portfolio.json",
        "current-weekly-pilot.json",
        "current-weekly-pilot-live.json",
        "current-weekly-live.json",
    ):
        source = next(
            item
            for item in load_validated_config(Path(__file__).resolve().parents[1] / "configs" / filename)["sources"]
            if item["name"] == "BookInfo Weekly Nonfiction"
        )
        assert source["active"] is False
        assert source["lifecycle_status"] == "retired"
        assert source["retired_at"] == "2026-11-12"
        assert source["retired_cards_hidden"] is True
        assert "#777" in source["lifecycle_note"]
