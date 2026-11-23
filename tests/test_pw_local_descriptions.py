"""Licensed review text stays in memory; these tests use invented reviews."""

import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from state_store_support import seed_state

from autoscout.candidate_identity import candidate_id_for
from autoscout.pw_local_backfill import stage_archives
from autoscout.pw_local_descriptions import LoopbackModel
from autoscout.pw_local_descriptions import _validated_description
from autoscout.pw_local_descriptions import prepare_descriptions

REVIEW = "Вымышленный обзор книги о том, как жители острова изучали берег и меняли способы наблюдения. " * 3
PAGE = (
    "Nonfiction Reviews\nGeneral Nonfiction\nAn Invented Coast\n"
    "Ada Example. Imaginary Press, $28 hardcover (240p)\n"
    "ISBN 978-0-306-40615-7\n" + REVIEW + "\n(Nov.)"
)
DESCRIPTION = (
    "Книга рассказывает о наблюдениях за береговой линией вымышленного острова и о том, как жители "
    "сопоставляли изменения местности с собственными записями. Автор рассматривает, как способы изучения "
    "берега менялись со временем и что это означало для понимания окружающей среды."
)
ENGLISH_REVIEW = (
    "An entirely invented review describes a nonfiction history of a coastal town. "
    "The author uses municipal archives and interviews to trace changes in trade, housing, and local policy. "
    "Residents' accounts show how these changes affected daily life and the town's relationship with the shore."
)
ENGLISH_DESCRIPTION = (
    "This history traces how trade, housing, and local policy reshaped an invented coastal town. "
    "Drawing on municipal archives and residents' interviews, it connects economic decisions with changes in daily "
    "life and the community's relationship with the shoreline."
)


def test_local_descriptions_store_only_original_text_and_resume(tmp_path: Path):
    archive = tmp_path / "issues.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("issue.pdf", b"invented")

    def read_pdf(_path: Path):
        return "26_11_02_AD Cover.indd", [PAGE]

    staged = tmp_path / "staged.json"
    stage_archives([archive], 2026, staged, read_pdf=read_pdf)
    output = tmp_path / "descriptions.json"

    class Model:
        model = "on-device-test"
        calls = 0

        def describe(self, row, review):
            self.calls += 1
            assert row["isbn13"] == "9780306406157"
            assert REVIEW.strip() in review
            return DESCRIPTION

    model = Model()
    first = prepare_descriptions([archive], staged, output, model, read_pdf=read_pdf)
    second = prepare_descriptions([archive], staged, output, model, read_pdf=read_pdf)
    stored = json.loads(output.read_text())
    assert first["stored"] == second["stored"] == 1
    assert model.calls == 1
    assert stored["descriptions"]["9780306406157"]["content_description"] == DESCRIPTION
    assert REVIEW.strip() not in output.read_text()
    assert "review_sha256" in output.read_text()


def test_loopback_model_never_uses_a_remote_host():
    calls = []

    class Response:
        status = 200

        def read(self, _size):
            return json.dumps(
                {"choices": [{"message": {"content": json.dumps({"content_description": DESCRIPTION})}}]}
            ).encode()

    class Connection:
        def __init__(self, host, port, timeout):
            calls.append((host, port, timeout))

        def request(self, method, path, body, headers):
            assert method == "POST" and path == "/v1/chat/completions"
            message = json.loads(json.loads(body)["messages"][1]["content"])
            assert message["review"] == REVIEW
            assert set(message) == {"review"}

        def getresponse(self):
            return Response()

        def close(self):
            pass

    with patch("autoscout.pw_local_descriptions.http.client.HTTPConnection", Connection):
        assert LoopbackModel(1234, "fixture").describe({"title": "X", "primary_authors": ["A"], "isbn13": "0"}, REVIEW)
    assert calls == [("127.0.0.1", 1234, 180.0)]


def test_loopback_model_requests_json_mode():
    class Response:
        status = 200

        def __init__(self, content):
            self.content = content

        def read(self, _size):
            return json.dumps({"choices": [{"message": {"content": self.content}}]}).encode()

    class Connection:
        def __init__(self, _host, _port, timeout):
            pass

        def request(self, _method, _path, body, headers):
            payload = json.loads(body)
            self.json_mode = payload.get("response_format") == {"type": "json_object"}

        def getresponse(self):
            content = json.dumps({"content_description": DESCRIPTION}) if self.json_mode else DESCRIPTION
            return Response(content)

        def close(self):
            pass

    with patch("autoscout.pw_local_descriptions.http.client.HTTPConnection", Connection):
        assert (
            LoopbackModel(1234, "fixture").describe({"title": "X", "primary_authors": ["A"], "isbn13": "0"}, REVIEW)
            == DESCRIPTION
        )


