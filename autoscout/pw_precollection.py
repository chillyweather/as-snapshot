"""One bounded PW mailbox pass; counts and bibliographic fields only (#954)."""

from __future__ import annotations

import imaplib
import json
import re
import threading
import time
from collections import Counter
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from email import message_from_bytes
from email import policy
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.pm_mailbox import MailboxOpener
from autoscout.pm_mailbox import MessageHeader
from autoscout.pm_mailbox import PmMailboxError
from autoscout.pm_mailbox import PmSource
from autoscout.pm_mailbox import load_credential
from autoscout.pm_mailbox import load_source
from autoscout.pm_mailbox import open_imap_mailbox
from autoscout.progress_snapshot import ProgressSnapshotFailureRecorder
from autoscout.progress_snapshot import ProgressSnapshotPresentation
from autoscout.progress_snapshot import ProgressSnapshotWriter
from autoscout.pw_issue_fetch import HttpClient
from autoscout.pw_issue_fetch import PwError
from autoscout.pw_issue_fetch import SubscriberClient
from autoscout.pw_issue_fetch import fetch_issue
from autoscout.pw_issue_fetch import issue_link
from autoscout.pw_issue_fetch import pdf_page_texts
from autoscout.pw_issue_parser import parse_pages
from autoscout.runtime_budget import PW_PASS_RUNTIME_BUDGET_SECONDS
from autoscout.unexpected_error import exception_facts
from autoscout.unexpected_error import keep_exception


def _subject_date(subject: str, pattern: str) -> str:
    match = re.fullmatch(pattern, subject)
    if not match:
        return ""
    try:
        return datetime.strptime(match["date"], "%B %d, %Y").date().isoformat()
    except (ValueError, IndexError):
        return ""


def _forwarding_sender(
    declaration: dict[str, Any], environ: Mapping[str, str] | None, key_file: Path | None
) -> str | None:
    """The optional extra forwarding sender, read like the mailbox user; None when it is not set.

    The editor's own address is not kept in the tracked config: the config names the
    environment variable (or key-file entry) that holds it. A value without ``@`` is
    rejected as a bad declaration, and the value itself is never reported.
    """
    name = declaration.get("forwarding_sender_env")
    if name is None:
        return None
    if not isinstance(name, str) or not name.strip():
        raise ValueError("forwarding_sender_env is not a variable name")
    value = (load_credential(name.strip(), environ, key_file) or "").strip().casefold()
    if not value:
        return None
    if "@" not in value:
        raise ValueError("the forwarding sender is not an address")
    return value


def _message_body(raw: bytes, original_sender: str) -> str:
    message = message_from_bytes(raw, policy=policy.default)
    parts = []
    for part in message.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                parts.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
    body = "\n".join(parts)
    # The outer sender may be the editor. The original PW sender must still be
    # present in the forwarded body; directly delivered mail has it in From.
    if parseaddr(str(message.get("From", "")))[1].casefold() != original_sender:
        if not re.search(r"(?:From|De|От)\s*:[^\n]*" + re.escape(original_sender), body, re.I):
            raise PwError("pw_original_sender_missing")
    return body


# Retrying cannot change these: the issue is recorded once and not fetched again.
# Removing its entry from skipped_issues in the result file retries it. An over-cap
# PDF is retried by itself once the declared cap rises above the one it failed under.
PERMANENT_ISSUE_FAILURES = frozenset(
    {
        "pw_pdf_too_large",
        "pw_issue_date_mismatch",
        "pw_issue_link_missing_or_ambiguous",
        "pw_original_sender_missing",
        "pw_page_unreadable",
    }
)
# Issue links are read by this rule. A skip recorded under an older rule (no link_rule)
# is retried once by itself: rule 2 ignores links to issues already collected (#1117).
LINK_RULE = 2
# BookLife sends its own "Your <date> Digital Edition Has Arrived" from PW's address;
# only the From display name tells it apart, and it never links to the PW issue.
IGNORED_SENDER_NAMES = frozenset({"booklife"})
# Older issues run to 273 MB (12 Oct 2026); the declaration may not go beyond this.
MAX_PDF_BYTES_LIMIT = 500_000_000
# Skips recorded before the cap was stored with them failed under this one.
FIRST_PDF_CAP = 150_000_000
# A one-time backfill reads the whole PW label.
BACKFILL_SINCE = date(2000, 1, 1)
RETRY_EMAIL_SLACK_DAYS = 14
# Why a pass stopped before every pending issue: its time allowance, or PW refusing the subscriber
# link. A refused link refuses every issue the same way, and each new attempt opens one more PW
# device session, so the pass stops at the first refusal (the account allows only a few).
STOP_TIME_BUDGET = "pw_time_budget"
STOP_LINK_REJECTED = "pw_subscriber_link_rejected"
REASON_IN_PROGRESS = "pw_pass_in_progress"
REASON_HISTORICAL_RUN = "pw_historical_run_date"
REASON_UNEXPECTED = "unexpected_error"
WEEKLY_STAGE_NAME = "pw-precollection"
PW_CONFIG_FILENAME = "pw-issue-pdf.json"


