"""One owned asyncio loop for Flask-facing browser execution V2 services.

Playwright's async objects are tied to the event loop that created them.  This
small bridge is deliberately the only place that creates a V2 background loop.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
import inspect
import threading
from collections.abc import Coroutine
from typing import Any


class AsyncRuntimeClosedError(RuntimeError):
    """A caller tried to submit browser work after shutdown."""


class AsyncRuntime:
    """A daemon thread with one asyncio loop, safe to call from Flask threads."""

    def __init__(self, *, name: str = "execution-v2-runtime") -> None:
        self._name = name
        self._closed = False
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("execution_v2_runtime_start_timeout")

    @property
    def thread(self) -> threading.Thread:
        return self._thread

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if loop is None:
            raise RuntimeError("execution_v2_runtime_not_ready")
        return loop

    def submit(self, coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        """Schedule one coroutine on the owned loop without crossing objects."""

        if not inspect.iscoroutine(coroutine):
            raise TypeError("submit requires a coroutine object")
        with self._lock:
            if self._closed:
                coroutine.close()
                raise AsyncRuntimeClosedError("execution_v2_runtime_closed")
            return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def close(self, *, timeout: float = 5) -> None:
        """Stop once and cancel all remaining V2 loop tasks."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()


AsyncBridge = AsyncRuntime


__all__ = ["AsyncBridge", "AsyncRuntime", "AsyncRuntimeClosedError"]
