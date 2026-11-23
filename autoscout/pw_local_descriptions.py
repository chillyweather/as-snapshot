"""Write original PW-based descriptions from the local PDF archive.

By default review prose exists only in this process and in the request to a
model on 127.0.0.1. The DeepSeek provider sends review prose to DeepSeek's
API and runs only behind an explicit command-line opt-in.
The output contains original Russian descriptions, bibliographic identity,
and hashes of the source reviews; it contains no PDF or review text.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import tempfile
import zipfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from autoscout.content_description import record_has_usable_description
from autoscout.content_description import validate_content_description
from autoscout.content_description import validate_english_description
from autoscout.description_depth import meets_description_depth
from autoscout.identity import normalized_identity_key
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.pw_issue_capture import validate_collection
from autoscout.pw_issue_parser import parse_pages
from autoscout.pw_local_backfill import _issue_date
from autoscout.pw_local_backfill import _read_pdf
from autoscout.state_store import Collection
from autoscout.state_store import open_state_store
from autoscout.state_store.layout import CANDIDATE_HISTORY_DIR

SCHEMA_VERSION = 1
MAX_REVIEW_CHARS = 10_000
MAX_RESPONSE_BYTES = 16_384
PROMPT = (
    "Напишите оригинальное русскоязычное описание взрослой нон-фикшн книги для редактора. "
    "Единственный источник сведений о содержании книги — рецензия в сообщении пользователя. "
    "Укажите предмет книги, ее конкретный ракурс или тезис, а также подход и охват, если они подтверждены рецензией. "
    "Напишите 2–3 связных нейтральных предложения общей длиной 200–450 знаков. "
    "Пишите естественным русским языком. Первое предложение начните сразу с предмета или тезиса книги. "
    "Не начинайте со слов «Книга», «Эта книга» или «Автор». Не называйте автора и не используйте местоимения с неясной отсылкой. "
    "Не смешивайте латиницу и кириллицу внутри слова. "
    "Не называйте другие книги и фильмы, упомянутые в рецензии: опишите сам тип сравнения или изменения. "
    "Не цитируйте и не переводите предложения рецензии, не упоминайте рецензента или Publishers Weekly, "
    "не повторяйте название книги и не добавляйте сведения извне. "
    'Верните только JSON: {"content_description":"..."}.'
)
PROMPT_EN = (
    "Write an original English description of the nonfiction book reviewed in the user message, "
    "in 2-3 sentences totalling 250-500 characters. "
    "State the book's subject, its central question or argument, and its approach or scope, "
    "using only what the review supports. "
    "Open with the subject itself, not with the author's job title and not with 'This book'. "
    "Describe the book, not the review: no praise, criticism, or verdict on its quality or readability, "
    "no speculation, and no outside facts. "
    "Use your own wording and sentence structure; never reuse a run of more than five consecutive words "
    "from the review. "
    'Return only JSON with an "english_description" string.'
)
CHECK_PROMPT_EN = (
    "You check a draft book description against the review it was written from. "
    "Report two things. verdict: phrases in the draft that judge the book's quality, style, readability or "
    "shortcomings (praise or criticism), rather than say what the book covers. "
    "unsupported: statements in the draft that the review does not support, including a wrong book title. "
    "Quote phrases exactly as they appear in the draft. "
    'Return only JSON: {"verdict": ["..."], "unsupported": ["..."]}. Use empty lists when the draft is clean.'
)
MAX_ATTEMPTS = 3
ENGLISH_MAX_CHARS = 650
_RETRY_FEEDBACK = {
    "empty": "Your previous answer was empty. The review has enough detail: write the description.",
    "invalid": "Your previous answer was not plain prose in the required language. Write it again.",
    "too_short": "Your previous answer was too short. Write 2-3 full sentences, at least 250 characters.",
    "too_long": "Your previous answer was too long. Write at most 3 sentences and 500 characters.",
    "mixed_script": "Your previous answer mixed alphabets inside one word. Write it again.",
}
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)


class LocalModelError(Exception):
    """Bounded failure code; raw model output and review text are never logged."""


class CloudModelRejectedError(ValueError):
    """The cloud provider refused the request itself; retrying other books cannot help."""


class ModelNeverAnsweredError(ValueError):
    """The run's first books all failed at the model, so the setup is wrong, not one review."""


