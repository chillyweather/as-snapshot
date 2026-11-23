"""Issue #843: qualify a model for description synthesis on frozen evidence.

These tests spend no Codex request.  A synthetic benchmark is built through
the real refresh step from fake Git history, and fake runners answer each
work item, so the production description gate and the qualification metrics
see realistic answers.  The committed benchmark is checked against the
production synthesizer, and the real runner is exercised against a fake
``codex`` executable.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import textwrap
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from autoscout import description_qualification as dq
from autoscout.__main__ import main
from autoscout.codex_model_pin import CodexModelPin
from autoscout.grounded_description import GROUNDED_SYNTHESIS_PROMPT
from autoscout.grounded_description import CodexGroundedDescriptionSynthesizer
from autoscout.grounded_description import evaluate_grounding_gate

FIXED_NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
RUN_DIR = dq.WEEKLY_RUN_DIR

RUSSIAN = (
    "Книга прослеживает историю киномузыки от первых звуковых фильмов до современных блокбастеров "
    "и показывает, как менялись её стили. Автор подробно разбирает совместную работу композиторов "
    "и режиссёров и объясняет, почему эти союзы определили облик многих известных картин."
)
ENGLISH = (
    "The book traces film music from the first sound pictures to present-day blockbusters and shows "
    "how its styles changed. It examines how composers and directors worked together and why those "
    "partnerships shaped many well-known films."
)

EVIDENCE = (
    "A guide to the history of movie music that goes behind the scenes of hundreds of film scores, "
    "with photographs from recording studios and portraits of the composers who wrote them. "
    "It explains how composer and director partnerships changed the sound of cinema."
)


# -- Synthetic history -------------------------------------------------------


def _record(url: str, language: str, text: str, title: str, author: str) -> dict[str, Any]:
    return {
        "url": url,
        "source_kind": "independent",
        "source_family": "independent",
        "owner_id": url.split("/")[2],
        "source_language": language,
        "title_evidence_route": "direct_original",
        "checked_date": "2026-10-17",
        "raw_title": title,
        "raw_authors": [author],
        "subject_key": "",
        "display_text": text,
        "relevance_tier": 0,
        "discovery_ordinal": 0,
    }


def _outcome(key: str, outcome: str, reason: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "candidate_id": f"candidate-{abs(hash(key)) % 10**8}",
        "identity_key": key,
        "outcome": outcome,
        "reason_code": reason,
        "content_description": RUSSIAN if outcome != "attempted_no_description" else "",
        "research_source_records": records,
    }


def _history_record(key: str, title: str, author: str) -> dict[str, Any]:
    return {"identity_key": key, "title": title, "primary_authors": [author], "content_description": RUSSIAN}


SYNTHETIC = [
    # (identity key, outcome, reason, language, source count, evidence)
    ("film scores|ann author", "grounded", "", "en", 2, EVIDENCE),
    ("klang der bilder|berta autorin", "single_source", "", "de", 1, EVIDENCE),
    ("musica del cine|carla autora", "grounded", "", "es", 2, EVIDENCE),
    ("musique de film|denise auteur", "single_source", "", "fr", 1, EVIDENCE),
    ("filmmusikk|erik forfatter", "single_source", "", "no", 1, EVIDENCE),
    ("hudba filmu|frantisek autor", "grounded", "", "cs", 2, EVIDENCE),
    ("filmmuziek|geert auteur", "single_source", "", "nl", 1, EVIDENCE),
    ("thin one|hana author", "attempted_no_description", "invalid_description", "en", 1, "Includes index"),
    ("thin two|ivan author", "attempted_no_description", "insufficient_description_depth", "en", 1, "A short note."),
]


def _fake_git(tmp_path: Path) -> tuple[Any, Any, list[tuple[str, ...]]]:
    calls: list[tuple[str, ...]] = []
    outcomes = []
    history = []
    for key, outcome, reason, language, count, text in SYNTHETIC:
        title = key.partition("|")[0].title()
        author = key.partition("|")[2].title()
        records = [
            _record(
                f"https://shop{index}.example/{key.partition('|')[0].replace(' ', '-')}", language, text, title, author
            )
            for index in range(count)
        ]
        outcomes.append(_outcome(key, outcome, reason, records))
        history.append(_history_record(key, title, author))
    files = {
        ("c1", f"{RUN_DIR}/{dq.STAGE_ARTIFACT}"): {"operation_date": "2026-10-17", "outcomes": outcomes},
        ("c1", f"{RUN_DIR}/candidate-history.json"): history,
        ("HEAD", f"{RUN_DIR}/candidate-history.json"): history,
    }

    def git_show(repo_root: Path, commit: str, path: str) -> bytes | None:
        calls.append(("show", commit, path))
        document = files.get((commit, path))
        return None if document is None else json.dumps(document, ensure_ascii=False).encode("utf-8")

    def git_commits(repo_root: Path, path: str, depth: int) -> list[str]:
        calls.append(("log", path))
        return ["c1"]

    return git_show, git_commits, calls


@pytest.fixture()
def benchmark(tmp_path: Path) -> Path:
    git_show, git_commits, _calls = _fake_git(tmp_path)
    directory = tmp_path / "benchmark"
    dq.refresh_benchmark(
        repo_root=tmp_path, benchmark_dir=directory, git_show=git_show, git_commits=git_commits, target_size=36
    )
    return directory


# -- Fake runners ------------------------------------------------------------


class ScriptedRunner:
    """Answers each work item by the behaviour assigned to the candidate."""

    def __init__(self, behaviours: Mapping[str, str], *, thin_keys: set[str]) -> None:
        self.behaviours = behaviours
        self.thin_keys = thin_keys
        self.calls: list[tuple[str, str, str, float]] = []

    def cli_version(self) -> str:
        return "codex-cli 0.0.0-test"

    def run(self, pin: CodexModelPin, work_item: Mapping[str, Any], *, prompt: str, timeout_seconds: float):
        label = f"{pin.model}:{pin.reasoning_effort}"
        key = str(work_item["identity_key"])
        self.calls.append((label, key, prompt, timeout_seconds))
        behaviour = self.behaviours[label]
        profile = {"model": pin.model, "reasoning_effort": pin.reasoning_effort}
        duration = 120.0 if behaviour == "slow" else 10.0
        if behaviour == "wrong_model":
            profile = {"model": "some-default", "reasoning_effort": "high"}
        thin = key in self.thin_keys
        if behaviour == "invalid":
            value: dict[str, Any] = {"content_description": "An English description is not a Russian one."}
        elif thin and behaviour != "padder":
            value = {}
        else:
            value = {"content_description": RUSSIAN, "english_description": ENGLISH}
        return dq.SynthesisCall(dq.STATUS_ANSWERED, {key: value}, duration, profile, 1000)


def _thin_keys(directory: Path) -> set[str]:
    return {item.identity_key for item in dq.load_benchmark(directory) if item.item_class == dq.CLASS_THIN}


def _qualify(directory: Path, tmp_path: Path, behaviours: Mapping[str, str], **kwargs: Any) -> dict[str, Any]:
    runner = kwargs.pop("runner", None) or ScriptedRunner(behaviours, thin_keys=_thin_keys(directory))
    return dq.qualify_description_models(
        list(behaviours),
        benchmark_dir=directory,
        reports_dir=tmp_path / "reports",
        runner=runner,
        seed="seed-1",
        now=lambda: FIXED_NOW,
        **kwargs,
    )


def _candidate(report: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    return next(item for item in report["candidates"] if item["label"] == label)


def _criterion(candidate: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    return next(item for item in candidate["criteria"] if item["name"] == name)


# -- Benchmark ---------------------------------------------------------------


def test_refresh_builds_a_mixed_benchmark_from_history(benchmark: Path) -> None:
    manifest = json.loads((benchmark / dq.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["coverage"]["thin_evidence"] == 2
    assert manifest["coverage"]["single_source"] == 4
    assert manifest["coverage"]["multi_source"] == 3
    assert set(manifest["coverage"]["languages"]) == {"cs", "de", "en", "es", "fr", "nl", "no"}
    assert "prominence_claim" in manifest["uncovered"]
    items = dq.load_benchmark(benchmark)
    assert len(items) == len(SYNTHETIC)
    # Thin items come first, and every item rebuilds its production work item.
    assert [item.item_class for item in items[:2]] == [dq.CLASS_THIN, dq.CLASS_THIN]
    for item in items:
        loaded = dq.load_item(item)
        assert loaded.work_item["identity_key"] == item.identity_key


def test_refresh_reads_git_and_only_writes_the_benchmark(tmp_path: Path) -> None:
    git_show, git_commits, calls = _fake_git(tmp_path)
    directory = tmp_path / "benchmark"
    (directory / "items").mkdir(parents=True)
    stale = directory / "items" / "stale-item.json"
    stale.write_text("{}", encoding="utf-8")
    dq.refresh_benchmark(repo_root=tmp_path, benchmark_dir=directory, git_show=git_show, git_commits=git_commits)
    assert ("log", f"{RUN_DIR}/{dq.STAGE_ARTIFACT}") in calls
    assert not stale.exists()
    written = {path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file()}
    assert all(path.parts[0] == "benchmark" for path in written)


def test_tampered_evidence_is_refused(benchmark: Path) -> None:
    item = dq.load_benchmark(benchmark)[-1]
    document = json.loads(item.path.read_text(encoding="utf-8"))
    document["research_source_records"][0]["display_text"] = "Different evidence than was frozen."
    item.path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(dq.QualificationInputError, match="no longer matches the production work item"):
        dq.load_item(item)


def test_committed_benchmark_rebuilds_what_the_production_synthesizer_sends() -> None:
    """Every frozen work item is the item production would send for its evidence."""

    class CapturingAgent:
        def __init__(self) -> None:
            self.items: list[dict[str, Any]] = []

        def generate(self, work_items, *, _prompt_override=None, _timeout_seconds=None):
            assert _prompt_override == GROUNDED_SYNTHESIS_PROMPT
            self.items.extend(work_items)
            return {}

    items = dq.load_benchmark()
    assert 30 <= len(items) <= 40
    for item in items:
        loaded = dq.load_item(item)
        production_sources = loaded.sources
        if len(loaded.sources) > 1:
            records = item.document()["research_source_records"]
            fetched = [
                {
                    **record,
                    "title": record.get("raw_title", ""),
                    "primary_authors": record.get("raw_authors", []),
                    "subject_text": record.get("display_text", ""),
                }
                for record in records
            ]
            gate = evaluate_grounding_gate(loaded.target(), fetched)
            assert gate.accepted
            # Check the gate-to-synthesizer seam, not just the work-item builder.
            assert [source.to_dict() for source in gate.accepted_sources] == [
                source.to_dict() for source in loaded.sources
            ]
            production_sources = gate.accepted_sources
        agent = CapturingAgent()
        CodexGroundedDescriptionSynthesizer(agent).synthesize(
            loaded.candidate, production_sources, remaining_seconds=60
        )
        assert agent.items == [item.document()["work_item"]]


def test_committed_benchmark_is_deliberately_mixed() -> None:
    manifest = json.loads((dq.DEFAULT_BENCHMARK_DIR / dq.MANIFEST_NAME).read_text(encoding="utf-8"))
    coverage = manifest["coverage"]
    assert manifest["uncovered"] == []
    assert coverage["thin_evidence"] >= 6
    assert coverage["prominence_claim"] >= 1
    assert coverage["single_source"] >= 6 and coverage["multi_source"] >= 6
    languages = set(coverage["languages"])
    assert {"en", "de", "es", "fr", "nl"} <= languages
    assert languages & {"no", "nb", "nn", "sv", "da", "fi", "is"}
    assert languages & {"cs", "pl"}


# -- Execution and metrics ---------------------------------------------------


def test_every_candidate_runs_every_item_for_each_repetition(benchmark: Path, tmp_path: Path) -> None:
    behaviours = {"model-a:low": "good", "model-a:medium": "good", "model-b:low": "good"}
    runner = ScriptedRunner(behaviours, thin_keys=_thin_keys(benchmark))
    _qualify(benchmark, tmp_path, behaviours, repetitions=2, runner=runner, per_call_timeout=77.0)
    keys = {item.identity_key for item in dq.load_benchmark(benchmark)}
    for label in behaviours:
        made = [call for call in runner.calls if call[0] == label]
        assert len(made) == 2 * len(keys)
        assert {call[1] for call in made} == keys
    assert {call[2] for call in runner.calls} == {GROUNDED_SYNTHESIS_PROMPT}
    assert {call[3] for call in runner.calls} == {77.0}


def test_the_codex_command_is_isolated_and_names_model_and_effort(tmp_path: Path) -> None:
    pin = dq.parse_candidate("gpt-6-luna:medium")
    command = dq.codex_synthesis_command(pin, "PROMPT", tmp_path / "response.json", tmp_path)
    assert command[:2] == ["codex", "exec"]
    for flag in ("--ignore-user-config", "--ephemeral", "--skip-git-repo-check"):
        assert flag in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("-m") + 1] == "gpt-6-luna"
    assert command[command.index("-c") + 1] == 'model_reasoning_effort="medium"'
    assert command[command.index("--disable") + 1] == "memories"
    assert command[-1] == "PROMPT"


def test_metrics_and_eligibility_for_synthetic_failures(benchmark: Path, tmp_path: Path) -> None:
    behaviours = {
        "good:low": "good",
        "invalid:low": "invalid",
        "padder:low": "padder",
        "slow:low": "slow",
        "default:low": "wrong_model",
    }
    report = _qualify(benchmark, tmp_path, behaviours)

    good = _candidate(report, "good:low")
    assert good["summary"]["validity_rate"] == 1.0
    assert good["summary"]["honest_decline_rate"] == 1.0
    assert good["summary"]["stability_rate"] == 1.0
    assert good["verdict"] == dq.VERDICT_PENDING

    invalid = _candidate(report, "invalid:low")
    assert invalid["summary"]["validity_rate"] == 0.0
    assert _criterion(invalid, "validity_within_5_points_of_best")["passed"] is False
    assert invalid["verdict"] == dq.VERDICT_INELIGIBLE

    padder = _candidate(report, "padder:low")
    assert padder["summary"]["honest_decline_rate"] is None
    assert padder["summary"]["unreviewed_thin_calls"] == padder["summary"]["thin_calls"] > 0
    assert padder["summary"]["padded_thin_calls"] == 0
    assert _criterion(padder, "honest_decline_at_least_90_percent")["passed"] is None
    assert padder["verdict"] == dq.VERDICT_PENDING

    slow = _candidate(report, "slow:low")
    assert slow["summary"]["duration_p95_seconds"] == 120.0
    assert _criterion(slow, "p95_within_candidate_envelope")["passed"] is False

    default = _candidate(report, "default:low")
    assert _criterion(default, "ran_on_requested_model")["passed"] is False

    assert report["ranking"] == []
    assert dq.exit_code(report) == dq.EXIT_OK


def test_an_unmeasured_criterion_fails(benchmark: Path, tmp_path: Path) -> None:
    substantive = [item.slug for item in dq.load_benchmark(benchmark) if item.item_class == dq.CLASS_SUBSTANTIVE]
    report = _qualify(benchmark, tmp_path, {"good:low": "good"}, items=substantive[:2], repetitions=1)
    good = _candidate(report, "good:low")
    assert _criterion(good, "honest_decline_at_least_90_percent")["passed"] is False
    assert good["verdict"] == dq.VERDICT_INELIGIBLE
    assert dq.exit_code(report) == dq.EXIT_NONE_ELIGIBLE


def test_answers_go_through_the_production_description_gate() -> None:
    items = [dq.load_item(item) for item in dq.load_benchmark()]
    item = next(item for item in items if item.item.item_class == dq.CLASS_SUBSTANTIVE)
    key = item.work_item["identity_key"]

    def assess(value: Any) -> dict[str, Any]:
        return dq.assess_answer(item, dq.SynthesisCall(dq.STATUS_ANSWERED, {key: value}, 1.0))

    assert assess({"content_description": RUSSIAN, "english_description": ENGLISH})["valid"] is True
    short = assess({"content_description": "Короткая книга о музыке. Она интересна."})
    assert short["russian_status"] == "insufficient_description_depth"
    assert short["valid"] is False
    assert assess({"content_description": "Only English text here."})["russian_status"] == "invalid_description"
    missing_english = assess({"content_description": RUSSIAN})
    assert missing_english["described"] is True and missing_english["english_status"] == "missing"
    assert missing_english["valid"] is False
    bad_claim = assess(
        {
            "content_description": RUSSIAN,
            "english_description": ENGLISH,
            "prominence_claims": [{"claim_type": "prize", "evidence_urls": ["https://elsewhere.example/"]}],
        }
    )
    assert bad_claim["claims_refusal_detail"] and bad_claim["valid"] is False
    declined = assess({})
    assert declined["declined"] is True and declined["described"] is False
    assert assess("")["declined"] is True
    for malformed in (
        {"other|key": {"content_description": RUSSIAN}},
        {key: 5},
        {key: {"content_description": 5}},
        {key: {"content_description": [RUSSIAN], "english_description": ENGLISH}},
        {key: {"content_description": RUSSIAN, "english_description": ENGLISH}, "extra|key": {}},
    ):
        answer = dq.assess_answer(item, dq.SynthesisCall(dq.STATUS_ANSWERED, malformed, 1.0))
        assert answer["declined"] is False
        assert answer["russian_status"] == "malformed_response"
        assert answer["valid"] is False
        assert answer["described"] is False
    assert dq.assess_answer(item, dq.SynthesisCall(dq.STATUS_ANSWERED, {}, 1.0))["declined"] is True
    timeout = dq.assess_answer(item, dq.SynthesisCall(dq.STATUS_TIMEOUT, None, 180.0))
    assert timeout["declined"] is False and timeout["russian_status"] == "no_answer"


def test_unsupported_numbers_and_names_are_flagged() -> None:
    evidence = "A history of 400 film scores by Bernard Herrmann, written in 2024."
    text = "It covers 400 scores and Herrmann's work. Published in 2019 with a foreword by John Williams."
    assert dq.unsupported_tokens(text, evidence) == ["2019", "John", "Williams"]
    assert dq.unsupported_tokens("В книге 400 партитур, изданных в 2024 году.", evidence) == []


def test_candidates_must_name_model_and_effort() -> None:
    assert dq.parse_candidate("gpt-6.1-sol:low").as_profile() == {"model": "gpt-6.1-sol", "reasoning_effort": "low"}
    for value in ("gpt-6.1-sol", ":low", "gpt-6.1-sol:", "-m:low"):
        with pytest.raises(dq.QualificationInputError):
            dq.parse_candidate(value)


# -- Read-only guarantees ----------------------------------------------------


def test_qualification_writes_only_its_report_and_calls_no_git_or_network(
    benchmark: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"unexpected process or network access: {args!r}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    report = _qualify(benchmark, tmp_path, {"good:low": "good", "padder:low": "padder"})

    after = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert {path: data for path, data in after.items() if path in before} == before
    created = {path.relative_to(tmp_path / "reports") for path in set(after) - set(before)}
    report_dir = Path(report["report_dir"]).name
    assert created == {
        Path(report_dir) / name
        for name in (dq.REPORT_JSON, dq.REPORT_MARKDOWN, dq.BLIND_SHEET, dq.BLIND_KEY, dq.BLIND_RATINGS)
    }


# -- Blind review ------------------------------------------------------------


def _ratings_for(report_dir: Path, rate: Any) -> dict[str, Any]:
    key = json.loads((report_dir / dq.BLIND_KEY).read_text(encoding="utf-8"))
    template = json.loads((report_dir / dq.BLIND_RATINGS).read_text(encoding="utf-8"))
    return {
        "items": {
            slug: {letter: rate(key["items"][slug][letter], slug) for letter in letters}
            for slug, letters in template["items"].items()
        }
    }


def test_blind_sheet_never_reveals_the_model_and_shuffles_from_the_seed(benchmark: Path, tmp_path: Path) -> None:
    behaviours = {"alpha-model:low": "good", "beta-model:medium": "padder"}
    report = _qualify(benchmark, tmp_path, behaviours)
    report_dir = Path(report["report_dir"])
    sheet = (report_dir / dq.BLIND_SHEET).read_text(encoding="utf-8")
    for secret in ("alpha-model", "beta-model", dq.HISTORY_COLUMN, "low", "medium"):
        assert secret not in sheet

    items = [dq.load_item(item) for item in dq.load_benchmark(benchmark)]
    records = [run for candidate in report["candidates"] for run in candidate["runs"]]
    labels = list(behaviours)
    first = dq.build_blind_review(items, labels, records, "seed-1")
    again = dq.build_blind_review(items, labels, records, "seed-1")
    other = dq.build_blind_review(items, labels, records, "seed-2")
    assert first == again
    assert json.loads((report_dir / dq.BLIND_KEY).read_text(encoding="utf-8")) == first[1]
    assert first[1]["items"] != other[1]["items"]
    # The Candidate History description joins as one more anonymous column.
    for columns in first[1]["items"].values():
        assert sorted(columns.values()) == sorted([*labels, dq.HISTORY_COLUMN])


def test_score_folds_ratings_and_settles_verdicts(benchmark: Path, tmp_path: Path) -> None:
    behaviours = {"cheap:low": "good", "rich:low": "good", "liar:low": "good"}
    runner = ScriptedRunner(behaviours, thin_keys=_thin_keys(benchmark))
    report = _qualify(benchmark, tmp_path, behaviours, runner=runner)
    report_dir = Path(report["report_dir"])
    first_slug = next(iter(json.loads((report_dir / dq.BLIND_KEY).read_text(encoding="utf-8"))["items"]))

    def rate(name: str, slug: str) -> dict[str, Any]:
        faithful = "unfaithful" if name == "liar:low" and slug == first_slug else "faithful"
        useful = 5 if name == "rich:low" else 4
        return {"faithfulness": faithful, "usefulness": useful, "russian_quality": 4}

    ratings_path = tmp_path / "ratings.json"
    ratings_path.write_text(json.dumps(_ratings_for(report_dir, rate)), encoding="utf-8")
    scored = dq.score_blind_review(report_dir, ratings_path)
    assert scored["blind_review"]["status"] == "complete"
    assert _candidate(scored, "liar:low")["verdict"] == dq.VERDICT_INELIGIBLE
    assert _candidate(scored, "rich:low")["verdict"] == dq.VERDICT_ELIGIBLE
    assert scored["ranking"] == ["rich:low", "cheap:low"]
    written = json.loads((report_dir / dq.REPORT_JSON).read_text(encoding="utf-8"))
    assert written["ranking"] == ["rich:low", "cheap:low"]
    assert "no_unfaithful_blind_rating: pass" in (report_dir / dq.REPORT_MARKDOWN).read_text(encoding="utf-8")


def test_score_keeps_unrated_candidates_pending_and_rejects_bad_ratings(benchmark: Path, tmp_path: Path) -> None:
    report = _qualify(benchmark, tmp_path, {"good:low": "good"})
    report_dir = Path(report["report_dir"])
    partial = dq.score_blind_review(report_dir)  # the untouched template
    assert partial["blind_review"]["status"] == "incomplete"
    assert _candidate(partial, "good:low")["verdict"] == dq.VERDICT_PENDING

    bad = _ratings_for(report_dir, lambda name, slug: {"faithfulness": "great", "usefulness": 4, "russian_quality": 4})
    (report_dir / dq.BLIND_RATINGS).write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(dq.QualificationInputError, match="faithfulness"):
        dq.score_blind_review(report_dir)


# -- The real runner against a fake codex ------------------------------------


FAKE_CODEX = """\
#!{python}
import json, sys, time
args = sys.argv[1:]
if args == ["--version"]:
    print("shim line")
    print("codex-cli 9.9.9")
    sys.exit(0)
