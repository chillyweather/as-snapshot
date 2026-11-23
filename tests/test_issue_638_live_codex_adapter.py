"""Offline coverage for issue #638: the production Codex adjudication adapter.

The adapter runs ``codex exec`` with the proven flags, including
``--ignore-user-config``, inside an isolated temporary directory, one
candidate per call, under a hard timeout. Every test injects a fake
subprocess seam: no test invokes the real Codex CLI and no test touches
the network. A timeout, a non-zero exit, and an unparsable response are
three distinct bounded per-candidate failures; none of them stops the
command and none of them produces a description.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.__main__ import main
from autoscout.description_adjudication import ADJUDICATION_CALL_TIMEOUT_SECONDS
from autoscout.description_adjudication import MAX_ADJUDICATION_REASON_LENGTH
from autoscout.description_adjudication import CodexAdjudicationExitError
from autoscout.description_adjudication import CodexAdjudicationResponseError
from autoscout.description_adjudication import CodexDescriptionAdjudicationAdapter
from autoscout.description_adjudication import ReplayDescriptionAdjudicationAdapter
from autoscout.description_adjudication import adjudicate_descriptions
from autoscout.description_adjudication import build_adjudication_prompt
from autoscout.grounded_description import BoundedHTTPError
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.json_io import read_json

OPERATION_DATE = "2026-09-14"

IDENTITY_KEY = "work one|alex author"
OTHER_KEY = "work two|alex author"

PREVIEW_NAME = "description-adjudication-preview.json"


class _UnreachableTransport:
    def request(self, url: str, **_kwargs: Any) -> Any:
        raise BoundedHTTPError("offline test never fetches")


def _offline_fetcher() -> BoundedHTTPFetcher:
    return BoundedHTTPFetcher(transport=_UnreachableTransport())  # type: ignore[arg-type]


def _record(candidate_id: str, identity_key: str, title: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "identity_key": identity_key,
        "title": title,
        "primary_authors": ["Alex Author"],
        "content_description": "",
    }


def _valid_payload() -> dict[str, Any]:
    return {
        "resolved": True,
        "unresolved_reason": "",
        "confidence": "high",
        "content_description": "Книга о памяти, обществе и общественной жизни.",
        "citations": [],
    }


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir(exist_ok=True)
    output_dir.mkdir(exist_ok=True)
    seed_state(
        state_dir,
        candidate_history=[
            _record("candidate-001", IDENTITY_KEY, "Work One"),
            _record("candidate-002", OTHER_KEY, "Work Two"),
        ],
    )
    return state_dir, output_dir


def _adapter(
    runner: Any, *, timeout_seconds: float = 240.0, model: str = "", reasoning_effort: str = ""
) -> CodexDescriptionAdjudicationAdapter:
    # Explicit values keep every other test independent of whichever model
    # and reasoning effort the machine running the suite happens to have
    # selected in its Codex config. Resolution is covered on its own below.
    return CodexDescriptionAdjudicationAdapter(
        runner=runner, timeout_seconds=timeout_seconds, model=model, reasoning_effort=reasoning_effort
    )


def _pin_codex_config(monkeypatch: pytest.MonkeyPatch, *, model: str, effort: str) -> None:
    """Set the description pin the adapter reads, so no test depends on the repository's."""
    monkeypatch.setattr("autoscout.description_adjudication._pinned_model_and_effort", lambda: (model, effort))