def lock_path(source: PmSource) -> Path:
    return source.attempt_file.with_suffix(".lock")


@contextmanager
def _pass_lock(source: PmSource) -> Iterator[None]:
    """Hold the PW pass lock for the whole pass, or raise ``pw_pass_in_progress`` at once.

    Two overlapping passes would read the same result, download the same issues
    and each write its own result over the other's. The lock is an advisory
    ``flock`` that the operating system releases when the process ends.
    """
    import fcntl

    path = lock_path(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PwError(REASON_IN_PROGRESS) from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _pending_issues(
    headers: list[MessageHeader], pattern: str, done: set[str], retrying: list[str]
) -> tuple[list[tuple[str, int]], int]:
    """Uncollected issue dates, each with its newest email, and the count of emails not chosen.

    New issues come oldest first; issues that failed in an earlier pass follow in
    queue order, the longest-waiting first. An issue that fails again moves to the
    back of the queue, so neither new issues nor other retries wait behind it.
    """
    newest: dict[str, int] = {}
    for header in headers:
        if header.sender_name.casefold() in IGNORED_SENDER_NAMES:
            continue
        issue_date = _subject_date(header.subject, pattern)
        if issue_date and issue_date not in done:
            newest[issue_date] = max(newest.get(issue_date, 0), header.uid)
    queue = {day: position for position, day in enumerate(retrying)}
    order = sorted(newest.items(), key=lambda item: (item[0] in queue, queue.get(item[0], 0), item[0]))
    return order, len(headers) - len(newest)


def _unqueue(queue: list[str], issue_date: str) -> None:
    if issue_date in queue:
        queue.remove(issue_date)


def run_pass(
    config_path: Path,
    *,
    backfill: bool = False,
    now: datetime | None = None,
    opener: MailboxOpener = open_imap_mailbox,
    client: HttpClient | None = None,
    text_reader: Callable[[Path], list[str]] = pdf_page_texts,
    environ: Mapping[str, str] | None = None,
    key_file: Path | None = None,
    allowance: Callable[[], float] | None = None,
    on_issue: Callable[[bool], None] | None = None,
) -> dict[str, Any]:
    """Collect every uncollected issue, oldest first and bounded per pass; save after each one.

    *allowance* returns the seconds left: once it is spent the pass starts no
    further issue, and the issues not started stay pending for the next pass.
    *on_issue* is told when an issue starts (True) and ends (False), for the
    weekly heartbeat. Only one pass runs at a time.
    """
    try:
        source = load_source(config_path, family="pw-issue-pdf")
        declaration = read_json(config_path)
        pattern = source.subjects["issue"]
        re.compile(pattern)
        max_bytes = declaration["max_pdf_bytes"]
        max_issues = declaration["max_issues_per_pass"]
        sections = declaration["sections"]
        original_sender = declaration["original_sender"].casefold()
        forwarding = _forwarding_sender(declaration, environ, key_file)
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or not 0 < max_bytes <= MAX_PDF_BYTES_LIMIT
            or not isinstance(max_issues, int)
            or isinstance(max_issues, bool)
            or not 0 < max_issues <= 10
            or sections != ["Nonfiction Reviews"]
            or original_sender not in source.senders
        ):
            raise ValueError
    except (PmMailboxError, OSError, KeyError, ValueError, TypeError, AttributeError, re.error):
        raise PwError("pw_declaration_invalid") from None
    if forwarding and forwarding not in source.senders:
        source = replace(source, senders=(*source.senders, forwarding))
    with _pass_lock(source):
        return _locked_pass(
            source,
            pattern=pattern,
            max_bytes=max_bytes,
            max_issues=max_issues,
            sections=sections,
            original_sender=original_sender,
            backfill=backfill,
            now=now,
            opener=opener,
            client=client,
            text_reader=text_reader,
            environ=environ,
            key_file=key_file,
            allowance=allowance,
            on_issue=on_issue,
        )


