import csv
from pathlib import Path

from state_store_support import read_state
from state_store_support import seed_state

from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.pipeline import refresh_post_enrichment_artifacts
from autoscout.pipeline import run_pipeline


def test_feedback_after_enrichment_does_not_recollect(tmp_path, monkeypatch):
    config = Path(__file__).resolve().parents[1] / "configs/weekly-mvp-fixture.json"
    run_pipeline(config, tmp_path, tmp_path)
    candidates = read_json(tmp_path / "scouting-candidates.json")
    identity = candidates[0]["identity_key"]

    def describe(records):
        for record in records:
            if record["identity_key"] == identity:
                record["content_description"] = "Описание, сохранённое после проверки источников."
        return records

    write_json(tmp_path / "scouting-candidates.json", describe(read_json(tmp_path / "scouting-candidates.json")))
    for collection in ("candidate_history", "latest_scouting_candidates"):
        seed_state(tmp_path, **{collection: describe(read_state(tmp_path, collection))})
    assert refresh_post_enrichment_artifacts(config, tmp_path, tmp_path)["status"] == "ok"
    csv_path = tmp_path / "editor-facing.csv"
    with csv_path.open() as handle:
        reader = csv.DictReader(handle)
        headers = list(reader.fieldnames)
        rows = list(reader)
    headers += ["Description reason", "Description provenance"]
    for row in rows:
        row["Description reason"] = "Проверено"
        row["Description provenance"] = "backfilled"
    with csv_path.open("w") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    protected = ("source-mentions.json", "source-runs.json", "russian-edition-checks.json")
    before = {name: (tmp_path / name).read_bytes() for name in protected}
    seed_state(
        tmp_path,
        pending_editor_rows=[
            {
                "identity_key": identity,
                "Editor Status": "interested",
                "Editor Note": "Надо запросить.",
            }
        ],
    )

    def no_recollection(*args, **kwargs):
        raise AssertionError("Feedback must preserve the enriched report")

    monkeypatch.setattr("autoscout.pipeline._run_pipeline", no_recollection)
    assert run_pipeline(config, tmp_path, tmp_path)["pipeline_path"] == "feedback-applied"
    history = read_state(tmp_path, "candidate_history")
    saved = next(record for record in history if record["identity_key"] == identity)
    assert saved["editor_note"] == "Надо запросить."
    assert saved["content_description"] == "Описание, сохранённое после проверки источников."
    with csv_path.open() as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == headers
        after_rows = list(reader)

    def non_editor(row):
        return {k: v for k, v in row.items() if k not in {"Editor status", "Editor note"}}

    assert [non_editor(row) for row in after_rows] == [non_editor(row) for row in rows]
    assert all((tmp_path / name).read_bytes() == content for name, content in before.items())
    assert run_pipeline(config, tmp_path, tmp_path)["pipeline_path"] == "feedback-noop"
