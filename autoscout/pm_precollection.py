"""The PM deals pre-collection pass: new deals emails into one Git-ignored result (Issue #943).

One pass opens the declared mailbox folder read-only, reads the messages
from the declared senders that are newer than the last pass's watermark and
no older than the declared maximum age, and parses each "Today's Deals"
email into deal rows. Messages whose subject names no declared newsletter
are skipped by rule and never fetched. The deal rows are merged into one
result file, deduplicated by their deal key, and a separate attempt record
says how the last pass ended. The summary carries counts only.
"""

from __future__ import annotations

import imaplib
import json
import threading
import time
from collections import Counter
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import AbstractContextManager
from contextlib import contextmanager
from contextlib import nullcontext
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any

from autoscout.json_io import write_json
from autoscout.pm_deal_email import PmDealEmailError
from autoscout.pm_deal_email import parse_message
from autoscout.pm_mailbox import REASON_CONNECTION
from autoscout.pm_mailbox import REASON_CREDENTIALS
from autoscout.pm_mailbox import REASON_DECLARATION
from autoscout.pm_mailbox import REASON_PROTOCOL
from autoscout.pm_mailbox import REASON_TIMEOUT
from autoscout.pm_mailbox import Mailbox
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
from autoscout.runtime_budget import PM_PASS_RUNTIME_BUDGET_SECONDS
from autoscout.unexpected_error import keep_exception

# Version 2 adds the rights-lead fields of each row (#948). A version 1 result stays readable: its
# rows are kept, and the next pass reads their emails again so each row gains those fields as its
# email is read. A row whose email was not read again yet keeps no such fields.
RESULT_SCHEMA_VERSION = 2
READABLE_RESULT_SCHEMA_VERSIONS = (1, 2)
# The last email an unfinished upgrade of version 1 rows has read again (#948).
UPGRADE_CURSOR_FIELD = "upgrade_cursor_uid"
RIGHTS_LEAD_FIELDS = (
    "deal_size",
    "deal_types",
    "book_count",
    "publication",
    "rights_also",
    "contacts",
    "earlier_books",
)
STOP_MAX_MESSAGES = "pm_max_messages"
STOP_TIME_BUDGET = "pm_time_budget"
# A weekly run for an earlier date reads no mail: a pass always collects today's emails (#946).
REASON_HISTORICAL_RUN = "pm_historical_run_date"
# Yields whether the pass may still write its result and watermark.
CommitGuard = Callable[[], AbstractContextManager[bool]]
# Another pass holds the lock: its result and watermark writes must not interleave with this one's (#946).
REASON_IN_PROGRESS = "pm_pass_in_progress"
# The pass read deals emails and none of them yielded a deal: a format change, not a quiet week (#946).
REASON_NO_DEALS_PARSED = "pm_no_deals_parsed"
REASON_UNEXPECTED = "unexpected_error"
WEEKLY_STAGE_NAME = "pm-precollection"
# How often the stage refreshes its snapshot while the pass is inside one mail-server call.
PROGRESS_REFRESH_SECONDS = 5.0
# The end of the stage budget kept for parsing and writing the emails a pass has read.
PM_COMMIT_RESERVE_SECONDS = 60.0
# The PM declaration sits beside the weekly config it serves.
PM_CONFIG_FILENAME = "pm-deal-email.json"


class PmNoDealsParsedError(PmMailboxError):
    """Deals emails were read and none parsed into a deal. Carries the counts of the pass."""

    def __init__(self, counts: Mapping[str, Any]) -> None:
        super().__init__(REASON_NO_DEALS_PARSED, "the PM deals emails parsed into no deal")
        self.counts = dict(counts)


def lock_path(source: PmSource) -> Path:
    return source.attempt_file.with_suffix(".lock")