FAILURES_BEFORE_FIRST_ANSWER = 5


DEEPSEEK_HOST = "api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-flash"
DEEPSEEK_KEY_ENV = "DEEPSEEK_API_KEY"
_CLOUD_LANGUAGES = {"ru": ("Russian", "content_description"), "en": ("English", "english_description")}
CLOUD_PROMPT = (
    "You help an editor at a Russian publishing house who scouts foreign nonfiction for translation rights. "
    "The editor needs to know what each book is about, and the only source for this book is one "
    "Publishers Weekly review, supplied in the user message with the book's title and authors.\n\n"
    "Write a description of the book in {language}: two or three sentences, 250-500 characters, covering its "
    "subject, its central question or argument, and its approach or scope. Everything you state must be "
    "supported by the review; use nothing you know about the book or author from elsewhere.\n\n"
    "The review is licensed text and a critic's opinion, and the editor wants neither. Write the description "
    "in your own words and sentence structure rather than translating or paraphrasing the review sentence by "
    "sentence, and leave out the reviewer's praise, criticism and verdict, along with any mention of the "
    "review or Publishers Weekly. Do not repeat the book's title.{language_note}\n\n"
    "Return only JSON with a single string field named {field}.\n\n"
    "If the user message carries a correction, an automated check rejected your previous draft for that "
    "reason; write a new description that resolves it."
)
_CLOUD_RUSSIAN_NOTE = (
    " Write natural editorial Russian. Give personal and place names in their established Russian form, "
    "transliterating where none exists, so the text stays in Cyrillic."
)