def test_loopback_model_falls_back_when_structured_output_is_unavailable():
    requests = []

    class Response:
        def __init__(self, status, body):
            self.status = status
            self.body = body

        def read(self, _size):
            return self.body

    class Connection:
        def __init__(self, _host, _port, timeout):
            pass

        def request(self, _method, _path, body, headers):
            requests.append(json.loads(body))

        def getresponse(self):
            if len(requests) == 1:
                return Response(501, b'{"error":{"message":"structured output is unavailable"}}')
            content = json.dumps({"content_description": DESCRIPTION})
            return Response(200, json.dumps({"choices": [{"message": {"content": content}}]}).encode())

        def close(self):
            pass

    with patch("autoscout.pw_local_descriptions.http.client.HTTPConnection", Connection):
        assert (
            LoopbackModel(1234, "fixture").describe({"title": "X", "primary_authors": ["A"], "isbn13": "0"}, REVIEW)
            == DESCRIPTION
        )
    assert len(requests) == 2
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in requests[1]
    assert requests[1]["reasoning_effort"] == "none"
    assert requests[1]["max_tokens"] == 512
    assert requests[1]["temperature"] == 0.7
    assert requests[1]["top_p"] == 0.8
    assert requests[1]["presence_penalty"] == 1.5
    assert requests[1]["seed"] == 0


def test_loopback_model_requests_english_description():
    requests = []

    class Response:
        status = 200

        def read(self, _size):
            content = json.dumps({"english_description": ENGLISH_DESCRIPTION})
            return json.dumps({"choices": [{"message": {"content": content}}]}).encode()

    class Connection:
        def __init__(self, _host, _port, timeout):
            pass

        def request(self, _method, _path, body, headers):
            requests.append(json.loads(body))

        def getresponse(self):
            return Response()

        def close(self):
            pass

    with patch("autoscout.pw_local_descriptions.http.client.HTTPConnection", Connection):
        assert LoopbackModel(1234, "fixture", language="en").describe({}, ENGLISH_REVIEW) == ENGLISH_DESCRIPTION
    assert "English" in requests[0]["messages"][0]["content"]
    assert set(json.loads(requests[0]["messages"][1]["content"])) == {"review"}


def test_local_english_descriptions_are_separate_and_resume(tmp_path: Path):
    archive = tmp_path / "issues.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("issue.pdf", b"invented")

    def read_pdf(_path: Path):
        return "26_11_02_AD Cover.indd", [PAGE.replace(REVIEW, ENGLISH_REVIEW)]

    staged = tmp_path / "staged.json"
    stage_archives([archive], 2026, staged, read_pdf=read_pdf)
    output = tmp_path / "english-descriptions.json"

    class Model:
        model = "on-device-test"
        calls = 0

        def describe(self, _row, review):
            self.calls += 1
            assert ENGLISH_REVIEW in review
            return ENGLISH_DESCRIPTION

    model = Model()
    first = prepare_descriptions([archive], staged, output, model, language="en", read_pdf=read_pdf)
    second = prepare_descriptions([archive], staged, output, model, language="en", read_pdf=read_pdf)
    stored = json.loads(output.read_text())
    assert first["stored"] == second["stored"] == 1
    assert model.calls == 1
    assert stored["language"] == "en"
    assert stored["descriptions"]["9780306406157"]["english_description"] == ENGLISH_DESCRIPTION
    assert "content_description" not in stored["descriptions"]["9780306406157"]
    assert ENGLISH_REVIEW not in output.read_text()


def test_verbatim_span_is_rejected():
    assert _validated_description(DESCRIPTION, REVIEW) == DESCRIPTION
    assert _validated_description(DESCRIPTION + " " + REVIEW, REVIEW) is None
    assert _validated_description(DESCRIPTION + " Автор Боgstа описывает эти перемены.", REVIEW) is None


