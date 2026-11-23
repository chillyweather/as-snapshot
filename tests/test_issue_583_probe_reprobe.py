"""Offline acceptance tests for Issue #583 probe re-probing and diagnostics."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from autoscout.__main__ import main
from autoscout.source_probe import SourceCandidateLedgerError
from autoscout.source_probe import probe_source_candidates
from autoscout.source_probe import run_source_candidate_probe

ROOT = Path(__file__).resolve().parents[1]
PROBE_URL = "https://example.test/list"

SOURCE_URL = PROBE_URL


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"", url: str = SOURCE_URL) -> None:
        self.status = status
        self.body = body
        self.url = url

    def geturl(self) -> str:
        return self.url

    def read(self, _size: int) -> bytes:
        return self.body

    def close(self) -> None:
        return None


class FakeTransport:
    def __init__(self, plans: dict[str, list[FakeResponse]]) -> None:
        self.plans = {url: list(responses) for url, responses in plans.items()}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: Any, timeout: float) -> FakeResponse:
        method = request.get_method()
        url = request.full_url
        self.calls.append((method, url))
        return self.plans[url].pop(0)


def _rejected_page() -> bytes:
    return b"<html><body><h1>Current list</h1><ul><li><a href='/book'>Book</a></li></ul></body></html>"


def _accepted_page() -> bytes:
    return b"<html><body><h1>Current list</h1><ul><li><a href='/book'>Book</a><span>By: Jane Author</span></li></ul></body></html>"


def test_default_duplicate_is_a_no_network_no_json_rewrite_latch(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    first_transport = FakeTransport({SOURCE_URL: [FakeResponse(200), FakeResponse(200, _accepted_page())]})
    run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=first_transport)
    original = ledger.read_bytes()

    class NoNetwork:
        def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("default duplicate must not issue a request")

    report, code = run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=NoNetwork())

    assert code == 0
    assert report["candidates"][0]["reason_code"] == "already_evaluated"
    assert ledger.read_bytes() == original


@pytest.mark.parametrize(
    ("requested_contract", "requested_metadata", "mismatch_text"),
    [
        ("exact-book-list", {"probe_revision": "old"}, "contract family"),
        ("generic", {"probe_revision": "new"}, "contract metadata"),
    ],
)
def test_default_compatibility_mismatch_guides_to_reprobe_without_network(
    tmp_path: Path,
    capsys: Any,
    requested_contract: str,
    requested_metadata: dict[str, str],
    mismatch_text: str,
) -> None:
    ledger = tmp_path / "ledger.json"
    run_source_candidate_probe(
        [SOURCE_URL],
        contract_families=["generic"],
        contract_metadata=[{"probe_revision": "old"}],
        ledger_path=ledger,
        feasibility_dir=tmp_path / "drafts",
        opener=FakeTransport({SOURCE_URL: [FakeResponse(200), FakeResponse(200, _accepted_page())]}),
    )
    original = ledger.read_bytes()

    class NoNetwork:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
            self.calls += 1
            raise AssertionError("default compatibility mismatch must not issue a request")

    transport = NoNetwork()
    code = probe_source_candidates(
        [SOURCE_URL],
        contract_families=[requested_contract],
        contract_metadata=[requested_metadata],
        ledger_path=ledger,
        opener=transport,
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert transport.calls == 0
    assert mismatch_text in payload["error"]
    assert "preferred fresh evaluation" in payload["error"]
    assert "--re-probe" in payload["error"]
    assert "new ledger path" in payload["error"]
    assert "separate history" in payload["error"]
    assert ledger.read_bytes() == original


def test_reprobe_applies_once_to_a_durable_duplicate_in_one_batch(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    run_source_candidate_probe(
        [SOURCE_URL],
        ledger_path=ledger,
        opener=FakeTransport({SOURCE_URL: [FakeResponse(200), FakeResponse(200, _rejected_page())]}),
    )
    transport = FakeTransport({SOURCE_URL: [FakeResponse(200), FakeResponse(200, _accepted_page())]})

    report, code = run_source_candidate_probe(
        [SOURCE_URL, SOURCE_URL], ledger_path=ledger, re_probe=True, opener=transport
    )

    assert code == 0
    assert transport.calls == [("HEAD", SOURCE_URL), ("GET", SOURCE_URL)]
    assert report["candidates"][0]["outcome"] == "accept"
    assert report["candidates"][1]["reason_code"] == "already_evaluated"
    assert len(json.loads(ledger.read_text(encoding="utf-8"))["entries"][0]["prior_verdicts"]) == 1


def test_explicit_reprobe_preserves_the_previous_verdict_in_version_3_history(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    transport = FakeTransport(
        {
            SOURCE_URL: [
                FakeResponse(200),
                FakeResponse(200, _rejected_page()),
                FakeResponse(200),
                FakeResponse(200, _accepted_page()),
            ]
        }
    )

    first_report, first_code = run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=transport)
    first_current = json.loads(ledger.read_text(encoding="utf-8"))["entries"][0]
    second_report, second_code = run_source_candidate_probe(
        [SOURCE_URL],
        ledger_path=ledger,
        feasibility_dir=tmp_path / "drafts",
        re_probe=True,
        opener=transport,
    )

    assert first_code == 1
    assert first_report["candidates"][0]["outcome"] == "reject"
    assert second_code == 0
    fresh = second_report["candidates"][0]
    assert fresh["outcome"] == "accept"
    assert fresh["draft_status"] == "created"
    assert fresh["prior_outcome"] == "reject"
    assert fresh["prior_reason_code"] == first_report["candidates"][0]["reason_code"]
    assert fresh["prior_probe_date"] == first_report["candidates"][0]["probe_date"]
    assert transport.calls == [("HEAD", SOURCE_URL), ("GET", SOURCE_URL), ("HEAD", SOURCE_URL), ("GET", SOURCE_URL)]

    data = json.loads(ledger.read_text(encoding="utf-8"))
    current = data["entries"][0]
    assert data["version"] == 3
    assert data["accept_count"] == 1
    assert data["reject_count"] == 0
    assert current["outcome"] == "accept"
    assert len(current["prior_verdicts"]) == 1
    assert current["prior_verdicts"][0] == first_current
    assert current["prior_verdicts"][0]["outcome"] == "reject"
    assert current["prior_verdicts"][0]["structural_diagnostics"] == {"repeated_siblings": []}
    assert "prior_verdicts" not in current["prior_verdicts"][0]
    markdown = ledger.with_suffix(".md").read_text(encoding="utf-8")
    assert "Prior verdicts" in markdown
    assert "reject/missing_authors" in markdown


def test_reprobe_allows_generic_to_declared_contract_change_and_preserves_both(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    transport = FakeTransport(
        {
            SOURCE_URL: [
                FakeResponse(200),
                FakeResponse(200, _rejected_page()),
                FakeResponse(200),
                FakeResponse(200, _accepted_page()),
            ]
        }
    )
    run_source_candidate_probe(
        [SOURCE_URL],
        contract_families=["generic"],
        contract_metadata=[{"probe_revision": "old"}],
        ledger_path=ledger,
        opener=transport,
    )

    report, code = run_source_candidate_probe(
        [SOURCE_URL],
        contract_families=["exact-book-list"],
        contract_metadata=[{"probe_revision": "new"}],
        ledger_path=ledger,
        re_probe=True,
        opener=transport,
    )

    assert code == 0
    assert transport.calls == [
        ("HEAD", SOURCE_URL),
        ("GET", SOURCE_URL),
        ("HEAD", SOURCE_URL),
        ("GET", SOURCE_URL),
    ]
    assert report["candidates"][0]["outcome"] == "accept"
    current = json.loads(ledger.read_text(encoding="utf-8"))["entries"][0]
    assert current["contract_family"] == "exact-book-list"
    assert current["contract_metadata"] == {"probe_revision": "new"}
    assert current["prior_verdicts"][0]["contract_family"] == "generic"
    assert current["prior_verdicts"][0]["contract_metadata"] == {"probe_revision": "old"}


def test_reprobe_appends_history_without_changing_current_counts(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    transport = FakeTransport(
        {
            SOURCE_URL: [
                FakeResponse(200),
                FakeResponse(200, _rejected_page()),
                FakeResponse(200),
                FakeResponse(200, _accepted_page()),
                FakeResponse(200),
                FakeResponse(200, _rejected_page()),
            ]
        }
    )
    run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=transport)
    run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, re_probe=True, opener=transport)
    run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, re_probe=True, opener=transport)

    current = json.loads(ledger.read_text(encoding="utf-8"))["entries"][0]
    assert current["outcome"] == "reject"
    assert [(item["outcome"], item["reason_code"]) for item in current["prior_verdicts"]] == [
        ("reject", "missing_authors"),
        ("accept", "ok"),
    ]
    assert all("prior_verdicts" not in item for item in current["prior_verdicts"])
    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert data["accept_count"] == 0
    assert data["reject_count"] == 1


def test_malformed_reprobe_history_is_rejected_before_network(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    run_source_candidate_probe(
        [SOURCE_URL],
        ledger_path=ledger,
        opener=FakeTransport({SOURCE_URL: [FakeResponse(200), FakeResponse(200, _accepted_page())]}),
    )
    data = json.loads(ledger.read_text(encoding="utf-8"))
    data["entries"][0]["prior_verdicts"] = [{**data["entries"][0], "normalized_url": "https://other.test/list"}]
    ledger.write_text(json.dumps(data), encoding="utf-8")
    original = ledger.read_bytes()

    class NoNetwork:
        def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("malformed history must fail before network")

    try:
        run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, re_probe=True, opener=NoNetwork())
    except ValueError as exc:
        assert "normalized_url" in str(exc)
    else:
        raise AssertionError("malformed history was accepted")
    assert ledger.read_bytes() == original


def test_reprobe_requires_a_real_bool_before_ledger_repair_or_network(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    run_source_candidate_probe(
        [SOURCE_URL],
        ledger_path=ledger,
        opener=FakeTransport({SOURCE_URL: [FakeResponse(200), FakeResponse(200, _accepted_page())]}),
    )
    original = ledger.read_bytes()
    markdown = ledger.with_suffix(".md").read_bytes()

    class NoNetwork:
        def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("invalid re_probe value reached network")

    with pytest.raises(ValueError, match="re_probe must be a boolean"):
        run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, re_probe="yes", opener=NoNetwork())  # type: ignore[arg-type]
    assert ledger.read_bytes() == original
    assert ledger.with_suffix(".md").read_bytes() == markdown


def test_cli_reprobe_flag_reaches_the_public_probe_seam(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    ledger = tmp_path / "ledger.json"
    monkeypatch.setattr(
        "autoscout.source_probe.urlopen",
        FakeTransport({SOURCE_URL: [FakeResponse(200), FakeResponse(200, _rejected_page())]}),
    )
    assert main(["probe-source-candidate", "--url", SOURCE_URL, "--ledger", str(ledger)]) == 1
    capsys.readouterr()

    monkeypatch.setattr(
        "autoscout.source_probe.urlopen",
        FakeTransport({SOURCE_URL: [FakeResponse(200), FakeResponse(200, _accepted_page())]}),
    )
    code = main(["probe-source-candidate", "--url", SOURCE_URL, "--re-probe", "--ledger", str(ledger)])
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["candidates"][0]["prior_reason_code"] == "missing_authors"


def test_generic_rejection_exposes_only_bounded_repeated_visible_structure(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    html = b"""
    <html><body><h1>Aladin category</h1>
      <div class="breadcrumbs"><ul><li><a href="/">HOME</a></li></ul></div>
      <div class="book-list">
        <div class="book-row"><a class="book-title" href="/book-1">Sensitive Book One</a><span class="book-author">Author One</span><span class="book-identifier price">9780306406157 $20</span></div>
        <div class="book-row"><a class="book-title" href="/book-2">Sensitive Book Two</a><span class="book-author">Author Two</span><span class="book-identifier price">9780140328721 $21</span></div>
      </div>
      <div class="hidden-list" hidden><div class="hidden-row">Hidden text</div><div class="hidden-row">Hidden text</div></div>
    </body></html>
    """
    monkeypatch.setattr(
        "autoscout.source_probe.urlopen",
        FakeTransport({SOURCE_URL: [FakeResponse(200), FakeResponse(200, html)]}),
    )

    code = main(["probe-source-candidate", "--url", SOURCE_URL, "--ledger", str(tmp_path / "ledger.json")])
    report = json.loads(capsys.readouterr().out)
    assert code == 1
    result = report["candidates"][0]
    assert result["reason_code"] == "missing_authors"
    assert "HOME" in result["reason"]
    diagnostic = result["structural_diagnostics"]
    assert diagnostic == {
        "repeated_siblings": [
            {
                "parent": {"tag": "div", "class_tokens": ["book-list"]},
                "child": {"tag": "div", "class_tokens": ["book-row"]},
                "count": 2,
            }
        ]
    }
    parent_selector = "." + diagnostic["repeated_siblings"][0]["parent"]["class_tokens"][0]
    child_selector = "." + diagnostic["repeated_siblings"][0]["child"]["class_tokens"][0]
    assert f"{parent_selector} > {child_selector}" == ".book-list > .book-row"
    diagnostic_json = json.dumps(diagnostic, ensure_ascii=False)
    for forbidden in (
        "HOME",
        "Sensitive Book",
        "Author One",
        "description",
        "review",
        "9780306406157",
        "$20",
        "raw HTML",
        "book-identifier",
        "price",
    ):
        assert forbidden not in diagnostic_json
    assert "hidden-row" not in diagnostic_json


def test_structural_selection_keeps_high_frequency_rows_within_bounded_output(tmp_path: Path) -> None:
    noise_url = "https://example.test/noise"
    noise = "".join(
        f'<div class="noise-parent-{index}"><span class="noise-child-{index}">x</span>'
        f'<span class="noise-child-{index}">x</span></div>'
        for index in range(13)
    )
    sensitive = (
        '<div class="sensitive-parent"><span class="identifier price">x</span>'
        '<span class="identifier price">x</span></div>'
    )
    rows = "".join(
        f'<div class="book-row"><a href="/book-{index}">Book {index}</a><span>By: Author {index}</span></div>'
        for index in range(8)
    )
    html = f"<html><body><h1>List</h1><div class=breadcrumbs><ul><li><a href='/'>HOME</a></li></ul></div>{noise}{sensitive}<div class=book-list>{rows}</div></body></html>".encode()
    transport = FakeTransport(
        {
            noise_url: [FakeResponse(200, url=noise_url), FakeResponse(200, html, url=noise_url)],
        }
    )

    report, code = run_source_candidate_probe([noise_url], ledger_path=tmp_path / "ledger.json", opener=transport)

    assert code == 1
    diagnostic = report["candidates"][0]["structural_diagnostics"]
    structures = diagnostic["repeated_siblings"]
    assert structures[0] == {
        "parent": {"tag": "div", "class_tokens": ["book-list"]},
        "child": {"tag": "div", "class_tokens": ["book-row"]},
        "count": 8,
    }
    assert 1 <= len(structures) <= 12
    assert sum(len(item["parent"]["class_tokens"]) + len(item["child"]["class_tokens"]) for item in structures) <= 24
    assert all(item["child"]["class_tokens"] for item in structures)
    diagnostic_json = json.dumps(diagnostic, ensure_ascii=False)
    assert "identifier" not in diagnostic_json
    assert "price" not in diagnostic_json
    assert "Book 1" not in diagnostic_json
    assert "Author 1" not in diagnostic_json


def test_malformed_v3_structural_diagnostics_are_rejected_before_network(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    run_source_candidate_probe(
        [SOURCE_URL],
        ledger_path=ledger,
        opener=FakeTransport({SOURCE_URL: [FakeResponse(200), FakeResponse(200, _accepted_page())]}),
    )
    data = json.loads(ledger.read_text(encoding="utf-8"))
    data["entries"][0]["structural_diagnostics"] = {
        "repeated_siblings": [
            {
                "parent": {"tag": "div", "class_tokens": ["list"]},
                "child": {"tag": "div", "class_tokens": ["row"]},
                "count": 1001,
            }
        ]
    }
    ledger.write_text(json.dumps(data), encoding="utf-8")
    original = ledger.read_bytes()

    class NoNetwork:
        def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("unsafe structural diagnostics reached network")

    with pytest.raises(SourceCandidateLedgerError, match="structural_diagnostics"):
        run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, re_probe=True, opener=NoNetwork())
    assert ledger.read_bytes() == original


def test_probe_launcher_forwards_reprobe_flag(tmp_path: Path) -> None:
    launcher_root = tmp_path / "launcher-repo"
    scripts = launcher_root / "scripts"
    scripts.mkdir(parents=True)
    launcher = ROOT / "scripts" / "probe-source"
    shutil.copy2(launcher, scripts / "probe-source")
    scripts.joinpath("probe-source").chmod(0o755)
    python = launcher_root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    python.chmod(0o755)

    result = subprocess.run(
        [str(scripts / "probe-source"), "--url", SOURCE_URL, "--re-probe"],
        cwd=launcher_root,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": os.environ["PATH"]},
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "-m",
        "autoscout",
        "probe-source-candidate",
        "--url",
        SOURCE_URL,
        "--re-probe",
    ]


def test_add_source_probe_forwards_reprobe_flag(tmp_path: Path) -> None:
    launcher_root = tmp_path / "launcher-repo"
    scripts = launcher_root / "scripts"
    scripts.mkdir(parents=True)
    add_source = ROOT / "scripts" / "add-source"
    shutil.copy2(add_source, scripts / "add-source")
    scripts.joinpath("add-source").chmod(0o755)
    marker = tmp_path / "delegated-arguments.txt"
    probe = scripts / "probe-source"
    probe.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {marker}\n", encoding="utf-8")
    probe.chmod(0o755)

    result = subprocess.run(
        [str(scripts / "add-source"), "probe", "--url", SOURCE_URL, "--re-probe"],
        cwd=launcher_root,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": os.environ["PATH"]},
    )

    assert result.returncode == 0
    assert marker.read_text(encoding="utf-8").splitlines() == ["--url", SOURCE_URL, "--re-probe"]
