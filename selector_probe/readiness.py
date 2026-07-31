"""Bounded semantic readiness checks for probe-owned pages."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import time
from urllib.parse import urlsplit


_READINESS_SAMPLE_SCRIPT = r"""
() => {
  const visible = (node) => {
    if (!(node instanceof Element)) return false;
    const style = getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style.display !== "none" &&
      style.visibility !== "hidden" &&
      rect.width > 0 && rect.height > 0;
  };
  const firstVisible = (selectors) => {
    for (const selector of selectors) {
      for (const node of document.querySelectorAll(selector)) {
        if (visible(node)) return selector;
      }
    }
    return "";
  };
  const blockers = [
    '[data-e2e*="captcha" i]',
    'iframe[src*="captcha" i]',
    '[id*="captcha" i]',
    '[data-e2e="login-modal"]',
    '[data-e2e="login-container"]'
  ];
  const skeletons = [
    ...document.querySelectorAll(
      '[data-e2e*="skeleton" i], [class*="skeleton" i], [aria-busy="true"]'
    )
  ].filter(visible);
  const candidates = [
    ...document.querySelectorAll(
      'button, a[href], input, textarea, select, [role], [aria-label], [data-e2e]'
    )
  ].filter(visible).slice(0, 200);
  const fingerprints = candidates.map((node) => {
    const role = (
      node.getAttribute("role") ||
      node.tagName.toLowerCase()
    ).trim().toLowerCase();
    const name = (
      node.getAttribute("aria-label") ||
      node.getAttribute("placeholder") ||
      node.getAttribute("title") ||
      node.textContent ||
      ""
    ).replace(/\s+/g, " ").trim().slice(0, 120).toLowerCase();
    const stable = ["data-e2e", "aria-label", "name", "placeholder"]
      .map((key) => {
        const value = node.getAttribute(key);
        return value ? `${key}=${value.slice(0, 120).toLowerCase()}` : "";
      })
      .filter(Boolean)
      .join("|");
    return `${role}|${name}|${stable}`.slice(0, 256);
  });
  const root = document.documentElement;
  const body = document.body;
  return {
    origin: location.origin,
    ready_state: document.readyState,
    root_visible: visible(root) && visible(body),
    blocked_marker: firstVisible(blockers),
    skeleton_count: skeletons.length,
    feed_visible: Boolean(firstVisible([
      "video",
      "main",
      '[data-e2e*="feed" i]',
      '[data-e2e*="recommend" i]'
    ])),
    fingerprints
  };
}
"""


@dataclass(frozen=True)
class ReadinessToken:
    origin: str
    semantic_digest: str


class ReadinessError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def semantic_similarity(
    left: frozenset[str],
    right: frozenset[str],
) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _origin(value: object) -> str:
    parsed = urlsplit(str(value or ""))
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not hostname:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    default_port = 443 if parsed.scheme == "https" else 80
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme}://{hostname}{suffix}"


def _sample(value: object, expected_origin: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ReadinessError("probe_readiness_invalid")
    origin = _origin(value.get("origin"))
    if origin != expected_origin:
        raise ReadinessError("probe_origin_mismatch")
    blocked = value.get("blocked_marker")
    blocked_marker = (
        str(blocked)[:64]
        if isinstance(blocked, str) and blocked
        else ""
    )
    raw_fingerprints = value.get("fingerprints")
    if not isinstance(raw_fingerprints, list):
        raise ReadinessError("probe_readiness_invalid")
    fingerprints = frozenset(
        item
        for item in raw_fingerprints[:200]
        if isinstance(item, str)
        and 1 <= len(item) <= 256
        and not any(ord(character) < 32 for character in item)
    )
    skeleton_count = value.get("skeleton_count")
    if (
        isinstance(skeleton_count, bool)
        or not isinstance(skeleton_count, int)
        or skeleton_count < 0
    ):
        raise ReadinessError("probe_readiness_invalid")
    return {
        "blocked_marker": blocked_marker,
        "skeleton_count": min(skeleton_count, 10_000),
        "fingerprints": fingerprints,
        "structurally_ready": (
            value.get("ready_state") in {"interactive", "complete"}
            and value.get("root_visible") is True
            and value.get("feed_visible") is True
            and bool(fingerprints)
        ),
    }


async def wait_for_semantic_readiness(
    page,
    *,
    expected_origin: str,
    timeout_seconds: float = 60.0,
    poll_interval_seconds: float = 1.0,
    required_skeleton_clear_samples: int = 3,
    required_stable_samples: int = 2,
    similarity_threshold: float = 0.85,
    sleep_fn=asyncio.sleep,
    monotonic_fn=time.monotonic,
) -> tuple[ReadinessToken, dict[str, object]]:
    deadline = monotonic_fn() + timeout_seconds
    skeleton_clear = 0
    stable = 0
    previous = frozenset()
    last: dict[str, object] = {}
    while monotonic_fn() <= deadline:
        last = _sample(
            await page.evaluate(_READINESS_SAMPLE_SCRIPT),
            expected_origin,
        )
        if last["blocked_marker"]:
            raise ReadinessError("probe_page_blocked")
        skeleton_clear = (
            skeleton_clear + 1
            if last["skeleton_count"] == 0
            else 0
        )
        current = last["fingerprints"]
        stable = (
            stable + 1
            if previous
            and semantic_similarity(previous, current)
            >= similarity_threshold
            else 1
        )
        previous = current
        if (
            last["structurally_ready"]
            and skeleton_clear >= required_skeleton_clear_samples
            and stable >= required_stable_samples
        ):
            digest = "sha256:" + hashlib.sha256(
                "\n".join(sorted(current)).encode("utf-8")
            ).hexdigest()
            token = ReadinessToken(expected_origin, digest)
            return token, {
                "ready": True,
                "origin": expected_origin,
                "title_or_root": True,
                "blocked_marker": None,
                "skeleton_timed_out": False,
                "semantic_count": len(current),
                "semantic_digest": digest,
                "readiness_token": token,
            }
        await sleep_fn(poll_interval_seconds)
    raise ReadinessError("page_readiness_timeout")


__all__ = [
    "ReadinessError",
    "ReadinessToken",
    "semantic_similarity",
    "wait_for_semantic_readiness",
]