def test_existing_history_description_avoids_local_model_call(tmp_path: Path):
    archive = tmp_path / "issues.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("issue.pdf", b"invented")

    def read_pdf(_path: Path):
        return "26_11_02_AD Cover.indd", [PAGE]

    staged = tmp_path / "staged.json"
    stage_archives([archive], 2026, staged, read_pdf=read_pdf)
    history = tmp_path / "history.json"
    history.write_text(json.dumps([{"isbn": "9780306406157", "content_description": DESCRIPTION}]))

    class Model:
        def describe(self, _row, _review):
            raise AssertionError("already described book must not reach the model")

    result = prepare_descriptions(
        [archive], staged, tmp_path / "descriptions.json", Model(), history_file=history, read_pdf=read_pdf
    )
    assert result["attempted"] == 0
    assert result["counts"] == {"already_in_history": 1}


@pytest.mark.parametrize("given", ["state-dir", "shard-dir", "old-file-name"])
def test_a_sharded_state_directory_is_read_as_the_history(tmp_path: Path, given: str):
    archive = tmp_path / "issues.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("issue.pdf", b"invented")

    def read_pdf(_path: Path):
        return "26_11_02_AD Cover.indd", [PAGE]

    staged = tmp_path / "staged.json"
    stage_archives([archive], 2026, staged, read_pdf=read_pdf)
    state_dir = tmp_path / "state"
    record = {
        "candidate_id": candidate_id_for("an invented coast|ada example"),
        "identity_key": "an invented coast|ada example",
        "isbn": "9780306406157",
        "content_description": DESCRIPTION,
    }
    seed_state(state_dir, candidate_history=[record])
    history = {
        "state-dir": state_dir,
        "shard-dir": state_dir / "candidate-history",
        "old-file-name": state_dir / "candidate-history.json",
    }[given]

    class Model:
        def describe(self, _row, _review):
            raise AssertionError("already described book must not reach the model")

    result = prepare_descriptions(
        [archive], staged, tmp_path / "descriptions.json", Model(), history_file=history, read_pdf=read_pdf
    )
    assert result["counts"] == {"already_in_history": 1}


def test_existing_history_identity_without_isbn_avoids_local_model_call(tmp_path: Path):
    archive = tmp_path / "issues.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("issue.pdf", b"invented")

    def read_pdf(_path: Path):
        return "26_11_02_AD Cover.indd", [PAGE]

    staged = tmp_path / "staged.json"
    stage_archives([archive], 2026, staged, read_pdf=read_pdf)
    history = tmp_path / "history.json"
    history.write_text(
        json.dumps([{"identity_key": "an invented coast|ada example", "content_description": DESCRIPTION}])
    )

    class Model:
        def describe(self, _row, _review):
            raise AssertionError("already described work must not reach the model")

    result = prepare_descriptions(
        [archive], staged, tmp_path / "descriptions.json", Model(), history_file=history, read_pdf=read_pdf
    )
    assert result["attempted"] == 0
    assert result["counts"] == {"already_in_history": 1}


def test_rejected_english_draft_is_retried_with_feedback(tmp_path: Path):
    archive = tmp_path / "issues.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("issue.pdf", b"invented")

    def read_pdf(_path: Path):
        return "26_11_02_AD Cover.indd", [PAGE.replace(REVIEW, ENGLISH_REVIEW)]

    staged = tmp_path / "staged.json"
    stage_archives([archive], 2026, staged, read_pdf=read_pdf)

    class Model:
        model = "on-device-test"

        def __init__(self, answers):
            self.answers = list(answers)
            self.feedback = []

        def describe(self, _row, _review, feedback=None):
            self.feedback.append(feedback)
            return self.answers.pop(0)

    copied = ENGLISH_DESCRIPTION + " " + ENGLISH_REVIEW
    model = Model(["", copied, ENGLISH_DESCRIPTION])
    result = prepare_descriptions(
        [archive], staged, tmp_path / "accepted.json", model, language="en", read_pdf=read_pdf
    )
    assert result["counts"] == {"retried": 2, "described": 1}
    assert model.feedback[0] is None
    assert "empty" in model.feedback[1]
    assert "reused the review's wording" in model.feedback[2]

    stubborn = Model([copied] * 3)
    result = prepare_descriptions(
        [archive], staged, tmp_path / "rejected.json", stubborn, language="en", read_pdf=read_pdf
    )
    assert result["stored"] == 0
    assert result["counts"] == {"retried": 2, "description_rejected": 1}
    assert not stubborn.answers


