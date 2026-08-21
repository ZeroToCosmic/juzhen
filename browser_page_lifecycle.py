from __future__ import annotations

import asyncio
import inspect
import time
from urllib.parse import urlsplit


CLOSED_TARGET_MARKERS = (
    "target page, context or browser has been closed",
    "page closed",
    "context closed",
    "browser has been closed",
    "target closed",
    "target detached",
)
SAFE_RETRY_ACTIONS = frozenset({"move", "scroll_up", "scroll_down", "pause"})
WHOLE_ACTION_RETRY_ACTIONS = frozenset({"move"})


def is_closed_target_error(error: BaseException) -> bool:
    message = str(error).casefold()
    return any(marker in message for marker in CLOSED_TARGET_MARKERS)


def page_origin(page) -> str:
    try:
        parsed = urlsplit(str(page.url or ""))
        port = f":{parsed.port}" if parsed.port else ""
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def closed_target_diagnostic(error: BaseException) -> tuple[str, str]:
    message = str(error).casefold()
    classifications = (
        ("browser disconnected", "browser_disconnected"),
        (
            "target page, context or browser has been closed",
            "target_closed",
        ),
        ("browser has been closed", "browser_closed"),
        ("context closed", "context_closed"),
        ("page closed", "page_closed"),
        ("target detached", "target_detached"),
        ("target closed", "target_closed"),
    )
    for marker, closure_type in classifications:
        if marker in message:
            return closure_type, marker
    return "target_closed", "closed target"


def page_recovery_event(
    action,
    error,
    old_origin,
    *,
    replacement=None,
    retry=0,
    status,
    outcome,
):
    closure_type, closure_reason = closed_target_diagnostic(error)
    return {
        "action_id": action["id"],
        "action_type": action["type"],
        "old_page_origin": old_origin,
        "new_page_origin": page_origin(replacement) if replacement is not None else "",
        "closure_type": closure_type,
        "closure_reason": closure_reason,
        "replacement_found": replacement is not None,
        "retry": retry,
        "status": status,
        "outcome": outcome,
    }


def set_page_recoveries(error: BaseException, events) -> BaseException:
    recoveries = list(events)
    try:
        error.page_recoveries = recoveries
        return error
    except (AttributeError, TypeError):
        wrapped = RuntimeError(str(error))
        wrapped.page_recoveries = recoveries
        return wrapped


def attach_page_recoveries(error: BaseException, events) -> BaseException:
    recoveries = list(getattr(error, "page_recoveries", []))
    recoveries.extend(events)
    return set_page_recoveries(error, recoveries)


def attach_page_recovery(error: BaseException, event: dict) -> BaseException:
    return attach_page_recoveries(error, [event])


def page_unavailable_error(context, page) -> RuntimeError:
    browser = getattr(context, "browser", None)
    checker = getattr(browser, "is_connected", None)
    if callable(checker) and not checker():
        return RuntimeError("browser disconnected")
    try:
        if page.is_closed():
            return RuntimeError("page closed")
    except Exception:
        pass
    if page not in list(getattr(context, "pages", [])):
        return RuntimeError("target closed")
    return RuntimeError("target closed")


