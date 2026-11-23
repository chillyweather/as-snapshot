"""The Independent Press Top 40 CSV contract: one named file, dated by its hosting URL."""

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from autoscout.ipc_top40_ranking import EVIDENCE_FIELD
from autoscout.ipc_top40_ranking import FAMILY
from autoscout.ipc_top40_ranking import ORIGIN
from autoscout.ipc_top40_ranking import SOURCE_URL
from autoscout.ipc_top40_ranking import IpcTop40Error
from autoscout.ipc_top40_ranking import capture
from autoscout.ipc_top40_ranking import feasibility
from autoscout.ipc_top40_ranking import file_link
from autoscout.ipc_top40_ranking import mention_violation
from autoscout.ipc_top40_ranking import page_data
from autoscout.ipc_top40_ranking import qualifying_rows
from autoscout.ipc_top40_ranking import validate_bundle
from autoscout.ipc_top40_ranking import validate_declaration

FIXTURES = Path(__file__).parent / "fixtures/ipc_top40"
PAGE = (FIXTURES / "top40-page.html").read_text()
CSV = (FIXTURES / "nonfiction-2026-10-07.csv").read_text()
SOURCE = {
    "url": SOURCE_URL,
    "contract_family": FAMILY,
    "ipc_top40_period_evidence": "hosted-file-upload-date",
    "language": "en",
    "eligible_candidate_category": "adult nonfiction",
}
REFERENCE = date(2026, 10, 9)
FILE_URL = ORIGIN + "/s/independentpresstop40nonfiction-fhn8.csv"
# Upload time 2026-10-07T03:00:14Z, as observed on the publisher's hosted file.
HOSTED_URL = (
    "https://static1.squarespace.com/static/61d85d41a24345648f560d14/t/"
    "6ac5b5be5b585529941dfff7/1791342014236/independentpresstop40nonfiction.csv"
)


def captured(source=None, reference=REFERENCE, page=PAGE, body=CSV, hosted=HOSTED_URL):
    requests = []

    def fetch(url):
        requests.append(url)
        if url == SOURCE_URL:
            return {"body": page, "status": 200, "resolved_url": url}
        return {"body": body, "status": 200, "resolved_url": hosted}

    return capture(source or SOURCE, reference, fetch), requests


def rewrite(bundle, body):
    bundle["file"]["body"] = body
    bundle["file"]["sha256"] = hashlib.sha256(body.encode()).hexdigest()


def replace_row(index, **fields):
    lines = CSV.splitlines(keepends=True)
    import csv
    import io

    row = dict(zip(["Ranking", "Title", "Publisher", "ISBN", "Author"], next(csv.reader([lines[index]])), strict=True))
    row.update(fields)
    out = io.StringIO()
    csv.writer(out, lineterminator="\r\n").writerow(row.values())
    lines[index] = out.getvalue()
    return "".join(lines)


def test_complete_capture_and_shared_feasibility():
    bundle, requests = captured()
    assert requests == [SOURCE_URL, FILE_URL]
    assert bundle["published_date"] == "2026-10-07"
    rows = validate_bundle(bundle, SOURCE)
    assert len(rows) == 40
    assert rows[0]["isbn"] == "9781324099406"
    assert rows[2]["current_rank"] == 3
    assert rows[10]["author"] == "Robert Macfarlane, Jackie Morris"
    assert rows[10]["primary_authors"] == ["Robert Macfarlane", "Jackie Morris"]
    assert rows[22]["primary_authors"] == ["Frédéric Gros"]
    assert {r["evidence_url"] for r in rows} == {FILE_URL}
    page = page_data(bundle, SOURCE)
    assert page["html"] == ""
    verdict = feasibility(page, SOURCE)
    assert verdict.accepted and verdict.raw_row_count == 40 and verdict.reporting_period == "2026-10-07"
    from autoscout.source_feasibility import evaluate_source_feasibility

    assert evaluate_source_feasibility(SOURCE, page) == verdict


def test_retained_page_observation_holds_no_page_body():
    bundle, _ = captured()
    assert set(bundle["page"]) == {"url", "resolved_url", "status", "sha256", "link_label", "link_href"}
    assert bundle["page"]["sha256"] == hashlib.sha256(PAGE.encode()).hexdigest()
    assert bundle["page"]["link_href"] == "/s/independentpresstop40nonfiction-fhn8.csv"


