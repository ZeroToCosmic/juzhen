"""Inject and collect privacy-preserving behavior patterns through CDP."""

from __future__ import annotations

import json
import math
from typing import Any

from browser_cdp import CdpClient


CONTEXT_INVALID = "录制上下文已失效"
_STATE_NAME = "__codexPatternRecorderV1"


def _number(value: Any, description: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{description} 必须是数字")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{description} 必须是数字") from error
    if not math.isfinite(result):
        raise ValueError(f"{description} 必须是有限数字")
    return result


def _duration(raw: dict, last_sample_time: float) -> tuple[float, float]:
    started = _number(raw.get("started_at_ms"), "录制开始时间")
    stopped = _number(raw.get("stopped_at_ms", last_sample_time), "录制结束时间")
    if stopped < started or last_sample_time < started or stopped < last_sample_time:
        raise ValueError("录制时间顺序无效")
    return started, stopped - started


def normalize_recording_sample(raw) -> dict:
    """Convert transient page samples to the persisted pattern data schema."""

    if not isinstance(raw, dict):
        raise ValueError("录制数据格式无效")
    pattern_type = str(raw.get("type") or "").strip()

    if pattern_type == "mouse":
        if "points" in raw:
            source_points = raw.get("points")
            viewport = raw.get("viewport")
            if not isinstance(source_points, list) or not isinstance(viewport, dict):
                raise ValueError("鼠标录制样本格式无效")
            if len(source_points) < 2:
                raise ValueError("鼠标录制样本不足，至少需要 2 个移动点")
            width = _number(viewport.get("width"), "录制视口宽度")
            height = _number(viewport.get("height"), "录制视口高度")
            if width <= 0 or height <= 0:
                raise ValueError("录制视口尺寸必须大于 0")
            points = []
            total_duration = 0.0
            for point in source_points:
                if not isinstance(point, dict):
                    raise ValueError("鼠标录制样本格式无效")
                dt_ms = _number(point.get("dt_ms"), "鼠标采样间隔")
                if dt_ms < 0:
                    raise ValueError("鼠标采样间隔不能小于 0")
                total_duration += dt_ms
                points.append(
                    {
                        "x_ratio": round(min(max(_number(point.get("x"), "鼠标横坐标") / width, 0.0), 1.0), 6),
                        "y_ratio": round(min(max(_number(point.get("y"), "鼠标纵坐标") / height, 0.0), 1.0), 6),
                        "dt_ms": dt_ms,
                    }
                )
            return {
                "points": points,
                "sample_count": len(points),
                "total_duration_ms": round(total_duration, 3),
            }
        samples = raw.get("samples")
        if not isinstance(samples, list):
            raise ValueError("录制样本格式无效")
        if len(samples) < 2:
            raise ValueError("鼠标录制样本不足，至少需要 2 个移动点")
        viewport = raw.get("viewport")
        if not isinstance(viewport, dict):
            raise ValueError("鼠标录制视口格式无效")
        width = _number(viewport.get("width"), "录制视口宽度")
        height = _number(viewport.get("height"), "录制视口高度")
        if width <= 0 or height <= 0:
            raise ValueError("录制视口尺寸必须大于 0")
        first_time = _number(samples[0].get("at_ms") if isinstance(samples[0], dict) else None, "鼠标采样时间")
        started, total_duration = _duration(raw, _number(samples[-1].get("at_ms") if isinstance(samples[-1], dict) else None, "鼠标采样时间"))
        previous_time = started
        points = []
        for sample in samples:
            if not isinstance(sample, dict):
                raise ValueError("鼠标录制样本格式无效")
            x = _number(sample.get("x"), "鼠标横坐标")
            y = _number(sample.get("y"), "鼠标纵坐标")
            at_ms = _number(sample.get("at_ms"), "鼠标采样时间")
            if at_ms < previous_time:
                raise ValueError("鼠标采样时间顺序无效")
            points.append(
                {
                    "x_ratio": round(min(max(x / width, 0.0), 1.0), 6),
                    "y_ratio": round(min(max(y / height, 0.0), 1.0), 6),
                    "dt_ms": round(at_ms - previous_time, 3),
                }
            )
            previous_time = at_ms
        # Keep the explicit read so malformed first samples fail before output.
        if first_time < started:
            raise ValueError("鼠标采样时间顺序无效")
        return {
            "points": points,
            "sample_count": len(points),
            "total_duration_ms": round(total_duration, 3),
        }

    if pattern_type == "keyboard":
        if "events" in raw:
            events = raw.get("events")
            if not isinstance(events, list):
                raise ValueError("键盘录制样本格式无效")
            if len(events) < 2:
                raise ValueError("键盘录制样本不足，至少需要 2 次有效按键")
            intervals = []
            holds = []
            for event in events:
                if not isinstance(event, dict):
                    raise ValueError("键盘录制样本格式无效")
                interval = _number(event.get("interval_ms"), "按键间隔")
                hold = _number(event.get("hold_ms"), "按键按下时长")
                if interval < 0 or hold < 0:
                    raise ValueError("键盘录制时间不能小于 0")
                intervals.append(interval)
                holds.append(hold)
            # Compatibility boundary for timing-only callers: content-bearing
            # fields such as key/code/text are deliberately not projected.
            return {
                "intervals_ms": intervals,
                "hold_ms": holds,
                "sample_count": len(intervals),
            }
        samples = raw.get("samples")
        if not isinstance(samples, list):
            raise ValueError("录制样本格式无效")
        if len(samples) < 2:
            raise ValueError("键盘录制样本不足，至少需要 2 次有效按键")
        last = samples[-1]
        if not isinstance(last, dict):
            raise ValueError("键盘录制样本格式无效")
        started, total_duration = _duration(raw, _number(last.get("up_at_ms"), "按键抬起时间"))
        previous_down = started
        sequences = set()
        intervals = []
        holds = []
        for sample in samples:
            if not isinstance(sample, dict):
                raise ValueError("键盘录制样本格式无效")
            if not {"sequence", "down_at_ms", "up_at_ms"}.issubset(sample):
                raise ValueError("键盘录制样本格式无效")
            sequence = sample["sequence"]
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
                raise ValueError("按键事件序号无效")
            if sequence in sequences:
                raise ValueError("按键事件序号重复")
            sequences.add(sequence)
            down = _number(sample["down_at_ms"], "按键按下时间")
            up = _number(sample["up_at_ms"], "按键抬起时间")
            if down < previous_down or up < down:
                raise ValueError("键盘采样时间顺序无效")
            intervals.append(round(down - previous_down, 3))
            holds.append(round(up - down, 3))
            previous_down = down
        return {
            "intervals_ms": intervals,
            "hold_ms": holds,
            "sample_count": len(intervals),
            "total_duration_ms": round(total_duration, 3),
        }

    raise ValueError("录制类型必须是 mouse 或 keyboard")