out = args[args.index("--output-last-message") + 1]
workdir = args[args.index("-C") + 1]
model = args[args.index("-m") + 1]
effort = args[args.index("-c") + 1].split("=", 1)[1].strip('"')
items = json.load(open(workdir + "/work_items.json", encoding="utf-8"))
sys.stderr.write(f"model: {{model}}\\nreasoning effort: {{effort}}\\n")
if model == "sleepy":
    time.sleep(30)
answer = {{items[0]["identity_key"]: {{"content_description": "ok"}}, "other|key": {{"content_description": "x"}}}}
json.dump(answer, open(out, "w", encoding="utf-8"))
sys.stderr.write("tokens used\\n1,234\\n")
"""


def test_codex_runner_reads_the_answer_profile_and_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    script.write_text(textwrap.dedent(FAKE_CODEX.format(python=sys.executable)), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    runner = dq.CodexSynthesisRunner()
    assert runner.cli_version() == "codex-cli 9.9.9"

    work_item = {"identity_key": "a book|an author", "title": "A Book", "metadata": {"title": "A Book"}}
    call = runner.run(dq.parse_candidate("fake-model:low"), work_item, prompt="PROMPT", timeout_seconds=30)
    assert call.status == dq.STATUS_ANSWERED
    assert call.response == {
        "a book|an author": {"content_description": "ok"},
        "other|key": {"content_description": "x"},
    }
    assert dict(call.effective_profile) == {"model": "fake-model", "reasoning_effort": "low"}
    assert call.tokens_used == 1234

    slow = runner.run(dq.parse_candidate("sleepy:low"), work_item, prompt="PROMPT", timeout_seconds=0.5)
    assert slow.status == dq.STATUS_TIMEOUT
    assert slow.response is None
    assert slow.duration_seconds < 10


# -- Command line ------------------------------------------------------------


def test_cli_reports_input_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["qualify-description-model", "--candidate", "no-effort", "--benchmark-dir", str(tmp_path)])
    assert code == dq.EXIT_INPUT_ERROR
    assert "error=" in capsys.readouterr().out


def test_no_valid_answer_is_never_within_reach_of_the_best(benchmark: Path, tmp_path: Path) -> None:
    report = _qualify(benchmark, tmp_path, {"invalid:low": "invalid"})
    invalid = _candidate(report, "invalid:low")
    assert _criterion(invalid, "validity_within_5_points_of_best")["passed"] is False
    assert invalid["verdict"] == dq.VERDICT_INELIGIBLE


def test_thin_evidence_is_judged_from_the_evidence_not_the_old_outcome(tmp_path: Path) -> None:
    rich = _record("https://shop.example/rich", "en", EVIDENCE, "Rich", "Author")
    thin = _record("https://shop.example/thin", "en", "A short note on a book.", "Thin", "Author")
    history = {key: _history_record(key, "Title", "Author") for key in ("described thin|author", "refused rich|author")}
    described_thin = dq._pool_entry(0, "c1", "", _outcome("described thin|author", "grounded", "", [thin]), history)
    refused_rich = dq._pool_entry(
        1, "c1", "", _outcome("refused rich|author", "attempted_no_description", "invalid_description", [rich]), history
    )
    assert described_thin is not None and described_thin.item_class == dq.CLASS_THIN
    assert refused_rich is not None and refused_rich.item_class == dq.CLASS_SUBSTANTIVE


def test_a_partial_run_stays_ineligible_after_scoring(benchmark: Path, tmp_path: Path) -> None:
    every = [item.slug for item in dq.load_benchmark(benchmark)]
    report = _qualify(benchmark, tmp_path, {"good:low": "good"}, items=every[:-1])
    assert _criterion(_candidate(report, "good:low"), "full_benchmark_run")["passed"] is False
    report_dir = Path(report["report_dir"])
    ratings = _ratings_for(
        report_dir, lambda name, slug: {"faithfulness": "faithful", "usefulness": 5, "russian_quality": 5}
    )
    (report_dir / dq.BLIND_RATINGS).write_text(json.dumps(ratings), encoding="utf-8")
    scored = dq.score_blind_review(report_dir)
    assert scored["blind_review"]["status"] == "complete"
    assert _candidate(scored, "good:low")["verdict"] == dq.VERDICT_INELIGIBLE
    assert scored["ranking"] == []

    fewer = _qualify(benchmark, tmp_path / "fewer", {"good:low": "good"}, repetitions=2)
    assert _criterion(_candidate(fewer, "good:low"), "full_benchmark_run")["passed"] is False


class UnreportedRunner(ScriptedRunner):
    """Reports the requested profile on the first call only."""

    def run(self, pin, work_item, *, prompt, timeout_seconds):
        call = super().run(pin, work_item, prompt=prompt, timeout_seconds=timeout_seconds)
        if len(self.calls) == 1:
            return call
        return dq.SynthesisCall(call.status, call.response, call.duration_seconds, {}, call.tokens_used)


def test_every_call_must_report_the_requested_model(benchmark: Path, tmp_path: Path) -> None:
    behaviours = {"good:low": "good"}
    runner = UnreportedRunner(behaviours, thin_keys=_thin_keys(benchmark))
    report = _qualify(benchmark, tmp_path, behaviours, runner=runner, workers=1)
    criterion = _criterion(_candidate(report, "good:low"), "ran_on_requested_model")
    assert criterion["passed"] is False
    assert criterion["detail"]["unreported_calls"] == 3 * len(dq.load_benchmark(benchmark)) - 1


def test_an_interrupted_comparison_kills_running_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import time

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    script.write_text(textwrap.dedent(FAKE_CODEX.format(python=sys.executable)), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    runner = dq.CodexSynthesisRunner()
    work_item = {"identity_key": "a book|an author", "title": "A Book", "metadata": {"title": "A Book"}}
    results: list[dq.SynthesisCall] = []
    worker = threading.Thread(
        target=lambda: results.append(
            runner.run(dq.parse_candidate("sleepy:low"), work_item, prompt="PROMPT", timeout_seconds=60)
        )
    )
    started = time.monotonic()
    worker.start()
    while not runner._active and time.monotonic() - started < 10:
        time.sleep(0.01)
    runner.terminate_active()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert time.monotonic() - started < 15
    assert results and results[0].status != dq.STATUS_ANSWERED
    # A closed runner starts no new call.
    late = runner.run(dq.parse_candidate("fake-model:low"), work_item, prompt="PROMPT", timeout_seconds=30)
    assert late.status == dq.STATUS_CANCELLED


class InterruptibleRunner(ScriptedRunner):
    terminated = False

    def terminate_active(self) -> None:
        self.terminated = True


def test_run_candidates_terminates_the_runner_on_interrupt(benchmark: Path) -> None:
    items = [dq.load_item(item) for item in dq.load_benchmark(benchmark)]
    runner = InterruptibleRunner({"good:low": "good"}, thin_keys=_thin_keys(benchmark))

    def interrupt(done: int, total: int) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        dq.run_candidates(
            [dq.parse_candidate("good:low")],
            items,
            repetitions=1,
            workers=1,
            timeout_seconds=10,
            runner=runner,
            progress=interrupt,
        )
    assert runner.terminated


@pytest.mark.parametrize("faithfulness,eligible", [("faithful", True), ("minor_issue", False), ("unfaithful", False)])
def test_thin_descriptions_are_judged_by_the_blind_review(
    benchmark: Path, tmp_path: Path, faithfulness: str, eligible: bool
) -> None:
    report = _qualify(benchmark, tmp_path, {"model:low": "padder"})
    report_dir = Path(report["report_dir"])
    ratings = _ratings_for(
        report_dir,
        lambda name, slug: {
            "faithfulness": faithfulness,
            "usefulness": 4,
            "russian_quality": 4,
        },
    )
    (report_dir / dq.BLIND_RATINGS).write_text(json.dumps(ratings), encoding="utf-8")
    scored = dq.score_blind_review(report_dir)
    candidate = _candidate(scored, "model:low")
    summary = candidate["summary"]
    assert summary["honest_decline_rate"] == (1.0 if eligible else 0.0)
    assert summary["padded_thin_calls"] == (0 if eligible else summary["thin_calls"])
    assert summary["faithful_thin_calls"] == (summary["thin_calls"] if eligible else 0)
    assert summary["unreviewed_thin_calls"] == 0
    assert candidate["verdict"] == (dq.VERDICT_ELIGIBLE if eligible else dq.VERDICT_INELIGIBLE)


def test_each_distinct_thin_description_needs_its_own_rating(benchmark: Path, tmp_path: Path) -> None:
    report = _qualify(benchmark, tmp_path, {"model:low": "padder"})
    candidate = report["candidates"][0]
    thin_runs = [run for run in candidate["runs"] if run["item_class"] == dq.CLASS_THIN]
    slug = thin_runs[0]["item"]
    varied = [run for run in thin_runs if run["item"] == slug]
    varied[1]["content_description"] += " Другой текст."
    varied[2]["content_description"] = varied[1]["content_description"]
    loaded = [dq.load_item(item) for item in dq.load_benchmark(benchmark)]
    sheet, key, template = dq.build_blind_review(loaded, ["model:low"], candidate["runs"], "seed-1")
    own_letters = [letter for letter, name in key["items"][slug].items() if name == "model:low"]
    assert len(own_letters) == 2
    assert "Другой текст." in sheet
    report_dir = Path(report["report_dir"])
    (report_dir / dq.REPORT_JSON).write_text(json.dumps(report), encoding="utf-8")
    (report_dir / dq.BLIND_KEY).write_text(json.dumps(key), encoding="utf-8")
    (report_dir / dq.BLIND_RATINGS).write_text(json.dumps(template), encoding="utf-8")
    ratings = _ratings_for(
        report_dir,
        lambda name, slug: {
            "faithfulness": "faithful",
            "usefulness": 4,
            "russian_quality": 4,
        },
    )
    # The first text's rating must not vouch for a different repetition's text.
    different = next(letter for letter in own_letters if "Другой текст." in key["texts"][slug][letter])
    del ratings["items"][slug][different]
    (report_dir / dq.BLIND_RATINGS).write_text(json.dumps(ratings), encoding="utf-8")
    pending = dq.score_blind_review(report_dir)
    assert pending["candidates"][0]["summary"]["unreviewed_thin_calls"] == 2
    assert pending["candidates"][0]["verdict"] == dq.VERDICT_PENDING
    ratings["items"][slug][different] = {
        "faithfulness": "minor_issue",
        "usefulness": 4,
        "russian_quality": 4,
    }
    (report_dir / dq.BLIND_RATINGS).write_text(json.dumps(ratings), encoding="utf-8")
    scored = dq.score_blind_review(report_dir)
    assert scored["candidates"][0]["summary"]["padded_thin_calls"] == 2
    assert scored["candidates"][0]["verdict"] == dq.VERDICT_INELIGIBLE
    # Re-scoring recomputes rather than retaining the previous credit or penalty.
    ratings["items"][slug][different]["faithfulness"] = "faithful"
    (report_dir / dq.BLIND_RATINGS).write_text(json.dumps(ratings), encoding="utf-8")
    assert dq.score_blind_review(report_dir)["candidates"][0]["verdict"] == dq.VERDICT_ELIGIBLE


def _thin_only_candidate(declines: int, described: int, thin_calls: int) -> dict[str, Any]:
    summary = {
        "calls": thin_calls,
        "validity_rate": 1.0,
        "honest_declines": declines,
        "thin_calls": thin_calls,
        "duration_p95_seconds": 5.0,
        "effective_profiles": {"m:low": thin_calls},
        "profile_mismatched_calls": 0,
        "profile_unreported_calls": 0,
    }
    runs = [
        {"item_class": dq.CLASS_THIN, "described": True, "item": "thin", "content_description": f"text {index}"}
        for index in range(described)
    ]
    return {"label": "m:low", "summary": summary, "runs": runs}


def test_an_unreachable_decline_bar_fails_without_waiting_for_ratings() -> None:
    scope = {"items_run": 1, "benchmark_items": 1, "repetitions": 3}
    # 5 declines and 1 unrated description of 10: even a faithful rating gives 6/10.
    hopeless = _thin_only_candidate(declines=5, described=1, thin_calls=10)
    # 8 declines and 2 unrated descriptions of 10: two faithful ratings give 10/10.
    open_case = _thin_only_candidate(declines=8, described=2, thin_calls=10)
    dq.evaluate([hopeless], None, scope)
    dq.evaluate([open_case], None, scope)
    assert _criterion(hopeless, "honest_decline_at_least_90_percent")["passed"] is False
    assert _criterion(hopeless, "honest_decline_at_least_90_percent")["detail"]["best_possible_rate"] == 0.6
    assert hopeless["verdict"] == dq.VERDICT_INELIGIBLE
    assert _criterion(open_case, "honest_decline_at_least_90_percent")["passed"] is None
    assert open_case["verdict"] == dq.VERDICT_PENDING
