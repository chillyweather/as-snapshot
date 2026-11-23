"""Keep what an unexpected exception tells the operator, and nothing the editor sees (#1158).

A run or a collection pass that dies on an exception nobody expected tells the editor only
«непредвиденная ошибка». The operator needs more: the full traceback in the journal and the
per-run log (stderr), and the exception's kind and location in the run log that the doctor
reads. Everything written here is redacted by ``autoscout.secret_redaction``; none of it goes
into the Russian editor output or the morning report.
"""

from __future__ import annotations

import sys
import threading
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from autoscout.repository_root import code_root
from autoscout.secret_redaction import redact_secrets

UNEXPECTED_ERROR_STAGE = "unexpected-error"

# Frames are named relative to the running package's own location (the snapshot in a run on the last passing code).
_PROJECT_ROOT = code_root()
_write_lock = threading.Lock()
_stream: TextIO | None = None


@contextmanager
def tracebacks_to(stream: TextIO) -> Iterator[None]:
    """Send every traceback to *stream* while it runs, even if ``sys.stderr`` is redirected meanwhile.

    The weekly command silences stderr while the workflow runs; its passes still report
    their tracebacks to the stream that was stderr before the silencing.
    """
    global _stream
    previous = _stream
    _stream = stream
    try:
        yield
    finally:
        _stream = previous


def exception_location(exc: BaseException) -> str:
    """Where *exc* was raised: ``autoscout/file.py:12 in function``, the innermost frame of this project."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return "unknown"
    inside = [frame for frame in frames if _relative(frame.filename) is not None]
    frame = (inside or frames)[-1]
    shown = _relative(frame.filename) or Path(frame.filename).name
    return f"{shown}:{frame.lineno} in {frame.name}"


def _relative(filename: str) -> str | None:
    try:
        return Path(filename).resolve().relative_to(_PROJECT_ROOT).as_posix()
    except (OSError, ValueError):
        return None


def exception_facts(exc: BaseException) -> dict[str, str]:
    """The kind and location of *exc*, safe to persist and to show the operator: no message, no absolute path."""
    return {"exception_type": type(exc).__name__, "exception_location": exception_location(exc)}


def emit_traceback(exc: BaseException, where: str, *, omit_message: bool = False) -> None:
    """Write *exc*'s full traceback, with secrets redacted, to stderr (the journal and the per-run log).

    *omit_message* keeps the frames and the exception type but not the message or the chained
    exceptions: for a pass whose errors may echo a URL that grants access (PW's subscriber link,
    which no redaction pattern recognizes).
    """
    try:
        if omit_message:
            frames = "".join(traceback.format_tb(exc.__traceback__))
            text = f"Traceback (most recent call last):\n{frames}{type(exc).__name__}: <message omitted>\n"
        else:
            text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        message = f"Unexpected error in {where}:\n{redact_secrets(text)}"
        write_stderr(message if message.endswith("\n") else message + "\n")
    except Exception:  # keeping the diagnosis must never be the thing that breaks the run
        pass


def write_stderr(text: str) -> None:
    """One write to the operator's stderr, whole, so lines of parallel passes do not interleave."""
    stream = _stream if _stream is not None else sys.stderr
    with _write_lock:
        stream.write(redact_secrets(text))
        stream.flush()


def keep_exception(exc: BaseException, where: str, *, omit_message: bool = False) -> dict[str, str]:
    """Emit the traceback of *exc* and return its kind and location for the stage report."""
    emit_traceback(exc, where, omit_message=omit_message)
    return exception_facts(exc)