def _locked_pass(
    source: PmSource,
    *,
    pattern: str,
    max_bytes: int,
    max_issues: int,
    sections: list[str],
    original_sender: str,
    backfill: bool,
    now: datetime | None,
    opener: MailboxOpener,
    client: HttpClient | None,
    text_reader: Callable[[Path], list[str]],
    environ: Mapping[str, str] | None,
    key_file: Path | None,
    allowance: Callable[[], float] | None,
    on_issue: Callable[[bool], None] | None,
) -> dict[str, Any]:
    moment = now or datetime.now(UTC)
    summary: dict[str, Any] = {
        "status": "started",
        "emails_read": 0,
        "emails_skipped": 0,
        "issue_date": "",
        "issues_collected": [],
        "issues_failed": [],
        "issues_pending": 0,
        "stop_reason": "",
        "pages_read": 0,
        "nonfiction_rows": 0,
        "rejections": {},
        "excluded_paid_self_published": 0,
    }

    def attempt() -> None:
        source.attempt_file.parent.mkdir(parents=True, exist_ok=True)
        write_json(source.attempt_file, {"attempted_at": moment.isoformat(), "backfill": backfill, **summary})

    try:
        attempt()
        user = load_credential(source.user_env, environ, key_file)
        password = load_credential(source.password_env, environ, key_file)
        if not user or not password:
            raise PwError("pw_credentials_missing")
        previous = read_json(source.collection_file, {"schema_version": 1, "rows": [], "issues": []})
        if not isinstance(previous, dict) or previous.get("schema_version") != 1:
            raise PwError("pw_result_invalid")
        rows = {row["isbn13"]: row for row in previous["rows"]}
        issues = list(previous["issues"])
        # A queue: the issue waiting longest comes first.
        retrying = list(dict.fromkeys(previous.get("retry_issues", [])))
        # Issues an earlier pass found but never attempted (its cap, budget or a refused
        # link stopped it). Unlike retries they keep their place among new issues.
        waiting = [day for day in dict.fromkeys(previous.get("untried_issues", [])) if isinstance(day, str)]
        skipped = []
        for entry in previous.get("skipped_issues", []):
            if entry["reason_code"] == "pw_pdf_too_large" and entry.get("max_pdf_bytes", FIRST_PDF_CAP) < max_bytes:
                # The cap rose: try it again, even outside the window.
                _unqueue(retrying, entry["issue_date"])
                retrying.append(entry["issue_date"])
            elif entry["reason_code"] == "pw_issue_link_missing_or_ambiguous" and entry.get("link_rule", 1) < LINK_RULE:
                # The link rule changed: try it again, even outside the window.
                _unqueue(retrying, entry["issue_date"])
                retrying.append(entry["issue_date"])
            else:
                skipped.append(entry)
        done = {issue["issue_date"] for issue in issues} | {issue["issue_date"] for issue in skipped}
        since = BACKFILL_SINCE if backfill else moment.date() - timedelta(days=source.max_message_age_days)
        # An issue waiting for a retry or never attempted stays reachable after it ages
        # out of the window; its email can arrive a little before or after the cover date.
        reachable = {*retrying, *waiting}
        search_since = min(
            [since, *(date.fromisoformat(day) - timedelta(days=RETRY_EMAIL_SLACK_DAYS) for day in reachable)]
        )
        mailbox = opener(source, user, password)
        try:
            headers = [
                h
                for h in mailbox.headers_since(search_since, source.senders)
                if h.sender.casefold() in source.senders
                and h.date is not None
                and h.date.date() <= moment.date()
                and (since <= h.date.date() or _subject_date(h.subject, pattern) in reachable)
            ]
            pending, summary["emails_skipped"] = _pending_issues(headers, pattern, done, retrying)
            selected = pending[:max_issues]
            summary["issues_pending"] = len(pending) - len(selected)
            # Issues this pass does not attempt, saved so a later pass still finds their emails.
            untried = [issue_date for issue_date, _uid in pending[len(selected) :]]
            # Read the chosen emails before closing; the downloads need no mailbox.
            # An email that cannot be read fails its issue alone.
            raws: list[tuple[str, bytes | str]] = []
            for issue_date, uid in selected:
                try:
                    raws.append((issue_date, mailbox.fetch(uid)))
                    summary["emails_read"] += 1
                except PmMailboxError as exc:
                    raws.append((issue_date, exc.reason_code.replace("pm_", "pw_", 1)))
                except TimeoutError:
                    raws.append((issue_date, "pw_timeout"))
                except (OSError, imaplib.IMAP4.error):
                    raws.append((issue_date, "pw_connection_failed"))
        finally:
            mailbox.close()

        def save() -> None:
            source.collection_file.parent.mkdir(parents=True, exist_ok=True)
            write_json(
                source.collection_file,
                {
                    "schema_version": 1,
                    "rows": sorted(rows.values(), key=lambda r: r["isbn13"]),
                    "issues": issues,
                    "skipped_issues": skipped,
                    "retry_issues": retrying,
                    "untried_issues": waiting,
                },
            )

        rejections: Counter[str] = Counter()
        for position, (issue_date, raw) in enumerate(raws):
            if not summary["stop_reason"] and allowance is not None and allowance() <= 0:
                summary["stop_reason"] = STOP_TIME_BUDGET
            if summary["stop_reason"]:
                # Not attempted: still pending, so the next pass takes it.
                summary["issues_pending"] += len(raws) - position
                untried[:0] = [day for day, _raw in raws[position:]]
                break
            if on_issue is not None:
                on_issue(True)
            _unqueue(waiting, issue_date)
            try:
                if isinstance(raw, str):
                    raise PwError(raw)
                body = _message_body(raw, original_sender)
                link, issue_id = issue_link(body, frozenset(str(issue["issue_id"]) for issue in issues))
                pages = fetch_issue(
                    client or SubscriberClient(source.timeout_seconds),
                    link,
                    issue_date,
                    max_bytes,
                    text_reader=text_reader,
                )
                summary["pages_read"] += len(pages)
                parsed = parse_pages(pages, issue_date, sections=tuple(sections))
                # Counted before the empty check, so a failed attempt still says why.
                summary["excluded_paid_self_published"] += parsed.excluded_paid_self_published
                rejections.update(parsed.rejections)
                if parsed.unreadable_section_pages:
                    raise PwError("pw_page_unreadable")
                if not parsed.rows:
                    raise PwError("pw_zero_nonfiction_rows")
            except PwError as exc:
                if exc.reason_code == "pw_pdf_reader_missing":
                    raise  # every issue would fail the same way
                summary["issues_failed"].append({"issue_date": issue_date, "reason_code": exc.reason_code})
                if exc.reason_code in PERMANENT_ISSUE_FAILURES:
                    skip: dict[str, Any] = {"issue_date": issue_date, "reason_code": exc.reason_code}
                    if exc.reason_code == "pw_pdf_too_large":
                        skip["max_pdf_bytes"] = max_bytes
                    if exc.reason_code == "pw_issue_link_missing_or_ambiguous":
                        skip["link_rule"] = LINK_RULE
                    skipped.append(skip)
                    _unqueue(retrying, issue_date)
                else:
                    _unqueue(retrying, issue_date)
                    retrying.append(issue_date)
                if exc.reason_code == STOP_LINK_REJECTED:
                    summary["stop_reason"] = STOP_LINK_REJECTED
                save()
                if on_issue is not None:
                    on_issue(False)
                continue
            # Each issue is saved before the next download, so a later failure keeps it.
            for row in parsed.rows:
                # A backfilled or retried older issue never replaces a newer review of the same book.
                if row["isbn13"] not in rows or rows[row["isbn13"]]["issue_date"] <= row["issue_date"]:
                    rows[row["isbn13"]] = row
            issues.append({"issue_date": issue_date, "issue_id": issue_id})
            _unqueue(retrying, issue_date)
            save()
            summary["issues_collected"].append(issue_date)
            summary["issue_date"] = issue_date
            summary["nonfiction_rows"] += len(parsed.rows)
            if on_issue is not None:
                on_issue(False)
        summary["rejections"] = dict(sorted(rejections.items()))
        if waiting != untried:
            waiting[:] = untried
            save()
        if summary["issues_failed"] and not summary["issues_collected"]:
            # A refused subscriber link names the pass: it is the one failure the editor must act on.
            if summary["stop_reason"] == STOP_LINK_REJECTED:
                raise PwError(STOP_LINK_REJECTED)
            raise PwError(summary["issues_failed"][0]["reason_code"])
        if not source.collection_file.exists():
            save()
        summary["status"] = (
            "partial"
            if summary["issues_failed"] or summary["stop_reason"]
            else "collected"
            if summary["issues_collected"]
            else "no_new_issue"
        )
    except PmMailboxError as exc:
        summary.update(status="failed", reason_code=exc.reason_code.replace("pm_", "pw_", 1))
        attempt()
        raise PwError(summary["reason_code"]) from None
    except (OSError, imaplib.IMAP4.error):
        summary.update(status="failed", reason_code="pw_collection_failed")
        attempt()
        raise PwError("pw_collection_failed") from None
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        # The public reason stays; the cause stays for the caller to report its frames and kind (#1158).
        summary.update(status="failed", reason_code="pw_collection_failed", **exception_facts(exc))
        attempt()
        raise PwError("pw_collection_failed") from exc
    except PwError as exc:
        summary.update(status="failed", reason_code=exc.reason_code)
        attempt()
        raise
    except Exception as exc:
        # Whatever broke, the attempt record names it instead of staying "started"; the cause
        # stays on the error for the caller to report its traceback (#1158).
        summary.update(status="failed", reason_code=REASON_UNEXPECTED, **exception_facts(exc))
        attempt()
        raise PwError(REASON_UNEXPECTED) from exc
    attempt()
    return summary