def test_draft_flagged_by_self_check_is_rewritten(tmp_path: Path):
    archive = tmp_path / "issues.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("issue.pdf", b"invented")

    def read_pdf(_path: Path):
        return "26_11_02_AD Cover.indd", [PAGE.replace(REVIEW, ENGLISH_REVIEW)]

    staged = tmp_path / "staged.json"
    stage_archives([archive], 2026, staged, read_pdf=read_pdf)
    judged = ENGLISH_DESCRIPTION + " It is a brisk and rewarding read."

    class Model:
        model = "on-device-test"

        def __init__(self):
            self.feedback = []
            self.answers = [judged, ENGLISH_DESCRIPTION]

        def describe(self, _row, _review, feedback=None):
            self.feedback.append(feedback)
            return self.answers.pop(0)

        def flagged_phrases(self, review, draft):
            assert ENGLISH_REVIEW in review
            return ["rewarding read"] if "rewarding read" in draft else []

    model = Model()
    output = tmp_path / "accepted.json"
    result = prepare_descriptions([archive], staged, output, model, language="en", read_pdf=read_pdf)
    assert result["counts"] == {"retried": 1, "described": 1}
    assert '"rewarding read"' in model.feedback[1]
    assert json.loads(output.read_text())["descriptions"]["9780306406157"]["english_description"] == ENGLISH_DESCRIPTION


def test_deepseek_model_sends_key_review_and_correction_over_https():
    from autoscout.pw_local_descriptions import DeepSeekModel

    seen: dict = {}

    class Response:
        status = 200

        def read(self, _limit):
            content = json.dumps({"content_description": DESCRIPTION})
            return json.dumps({"choices": [{"message": {"content": content}}]}).encode()

    class Connection:
        def __init__(self, host, timeout):
            seen["host"] = host

        def request(self, method, path, body, headers):
            seen.update(path=path, headers=headers, payload=json.loads(body))

        def getresponse(self):
            return Response()

        def close(self):
            pass

    model = DeepSeekModel("deepseek-flash", "test-key")
    row = {"title": "An Invented Coast", "primary_authors": ["Ada Example"]}
    with patch("autoscout.pw_local_descriptions.http.client.HTTPSConnection", Connection):
        assert model.describe(row, REVIEW, "too short") == DESCRIPTION
    assert seen["host"] == "api.deepseek.com"
    assert seen["path"] == "/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer test-key"
    assert "Russian" in seen["payload"]["messages"][0]["content"]
    assert json.loads(seen["payload"]["messages"][1]["content"]) == {
        "title": "An Invented Coast",
        "authors": ["Ada Example"],
        "review": REVIEW,
        "correction": "too short",
    }


def test_deepseek_rejected_key_stops_the_run_instead_of_failing_each_book():
    from autoscout.pw_local_descriptions import CloudModelRejectedError
    from autoscout.pw_local_descriptions import DeepSeekModel

    class Response:
        status = 401

        def read(self, _limit):
            return b"{}"

    class Connection:
        def __init__(self, host, timeout):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    model = DeepSeekModel("deepseek-flash", "bad-key", language="en")
    with patch("autoscout.pw_local_descriptions.http.client.HTTPSConnection", Connection):
        with pytest.raises(CloudModelRejectedError):
            model.describe({"title": "T", "primary_authors": ["A B"]}, ENGLISH_REVIEW)


def test_deepseek_provider_needs_explicit_upload_flag(monkeypatch, capsys, tmp_path: Path):
    from autoscout import pw_local_descriptions

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "x",
            "--archive",
            "a.zip",
            "--staged",
            "s.json",
            "--output",
            "data/pw-precollection/o.json",
            "--provider",
            "deepseek",
        ],
    )
    assert pw_local_descriptions.main() == 2
    assert json.loads(capsys.readouterr().out)["reason_code"] == "pw_review_text_upload_not_allowed"


def test_truncated_model_response_is_one_failed_book():
    import http.client

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            raise http.client.BadStatusLine("")

        def close(self):
            pass

    from autoscout.pw_local_descriptions import LocalModelError

    with patch("autoscout.pw_local_descriptions.http.client.HTTPConnection", Connection):
        with pytest.raises(LocalModelError):
            LoopbackModel(1234, "fixture").describe({}, REVIEW)


