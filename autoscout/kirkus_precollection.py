"""The Kirkus pre-collection step: one pass over the coming-soon listing, before the source check.

The site asks for twenty seconds between requests, so a pass takes many
minutes. The weekly run starts it as its own stage, once a day, and it also
runs as its own command. It writes one result file. The capture stage reads
that file and requests nothing.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import threading
import time
from collections import Counter
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import Request

from autoscout import kirkus_listing
from autoscout.config import load_validated_config
from autoscout.http_decoding import MAX_RESPONSE_BYTES
from autoscout.http_decoding import decode_bounded_http_body
from autoscout.json_io import write_json
from autoscout.progress_snapshot import ProgressSnapshotFailureRecorder
from autoscout.progress_snapshot import ProgressSnapshotPresentation
from autoscout.progress_snapshot import ProgressSnapshotWriter
from autoscout.runtime_budget import KIRKUS_PASS_RUNTIME_BUDGET_SECONDS
from autoscout.source_collectors.base import collector_request_headers
from autoscout.source_collectors.base import contract_family
from autoscout.source_rules import is_active_source
from autoscout.source_rules import is_skipped_source
from autoscout.unexpected_error import emit_traceback
from autoscout.unexpected_error import keep_exception

REQUEST_TIMEOUT_SECONDS = 30
WEEKLY_STAGE_NAME = "kirkus-precollection"
# The pass lock (#1057): one pass at a time, whoever starts it.
PASS_IN_PROGRESS = "kirkus_pass_in_progress"
SCHEDULED_SKIP_ATTEMPTED = "Сбор Kirkus по расписанию пропущен: сегодня сбор уже начинался."
SCHEDULED_SKIP_RUNNING = "Сбор Kirkus по расписанию пропущен: сбор уже идёт."


def fetch_page(url: str, allowance_seconds: Callable[[], float] | None = None) -> dict[str, Any]:
    """One bounded GET with the collector's identifying headers; no redirect and no retry.

    The whole request, from the connection to the last byte of the body, may
    take REQUEST_TIMEOUT_SECONDS and no longer than *allowance_seconds* reports
    is left. A socket timeout bounds one receive and not a response whose
    headers or body trickle in, so the request runs on a thread of its own and
    is given up at the deadline as a request that got no answer.
    """
    request = Request(url, headers=collector_request_headers())
    started = time.monotonic()

    def remaining() -> float:
        left = REQUEST_TIMEOUT_SECONDS - (time.monotonic() - started)
        return left if allowance_seconds is None else min(left, allowance_seconds())

    def get() -> dict[str, Any]:
        try:
            with kirkus_listing.open_without_redirect(request, timeout=max(1.0, remaining())) as response:
                # The shared decoder handles the content encoding, the charset and the size bound.
                decoded = decode_bounded_http_body(response, MAX_RESPONSE_BYTES, remaining_seconds=remaining)
                return {
                    "status": response.status,
                    "resolved_url": response.geturl(),
                    "body": decoded.text,
                    "truncated": decoded.truncated or bool(decoded.decode_failure) or decoded.timed_out,
                }
        except HTTPError as exc:
            return {"status": exc.code, "resolved_url": url, "body": "", "truncated": False}
        except (URLError, TimeoutError, OSError):
            return {"status": None, "resolved_url": url, "body": "", "truncated": False}
        except Exception as exc:
            # Still a request that got no answer, as before; but the worker's traceback is kept (#1158).
            keep_exception(exc, WEEKLY_STAGE_NAME)
            return {"status": None, "resolved_url": url, "body": "", "truncated": False}

    answers: list[dict[str, Any]] = []
    worker = threading.Thread(target=lambda: answers.append(get()), daemon=True)
    worker.start()
    worker.join(max(0.0, remaining()))
    return answers[0] if answers else {"status": None, "resolved_url": url, "body": "", "truncated": False}


def configured_source(config: Mapping[str, Any]) -> dict[str, Any]:
    """The one active Kirkus source of a config that declares where its result is kept."""
    sources = [
        source
        for source in config.get("sources", [])
        if isinstance(source, dict)
        and contract_family(source) == kirkus_listing.FAMILY
        and is_active_source(source)
        # A source the capture stage skips for its access status is never requested.
        and not is_skipped_source(source)
    ]
    if len(sources) != 1:
        raise kirkus_listing.KirkusError(
            "kirkus_declaration", "the config must hold exactly one active Kirkus listing source"
        )
    if not str(sources[0].get("kirkus_collection_file", "")).strip():
        raise kirkus_listing.KirkusError(
            "kirkus_declaration", "the Kirkus source declares no kirkus_collection_file in this config"
        )
    return sources[0]


def summary(collection: Mapping[str, Any], requested: set[str]) -> dict[str, Any]:
    """Counts of one pass, with no title, author or other page text.

    *requested* names the review pages this pass requested. A page is counted
    by the request made for it, not by its date or by an earlier pass having
    opened it, so a second pass on the same day and a page read again because
    its card changed are both reported as they happened.
    """
    rows = collection["rows"]
    opened = [row for row in rows if row["page_fetched_date"]]
    return {
        "status": "partial" if collection["stop_reason"] else "collected",
        "collected_date": collection["collected_date"],
        "listing_pages": len(collection["listing_pages"]),
        "listed_rows": len(rows),
        "review_pages_opened_this_pass": sum(row["review_url"] in requested for row in opened),
        "review_pages_kept_from_earlier_passes": sum(row["review_url"] not in requested for row in opened),
        "rows_waiting_for_a_later_pass": kirkus_listing.waiting_row_count(collection),
        "rows_with_facts": sum(row["detail"] is not None for row in rows),
        "rejected_rows": dict(sorted(Counter(row["rejection"] for row in rows if row["rejection"]).items())),
        "stop_reason": collection["stop_reason"],
    }


def pass_lock_path(config_path: Path, source: Mapping[str, Any]) -> Path:
    """The pass lock, beside the attempt record in the ignored collection folder."""
    path = kirkus_listing.collection_path(config_path, source)
    assert path is not None
    return path.with_name(path.stem + "-pass.lock")


def pass_lock_held(lock: Path) -> bool:
    """Whether a live pass holds *lock*: an advisory probe of the same OS lock ``run_pass`` takes.

    The probe tries the lock without waiting and lets it go at once, so it
    never holds a pass back; ``run_pass`` takes the lock for real and closes
    any gap between this check and the start. An unopenable lock counts as
    held, so a check never starts a pass it cannot exclude.
    """
    try:
        descriptor = os.open(lock, os.O_RDONLY)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(descriptor)
    return False


@contextmanager
def _pass_lock(lock: Path) -> Iterator[bool]:
    """Hold the pass lock for the whole pass; yields whether it was acquired.

    The lock is the kernel's ``flock`` on the file, never the file itself: the
    file is created once and never removed, and the lock goes with the
    descriptor, so a pass that dies releases it and no stale lock is left.
    The PID written into it is a diagnostic only.
    """
    lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            acquired = False
        else:
            acquired = True
            os.ftruncate(descriptor, 0)
            os.write(descriptor, str(os.getpid()).encode("utf-8"))
        yield acquired
    finally:
        os.close(descriptor)


def run_pass(
    config_path: Path,
    *,
    reference: date | None = None,
    fetch: Callable[[str], Mapping[str, Any]] | None = None,
    pause: Callable[[float], None] | None = None,
    skip_if_attempted: bool = False,
) -> dict[str, Any]:
    """Run one pass for the Kirkus source of *config_path* and write its result file.

    This is the one place a pass starts, so every path holds the pass lock
    around "check the day, record the attempt, collect" (#1057). A live pass
    holding it raises ``kirkus_pass_in_progress`` before any request. With
    *skip_if_attempted*, a day that already had an attempt returns
    ``{"status": "already_attempted_today"}`` and makes no request.
    """
    source = configured_source(load_validated_config(config_path))
    path = kirkus_listing.collection_path(config_path, source)
    assert path is not None
    day = reference or date.today()
    attempt = kirkus_listing.attempt_path(config_path, source)
    assert attempt is not None
    with _pass_lock(pass_lock_path(config_path, source)) as acquired:
        if not acquired:
            raise kirkus_listing.KirkusError(PASS_IN_PROGRESS, "another Kirkus pass is running")
        if skip_if_attempted and _attempted_on(config_path, source, day):
            return {"status": "already_attempted_today"}
        return _collect_pass(source, path, attempt, day, fetch, pause)


def _collect_pass(
    source: Mapping[str, Any],
    path: Path,
    attempt: Path,
    day: date,
    fetch: Callable[[str], Mapping[str, Any]] | None,
    pause: Callable[[float], None] | None,
) -> dict[str, Any]:
    """Record the attempt, collect, and write the result; the caller holds the pass lock."""

    # The status of the last answer that was not HTTP 200, "none" when no answer came (a timeout
    # or a dropped connection). Kept so a kirkus_http attempt says which refusal it was.
    refusal: list[str] = []

    def record(status: str, reason_code: str = "") -> None:
        # The attempt is recorded apart from the result: a pass that fails writes no result, and the
        # weekly run must still know that the site was asked on this date.
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"attempted_date": day.isoformat(), "status": status, "reason_code": reason_code}
        if reason_code == "kirkus_http" and refusal:
            record["http_status"] = refusal[-1]
        write_json(attempt, record)

    record("started")
    try:
        previous = kirkus_listing.read_collection(path)
    except kirkus_listing.KirkusError:
        previous = None
    try:
        # An earlier result that no longer validates is not reused; its review pages are opened again.
        if previous is not None:
            kirkus_listing.validate_collection(previous, source)
    except kirkus_listing.KirkusError:
        previous = None
    requested: list[str] = []
    read = fetch or fetch_page

    def recorded_fetch(url: str) -> Mapping[str, Any]:
        requested.append(url)
        response = read(url)
        if response.get("status") != 200:
            refusal.append(str(response.get("status") or "none"))
        return response

    try:
        collection = kirkus_listing.collect(
            source, day, recorded_fetch, pause or (lambda seconds: kirkus_listing.default_pause(seconds)), previous
        )
    except kirkus_listing.KirkusError as exc:
        record("not_collected", exc.reason_code)
        raise
    write_json(path, collection)
    record("partial" if collection["stop_reason"] else "collected", collection["stop_reason"])
    return summary(collection, set(requested))


def collect_kirkus_command(
    config_path: Path,
    *,
    scheduled: bool = False,
    today: date | None = None,
    code_passed: Callable[[], bool] | None = None,
) -> int:
    """Print the counts of one pass. Returns 0 when a result was written and 2 when it was not.

    A pass that finds another pass running (the pass lock) makes no request:
    a manual one reports ``kirkus_pass_in_progress`` and returns 2. A
    *scheduled* pass (#1057), started by the server's timer, makes no pass
    when one is running or one already started on its day; it then prints
    one line and returns 0. A scheduled pass that fails prints its stage and
    reason for the journal, also when config validation exits
    (``config_invalid``, the exit's own code), and sends nothing: the run's
    morning report states the pass from its attempt record (#1057, decided
    2026-11-14). It never touches Git; *today* is a seam for tests.

    *code_passed* is the run server's gate on new code (#1124): a scheduled
    pass on code that has not passed ``./scripts/check`` on this machine
    makes no request, prints one line and returns 0. It runs no check
    itself; the nightly run does, and makes the pass itself when none ran
    that day. ``None`` means no gate.
    """
    day = today or date.today()
    if scheduled and code_passed is not None and not code_passed():
        from autoscout.code_gate import KIRKUS_SKIPPED_UNCHECKED_LINE

        print(KIRKUS_SKIPPED_UNCHECKED_LINE)
        return 0
    try:
        report = run_pass(config_path, reference=day, skip_if_attempted=scheduled)
        if report.get("status") == "already_attempted_today":
            print(SCHEDULED_SKIP_ATTEMPTED)
            return 0
    except kirkus_listing.KirkusError as exc:
        if scheduled and exc.reason_code == PASS_IN_PROGRESS:
            print(SCHEDULED_SKIP_RUNNING)
            return 0
        print(json.dumps({"status": "failed", "reason_code": exc.reason_code, "reason": str(exc)}, indent=2))
        if scheduled:
            _print_failed_pass(exc.reason_code)
        return 2
    except SystemExit as exc:
        # load_validated_config exits with SystemExit(1), which ``except
        # Exception`` does not catch; a manual pass lets it through as before.
        from autoscout.operator_notice import exit_code_of

        code = exit_code_of(exc)
        if not scheduled or code == 0:
            raise
        _print_failed_pass("config_invalid")
        return code
    except Exception as exc:
        if not scheduled:
            raise
        emit_traceback(exc, WEEKLY_STAGE_NAME)
        _print_failed_pass("unexpected_error")
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _print_failed_pass(reason_code: str) -> None:
    from autoscout.operator_notice import failure_line

    print(failure_line(WEEKLY_STAGE_NAME, reason_code))


def last_attempt(config_path: Path) -> dict[str, str] | None:
    """The last Kirkus attempt record for the morning report; ``None`` when no Kirkus source is configured.

    Never raises and never exits: the report of a run stopped by its own
    config must still go out, so an unreadable config reads as no source.
    """
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            source = configured_source(load_validated_config(config_path))
        return kirkus_listing.read_attempt(kirkus_listing.attempt_path(config_path, source))
    except (Exception, SystemExit):
        return None


def _skip_reason(config_path: Path, reference: date) -> str:
    try:
        source = configured_source(load_validated_config(config_path))
    except (kirkus_listing.KirkusError, ValueError, OSError):
        return "not_configured"
    if pass_lock_held(pass_lock_path(config_path, source)):
        return "pass_in_progress"
    return "already_attempted_today" if _attempted_on(config_path, source, reference) else ""


def _attempted_on(config_path: Path, source: Mapping[str, Any], reference: date) -> bool:
    # A pass that failed wrote no result, and the site is still not asked twice on one day.
    attempt = kirkus_listing.read_attempt(kirkus_listing.attempt_path(config_path, source))
    if attempt.get("attempted_date") == reference.isoformat():
        return True
    try:
        previous = kirkus_listing.read_collection(kirkus_listing.collection_path(config_path, source))
    except kirkus_listing.KirkusError:
        previous = None
    return previous is not None and previous.get("collected_date") == reference.isoformat()


def kirkus_is_due(config_path: Path, reference: date) -> bool:
    """Whether the weekly run of *reference* has a Kirkus pass to make."""
    return not _skip_reason(config_path, reference)


def weekly_stage(
    config_path: Path,
    output_dir: Path,
    reference: date,
    *,
    presentation: ProgressSnapshotPresentation | None = None,
    failure_recorder: ProgressSnapshotFailureRecorder | None = None,
    fetch: Callable[[str], Mapping[str, Any]] | None = None,
    pause: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    budget_seconds: float = KIRKUS_PASS_RUNTIME_BUDGET_SECONDS,
    write_snapshot: bool = True,
    stop_requested: threading.Event | None = None,
) -> dict[str, Any]:
    """Run the pass of the day as a stage of the weekly run; never raises.

    A config with no Kirkus source is skipped. So is a day that already has a
    result, whether complete or cut short: the site refused a request of a pass
    cut short, and a later pass continues from there. So is a day whose pass
    failed. The pass stops when the next delay would take it past
    *budget_seconds*, so the stages after it keep their time; the pages it
    opened are kept and the next pass continues. Every other outcome is
    left to the capture stage, which reads the result file and names a missing,
    stale or partial one for this source alone.
    """
    skipped = _skip_reason(config_path, reference)
    if skipped:
        return {"status": skipped}
    started = monotonic()
    writer = ProgressSnapshotWriter(
        output_dir,
        monotonic=monotonic,
        failure_recorder=failure_recorder,
        presentation=presentation,
        write_snapshot=write_snapshot,
    )
    requests = 0

    def allowance() -> float:
        if stop_requested is not None and stop_requested.is_set():
            return 0.0
        return budget_seconds - (monotonic() - started)

    read = fetch or (lambda url: fetch_page(url, allowance))

    def publish(running: bool) -> None:
        # One row per page request: no title, author or other page text.
        rows = [{"source_name": "Kirkus", "state": "finished"} for _ in range(requests - running)]
        if running:
            rows.append({"source_name": "Kirkus", "state": "in_flight"})
        writer.publish(
            stage=WEEKLY_STAGE_NAME,
            status="running" if running else "finished",
            elapsed_seconds=monotonic() - started,
            rows=rows,
            stage_budget_seconds=budget_seconds,
        )

    def counted_fetch(url: str) -> Mapping[str, Any]:
        nonlocal requests
        if allowance() <= 0:
            raise kirkus_listing.KirkusError("kirkus_time_budget", "Kirkus pass reached its time allowance")
        requests += 1
        publish(True)
        return read(url)

    wait = pause or (stop_requested.wait if stop_requested is not None else kirkus_listing.default_pause)

    def bounded_pause(seconds: float) -> None:
        if seconds > allowance():
            raise kirkus_listing.KirkusError("kirkus_time_budget", "Kirkus pass reached its time allowance")
        wait(seconds)

    try:
        report = run_pass(
            config_path, reference=reference, fetch=counted_fetch, pause=bounded_pause, skip_if_attempted=True
        )
    except kirkus_listing.KirkusError as exc:
        # A pass started elsewhere since the check above: no request, no wait.
        if exc.reason_code == PASS_IN_PROGRESS:
            report = {"status": "pass_in_progress"}
        else:
            report = {"status": "not_collected", "reason_code": exc.reason_code}
    except Exception as exc:
        report = {
            "status": "not_collected",
            "reason_code": "unexpected_error",
            **keep_exception(exc, WEEKLY_STAGE_NAME),
        }
    publish(False)
    report["duration_seconds"] = round(monotonic() - started, 3)
    return report


class ProductionKirkusPrecollectionStage:
    """The live pass, as the weekly run starts it."""

    def collect(
        self,
        config_path: Path,
        output_dir: Path,
        reference: date,
        *,
        presentation: ProgressSnapshotPresentation | None = None,
        failure_recorder: ProgressSnapshotFailureRecorder | None = None,
        budget_seconds: float = KIRKUS_PASS_RUNTIME_BUDGET_SECONDS,
        write_snapshot: bool = True,
        stop_requested: threading.Event | None = None,
    ) -> dict[str, Any]:
        return weekly_stage(
            config_path,
            output_dir,
            reference,
            presentation=presentation,
            failure_recorder=failure_recorder,
            budget_seconds=budget_seconds,
            write_snapshot=write_snapshot,
            stop_requested=stop_requested,
        )