class _ScriptedRunner:
    """Fake subprocess seam: a scripted sequence of call outcomes.

    Each script entry is one of:

    - an exception instance: raised verbatim (a real ``TimeoutExpired``
      carries the whole command line, paths included, which is the point
      of the leak test);
    - ``("exit", code)``: the call ends with that non-zero exit code and
      writes no response;
    - ``("raw", text)``: exactly that text is written as the response;
    - a dict: written as the JSON response, keyed to the identity key the
      work-item file carries.

    When the script runs out, later calls succeed with a valid payload, so
    one failing candidate never blocks the next one.
    """

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.commands: list[list[str]] = []
        self.work_item_files: list[dict[str, Any]] = []
        self.isolated_dirs: list[str] = []
        self.isolated_listings: list[list[str]] = []
        self.schemas: list[dict[str, Any]] = []
        self.timeouts: list[float | None] = []

    def __call__(self, command: list[str], **kwargs: Any) -> Any:
        self.commands.append(list(command))
        self.timeouts.append(kwargs.get("timeout"))
        isolated = command[command.index("-C") + 1]
        self.isolated_dirs.append(isolated)
        # The isolated directory is removed once the call returns, so the
        # listing and files are snapshotted while the call runs.
        self.isolated_listings.append(sorted(entry.name for entry in Path(isolated).iterdir()))
        self.schemas.append(json.loads((Path(isolated) / "response-schema.json").read_text(encoding="utf-8")))
        work_item_paths = [p for p in Path(isolated).iterdir() if p.name.startswith("work_item")]
        assert len(work_item_paths) == 1, "exactly one input work-item file per call"
        encoded = json.loads(work_item_paths[0].read_text(encoding="utf-8"))
        assert isinstance(encoded, list) and len(encoded) == 1, "one candidate per call"
        self.work_item_files.append(encoded[0])
        response_path = Path(command[command.index("--output-last-message") + 1])
        if self.script:
            outcome = self.script.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            if isinstance(outcome, tuple) and outcome[0] == "exit":
                return subprocess.CompletedProcess(command, int(outcome[1]), stdout="", stderr="")
            if isinstance(outcome, tuple) and outcome[0] == "raw":
                response_path.write_text(str(outcome[1]), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            payload = dict(outcome)
            payload["identity_key"] = self.work_item_files[-1]["identity_key"]
            response_path.write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        payload = dict(_valid_payload())
        payload["identity_key"] = self.work_item_files[-1]["identity_key"]
        response_path.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def _preview_rows(output_dir: Path) -> dict[str, dict[str, Any]]:
    preview = read_json(output_dir / PREVIEW_NAME)
    return {row["identity_key"]: row for row in preview["preview"]}


def _assert_bounded_failure(
    tmp_path: Path, failing_key: str, reason_fragment: str, state_before: dict[str, str]
) -> None:
    output_dir = tmp_path / "run"
    state_dir = tmp_path / "state"
    rows = _preview_rows(output_dir)
    failed = rows[failing_key]
    assert failed["decision"] == "invalid"
    assert failed["response"] == {}
    assert failed["artifact"] == {}
    reason = str(failed["reason"])
    assert reason_fragment.casefold() in reason.casefold()
    assert len(reason) <= MAX_ADJUDICATION_REASON_LENGTH
    # The other candidate still went through: the command did not stop.
    assert rows[OTHER_KEY]["decision"] != "invalid"
    # No absolute local path from this run appears in the artifact.
    assert str(tmp_path) not in (output_dir / PREVIEW_NAME).read_text(encoding="utf-8")
    # The preview never changed Candidate History.
    assert state_snapshot(state_dir) == state_before


# ── Command shape and isolation ──────────────────────────────────────────


def test_adapter_sends_proven_codex_command_shape(tmp_path: Path) -> None:
    runner = _ScriptedRunner()
    adapter = _adapter(runner)

    response = adapter.adjudicate({"identity_key": IDENTITY_KEY, "title": "Work One", "metadata": {}})

    assert response["resolved"] is True
    assert len(runner.commands) == 1
    command = runner.commands[0]
    assert command[:2] == ["codex", "exec"]
    assert command[command.index("-c") + 1] == "tools.web_search=true"
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--skip-git-repo-check" in command
    assert "--ephemeral" in command
    # Without this flag the prototype loaded unrelated user memory from the
    # local Codex home, so the call was neither bounded nor repeatable.
    assert "--ignore-user-config" in command
    assert command[command.index("--output-schema") + 1].endswith("response-schema.json")
    assert command[command.index("--output-last-message") + 1].endswith("response.json")
    assert command.index("-C") < command.index("--output-schema")
    # The prompt is the single trailing argument, not a file path.
    prompt = command[-1]
    assert "work_item.json" in prompt
    assert not prompt.startswith("/")
    assert prompt not in {str(part) for part in command[:-1]}


def test_adapter_runs_the_pinned_description_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter runs the repository's description pin, passed with ``-m``.

    ``--ignore-user-config`` keeps the whole Codex home out: an unattended
    batch must not fire the operator's ``notify`` hook once per candidate,
    and the operator's global default model must not decide the run.
    """
    _pin_codex_config(monkeypatch, model="gpt-5.6-luna", effort="high")
    runner = _ScriptedRunner()

    CodexDescriptionAdjudicationAdapter(runner=runner).adjudicate(
        {"identity_key": IDENTITY_KEY, "title": "Work One", "metadata": {}}
    )

    command = runner.commands[0]
    assert command[command.index("-m") + 1] == "gpt-5.6-luna"
    # The pinned effort comes with the pinned model.
    assert 'model_reasoning_effort="high"' in command
    assert "--ignore-user-config" in command
    # The model and its effort are the whole exception. Nothing else returns.
    assert not [part for part in command if part.startswith("--profile") or part == "-p"]
    assert "tools.web_search=true" in command


def test_adapter_omits_the_model_flags_when_none_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    # An unreadable or empty Codex config must not stop adjudication: the
    # CLI defaults apply and the call still runs.
    _pin_codex_config(monkeypatch, model="   ", effort="")
    runner = _ScriptedRunner()

    response = CodexDescriptionAdjudicationAdapter(runner=runner).adjudicate(
        {"identity_key": IDENTITY_KEY, "title": "Work One", "metadata": {}}
    )

    assert response["resolved"] is True
    command = runner.commands[0]
    assert "-m" not in command
    assert not [part for part in command if part.startswith("model_reasoning_effort")]


@pytest.mark.parametrize("effort", ["", "  ", "high effort", 'high"', "HIGH-ish", "x" * 17, "3"])
def test_a_reasoning_effort_that_is_not_a_bare_word_is_dropped(monkeypatch: pytest.MonkeyPatch, effort: str) -> None:
    # The value is read from a file and goes inside a quoted -c value, so
    # anything that is not a bare word is dropped rather than passed on.
    _pin_codex_config(monkeypatch, model="gpt-5.6-luna", effort=effort)
    runner = _ScriptedRunner()

    CodexDescriptionAdjudicationAdapter(runner=runner).adjudicate(
        {"identity_key": IDENTITY_KEY, "title": "Work One", "metadata": {}}
    )

    assert not [part for part in runner.commands[0] if part.startswith("model_reasoning_effort")]


def test_the_model_choice_is_resolved_once_so_one_run_is_one_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # A run that changed model or effort between candidates would produce
    # descriptions that are not comparable with each other.
    models = iter(["first-model", "second-model", "third-model"])
    efforts = iter(["low", "high", "medium"])
    monkeypatch.setattr(
        "autoscout.description_adjudication._pinned_model_and_effort", lambda: (next(models), next(efforts))
    )
    runner = _ScriptedRunner()
    adapter = CodexDescriptionAdjudicationAdapter(runner=runner)

    adapter.adjudicate({"identity_key": IDENTITY_KEY, "title": "Work One", "metadata": {}})
    adapter.adjudicate({"identity_key": OTHER_KEY, "title": "Work Two", "metadata": {}})

    assert [command[command.index("-m") + 1] for command in runner.commands] == ["first-model", "first-model"]
    for command in runner.commands:
        assert 'model_reasoning_effort="low"' in command


def test_adapter_runs_inside_one_isolated_temporary_directory(tmp_path: Path) -> None:
    runner = _ScriptedRunner()
    adapter = _adapter(runner)

    adapter.adjudicate({"identity_key": IDENTITY_KEY, "title": "Work One", "metadata": {}})

    isolated = Path(runner.isolated_dirs[0])
    temp_root = Path(tempfile.gettempdir()).resolve()
    assert isolated.resolve().is_relative_to(temp_root)
    # The isolation pattern at call time: exactly one input work-item file
    # and one schema file; the response file is the single output the
    # command writes back.
    assert runner.isolated_listings[0] == ["response-schema.json", "work_item.json"]
    schema = runner.schemas[0]
    assert schema["required"] == [
        "identity_key",
        "resolved",
        "unresolved_reason",
        "confidence",
        "content_description",
        "citations",
        # Issue #727: structured output needs every property required, so the
        # machine-facing English is required in the schema and "" means none.
        "english_description",
        # Issue #791: the one closed unresolved verdict, "" for every other answer.
        "unresolved_kind",
    ]
    assert schema["additionalProperties"] is False
    assert runner.timeouts == [ADJUDICATION_CALL_TIMEOUT_SECONDS]


def test_adapter_encodes_one_bounded_work_item_not_raw_page_body(tmp_path: Path) -> None:
    runner = _ScriptedRunner()
    adapter = _adapter(runner)
    raw_body = "RAW PAGE BODY " + "x" * 20000
    work_item = {
        "identity_key": IDENTITY_KEY,
        "title": "Work One",
        "metadata": {
            "short_description": raw_body,
            "page_body": raw_body,
            "primary_authors": ["Alex Author"],
        },
    }

    adapter.adjudicate(work_item)

    encoded = runner.work_item_files[0]
    # The existing bounded work-item encoder ran: the safe metadata field is
    # truncated and the unsafe field never reaches the model at all.
    assert len(encoded["metadata"]["short_description"]) < 1000
    assert raw_body not in json.dumps(encoded)
    assert "page_body" not in encoded["metadata"]
    assert encoded["identity_key"] == IDENTITY_KEY
    assert encoded["metadata"]["primary_authors"] == ["Alex Author"]


def test_adapter_prompt_states_every_adjudication_rule() -> None:
    prompt = build_adjudication_prompt()
    lowered = prompt.casefold()
    # The work item holds exactly one item, and the model must echo its
    # exact identity_key back: the apply gate requires the match.
    assert "exactly one work item" in lowered
    assert "identity_key" in lowered and "echo" in lowered
    # description_requirements are binding, as for the enrichment agent.
    assert "obey" in lowered and "description_requirements" in lowered
    # Printed title and authors exactly as printed on the page.
    assert "exactly as printed" in lowered
    # describes_this_work false with a reason in identity_note.
    assert "describes_this_work" in lowered
    assert "identity_note" in lowered
    # Original Russian description of 2 to 5 sentences with subject, angle,
    # and editorial hook.
    assert "2 to 5 sentences" in lowered
    assert "russian" in lowered
    assert "subject" in lowered and "angle" in lowered and "editorial hook" in lowered
    # No copied publisher or review prose, no invented facts or URLs.
    assert "do not copy" in lowered
    assert "do not invent" in lowered
    # Two pages with different owners; a publisher page and its own retail
    # listings count as one owner.
    assert "different owners" in lowered
    assert "one owner" in lowered
    # resolved false with a concrete unresolved_reason when the evidence is
    # not enough; never guess.
    assert "resolved" in lowered
    assert "unresolved_reason" in lowered
    assert "never guess" in lowered
    # Grounded search hints are starting points only, never evidence (#684).
    assert "search_hints" in lowered
    assert "starting point" in lowered
    assert "not evidence" in lowered
    assert "two-owner" in lowered


def test_adapter_validates_response_against_schema_before_returning() -> None:
    runner = _ScriptedRunner({**_valid_payload(), "resolved": "yes"})
    adapter = _adapter(runner)

    with pytest.raises(CodexAdjudicationResponseError):
        adapter.adjudicate({"identity_key": IDENTITY_KEY, "title": "Work One", "metadata": {}})


def test_adapter_validates_unparsable_response_before_returning() -> None:
    runner = _ScriptedRunner(("raw", "<html>not json at all</html>"))
    adapter = _adapter(runner)

    with pytest.raises(CodexAdjudicationResponseError):
        adapter.adjudicate({"identity_key": IDENTITY_KEY, "title": "Work One", "metadata": {}})


def test_adapter_rejects_a_wrong_identity_key_echo_before_returning() -> None:
    # The live call the reviewer ran first answered with an empty
    # identity_key before it read the file. The adapter must catch that at
    # the source, as its own bounded failure, not let it surface much
    # later as an evidence-ambiguity fallback.
    empty_key = ("raw", json.dumps({**_valid_payload(), "identity_key": ""}))
    with pytest.raises(CodexAdjudicationResponseError):
        _adapter(_ScriptedRunner(empty_key)).adjudicate(
            {"identity_key": IDENTITY_KEY, "title": "Work One", "metadata": {}}
        )

    # A paraphrased or wrong key is the same formatting slip.
    wrong_key = ("raw", json.dumps({**_valid_payload(), "identity_key": OTHER_KEY}))
    with pytest.raises(CodexAdjudicationResponseError):
        _adapter(_ScriptedRunner(wrong_key)).adjudicate(
            {"identity_key": IDENTITY_KEY, "title": "Work One", "metadata": {}}
        )


def _echo_runner(transform: Any) -> Any:
    """A runner that echoes back the sent key, changed by ``transform``."""

    def run(command: list[str], **_kwargs: Any) -> Any:
        isolated = Path(command[command.index("-C") + 1])
        sent = json.loads((isolated / "work_item.json").read_text(encoding="utf-8"))[0]
        payload = {**_valid_payload(), "identity_key": transform(sent["identity_key"])}
        Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    return run


@pytest.mark.parametrize(
    ("identity_key", "transform"),
    [
        # The encoder bounds identity_key to 200 characters. A longer key is
        # truncated on the way out, so a perfect echo of what the model
        # received still is not the key the command decides identity with.
        pytest.param(("l" * 190) + "|" + ("a" * 40), lambda key: key, id="over-the-encoder-cap"),
        # A padded echo must not pass the adapter and fail the command.
        pytest.param(IDENTITY_KEY, lambda key: f"  {key}  ", id="padded-echo"),
    ],
)
def test_an_echo_the_command_would_reject_fails_in_the_adapter_instead(
    tmp_path: Path, identity_key: str, transform: Any
) -> None:
    """The adapter and the command must apply one identity predicate.

    When the two disagree, the response passes the adapter and fails the
    command, which reports evidence ambiguity: the wrong cause, with no
    detail, after a full call's cost.
    """
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir()
    output_dir.mkdir()
    seed_state(state_dir, candidate_history=[_record("candidate-001", identity_key, "Work One")])

    adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=_adapter(_echo_runner(transform)),
        fetcher=_offline_fetcher(),
    )

    row = read_json(output_dir / PREVIEW_NAME)["preview"][0]
    assert row["decision"] == "invalid"
    assert row["decision_reason"] != "ambiguity"
    assert "identity echo mismatch" in str(row["reason"])
    assert row["artifact"] == {}


def test_each_response_failure_names_itself_for_an_operator(tmp_path: Path) -> None:
    # ``unparsable`` and a wrong echo are both response failures. They need
    # separate bounded reasons, or an operator cannot tell a model that
    # returned garbage from one that returned a good answer for the wrong
    # book. Both details are fixed text from this module.
    wrong_echo = json.dumps({**_valid_payload(), "identity_key": OTHER_KEY})
    invalid_schema = json.dumps({**_valid_payload(), "identity_key": IDENTITY_KEY, "resolved": "yes"})
    scenarios = (
        ("unparsable", "<html>", "unparsable response"),
        ("echo", wrong_echo, "identity echo mismatch"),
        ("schema", invalid_schema, "schema invalid"),
    )
    for name, response_text, expected_reason in scenarios:
        workspace = tmp_path / name
        workspace.mkdir()
        _run_failure_scenario(workspace, _ScriptedRunner(("raw", response_text)), expected_reason)


# ── Three distinct bounded per-candidate failures ────────────────────────


def _run_failure_scenario(tmp_path: Path, runner: _ScriptedRunner, reason_fragment: str) -> None:
    adapter = _adapter(runner)
    state_dir, output_dir = _workspace(tmp_path)
    state_before = state_snapshot(state_dir)

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=adapter,
        fetcher=_offline_fetcher(),
        max_workers=1,
    )

    assert result["status"] == "ok"
    assert result["rejected"] == 1
    assert result["valid"] == 1
    _assert_bounded_failure(tmp_path, IDENTITY_KEY, reason_fragment, state_before)


def test_timeout_is_a_bounded_failure_that_does_not_stop_the_command(tmp_path: Path) -> None:
    # A timed-out call is retried once at the end of the stage (issue #865),
    # so the failing candidate times out on its first call and on the retry;
    # the other candidate's call in between succeeds.
    _run_failure_scenario(
        tmp_path,
        _ScriptedRunner(
            subprocess.TimeoutExpired(cmd=["codex"], timeout=120.0),
            _valid_payload(),
            subprocess.TimeoutExpired(cmd=["codex"], timeout=120.0),
        ),
        "timeout",
    )


def test_nonzero_exit_is_a_bounded_failure_that_does_not_stop_the_command(tmp_path: Path) -> None:
    _run_failure_scenario(tmp_path, _ScriptedRunner(("exit", 1)), "exit")
    # The integer exit code is retained, so a model failure is diagnosable.
    _run_failure_scenario(tmp_path, _ScriptedRunner(("exit", 7)), "exit code 7")


def test_missing_cli_spawn_failure_is_distinct_from_an_exit_code_and_leaks_no_path(
    tmp_path: Path,
) -> None:
    class _LeakyFileError(FileNotFoundError):
        def __str__(self) -> str:
            return f"[Errno 2] No such file or directory: '{tmp_path}/codex'"

    _run_failure_scenario(tmp_path, _ScriptedRunner(_LeakyFileError()), "spawn failed")
    rows = _preview_rows(tmp_path / "run")
    assert str(tmp_path) not in str(rows[IDENTITY_KEY]["reason"])


def test_unparsable_response_is_a_bounded_failure_that_does_not_stop_the_command(tmp_path: Path) -> None:
    _run_failure_scenario(tmp_path, _ScriptedRunner(("raw", "<html>not json at all</html>")), "response")


def test_every_call_carries_a_hard_timeout() -> None:
    runner = _ScriptedRunner(("exit", 1))
    adapter = _adapter(runner, timeout_seconds=7)

    with pytest.raises(CodexAdjudicationExitError):
        adapter.adjudicate({"identity_key": IDENTITY_KEY, "title": "Work One", "metadata": {}})
    assert runner.timeouts == [7.0]


def test_failure_artifacts_never_carry_paths_or_credentials(tmp_path: Path) -> None:
    # A real TimeoutExpired carries the whole command line: absolute
    # temporary paths and anything else that was on it. The adapter must
    # never let that text reach an artifact or a log.
    leaky = subprocess.TimeoutExpired(
        cmd=["codex", "exec", "--ignore-user-config", "AKIAIOSFODNN7EXAMPLE", str(tmp_path / "x")],
        timeout=120.0,
    )
    runner = _ScriptedRunner(leaky)
    adapter = _adapter(runner)
    state_dir, output_dir = _workspace(tmp_path)

    result = adjudicate_descriptions(
        state_dir, output_dir, effective_date=OPERATION_DATE, adapter=adapter, fetcher=_offline_fetcher()
    )

    assert result["status"] == "ok"
    preview_text = (output_dir / PREVIEW_NAME).read_text(encoding="utf-8")
    # The result dict names the caller's own requested preview path, which
    # is intended CLI output; everything else must be clean.
    result_without_explicit_path = {key: value for key, value in result.items() if key != "preview_file"}
    for artifact in (preview_text, json.dumps(result_without_explicit_path)):
        assert str(tmp_path) not in artifact
        assert "AKIAIOSFODNN7EXAMPLE" not in artifact


# ── The fixture run stays on the replay adapter ──────────────────────────


def test_default_command_path_uses_replay_adapter_and_never_spawns_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the offline fixture path must never spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    state_dir, output_dir = _workspace(tmp_path)

    assert (
        main(
            [
                "adjudicate-descriptions",
                "--state-dir",
                str(state_dir),
                "--output-dir",
                str(output_dir),
                "--run-date",
                OPERATION_DATE,
            ]
        )
        == 0
    )
    # The replay adapter answered every candidate itself, unresolved.
    preview = read_json(output_dir / PREVIEW_NAME)
    assert preview["counts"]["previewed"] == 2
    assert all(row["response"]["resolved"] is False for row in preview["preview"])


def test_replay_adapter_still_answers_without_the_production_adapter(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)

    result = adjudicate_descriptions(state_dir, output_dir, effective_date=OPERATION_DATE)

    # adapter=None is the documented default: the deterministic replay.
    assert result["status"] == "ok"
    assert ReplayDescriptionAdjudicationAdapter().adjudicate({"identity_key": "k"})["resolved"] is False
    assert read_json(output_dir / PREVIEW_NAME)["counts"]["previewed"] == 2


# ── Command wiring ───────────────────────────────────────────────────────


def test_cli_codex_adapter_flag_selects_the_production_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--adapter codex reaches the production adapter; the default never does."""
    spawned: list[list[str]] = []

    def _fake_run(command: list[str], **_kwargs: Any) -> Any:
        spawned.append(list(command))
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    monkeypatch.setattr("autoscout.description_adjudication._run_codex_subprocess", _fake_run)
    # The command builds the adapter itself, which resolves the selected
    # model and effort. Pin them, so the suite never reads the Codex config
    # of whichever machine is running it.
    _pin_codex_config(monkeypatch, model="", effort="")
    state_dir, output_dir = _workspace(tmp_path)

    # Explicit opt-in: the Codex adapter runs, fails closed, and the
    # command still completes with bounded per-candidate failures.
    assert (
        main(
            [
                "adjudicate-descriptions",
                "--state-dir",
                str(state_dir),
                "--output-dir",
                str(output_dir),
                "--run-date",
                OPERATION_DATE,
                "--adapter",
                "codex",
            ]
        )
        == 0
    )
    assert len(spawned) == 2
    assert "--ignore-user-config" in spawned[0]
    rows = _preview_rows(output_dir)
    assert all(row["decision"] == "invalid" for row in rows.values())

    # The default stays on the replay adapter: nothing is spawned.
    spawned.clear()
    replay_output = tmp_path / "run-replay"
    replay_output.mkdir()
    assert (
        main(
            [
                "adjudicate-descriptions",
                "--state-dir",
                str(state_dir),
                "--output-dir",
                str(replay_output),
                "--run-date",
                OPERATION_DATE,
            ]
        )
        == 0
    )
    assert spawned == []


def test_unknown_adapter_kind_is_a_command_input_error(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)

    with pytest.raises(ValueError, match="unknown adapter kind"):
        adjudicate_descriptions(state_dir, output_dir, effective_date=OPERATION_DATE, adapter_kind="bogus")