@pytest.mark.parametrize(
    "reply", [{}, {"verdict": "a rewarding read", "unsupported": []}, {"verdict": [1], "unsupported": []}]
)
def test_malformed_self_check_reply_does_not_approve_the_draft(reply):
    from autoscout.pw_local_descriptions import LocalModelError

    model = LoopbackModel(1234, "fixture", language="en")
    with patch.object(model, "_chat", return_value=reply):
        with pytest.raises(LocalModelError):
            model.flagged_phrases(ENGLISH_REVIEW, "This is a rewarding read.")


def _english_archive(tmp_path: Path):
    archive = tmp_path / "issues.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("issue.pdf", b"invented")

    def read_pdf(_path: Path):
        return "26_11_02_AD Cover.indd", [PAGE.replace(REVIEW, ENGLISH_REVIEW)]

    staged = tmp_path / "staged.json"
    stage_archives([archive], 2026, staged, read_pdf=read_pdf)
    return archive, staged, read_pdf


def test_failed_self_check_stores_nothing_and_counts_one_failed_book(tmp_path: Path):
    from autoscout.pw_local_descriptions import LocalModelError

    archive, staged, read_pdf = _english_archive(tmp_path)

    class Model:
        model = "on-device-test"

        def describe(self, _row, _review, feedback=None):
            return ENGLISH_DESCRIPTION

        def flagged_phrases(self, _review, _draft):
            raise LocalModelError("pw_local_model_invalid_response")

    output = tmp_path / "out.json"
    result = prepare_descriptions([archive], staged, output, Model(), language="en", read_pdf=read_pdf)
    assert result["stored"] == 0
    assert result["counts"] == {"model_failure": 1}
    assert not output.exists()


def _connection_answering(statuses, seen):
    class Response:
        def __init__(self, status):
            self.status = status

        def read(self, _limit):
            content = json.dumps({"content_description": DESCRIPTION})
            return json.dumps({"choices": [{"message": {"content": content}}]}).encode()

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, _method, _path, body, headers):
            seen.append(json.loads(body))

        def getresponse(self):
            return Response(statuses.pop(0))

        def close(self):
            pass

    return Connection


def test_local_server_refusing_json_mode_is_asked_again_without_it():
    seen: list = []
    connection = _connection_answering([400, 200], seen)
    with patch("autoscout.pw_local_descriptions.http.client.HTTPConnection", connection):
        assert LoopbackModel(1234, "fixture").describe({}, REVIEW) == DESCRIPTION
    assert "response_format" in seen[0]
    assert "response_format" not in seen[1]


def test_deepseek_declining_one_review_fails_that_book_only():
    from autoscout.pw_local_descriptions import DeepSeekModel
    from autoscout.pw_local_descriptions import LocalModelError

    seen: list = []
    connection = _connection_answering([400], seen)
    model = DeepSeekModel("deepseek-flash", "test-key")
    with patch("autoscout.pw_local_descriptions.http.client.HTTPSConnection", connection):
        with pytest.raises(LocalModelError):
            model.describe({"title": "T", "primary_authors": ["A B"]}, REVIEW)
    assert len(seen) == 1


def test_english_backfill_description_counts_as_described_in_coverage():
    from autoscout.content_description import PW_LOCAL_ENGLISH_SOURCE
    from autoscout.content_description import description_coverage

    record = {
        "identity_key": "an invented coast|ada example",
        "content_description": ENGLISH_DESCRIPTION,
        "description_provenance": {
            "language": "en",
            "source": PW_LOCAL_ENGLISH_SOURCE,
            "content_description": ENGLISH_DESCRIPTION,
        },
    }
    coverage = description_coverage([record])
    assert coverage["validated_count"] == 1
    assert coverage["fallback_count"] == 0


def test_run_stops_when_the_model_never_answers(tmp_path: Path, monkeypatch):
    from autoscout import pw_local_descriptions
    from autoscout.pw_local_descriptions import LocalModelError
    from autoscout.pw_local_descriptions import ModelNeverAnsweredError

    archive, staged, read_pdf = _english_archive(tmp_path)
    monkeypatch.setattr(pw_local_descriptions, "FAILURES_BEFORE_FIRST_ANSWER", 1)

    class Model:
        model = "misnamed"

        def describe(self, _row, _review, feedback=None):
            raise LocalModelError("pw_local_model_http_failure")

    with pytest.raises(ModelNeverAnsweredError):
        prepare_descriptions([archive], staged, tmp_path / "out.json", Model(), language="en", read_pdf=read_pdf)