@contextmanager
def _pass_lock(source: PmSource) -> Iterator[None]:
    """Hold the PM pass lock for the whole pass, or raise ``pm_pass_in_progress`` at once.

    Two overlapping passes could write B's result, A's result, A's watermark
    and B's watermark in that order: the result would lose B's new deals while
    the watermark marks them read. The lock is an advisory ``flock`` that the
    operating system releases when the process ends, so a crashed pass never
    leaves it behind.
    """
    import fcntl

    path = lock_path(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PmMailboxError(REASON_IN_PROGRESS, "another PM deals pass is running") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _previous_result(
    source: PmSource, *, versions: tuple[int, ...] = (RESULT_SCHEMA_VERSION,)
) -> dict[str, Any] | None:
    """The earlier result, or None when it is missing, no longer valid or of a version not in *versions*."""
    value = _read_object(source.collection_file)
    if (
        value is None
        or value.get("schema_version") not in versions
        or not isinstance(value.get("rows"), list)
        or not isinstance(value.get("messages"), list)
    ):
        return None
    return value


def _watermark_file(source: PmSource) -> Path:
    # load_source always declares one for the PM family.
    if source.watermark_file is None:
        raise PmMailboxError(REASON_DECLARATION, "the PM source declares no watermark_file")
    return source.watermark_file


def _watermark(source: PmSource) -> tuple[int, int]:
    value = _read_object(_watermark_file(source)) or {}
    uidvalidity, last_uid = value.get("uidvalidity"), value.get("last_uid")
    if isinstance(uidvalidity, int) and isinstance(last_uid, int):
        return uidvalidity, last_uid
    return 0, 0


def _newsletter_kind(source: PmSource, header: MessageHeader) -> str:
    if header.sender not in source.senders:
        return ""
    for kind, subject in source.subjects.items():
        if header.subject.casefold() == subject.casefold():
            return kind
    return ""


def _last_seen(row: Mapping[str, Any]) -> str:
    """The date of the newest email that carried the row's deal."""
    return str(row.get("last_seen_message_date") or row.get("message_date", ""))


def _within_age(stamp: str, since: date) -> bool:
    try:
        return datetime.fromisoformat(stamp).date() >= since
    except ValueError:
        return False


def run_pass(
    config_path: Path,
    *,
    now: datetime | None = None,
    opener: MailboxOpener | None = None,
    environ: Mapping[str, str] | None = None,
    key_file: Path | None = None,
    allowance: Callable[[], float] | None = None,
    commit_guard: CommitGuard | None = None,
) -> dict[str, Any]:
    """Run one pass for the PM source of *config_path*; return its counts-only summary.

    Only one pass runs at a time: a pass that finds another one running raises
    ``pm_pass_in_progress`` and writes nothing, not even its attempt record.
    *allowance* reports the seconds left; the pass fetches no further message
    once it reaches zero, keeps what it read and ends ``partial``.
    """
    source = load_source(config_path)
    with _pass_lock(source):
        return _locked_pass(source, now, opener, environ, key_file, allowance, commit_guard)


def _locked_pass(
    source: PmSource,
    now: datetime | None,
    opener: MailboxOpener | None,
    environ: Mapping[str, str] | None,
    key_file: Path | None,
    allowance: Callable[[], float] | None,
    commit_guard: CommitGuard | None = None,
) -> dict[str, Any]:
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    since = moment.date() - timedelta(days=source.max_message_age_days)

    def record(status: str, reason_code: str = "", counts: Mapping[str, Any] | None = None) -> None:
        # The attempt is kept apart from the result: a failed pass writes no result.
        source.attempt_file.parent.mkdir(parents=True, exist_ok=True)
        write_json(
            source.attempt_file,
            {
                "attempted_at": moment.isoformat(),
                "status": status,
                "reason_code": reason_code,
                "counts": dict(counts or {}),
            },
        )

    record("started")
    user = load_credential(source.user_env, environ, key_file)
    password = load_credential(source.password_env, environ, key_file)
    if not user or not password:
        record("not_collected", REASON_CREDENTIALS)
        raise PmMailboxError(
            REASON_CREDENTIALS,
            f"set {source.user_env} and {source.password_env} in the environment or the PM mailbox key file",
        )
    try:
        summary = _collect(source, user, password, moment, since, opener or open_imap_mailbox, allowance, commit_guard)
    except PmMailboxError as exc:
        record("not_collected", exc.reason_code, getattr(exc, "counts", None))
        raise
    except TimeoutError:
        record("not_collected", REASON_TIMEOUT)
        raise PmMailboxError(REASON_TIMEOUT, "the PM mail server did not answer in time") from None
    except imaplib.IMAP4.error:
        record("not_collected", REASON_PROTOCOL)
        raise PmMailboxError(REASON_PROTOCOL, "the PM mail server ended the session") from None
    except OSError:
        record("not_collected", REASON_CONNECTION)
        raise PmMailboxError(REASON_CONNECTION, "the PM mail server connection failed") from None
    except Exception:
        # Recorded while the lock is held, so doctor reports the failure and not a pass still running.
        record("not_collected", REASON_UNEXPECTED)
        raise
    record(summary["status"], summary["stop_reason"], summary)
    return summary


def _has_rows_to_upgrade(result: Mapping[str, Any]) -> bool:
    """Whether a result holds rows written before the rights-lead fields (schema version 1)."""
    if result.get("schema_version") != RESULT_SCHEMA_VERSION:
        return True
    return any(isinstance(row, dict) and "deal_size" not in row for row in result.get("rows", []))


def _collect(
    source: PmSource,
    user: str,
    password: str,
    moment: datetime,
    since: date,
    opener: MailboxOpener,
    allowance: Callable[[], float] | None = None,
    commit_guard: CommitGuard | None = None,
) -> dict[str, Any]:
    previous = _previous_result(source, versions=READABLE_RESULT_SCHEMA_VERSIONS)
    known_uidvalidity, last_uid = _watermark(source)
    if previous is not None and _has_rows_to_upgrade(previous):
        # Rows of an earlier version: keep them, and read their emails again so they gain the new
        # fields. The upgrade's progress is kept in the result itself, written in one step with the
        # rows, so a pass cut short or a failed watermark write never loses or repeats it.
        cursor = previous.get(UPGRADE_CURSOR_FIELD) if previous.get("schema_version") == RESULT_SCHEMA_VERSION else 0
        last_uid = cursor if type(cursor) is int and cursor >= 0 else 0
    if previous is None:
        # The watermark says what the lost result held: read again within the maximum age.
        # A result of an earlier version is read again the same way, so its rows gain the new fields.
        previous, last_uid = {"rows": [], "messages": []}, 0
    mailbox = opener(source, user, password)
    try:
        if mailbox.uidvalidity != known_uidvalidity:
            # The server renumbered the folder: read again within the maximum age.
            # The deal keys of the result keep the deals from being counted twice.
            last_uid = 0
        headers = [header for header in mailbox.headers_since(since, source.senders) if header.uid > last_uid]
        headers = [header for header in headers if header.date is None or header.date.date() >= since]
        skipped = 0
        read: list[tuple[MessageHeader, str, bytes]] = []
        newest_uid = last_uid
        stop_reason = ""
        for header in headers:
            kind = _newsletter_kind(source, header)
            if not kind:
                skipped += 1
                newest_uid = header.uid
                continue
            if len(read) == source.max_messages_per_pass:
                stop_reason = STOP_MAX_MESSAGES
                break
            if allowance is not None and allowance() <= 0:
                stop_reason = STOP_TIME_BUDGET
                break
            read.append((header, kind, mailbox.fetch(header.uid)))
            newest_uid = header.uid
        uidvalidity = mailbox.uidvalidity
    finally:
        mailbox.close()

    # Rows and messages past the maximum age leave first, so a deal that reappears in a
    # new email is recorded from that email rather than hidden by its expired row.
    rows = {
        row["deal_key"]: row
        for row in previous["rows"]
        if isinstance(row, dict) and "deal_key" in row and _within_age(_last_seen(row), since)
    }
    messages = {
        entry["message_id"]: entry
        for entry in previous["messages"]
        if isinstance(entry, dict)
        and isinstance(entry.get("message_id"), str)
        and _within_age(str(entry.get("message_date", "")), since)
    }
    parsed_count = new_count = 0
    rejected: Counter[str] = Counter()
    unreadable: Counter[str] = Counter()
    for header, kind, raw in read:
        try:
            parsed = parse_message(raw, newsletter_kind=kind)
        except PmDealEmailError as exc:
            unreadable[exc.reason_code] += 1
            continue
        message_date = parsed.message_date or (header.date.isoformat() if header.date else moment.isoformat())
        message_id = parsed.message_id or f"uid:{uidvalidity}:{header.uid}"
        parsed_count += len(parsed.rows)
        rejected.update(rejection.reason_code for rejection in parsed.rejections)
        messages[message_id] = {
            "message_id": message_id,
            "message_date": message_date,
            "newsletter_kind": kind,
            "deals": len(parsed.rows),
            "rejections": [rejection.as_json() for rejection in parsed.rejections],
        }
        for row in parsed.rows:
            known = rows.get(row.deal_key)
            if known is not None and "deal_size" not in known:
                # A row of an earlier version gains the rights-lead fields from its email.
                known.update({name: value for name, value in row.as_json().items() if name in RIGHTS_LEAD_FIELDS})
            if known is not None:
                # The first email stays the deal's record; a repeat keeps it from expiring.
                if message_date > _last_seen(known):
                    known["last_seen_message_date"] = message_date
                continue
            new_count += 1
            rows[row.deal_key] = {
                **row.as_json(),
                "message_id": message_id,
                "message_date": message_date,
                "last_seen_message_date": message_date,
                "newsletter_kind": kind,
                "first_collected_at": moment.isoformat(),
            }

    if read and parsed_count == 0:
        # Written neither the result nor the watermark: once the parser reads the new format,
        # the next pass reads these emails again.
        raise PmNoDealsParsedError(
            {
                "messages_read": len(read),
                "messages_unreadable": dict(sorted(unreadable.items())),
                "deals_rejected": dict(sorted(rejected.items())),
            }
        )
    kept_rows = [row for row in rows.values() if _within_age(_last_seen(row), since)]
    kept_messages = [entry for entry in messages.values() if _within_age(str(entry.get("message_date", "")), since)]
    status = "partial" if stop_reason else "collected"
    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "source_name": source.name,
        "collected_at": moment.isoformat(),
        "status": status,
        "stop_reason": stop_reason,
        "oldest_message_date": since.isoformat(),
        "rows": sorted(kept_rows, key=lambda row: (str(row["message_date"]), str(row["deal_key"]))),
        "messages": sorted(kept_messages, key=lambda entry: (str(entry["message_date"]), str(entry["message_id"]))),
    }
    if _has_rows_to_upgrade(result):
        # The next pass continues the upgrade after the last email this one read.
        result[UPGRADE_CURSOR_FIELD] = newest_uid
    with commit_guard() if commit_guard is not None else nullcontext(True) as may_commit:
        if not may_commit:
            # The weekly stage stopped waiting for this pass and moved on: it writes nothing.
            raise PmMailboxError(STOP_TIME_BUDGET, "the PM pass ran past its weekly stage")
        for path in (source.collection_file, _watermark_file(source)):
            path.parent.mkdir(parents=True, exist_ok=True)
        write_json(source.collection_file, result)
        # The watermark moves only once the result that holds the messages is written.
        write_json(_watermark_file(source), {"uidvalidity": uidvalidity, "last_uid": newest_uid})
    return {
        "status": status,
        "collected_at": moment.isoformat(),
        "messages_read": len(read),
        "messages_skipped_by_rule": skipped,
        "messages_unreadable": dict(sorted(unreadable.items())),
        "deals_parsed": parsed_count,
        "deals_new": new_count,
        "deals_rejected": dict(sorted(rejected.items())),
        "deals_in_result": len(kept_rows),
        "stop_reason": stop_reason,
    }


