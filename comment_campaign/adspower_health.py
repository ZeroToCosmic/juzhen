"""Bounded, single-flight AdsPower Local API reachability probe."""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from threading import Lock

import requests

from adspower import AdsPowerDependencyError


SAFE_REASONS = frozenset({"connected", "timeout", "connection_refused", "authentication_failed", "invalid_response", "not_configured"})


def _is_explicit_connection_refused(error: BaseException) -> bool:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ConnectionRefusedError):
            return True
        if isinstance(current, OSError) and getattr(current, "winerror", None) == 10061:
            return True
        if isinstance(current, OSError) and getattr(current, "errno", None) in {61, 111, 10061}:
            return True
        for nested in (current.__cause__, current.__context__, *current.args):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


def safe_health_reason(error: BaseException) -> str:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, AdsPowerDependencyError):
            return current.reason
        if isinstance(current, requests.Timeout):
            return "timeout"
        if _is_explicit_connection_refused(current):
            return "connection_refused"
        if isinstance(current, requests.HTTPError):
            response = getattr(current, "response", None)
            if getattr(response, "status_code", None) in {401, 403}:
                return "authentication_failed"
        current = current.__cause__
    return "invalid_response"


class AdsPowerHealthProbe:
    def __init__(self, controller_factory, settings_provider, timeout_seconds: float = 4.0):
        self._controller_factory = controller_factory
        self._settings_provider = settings_provider
        self._timeout_seconds = float(timeout_seconds)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="adspower-health")
        self._lock = Lock()
        self._future = None

    def probe(self) -> dict[str, str]:
        with self._lock:
            if self._future is None or self._future.done():
                self._future = self._executor.submit(self._probe_once)
            future = self._future
        try:
            return dict(future.result(timeout=self._timeout_seconds))
        except FutureTimeoutError:
            return {"status": "unavailable", "reason": "timeout"}

    def _probe_once(self) -> dict[str, str]:
        try:
            config = self._settings_provider()
            if config is None:
                return {"status": "unavailable", "reason": "not_configured"}
            controller = self._controller_factory(base_url=config.base_url, api_key=config.api_key, timeout=self._timeout_seconds, max_retries=1, retry_delay=0)
            rows = controller.list_profiles(page=1, page_size=1)
            if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
                raise ValueError("invalid response shape")
            return {"status": "connected", "reason": "connected"}
        except Exception as error:
            return {"status": "unavailable", "reason": safe_health_reason(error)}

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