def _isbn13(serial: int) -> str:
    digits = f"97803064{serial:04d}"
    check = (10 - sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(digits)) % 10) % 10
    return f"978-0-306-4{serial:04d}-{check}"


def _many_book_archive(tmp_path: Path, books: int):
    words = ["Amber", "Birch", "Cedar", "Dune", "Ember", "Fjord", "Grove", "Heath"]
    page = "Nonfiction Reviews\nGeneral Nonfiction\n" + "\n".join(
        f"An Invented {words[n]} Coast\n{words[n]} Example. Imaginary Press, $28 hardcover (240p)\n"
        f"ISBN {_isbn13(n)}\n{ENGLISH_REVIEW}\n(Nov.)"
        for n in range(books)
    )
    archive = tmp_path / "issues.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("issue.pdf", b"invented")

    def read_pdf(_path: Path):
        return "26_11_02_AD Cover.indd", [page]

    staged = tmp_path / "staged.json"
    stage_archives([archive], 2026, staged, read_pdf=read_pdf)
    assert len(json.loads(staged.read_text())["rows"]) == books
    return archive, staged, read_pdf


def test_outage_after_the_first_answer_costs_only_the_books_it_hits(tmp_path: Path):
    from autoscout.pw_local_descriptions import LocalModelError

    archive, staged, read_pdf = _many_book_archive(tmp_path, 8)

    class Model:
        model = "on-device-test"
        calls = 0

        def describe(self, _row, _review, feedback=None):
            self.calls += 1
            if self.calls == 1:
                return ENGLISH_DESCRIPTION
            raise LocalModelError("pw_local_model_unavailable")

    result = prepare_descriptions([archive], staged, tmp_path / "out.json", Model(), language="en", read_pdf=read_pdf)
    assert result["counts"] == {"described": 1, "model_failure": 7}


def test_model_that_answers_then_fails_the_retry_is_not_called_silent(tmp_path: Path):
    from autoscout.pw_local_descriptions import LocalModelError

    archive, staged, read_pdf = _many_book_archive(tmp_path, 6)

    class Model:
        model = "on-device-test"

        def describe(self, _row, _review, feedback=None):
            if feedback:
                raise LocalModelError("pw_local_model_http_failure")
            return "Too short."

    result = prepare_descriptions([archive], staged, tmp_path / "out.json", Model(), language="en", read_pdf=read_pdf)
    assert result["counts"]["model_failure"] == 6


def test_small_batch_with_a_silent_model_is_not_a_finished_run(tmp_path: Path):
    from autoscout.pw_local_descriptions import LocalModelError
    from autoscout.pw_local_descriptions import ModelNeverAnsweredError

    archive, staged, read_pdf = _many_book_archive(tmp_path, 3)

    class Model:
        model = "misnamed"

        def describe(self, _row, _review, feedback=None):
            raise LocalModelError("pw_local_model_http_failure")

    with pytest.raises(ModelNeverAnsweredError):
        prepare_descriptions(
            [archive], staged, tmp_path / "out.json", Model(), language="en", limit=1, read_pdf=read_pdf
        )


def test_main_reports_a_silent_model_and_exits_2(monkeypatch, capsys, tmp_path: Path):
    from autoscout import pw_local_descriptions
    from autoscout.pw_local_descriptions import ModelNeverAnsweredError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "x",
            "--archive",
            "a.zip",
            "--staged",
            "s.json",
            "--output",
            "data/pw-precollection/o.json",
            "--port",
            "1234",
            "--model",
            "fixture",
        ],
    )
    monkeypatch.setattr(pw_local_descriptions.LoopbackModel, "ready", lambda self: True)

    def never_answered(*_args, **_kwargs):
        raise ModelNeverAnsweredError("pw_model_never_answered")

    monkeypatch.setattr(pw_local_descriptions, "prepare_descriptions", never_answered)
    assert pw_local_descriptions.main() == 2
    assert json.loads(capsys.readouterr().out) == {"status": "failed", "reason_code": "pw_model_never_answered"}
