"""Bounded daemon workers shared by production and diagnostics.

Running workers are daemon threads so a late bounded attempt cannot block
process exit.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any

_WORKER_JOIN_TIMEOUT_SECONDS = 5.0


class CancellationToken:
    """Thread-safe cancellation state used to gate late results."""

    def __init__(self) -> None:
        self._cancelled = False
        self._lock = threading.Lock()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled


@dataclass(frozen=True)
class _WorkItem:
    function: Any
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    future: DaemonFuture


class _ShutdownSentinel:
    pass


class DaemonFuture:
    """A small Future-compatible result used by the bounded pool."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._result: Any = None
        self._exception: BaseException | None = None
        self._cancelled = False
        self._callbacks: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []
        self._callbacks_lock = threading.Lock()

    def add_done_callback(self, function: Any, *args: Any, **kwargs: Any) -> None:
        with self._callbacks_lock:
            if self._event.is_set():
                try:
                    function(*args, **kwargs)
                except Exception:
                    pass
                return
            self._callbacks.append((function, args, kwargs))

    def _invoke_callbacks(self) -> None:
        with self._callbacks_lock:
            callbacks = list(self._callbacks)
            self._callbacks.clear()
        for function, args, kwargs in callbacks:
            try:
                function(*args, **kwargs)
            except Exception:
                pass

    def result(self, timeout: float | None = None) -> Any:
        if not self._event.wait(timeout=timeout):
            raise TimeoutError()
        if self._exception is not None:
            raise self._exception
        return self._result

    def done(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> bool:
        if self._event.is_set():
            return False
        self._cancelled = True
        self._event.set()
        self._invoke_callbacks()
        return True

    def _set_result(self, result: Any) -> None:
        self._result = result
        self._event.set()
        self._invoke_callbacks()

    def _set_exception(self, exception: BaseException) -> None:
        self._exception = exception
        self._event.set()
        self._invoke_callbacks()


class DaemonThreadPool:
    """A bounded daemon pool with cancellable queued work."""

    def __init__(self, max_workers: int, thread_name_prefix: str = "") -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self._work_queue: queue.SimpleQueue[_WorkItem | _ShutdownSentinel] = queue.SimpleQueue()
        self._shutdown_sentinel = _ShutdownSentinel()
        self._threads: list[threading.Thread] = []
        for index in range(max_workers):
            thread = threading.Thread(
                target=self._worker_loop,
                name=f"{thread_name_prefix}{index}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def submit(self, function: Any, *args: Any, **kwargs: Any) -> DaemonFuture:
        future = DaemonFuture()
        self._work_queue.put(_WorkItem(function, args, kwargs, future))
        return future

    def shutdown(self, *, wait: bool = False, cancel_futures: bool = False) -> None:
        if cancel_futures:
            while True:
                try:
                    item = self._work_queue.get(block=False)
                except queue.Empty:
                    break
                if isinstance(item, _WorkItem) and not item.future.done():
                    item.future._set_exception(RuntimeError("cancelled"))
        for _ in self._threads:
            self._work_queue.put(self._shutdown_sentinel)
        if wait:
            for thread in self._threads:
                thread.join(timeout=_WORKER_JOIN_TIMEOUT_SECONDS)

    def _worker_loop(self) -> None:
        while True:
            item = self._work_queue.get()
            if isinstance(item, _ShutdownSentinel):
                return
            if item.future._cancelled:
                continue
            try:
                result = item.function(*item.args, **item.kwargs)
            except BaseException as exception:
                item.future._set_exception(exception)
            else:
                item.future._set_result(result)


__all__ = ["CancellationToken", "DaemonFuture", "DaemonThreadPool"]