def collect_pm_deals_command(config_path: Path) -> int:
    """Print the counts of one pass. Returns 0 when a result was written and 2 when it was not."""
    try:
        report = run_pass(config_path)
    except PmMailboxError as exc:
        print(json.dumps({"status": "failed", "reason_code": exc.reason_code, "reason": str(exc)}, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def pm_config_path(weekly_config: Path) -> Path:
    """The PM declaration of the weekly config *weekly_config*."""
    return weekly_config.parent / PM_CONFIG_FILENAME


class _CountedMailbox:
    """A mailbox that reports each fetch to the weekly heartbeat."""

    def __init__(self, inner: Mailbox, on_fetch: Callable[[bool], None]) -> None:
        self._inner = inner
        self._on_fetch = on_fetch

    @property
    def uidvalidity(self) -> int:
        return self._inner.uidvalidity

    def headers_since(self, since: date, senders: tuple[str, ...]) -> list[MessageHeader]:
        return self._inner.headers_since(since, senders)

    def fetch(self, uid: int) -> bytes:
        self._on_fetch(True)
        raw = self._inner.fetch(uid)
        # Only an email actually received counts as read; a failed fetch raises past this line.
        self._on_fetch(False)
        return raw

    def close(self) -> None:
        self._inner.close()


def weekly_outcome(report: Mapping[str, Any]) -> str:
    """Name what a pass that wrote its result found: a quiet week is not a failure."""
    if not report.get("messages_read"):
        return "no_new_messages"
    if not report.get("deals_new"):
        return "no_new_deals"
    return "new_deals"


def weekly_stage(
    weekly_config: Path,
    output_dir: Path,
    *,
    presentation: ProgressSnapshotPresentation | None = None,
    failure_recorder: ProgressSnapshotFailureRecorder | None = None,
    opener: MailboxOpener | None = None,
    environ: Mapping[str, str] | None = None,
    key_file: Path | None = None,
    now: datetime | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    budget_seconds: float = PM_PASS_RUNTIME_BUDGET_SECONDS,
    write_snapshot: bool = True,
    run_date: date | None = None,
) -> dict[str, Any]:
    """Run one PM pass as a stage of the weekly run; never raises.

    Mail is cheap to read, so every weekly run makes a pass. A weekly config
    with no PM declaration beside it is skipped as ``not_configured``. Every
    failure is a named ``not_collected`` report for PM alone. The pass fetches
    no message once *budget_seconds* are spent, and the stage stops waiting for
    a pass that is still inside one mail-server call at that point. Heartbeat
    rows carry the source name only: no title, sender or subject.
    """
    config = pm_config_path(weekly_config)
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
    state = {"fetched": 0, "closed": False}
    guard = threading.Lock()

    def publish(*, running: bool, close: bool = False) -> None:
        with guard:
            if state["closed"]:
                # A pass the stage stopped waiting for must not overwrite a later stage's snapshot.
                return
            rows: list[dict[str, Any]] = [{"source_name": "PM", "state": "finished"} for _ in range(state["fetched"])]
            if running:
                # The pass is at work (login, header search or a fetch) for as long as it runs; the
                # heartbeat counts finished emails only, so this row never inflates the count.
                rows.append({"source_name": "PM", "state": "in_flight"})
            writer.publish(
                stage=WEEKLY_STAGE_NAME,
                status="running" if running else "finished",
                elapsed_seconds=monotonic() - started,
                rows=rows,
                stage_budget_seconds=budget_seconds,
            )
            state["closed"] = close

    def on_fetch(starting: bool) -> None:
        if not starting:
            with guard:
                state["fetched"] += 1
        publish(running=True)

    read = opener or open_imap_mailbox

    def counted_opener(source: PmSource, user: str, password: str) -> Mailbox:
        return _CountedMailbox(read(source, user, password), on_fetch)

    # The pass stops fetching while time remains to parse and write what it read, so a fetch
    # that runs past the cut-off never costs the emails read before it.
    reserve = min(PM_COMMIT_RESERVE_SECONDS, budget_seconds / 4)

    def allowance() -> float:
        return budget_seconds - reserve - (monotonic() - started)

    answers: list[dict[str, Any]] = []
    commit = threading.Lock()
    abandoned = {"value": False, "committed": False}

    @contextmanager
    def commit_guard() -> Iterator[bool]:
        # The stage's cut-off and the pass's write never interleave: either the result is
        # written before the stage stops waiting, or it is never written by this pass.
        with commit:
            allowed = not abandoned["value"]
            abandoned["committed"] = allowed
            yield allowed

    def work() -> None:
        report: dict[str, Any]
        try:
            report = run_pass(
                config,
                now=now,
                opener=counted_opener,
                environ=environ,
                key_file=key_file,
                allowance=allowance,
                commit_guard=commit_guard,
            )
            report["outcome"] = weekly_outcome(report)
        except PmMailboxError as exc:
            report = {"status": "not_collected", "reason_code": exc.reason_code, **getattr(exc, "counts", {})}
        except Exception as exc:
            report = {
                "status": "not_collected",
                "reason_code": REASON_UNEXPECTED,
                **keep_exception(exc, WEEKLY_STAGE_NAME),
            }
        answers.append(report)

    publish(running=True)
    worker = threading.Thread(target=work, name="pm-pass", daemon=True)
    worker.start()
    # The pass checks its allowance before each message; one mail-server call may still run past it.
    # Waiting in slices keeps the snapshot's elapsed time fresh through a slow login or fetch.
    deadline = time.monotonic() + max(0.0, budget_seconds)
    while worker.is_alive():
        left = deadline - time.monotonic()
        if left <= 0:
            break
        worker.join(min(PROGRESS_REFRESH_SECONDS, left))
        if worker.is_alive():
            publish(running=True)
    with commit:
        abandoned["value"] = True
        committed = abandoned["committed"]
    if committed and not answers:
        # The result was written before the cut-off: the pass is only closing, so wait for its report.
        worker.join(PROGRESS_REFRESH_SECONDS)
    report: dict[str, Any] = answers[0] if answers else {"status": "not_collected", "reason_code": STOP_TIME_BUDGET}
    publish(running=False, close=True)
    report["duration_seconds"] = round(monotonic() - started, 3)
    return report


def weekly_source_problem(report: Mapping[str, Any], source_name: str) -> dict[str, str] | None:
    """The weekly completion's named PM problem for the stage report *report*, if any."""
    status = report.get("status")
    if status == "not_collected":
        return {
            "source_name": source_name,
            "status": "failed",
            "reason": f"PM deals pass failed ({report.get('reason_code') or 'unknown'}); "
            "capture uses the previous result when available",
        }
    if status == "partial":
        return {
            "source_name": source_name,
            "status": "incomplete",
            "reason": f"PM deals pass stopped early ({report.get('stop_reason') or 'unknown'}); "
            "the next pass reads the remaining emails",
        }
    return None


def configured_source_name(weekly_config: Path) -> str:
    """The declared PM source name, or a generic one when the declaration cannot be read."""
    try:
        return load_source(pm_config_path(weekly_config)).name
    except PmMailboxError:
        return "Publishers Marketplace"


def last_pass_status(weekly_config: Path) -> dict[str, Any] | None:
    """Read-only: the last PM attempt and result, or None when PM is not configured.

    Reads the attempt record and the result file only; it takes no lock and
    opens no mailbox, so it can never refuse or disturb a running pass.
    """
    config = pm_config_path(weekly_config)
    if not config.is_file():
        return None
    try:
        source = load_source(config)
    except PmMailboxError as exc:
        return {"configured": False, "reason_code": exc.reason_code}
    attempt = _read_object(source.attempt_file)
    result = _previous_result(source, versions=READABLE_RESULT_SCHEMA_VERSIONS)
    status: dict[str, Any] = {"configured": True, "source_name": source.name}
    if attempt is not None:
        counts = attempt.get("counts")
        status["attempt"] = {
            "status": str(attempt.get("status", "")),
            "reason_code": str(attempt.get("reason_code", "")),
            "attempted_at": str(attempt.get("attempted_at", "")),
            "counts": dict(counts) if isinstance(counts, dict) else {},
        }
    if result is not None:
        status["result"] = {
            "status": str(result.get("status", "")),
            "collected_at": str(result.get("collected_at", "")),
            "deals_in_result": len(result["rows"]),
            "messages_in_result": len(result["messages"]),
        }
    return status


class ProductionPmPrecollectionStage:
    """The live pass, as the weekly run starts it."""

    def collect(
        self,
        config_path: Path,
        output_dir: Path,
        *,
        presentation: ProgressSnapshotPresentation | None = None,
        failure_recorder: ProgressSnapshotFailureRecorder | None = None,
        budget_seconds: float = PM_PASS_RUNTIME_BUDGET_SECONDS,
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