class PageLifecycle:
    def __init__(
        self,
        context,
        target_url,
        *,
        timeout_seconds=3.0,
        sleep_fn=asyncio.sleep,
        monotonic_fn=time.monotonic,
    ):
        self.context = context
        self.target_url = str(target_url)
        self.target_host = (urlsplit(self.target_url).hostname or "").casefold()
        self.timeout_seconds = float(timeout_seconds)
        self.sleep_fn = sleep_fn
        self.monotonic_fn = monotonic_fn

    def _browser_connected(self):
        browser = getattr(self.context, "browser", None)
        checker = getattr(browser, "is_connected", None)
        return browser is None or not callable(checker) or bool(checker())

    @staticmethod
    def _open_pages(pages):
        return [
            page
            for page in pages
            if not bool(getattr(page, "is_closed", lambda: False)())
        ]

    async def _visible(self, page):
        try:
            return await page.evaluate("document.visibilityState") == "visible"
        except Exception:
            return False

    async def _pick(self, current=None, *, allow_blank=False, excluded_pages=()):
        pages = self._open_pages(list(getattr(self.context, "pages", [])))
        pages = [
            page
            for page in pages
            if not any(page is excluded for excluded in excluded_pages)
        ]
        if current in pages and page_origin(current):
            return current
        usable = [page for page in pages if page_origin(page)]
        target = [
            page
            for page in usable
            if (urlsplit(str(page.url)).hostname or "").casefold() == self.target_host
        ]
        candidates = target or usable
        for page in reversed(candidates):
            if await self._visible(page):
                return page
        if candidates:
            return candidates[-1]
        return pages[-1] if allow_blank and pages else None

    async def resolve(self, current=None, *, allow_blank=False):
        return await self._resolve(current, allow_blank=allow_blank)

    async def resolve_replacement(self, failed_page):
        return await self._resolve(None, excluded_pages=(failed_page,))

    async def _resolve(
        self,
        current=None,
        *,
        allow_blank=False,
        excluded_pages=(),
        deadline=None,
    ):
        resolution_deadline = self.monotonic_fn() + self.timeout_seconds
        if deadline is not None:
            resolution_deadline = min(resolution_deadline, float(deadline))
        while True:
            if not self._browser_connected():
                raise RuntimeError("browser disconnected")
            page = await self._pick(
                current,
                allow_blank=allow_blank,
                excluded_pages=excluded_pages,
            )
            if page is not None:
                return page
            remaining = resolution_deadline - self.monotonic_fn()
            if remaining <= 0:
                raise RuntimeError("no active page available before timeout")
            try:
                await asyncio.wait_for(
                    self.sleep_fn(min(0.1, remaining)),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "no active page available before timeout"
                ) from None
            current = None

    async def observe(
        self,
        current,
        action,
        callback,
        *,
        timeout_seconds,
    ):
        """Observe a postcondition across page replacement without re-running the action."""

        deadline = self.monotonic_fn() + float(timeout_seconds)
        recoveries = []
        while True:
            requested = current
            remaining = deadline - self.monotonic_fn()
            if remaining <= 0:
                return current, False, recoveries
            try:
                current = await asyncio.wait_for(
                    self._resolve(current, deadline=deadline),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                event = page_recovery_event(
                    action,
                    page_unavailable_error(self.context, requested),
                    page_origin(requested),
                    status="failed",
                    outcome="replacement_not_found",
                )
                recoveries.append(event)
                return requested, False, recoveries
            except Exception:
                event = page_recovery_event(
                    action,
                    page_unavailable_error(self.context, requested),
                    page_origin(requested),
                    status="failed",
                    outcome="replacement_not_found",
                )
                recoveries.append(event)
                return requested, False, recoveries
            if requested is not None and current is not requested:
                recoveries.append(
                    page_recovery_event(
                        action,
                        page_unavailable_error(self.context, requested),
                        page_origin(requested),
                        replacement=current,
                        status="recovered",
                        outcome="recovered",
                    )
                )
            try:
                observed = callback(current)
                if inspect.isawaitable(observed):
                    remaining = deadline - self.monotonic_fn()
                    if remaining <= 0:
                        closer = getattr(observed, "close", None)
                        if callable(closer):
                            closer()
                        return current, False, recoveries
                    observed = await asyncio.wait_for(
                        observed,
                        timeout=remaining,
                    )
            except asyncio.TimeoutError:
                return current, False, recoveries
            except Exception as error:
                if not is_closed_target_error(error):
                    if recoveries:
                        raise attach_page_recoveries(error, recoveries)
                    raise
                old_origin = page_origin(current)
                remaining = deadline - self.monotonic_fn()
                if remaining <= 0:
                    return current, False, recoveries
                try:
                    replacement = await asyncio.wait_for(
                        self._resolve(
                            None,
                            excluded_pages=(current,),
                            deadline=deadline,
                        ),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    event = page_recovery_event(
                        action,
                        error,
                        old_origin,
                        status="failed",
                        outcome="replacement_not_found",
                    )
                    recoveries.append(event)
                    return current, False, recoveries
                except Exception:
                    event = page_recovery_event(
                        action,
                        error,
                        old_origin,
                        status="failed",
                        outcome="replacement_not_found",
                    )
                    recoveries.append(event)
                    return current, False, recoveries
                recoveries.append(
                    page_recovery_event(
                        action,
                        error,
                        old_origin,
                        replacement=replacement,
                        status="recovered",
                        outcome="recovered",
                    )
                )
                current = replacement
                continue
            if observed and self.monotonic_fn() <= deadline:
                return current, True, recoveries
            remaining = deadline - self.monotonic_fn()
            if remaining <= 0:
                return current, False, recoveries
            try:
                await asyncio.wait_for(
                    self.sleep_fn(min(0.1, remaining)),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                return current, False, recoveries

    async def execute(self, current, action, callback):
        requested_page = current
        recoveries = []
        boundary_error = (
            page_unavailable_error(self.context, requested_page)
            if requested_page is not None
            else None
        )
        try:
            current = await self.resolve(current)
        except Exception as resolution_error:
            if requested_page is not None:
                event = page_recovery_event(
                    action,
                    boundary_error,
                    page_origin(requested_page),
                    status="failed",
                    outcome="replacement_not_found",
                )
                raise attach_page_recoveries(resolution_error, [event])
            raise
        if requested_page is not None and current is not requested_page:
            recoveries.append(
                page_recovery_event(
                    action,
                    boundary_error,
                    page_origin(requested_page),
                    replacement=current,
                    status="recovered",
                    outcome="recovered",
                )
            )
        try:
            result = callback(current)
            if inspect.isawaitable(result):
                result = await result
            return current, result, recoveries
        except Exception as error:
            nested_recoveries = list(
                getattr(error, "page_recoveries", [])
            )
            if nested_recoveries:
                raise set_page_recoveries(
                    error,
                    [*recoveries, *nested_recoveries],
                )
            closed_target = is_closed_target_error(error)
            if (
                action["type"] not in WHOLE_ACTION_RETRY_ACTIONS
                or not closed_target
            ):
                if closed_target:
                    event = page_recovery_event(
                        action,
                        error,
                        page_origin(current),
                        status="failed",
                        outcome="not_retried",
                    )
                    recoveries.append(event)
                if recoveries:
                    raise attach_page_recoveries(error, recoveries)
                raise
            old_origin = page_origin(current)
            try:
                replacement = await self.resolve_replacement(current)
            except Exception as replacement_error:
                event = page_recovery_event(
                    action,
                    error,
                    old_origin,
                    status="failed",
                    outcome="replacement_not_found",
                )
                recoveries.append(event)
                raise attach_page_recoveries(replacement_error, recoveries)
            try:
                result = callback(replacement)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as retry_error:
                event = page_recovery_event(
                    action,
                    error,
                    old_origin,
                    replacement=replacement,
                    retry=1,
                    status="failed",
                    outcome="retry_failed",
                )
                recoveries.append(event)
                raise attach_page_recoveries(retry_error, recoveries)
            recoveries.append(
                page_recovery_event(
                    action,
                    error,
                    old_origin,
                    replacement=replacement,
                    retry=1,
                    status="recovered",
                    outcome="recovered",
                )
            )
            return replacement, result, recoveries


async def prepare_target_page(
    lifecycle,
    target_url,
    *,
    wait_milliseconds=2000,
):
    page = await lifecycle.resolve(allow_blank=True)
    closed_tabs = 0
    for other in list(lifecycle.context.pages):
        if other is not page and not other.is_closed():
            await other.close()
            closed_tabs += 1
    await page.goto(target_url, wait_until="commit", timeout=30_000)
    if wait_milliseconds > 0:
        await page.wait_for_timeout(wait_milliseconds)
    page = await lifecycle.resolve(page)
    for other in list(lifecycle.context.pages):
        if other is not page and not other.is_closed():
            await other.close()
            closed_tabs += 1
    return page, {
        "target_url": target_url,
        "current_url": str(page.url or ""),
        "closed_tabs": closed_tabs,
        "stages": [
            {"stage": "wait_for_cdp", "status": "ok"},
            {"stage": "close_other_tabs", "status": "ok", "closed_tabs": closed_tabs},
            {
                "stage": "navigate",
                "status": "ok",
                "target_url": target_url,
                "current_url": str(page.url or ""),
            },
        ],
    }