def _evaluate(client: CdpClient, session_id: str, expression: str):
    result = client.command(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
        session_id,
    )
    if result.get("exceptionDetails"):
        raise RuntimeError(result["exceptionDetails"].get("text") or CONTEXT_INVALID)
    return result.get("result", {}).get("value")


def _with_page(ws_url: str, expression: str):
    client = CdpClient(ws_url)
    try:
        session_id, _pages = client.page_session()
        return _evaluate(client, session_id, expression)
    finally:
        client.close()


def _prepare_expression(recording_id: str, pattern_type: str) -> str:
    recording_json = json.dumps(recording_id)
    type_json = json.dumps(pattern_type)
    state_json = json.dumps(_STATE_NAME)
    return f"""(() => {{
      const recordingId = {recording_json};
      const patternType = {type_json};
      const stateName = {state_json};
      const store = window[stateName] || Object.create(null);
      const old = store[recordingId];
      if (old && typeof old.cleanup === 'function') old.cleanup();
      delete store[recordingId];

      const host = document.createElement('div');
      host.setAttribute('data-codex-pattern-recorder', '');
      host.style.cssText = 'all:initial;position:fixed;right:18px;top:18px;z-index:2147483647';
      const shadow = host.attachShadow({{mode: 'closed'}});
      const panel = document.createElement('div');
      panel.style.cssText = 'font:14px sans-serif;background:#102a24;color:#fff;padding:12px;border-radius:10px;box-shadow:0 4px 18px #0005;display:flex;gap:8px;align-items:center';
      const startButton = document.createElement('button');
      startButton.textContent = '开始录制';
      const stopButton = document.createElement('button');
      stopButton.textContent = '结束录制';
      const statusLabel = document.createElement('span');
      statusLabel.textContent = '等待开始 · Ctrl+Shift+F10 结束';
      for (const button of [startButton, stopButton]) button.style.cssText = 'font:14px sans-serif;padding:6px 10px;cursor:pointer';
      panel.append(startButton, stopButton, statusLabel);
      shadow.append(panel);
      (document.documentElement || document.body).append(host);

      const state = {{
        recording_id: recordingId,
        type: patternType,
        status: 'ready',
        samples: [],
        pending: [],
        sequence: 0,
        started_at_ms: null,
        stopped_at_ms: null,
        viewport: {{width: Math.max(window.innerWidth || 1, 1), height: Math.max(window.innerHeight || 1, 1)}},
        host
      }};
      const now = () => performance.now();
      const isOverlayEvent = event => event.composedPath().includes(host);
      const stop = () => {{
        if (state.status !== 'recording') return;
        state.stopped_at_ms = now();
        for (const sample of state.pending) if (sample.up_at_ms === null) sample.up_at_ms = state.stopped_at_ms;
        state.pending.length = 0;
        state.status = 'stopped';
        statusLabel.textContent = '录制已结束';
      }};
      const start = () => {{
        state.samples.length = 0;
        state.pending.length = 0;
        state.sequence = 0;
        state.started_at_ms = now();
        state.stopped_at_ms = null;
        state.viewport = {{width: Math.max(window.innerWidth || 1, 1), height: Math.max(window.innerHeight || 1, 1)}};
        state.status = 'recording';
        statusLabel.textContent = '正在录制 · Ctrl+Shift+F10 结束';
      }};
      const onPointerMove = event => {{
        if (state.status !== 'recording' || state.type !== 'mouse' || isOverlayEvent(event)) return;
        state.samples.push({{x: event.clientX, y: event.clientY, at_ms: now()}});
      }};
      const onKeyDown = event => {{
        if (isOverlayEvent(event)) return;
        const isStopShortcut = event.ctrlKey && event.shiftKey && !event.altKey && !event.metaKey && (event.key === 'F10' || event.keyCode === 121);
        if (isStopShortcut) {{ event.preventDefault(); event.stopPropagation(); stop(); return; }}
        if (state.status !== 'recording' || state.type !== 'keyboard' || event.ctrlKey || event.altKey || event.metaKey || event.repeat) return;
        const sample = {{sequence: ++state.sequence, down_at_ms: now(), up_at_ms: null}};
        state.samples.push(sample);
        state.pending.push(sample);
      }};
      const onKeyUp = event => {{
        if (isOverlayEvent(event) || state.status !== 'recording' || state.type !== 'keyboard' || event.ctrlKey || event.altKey || event.metaKey) return;
        const sample = state.pending.shift();
        if (sample) sample.up_at_ms = now();
      }};
      const cleanup = () => {{
        document.removeEventListener('pointermove', onPointerMove, true);
        document.removeEventListener('keydown', onKeyDown, true);
        document.removeEventListener('keyup', onKeyUp, true);
        startButton.removeEventListener('click', start);
        stopButton.removeEventListener('click', stop);
        host.remove();
      }};
      const snapshot = () => ({{recording_id: state.recording_id, type: state.type, status: state.status, sample_count: state.samples.length}});
      const exportRaw = () => ({{
        recording_id: state.recording_id,
        type: state.type,
        status: state.status,
        viewport: state.viewport,
        started_at_ms: state.started_at_ms,
        stopped_at_ms: state.stopped_at_ms,
        samples: state.samples
      }});
      Object.assign(state, {{cleanup, stop, snapshot, exportRaw}});
      startButton.addEventListener('click', start);
      stopButton.addEventListener('click', stop);
      document.addEventListener('pointermove', onPointerMove, true);
      document.addEventListener('keydown', onKeyDown, true);
      document.addEventListener('keyup', onKeyUp, true);
      store[recordingId] = state;
      window[stateName] = store;
      return snapshot();
    }})()"""


