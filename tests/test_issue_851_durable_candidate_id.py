"""Issue #851: every review shows the durable Candidate ID, and returned rows resolve by it."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_databases

from autoscout.__main__ import main
from autoscout.candidate_identity import candidate_id_for
from autoscout.candidate_identity import short_candidate_ids
from autoscout.editor_feedback import ingest_feedback
from autoscout.json_io import write_json
from autoscout.review_rendering import render_review
from autoscout.review_row_resolution import AMBIGUOUS
from autoscout.review_row_resolution import NOT_FOUND
from autoscout.review_row_resolution import RESOLVED
from autoscout.review_row_resolution import ReturnedRow
from autoscout.review_row_resolution import ReviewRowResolver
from autoscout.review_row_resolution import read_returned_rows

# The five #849 titles, each held by two identities in Candidate History.
ISSUE_849_PAIRS = [
    (
        "Crucial Conversations (Third Edition)",
        "crucial conversations third edition|joseph grenny & kerry patterson & ron mcmillan",
        ["Joseph Grenny", "Kerry Patterson", "Ron McMillan"],
        "crucial conversations third edition|kerry patterson ron mcmillan and others joseph grenny",
        ["Joseph Grenny, Kerry Patterson, Ron McMillan, and others"],
    ),
    (
        "Soldat d'avantguarda",
        "soldat d avantguarda|joan salvat papasseit",
        ["Joan Salvat-Papasseit"],
        "soldat d avantguarda|joan salvat papasseit frederic j porta",
        ["Joan Salvat-Papasseit Frederic J. Porta"],
    ),
    (
        "A Great Book with Barack Obama",
        "a great book with barack obama|barack obama",
        ["Barack Obama"],
        "a great book with barack obama|higher ground et al barack obama",
        ["Barack Obama, Higher Ground, et al"],
    ),
    (
        "Protocols",
        "protocols|andrew d huberman",
        ["Andrew D. Huberman Ph.D."],
        "protocols|andrew huberman",
        ["Andrew Huberman"],
    ),
    (
        "I Won’t Pretend These Missiles Are Stars",
        "i won t pretend these missiles are stars|the cartoonist collective",
        ["The Cartoonist Collective"],
        "i won t pretend these missiles are stars|the cartoonist cooperative",
        ["The Cartoonist Cooperative"],
    ),
]


def _record(identity_key: str, title: str, authors: list[str], status: str = "") -> dict[str, Any]:
    return {
        "candidate_id": candidate_id_for(identity_key),
        "identity_key": identity_key,
        "title": title,
        "primary_authors": authors,
        "editor_status": status,
    }


def _issue_849_history() -> list[dict[str, Any]]:
    history = []
    for title, first_key, first_authors, second_key, second_authors in ISSUE_849_PAIRS:
        history.append(_record(first_key, title, first_authors, "reject"))
        history.append(_record(second_key, title, second_authors))
    return history


def _resolver(history: list[dict[str, Any]], alias_table: dict[str, Any] | None = None) -> ReviewRowResolver:
    return ReviewRowResolver(history, alias_table or {"version": 1, "aliases": {}, "candidates": {}})


def _row(**fields: str) -> ReturnedRow:
    return ReturnedRow.from_mapping(fields)


# --- short IDs -------------------------------------------------------------


def test_short_ids_are_the_last_eight_hex_characters() -> None:
    assert short_candidate_ids(["candidate-6d58372233ef3494dc8db9e0"]) == {
        "candidate-6d58372233ef3494dc8db9e0": "dc8db9e0"
    }


def test_short_ids_lengthen_until_unique() -> None:
    ids = ["candidate-aaaaaaaaaaaa11112222", "candidate-bbbbbbbbbbbb11112222", "candidate-cccccccccccc33334444"]
    short = short_candidate_ids(ids)
    assert len(set(short.values())) == 3
    assert {len(value) for value in short.values()} == {9}
    assert short["candidate-aaaaaaaaaaaa11112222"] == "a11112222"


# --- rendered reviews ------------------------------------------------------


def _write_review_state(tmp_path: Path) -> dict[str, Any]:
    record = _record("city book|alex author", "City Book", ["Alex Author"], "maybe_watch")
    candidate = {
        "candidate_id": record["candidate_id"],
        "identity_key": record["identity_key"],
        "title": "City Book",
        "primary_authors": ["Alex Author"],
        "update_delta": "new",
        "priority_label": "A",
        "independent_signal_count": 1,
        "source_evidence": [{"source_name": "Source", "source_url": "https://example.test/book"}],
    }
    older = _record("old book|sam writer", "Old Book", ["Sam Writer"], "reject")
    older["last_seen_date"] = "2026-09-01"
    write_json(tmp_path / "scouting-candidates.json", [candidate])
    seed_state(tmp_path, candidate_history=[record, older])
    write_json(tmp_path / "run-summary.json", {"run_date": "2026-09-14", "editor_facing_count": 1})
    return record


def test_every_review_row_carries_the_durable_id(tmp_path: Path) -> None:
    record = _write_review_state(tmp_path)
    older_id = candidate_id_for("old book|sam writer")

    assert render_review(tmp_path) == 0

    markdown = (tmp_path / "weekly-review.md").read_text(encoding="utf-8")
    html = (tmp_path / "weekly-review.html").read_text(encoding="utf-8")
    with (tmp_path / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        rows = list(reader)

    assert "| # | ID | Название |" in markdown
    for candidate_id in (record["candidate_id"], older_id):
        assert f"| {candidate_id[-8:]} |" in markdown
        assert f'data-candidate-id="{candidate_id}"' in html
        assert f'<code class="cid">{candidate_id[-8:]}</code>' in html
    # The identity-key column keeps its name and value; the durable ID sits beside it.
    assert header[header.index("Candidate ID") + 1] == "Durable Candidate ID"
    assert {(row["Candidate ID"], row["Durable Candidate ID"]) for row in rows} == {
        (record["identity_key"], record["candidate_id"]),
        ("old book|sam writer", older_id),
    }


def test_rendered_markdown_and_csv_resolve_back_to_their_rows(tmp_path: Path) -> None:
    _write_review_state(tmp_path)
    assert render_review(tmp_path) == 0
    history = read_state(tmp_path, "candidate_history")
    resolver = _resolver(history)

    for name, method in (("weekly-review.md", "short_durable_id"), ("editor-facing.csv", "durable_id")):
        rows = read_returned_rows(tmp_path / name)
        results = [resolver.resolve(row, number) for number, row in enumerate(rows, start=1)]
        assert [result["status"] for result in results] == [RESOLVED, RESOLVED]
        assert {result["matched_by"] for result in results} == {method}
        assert [result["candidate"]["title"] for result in results] == ["City Book", "Old Book"]


# --- resolver --------------------------------------------------------------


def test_issue_849_titles_alone_are_ambiguous_with_both_identities() -> None:
    resolver = _resolver(_issue_849_history())
    for title, first_key, _first_authors, second_key, _second_authors in ISSUE_849_PAIRS:
        result = resolver.resolve(_row(title=title), 1)
        assert result["status"] == AMBIGUOUS
        assert result["matched_by"] == "title"
        assert {alternative["identity_key"] for alternative in result["alternatives"]} == {first_key, second_key}
        statuses = {alternative["identity_key"]: alternative["editor_status"] for alternative in result["alternatives"]}
        assert statuses == {first_key: "reject", second_key: ""}
        assert all(alternative["candidate_id"].startswith("candidate-") for alternative in result["alternatives"])


def test_issue_849_durable_ids_resolve_each_identity_uniquely() -> None:
    resolver = _resolver(_issue_849_history())
    for title, first_key, _first_authors, second_key, _second_authors in ISSUE_849_PAIRS:
        for identity_key in (first_key, second_key):
            candidate_id = candidate_id_for(identity_key)
            for supplied, method in ((candidate_id, "durable_id"), (candidate_id[-8:], "short_durable_id")):
                result = resolver.resolve(_row(durable_candidate_id=supplied, title=title), 1)
                assert result["status"] == RESOLVED, (supplied, result)
                assert result["matched_by"] == method
                assert result["candidate"]["identity_key"] == identity_key
                assert result["warnings"] == []


def test_a_short_id_shared_by_two_ids_is_ambiguous() -> None:
    first = {"candidate_id": "candidate-aaaaaaaaaaaa11112222", "identity_key": "one|a", "title": "One"}
    second = {"candidate_id": "candidate-bbbbbbbbbbbb11112222", "identity_key": "two|b", "title": "Two"}
    result = _resolver([first, second]).resolve(_row(ID="11112222"), 1)
    assert result["status"] == AMBIGUOUS
    assert [alternative["identity_key"] for alternative in result["alternatives"]] == ["one|a", "two|b"]


def test_a_redirected_durable_id_is_the_same_identity() -> None:
    survivor = _record("book|ann author", "Book", ["Ann Author"], "reject")
    retired_id = candidate_id_for("book|ann author & md")
    table = {
        "version": 2,
        "aliases": {"book|ann author & md": retired_id},
        "candidates": {retired_id: {"candidate_id": retired_id, "redirect_to": survivor["candidate_id"]}},
        "redirects": {retired_id: survivor["candidate_id"]},
        "durable_merges": [],
    }
    resolver = _resolver([survivor], table)

    by_id = resolver.resolve(_row(durable_candidate_id=retired_id), 1)
    assert by_id["status"] == RESOLVED
    assert by_id["candidate"]["candidate_id"] == survivor["candidate_id"]
    assert by_id["warnings"] == ["redirected_durable_id"]
    by_short_id = resolver.resolve(_row(ID=retired_id[-8:]), 2)
    assert by_short_id["candidate"]["candidate_id"] == survivor["candidate_id"]
    by_alias_key = resolver.resolve(_row(identity_key="book|ann author & md"), 3)
    assert by_alias_key["status"] == RESOLVED
    assert by_alias_key["matched_by"] == "identity_key"
    assert by_alias_key["candidate"]["candidate_id"] == survivor["candidate_id"]


def test_resolution_order_and_reported_fallbacks() -> None:
    resolver = _resolver(_issue_849_history())

    by_key = resolver.resolve(_row(identity_key="protocols|andrew huberman", title="Protocols"), 1)
    assert (by_key["status"], by_key["matched_by"]) == (RESOLVED, "identity_key")

    by_author = resolver.resolve(_row(title="Protocols", author="Andrew Huberman"), 2)
    assert (by_author["status"], by_author["matched_by"]) == (RESOLVED, "title_author")
    assert by_author["candidate"]["identity_key"] == "protocols|andrew huberman"
    # The other identity under the same title is reported, never dropped silently.
    assert by_author["warnings"] == ["other_identities_share_title"]
    assert [item["identity_key"] for item in by_author["same_title_alternatives"]] == ["protocols|andrew d huberman"]

    wrong_author = resolver.resolve(_row(title="Protocols", author="Somebody Else"), 3)
    assert (wrong_author["status"], wrong_author["matched_by"]) == (AMBIGUOUS, "title")
    assert wrong_author["warnings"] == ["author_not_matched"]

    missing = resolver.resolve(_row(title="No Such Book", durable_candidate_id="candidate-0123456789abcdef01234567"), 4)
    assert missing["status"] == NOT_FOUND
    assert missing["warnings"] == ["durable_id_not_found"]


def test_identifiers_that_disagree_are_ambiguous() -> None:
    resolver = _resolver(_issue_849_history())
    result = resolver.resolve(
        _row(
            durable_candidate_id=candidate_id_for("protocols|andrew huberman"),
            identity_key="protocols|andrew d huberman",
        ),
        1,
    )
    assert result["status"] == AMBIGUOUS
    assert result["warnings"] == ["identifiers_disagree"]
    assert len(result["alternatives"]) == 2


def _tree_digest(root: Path) -> dict[str, str]:
    files = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    return {**files, **{name: checksum.hex() for name, checksum in state_databases().items()}}


def test_command_is_read_only_and_reports_exit_codes(tmp_path: Path, capsys: Any) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    seed_state(state_dir, candidate_history=_issue_849_history())
    rows_path = tmp_path / "rows.json"
    write_json(rows_path, [{"title": title} for title, *_rest in ISSUE_849_PAIRS])
    before = _tree_digest(tmp_path)

    assert main(["resolve-review-rows", "--state-dir", str(state_dir), "--rows", str(rows_path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["counts"] == {RESOLVED: 0, AMBIGUOUS: 5, NOT_FOUND: 0}
    assert _tree_digest(tmp_path) == before

    write_json(rows_path, [{"durable_candidate_id": candidate_id_for(ISSUE_849_PAIRS[0][1])}])
    after_rewrite = _tree_digest(tmp_path)
    assert main(["resolve-review-rows", "--state-dir", str(state_dir), "--rows", str(rows_path)]) == 0
    assert json.loads(capsys.readouterr().out)["counts"][RESOLVED] == 1
    assert _tree_digest(tmp_path) == after_rewrite

    bad = tmp_path / "rows.txt"
    bad.write_text("nothing", encoding="utf-8")
    assert main(["resolve-review-rows", "--state-dir", str(state_dir), "--rows", str(bad)]) == 2


def test_ingest_feedback_ignores_the_durable_id_column(tmp_path: Path, capsys: Any) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    record = _record("city book|alex author", "City Book", ["Alex Author"])
    seed_state(state_dir, candidate_history=[record])
    csv_path = tmp_path / "returned.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["Title", "Author", "Editor status", "Editor note", "Candidate ID", "Durable Candidate ID"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "Title": "City Book",
                "Author": "Alex Author",
                "Editor status": "reject",
                "Editor note": "нет",
                "Candidate ID": record["identity_key"],
                "Durable Candidate ID": record["candidate_id"],
            }
        )

    assert ingest_feedback(csv_path, state_dir) == 0
    staged = read_state(state_dir, "pending_editor_rows")
    assert staged == [
        {
            "identity_key": "city book|alex author",
            "Editor Status": "reject",
            "Editor Note": "нет",
            "Title": "City Book",
            "Author": "Alex Author",
            # Stamped on every confirmed row since #1053; the record has no description.
            "Decision Date": date.today().isoformat(),
            "Description Digest": "",
        }
    ]


def test_comma_joined_author_displays_match_on_both_sides() -> None:
    joined = _record("book|ann author & bob writer", "Book", ["Ann Author", "Bob Writer"])
    unsplit = _record("book|bob writer ann author", "Book", ["Ann Author, Bob Writer"])
    other = _record("book|zed other", "Book", ["Zed Other"])

    only_joined = _resolver([joined, other]).resolve(_row(title="Book", author="Ann Author, Bob Writer"), 1)
    assert (only_joined["status"], only_joined["matched_by"]) == (RESOLVED, "title_author")
    assert only_joined["candidate"]["identity_key"] == joined["identity_key"]

    # The two records show the same display, so the author cannot tell them apart.
    both = _resolver([joined, unsplit, other]).resolve(_row(title="Book", author="Ann Author, Bob Writer"), 1)
    assert (both["status"], both["matched_by"]) == (AMBIGUOUS, "title_author")
    assert {item["identity_key"] for item in both["alternatives"]} == {joined["identity_key"], unsplit["identity_key"]}


def test_markdown_tables_without_outer_pipes_are_read(tmp_path: Path) -> None:
    path = tmp_path / "review.md"
    path.write_text("ID | Название\n--- | ---\ndc8db9e0 | Protocols\n", encoding="utf-8")
    rows = read_returned_rows(path)
    assert [(row.durable_id, row.title) for row in rows] == [("dc8db9e0", "Protocols")]


def test_markdown_cells_keep_escaped_pipes(tmp_path: Path) -> None:
    path = tmp_path / "review.md"
    path.write_text(
        "| Candidate ID | Название | Автор |\n|---|---|---|\n| book\\|ann author | Book | Ann Author |\n"
        "| home&#124;thi le | Home | Thi Lê |\n",
        encoding="utf-8",
    )
    rows = read_returned_rows(path)
    assert [(row.identity_key, row.title, row.author) for row in rows] == [
        ("book|ann author", "Book", "Ann Author"),
        ("home|thi le", "Home", "Thi Lê"),
    ]


def test_a_nonempty_file_without_rows_is_an_input_error(tmp_path: Path, capsys: Any) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    seed_state(state_dir, candidate_history=_issue_849_history())
    path = tmp_path / "review.md"
    path.write_text("Редактор: всё отклонить.\n", encoding="utf-8")
    assert main(["resolve-review-rows", "--state-dir", str(state_dir), "--rows", str(path)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "input_error"


def test_surname_first_coauthors_keep_each_person_together() -> None:
    first = _record("home|john smith & jane doe", "Home", ["Smith, John", "Doe, Jane"])
    second = _record("home|jane smith & john doe", "Home", ["Smith, Jane", "Doe, John"])
    resolver = _resolver([first, second])
    result = resolver.resolve(_row(title="Home", author="Smith, John, Doe, Jane"), 1)
    assert (result["status"], result["matched_by"]) == (RESOLVED, "title_author")
    assert result["candidate"]["identity_key"] == first["identity_key"]


def test_a_partial_author_overlap_does_not_pick_an_identity() -> None:
    john = _record("home|john smith", "Home", ["Smith, John"])
    alice = _record("home|alice brown", "Home", ["Brown, Alice"])
    result = _resolver([john, alice]).resolve(_row(title="Home", author="Smith, Jane"), 1)
    assert (result["status"], result["matched_by"]) == (AMBIGUOUS, "title")
    assert result["warnings"] == ["author_not_matched"]


def test_short_ids_stay_unique_against_retired_ids(tmp_path: Path) -> None:
    record = _write_review_state(tmp_path)
    # A retired ID sharing the live ID's eight-character tail, kept by a merge.
    retired_id = "candidate-ffffffffffffffff" + record["candidate_id"][-8:]
    survivor_id = candidate_id_for("old book|sam writer")
    seed_state(
        tmp_path,
        candidate_identity_aliases={
            "version": 2,
            "aliases": {},
            "candidates": {retired_id: {"candidate_id": retired_id, "redirect_to": survivor_id}},
            "redirects": {retired_id: survivor_id},
            "durable_merges": [],
        },
    )

    assert render_review(tmp_path) == 0
    history = read_state(tmp_path, "candidate_history")
    table = read_state(tmp_path, "candidate_identity_aliases")
    rows = read_returned_rows(tmp_path / "weekly-review.md")
    assert {len(row.durable_id) for row in rows} == {9}
    results = [_resolver(history, table).resolve(row, number) for number, row in enumerate(rows, start=1)]
    assert [result["status"] for result in results] == [RESOLVED, RESOLVED]
    assert results[0]["candidate"]["candidate_id"] == record["candidate_id"]


def test_legacy_records_without_a_durable_id_still_match() -> None:
    current = _record("home|john smith", "Home", ["John Smith"])
    legacy = {"identity_key": "home|alice brown", "title": "Home", "primary_authors": ["Alice Brown"]}
    resolver = _resolver([current, legacy])

    by_key = resolver.resolve(_row(identity_key="home|alice brown"), 1)
    assert (by_key["status"], by_key["candidate"]["candidate_id"]) == (RESOLVED, "")
    by_title = resolver.resolve(_row(title="Home"), 2)
    assert by_title["status"] == AMBIGUOUS
    assert {item["identity_key"] for item in by_title["alternatives"]} == {"home|john smith", "home|alice brown"}