def collect_pw_issue_command(config_path: Path, *, backfill: bool = False) -> int:
    try:
        report = run_pass(config_path, backfill=backfill)
    except (PwError, OSError) as exc:
        report = {
            "status": "failed",
            "reason_code": exc.reason_code if isinstance(exc, PwError) else "pw_storage_failed",
        }
        if exc.__cause__ is not None:
            keep_exception(exc.__cause__, WEEKLY_STAGE_NAME, omit_message=True)
        if report["reason_code"] == "pw_credentials_missing":
            report["next_action"] = "Set the PM mailbox environment credentials or ~/.config/autoscout/pm-mailbox.env."
        print(json.dumps(report, indent=2))
        return 2
    print(json.dumps(report, indent=2))
    return 0


def pw_config_path(weekly_config: Path) -> Path:
    """The PW declaration of the weekly config *weekly_config*."""
    return weekly_config.parent / PW_CONFIG_FILENAME


def weekly_outcome(report: Mapping[str, Any]) -> str:
    """Name what a pass that finished found: a week with no new issue is not a failure."""
    return "new_issue" if report.get("issues_collected") else "no_new_issue"


# How often the stage refreshes its heartbeat while one issue downloads or parses.
PROGRESS_REFRESH_SECONDS = 5.0


def weekly_stage(
    weekly_config: Path,
    output_dir: Path,
    *,
    presentation: ProgressSnapshotPresentation | None = None,
    failure_recorder: ProgressSnapshotFailureRecorder | None = None,
    opener: MailboxOpener = open_imap_mailbox,
    client: HttpClient | None = None,
    text_reader: Callable[[Path], list[str]] = pdf_page_texts,
    environ: Mapping[str, str] | None = None,
    key_file: Path | None = None,
    now: datetime | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    budget_seconds: float = PW_PASS_RUNTIME_BUDGET_SECONDS,
    write_snapshot: bool = True,
    run_date: date | None = None,
) -> dict[str, Any]:
    """Run one PW pass as a stage of the weekly run; never raises.

    A weekly config with no PW declaration beside it is skipped as
    ``not_configured``. A week with no new issue email opens no subscriber
    link, so it uses no PW device session. Every failure is a named
    ``not_collected`` report for PW alone. Once *budget_seconds* are spent the
    pass starts no further issue; the stage waits for an issue already started,
    because abandoning it would leave its PDF behind. Heartbeat rows carry the
    source name only: no title, sender or subject.
    """
    config = pw_config_path(weekly_config)
    if not config.is_file():
        return {"status": "not_configured"}
    today = (now or datetime.now(UTC)).astimezone(UTC).date()
    if run_date is not None and run_date < today - timedelta(days=1):
        # A pass reads today's mail; its result would be newer than this run's date.
        return {"status": "not_run", "reason_code": REASON_HISTORICAL_RUN}
    started = monotonic()
    writer = ProgressSnapshotWriter(
        output_dir,
        monotonic=monotonic,
        failure_recorder=failure_recorder,
        presentation=presentation,
        write_snapshot=write_snapshot,
    )
    state = {"done": 0}
    guard = threading.Lock()

    def publish(*, running: bool) -> None:
        with guard:
            rows: list[dict[str, Any]] = [{"source_name": "PW", "state": "finished"} for _ in range(state["done"])]
            if running:
                # The pass is at work (mailbox, download or parsing) for as long as it runs; the
                # heartbeat counts finished issues only, so this row never inflates the count.
                rows.append({"source_name": "PW", "state": "in_flight"})
            writer.publish(
                stage=WEEKLY_STAGE_NAME,
                status="running" if running else "finished",
                elapsed_seconds=monotonic() - started,
                rows=rows,
                stage_budget_seconds=budget_seconds,
            )

    def on_issue(starting: bool) -> None:
        if not starting:
            with guard:
                state["done"] += 1
        publish(running=True)

    answers: list[dict[str, Any]] = []

    def work() -> None:
        report: dict[str, Any]
        try:
            report = run_pass(
                config,
                now=now,
                opener=opener,
                client=client,
                text_reader=text_reader,
                environ=environ,
                key_file=key_file,
                allowance=lambda: budget_seconds - (monotonic() - started),
                on_issue=on_issue,
            )
            report["outcome"] = weekly_outcome(report)
        except PwError as exc:
            report = {"status": "not_collected", "reason_code": exc.reason_code}
            if exc.__cause__ is not None:
                report.update(keep_exception(exc.__cause__, WEEKLY_STAGE_NAME, omit_message=True))
        except Exception as exc:
            report = {
                "status": "not_collected",
                "reason_code": REASON_UNEXPECTED,
                **keep_exception(exc, WEEKLY_STAGE_NAME, omit_message=True),
            }
        answers.append(report)

    publish(running=True)
    worker = threading.Thread(target=work, name="pw-pass", daemon=True)
    worker.start()
    while worker.is_alive():
        worker.join(PROGRESS_REFRESH_SECONDS)
        if worker.is_alive():
            publish(running=True)
    publish(running=False)
    report: dict[str, Any] = answers[0] if answers else {"status": "not_collected", "reason_code": REASON_UNEXPECTED}
    report["duration_seconds"] = round(monotonic() - started, 3)
    return report