def prepare_recording(ws_url, recording_id, pattern_type) -> dict:
    recording_id = str(recording_id or "").strip()
    pattern_type = str(pattern_type or "").strip()
    if not ws_url or not recording_id:
        raise ValueError("录制窗口和 recording_id 不能为空")
    if pattern_type not in {"mouse", "keyboard"}:
        raise ValueError("录制类型必须是 mouse 或 keyboard")
    result = _with_page(ws_url, _prepare_expression(recording_id, pattern_type))
    if not isinstance(result, dict) or result.get("recording_id") != recording_id:
        raise RuntimeError("录制控件注入失败")
    return result


def _state_expression(recording_id: str, operation: str) -> str:
    recording_json = json.dumps(str(recording_id or "").strip())
    state_json = json.dumps(_STATE_NAME)
    if operation == "read":
        action = "return state.snapshot();"
    else:
        action = """
          state.stop();
          const raw = state.exportRaw();
          state.cleanup();
          delete store[state.recording_id];
          if (Object.keys(store).length === 0) delete window[stateName];
          return raw;
        """
    return f"""(() => {{
      const stateName = {state_json};
      const store = window[stateName];
      const state = store && store[{recording_json}];
      if (!state || state.recording_id !== {recording_json}) return null;
      {action}
    }})()"""


def read_recording(ws_url, recording_id) -> dict:
    try:
        result = _with_page(ws_url, _state_expression(recording_id, "read"))
    except Exception as error:
        raise RuntimeError(CONTEXT_INVALID) from error
    if not isinstance(result, dict):
        raise RuntimeError(CONTEXT_INVALID)
    return result


def finish_recording(ws_url, recording_id) -> dict:
    try:
        raw = _with_page(ws_url, _state_expression(recording_id, "finish"))
    except Exception as error:
        raise RuntimeError(CONTEXT_INVALID) from error
    if not isinstance(raw, dict):
        raise RuntimeError(CONTEXT_INVALID)
    return {
        "recording_id": str(recording_id),
        "type": raw.get("type"),
        "status": "finished",
        "sample": normalize_recording_sample(raw),
    }


__all__ = [
    "CONTEXT_INVALID",
    "prepare_recording",
    "read_recording",
    "finish_recording",
    "normalize_recording_sample",
]