def test_stale_unlabelled_links_are_ignored():
    assert file_link(PAGE) == "/s/independentpresstop40nonfiction-fhn8.csv"
    assert file_link(PAGE.replace('href="/s/independentpresstop40nonfiction-fhn8.csv"', f'href="{FILE_URL}"')) == (
        "/s/independentpresstop40nonfiction-fhn8.csv"
    )


@pytest.mark.parametrize(
    "page",
    [
        PAGE.replace("NONFICTION EXCEL", "NONFICTION SHEET"),
        PAGE.replace("FICTION pdf", "NONFICTION EXCEL"),
        PAGE.replace("nonfiction-fhn8.csv", "nonfiction-fhn8.pdf"),
        PAGE.replace("/s/independentpresstop40nonfiction-fhn8.csv", "https://example.test/top40.csv"),
        PAGE.replace("nonfiction-fhn8.csv", "fiction-fhn8.csv"),
    ],
)
def test_missing_ambiguous_or_foreign_file_link_stops_before_file_request(page):
    requests = []

    def fetch(url):
        requests.append(url)
        return {"body": page, "status": 200, "resolved_url": url}

    with pytest.raises(IpcTop40Error) as exc:
        capture(SOURCE, REFERENCE, fetch)
    assert exc.value.reason_code == "ipc_top40_file_link"
    assert requests == [SOURCE_URL]


@pytest.mark.parametrize(
    "fields,reason",
    [
        ({"ISBN": "9781324099407"}, "ipc_top40_isbn"),
        ({"ISBN": "1324099402"}, "ipc_top40_isbn"),
        ({"Title": ""}, "ipc_top40_row_shape"),
        ({"Author": " "}, "ipc_top40_row_shape"),
        ({"Publisher": ""}, "ipc_top40_row_shape"),
        ({"Ranking": "01"}, "ipc_top40_rank"),
        ({"Ranking": "2"}, "ipc_top40_rank"),
    ],
)
def test_bad_row_fails(fields, reason):
    bundle, _ = captured()
    rewrite(bundle, replace_row(1, **fields))
    with pytest.raises(IpcTop40Error) as exc:
        validate_bundle(bundle, SOURCE)
    assert exc.value.reason_code == reason


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("duplicate", "ipc_top40_isbn"),
        ("missing", "ipc_top40_row_count"),
        ("extra", "ipc_top40_row_count"),
        ("empty", "ipc_top40_csv"),
        ("header", "ipc_top40_csv"),
        ("width", "ipc_top40_row_shape"),
        ("html", "ipc_top40_csv"),
    ],
)
def test_bad_file_fails(mutation, reason):
    bundle, _ = captured()
    lines = CSV.splitlines(keepends=True)
    body = CSV
    if mutation == "duplicate":
        body = replace_row(2, ISBN="9781324099406")
    if mutation == "missing":
        body = "".join(lines[:-1])
    if mutation == "extra":
        body = CSV + "\r\n41,Extra Title,Extra Press,9781324098423,Extra Author"
    if mutation == "empty":
        body = ""
    if mutation == "header":
        body = CSV.replace("Ranking,Title,Publisher,ISBN,Author", "Rank,Title,Publisher,ISBN,Author")
    if mutation == "width":
        body = "".join([*lines[:5], "5,Only Title\r\n", *lines[6:]])
    if mutation == "html":
        body = "<html><body>Sign in</body></html>"
    rewrite(bundle, body)
    with pytest.raises(IpcTop40Error) as exc:
        validate_bundle(bundle, SOURCE)
    assert exc.value.reason_code == reason


@pytest.mark.parametrize("mutation", ["digest", "file_url", "href", "label", "published", "reference", "page", "host"])
def test_replay_identity_fails(mutation):
    bundle, _ = captured()
    if mutation == "digest":
        bundle["file"]["body"] += " "
    if mutation == "file_url":
        bundle["file"]["url"] = ORIGIN + "/s/independentpresstop40nonfiction-wtnn.csv"
    if mutation == "href":
        bundle["page"]["link_href"] = "/s/independentpresstop40nonfiction-wtnn.csv"
    if mutation == "label":
        bundle["page"]["link_label"] = "NONFICTION PDF"
    if mutation == "published":
        bundle["published_date"] = "2026-10-08"
    if mutation == "reference":
        bundle.pop("reference_date")
    if mutation == "page":
        bundle["page"] = []
    if mutation == "host":
        bundle["file"]["resolved_url"] = bundle["file"]["resolved_url"].replace("static1.", "static2.")
    with pytest.raises(IpcTop40Error):
        validate_bundle(bundle, SOURCE)