def weekly_source_problem(report: Mapping[str, Any], source_name: str) -> dict[str, str] | None:
    """The weekly completion's named PW problem for the stage report *report*, if any."""
    status = report.get("status")
    code = str(report.get("reason_code") or report.get("stop_reason") or "")
    detail = {"editor_detail": REFUSED_LINK_EDITOR_DETAIL} if code == STOP_LINK_REJECTED else {}
    if status == "not_collected":
        return {
            "source_name": source_name,
            "status": "failed",
            "reason": f"PW issue pass failed ({code or 'unknown'}); capture uses the previous result when available",
            **detail,
        }
    pending = report.get("issues_pending")
    if status == "partial" or (isinstance(pending, int) and pending > 0):
        # Issues beyond the per-pass cap are not in the result's failure queues, so capture cannot name them.
        return {
            "source_name": source_name,
            "status": "incomplete",
            "reason": f"PW issue pass collected only part of the pending issues "
            f"({code or ('issues left for the next pass: ' + str(pending) if pending else 'an issue failed')}); "
            "the next pass takes the rest",
            **detail,
        }
    return None


# PW refused the subscriber link: the editor's one plain action (#956).
REFUSED_LINK_EDITOR_DETAIL = (
    "подписка не открыла выпуск — откройте цифровую библиотеку PW по ссылке из письма "
    "и подтвердите аккаунт, если PW об этом просит"
)


