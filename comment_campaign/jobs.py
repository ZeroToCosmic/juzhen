"""Importable RQ entry points.  Their arguments deliberately contain IDs only."""

from __future__ import annotations

import asyncio
import inspect


def _service():
    from .worker import build_runtime_service

    return build_runtime_service()


def _call(service, method: str, *args):
    operation = getattr(service, method, None)
    if not callable(operation):
        raise RuntimeError(f"Comment Campaign job operation unavailable: {method}")
    return operation(*args)


def _run(method: str, *args):
    service = _service()
    async def operation():
        candidate = {"prepare_campaign": "job_prepare_campaign", "submit_assignment": "job_submit_assignment"}.get(method)
        async_method = candidate if candidate and callable(getattr(service, candidate, None)) else method
        try:
            value = _call(service, async_method or method, *args)
            return await value if inspect.isawaitable(value) else value
        finally:
            close = getattr(service, "aclose", None)
            if callable(close):
                value = close()
                if inspect.isawaitable(value):
                    await value
            else:
                close = getattr(service, "close", None)
                if callable(close):
                    close()
    return asyncio.run(operation())


def run_prepare_campaign(campaign_id: str):
    return _run("prepare_campaign", campaign_id)


def run_submit_assignment(campaign_id: str, assignment_id: str, revision: int):
    return _run("submit_assignment", campaign_id, assignment_id, int(revision))


def run_reconcile_campaign(campaign_id: str):
    return _run("reconcile_campaign", campaign_id)