@pytest.mark.parametrize(
    "hosted",
    [
        FILE_URL,
        HOSTED_URL.replace("61d85d41a24345648f560d14", "00005d41a24345648f560d14"),
        HOSTED_URL.replace("nonfiction.csv", "fiction.csv"),
        HOSTED_URL.replace("/1791342014236/", "/"),
        HOSTED_URL + "?x=1",
    ],
)
def test_file_must_resolve_to_the_publishers_hosted_file(hosted):
    with pytest.raises(IpcTop40Error) as exc:
        captured(hosted=hosted)
    assert exc.value.reason_code == "ipc_top40_wrong_target"


@pytest.mark.parametrize(
    "reference,accepted",
    [
        (date(2026, 10, 7), True),
        (date(2026, 10, 6), True),
        (date(2026, 10, 5), False),
        (date(2026, 10, 21), True),
        (date(2026, 10, 22), False),
    ],
)
def test_file_age_limit(reference, accepted):
    if accepted:
        bundle, _ = captured(reference=reference)
        assert bundle["published_date"] == "2026-10-07"
        return
    with pytest.raises(IpcTop40Error) as exc:
        captured(reference=reference)
    assert exc.value.reason_code == "ipc_top40_stale"


@pytest.mark.parametrize(
    "change",
    [
        {"url": "https://lithub.com/tag/independent-press-top-40/"},
        {"ipc_top40_period_evidence": "week-ending"},
        {"ipc_top40_max_age_days": 30},
        {"ipc_top40_max_age_days": True},
        {"adult_nonfiction_exclusions": [""]},
    ],
)
def test_declaration_is_explicit(change):
    with pytest.raises(IpcTop40Error) as exc:
        validate_declaration({**SOURCE, **change})
    assert exc.value.reason_code == "ipc_top40_declaration"
    from autoscout.config import _validate_source

    assert _validate_source({"name": "Top 40", "family": "bestseller/ranking", **SOURCE, **change}, 0, "source[0]")


@pytest.mark.parametrize("status,truncated,resolved", [(429, False, True), (200, True, True), (200, False, False)])
def test_transport_refusal_stops_requests(status, truncated, resolved):
    observed = []

    def fetch(url):
        observed.append(url)
        return {"body": PAGE, "status": status, "truncated": truncated, "resolved_url": url if resolved else ORIGIN}

    with pytest.raises(IpcTop40Error):
        capture(SOURCE, REFERENCE, fetch)
    assert observed == [SOURCE_URL]


def test_no_retry_on_file_failure():
    observed = []

    def fetch(url):
        observed.append(url)
        if url == SOURCE_URL:
            return {"body": PAGE, "status": 200, "resolved_url": url}
        return {"body": "denied", "status": 403, "resolved_url": url}

    with pytest.raises(IpcTop40Error) as exc:
        capture(SOURCE, REFERENCE, fetch)
    assert exc.value.reason_code == "ipc_top40_http"
    assert observed == [SOURCE_URL, FILE_URL]


def test_exclusion_preserves_rank_and_grounding():
    source = {**SOURCE, "adult_nonfiction_exclusions": ["Crossing the Wine-Dark Sea"]}
    bundle, _ = captured(source)
    rows = qualifying_rows(validate_bundle(bundle, source), source)
    assert len(rows) == 39 and rows[0]["current_rank"] == 2
    row = rows[0]
    entry = {
        **row,
        "primary_authors": row["primary_authors"],
        "source_url": SOURCE_URL,
        "signal_type": "bestseller/ranking",
        "candidate_category": "adult nonfiction",
    }
    page = page_data(bundle, source)
    assert not mention_violation(entry, page, source)
    entry["current_rank"] = 1
    assert mention_violation(entry, page, source)


@pytest.mark.parametrize(
    "extra,accepted",
    [
        ({}, True),
        ({"publisher": "Liveright"}, True),
        ({"publisher": ""}, True),
        ({"publisher": "W. W. Norton & Company"}, False),
        ({"publication_year": "2026"}, False),
        ({"publication_year": 2026}, False),
        ({"publication_year": ""}, True),
    ],
)
def test_supplied_bibliographic_fields_must_come_from_the_csv(extra, accepted):
    bundle, _ = captured()
    row = validate_bundle(bundle, SOURCE)[0]
    entry = {
        **{k: v for k, v in row.items() if k != "publisher"},
        "primary_authors": row["primary_authors"],
        "source_url": SOURCE_URL,
        "signal_type": "bestseller/ranking",
        "candidate_category": "adult nonfiction",
        **extra,
    }
    assert bool(mention_violation(entry, page_data(bundle, SOURCE), SOURCE)) is not accepted