class LoopbackModel:
    """OpenAI-compatible chat completions on a fixed loopback host, no proxy."""

    def __init__(self, port: int, model: str, *, timeout: float = 180.0, language: str = "ru") -> None:
        if not 1 <= port <= 65535 or not model.strip() or language not in {"ru", "en"}:
            raise ValueError("pw_local_model_config_invalid")
        self.port = port
        self.model = model.strip()
        self.timeout = timeout
        self.language = language

    def ready(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=3):
                return True
        except OSError:
            return False

    _path = "/v1/chat/completions"
    _rejected_statuses: frozenset[int] = frozenset()
    _json_mode_refusals: frozenset[int] = frozenset({400, 422})
    _max_response_bytes = MAX_RESPONSE_BYTES

    def _connect(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def _chat(self, system: str, request: dict[str, Any]) -> dict[str, Any]:
        """Send one JSON-answer chat request and return the decoded answer object."""
        payload = {
            "model": self.model,
            "temperature": 0,
            "stream": False,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ],
        }

        def exchange(request_payload: dict[str, Any]) -> tuple[int, bytes]:
            connection = self._connect()
            try:
                connection.request(
                    "POST",
                    self._path,
                    body=json.dumps(request_payload).encode("utf-8"),
                    headers=self._headers(),
                )
                response = connection.getresponse()
                raw = response.read(self._max_response_bytes + 1)
                if len(raw) > self._max_response_bytes:
                    raise LocalModelError("pw_local_model_response_too_large")
                return response.status, raw
            finally:
                connection.close()

        try:
            status, raw = exchange(payload)
            if status == 501:
                try:
                    message = json.loads(raw).get("error", {}).get("message")
                except (ValueError, TypeError, AttributeError):
                    message = None
                if message == "structured output is unavailable":
                    fallback = {key: value for key, value in payload.items() if key != "response_format"}
                    fallback.update(
                        {
                            "reasoning_effort": "none",
                            "max_tokens": 512,
                            "temperature": 0.7,
                            "top_p": 0.8,
                            "presence_penalty": 1.5,
                            "seed": 0,
                        }
                    )
                    status, raw = exchange(fallback)
            elif status in self._json_mode_refusals:
                # Some local servers answer 400 or 422 to JSON mode instead of 501.
                status, raw = exchange({key: value for key, value in payload.items() if key != "response_format"})
            if status in self._rejected_statuses:
                raise CloudModelRejectedError("pw_cloud_model_request_rejected")
            if status != 200:
                raise LocalModelError("pw_local_model_http_failure")
            content = json.loads(raw)["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError
            # Without JSON mode a model may wrap the object in a code fence.
            answer = json.loads(content[content.index("{") : content.rindex("}") + 1])
            if not isinstance(answer, dict):
                raise ValueError
            return answer
        except CloudModelRejectedError:
            raise
        except (TimeoutError, OSError, http.client.HTTPException):
            raise LocalModelError("pw_local_model_unavailable") from None
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            raise LocalModelError("pw_local_model_invalid_response") from None

    def describe(self, row: dict[str, Any], review: str, feedback: str | None = None) -> str:
        """Draft one description; *feedback* says why the previous draft was rejected."""
        request = {"review": review}
        if feedback:
            request["correction"] = feedback
        answer = self._chat(PROMPT_EN if self.language == "en" else PROMPT, request)
        value = answer.get("english_description" if self.language == "en" else "content_description")
        if not isinstance(value, str):
            raise LocalModelError("pw_local_model_invalid_response")
        return value

    def flagged_phrases(self, review: str, draft: str) -> list[str]:
        """Phrases of *draft* the model itself calls a verdict or unsupported.

        The model often lists review wording instead of draft wording, so only
        phrases that occur in the draft count.
        """
        answer = self._chat(CHECK_PROMPT_EN, {"review": review, "draft": draft})
        lowered = draft.casefold()
        candidates: list[str] = []
        for key in ("verdict", "unsupported"):
            phrases = answer.get(key)
            # A reply without both lists is not an approval of the draft.
            if not isinstance(phrases, list) or not all(isinstance(phrase, str) for phrase in phrases):
                raise LocalModelError("pw_local_model_invalid_response")
            candidates.extend(phrases)
        return [phrase for phrase in candidates if len(phrase.strip()) >= 4 and phrase.strip().casefold() in lowered]


class DeepSeekModel(LoopbackModel):
    """DeepSeek's hosted chat completions. Sends review prose off this machine."""

    _path = "/chat/completions"
    # Bad key, no access, no balance, unknown model: no other book would fare better.
    # A 400 or 422 can be the provider declining one review, so it fails that book only.
    _rejected_statuses = frozenset({401, 402, 403, 404})
    _json_mode_refusals = frozenset()
    # The response carries the model's reasoning next to the short answer.
    _max_response_bytes = 262_144

    def __init__(self, model: str, api_key: str, *, timeout: float = 180.0, language: str = "ru") -> None:
        if not model.strip() or not api_key.strip() or language not in _CLOUD_LANGUAGES:
            raise ValueError("pw_cloud_model_config_invalid")
        self.model = model.strip()
        self.timeout = timeout
        self.language = language
        self._api_key = api_key.strip()

    def _connect(self) -> http.client.HTTPConnection:
        return http.client.HTTPSConnection(DEEPSEEK_HOST, timeout=self.timeout)

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"}

    def describe(self, row: dict[str, Any], review: str, feedback: str | None = None) -> str:
        language_name, field = _CLOUD_LANGUAGES[self.language]
        request = {"title": row["title"], "authors": row["primary_authors"], "review": review}
        if feedback:
            request["correction"] = feedback
        system = CLOUD_PROMPT.format(
            language=language_name,
            field=field,
            language_note=_CLOUD_RUSSIAN_NOTE if self.language == "ru" else "",
        )
        value = self._chat(system, request).get(field)
        if not isinstance(value, str):
            raise LocalModelError("pw_local_model_invalid_response")
        return value


def _review_digest(review: str) -> str:
    return hashlib.sha256(review.encode("utf-8")).hexdigest()


def _copied_spans(description: str, review: str) -> list[str]:
    """Eight-word runs of *description* that appear verbatim in *review*."""
    source = [word.casefold() for word in _WORDS.findall(review)]
    answer = _WORDS.findall(description)
    spans = {tuple(source[i : i + 8]) for i in range(max(0, len(source) - 7))}
    return [
        " ".join(answer[i : i + 8])
        for i in range(max(0, len(answer) - 7))
        if tuple(word.casefold() for word in answer[i : i + 8]) in spans
    ]


def _copies_review(description: str, review: str) -> bool:
    """Reject any eight-word verbatim span, including a translated answer's English aside."""
    return bool(_copied_spans(description, review))


def _has_mixed_script_word(description: str) -> bool:
    return any(
        any("A" <= char <= "Z" or "a" <= char <= "z" for char in word)
        and any("А" <= char <= "я" or char in "Ёё" for char in word)
        for word in _WORDS.findall(description)
    )


def _assess_description(value: str, review: str, *, language: str = "ru") -> tuple[str | None, str | None]:
    """Return ``(description, None)`` or ``(None, retry feedback for the model)``."""
    if not value.strip():
        return None, _RETRY_FEEDBACK["empty"]
    description, _reason = (
        validate_english_description(value) if language == "en" else validate_content_description(value)
    )
    if description is None:
        return None, _RETRY_FEEDBACK["invalid"]
    if not meets_description_depth(description):
        return None, _RETRY_FEEDBACK["too_short"]
    if language == "en" and len(description) > ENGLISH_MAX_CHARS:
        return None, _RETRY_FEEDBACK["too_long"]
    if _has_mixed_script_word(description):
        return None, _RETRY_FEEDBACK["mixed_script"]
    copied = _copied_spans(description, review)
    if copied:
        # The phrases come from the model's own draft and go back only to the loopback model.
        return None, (
            "Your previous answer reused the review's wording. Rewrite it with a different sentence "
            "structure and without these phrases: " + "; ".join(f'"{span}"' for span in copied[:6])
        )
    return description, None


def _validated_description(value: str, review: str, *, language: str = "ru") -> str | None:
    return _assess_description(value, review, language=language)[0]


def _read_history(history_file: Path | None) -> Any:
    """Candidate History for ``--history-file``: a state directory, its shard directory, or a single history file.

    A state directory, or a history file name in a state directory that keeps
    Candidate History one file per book (#1073), is read through its State
    Store, so the flag keeps working after the conversion.
    """
    if history_file is None:
        return []
    state_dir: Path | None = None
    if history_file.is_dir():
        state_dir = history_file.parent if history_file.name == CANDIDATE_HISTORY_DIR else history_file
    elif not history_file.exists() and (history_file.parent / CANDIDATE_HISTORY_DIR).is_dir():
        state_dir = history_file.parent
    if state_dir is None:
        return read_json(history_file, [])
    return open_state_store(state_dir).read(Collection.CANDIDATE_HISTORY)


def prepare_descriptions(
    archives: list[Path],
    staged_file: Path,
    output_file: Path,
    model: Any,
    *,
    limit: int | None = None,
    history_file: Path | None = None,
    excluded_subsections: frozenset[str] = frozenset(),
    language: str = "ru",
    read_pdf: Callable[[Path], tuple[str, list[str]]] = _read_pdf,
) -> dict[str, Any]:
    """Resume local inference from zipped PDFs, saving only accepted summaries.

    The staged bibliographic result is the allowlist: an ISBN, issue date,
    title and authors must match it exactly before review text is sent.
    """
    staged = read_json(staged_file)
    if not isinstance(staged, dict) or staged.get("schema_version") != 1:
        raise ValueError("pw_backfill_state_invalid")
    if limit is not None and limit < 1:
        raise ValueError("pw_description_limit_invalid")
    if language not in {"ru", "en"}:
        raise ValueError("pw_description_language_invalid")
    allowed = {row["isbn13"]: row for row in validate_collection(staged)}
    history = _read_history(history_file)
    if not isinstance(history, list):
        raise ValueError("pw_description_history_invalid")
    described_history = [
        record for record in history if isinstance(record, dict) and record_has_usable_description(record)
    ]
    covered_isbns = {record["isbn"] for record in described_history if isinstance(record.get("isbn"), str)}
    covered_identities = {
        str(record.get("identity_key") or normalized_identity_key(record)) for record in described_history
    }
    issue_dates = {issue["issue_date"] for issue in staged["issues"]}
    existing = read_json(output_file, None)
    if existing is None:
        existing = {"schema_version": SCHEMA_VERSION, "year": staged["year"], "descriptions": {}}
        if language == "en":
            existing["language"] = "en"
    if (
        not isinstance(existing, dict)
        or existing.get("schema_version") != SCHEMA_VERSION
        or existing.get("year") != staged["year"]
        or existing.get("language", "ru") != language
        or not isinstance(existing.get("descriptions"), dict)
    ):
        raise ValueError("pw_description_state_invalid")
    descriptions = existing["descriptions"]
    counts: Counter[str] = Counter()
    attempted = 0
    answered = False
    stop = False
    for archive in archives:
        if stop:
            break
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                if stop:
                    break
                if member.is_dir() or not member.filename.lower().endswith(".pdf") or member.file_size > 500_000_000:
                    continue
                with tempfile.TemporaryDirectory(prefix="autoscout-pw-description-") as folder:
                    local = Path(folder) / "issue.pdf"
                    with bundle.open(member) as source, local.open("wb") as target:
                        shutil.copyfileobj(source, target)
                    title, pages = read_pdf(local)
                issue = _issue_date(title, pages)
                if issue is None or issue.isoformat() not in issue_dates:
                    continue
                reviews: list[tuple[dict[str, Any], str]] = []

                def append_review(
                    row: dict[str, Any], text: str, sink: list[tuple[dict[str, Any], str]] = reviews
                ) -> None:
                    sink.append((row, text))

                parsed = parse_pages(pages, issue.isoformat(), _on_review=append_review)
                if parsed.unreadable_section_pages:
                    counts["unreadable_issue"] += 1
                    continue
                for row, review in reviews:
                    known = allowed.get(row["isbn13"])
                    if not known or any(known[key] != row[key] for key in ("title", "primary_authors", "issue_date")):
                        counts["not_in_staged_result"] += 1
                        continue
                    if row["subsection"] in excluded_subsections:
                        counts["excluded_subsection"] += 1
                        continue
                    if row["isbn13"] in covered_isbns or normalized_identity_key(row) in covered_identities:
                        counts["already_in_history"] += 1
                        continue
                    if row["isbn13"] in descriptions:
                        counts["already_described"] += 1
                        continue
                    if len(review) < 80 or len(review) > MAX_REVIEW_CHARS:
                        counts["review_length_invalid"] += 1
                        continue
                    if limit is not None and attempted >= limit:
                        stop = True
                        break
                    attempted += 1
                    description = feedback = None
                    try:
                        for _attempt in range(MAX_ATTEMPTS):
                            # A rejected draft goes back once with the reason; temperature 0
                            # would otherwise repeat the same answer.
                            answer = model.describe(row, review, feedback) if feedback else model.describe(row, review)
                            answered = True
                            description, feedback = _assess_description(answer, review, language=language)
                            check = getattr(model, "flagged_phrases", None)
                            if description is not None and language == "en" and check is not None:
                                flagged = check(review, description)
                                if flagged:
                                    description, feedback = (
                                        None,
                                        (
                                            "Your previous answer judged the book or said things the review does not "
                                            "support. Rewrite it without: " + "; ".join(f'"{p}"' for p in flagged[:6])
                                        ),
                                    )
                            if description is not None:
                                break
                            counts["retried"] += 1
                    except LocalModelError:
                        counts["model_failure"] += 1
                        # A wrong model name or request shape fails every book; an outage
                        # after the first answer is transient and the run rides it out.
                        if not answered and counts["model_failure"] >= FAILURES_BEFORE_FIRST_ANSWER:
                            raise ModelNeverAnsweredError("pw_model_never_answered") from None
                        continue
                    if description is None:
                        counts["retried"] -= 1
                        counts["description_rejected"] += 1
                        continue
                    descriptions[row["isbn13"]] = {
                        "isbn13": row["isbn13"],
                        "title": row["title"],
                        "primary_authors": row["primary_authors"],
                        "issue_date": row["issue_date"],
                        "review_sha256": _review_digest(review),
                        "english_description" if language == "en" else "content_description": description,
                        "model": str(getattr(model, "model", "local")),
                    }
                    output_file.parent.mkdir(parents=True, exist_ok=True)
                    write_json(output_file, existing)
                    counts["described"] += 1
    if attempted and not answered:
        # A small trial batch must not report a model that never answered as a finished run.
        raise ModelNeverAnsweredError("pw_model_never_answered")
    return {"attempted": attempted, "stored": len(descriptions), "counts": dict(sorted(counts.items()))}


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Describe local PW reviews with a local model or, on opt-in, DeepSeek")
    parser.add_argument("--archive", type=Path, action="append", required=True)
    parser.add_argument("--staged", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider", choices=("local", "deepseek"), default="local")
    parser.add_argument(
        "--allow-review-text-upload",
        action="store_true",
        help="required with --provider deepseek: PW review text is sent to the DeepSeek API",
    )
    parser.add_argument("--port", type=int)
    parser.add_argument("--model")
    parser.add_argument("--language", choices=("ru", "en"), default="ru")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--history-file", type=Path)
    parser.add_argument("--exclude-subsection", action="append", default=[])
    args = parser.parse_args()
    try:
        if not args.output.resolve().is_relative_to((Path.cwd() / "data/pw-precollection").resolve()):
            raise ValueError("pw_description_output_outside_ignored_directory")
        model: Any
        if args.provider == "deepseek":
            if not args.allow_review_text_upload:
                print(json.dumps({"status": "failed", "reason_code": "pw_review_text_upload_not_allowed"}))
                return 2
            api_key = os.environ.get(DEEPSEEK_KEY_ENV, "")
            if not api_key.strip():
                print(json.dumps({"status": "failed", "reason_code": "pw_cloud_model_key_missing"}))
                return 2
            model = DeepSeekModel(args.model or DEEPSEEK_MODEL, api_key, language=args.language)
        else:
            if args.port is None or not args.model:
                raise ValueError("pw_local_model_config_invalid")
            model = LoopbackModel(args.port, args.model, language=args.language)
            if not model.ready():
                print(json.dumps({"status": "failed", "reason_code": "pw_local_model_unavailable"}))
                return 2
        summary = prepare_descriptions(
            args.archive,
            args.staged,
            args.output,
            model,
            limit=args.limit,
            history_file=args.history_file,
            excluded_subsections=frozenset(args.exclude_subsection),
            language=args.language,
        )
    except (CloudModelRejectedError, ModelNeverAnsweredError) as error:
        print(json.dumps({"status": "failed", "reason_code": str(error)}))
        return 2
    except (OSError, ValueError, zipfile.BadZipFile):
        print(json.dumps({"status": "failed", "reason_code": "pw_local_description_failed"}))
        return 2
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
