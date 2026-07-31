"""Verified video-switch scrolling for a vertically paged feed."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import time
from dataclasses import dataclass

from browser_page_lifecycle import (
    attach_page_recoveries,
    is_closed_target_error,
    page_origin,
    page_recovery_event,
)


WHEEL_DELTA = 120
STABILITY_TOLERANCE_PX = 2.0
STATE_POLL_SECONDS = 0.05
PULSE_OBSERVATION_SECONDS = 0.45
SWITCH_TIMEOUT_SECONDS = 8.0
INTERVAL_GRACE_SECONDS = 5.0
_monotonic = time.monotonic


async def _run_before_side_effect(before_side_effect) -> None:
    if before_side_effect is None:
        return
    result = before_side_effect()
    if inspect.isawaitable(result):
        await result


def _is_strategy_pause(error: BaseException) -> bool:
    return getattr(error, "code", None) == "strategy_paused_during_execution"


_PULSE_PROBE_INSTALL_SCRIPT = """() => {
    const key = '__codex_verified_scroll_pulse_probe__';
    const container = document.querySelector('#column-list-container');
    if (!container) {
        throw new Error('video feed container unavailable');
    }
    const finite = (value) => Number.isFinite(Number(value))
        ? Number(value)
        : null;
    const snapshot = () => {
        const containerRect = container.getBoundingClientRect();
        const centerY = containerRect.top + containerRect.height / 2;
        const articles = Array.from(container.querySelectorAll('article'));
        const article = articles.find((candidate) => {
            const rect = candidate.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0
                && rect.top <= centerY && rect.bottom >= centerY;
        }) || null;
        let identity = null;
        let identitySource = 'none';
        let articleRect = null;
        if (article) {
            articleRect = article.getBoundingClientRect();
            for (const link of article.querySelectorAll('a[href]')) {
                const match = String(link.getAttribute('href') || '')
                    .match(/\\/video\\/(\\d+)/);
                if (match) {
                    identity = match[1];
                    identitySource = 'video_id';
                    break;
                }
            }
            if (identity === null && article.id) {
                identity = String(article.id);
                identitySource = 'article_id';
            }
            if (identity === null) {
                identity = String(articles.indexOf(article));
                identitySource = 'fallback';
            }
        }
        return {
            container_scroll_top: finite(container.scrollTop),
            window_scroll_y: finite(window.scrollY),
            article_top: articleRect ? finite(articleRect.top) : null,
            article_bottom: articleRect ? finite(articleRect.bottom) : null,
            article_center_offset: articleRect
                ? finite(
                    (articleRect.top + articleRect.bottom) / 2 - centerY
                )
                : null,
            identity_source: identitySource,
            identity,
        };
    };
    const state = {
        wheel_seen: false,
        target_in_container: false,
        default_prevented: false,
        mutation_count: 0,
        observer: null,
        on_wheel: null,
        snapshot,
    };
    state.on_wheel = (event) => {
        state.wheel_seen = true;
        state.target_in_container = Boolean(
            event.target instanceof Node && container.contains(event.target)
        );
        queueMicrotask(() => {
            state.default_prevented = Boolean(event.defaultPrevented);
        });
    };
    state.observer = new MutationObserver((records) => {
        state.mutation_count += records.length;
    });
    window.addEventListener('wheel', state.on_wheel, {capture: true});
    state.observer.observe(container, {childList: true, subtree: true});
    window[key] = state;
    return snapshot();
}"""

_PULSE_PROBE_CLEANUP_SCRIPT = """() => {
    const key = '__codex_verified_scroll_pulse_probe__';
    const state = window[key];
    if (!state) return null;
    try {
        return {
            wheel_seen: Boolean(state.wheel_seen),
            target_in_container: Boolean(state.target_in_container),
            default_prevented: Boolean(state.default_prevented),
            mutation_count: Math.max(0, Number(state.mutation_count) || 0),
            after: state.snapshot(),
        };
    } finally {
        state.observer.disconnect();
        window.removeEventListener('wheel', state.on_wheel, true);
        delete window[key];
    }
}"""


@dataclass(frozen=True)
class FeedState:
    fingerprint: str
    safe_fingerprint: str
    container_x: float
    container_y: float
    container_width: float
    container_height: float
    scroll_top: float


class VideoSwitchError(RuntimeError):
    """A sanitized switch failure with partial progress measurements."""

    def __init__(
        self,
        code,
        *,
        completed_switches=0,
        wheel_events=0,
        requested_switches=0,
        safe_fingerprint="",
        switches=(),
        pulse_diagnostics=None,
    ):
        super().__init__(str(code))
        self.code = str(code)
        self.completed_switches = int(completed_switches)
        self.wheel_events = int(wheel_events)
        self.requested_switches = int(requested_switches)
        self.safe_fingerprint = str(safe_fingerprint)
        self.switches = list(switches)
        if pulse_diagnostics is not None:
            self.pulse_diagnostics = list(pulse_diagnostics)


class _SwitchInterrupted(Exception):
    def __init__(
        self,
        cause,
        *,
        wheel_events,
        safe_fingerprint,
        pulse_diagnostics=None,
    ):
        super().__init__("video switch interrupted")
        self.cause = cause
        self.wheel_events = int(wheel_events)
        self.safe_fingerprint = str(safe_fingerprint)
        if pulse_diagnostics is not None:
            self.pulse_diagnostics = list(pulse_diagnostics)


class _SwitchDeadlineExceeded(Exception):
    pass


def _finite_number_or_none(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _identity_source(snapshot):
    source = str(dict(snapshot or {}).get("identity_source") or "none")
    return source if source in {"video_id", "article_id", "fallback"} else "none"


def _identity_hash(snapshot):
    snapshot = dict(snapshot or {})
    if _identity_source(snapshot) == "none":
        return None
    identity = snapshot.get("identity")
    if identity is None:
        return None
    return hashlib.sha256(str(identity).encode("utf-8")).hexdigest()[:12]


def _pulse_record(
    *,
    pulse_index,
    delta,
    before_snapshot,
    captured,
    poll_count,
    different_identity_seen,
):
    before_snapshot = dict(before_snapshot or {})
    captured = dict(captured or {})
    after_snapshot = dict(captured.get("after") or {})

    def number(snapshot, key):
        return _finite_number_or_none(snapshot.get(key))

    try:
        mutation_count = max(0, int(captured.get("mutation_count") or 0))
    except (TypeError, ValueError):
        mutation_count = 0
    return {
        "pulse_index": max(1, int(pulse_index)),
        "delta": int(delta),
        "wheel_seen": bool(captured.get("wheel_seen", False)),
        "target_in_container": bool(
            captured.get("target_in_container", False)
        ),
        "default_prevented": bool(captured.get("default_prevented", False)),
        "mutation_count": mutation_count,
        "poll_count": max(0, int(poll_count)),
        "different_identity_seen": bool(different_identity_seen),
        "container_scroll_top_before": number(
            before_snapshot,
            "container_scroll_top",
        ),
        "container_scroll_top_after": number(
            after_snapshot,
            "container_scroll_top",
        ),
        "window_scroll_y_before": number(before_snapshot, "window_scroll_y"),
        "window_scroll_y_after": number(after_snapshot, "window_scroll_y"),
        "article_top_before": number(before_snapshot, "article_top"),
        "article_top_after": number(after_snapshot, "article_top"),
        "article_bottom_before": number(before_snapshot, "article_bottom"),
        "article_bottom_after": number(after_snapshot, "article_bottom"),
        "article_center_offset_before": number(
            before_snapshot,
            "article_center_offset",
        ),
        "article_center_offset_after": number(
            after_snapshot,
            "article_center_offset",
        ),
        "identity_source_before": _identity_source(before_snapshot),
        "identity_source_after": _identity_source(after_snapshot),
        "identity_hash_before": _identity_hash(before_snapshot),
        "identity_hash_after": _identity_hash(after_snapshot),
    }


async def _finish_pulse_probe(page):
    cleanup_task = asyncio.create_task(
        page.evaluate(_PULSE_PROBE_CLEANUP_SCRIPT)
    )
    cancellation_received = False
    captured = None
    try:
        try:
            captured = await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            if cleanup_task.cancelled():
                raise
            cancellation_received = True
            if cleanup_task.done():
                try:
                    captured = cleanup_task.result()
                except Exception:
                    captured = None
            else:
                try:
                    captured = await asyncio.shield(cleanup_task)
                except Exception:
                    captured = None
        except Exception:
            captured = None
    finally:
        cleanup_task = None
    return captured, cancellation_received


async def _run_diagnostic_pulse(
    page,
    *,
    deadline,
    pulse_index,
    delta,
    pulse_diagnostics,
    operation,
):
    before_snapshot = None
    evidence = {
        "poll_count": 0,
        "different_identity_seen": False,
    }
    captured = None
    try:
        before_snapshot = await _within_deadline(
            page.evaluate(_PULSE_PROBE_INSTALL_SCRIPT),
            deadline,
        )
        return await operation(evidence)
    finally:
        cancellation_received = False
        try:
            captured, cancellation_received = await _finish_pulse_probe(page)
            if before_snapshot is not None:
                pulse_diagnostics.append(
                    _pulse_record(
                        pulse_index=pulse_index,
                        delta=delta,
                        before_snapshot=before_snapshot,
                        captured=captured,
                        poll_count=evidence["poll_count"],
                        different_identity_seen=evidence[
                            "different_identity_seen"
                        ],
                    )
                )
        finally:
            before_snapshot = None
            captured = None
        if cancellation_received:
            raise asyncio.CancelledError() from None


async def _within_deadline(awaitable, deadline):
    remaining = deadline - _monotonic()
    if remaining <= 0:
        closer = getattr(awaitable, "close", None)
        if callable(closer):
            closer()
        raise _SwitchDeadlineExceeded()
    try:
        return await asyncio.wait_for(awaitable, timeout=remaining)
    except asyncio.TimeoutError:
        raise _SwitchDeadlineExceeded() from None


async def capture_feed_state(page) -> FeedState:
    """Capture the container and the article crossing its vertical center."""

    captured = await page.evaluate(
        """() => {
            const container = document.querySelector('#column-list-container');
            if (!container) {
                throw new Error('video feed container unavailable');
            }
            const containerRect = container.getBoundingClientRect();
            const centerY = containerRect.top + containerRect.height / 2;
            const articles = Array.from(container.querySelectorAll('article'));
            const visibleArticles = articles.filter((article) => {
                const rect = article.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0
                    && rect.top <= centerY && rect.bottom >= centerY;
            });
            const article = visibleArticles[0];
            if (!article) {
                throw new Error('active video article unavailable');
            }
            const hrefs = Array.from(article.querySelectorAll('a[href]'))
                .map((link) => String(link.getAttribute('href') || ''));
            let videoId = '';
            for (const href of hrefs) {
                const match = href.match(/\\/video\\/(\\d+)/);
                if (match) {
                    videoId = match[1];
                    break;
                }
            }
            const stableAttributes = {};
            for (const name of ['data-e2e', 'data-testid', 'aria-label', 'role']) {
                const value = article.getAttribute(name);
                if (value) stableAttributes[name] = value;
            }
            return {
                video_id: videoId,
                article_id: String(article.id || ''),
                stable_attributes: stableAttributes,
                visible_index: articles.indexOf(article),
                container_x: containerRect.x,
                container_y: containerRect.y,
                container_width: containerRect.width,
                container_height: containerRect.height,
                scroll_top: Number(container.scrollTop || 0),
            };
        }"""
    )
    identity = _feed_identity(captured)
    return FeedState(
        fingerprint=identity,
        safe_fingerprint=hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12],
        container_x=float(captured["container_x"]),
        container_y=float(captured["container_y"]),
        container_width=float(captured["container_width"]),
        container_height=float(captured["container_height"]),
        scroll_top=float(captured["scroll_top"]),
    )


def _feed_identity(captured) -> str:
    if "identity" in captured:
        return str(captured["identity"])
    video_id = str(captured.get("video_id") or "")
    if video_id.isdigit():
        return f"video:{video_id}"
    article_id = str(captured.get("article_id") or "")
    if article_id:
        return f"article:{article_id}"
    stable_attributes = {
        str(key): str(value)
        for key, value in dict(captured.get("stable_attributes") or {}).items()
    }
    serialized = json.dumps(
        stable_attributes,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"fallback:{serialized}:{int(captured['visible_index'])}"


async def wait_for_stable_changed_state(
    page,
    before,
    *,
    timeout=SWITCH_TIMEOUT_SECONDS,
    sleep_fn=asyncio.sleep,
):
    """Return a changed center state only after two stable consecutive polls."""

    deadline = _monotonic() + float(timeout)
    try:
        return await _wait_for_stable_changed_state_until(
            page,
            before,
            deadline=deadline,
            sleep_fn=sleep_fn,
        )
    except _SwitchDeadlineExceeded:
        return None


async def _wait_for_stable_changed_state_until(
    page,
    before,
    *,
    deadline,
    sleep_fn,
    diagnostic_evidence=None,
):
    candidate = None
    while _monotonic() < deadline:
        if diagnostic_evidence is not None:
            diagnostic_evidence["poll_count"] += 1
        try:
            current = await _within_deadline(capture_feed_state(page), deadline)
        except _SwitchDeadlineExceeded:
            return None
        if current.fingerprint == before.fingerprint:
            candidate = None
        else:
            if diagnostic_evidence is not None:
                diagnostic_evidence["different_identity_seen"] = True
            if (
                candidate is not None
                and current.fingerprint == candidate.fingerprint
                and abs(current.scroll_top - candidate.scroll_top)
                <= STABILITY_TOLERANCE_PX
            ):
                return current
            candidate = current
        remaining = deadline - _monotonic()
        if remaining <= 0:
            return None
        try:
            await _within_deadline(
                sleep_fn(min(STATE_POLL_SECONDS, remaining)),
                deadline,
            )
        except _SwitchDeadlineExceeded:
            return None
    return None


async def switch_once(
    page,
    direction,
    *,
    sleep_fn,
    diagnostic=False,
    before_side_effect=None,
):
    """Send bounded wheel pulses until one different stable video is observed."""

    deadline = _monotonic() + SWITCH_TIMEOUT_SECONDS
    successful_pulses = 0
    pulse_diagnostics = []
    try:
        before = await _within_deadline(capture_feed_state(page), deadline)
        await _run_before_side_effect(before_side_effect)
        await _within_deadline(
            page.mouse.move(
                before.container_x + before.container_width / 2,
                before.container_y + before.container_height / 2,
            ),
            deadline,
        )
    except _SwitchDeadlineExceeded:
        raise VideoSwitchError(
            "video_switch_timeout",
            wheel_events=successful_pulses,
            pulse_diagnostics=pulse_diagnostics if diagnostic else None,
        ) from None
    except Exception as error:
        if _is_strategy_pause(error):
            raise
        raise _SwitchInterrupted(
            error,
            wheel_events=successful_pulses,
            safe_fingerprint="",
            pulse_diagnostics=pulse_diagnostics if diagnostic else None,
        ) from None
    max_pulses = min(
        24,
        max(4, math.ceil(before.container_height / WHEEL_DELTA) + 4),
    )
    delta = WHEEL_DELTA if direction == "down" else -WHEEL_DELTA
    for pulse_index in range(1, max_pulses + 1):
        async def dispatch_and_observe(diagnostic_evidence=None):
            nonlocal successful_pulses
            await _run_before_side_effect(before_side_effect)
            await _within_deadline(page.mouse.wheel(0, delta), deadline)
            successful_pulses += 1
            pulse_deadline = min(
                deadline,
                _monotonic() + PULSE_OBSERVATION_SECONDS,
            )
            return await _wait_for_stable_changed_state_until(
                page,
                before,
                deadline=pulse_deadline,
                sleep_fn=sleep_fn,
                diagnostic_evidence=diagnostic_evidence,
            )

        try:
            if diagnostic:
                after = await _run_diagnostic_pulse(
                    page,
                    deadline=deadline,
                    pulse_index=pulse_index,
                    delta=delta,
                    pulse_diagnostics=pulse_diagnostics,
                    operation=dispatch_and_observe,
                )
            else:
                after = await dispatch_and_observe()
        except _SwitchDeadlineExceeded:
            raise VideoSwitchError(
                "video_switch_timeout",
                wheel_events=successful_pulses,
                safe_fingerprint=before.safe_fingerprint,
                pulse_diagnostics=pulse_diagnostics if diagnostic else None,
            ) from None
        except Exception as error:
            if _is_strategy_pause(error):
                raise
            raise _SwitchInterrupted(
                error,
                wheel_events=successful_pulses,
                safe_fingerprint=before.safe_fingerprint,
                pulse_diagnostics=pulse_diagnostics if diagnostic else None,
            ) from None
        if after is not None:
            if diagnostic:
                return before, after, successful_pulses, pulse_diagnostics
            return before, after, successful_pulses
        if _monotonic() >= deadline:
            raise VideoSwitchError(
                "video_switch_timeout",
                wheel_events=successful_pulses,
                safe_fingerprint=before.safe_fingerprint,
                pulse_diagnostics=pulse_diagnostics if diagnostic else None,
            )
    raise VideoSwitchError(
        "video_switch_not_observed",
        wheel_events=successful_pulses,
        safe_fingerprint=before.safe_fingerprint,
        pulse_diagnostics=pulse_diagnostics if diagnostic else None,
    )


async def execute_verified_switches(
    page,
    *,
    direction,
    requested,
    interval_range,
    lifecycle,
    rng,
    sleep_fn,
    diagnostic=False,
    before_side_effect=None,
):
    """Complete exactly ``requested`` verified switches."""

    if direction not in {"up", "down"}:
        raise ValueError("direction must be 'up' or 'down'")
    requested = int(requested)
    completed = 0
    wheel_events = 0
    records = []
    pulse_diagnostics = []
    recoveries = []
    recovery_used = False
    pending_recovery = None
    recovery_action = {
        "id": "verified-video-switch",
        "type": f"scroll_{direction}",
    }

    def failure(code, *, safe_fingerprint=""):
        error = VideoSwitchError(
            code,
            completed_switches=completed,
            wheel_events=wheel_events,
            requested_switches=requested,
            safe_fingerprint=safe_fingerprint,
            switches=records,
            pulse_diagnostics=pulse_diagnostics if diagnostic else None,
        )
        return attach_page_recoveries(error, recoveries) if recoveries else error

    def add_pulse_diagnostics(incoming):
        if not diagnostic:
            return
        for incoming_record in incoming:
            record = dict(incoming_record)
            record["pulse_index"] = len(pulse_diagnostics) + 1
            pulse_diagnostics.append(record)

    while completed < requested:
        try:
            switched = await switch_once(
                page,
                direction,
                sleep_fn=sleep_fn,
                diagnostic=diagnostic,
                before_side_effect=before_side_effect,
            )
            if diagnostic:
                before, after, pulses, switch_diagnostics = switched
                add_pulse_diagnostics(switch_diagnostics)
            else:
                before, after, pulses = switched
        except _SwitchInterrupted as interrupted:
            add_pulse_diagnostics(
                getattr(interrupted, "pulse_diagnostics", ())
            )
            wheel_events += interrupted.wheel_events
            cause = interrupted.cause
            if not is_closed_target_error(cause):
                if pending_recovery is not None:
                    original, old_origin, replacement = pending_recovery
                    recoveries.append(
                        page_recovery_event(
                            recovery_action,
                            original,
                            old_origin,
                            replacement=replacement,
                            retry=1,
                            status="failed",
                            outcome="retry_failed",
                        )
                    )
                    pending_recovery = None
                raise failure(
                    "video_switch_state_capture_failed",
                    safe_fingerprint=interrupted.safe_fingerprint,
                ) from None
            if lifecycle is None or recovery_used:
                if pending_recovery is not None:
                    original, old_origin, replacement = pending_recovery
                    recoveries.append(
                        page_recovery_event(
                            recovery_action,
                            original,
                            old_origin,
                            replacement=replacement,
                            retry=1,
                            status="failed",
                            outcome="retry_failed",
                        )
                    )
                    pending_recovery = None
                elif lifecycle is not None:
                    recoveries.append(
                        page_recovery_event(
                            recovery_action,
                            cause,
                            page_origin(page),
                            status="failed",
                            outcome="not_retried",
                        )
                    )
                raise failure(
                    "video_switch_closed_target",
                    safe_fingerprint=interrupted.safe_fingerprint,
                ) from None
            failed_page = page
            old_origin = page_origin(failed_page)
            try:
                page = await lifecycle.resolve_replacement(failed_page)
            except Exception:
                recoveries.append(
                    page_recovery_event(
                        recovery_action,
                        cause,
                        old_origin,
                        status="failed",
                        outcome="replacement_not_found",
                    )
                )
                raise failure(
                    "video_switch_recovery_failed",
                    safe_fingerprint=interrupted.safe_fingerprint,
                ) from None
            recovery_used = True
            pending_recovery = (cause, old_origin, page)
            continue
        except VideoSwitchError as error:
            add_pulse_diagnostics(getattr(error, "pulse_diagnostics", ()))
            wheel_events += error.wheel_events
            if pending_recovery is not None:
                cause, old_origin, replacement = pending_recovery
                recoveries.append(
                    page_recovery_event(
                        recovery_action,
                        cause,
                        old_origin,
                        replacement=replacement,
                        retry=1,
                        status="failed",
                        outcome="retry_failed",
                    )
                )
            raise failure(
                error.code,
                safe_fingerprint=error.safe_fingerprint,
            ) from None
        wheel_events += pulses
        if pending_recovery is not None:
            cause, old_origin, replacement = pending_recovery
            recoveries.append(
                page_recovery_event(
                    recovery_action,
                    cause,
                    old_origin,
                    replacement=replacement,
                    retry=1,
                    status="recovered",
                    outcome="recovered",
                )
            )
            pending_recovery = None
        completed += 1
        records.append(
            {
                "from": before.safe_fingerprint,
                "to": after.safe_fingerprint,
                "wheel_events": pulses,
            }
        )
        if completed < requested:
            try:
                interval = float(rng.uniform(*interval_range))
                await asyncio.wait_for(
                    sleep_fn(interval),
                    timeout=max(interval, 0.0) + INTERVAL_GRACE_SECONDS,
                )
            except Exception:
                raise failure(
                    "video_switch_interval_failed",
                    safe_fingerprint=after.safe_fingerprint,
                ) from None
    result = {
        "count": completed,
        "distance": WHEEL_DELTA,
        "requested_switches": requested,
        "completed_switches": completed,
        "wheel_events": wheel_events,
        "switches": records,
    }
    if lifecycle is not None:
        result.update(
            {
                "_active_page": page,
                "_page_recoveries": recoveries,
            }
        )
    if diagnostic:
        result["pulse_diagnostics"] = pulse_diagnostics
    return result


__all__ = [
    "FeedState",
    "VideoSwitchError",
    "capture_feed_state",
    "execute_verified_switches",
    "switch_once",
    "wait_for_stable_changed_state",
]