@pytest.mark.parametrize(
    "display,expected",
    [
        ("Emily Wilson", ["Emily Wilson"]),
        ("Robert Macfarlane, Jackie Morris", ["Robert Macfarlane", "Jackie Morris"]),
        ("Frédéric Gros, John Howe (Transl.)", ["Frédéric Gros"]),
        ("Thich Nhat Hanh, Jason DeAntonis (Illus.)", ["Thich Nhat Hanh"]),
        ("Rebecca Knight (Ed.)", ["Rebecca Knight"]),
        ("Ezekiel J.Emanuel MD,", ["Ezekiel J.Emanuel MD"]),
        ("Ezekiel J. Emanuel, MD", ["Ezekiel J. Emanuel"]),
        ("Alex Mayyasi, Hosts of NPR's Planet Money", ["Alex Mayyasi", "Hosts of NPR's Planet Money"]),
        ("John Howe (Transl.)", ["John Howe (Transl.)"]),
    ],
)
def test_author_display_splits_into_individual_primary_authors(display, expected):
    from autoscout.ipc_top40_ranking import primary_authors

    assert primary_authors(display) == expected


def test_joined_author_display_is_refused_and_split_authors_match_other_sources():
    from autoscout.identity import mentions_are_identity_match

    bundle, _ = captured()
    row = validate_bundle(bundle, SOURCE)[10]
    entry = {
        **{k: v for k, v in row.items() if k != "publisher"},
        "source_url": SOURCE_URL,
        "signal_type": "bestseller/ranking",
        "candidate_category": "adult nonfiction",
    }
    page = page_data(bundle, SOURCE)
    assert not mention_violation(entry, page, SOURCE)
    assert mention_violation({**entry, "primary_authors": [row["author"]]}, page, SOURCE)
    other = {"title": row["title"], "primary_authors": ["Robert Macfarlane", "Jackie Morris"]}
    assert mentions_are_identity_match({**entry, "isbn": ""}, other)
    assert not mentions_are_identity_match({**entry, "isbn": "", "primary_authors": [row["author"]]}, other)


def test_inline_prompt_carries_the_validated_rows():
    from autoscout.source_collectors.contract import agent_extraction_prompt

    bundle, _ = captured()
    prompt = agent_extraction_prompt(SOURCE, page_data(bundle, SOURCE), 120000)
    assert FILE_URL in prompt
    assert "9781324099406" in prompt and "Trash!: A Garbageman's Story" in prompt
    assert '"primary_authors": ["Robert Macfarlane", "Jackie Morris"]' in prompt
    assert "list-page HTML" not in prompt


def test_capture_bound_rules_point_at_the_rows_not_html():
    from autoscout.source_collectors.contract import extraction_contract

    rules = " ".join(extraction_contract(SOURCE, source_page=SOURCE_URL, capture_file="capture.json")["rules"])
    assert "its text field holds the validated CSV rows" in rules
    assert "html field is the authoritative" not in rules
    assert "occurs as a row link in the html field" not in rules
    assert (
        "publication_year"
        not in extraction_contract(SOURCE, source_page=SOURCE_URL)["required_output_shape"]["mentions"][0]
    )


def test_retained_live_capture_carries_the_validated_rows(tmp_path):
    from autoscout.source_collectors.base import capture_live_response

    source = {**SOURCE, "capture_live_response_file": "ipc-top40.json"}
    bundle, _ = captured()
    page = page_data(bundle, source)
    capture_live_response(tmp_path / "config.json", tmp_path, source, page)
    retained = json.loads((tmp_path / "ipc-top40.json").read_text())
    assert json.loads(retained["text"]) == validate_bundle(bundle, source)


def test_artifact_validators_verify_the_retained_file_url():
    from autoscout.source_collectors import evidence_url_verified

    bundle, _ = captured()
    page = page_data(bundle, SOURCE)
    assert evidence_url_verified(page, FILE_URL) == (True, "")
    assert evidence_url_verified(page, SOURCE_URL) == (True, "")
    assert not evidence_url_verified(page, ORIGIN + "/s/independentpresstop40nonfiction-wtnn.csv")[0]
    assert not evidence_url_verified(page, HOSTED_URL)[0]
    assert not evidence_url_verified({**page, "contract_family": "generic"}, FILE_URL)[0]
    tampered = json.loads(json.dumps(page))
    tampered[EVIDENCE_FIELD]["file"]["url"] = ORIGIN + "/s/independentpresstop40nonfiction-wtnn.csv"
    assert not evidence_url_verified(tampered, FILE_URL)[0]