def configured_source_name(weekly_config: Path) -> str:
    """The declared PW source name, or a generic one when the declaration cannot be read."""
    try:
        return load_source(pw_config_path(weekly_config), family="pw-issue-pdf").name
    except PmMailboxError:
        return "Publishers Weekly"


def last_pass_status(weekly_config: Path) -> dict[str, Any] | None:
    """Read-only: the last PW attempt and result, or None when PW is not configured.

    Reads the attempt record and the result file only; it takes no lock and
    opens no mailbox, so it can never refuse or disturb a running pass.
    """
    config = pw_config_path(weekly_config)
    if not config.is_file():
        return None
    try:
        source = load_source(config, family="pw-issue-pdf")
    except PmMailboxError as exc:
        return {"configured": False, "reason_code": exc.reason_code}
    status: dict[str, Any] = {"configured": True, "source_name": source.name}

    def readable(path: Path) -> Any:
        # A damaged file is a PW fact to report, never a reason for the whole diagnosis to fail.
        if not path.is_file():
            return None
        try:
            return read_json(path, None)
        except (OSError, ValueError):
            status.setdefault("unreadable", []).append(path.name)
            return None

    attempt = readable(source.attempt_file)
    if attempt is not None:
        try:
            failed = attempt.get("issues_failed", [])
            collected = attempt.get("issues_collected", [])
            if not isinstance(failed, list) or not all(isinstance(item, dict) for item in failed):
                raise TypeError("issues_failed")
            if not isinstance(collected, list):
                raise TypeError("issues_collected")
            status["attempt"] = {
                "status": str(attempt.get("status", "")),
                "reason_code": str(attempt.get("reason_code", "")),
                "stop_reason": str(attempt.get("stop_reason", "")),
                "attempted_at": str(attempt.get("attempted_at", "")),
                "issues_collected": [str(day) for day in collected],
                "issues_failed": [dict(item) for item in failed],
                "nonfiction_rows": int(attempt.get("nonfiction_rows", 0)),
                "issues_pending": int(attempt.get("issues_pending", 0)),
            }
        except (AttributeError, TypeError, ValueError):
            status.setdefault("unreadable", []).append(source.attempt_file.name)
    result = readable(source.collection_file)
    if result is not None:
        try:
            issues, rows, retry = result["issues"], result["rows"], result.get("retry_issues", [])
            if not isinstance(issues, list) or not isinstance(rows, list) or not isinstance(retry, list):
                raise TypeError("result")
            dates = sorted(str(issue["issue_date"]) for issue in issues)
            status["result"] = {
                "newest_issue_date": dates[-1] if dates else "",
                "issues_in_result": len(dates),
                "rows_in_result": len(rows),
                "retry_issues": len(retry),
            }
        except (AttributeError, KeyError, TypeError, ValueError):
            status.setdefault("unreadable", []).append(source.collection_file.name)
    return status


class ProductionPwPrecollectionStage:
    """The live pass, as the weekly run starts it."""

    def collect(
        self,
        config_path: Path,
        output_dir: Path,
        *,
        presentation: ProgressSnapshotPresentation | None = None,
        failure_recorder: ProgressSnapshotFailureRecorder | None = None,
        budget_seconds: float = PW_PASS_RUNTIME_BUDGET_SECONDS,
        run_date: date | None = None,
    ) -> dict[str, Any]:
        return weekly_stage(
            config_path,
            output_dir,
            presentation=presentation,
            failure_recorder=failure_recorder,
            budget_seconds=budget_seconds,
            run_date=run_date,
        )
