"""Rate-limited async boundary around the AdsPower Local API controller."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from adspower import AdsPowerController


class AdsPowerAdapter(Protocol):
    """The small AdsPower surface used by the V2 scheduler."""

    async def start(self, profile_id: str) -> str: ...

    async def stop(self, profile_id: str) -> None: ...

    async def is_active(self, profile_id: str) -> bool: ...


class AdsPowerStateError(RuntimeError):
    """AdsPower returned a state that cannot safely confirm browser closure."""


class RateLimitedAdsPowerAdapter:
    """Serialize AdsPower calls and space their starts by at least one second."""

    def __init__(
        self,
        controller: AdsPowerController,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        minimum_interval: float = 1.0,
    ) -> None:
        if minimum_interval < 1.0:
            raise ValueError("minimum_interval must be at least one second")
        self._controller = controller
        self._clock = clock
        self._sleep = sleep
        self._minimum_interval = minimum_interval
        self._lock = asyncio.Lock()
        self._last_call_started_at: float | None = None

    async def start(self, profile_id: str) -> str:
        result = await self._call("start_browser", profile_id)
        if not isinstance(result, str) or not result:
            raise RuntimeError("AdsPower start did not return a CDP endpoint")
        return result

    async def stop(self, profile_id: str) -> None:
        await self._call("stop_browser", profile_id)

    async def is_active(self, profile_id: str) -> bool:
        result = await self._call("get_browser_active", profile_id)
        if not isinstance(result, dict):
            raise AdsPowerStateError("AdsPower active response is not an object")
        status = result.get("status")
        if not isinstance(status, str) or not status.strip():
            raise AdsPowerStateError("AdsPower active response has no status")
        normalized = status.strip().casefold()
        if normalized == "active":
            return True
        if normalized in {"inactive", "stopped"}:
            return False
        raise AdsPowerStateError("AdsPower active response has an unknown status")

    async def _call(self, method_name: str, profile_id: str) -> Any:
        async with self._lock:
            if self._last_call_started_at is not None:
                wait_seconds = self._minimum_interval - (
                    self._clock() - self._last_call_started_at
                )
                if wait_seconds > 0:
                    await self._sleep(wait_seconds)
            self._last_call_started_at = self._clock()
            method = getattr(self._controller, method_name)
            return await asyncio.to_thread(method, profile_id)


__all__ = ["AdsPowerAdapter", "AdsPowerStateError", "RateLimitedAdsPowerAdapter"]
