"""Capture and replay a validated physical wheel gesture for Browser V2."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import math
import statistics
import time
from collections.abc import Awaitable, Callable
from typing import Any

from browser_video_switch import FeedState, capture_feed_state


_STATE_KEY = "__codexV2WheelCalibration"
_ACTIVE_STATUSES = {"preparing", "waiting_for_sample", "validating", "cancelling"}
_DRY_RUN_MULTIPLIERS = (1.0, 1.5, 2.0, 3.0)


class WheelCalibrationError(RuntimeError):
    """A fixed, public-safe wheel calibration failure."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


def _number(value: Any, *, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise WheelCalibrationError("wheel_calibration_inconsistent")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise WheelCalibrationError("wheel_calibration_inconsistent") from error
    if not math.isfinite(result) or minimum is not None and result < minimum:
        raise WheelCalibrationError("wheel_calibration_inconsistent")
    return result


def normalize_wheel_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate three single-video gestures and retain one real median gesture."""

    if not isinstance(samples, list) or len(samples) != 3:
        raise WheelCalibrationError("wheel_calibration_inconsistent")
    event_groups: list[list[dict[str, float | int]]] = []
    totals: list[float] = []
    for sample in samples:
        if not isinstance(sample, dict) or sample.get("direction") != "down":
            raise WheelCalibrationError("wheel_calibration_inconsistent")
        transitions = sample.get("identity_transitions")
        if transitions == 0:
            raise WheelCalibrationError("wheel_calibration_video_not_changed")
        if transitions != 1:
            raise WheelCalibrationError("wheel_calibration_multiple_videos")
        raw_events = sample.get("events")
        if not isinstance(raw_events, list) or not raw_events:
            raise WheelCalibrationError("wheel_calibration_inconsistent")
        events: list[dict[str, float | int]] = []
        for raw in raw_events:
            if not isinstance(raw, dict):
                raise WheelCalibrationError("wheel_calibration_inconsistent")
            if raw.get("delta_mode") != 0:
                raise WheelCalibrationError("wheel_calibration_unsupported_delta_mode")
            delta_x = _number(raw.get("delta_x"))
            delta_y = _number(raw.get("delta_y"))
            delay_ms = _number(raw.get("delay_ms"), minimum=0)
            if delta_y <= 0:
                raise WheelCalibrationError("wheel_calibration_inconsistent")
            events.append(
                {
                    "delta_x": delta_x,
                    "delta_y": delta_y,
                    "delta_mode": 0,
                    "delay_ms": delay_ms,
                }
            )
        event_groups.append(events)
        totals.append(sum(float(event["delta_y"]) for event in events))
    median_total = statistics.median(totals)
    if median_total <= 0 or any(
        abs(total - median_total) > abs(median_total) * 0.20 for total in totals
    ):
        raise WheelCalibrationError("wheel_calibration_inconsistent")
    selected_index = min(
        range(len(totals)),
        key=lambda index: (abs(totals[index] - median_total), index),
    )
    selected = [dict(event) for event in event_groups[selected_index]]
    return {"direction": "down", "events": selected, "sample_count": 3}


def _sample_metric(sample: dict[str, Any]) -> dict[str, float | int]:
    raw_events = sample.get("events") if isinstance(sample, dict) else None
    events = raw_events if isinstance(raw_events, list) else []
    total_delta = 0.0
    for event in events:
        if not isinstance(event, dict):
            continue
        try:
            value = float(event.get("delta_y", 0))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            total_delta += value
    return {"event_count": len(events), "total_delta": round(total_delta, 3)}


async def _confirm_original_feed_stable(
    page: Any,
    before: FeedState,
    *,
    sleep_fn: Callable[[float], Awaitable[None]],
) -> None:
    first = await capture_feed_state(page)
    await sleep_fn(0.1)
    second = await capture_feed_state(page)
    if first.fingerprint != before.fingerprint or second.fingerprint != before.fingerprint:
        raise WheelCalibrationError("wheel_calibration_replay_unstable")


async def dry_run_wheel_calibration(
    page: Any,
    normalized: dict[str, Any],
    progress: Callable[[dict[str, Any]], Awaitable[None]],
    cancel_event: asyncio.Event,
    *,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    """Find one replayable wheel delta using bounded single-event attempts."""

    events = normalized.get("events") if isinstance(normalized, dict) else None
    if not isinstance(events, list) or not events:
        raise WheelCalibrationError("wheel_calibration_inconsistent")
    base_delta = sum(_number(event.get("delta_y")) for event in events)
    if base_delta <= 0:
        raise WheelCalibrationError("wheel_calibration_inconsistent")
    results: list[dict[str, Any]] = []
    for offset, multiplier in enumerate(_DRY_RUN_MULTIPLIERS):
        if cancel_event.is_set():
            raise asyncio.CancelledError
        candidate_index = offset + 1
        candidate_delta = float(base_delta * multiplier)
        before = await capture_feed_state(page)
        await page.mouse.move(
            before.container_x + before.container_width / 2,
            before.container_y + before.container_height / 2,
        )
        state = {
            "status": "dry_run",
            "candidate_index": candidate_index,
            "candidate_multiplier": multiplier,
            "candidate_delta_y": candidate_delta,
            "candidate_results": list(results),
        }
        await progress(state)
        side_effect_done = asyncio.Event()
        observer = asyncio.create_task(
            observe_single_transition(
                page,
                before,
                side_effect_done=side_effect_done,
                sleep_fn=sleep_fn,
            )
        )
        try:
            await page.mouse.wheel(0.0, candidate_delta)
        except BaseException:
            observer.cancel()
            with suppress(asyncio.CancelledError):
                await observer
            raise
        finally:
            side_effect_done.set()
        try:
            await observer
        except WheelCalibrationError as error:
            if error.code != "wheel_calibration_video_not_changed":
                raise
            await _confirm_original_feed_stable(page, before, sleep_fn=sleep_fn)
            results.append(
                {
                    "candidate_index": candidate_index,
                    "multiplier": multiplier,
                    "delta_y": candidate_delta,
                    "result": "not_observed",
                }
            )
            await progress({**state, "candidate_results": list(results)})
            continue
        results.append(
            {
                "candidate_index": candidate_index,
                "multiplier": multiplier,
                "delta_y": candidate_delta,
                "result": "passed",
            }
        )
        await progress({**state, "candidate_results": list(results)})
        return {
            "direction": "down",
            "events": [
                {
                    "delta_x": 0.0,
                    "delta_y": candidate_delta,
                    "delta_mode": 0,
                    "delay_ms": 0.0,
                }
            ],
            "sample_count": 3,
            "replay_validated": True,
        }
    raise WheelCalibrationError("wheel_calibration_replay_not_observed")


def _recorder_script() -> str:
    key = json.dumps(_STATE_KEY)
    return f"""(() => {{
      const key = {key};
      const previous = window[key];
      if (previous && typeof previous.cleanup === 'function') previous.cleanup();
      const state = {{gestures: [], current: null, idleTimer: null, monitorTimer: null}};
      const identity = () => {{
        const container = document.querySelector('#column-list-container');
        if (!container) return '';
        const rect = container.getBoundingClientRect();
        const centerY = rect.top + rect.height / 2;
        const articles = Array.from(container.querySelectorAll('article'));
        const article = articles.find(item => {{
          const box = item.getBoundingClientRect();
          return box.width > 0 && box.height > 0 && box.top <= centerY && box.bottom >= centerY;
        }});
        if (!article) return '';
        const href = Array.from(article.querySelectorAll('a[href]'))
          .map(link => String(link.getAttribute('href') || ''))
          .find(value => /\\/video\\/\\d+/.test(value));
        const match = href && href.match(/\\/video\\/(\\d+)/);
        if (match) return `video:${{match[1]}}`;
        return `article:${{String(article.id || '')}}:${{articles.indexOf(article)}}`;
      }};
      const finish = transitions => {{
        if (!state.current) return;
        clearTimeout(state.idleTimer);
        clearInterval(state.monitorTimer);
        state.gestures.push({{
          direction: state.current.totalY > 0 ? 'down' : 'up',
          identity_transitions: transitions,
          events: state.current.events
        }});
        state.current = null;
        state.idleTimer = null;
        state.monitorTimer = null;
      }};
      const startMonitor = () => {{
        const started = performance.now();
        state.monitorTimer = setInterval(() => {{
          if (!state.current) return;
          const currentIdentity = identity();
          if (currentIdentity && currentIdentity !== state.current.before
              && !state.current.identities.includes(currentIdentity)) {{
            state.current.identities.push(currentIdentity);
            state.current.stableCount = 1;
          }} else if (state.current.identities.length
              && currentIdentity === state.current.identities[state.current.identities.length - 1]) {{
            state.current.stableCount += 1;
          }} else if (state.current.identities.length) {{
            state.current.stableCount = 0;
          }}
          if (state.current.identities.length > 1) finish(state.current.identities.length);
          else if (state.current.idle && state.current.stableCount >= 2) finish(1);
          else if (state.current.idle && performance.now() - started >= 8000) finish(0);
        }}, 50);
      }};
      const onWheel = event => {{
        if (!event.isTrusted) return;
        if (state.current && state.current.idle) return;
        const now = performance.now();
        if (!state.current) {{
          state.current = {{before: identity(), events: [], identities: [], stableCount: 0, totalY: 0, idle: false}};
          startMonitor();
        }}
        const events = state.current.events;
        const previousAt = events.length ? events[events.length - 1].at_ms : now;
        events.push({{
          delta_x: Number(event.deltaX), delta_y: Number(event.deltaY),
          delta_mode: Number(event.deltaMode), delay_ms: events.length ? now - previousAt : 0,
          at_ms: now
        }});
        state.current.totalY += Number(event.deltaY);
        clearTimeout(state.idleTimer);
        state.idleTimer = setTimeout(() => {{ if (state.current) state.current.idle = true; }}, 180);
      }};
      const drain = () => state.gestures.splice(0).map(gesture => ({{
        ...gesture,
        events: gesture.events.map(({{at_ms, ...event}}) => event)
      }}));
      const cleanup = () => {{
        clearTimeout(state.idleTimer);
        clearInterval(state.monitorTimer);
        document.removeEventListener('wheel', onWheel, true);
        if (window[key] === state) delete window[key];
      }};
      Object.assign(state, {{drain, cleanup}});
      document.addEventListener('wheel', onWheel, true);
      window[key] = state;
      return true;
    }})()"""


class WheelCalibrationRunner:
    """Own the short-lived page recorder for one calibration Profile."""

    def __init__(
        self,
        *,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sleep = sleep_fn
        self._clock = clock

    async def prepare(self, page: Any) -> None:
        deadline = self._clock() + 45
        while True:
            try:
                await capture_feed_state(page)
                break
            except Exception as error:
                if self._clock() >= deadline:
                    raise WheelCalibrationError(
                        "wheel_calibration_context_lost"
                    ) from error
                await self._sleep(0.5)
        prepared = await page.evaluate(_recorder_script())
        if prepared is not True:
            raise WheelCalibrationError("wheel_calibration_context_lost")

    async def collect(
        self,
        page: Any,
        progress: Callable[[dict[str, Any]], Awaitable[None]],
        cancel_event: asyncio.Event,
    ) -> dict[str, Any]:
        samples: list[dict[str, Any]] = []
        sample_metrics: list[dict[str, float | int]] = []
        try:
            while len(samples) < 3:
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                await progress(
                    {
                        "status": "waiting_for_sample",
                        "sample_index": len(samples),
                        "samples": ["passed"] * len(samples)
                        + ["waiting"]
                        + ["pending"] * (2 - len(samples)),
                        "sample_metrics": list(sample_metrics),
                    }
                )
                incoming = await self._wait_for_gesture(page, cancel_event)
                sample = incoming[0]
                samples.append(sample)
                sample_metrics.append(_sample_metric(sample))
                await progress(
                    {
                        "status": "validating",
                        "sample_index": len(samples),
                        "samples": ["passed"] * len(samples)
                        + ["pending"] * (3 - len(samples)),
                        "sample_metrics": list(sample_metrics),
                    }
                )
            normalized = normalize_wheel_samples(samples)
            return await dry_run_wheel_calibration(
                page,
                normalized,
                progress,
                cancel_event,
                sleep_fn=self._sleep,
            )
        finally:
            await self.cleanup(page)

    async def _wait_for_gesture(
        self, page: Any, cancel_event: asyncio.Event
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + 120
        expression = (
            f"() => window[{json.dumps(_STATE_KEY)}]"
            f" && window[{json.dumps(_STATE_KEY)}].drain()"
        )
        while time.monotonic() < deadline:
            if cancel_event.is_set():
                raise asyncio.CancelledError
            try:
                gestures = await page.evaluate(expression)
            except Exception as error:
                raise WheelCalibrationError("wheel_calibration_context_lost") from error
            if isinstance(gestures, list) and gestures:
                return gestures
            await asyncio.sleep(0.1)
        raise WheelCalibrationError("wheel_calibration_context_lost")

    async def cleanup(self, page: Any) -> None:
        expression = f"""() => {{
          const state = window[{json.dumps(_STATE_KEY)}];
          if (state && typeof state.cleanup === 'function') state.cleanup();
          return true;
        }}"""
        try:
            await page.evaluate(expression)
        except Exception:
            pass


async def observe_single_transition(
    page: Any,
    before: FeedState,
    *,
    side_effect_done: asyncio.Event,
    timeout: float = 8.0,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> FeedState:
    """Observe exactly one identity transition, stable after dispatch completes."""

    deadline = time.monotonic() + timeout
    seen: list[str] = []
    stable: FeedState | None = None
    while time.monotonic() < deadline:
        current = await capture_feed_state(page)
        if current.fingerprint == before.fingerprint:
            stable = None
        else:
            if current.fingerprint not in seen:
                seen.append(current.fingerprint)
            if len(seen) > 1:
                raise WheelCalibrationError("wheel_calibration_multiple_videos")
            if (
                side_effect_done.is_set()
                and stable is not None
                and stable.fingerprint == current.fingerprint
            ):
                return current
            stable = current
        await sleep_fn(0.05)
    raise WheelCalibrationError("wheel_calibration_video_not_changed")


async def execute_calibrated_switches(
    page: Any,
    calibration: dict[str, Any],
    *,
    direction: str,
    requested: int,
    interval_range: list[float] | list[int],
    rng: Any,
    sleep_fn: Callable[[float], Awaitable[None]],
) -> dict[str, Any]:
    """Replay one calibrated group per requested switch, without retries."""

    if direction not in {"up", "down"}:
        raise ValueError("direction must be up or down")
    if not isinstance(calibration, dict) or not isinstance(calibration.get("events"), list):
        raise WheelCalibrationError("wheel_calibration_missing")
    events = calibration["events"]
    completed = 0
    wheel_events = 0
    records: list[dict[str, Any]] = []
    for index in range(int(requested)):
        before = await capture_feed_state(page)
        await page.mouse.move(
            before.container_x + before.container_width / 2,
            before.container_y + before.container_height / 2,
        )
        side_effect_done = asyncio.Event()
        observer = asyncio.create_task(
            observe_single_transition(
                page, before, side_effect_done=side_effect_done, sleep_fn=sleep_fn
            )
        )
        try:
            for event in events:
                delay = _number(event.get("delay_ms"), minimum=0)
                if delay:
                    await sleep_fn(delay / 1000)
                delta_y = _number(event.get("delta_y"))
                if direction == "up":
                    delta_y = -delta_y
                await page.mouse.wheel(_number(event.get("delta_x")), delta_y)
                wheel_events += 1
        except BaseException:
            observer.cancel()
            with suppress(asyncio.CancelledError):
                await observer
            raise
        finally:
            side_effect_done.set()
        try:
            after = await observer
        except WheelCalibrationError as error:
            if error.code == "wheel_calibration_video_not_changed":
                raise WheelCalibrationError("calibrated_video_switch_not_observed") from error
            raise
        completed += 1
        records.append(
            {
                "from": before.safe_fingerprint,
                "to": after.safe_fingerprint,
                "wheel_events": len(events),
            }
        )
        if index + 1 < int(requested):
            await sleep_fn(rng.uniform(float(interval_range[0]), float(interval_range[1])))
    return {
        "requested_switches": int(requested),
        "completed_switches": completed,
        "wheel_events": wheel_events,
        "distance": int(
            round(sum(abs(float(event["delta_y"])) for event in events))
        ),
        "switches": records,
        "calibration_revision": int(calibration["revision"]),
    }


__all__ = [
    "WheelCalibrationError",
    "WheelCalibrationRunner",
    "dry_run_wheel_calibration",
    "execute_calibrated_switches",
    "normalize_wheel_samples",
    "observe_single_transition",
]
