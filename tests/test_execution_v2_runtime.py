import asyncio
import threading

import pytest

from execution_v2.runtime import AsyncRuntime, AsyncRuntimeClosedError


def test_runtime_uses_one_daemon_loop_and_closes_idempotently():
    runtime = AsyncRuntime()

    async def identify():
        return threading.current_thread().name, id(asyncio.get_running_loop())

    first = runtime.submit(identify()).result(timeout=2)
    second = runtime.submit(identify()).result(timeout=2)

    assert first == second
    assert runtime.thread.daemon is True
    runtime.close()
    runtime.close()
    assert not runtime.thread.is_alive()
    with pytest.raises(AsyncRuntimeClosedError):
        runtime.submit(identify())