def test_model_input_contains_validated_rows_only():
    from autoscout.source_collectors.agent_model_input import model_bounded_page_data

    bundle, _ = captured()
    model, _ = model_bounded_page_data(page_data(bundle, SOURCE), 20000, source=SOURCE)
    assert len(json.loads(model["text"])) == 40
    assert EVIDENCE_FIELD not in model
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
            self.headers = {"Content-Type": "text/csv; charset=utf-8"}
            self.url = url
            self.body = body.encode()

        def read(self, count):
            return self.body[:count]

        def geturl(self):
            return self.url

        def close(self):
            pass

    bundle, requests = captured()
    observed = []

    def opener(request, **kwargs):
        observed.append((request.get_method(), request.full_url))
        if request.full_url == SOURCE_URL:
            return Response(SOURCE_URL, PAGE)
        return Response(HOSTED_URL, CSV)

    monkeypatch.setattr(probe, "date", Clock)
    report, code = probe.run_source_candidate_probe(
        [SOURCE_URL],
        ["Independent Press Top 40 Nonfiction"],
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
    assert report["candidates"][0]["resolved_url"] == FILE_URL
    assert report["candidates"][0]["http_evidence"]["get"]["body_bytes"] == len(CSV.encode())
    retained = json.loads(next((tmp_path / "captures").glob("*.json")).read_text())
    assert retained[EVIDENCE_FIELD] == bundle
    assert feasibility(retained, SOURCE).accepted

    from autoscout.replay_fixtures import build_replay_fixture_pair

    samples = feasibility(retained, SOURCE).samples
    helpers = {
        "version": 1,
        "source_url": SOURCE_URL,
        "language": "en",
        "entries": [
            {
                "title": x.title,
                "author": x.author,
                "evidence_url": x.url,
                "english_title": x.title,
                "search_translation": f"Поиск книги {i}",
                "author_transliteration_note": f"Автор книги {i}",
            }
            for i, x in enumerate(samples, 1)
        ],
    }
    pair = build_replay_fixture_pair(
        SOURCE_URL,
        "independent-press-top-40",
        retained,
        signal_strength="top ranking",
        candidate_category="adult nonfiction",
        language="en",
        helper_metadata=helpers,
    )
    assert pair.entry_count == 3
    assert pair.page[EVIDENCE_FIELD] == bundle
    assert pair.page["html"] == ""
    assert pair.extraction["mentions"][2]["current_rank"] == 3
    from autoscout.source_collectors.agent_mentions import agent_extraction_mentions_from_response

    mentions, dropped = agent_extraction_mentions_from_response(pair.page, pair.extraction, SOURCE, "2026-10-09")
    assert len(mentions) == 3 and not dropped

    # The staged-artifact validators read the capture from disk and have no source declaration.
    from autoscout.observed_extraction import validate_extraction_artifact

    (tmp_path / "capture.json").write_bytes(pair.page_bytes)
    (tmp_path / "extraction.json").write_bytes(pair.extraction_bytes)
    item = {
        **SOURCE,
        "source_name": pair.extraction["source_name"],
        "source_url": SOURCE_URL,
        "operation_id": pair.operation_id,
        "capture_sha256": pair.capture_sha256,
    }
    observation = validate_extraction_artifact(tmp_path / "extraction.json", item, tmp_path / "capture.json")
    assert observation.valid, observation.diagnostic_class


def test_probe_reports_a_named_refusal(tmp_path, monkeypatch):
    import autoscout.source_probe as probe

    class Clock(date):
        @classmethod
        def today(cls):
            return REFERENCE

    class Response:
        status = 200

        def __init__(self):
            self.headers = {"Content-Type": "text/html; charset=utf-8"}

        def read(self, count):
            return PAGE.replace("NONFICTION EXCEL", "").encode()[:count]

        def geturl(self):
            return SOURCE_URL

        def close(self):
            pass

    monkeypatch.setattr(probe, "date", Clock)
    report, code = probe.run_source_candidate_probe(
        [SOURCE_URL],
        ["Independent Press Top 40 Nonfiction"],
        ["bestseller/ranking"],
        contract_families=[FAMILY],
        contract_metadata=[SOURCE],
        opener=lambda request, **kwargs: Response(),
        ledger_path=tmp_path / "ledger.json",
        feasibility_dir=tmp_path / "drafts",
    )
    assert code == 1
    assert report["candidates"][0]["outcome"] == "reject"
    assert report["candidates"][0]["reason_code"] == "ipc_top40_file_link"


def test_live_capture_uses_same_adapter(tmp_path, monkeypatch):
    import autoscout.source_collectors.agent_fetch as agent

    bundle, _ = captured()

    def fetch(source, url, **kwargs):
        assert kwargs["unwrap_response"] is False
        assert kwargs["strict_status"] is True
        if url == SOURCE_URL:
            # The page is streamed into the scan and never returned whole.
            kwargs["consume"](PAGE)
            return ("", False, url)
        assert "consume" not in kwargs
        return (CSV, True, HOSTED_URL)

    monkeypatch.setattr(agent, "_fetch_bounded_page", fetch)
    page, debug = agent.fetch_agent_source_page(tmp_path / "config.json", tmp_path, SOURCE, reference_date=REFERENCE)
    assert page[EVIDENCE_FIELD] == bundle
    assert debug["ipc_top40_published_date"] == "2026-10-07"


def test_csv_capture_reports_content_bytes():
    from autoscout.source_collectors.base import capture_content_evidence

    bundle, _ = captured()
    sizes = capture_content_evidence(page_data(bundle, SOURCE))
    assert sizes["capture_html_bytes"] == 0
    assert sizes["capture_content_bytes"] == len(CSV.encode())


def test_empty_extraction_from_a_full_file_is_refused(tmp_path):
    from autoscout.extraction_completeness import capture_listing_row_count
    from autoscout.observed_extraction import validate_extraction_artifact
    from autoscout.source_collectors.contract import extraction_contract

    excluding = {**SOURCE, "name": "Top 40", "adult_nonfiction_exclusions": ["Crossing the Wine-Dark Sea"]}
    bundle, _ = captured(excluding)
    page = page_data(bundle, excluding)
    assert capture_listing_row_count(excluding, page) == 39
    # A work item carries the declaration as source_metadata and the page as source_url.
    contract = extraction_contract(excluding, source_page=SOURCE_URL, capture_file="capture.json")
    assert "url" not in contract["source_metadata"]
    assert capture_listing_row_count(contract, page) == 39
    assert capture_listing_row_count(contract, {**page, EVIDENCE_FIELD: {}}) is None

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

    source = {**SOURCE, "contract_family": " IPC-TOP40-CSV ", "expected_page_heading": "Independent Press Top 40"}
    bundle, _ = captured(source)
    assert _validate_page_shape(source, page_data(bundle, source), "2026-10-09") == ""


def test_source_health_accepts_csv_identity_and_rejects_tampering():
    from autoscout.source_collection import _validate_page_shape

    source = {**SOURCE, "expected_page_heading": "Independent Press Top 40 Nonfiction"}
    bundle, _ = captured()
    page = page_data(bundle, source)
    assert _validate_page_shape(source, page, "2026-06-10", freshness_reference="page_reference_date") == ""
    assert _validate_page_shape(source, page, "2026-10-10")
    page["source_page"] = "https://example.test/wrong"
    assert _validate_page_shape(source, page, "2026-10-09")


def test_live_capture_preserves_csv_bundle(tmp_path):
    from autoscout.source_collectors.base import capture_live_response

    source = {**SOURCE, "capture_live_response_file": "ipc-top40.json"}
    bundle, _ = captured()
    capture_live_response(tmp_path / "config.json", tmp_path, source, page_data(bundle, source))
    retained = json.loads((tmp_path / "ipc-top40.json").read_text())
    assert retained[EVIDENCE_FIELD] == bundle
    assert retained["contract_family"] == FAMILY
    assert validate_bundle(retained[EVIDENCE_FIELD], source)


def test_model_output_budget_covers_forty_rows():
    from autoscout.source_scaffold import _live_model_output_tokens

    assert _live_model_output_tokens({"contract_family": FAMILY}) == 8000


# The publisher page outgrew the global 1,000,000-byte bound (#994) and then the
# 1,500,000-byte bound declared for it (2.6 MB in November 2026, nearly all site-builder
# markup).  The rule changed on Dima's decision of 2026-11-16: the page is read in slices
# into the link parser and the digest, none of it is kept, and no source declares a bound
# for it; only PAGE_CEILING_BYTES stops a response that never ends.
OVERSIZED_PAGE = PAGE.replace("</body>", "<!--" + "x" * 3_000_000 + "--></body>")


class _UrlopenResponse:
    """A urlopen response that is read in slices through read1, as http.client's is."""

    status = 200

    def __init__(self, url, body, slice_bytes=None):
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self._url = url
        self._body = body.encode()
        self._offset = 0
        self._slice_bytes = slice_bytes
        self.largest_read = 0

    def read1(self, count=-1):
        if self._slice_bytes is not None:
            count = min(count, self._slice_bytes)
        piece = self._body[self._offset : self._offset + count]
        self._offset += len(piece)
        self.largest_read = max(self.largest_read, len(piece))
        return piece

    def read(self, count=-1):
        return self.read1(len(self._body) if count is None or count < 0 else count)

    def geturl(self):
        return self._url

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def _live_fetch(monkeypatch, page, slice_bytes=None):
    import autoscout.source_collectors as collectors

    responses = []

    def opener(request, timeout=None):
        url = request.full_url
        response = _UrlopenResponse(url, page, slice_bytes) if url == SOURCE_URL else _UrlopenResponse(HOSTED_URL, CSV)
        responses.append(response)
        return response

    monkeypatch.setattr(collectors, "urlopen", opener)
    return responses


def test_oversized_page_is_read_in_slices_with_no_declared_bound(tmp_path, monkeypatch):
    from autoscout.http_decoding import READ_SLICE_BYTES
    from autoscout.source_collectors.agent_fetch import fetch_agent_source_page

    assert len(OVERSIZED_PAGE.encode()) > 2_000_000
    responses = _live_fetch(monkeypatch, OVERSIZED_PAGE)
    page, debug = fetch_agent_source_page(tmp_path / "config.json", tmp_path, SOURCE, reference_date=REFERENCE)
    bundle = page[EVIDENCE_FIELD]
    assert bundle["page"]["sha256"] == hashlib.sha256(OVERSIZED_PAGE.encode()).hexdigest()
    assert bundle["page"]["link_href"] == "/s/independentpresstop40nonfiction-fhn8.csv"
    assert len(validate_bundle(bundle, SOURCE)) == 40
    assert debug["ipc_top40_published_date"] == "2026-10-07"
    assert responses[0].largest_read <= READ_SLICE_BYTES


def test_scan_memory_follows_the_largest_token_not_the_page():
    import tracemalloc

    from autoscout.http_decoding import READ_SLICE_BYTES
    from autoscout.ipc_top40_ranking import PageScan

    # Like the live page: megabytes of site-builder markup in many tags of a few kilobytes.
    block = '<div class="sqs-block" data-block-json="' + "&quot;x&quot;," * 400 + '"></div>'
    page = PAGE.replace("</body>", block * 1_000 + "</body>")
    assert len(page.encode()) > 5_000_000
    data = page.encode()
    tracemalloc.start()
    try:
        scan = PageScan()
        for start in range(0, len(data), READ_SLICE_BYTES):
            scan.feed(data[start : start + READ_SLICE_BYTES].decode())
        link = scan.file_link()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert link == "/s/independentpresstop40nonfiction-fhn8.csv"
    assert scan.sha256() == hashlib.sha256(data).hexdigest()
    assert peak < 1_000_000


def test_link_split_across_slices_is_found(tmp_path, monkeypatch):
    from autoscout.source_collectors.agent_fetch import fetch_agent_source_page

    _live_fetch(monkeypatch, PAGE, slice_bytes=7)
    page, _debug = fetch_agent_source_page(tmp_path / "config.json", tmp_path, SOURCE, reference_date=REFERENCE)
    assert page[EVIDENCE_FIELD]["page"]["sha256"] == hashlib.sha256(PAGE.encode()).hexdigest()
    assert page[EVIDENCE_FIELD]["page"]["link_href"] == "/s/independentpresstop40nonfiction-fhn8.csv"


def test_streamed_page_with_two_file_links_still_stops(tmp_path, monkeypatch):
    from autoscout.source_collectors.agent_fetch import fetch_agent_source_page
    from autoscout.source_collectors.base import CollectorRunError

    # The second labelled link comes after the bulk, so it is only seen if the whole page is read.
    second = OVERSIZED_PAGE.replace(
        "</body>", '<a href="/s/independentpresstop40nonfiction-zz99.csv">NONFICTION EXCEL</a></body>'
    )
    responses = _live_fetch(monkeypatch, second)
    with pytest.raises(CollectorRunError, match="exactly one NONFICTION EXCEL link"):
        fetch_agent_source_page(tmp_path / "config.json", tmp_path, SOURCE, reference_date=REFERENCE)
    assert [r.geturl() for r in responses] == [SOURCE_URL]


def test_page_past_the_ceiling_fails(tmp_path, monkeypatch):
    import autoscout.ipc_top40_ranking as ipc
    from autoscout.source_collectors.agent_fetch import fetch_agent_source_page
    from autoscout.source_collectors.base import CollectorRunError

    monkeypatch.setattr(ipc, "PAGE_CEILING_BYTES", 2_000_000)
    _live_fetch(monkeypatch, OVERSIZED_PAGE)
    with pytest.raises(CollectorRunError, match="exceeds the response-size limit"):
        fetch_agent_source_page(tmp_path / "config.json", tmp_path, SOURCE, reference_date=REFERENCE)


def test_whole_page_fetch_bound_is_the_ceiling_and_csv_bound_is_unchanged(monkeypatch):
    import autoscout.ipc_top40_ranking as ipc
    from autoscout.ipc_top40_ranking import MAX_RESPONSE_BYTES

    def fetch(url):
        if url == SOURCE_URL:
            return {"body": OVERSIZED_PAGE, "status": 200, "resolved_url": url}
        return {"body": CSV, "status": 200, "resolved_url": HOSTED_URL}

    assert capture(SOURCE, REFERENCE, fetch)["page"]["sha256"] == hashlib.sha256(OVERSIZED_PAGE.encode()).hexdigest()
    monkeypatch.setattr(ipc, "PAGE_CEILING_BYTES", 2_000_000)
    with pytest.raises(IpcTop40Error) as exc:
        capture(SOURCE, REFERENCE, fetch)
    assert exc.value.reason_code == "ipc_top40_response_bounds"
    monkeypatch.undo()

    big_csv = CSV + "#" + "x" * MAX_RESPONSE_BYTES

    def fetch_big_csv(url):
        if url == SOURCE_URL:
            return {"body": PAGE, "status": 200, "resolved_url": url}
        return {"body": big_csv, "status": 200, "resolved_url": HOSTED_URL}

    with pytest.raises(IpcTop40Error) as exc:
        capture(SOURCE, REFERENCE, fetch_big_csv)
    assert exc.value.reason_code == "ipc_top40_response_bounds"


def test_probe_reads_an_oversized_page_in_slices(tmp_path, monkeypatch):
    import autoscout.source_probe as probe

    class Clock(date):
        @classmethod
        def today(cls):
            return REFERENCE

    monkeypatch.setattr(probe, "date", Clock)

    def opener(request, **kwargs):
        if request.full_url == SOURCE_URL:
            return _UrlopenResponse(SOURCE_URL, OVERSIZED_PAGE)
        return _UrlopenResponse(HOSTED_URL, CSV)

    report, code = probe.run_source_candidate_probe(
        [SOURCE_URL],
        ["Independent Press Top 40 Nonfiction"],
        ["bestseller/ranking"],
        contract_families=[FAMILY],
        contract_metadata=[SOURCE],
        opener=opener,
        ledger_path=tmp_path / "ledger.json",
        feasibility_dir=tmp_path / "drafts",
    )
    assert code == 0, report["candidates"][0].get("reason_code")


def test_portfolio_entry_declares_no_page_bound_and_validates():
    from autoscout.config import _validate_source

    portfolio = json.loads((Path(__file__).parents[1] / "configs/current-weekly-portfolio.json").read_text())
    entry = next(s for s in portfolio["sources"] if s["name"] == "Independent Press Top 40 Nonfiction")
    assert "max_response_bytes" not in entry
    assert _validate_source(entry, 0, "source[0]") == []


def test_probe_page_read_is_held_to_its_allowance(tmp_path, monkeypatch):
    import autoscout.source_probe as probe

    class Clock(date):
        @classmethod
        def today(cls):
            return REFERENCE

    monkeypatch.setattr(probe, "date", Clock)
    monkeypatch.setattr(probe, "IPC_TOP40_PAGE_SECONDS", 0.0)

    def opener(request, **kwargs):
        if request.full_url == SOURCE_URL:
            return _UrlopenResponse(SOURCE_URL, PAGE)
        return _UrlopenResponse(HOSTED_URL, CSV)

    report, code = probe.run_source_candidate_probe(
        [SOURCE_URL],
        ["Independent Press Top 40 Nonfiction"],
        ["bestseller/ranking"],
        contract_families=[FAMILY],
        contract_metadata=[SOURCE],
        opener=opener,
        ledger_path=tmp_path / "ledger.json",
        feasibility_dir=tmp_path / "drafts",
    )
    assert code == 1
    assert report["candidates"][0]["reason_code"] == "ipc_top40_response_bounds"
